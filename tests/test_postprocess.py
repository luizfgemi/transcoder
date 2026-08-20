from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest

from app.database import Database
from app.integrations import ArrPathMapper
from app.integrations import ArrPostProcessor, OutboxWorker, BazarrClient


class FakeRadarr:
    def __init__(self, calls: list[str], final_path: str) -> None:
        self.calls = calls
        self.final_path = final_path

    def refresh(self, media_id: int) -> int:
        self.calls.append(f"refresh:{media_id}")
        return 10

    def rename(self, media_id: int) -> int:
        self.calls.append(f"rename:{media_id}")
        return 11

    def final_file_path(self, media_id: int) -> str:
        self.calls.append(f"path:{media_id}")
        return self.final_path


class FakeSonarr:
    pass


class FakeWaiter:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def wait(self, client: object, command_id: int) -> None:
        self.calls.append(f"wait:{command_id}")


class FakeBazarr:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def scan_disk(self, arr_type: str, media_id: int) -> None:
        self.calls.append(f"bazarr:{arr_type}:{media_id}")


def test_postprocess_waits_refresh_then_rename_and_moves_sidecars(tmp_path: Path) -> None:
    calls: list[str] = []
    library = tmp_path / "movies"
    before = library / "A" / "Before.mkv"
    after = library / "A" / "After.mkv"
    before.parent.mkdir(parents=True)
    (before.parent / "Before.pt.srt").write_text("subtitle")
    db = Database(tmp_path / "state.sqlite")
    db.initialize()
    db.upsert_media_file(path=str(before), library="movies", size=1, mtime_ns=2, fingerprint="fp")
    mapper = ArrPathMapper(movie_library_root=PurePosixPath(library))
    processor = ArrPostProcessor(
        database=db,
        radarr=FakeRadarr(calls, "/movies/A/After.mkv"),  # type: ignore[arg-type]
        sonarr=FakeSonarr(),  # type: ignore[arg-type]
        bazarr=FakeBazarr(calls),  # type: ignore[arg-type]
        waiter=FakeWaiter(calls),  # type: ignore[arg-type]
        mapper=mapper,
    )
    result = processor.run(arr_type="radarr", media_id=5, file_id=6, promoted_library_path=str(before))
    assert calls == ["refresh:5", "wait:10", "rename:5", "wait:11", "path:5", "bazarr:radarr:5"]
    assert result.final_library_path == str(after)
    assert (after.parent / "After.pt.srt").read_text() == "subtitle"


