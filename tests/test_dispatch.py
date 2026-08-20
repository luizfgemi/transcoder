import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.daemon import EvaluationService, JobRunner, QueueService, import_callback
from app.database import Database
from app.domain import PlanSource, PlanState
from app.engine import SidecarResult
from app.integrations import ArrIdentity, PostprocessResult
from app.media import FFprobeRunner, MediaPathGuard, MediaProbe, fingerprint
from app.policy import Policy, RemuxPlan


class FakePlex:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def assert_path_idle(self, path: str) -> None:
        self.calls.append(f"idle:{path}")

    def refresh_path(self, path: str) -> None:
        self.calls.append(f"refresh:{path}")


class FakePostprocessor:
    def __init__(self, final_path: str | None = None) -> None:
        self.final_path = final_path

    def run(self, **kwargs) -> PostprocessResult:
        path = kwargs["promoted_library_path"]
        if self.final_path:
            return PostprocessResult(self.final_path, self.final_path, SidecarResult((), (), ()))
        return PostprocessResult(path, path, SidecarResult((), (), ()))


def test_probe_and_plan_round_trip() -> None:
    probe = MediaProbe.from_ffprobe("/x.mkv", {
        "format": {"format_name": "matroska", "duration": "1", "size": "2"},
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "hevc"},
            {"index": 1, "codec_type": "audio", "codec_name": "flac", "channels": 6,
             "tags": {"language": "jpn"}, "disposition": {"default": 1}},
        ],
    })
    restored_probe = MediaProbe.from_dict(probe.to_dict())
    plan = Policy().evaluate(probe, "jpn")
    restored_plan = RemuxPlan.from_dict(plan.to_dict())
    assert restored_probe == probe
    assert restored_plan == plan


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg tools are not installed",
)
def test_job_runner_executes_persisted_plan_and_finishes_postprocess(tmp_path: Path) -> None:
    movies = tmp_path / "movies"
    movies.mkdir()
    source = movies / "fixture.mkv"
    generation = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=128x72:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "1", "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "mpeg4", "-q:v", "5", "-c:a", "flac",
        "-metadata:s:a:0", "language=jpn", "-f", "matroska", str(source),
    ], capture_output=True, text=True, timeout=30)
    assert generation.returncode == 0, generation.stderr
    runner = FFprobeRunner(timeout_seconds=30)
    probe = runner.probe(source)
    plan = Policy().evaluate(probe, "jpn")
    fp = fingerprint(source)
    db = Database(tmp_path / "state.sqlite")
    db.initialize()
    media_id = db.upsert_media_file(
        path=str(source), library="movies", size=fp.size, mtime_ns=fp.mtime_ns,
        fingerprint=fp.digest, arr_type="radarr", arr_media_id=1, arr_file_id=2,
    )
    actions = {
        "stage": "transcode", "path": str(source), "fingerprint": fp.digest,
        "probe": probe.to_dict(), "plan": plan.to_dict(),
        "arr": {"arr_type": "radarr", "media_id": 1, "file_id": 2,
                "preferred_language": "jpn"},
    }
    plan_id = db.create_plan(
        media_file_id=media_id, source=PlanSource.MANUAL, priority=10,
        actions=actions, state=PlanState.RUNNING,
    )
    plex = FakePlex()
    job = db.plan(plan_id)
    JobRunner(
        database=db, cache_root=tmp_path / "cache", probe_runner=runner,
        plex=plex, postprocessor=FakePostprocessor(),  # type: ignore[arg-type]
    ).run(job)
    assert db.plan(plan_id)["state"] == "succeeded"
    assert runner.probe(source).audio[0].codec_name == "eac3"
    assert plex.calls == [f"idle:{source}", f"refresh:{source}"]
    final_media = db.media_file(str(source))
    assert final_media["state"] == "succeeded"
    assert final_media["fingerprint"] == fingerprint(source).digest
    assert not (movies / ".transcoder-backups").exists()


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg tools are not installed",
)
def test_job_runner_postprocess_reconciles_path_renamed_by_radarr(tmp_path: Path) -> None:
    movies = tmp_path / "movies"
    movies.mkdir()
    source = movies / "fixture.mkv"
    generation = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=128x72:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "1", "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "mpeg4", "-q:v", "5", "-c:a", "flac",
        "-metadata:s:a:0", "language=jpn", "-f", "matroska", str(source),
    ], capture_output=True, text=True, timeout=30)
    assert generation.returncode == 0, generation.stderr
    runner = FFprobeRunner(timeout_seconds=30)
    probe = runner.probe(source)
    plan = Policy().evaluate(probe, "jpn")
    fp = fingerprint(source)
    db = Database(tmp_path / "state.sqlite")
    db.initialize()
    media_id = db.upsert_media_file(
        path=str(source), library="movies", size=fp.size, mtime_ns=fp.mtime_ns,
        fingerprint=fp.digest, arr_type="radarr", arr_media_id=1, arr_file_id=2,
    )
    actions = {
        "stage": "transcode", "path": str(source), "fingerprint": fp.digest,
        "probe": probe.to_dict(), "plan": plan.to_dict(),
        "arr": {"arr_type": "radarr", "media_id": 1, "file_id": 2,
                "preferred_language": "jpn"},
    }
    plan_id = db.create_plan(
        media_file_id=media_id, source=PlanSource.MANUAL, priority=10,
        actions=actions, state=PlanState.RUNNING,
    )
    renamed = movies / "renamed.mkv"

    class RenamingPostprocessor(FakePostprocessor):
        def run(self, **kwargs) -> PostprocessResult:
            promoted = Path(kwargs["promoted_library_path"])
            promoted.rename(renamed)
            return PostprocessResult(str(renamed), str(renamed), SidecarResult((), (), ()))

    plex = FakePlex()
    job = db.plan(plan_id)
    JobRunner(
        database=db, cache_root=tmp_path / "cache", probe_runner=runner,
        plex=plex, postprocessor=RenamingPostprocessor(),  # type: ignore[arg-type]
    ).run(job)
    assert db.plan(plan_id)["state"] == "succeeded"
    assert db.media_file(str(renamed)) is not None
    assert db.media_file(str(source)) is None
    assert plex.calls == [f"idle:{source}", f"refresh:{renamed}"]


