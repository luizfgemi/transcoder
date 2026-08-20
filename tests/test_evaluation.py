import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.database import Database
from app.daemon import EvaluationService
from app.media import MediaPathGuard
from app.policy import Policy
from app.media import MediaProbe, Stream


class FakeProbeRunner:
    def __init__(self) -> None:
        self.calls = 0

    def probe(self, path: Path) -> MediaProbe:
        self.calls += 1
        return MediaProbe(
            path=str(path),
            format_names=("matroska", "webm"),
            duration_seconds=100,
            size=path.stat().st_size,
            streams=(
                Stream(index=0, codec_type="video", codec_name="hevc"),
                Stream(index=1, codec_type="audio", codec_name="dts", channels=6, language="eng"),
            ),
        )


def _set_mtime(path: Path, when: datetime) -> None:
    ns = int(when.timestamp() * 1e9)
    os.utime(path, ns=(ns, ns))


def setup(tmp_path: Path, *, age: timedelta | None = None):
    movies = tmp_path / "movies"
    series = tmp_path / "series"
    cache = tmp_path / "cache"
    movies.mkdir()
    series.mkdir()
    cache.mkdir()
    path = movies / "film.mkv"
    path.write_bytes(b"media")
    db = Database(tmp_path / "state.sqlite")
    db.initialize()
    runner = FakeProbeRunner()
    now = [datetime(2026, 8, 4, tzinfo=UTC)]
    _set_mtime(path, now[0] - (age if age is not None else timedelta(hours=1)))
    evaluator = EvaluationService(
        database=db,
        path_guard=MediaPathGuard(movies, series, (".mkv", ".mp4"), cache),
        probe_runner=runner,
        policy=Policy(),
        cache_path=cache,
        stability_seconds=60,
        clock=lambda: now[0],
    )
    return path, db, runner, now, evaluator


def test_evaluation_caches_across_service_instances(tmp_path: Path) -> None:
    path, db, runner, now, evaluator = setup(tmp_path)
    first = evaluator.evaluate(str(path), preferred_language="English")
    assert first.status == "evaluated"
    assert first.plan["criteria"] == ["audio_incompatible"]
    assert first.argv[0] == "ffmpeg"
    assert runner.calls == 1

    replacement_runner = FakeProbeRunner()
    replacement = EvaluationService(
        database=db,
        path_guard=evaluator.path_guard,
        probe_runner=replacement_runner,
        policy=evaluator.policy,
        cache_path=evaluator.cache_path,
        stability_seconds=60,
        clock=lambda: now[0],
    )
    cached = replacement.evaluate(str(path), preferred_language="English")
    assert cached.status == "cached"
    assert replacement_runner.calls == 0


def test_force_ignores_cache_and_reprobes(tmp_path: Path) -> None:
    path, _, runner, now, evaluator = setup(tmp_path)
    assert evaluator.evaluate(str(path), force=True).status == "evaluated"
    assert evaluator.evaluate(str(path), force=True).status == "evaluated"
    assert runner.calls == 2


def test_recently_added_file_is_deferred(tmp_path: Path) -> None:
    path, _, runner, now, evaluator = setup(tmp_path)
    recent = (now[0] - timedelta(seconds=10)).isoformat()
    result = evaluator.evaluate(str(path), added_at=recent)
    assert result.status == "deferred_unstable"
    assert runner.calls == 0


def test_old_added_file_evaluates_immediately(tmp_path: Path) -> None:
    path, _, runner, now, evaluator = setup(tmp_path)
    old = (now[0] - timedelta(hours=3)).isoformat()
    result = evaluator.evaluate(str(path), added_at=old)
    assert result.status == "evaluated"
    assert result.plan["criteria"] == ["audio_incompatible"]
    assert runner.calls == 1


def test_recently_imported_without_identity_is_deferred_by_mtime(tmp_path: Path) -> None:
    path, _, runner, now, evaluator = setup(tmp_path, age=timedelta(seconds=10))
    assert evaluator.evaluate(str(path)).status == "deferred_unstable"
    assert runner.calls == 0


def test_require_stability_false_evaluates_fresh_file_immediately(tmp_path: Path) -> None:
    path, _, runner, now, evaluator = setup(tmp_path, age=timedelta(seconds=10))
    result = evaluator.evaluate(str(path), require_stability=False)
    assert result.status == "evaluated"
    assert result.plan["criteria"] == ["audio_incompatible"]
    assert runner.calls == 1


def test_report_endpoint_is_authenticated_and_returns_dry_run(tmp_path: Path) -> None:
    path, db, _, now, evaluator = setup(tmp_path, age=timedelta(seconds=10))
    config = Settings(
        api_key="a-secure-test-key",
        database_path=db.path,
        cache_path=evaluator.cache_path,
        movie_root=evaluator.path_guard._roots["movies"],
        series_root=evaluator.path_guard._roots["series"],
        file_stability_seconds=60,
    )
    app = create_app(config, db, evaluator)
    with TestClient(app) as client:
        deferred = client.post(
            "/api/v1/reports",
            headers={"X-API-Key": config.api_key},
            json={"path": str(path), "preferredLanguage": "English"},
        )
        assert deferred.json()["status"] == "deferred_unstable"
        _set_mtime(path, now[0] - timedelta(hours=1))
        response = client.post(
            "/api/v1/reports",
            headers={"X-API-Key": config.api_key},
            json={"path": str(path), "preferredLanguage": "English"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "evaluated"
        assert response.json()["argv"][0] == "ffmpeg"
        assert not (evaluator.cache_path / "report.mkv").exists()