def test_outbox_worker_executes_sidecar_rename_once(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite")
    db.initialize()
    old = tmp_path / "Old.mkv"
    new = tmp_path / "New.mkv"
    (tmp_path / "Old.en.srt").write_text("sub")
    db.accept_inbound_event(
        event_key="rename:1",
        event_type="rename",
        payload={},
        operations=(("rename_sidecars", {
            "oldPath": str(old), "newPath": str(new), "arrType": "radarr", "mediaId": 1
        }, "rename-sidecars:1"),),
    )
    worker = OutboxWorker(db, evaluate_import=lambda _: None)
    assert worker.run_one()
    assert not worker.run_one()
    assert (tmp_path / "New.en.srt").exists()


def test_delete_outbox_cancels_active_plan_before_sidecar_cleanup(tmp_path: Path) -> None:
    from app.domain import PlanSource, PlanState

    db = Database(tmp_path / "state.sqlite")
    db.initialize()
    video = tmp_path / "Gone.mkv"
    (tmp_path / "Gone.pt.srt").write_text("sub")
    media_id = db.upsert_media_file(
        path=str(video), library="movies", size=1, mtime_ns=2, fingerprint="fp"
    )
    db.create_plan(media_file_id=media_id, source=PlanSource.IMPORT, priority=1, state=PlanState.QUEUED)
    db.accept_inbound_event(
        event_key="delete:1", event_type="delete", payload={},
        operations=(("delete_sidecars", {
            "path": str(video), "arrType": "radarr", "mediaId": 1
        }, "delete-sidecars:1"),),
    )
    assert OutboxWorker(db, evaluate_import=lambda _: None).run_one()
    assert db.status()["queue"] == {"cancelled": 1}
    assert not (tmp_path / "Gone.pt.srt").exists()


def test_bazarr_sync_uses_taskid_update_endpoints() -> None:
    calls: list[tuple[str, str, dict]] = []

    class FakeTransport:
        def request(self, method: str, url: str, headers: dict, body: dict) -> None:
            calls.append((method, url, body))

    bazarr = BazarrClient(
        base_url="http://bazarr:6767",
        api_key="secret",
        transport=FakeTransport(),  # type: ignore[arg-type]
    )
    bazarr.sync_radarr()
    bazarr.sync_sonarr()

    assert calls == [
        ("POST", "http://bazarr:6767/api/system/tasks", {"taskid": "update_movies"}),
        ("POST", "http://bazarr:6767/api/system/tasks", {"taskid": "update_series"}),
    ]


def test_bazarr_scan_disk_dispatches_to_radarr_and_sonarr() -> None:
    calls: list[tuple[str, str, dict]] = []

    class FakeTransport:
        def request(self, method: str, url: str, headers: dict, body: dict) -> None:
            calls.append((method, url, body))

    bazarr = BazarrClient(
        base_url="http://bazarr:6767",
        api_key="secret",
        transport=FakeTransport(),  # type: ignore[arg-type]
    )
    bazarr.scan_disk("radarr", 7)
    bazarr.scan_disk("sonarr", 8)

    assert calls == [
        ("POST", "http://bazarr:6767/api/system/tasks", {"taskid": "update_movies"}),
        ("POST", "http://bazarr:6767/api/system/tasks", {"taskid": "update_series"}),
    ]


def _outbox_row(db: Database):
    import sqlite3

    conn = sqlite3.connect(db.path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM outbox").fetchone()
    finally:
        conn.close()


def _make_outbox_due(db: Database) -> None:
    import sqlite3

    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    with sqlite3.connect(db.path) as conn:
        conn.execute("UPDATE outbox SET next_attempt_at=?", (past,))


def test_outbox_worker_retries_with_backoff_then_gives_up(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite")
    db.initialize()
    attempts: list[dict] = []

    def flaky(payload: dict) -> None:
        attempts.append(payload)
        raise FileNotFoundError("file not ready yet")

    db.accept_inbound_event(
        event_key="import:1", event_type="import", payload={},
        operations=(("evaluate_import", {"path": "/x.mkv"}, "evaluate-import:1"),),
    )
    worker = OutboxWorker(
        db, evaluate_import=flaky, max_attempts=3, retry_backoff_base_seconds=30,
        retry_backoff_multiplier=2,
    )

    for expected in (1, 2):
        with pytest.raises(FileNotFoundError):
            worker.run_one()
        row = _outbox_row(db)
        assert row["attempt_count"] == expected
        assert row["state"] == "retry_wait"
        assert row["next_attempt_at"] is not None
        _make_outbox_due(db)

    assert worker.run_one()
    row = _outbox_row(db)
    assert row["attempt_count"] == 3
    assert row["state"] == "done"
    assert attempts == [{"path": "/x.mkv"} for _ in range(3)]
    assert not worker.run_one()


def test_outbox_worker_retry_backoff_is_exponential(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.sqlite")
    db.initialize()

    def flaky(payload: dict) -> None:
        raise FileNotFoundError("not ready")

    db.accept_inbound_event(
        event_key="import:2", event_type="import", payload={},
        operations=(("evaluate_import", {"path": "/x.mkv"}, "evaluate-import:2"),),
    )
    worker = OutboxWorker(
        db, evaluate_import=flaky, max_attempts=5, retry_backoff_base_seconds=30,
        retry_backoff_multiplier=2,
    )
    with pytest.raises(FileNotFoundError):
        worker.run_one()
    first = _outbox_row(db)["next_attempt_at"]
    _make_outbox_due(db)
    with pytest.raises(FileNotFoundError):
        worker.run_one()
    second = _outbox_row(db)["next_attempt_at"]

    first_delay = datetime.fromisoformat(first) - datetime.now(UTC)
    second_delay = datetime.fromisoformat(second) - datetime.now(UTC)
    assert 29 < first_delay.total_seconds() < 31
    assert 59 < second_delay.total_seconds() < 61
