"""FastAPI HTTP application factory, route definitions, and authentication dependencies.

Contract:
  - Responsibility: Expose REST endpoints for service health, queue and scheduling status,
    media queries, manual evaluations/runs, and webhooks (`/radarr`, `/sonarr`).
  - Inputs: HTTP requests with `X-API-Key` or `Authorization: Bearer <key>` header (except webhooks).
  - Outputs: JSON responses matching API contracts.
  - Invariants:
      * Unauthenticated requests to protected endpoints return 401 Unauthorized.
      * Webhooks return 202 Accepted and delegate event parsing to `app.integrations`.
"""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from app import __version__
from app.config import Settings
from app.database import Database
from app.domain import PlanSource
from app.daemon import (
    AdaptiveScheduler,
    DispatcherDaemon,
    EvaluationService,
    JobRunner,
    QueueService,
    SchedulePolicy,
    import_callback,
)
from app.integrations import (
    ArrCatalog,
    ArrPostProcessor,
    ArrWebhookError,
    BazarrClient,
    IntegrationError,
    IntegrationService,
    OutboxWorker,
    PlexClient,
    PlexPathMapper,
    RadarrClient,
    SonarrClient,
    TelegramNotifier,
    normalize_radarr_webhook,
    normalize_sonarr_webhook,
    read_plex_token,
)
from app.media import FFprobeRunner, FileScanner, MediaPathGuard, ProbeError, UnsafeMediaPath
from app.policy import Policy


class ReportRequest(BaseModel):
    path: str = Field(min_length=1)
    preferred_language: str | None = Field(default=None, alias="preferredLanguage")
    force: bool = False


class ManualRunRequest(BaseModel):
    path: str = Field(min_length=1)
    force: bool = True


