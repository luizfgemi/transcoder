from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.daemon import DispatcherDaemon
from app.database import Database
from app.domain import PlanSource, PlanState
from app.policy import Policy
from app.media import Disposition, MediaProbe, Stream
from app.engine import SafePromoter
from app.media import fingerprint


def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "state.sqlite")
    db.initialize()
    return db


def make_daemon(
    db: Database,
    cache_root: Path | None,
    *,
    probe_runner=None,
    retry_limit: int = 2,
    retry_backoff_base_seconds: int = 60,
    retry_backoff_multiplier: int = 4,
    failure_retry_cooldown_days: int = 7,
) -> DispatcherDaemon:
    return DispatcherDaemon(
        database=db,
        queue=None,
        catalog=None,
        scanner=None,
        scheduler=None,
        outbox=None,
        job_runner=None,
        execution_enabled=True,
        automatic_scan_enabled=False,
        interval_seconds=5,
        cache_root=cache_root,
        probe_runner=probe_runner,
        retry_limit=retry_limit,
        retry_backoff_base_seconds=retry_backoff_base_seconds,
        retry_backoff_multiplier=retry_backoff_multiplier,
        failure_retry_cooldown_days=failure_retry_cooldown_days,
    )


def transcode_plan(db: Database, path: Path, source: MediaProbe, plan) -> str:
    media_id = db.upsert_media_file(
        path=str(path), library="movies", size=3, mtime_ns=1, fingerprint="fp",
        arr_type="radarr", arr_media_id=1, arr_file_id=2,
    )
    plan_id = db.create_plan(
        media_file_id=media_id,
        source=PlanSource.SCAN,
        priority=30,
        actions={
            "stage": "transcode",
            "path": str(path),
            "fingerprint": "fp",
            "probe": source.to_dict(),
            "plan": plan.to_dict(),
            "arr": {"arr_type": "radarr", "media_id": 1, "file_id": 2, "preferred_language": "eng"},
        },
        state=PlanState.QUEUED,
    )
    db.claim_next("worker")
    return plan_id


def probes():
    source = MediaProbe(
        path="source",
        format_names=("matroska",),
        duration_seconds=10,
        size=3,
        streams=(
            Stream(index=0, codec_type="video", codec_name="hevc"),
            Stream(
                index=1,
                codec_type="audio",
                codec_name="dts",
                channels=6,
                language="eng",
                disposition=Disposition(default=True),
            ),
        ),
    )
    output = MediaProbe(
        path="output",
        format_names=("matroska",),
        duration_seconds=10,
        size=3,
        streams=(
            Stream(index=0, codec_type="video", codec_name="hevc"),
            Stream(
                index=1,
                codec_type="audio",
                codec_name="eac3",
                channels=6,
                language="eng",
                disposition=Disposition(default=True),
            ),
        ),
    )
    return source, output, Policy().evaluate(source, "eng")


class StubProbe:
    def __init__(self, output: MediaProbe) -> None:
        self.output = output

    def probe(self, path: Path) -> MediaProbe:
        return replace(self.output, path=str(path), size=path.stat().st_size)


class FailingProbe:
    def probe(self, path: Path) -> MediaProbe:
        raise RuntimeError("probe failed")