def test_queue_refuses_action_without_arr_identity(tmp_path: Path) -> None:
    movies = tmp_path / "movies"
    series = tmp_path / "series"
    cache = tmp_path / "cache"
    for directory in (movies, series, cache):
        directory.mkdir()
    media = movies / "film.mkv"
    media.write_bytes(b"x")

    class FakeProbe:
        def probe(self, path: Path) -> MediaProbe:
            return MediaProbe(
                str(path), ("matroska",), 1, 1,
                streams=MediaProbe.from_ffprobe(str(path), {"streams": [
                    {"index": 0, "codec_type": "video", "codec_name": "hevc"},
                    {"index": 1, "codec_type": "audio", "codec_name": "dts", "channels": 6},
                ]}).streams,
            )

    db = Database(tmp_path / "state.sqlite")
    db.initialize()
    evaluator = EvaluationService(
        database=db, path_guard=MediaPathGuard(movies, series, (".mkv",), cache),
        probe_runner=FakeProbe(), policy=Policy(), cache_path=cache, stability_seconds=0,
    )
    # 1. SCAN source without Arr identity is unmanaged
    result_scan = QueueService(db, evaluator).evaluate_and_queue(
        str(media), source=PlanSource.SCAN, identity=None, force=True
    )
    assert result_scan.status == "unmanaged"
    assert result_scan.plan_id is None

    # 2. MANUAL operator source without Arr identity is allowed and queued
    result_manual = QueueService(db, evaluator).evaluate_and_queue(
        str(media), source=PlanSource.MANUAL, identity=None, force=True
    )
    assert result_manual.status == "queued"
    assert result_manual.plan_id is not None


