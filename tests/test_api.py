from pathlib import Path

from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.database import Database
from app.domain import PlanSource, PlanState


def settings(tmp_path: Path) -> Settings:
    return Settings(
        api_key="a-secure-test-key",
        database_path=tmp_path / "state.sqlite",
        cache_path=tmp_path / "cache",
        movie_root=tmp_path / "movies",
        series_root=tmp_path / "series",
    )


def test_api_requires_key_and_reports_health(tmp_path: Path) -> None:
    config = settings(tmp_path)
    db = Database(config.database_path)
    app = create_app(config, db)
    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 401
        assert client.get("/api/v1/health", headers={"X-API-Key": "wrong-key"}).status_code == 401
        response = client.get("/api/v1/health", headers={"X-API-Key": config.api_key})
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"
        assert response.json()["service"] == "transcoder"


def test_status_reads_sqlite_snapshot(tmp_path: Path) -> None:
    config = settings(tmp_path)
    db = Database(config.database_path)
    app = create_app(config, db)
    with TestClient(app) as client:
        response = client.get("/api/v1/status", headers={"X-API-Key": config.api_key})
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
        assert "decision" in data
        assert "queue" in data


def test_list_jobs_and_run_now(tmp_path: Path) -> None:
    config = settings(tmp_path)
    db = Database(config.database_path)
    app = create_app(config, db)
    with TestClient(app) as client:
        headers = {"X-API-Key": config.api_key}

        # Create sample media file and queued plan in db
        media_id = db.upsert_media_file(
            path="/movies/Test.mkv",
            library="movies",
            size=1000,
            mtime_ns=1000,
            fingerprint="fp123",
        )
        plan_id = db.create_plan(
            media_file_id=media_id,
            source=PlanSource.SCAN,
            priority=30,
            actions={"path": "/movies/Test.mkv"},
            state=PlanState.QUEUED,
        )

        # 1. List jobs via GET /api/v1/jobs
        resp_jobs = client.get("/api/v1/jobs", headers=headers)
        assert resp_jobs.status_code == 200
        data = resp_jobs.json()
        assert len(data) == 1
        assert data[0]["id"] == plan_id
        assert data[0]["priority"] == 30

        # 2. Trigger immediate run via POST /api/v1/jobs/{id}/run-now
        resp_run = client.post(f"/api/v1/jobs/{plan_id}/run-now", headers=headers)
        assert resp_run.status_code == 200
        assert resp_run.json()["success"] is True
        assert resp_run.json()["priority"] == 0

        # 3. Verify plan in db was elevated to priority 0
        plan = db.plan(plan_id)
        assert plan["priority"] == 0


def test_cancel_job(tmp_path: Path) -> None:
    config = settings(tmp_path)
    db = Database(config.database_path)
    app = create_app(config, db)
    with TestClient(app) as client:
        headers = {"X-API-Key": config.api_key}
        media_id = db.upsert_media_file(
            path="/movies/Test.mkv",
            library="movies",
            size=1000,
            mtime_ns=1000,
            fingerprint="fp123",
        )
        plan_id = db.create_plan(
            media_file_id=media_id,
            source=PlanSource.SCAN,
            priority=30,
            actions={"path": "/movies/Test.mkv"},
            state=PlanState.QUEUED,
        )
        resp = client.post(f"/api/v1/jobs/{plan_id}/cancel", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["state"] == "cancelled"
        plan = db.plan(plan_id)
        assert plan["state"] == "cancelled"


def test_arr_webhook_accepts_native_payload_without_key(tmp_path: Path) -> None:
    config = settings(tmp_path)
    db = Database(config.database_path)
    app = create_app(config, db)
    with TestClient(app) as client:
        payload = {
            "eventType": "Download",
            "movie": {"id": 10, "title": "A", "originalLanguage": {"name": "English"}},
            "movieFile": {"id": 20, "path": "/movies/A/A.mkv"},
            "downloadClient": "qBittorrent",
        }
        resp = client.post("/api/v1/webhooks/radarr", json=payload)
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"
        assert len(data["events"]) == 1
        assert data["events"][0]["path"].endswith("/movies/A/A.mkv")
        claimed = db.claim_outbox("worker")
        assert claimed is not None
        assert claimed["operation"] == "evaluate_import"


def test_sonarr_webhook_accepts_download_event(tmp_path: Path) -> None:
    config = settings(tmp_path)
    db = Database(config.database_path)
    app = create_app(config, db)
    with TestClient(app) as client:
        payload = {
            "eventType": "Download",
            "series": {"id": 3, "title": "Show", "originalLanguage": {"name": "English"}},
            "episodeFile": {"id": 30, "path": "/series/Show/S01E01.mkv"},
        }
        resp = client.post("/api/v1/webhooks/sonarr", json=payload)
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"
        assert len(data["events"]) == 1
        assert data["events"][0]["path"].endswith("/series/Show/S01E01.mkv")


def test_arr_webhook_test_event_is_acknowledged(tmp_path: Path) -> None:
    config = settings(tmp_path)
    db = Database(config.database_path)
    app = create_app(config, db)
    with TestClient(app) as client:
        resp = client.post("/api/v1/webhooks/radarr", json={"eventType": "Test"})
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"
        assert resp.json()["events"] == []

        resp = client.post("/api/v1/webhooks/sonarr", json={"eventType": "Test"})
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"
        assert resp.json()["events"] == []


def test_arr_webhook_rejects_payload_without_media(tmp_path: Path) -> None:
    config = settings(tmp_path)
    db = Database(config.database_path)
    app = create_app(config, db)
    with TestClient(app) as client:
        resp = client.post("/api/v1/webhooks/radarr", json={"eventType": "Download"})
        assert resp.status_code == 422
        resp = client.post("/api/v1/webhooks/sonarr", json={"eventType": "Download"})
        assert resp.status_code == 422