def test_fail_plan_schedules_quick_retry_then_permanent_failure(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    daemon = make_daemon(db, None)
    source, _, plan = probes()
    plan_id = transcode_plan(db, tmp_path / "movie.mkv", source, plan)

    daemon._fail_plan(plan_id, RuntimeError("boom"))
    row = db.plan(plan_id)
    assert row["state"] == "retry_wait"
    assert row["attempt_count"] == 1
    assert row["last_error"] == "RuntimeError: boom"
    delay = datetime.fromisoformat(row["next_attempt_at"]) - datetime.now(UTC)
    assert 59 < delay.total_seconds() < 61

    db.transition_plan(plan_id, PlanState.QUEUED, expected=PlanState.RETRY_WAIT)
    db.claim_next("worker")
    daemon._fail_plan(plan_id, RuntimeError("boom again"))
    row = db.plan(plan_id)
    assert row["state"] == "retry_wait"
    assert row["attempt_count"] == 2
    delay = datetime.fromisoformat(row["next_attempt_at"]) - datetime.now(UTC)
    assert 239 < delay.total_seconds() < 241

    db.transition_plan(plan_id, PlanState.QUEUED, expected=PlanState.RETRY_WAIT)
    db.claim_next("worker")
    daemon._fail_plan(plan_id, RuntimeError("still failing"))
    row = db.plan(plan_id)
    assert row["state"] == "failed"
    assert row["attempt_count"] == 3
    cooldown = datetime.fromisoformat(row["next_attempt_at"]) - datetime.now(UTC)
    assert timedelta(days=6) < cooldown < timedelta(days=8)


def test_fail_plan_custom_backoff_configuration(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    daemon = make_daemon(
        db, None, retry_limit=1, retry_backoff_base_seconds=30, retry_backoff_multiplier=4
    )
    source, _, plan = probes()
    plan_id = transcode_plan(db, tmp_path / "movie.mkv", source, plan)
    daemon._fail_plan(plan_id, RuntimeError("boom"))
    row = db.plan(plan_id)
    assert row["state"] == "retry_wait"
    assert row["attempt_count"] == 1
    delay = datetime.fromisoformat(row["next_attempt_at"]) - datetime.now(UTC)
    assert 29 < delay.total_seconds() < 31


def test_recovery_requeues_running_plan_with_valid_cache(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    source_path = tmp_path / "movie.mkv"
    source_path.write_bytes(b"old")
    source, output, plan = probes()
    plan_id = transcode_plan(db, source_path, source, plan)
    cache = tmp_path / "cache"
    cache.mkdir()
    cache_file = cache / f"{plan_id}.mkv"
    cache_file.write_bytes(b"new")

    daemon = make_daemon(db, cache, probe_runner=StubProbe(output))
    daemon._recover_interrupted_plans()

    row = db.plan(plan_id)
    assert row["state"] == "queued"
    assert row["claimed_by"] is None
    assert row["attempt_count"] == 0
    assert row["actions"]["cache_path"] == str(cache_file)
    assert cache_file.exists()


def test_recovery_deletes_invalid_cache_and_requeues(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    source_path = tmp_path / "movie.mkv"
    source_path.write_bytes(b"old")
    source, _, plan = probes()
    plan_id = transcode_plan(db, source_path, source, plan)
    cache = tmp_path / "cache"
    cache.mkdir()
    cache_file = cache / f"{plan_id}.mkv"
    cache_file.write_bytes(b"partial")

    daemon = make_daemon(db, cache, probe_runner=FailingProbe())
    daemon._recover_interrupted_plans()

    row = db.plan(plan_id)
    assert row["state"] == "queued"
    assert not cache_file.exists()


def test_recovery_releases_stale_postprocess_claim(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    plan_id = db.create_plan(
        media_file_id=db.upsert_media_file(
            path=str(tmp_path / "movie.mkv"), library="movies", size=3, mtime_ns=1, fingerprint="fp"
        ),
        source=PlanSource.SCAN,
        priority=30,
        state=PlanState.QUEUED,
    )
    db.transition_plan(plan_id, PlanState.RUNNING)
    db.transition_plan(plan_id, PlanState.POSTPROCESS_PENDING)
    db.claim_postprocess("worker")

    daemon = make_daemon(db, None)
    daemon._recover_interrupted_plans()

    row = db.plan(plan_id)
    assert row["state"] == "postprocess_pending"
    assert row["claimed_by"] is None


def _promote_migration(tmp_path: Path, plan_id: str):
    source_path = tmp_path / "movie.mp4"
    cache = tmp_path / "cache.mkv"
    source_path.write_bytes(b"old")
    cache.write_bytes(b"new")
    source, output, plan = probes()
    result = SafePromoter(StubProbe(output)).promote(
        plan_id=plan_id,
        source_path=source_path,
        cache_output=cache,
        expected_source=fingerprint(source_path),
        source_probe=source,
        plan=plan,
    )
    return (
        source_path,
        Path(result.final_path),
        Path(result.backup_path),
        Path(result.marker_path),
        output,
    )


def test_recovery_finalizes_promoted_migration(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    source, target, backup, marker, output = _promote_migration(tmp_path, "promo")
    media_id = db.upsert_media_file(
        path=str(source), library="movies", size=3, mtime_ns=1, fingerprint="fp",
        arr_type="radarr", arr_media_id=1, arr_file_id=2,
    )
    plan_id = db.create_plan(
        media_file_id=media_id, source=PlanSource.SCAN, priority=30,
        actions={"stage": "transcode", "path": str(source)},
        state=PlanState.QUEUED,
    )
    db.claim_next("worker")
    db.register_migration(
        plan_id=plan_id, source_path=str(source), target_path=str(target),
        backup_path=str(backup), marker_path=str(marker),
    )

    daemon = make_daemon(db, None, probe_runner=StubProbe(output))
    daemon._recover_interrupted_plans()

    row = db.plan(plan_id)
    assert row["state"] == "postprocess_pending"
    assert row["actions"]["stage"] == "postprocess"
    assert row["actions"]["promotion"]["final_path"] == str(target)
    assert target.exists()
    assert not backup.exists()
    assert not marker.exists()


def test_recovery_restores_source_when_target_never_promoted(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    source, target, backup, marker, _ = _promote_migration(tmp_path, "restore")
    target.unlink()
    media_id = db.upsert_media_file(
        path=str(source), library="movies", size=3, mtime_ns=1, fingerprint="fp",
        arr_type="radarr", arr_media_id=1, arr_file_id=2,
    )
    plan_id = db.create_plan(
        media_file_id=media_id, source=PlanSource.SCAN, priority=30,
        actions={"stage": "transcode", "path": str(source)},
        state=PlanState.QUEUED,
    )
    db.claim_next("worker")
    db.register_migration(
        plan_id=plan_id, source_path=str(source), target_path=str(target),
        backup_path=str(backup), marker_path=str(marker),
    )

    daemon = make_daemon(db, None, probe_runner=StubProbe(None))
    daemon._recover_interrupted_plans()

    row = db.plan(plan_id)
    assert row["state"] == "queued"
    assert source.read_bytes() == b"old"
    assert not marker.exists()