def test_import_without_stability_queues_fresh_file_immediately(tmp_path: Path) -> None:
    movies = tmp_path / "movies"
    series = tmp_path / "series"
    cache = tmp_path / "cache"
    for directory in (movies, series, cache):
        directory.mkdir()
    media = movies / "film.mkv"
    media.write_bytes(b"x")
    media.touch()

    class FakeProbe:
        def probe(self, path: Path) -> MediaProbe:
            return MediaProbe(
                str(path), ("matroska",), 1, 1,
                streams=MediaProbe.from_ffprobe(str(path), {"streams": [
                    {"index": 0, "codec_type": "video", "codec_name": "hevc"},
                    {"index": 1, "codec_type": "audio", "codec_name": "dts", "channels": 6},
                ]}).streams,
            )

    db = Database(tmp_path / "state.sqlite")
    db.initialize()
    evaluator = EvaluationService(
        database=db, path_guard=MediaPathGuard(movies, series, (".mkv",), cache),
        probe_runner=FakeProbe(), policy=Policy(), cache_path=cache, stability_seconds=60,
    )
    identity = ArrIdentity("radarr", 1, 2, "eng")
    result = QueueService(db, evaluator).evaluate_and_queue(
        str(media), source=PlanSource.IMPORT, identity=identity, require_stability=False
    )
    assert result.status == "queued"
    assert db.plan(result.plan_id)["state"] == "queued"
    assert db.plan(result.plan_id)["source"] == "import"


def test_import_without_stability_fresh_file_is_deferred_by_default(tmp_path: Path) -> None:
    movies = tmp_path / "movies"
    series = tmp_path / "series"
    cache = tmp_path / "cache"
    for directory in (movies, series, cache):
        directory.mkdir()
    media = movies / "film.mkv"
    media.write_bytes(b"x")
    media.touch()

    class FakeProbe:
        def probe(self, path: Path) -> MediaProbe:
            return MediaProbe(
                str(path), ("matroska",), 1, 1,
                streams=MediaProbe.from_ffprobe(str(path), {"streams": [
                    {"index": 0, "codec_type": "video", "codec_name": "hevc"},
                    {"index": 1, "codec_type": "audio", "codec_name": "dts", "channels": 6},
                ]}).streams,
            )

    db = Database(tmp_path / "state.sqlite")
    db.initialize()
    evaluator = EvaluationService(
        database=db, path_guard=MediaPathGuard(movies, series, (".mkv",), cache),
        probe_runner=FakeProbe(), policy=Policy(), cache_path=cache, stability_seconds=60,
    )
    identity = ArrIdentity("radarr", 1, 2, "eng")
    result = QueueService(db, evaluator).evaluate_and_queue(
        str(media), source=PlanSource.IMPORT, identity=identity
    )
    assert result.status == "deferred_unstable"
    assert result.plan_id is None


def test_import_callback_bypasses_stability_for_import_and_upgrade() -> None:
    calls: list[dict] = []

    class FakeQueue:
        def evaluate_and_queue(self, path, *, source, identity, force=False, require_stability=True):
            calls.append({"source": source, "require_stability": require_stability})
            return SimpleNamespace(status="queued", path=path, plan_id="p", report=None)

    callback = import_callback(FakeQueue())
    callback({
        "arrType": "radarr", "mediaId": 1, "fileId": 2,
        "path": "/movies/A.mkv", "preferredLanguage": "eng",
    })
    callback({
        "arrType": "radarr", "mediaId": 1, "fileId": 2,
        "path": "/movies/A.mkv", "preferredLanguage": "eng", "eventType": "upgrade",
    })
    assert calls == [
        {"source": PlanSource.IMPORT, "require_stability": False},
        {"source": PlanSource.UPGRADE, "require_stability": False},
    ]
