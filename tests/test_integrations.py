from pathlib import Path, PurePosixPath

import pytest

from app.database import Database
from app.integrations import ArrPathMapper, IntegrationError, IntegrationService


def service(tmp_path: Path) -> tuple[Database, IntegrationService]:
    db = Database(tmp_path / "state.sqlite")
    db.initialize()
    mapper = ArrPathMapper(
        movie_library_root=PurePosixPath(tmp_path / "movies"),
        series_library_root=PurePosixPath(tmp_path / "series"),
    )
    return db, IntegrationService(db, mapper)


def test_mapper_rejects_traversal_and_wrong_root() -> None:
    mapper = ArrPathMapper()
    with pytest.raises(IntegrationError):
        mapper.to_library("/movies/../series/bad.mkv", "radarr")
    with pytest.raises(IntegrationError):
        mapper.to_library("/series/bad.mkv", "radarr")


def test_import_is_durable_and_duplicate_delivery_does_not_duplicate_work(tmp_path: Path) -> None:
    db, integrations = service(tmp_path)
    payload = {
        "eventId": "download-1", "arrType": "radarr", "mediaId": 1,
        "fileId": 2, "path": "/movies/A/A.mkv"
    }
    first = integrations.accept_import(payload)
    second = integrations.accept_import(payload)
    assert first["accepted"] is True and second["accepted"] is False
    assert first["path"] == str(tmp_path / "movies/A/A.mkv")
    assert db.claim_outbox("worker")["operation"] == "evaluate_import"
    assert db.claim_outbox("worker") is None


def test_delete_during_extension_migration_preserves_sidecars(tmp_path: Path) -> None:
    db, integrations = service(tmp_path)
    old = str(tmp_path / "movies/A/A.mp4")
    new = str(tmp_path / "movies/A/A.mkv")
    db.register_migration(plan_id="p1", source_path=old, target_path=new)
    result = integrations.accept_delete({
        "eventId": "delete-1", "arrType": "radarr", "mediaId": 1,
        "fileId": 2, "path": "/movies/A/A.mp4"
    })
    assert result["preserveSidecars"] is True
    assert result["migrationPlanId"] == "p1"
    assert db.claim_outbox("worker") is None


def test_normal_delete_cancels_plan_and_enqueues_sidecar_cleanup(tmp_path: Path) -> None:
    db, integrations = service(tmp_path)
    path = str(tmp_path / "movies/A/A.mkv")
    media_id = db.upsert_media_file(
        path=path, library="movies", size=1, mtime_ns=2, fingerprint="fp"
    )
    from app.domain import PlanSource, PlanState
    db.create_plan(media_file_id=media_id, source=PlanSource.IMPORT, priority=1, state=PlanState.QUEUED)
    result = integrations.accept_delete({
        "eventId": "delete-normal", "arrType": "radarr", "mediaId": 1,
        "fileId": 2, "path": "/movies/A/A.mkv"
    })
    assert result["preserveSidecars"] is False
    assert db.status()["queue"] == {"queued": 1}
    assert db.claim_outbox("worker")["operation"] == "delete_sidecars"
