from pathlib import Path

from app.daemon import DispatcherDaemon
from app.database import Database
from app.domain import PlanSource, PlanState


def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "state.sqlite")
    db.initialize()
    return db


def make_daemon(db: Database, cache_root: Path) -> DispatcherDaemon:
    return DispatcherDaemon(
        database=db,
        queue=None,
        catalog=None,
        scanner=None,
        scheduler=None,
        outbox=None,
        job_runner=None,
        execution_enabled=False,
        automatic_scan_enabled=False,
        interval_seconds=5,
        cache_root=cache_root,
    )


def test_cleanup_removes_stale_cache_and_keeps_active(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "active-plan.mkv").write_bytes(b"active")
    (cache / "stale.mkv").write_bytes(b"stale")
    (cache / "unrelated.txt").write_bytes(b"keep")

    media_id = db.upsert_media_file(
        path="/library/movies/x.mkv", library="movies", size=1, mtime_ns=1, fingerprint="fp"
    )
    plan_id = db.create_plan(
        media_file_id=media_id, source=PlanSource.SCAN, priority=30, state=PlanState.QUEUED
    )
    db.transition_plan(plan_id, PlanState.RUNNING)
    (cache / f"{plan_id}.mkv").write_bytes(b"active-job")

    daemon = make_daemon(db, cache)
    daemon._cleanup_orphaned_cache()

    assert (cache / f"{plan_id}.mkv").exists()
    assert not (cache / "active-plan.mkv").exists()
    assert not (cache / "stale.mkv").exists()
    assert (cache / "unrelated.txt").exists()


def test_cleanup_is_noop_without_cache_root(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    daemon = make_daemon(db, None)
    daemon._cleanup_orphaned_cache()