def create_app(
    settings: Settings,
    database: Database | None = None,
    evaluation_service: EvaluationService | None = None,
    scheduler: AdaptiveScheduler | None = None,
    integration_service: IntegrationService | None = None,
    queue_service: QueueService | None = None,
    runtime: Any | None = None,
) -> FastAPI:
    """Construct and configure the FastAPI application instance.

    Contract:
      - Mounts health, status, reports, webhooks (/radarr, /sonarr), and job execution endpoints.
      - Authenticates protected endpoints against `settings.api_key`.
      - Starts/stops background daemon runtime during application lifespan.
    """
    db = database or Database(settings.database_path)
    probe_runner = FFprobeRunner()
    path_guard = MediaPathGuard(
        settings.movie_root,
        settings.series_root,
        settings.allowed_extensions,
        settings.cache_path,
    )
    policy = Policy(
        video_copy_codecs=settings.video_copy_codecs,
        playback_maxrate_kbps=settings.playback_maxrate_kbps,
        nvenc_cq=settings.nvenc_cq,
        nvenc_preset=settings.nvenc_preset,
        nvenc_tune=settings.nvenc_tune,
        nvenc_profile=settings.nvenc_profile,
        nvenc_pix_fmt=settings.nvenc_pix_fmt,
        nvenc_spatial_aq=settings.nvenc_spatial_aq,
        nvenc_temporal_aq=settings.nvenc_temporal_aq,
        nvenc_aq_strength=settings.nvenc_aq_strength,
        nvenc_rc_lookahead=settings.nvenc_rc_lookahead,
        nvenc_b_ref_mode=settings.nvenc_b_ref_mode,
        audio_copy_codecs=settings.audio_copy_codecs,
        stream_limit=settings.stream_limit,
        subtitle_keep_languages=settings.subtitle_keep_languages,
    )
    evaluator = evaluation_service or EvaluationService(
        database=db,
        path_guard=path_guard,
        probe_runner=probe_runner,
        policy=policy,
        cache_path=settings.cache_path,
        stability_seconds=settings.file_stability_seconds,
    )
    adaptive_scheduler = scheduler or AdaptiveScheduler(
        db,
        SchedulePolicy(
            days=settings.process_days,
            start=settings.window_start,
            end=settings.window_end,
            timezone=settings.timezone,
            max_jobs=settings.max_jobs_per_run,
            empty_scan_cooldown_days=settings.empty_scan_cooldown_days,
        ),
    )
    integrations = integration_service or IntegrationService(db)
    radarr = RadarrClient(settings.radarr_url, settings.radarr_api_key) if settings.radarr_url and settings.radarr_api_key else None
    sonarr = SonarrClient(settings.sonarr_url, settings.sonarr_api_key) if settings.sonarr_url and settings.sonarr_api_key else None
    catalog = ArrCatalog(radarr, sonarr)
    queue = queue_service or QueueService(db, evaluator, catalog)

    daemon_runtime = runtime
    if daemon_runtime is None and settings.execution_enabled:
        bazarr = BazarrClient(settings.bazarr_url, settings.bazarr_api_key) if settings.bazarr_url and settings.bazarr_api_key else None
        outbox = OutboxWorker(
            db,
            on_import=import_callback(queue),
        )
        job_runner = None
        try:
            plex_tok = read_plex_token(settings.plex_token_file) if settings.plex_token_file else None
            if settings.plex_url and plex_tok:
                mapper = PlexPathMapper(
                    movie_local=settings.movie_root,
                    series_local=settings.series_root,
                    movie_plex=settings.movie_root,
                    series_plex=settings.series_root,
                )
                plex = PlexClient(settings.plex_url, plex_tok, mapper=mapper)
                postprocessor = ArrPostProcessor(
                    radarr=radarr,
                    sonarr=sonarr,
                    bazarr=bazarr,
                )
                job_runner = JobRunner(
                    database=db,
                    cache_root=settings.cache_path,
                    probe_runner=probe_runner,
                    plex=plex,
                    postprocessor=postprocessor,
                    policy=policy,
                    duration_tolerance_seconds=settings.duration_tolerance_seconds,
                )
        except Exception:
            pass

        daemon_runtime = DispatcherDaemon(
            database=db,
            queue=queue,
            catalog=catalog,
            scanner=FileScanner(settings.movie_root, settings.series_root, settings.allowed_extensions),
            scheduler=adaptive_scheduler,
            outbox=outbox,
            job_runner=job_runner,
            execution_enabled=settings.execution_enabled,
            automatic_scan_enabled=settings.automatic_scan_enabled,
            interval_seconds=settings.loop_interval_seconds,
            cache_root=settings.cache_path,
            probe_runner=probe_runner,
            retry_limit=settings.retry_limit,
            retry_backoff_base_seconds=settings.retry_backoff_base_seconds,
            retry_backoff_multiplier=settings.retry_backoff_multiplier,
            failure_retry_cooldown_days=settings.failure_retry_cooldown_days,
            duration_tolerance_seconds=settings.duration_tolerance_seconds,
            reconcile_deleted_grace_hours=settings.reconcile_deleted_grace_hours,
            notifier=TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id),
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db.initialize()
        app.state.shutting_down = False
        if daemon_runtime is not None and hasattr(daemon_runtime, "start"):
            daemon_runtime.start()
        yield
        app.state.shutting_down = True
        if daemon_runtime is not None and hasattr(daemon_runtime, "stop"):
            daemon_runtime.stop()

    app = FastAPI(title="Transcoder", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.database = db
    app.state.shutting_down = False

    def authenticate(
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> None:
        key = x_api_key
        if not key and authorization:
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() == "bearer":
                key = token

        if key and secrets.compare_digest(key, settings.api_key):
            return

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    @app.get("/api/v1/health")
    def health(
        response: Response,
        authenticated: None = Depends(authenticate),
    ) -> dict[str, object]:
        try:
            status_data = db.status(recent_event_limit=1)
        except Exception as error:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "unhealthy", "error": str(error)}
        return {"status": "healthy", "service": "transcoder", "queue": status_data["queue"]}

    @app.get("/api/v1/status", dependencies=[Depends(authenticate)])
    def status_view() -> dict[str, object]:
        now = datetime.now(UTC)
        backlog = db.count_queued_for_sources((PlanSource.SCAN, PlanSource.RETRY))
        decision = adaptive_scheduler.decide(now, scheduled_backlog=backlog)
        active_window = adaptive_scheduler.policy.active_window(now)
        next_window = active_window.start if active_window else adaptive_scheduler.policy.next_window_start(now)
        db_status = db.status()
        daemon_status = runtime.status() if runtime and hasattr(runtime, "status") else {}
        return {
            "status": "ready",
            "scheduler": {
                "decision": decision.value,
                "maxJobs": adaptive_scheduler.policy.max_jobs,
                "scheduledBacklog": backlog,
                "window": {
                    "active": active_window is not None,
                    "start": adaptive_scheduler.policy.start.isoformat(timespec="minutes"),
                    "end": adaptive_scheduler.policy.end.isoformat(timespec="minutes"),
                    "nextWindow": next_window.isoformat(),
                },
            },
            "decision": decision.value,
            "scheduledBacklog": backlog,
            "queue": db_status["queue"],
            "recentEvents": db_status.get("recentEvents", []),
            "daemon": daemon_status,
        }

    @app.post("/api/v1/reports", dependencies=[Depends(authenticate)])
    def generate_report(payload: ReportRequest) -> dict[str, object]:
        try:
            report = evaluator.evaluate(
                payload.path,
                preferred_language=payload.preferred_language,
                force=payload.force,
            )
        except (UnsafeMediaPath, ProbeError, FileNotFoundError) as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)) from error
        return report.to_dict()

    @app.post(
        "/api/v1/webhooks/radarr",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def radarr_webhook(payload: dict[str, object]) -> dict[str, object]:
        """Ingest native Radarr webhook events (Download, Rename, MovieFileDelete, Test)."""
        try:
            events = normalize_radarr_webhook(payload)
        except ArrWebhookError as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)) from error
        stored = []
        for operation, event in events:
            if operation == "import":
                stored.append(integrations.record_import(event))
            elif operation == "rename":
                stored.append(integrations.record_rename(event))
            elif operation == "delete":
                stored.append(integrations.record_delete(event))
        return {"status": "accepted", "events": stored}

    @app.post(
        "/api/v1/webhooks/sonarr",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def sonarr_webhook(payload: dict[str, object]) -> dict[str, object]:
        """Ingest native Sonarr webhook events (Download, Rename, EpisodeFileDelete, Test)."""
        try:
            events = normalize_sonarr_webhook(payload)
        except ArrWebhookError as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)) from error
        stored = []
        for operation, event in events:
            if operation == "import":
                stored.append(integrations.record_import(event))
            elif operation == "rename":
                stored.append(integrations.record_rename(event))
            elif operation == "delete":
                stored.append(integrations.record_delete(event))
        return {"status": "accepted", "events": stored}

    @app.get("/api/v1/media", dependencies=[Depends(authenticate)])
    def media_list(state: str | None = Query(default=None)) -> list[dict[str, object]]:
        return db.media_files(state=state)

    @app.get("/api/v1/search", dependencies=[Depends(authenticate)])
    def search_media(q: str = Query(min_length=1)) -> list[dict[str, object]]:
        return db.search_media(q)

    @app.get("/api/v1/jobs", dependencies=[Depends(authenticate)])
    def list_jobs(state: str | None = Query(default=None)) -> list[dict[str, object]]:
        return db.list_plans(state=state)

    @app.post("/api/v1/jobs/{plan_id}/cancel", dependencies=[Depends(authenticate)])
    def cancel_job(plan_id: str) -> dict[str, object]:
        cancelled = runtime.cancel(plan_id) if runtime else db.cancel_plan(plan_id)
        if not cancelled:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found or cannot be cancelled")
        return {"success": True, "planId": plan_id, "state": "cancelled"}

    @app.post("/api/v1/jobs/{plan_id}/run-now", dependencies=[Depends(authenticate)])
    def run_job_now(plan_id: str) -> dict[str, object]:
        plan = db.plan(plan_id)
        if plan is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
        prioritized = db.prioritize_plan_now(plan_id)
        if not prioritized and plan["state"] not in {"queued", "running"}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"job in state '{plan['state']}' cannot be triggered immediately",
            )
        return {"success": True, "planId": plan_id, "state": "queued", "priority": 0}

    @app.post(
        "/api/v1/scan",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(authenticate)],
    )
    def scan_file(payload: ManualRunRequest) -> dict[str, object]:
        return manual_run(payload)

    @app.post(
        "/api/v1/manual-runs",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(authenticate)],
    )
    def manual_run(payload: ManualRunRequest) -> dict[str, object]:
        try:
            identity = queue.identity_for_path(payload.path)
            result = queue.evaluate_and_queue(
                payload.path, source=PlanSource.MANUAL, identity=identity, force=payload.force
            )
        except (UnsafeMediaPath, ProbeError, FileNotFoundError) as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)) from error
        return {
            "status": result.status,
            "path": result.path,
            "planId": result.plan_id,
            "report": result.report.to_dict(),
        }

    @app.get("/api/v1/manual-runs/{plan_id}", dependencies=[Depends(authenticate)])
    def manual_run_status(plan_id: str) -> dict[str, object]:
        plan = db.plan(plan_id)
        if plan is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
        return plan

    return app


# Backward-compatible factory alias
build_app = create_app

