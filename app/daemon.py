"""Background queue dispatcher, adaptive scheduler, job runner, and library reconcile daemon.

Contract:
  - Responsibility: Manage background evaluation loops, queue prioritization (`QueueService`),
    window-based execution scheduling (`AdaptiveScheduler`), job dispatching (`DispatcherDaemon`),
    and library sync/reconcile operations.
  - Invariants:
      * Execution only runs within the configured active schedule window.
      * Retries apply exponential backoff up to `retry_limit`.
      * Grace periods prevent premature deletion or reconcile during transient library mounts.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from html import escape
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.database import Database
from app.domain import PlanSource, PlanState
from app.engine import (
    FFmpegExecutor,
    ProcessingPipeline,
    SafePromoter,
    ValidationError,
    complete_extension_migration_paths,
    recover_extension_migration,
    validate_output,
)
from app.integrations import (
    ArrCatalog,
    ArrIdentity,
    ArrPostProcessor,
    OutboxWorker,
    PlexClient,
    TelegramNotifier,
)
from app.media import Fingerprint, MediaPathGuard, MediaProbe, ProbeRunner, fingerprint
from app.policy import Policy, RemuxPlan


logger = logging.getLogger(__name__)

PRIORITIES = {
    PlanSource.MANUAL: 10,
    PlanSource.IMPORT: 20,
    PlanSource.UPGRADE: 20,
    PlanSource.SCAN: 30,
    PlanSource.RETRY: 40,
}


@dataclass(frozen=True, slots=True)
class ScheduleWindow:
    start: datetime
    end: datetime
    key: str = ""


@dataclass(frozen=True, slots=True)
class SchedulePolicy:
    days: tuple[int, ...]
    start: time
    end: time
    timezone: ZoneInfo | str
    max_jobs: int
    empty_scan_cooldown_days: int

    @property
    def tz(self) -> ZoneInfo:
        return self.timezone if isinstance(self.timezone, ZoneInfo) else ZoneInfo(str(self.timezone))

    def active_window(self, now: datetime) -> ScheduleWindow | None:
        local_now = self._local(now)
        # Full day window if start == end
        if self.start == self.end:
            target_date = local_now.date()
            if target_date.weekday() in self.days:
                start_local = datetime.combine(target_date, self.start, tzinfo=self.tz)
                end_local = start_local + timedelta(days=1)
                return ScheduleWindow(start=start_local, end=end_local, key=target_date.isoformat())
            return None

        for offset in (0, -1):
            candidate_date = local_now.date() + timedelta(days=offset)
            if candidate_date.weekday() not in self.days:
                continue
            start_local = datetime.combine(candidate_date, self.start, tzinfo=self.tz)
            end_local = datetime.combine(candidate_date, self.end, tzinfo=self.tz)
            if self.end <= self.start:
                end_local += timedelta(days=1)
            if start_local <= local_now < end_local:
                return ScheduleWindow(start=start_local, end=end_local, key=candidate_date.isoformat())
        return None

    def next_window_start(self, after: datetime) -> datetime:
        active = self.active_window(after)
        if active is not None and active.start > after:
            return active.start
        local_after = self._local(after)
        for day_offset in range(0 if self.start == self.end else 1, 15):
            candidate_date = local_after.date() + timedelta(days=day_offset)
            if candidate_date.weekday() in self.days:
                start_local = datetime.combine(candidate_date, self.start, tzinfo=self.tz)
                if start_local > local_after or (self.start == self.end and start_local >= local_after):
                    return start_local
        return after

    def _local(self, value: datetime) -> datetime:
        return value.astimezone(self.tz)


class SchedulerDecision(StrEnum):
    RUN_SCAN = "run_scan"
    RUN_SCHEDULED_JOB = "run_scheduled_job"
    PROCESS_BACKLOG = "process_backlog"
    DRAIN_BACKLOG = "drain_backlog"
    QUOTA_REACHED = "quota_reached"
    WAIT_CONFIRMATION_WINDOW = "wait_confirmation_window"
    COOLDOWN = "cooldown"
    OUTSIDE_WINDOW = "outside_window"
    WAITING = "waiting"
    IDLE = "idle"


@dataclass(frozen=True, slots=True)
class SchedulerState:
    current_window_start: str | None = None
    jobs_executed_in_window: int = 0
    jobs_started: int = 0
    window_key: str | None = None
    last_scan_window: str | None = None
    scan_in_progress_window: str | None = None
    last_scan_finished_at: str | None = None
    next_scan_at: str | None = None
    backlog_active: bool = False
    confirmation_window_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "SchedulerState":
        return cls(**(value or {}))


class AdaptiveScheduler:
    def __init__(self, database: Database, policy: SchedulePolicy) -> None:
        self.database = database
        self.policy = policy

    def state(self) -> SchedulerState:
        return SchedulerState.from_dict(self.database.scheduler_state())

    def decide(self, now: datetime, *, scheduled_backlog: int) -> SchedulerDecision:
        window = self.policy.active_window(now)
        if window is None:
            return SchedulerDecision.OUTSIDE_WINDOW
        state = self.state()
        window_key = window.key or _utc_iso(window.start)
        if state.window_key != window_key:
            state = SchedulerState(
                current_window_start=_utc_iso(window.start),
                jobs_executed_in_window=0,
                jobs_started=0,
                window_key=window_key,
                last_scan_window=state.last_scan_window,
                scan_in_progress_window=None,
                last_scan_finished_at=state.last_scan_finished_at,
                next_scan_at=state.next_scan_at,
                backlog_active=state.backlog_active,
                confirmation_window_key=state.confirmation_window_key,
            )
            self._save(state)

        if state.scan_in_progress_window == window_key:
            return SchedulerDecision.WAITING
        if scheduled_backlog > 0:
            if self.policy.max_jobs > 0 and state.jobs_started >= self.policy.max_jobs:
                return SchedulerDecision.QUOTA_REACHED
            return SchedulerDecision.PROCESS_BACKLOG
        if state.last_scan_window == window_key:
            return SchedulerDecision.WAITING
        if state.next_scan_at and now < datetime.fromisoformat(state.next_scan_at):
            return SchedulerDecision.WAITING
        return SchedulerDecision.RUN_SCAN

    def record_scan_started(self, now: datetime) -> SchedulerState:
        window = self.policy.active_window(now)
        state = self.state()
        window_key = window.key if window else self.policy._local(now).date().isoformat()
        updated = SchedulerState(
            current_window_start=_utc_iso(window.start) if window else state.current_window_start,
            jobs_executed_in_window=state.jobs_executed_in_window,
            jobs_started=state.jobs_started,
            window_key=window_key,
            last_scan_window=state.last_scan_window,
            scan_in_progress_window=window_key,
            last_scan_finished_at=state.last_scan_finished_at,
            next_scan_at=state.next_scan_at,
            backlog_active=state.backlog_active,
            confirmation_window_key=state.confirmation_window_key,
        )
        self._save(updated)
        return updated

    def record_scan_finished(self, now: datetime, *, candidate_count: int) -> SchedulerState:
        state = self.state()
        window = self.policy.active_window(now)
        window_key = window.key if window else self.policy._local(now).date().isoformat()
        next_scan_iso: str | None = None
        if candidate_count == 0:
            local_now = self.policy._local(now)
            target_date = local_now.date() + timedelta(days=self.policy.empty_scan_cooldown_days)
            while target_date.weekday() not in self.policy.days:
                target_date += timedelta(days=1)
            next_scan_dt = datetime.combine(target_date, self.policy.start, tzinfo=self.policy.tz)
            next_scan_iso = _utc_iso(next_scan_dt)

        updated = SchedulerState(
            current_window_start=state.current_window_start,
            jobs_executed_in_window=state.jobs_executed_in_window,
            jobs_started=state.jobs_started,
            window_key=state.window_key,
            last_scan_window=window_key,
            scan_in_progress_window=None,
            last_scan_finished_at=_utc_iso(now),
            next_scan_at=next_scan_iso,
            backlog_active=candidate_count > 0,
            confirmation_window_key=state.confirmation_window_key,
        )
        self._save(updated)
        return updated

    def record_backlog_drained(self, now: datetime) -> SchedulerState:
        state = self.state()
        next_window_dt = self.policy.next_window_start(now)
        updated = SchedulerState(
            current_window_start=state.current_window_start,
            jobs_executed_in_window=state.jobs_executed_in_window,
            jobs_started=state.jobs_started,
            window_key=state.window_key,
            last_scan_window=state.last_scan_window,
            scan_in_progress_window=None,
            last_scan_finished_at=state.last_scan_finished_at,
            next_scan_at=_utc_iso(next_window_dt),
            backlog_active=False,
            confirmation_window_key=state.window_key,
        )
        self._save(updated)
        return updated

    def recover_interrupted_scan(self, now: datetime) -> SchedulerState:
        state = self.state()
        if state.scan_in_progress_window:
            updated = SchedulerState(
                current_window_start=state.current_window_start,
                jobs_executed_in_window=state.jobs_executed_in_window,
                jobs_started=state.jobs_started,
                window_key=state.window_key,
                last_scan_window=state.last_scan_window,
                scan_in_progress_window=None,
                last_scan_finished_at=state.last_scan_finished_at,
                next_scan_at=state.next_scan_at,
                backlog_active=state.backlog_active,
                confirmation_window_key=state.confirmation_window_key,
            )
            self._save(updated)
            return updated
        return state

    def record_scheduled_job_started(self, now: datetime) -> SchedulerState:
        state = self.state()
        window = self.policy.active_window(now)
        window_key = window.key if window else state.window_key
        updated = SchedulerState(
            current_window_start=_utc_iso(window.start) if window else state.current_window_start,
            jobs_executed_in_window=state.jobs_executed_in_window + 1,
            jobs_started=state.jobs_started + 1,
            window_key=window_key,
            last_scan_window=state.last_scan_window,
            scan_in_progress_window=state.scan_in_progress_window,
            last_scan_finished_at=state.last_scan_finished_at,
            next_scan_at=state.next_scan_at,
            backlog_active=state.backlog_active,
            confirmation_window_key=state.confirmation_window_key,
        )
        self._save(updated)
        return updated

    def _save(self, state: SchedulerState) -> None:
        self.database.set_scheduler_state(state.to_dict())


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


class QueueArbiter:
    def __init__(self, database: Database, scheduler: AdaptiveScheduler) -> None:
        self.database = database
        self.scheduler = scheduler

    def claim(self, worker_id: str, now: datetime) -> dict[str, Any] | None:
        claimed = self.database.claim_immediate(worker_id)
        if claimed:
            return claimed
        backlog = self.database.count_queued_for_sources((PlanSource.SCAN, PlanSource.RETRY))
        decision = self.scheduler.decide(now, scheduled_backlog=backlog)
        if decision in {SchedulerDecision.PROCESS_BACKLOG, SchedulerDecision.DRAIN_BACKLOG, SchedulerDecision.RUN_SCHEDULED_JOB}:
            job = self.database.claim_scheduled(worker_id)
            if job:
                self.scheduler.record_scheduled_job_started(now)
                return job
        return None


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    path: str
    status: str
    fingerprint: str | None
    probe: dict[str, Any] | None
    plan: dict[str, Any] | None
    argv: list[str] | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EvaluationService:
    def __init__(
        self,
        *,
        database: Database,
        path_guard: MediaPathGuard,
        probe_runner: ProbeRunner,
        policy: Policy,
        cache_path: Path,
        stability_seconds: int = 60,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.path_guard = path_guard
        self.probe_runner = probe_runner
        self.policy = policy
        self.cache_path = cache_path
        self.stability_seconds = stability_seconds
        self.clock = clock or (lambda: datetime.now(UTC))

    def evaluate(
        self,
        path_string: str,
        *,
        preferred_language: str | None = None,
        force: bool = False,
        added_at: str | None = None,
        require_stability: bool = True,
    ) -> EvaluationReport:
        resolved = self.path_guard.validate(path_string)
        library, target_path = resolved.library, resolved.path
        file_fingerprint = fingerprint(target_path)

        now = self.clock()
        if require_stability and self.stability_seconds > 0:
            if added_at:
                try:
                    added_dt = datetime.fromisoformat(added_at.replace("Z", "+00:00"))
                    if (now - added_dt).total_seconds() < self.stability_seconds:
                        return EvaluationReport(
                            path=str(target_path),
                            status="deferred_unstable",
                            fingerprint=file_fingerprint.digest,
                            probe=None,
                            plan=None,
                            argv=None,
                        )
                except Exception:
                    pass
            else:
                mtime_dt = datetime.fromtimestamp(target_path.stat().st_mtime, tz=UTC)
                if (now - mtime_dt).total_seconds() < self.stability_seconds:
                    return EvaluationReport(
                        path=str(target_path),
                        status="deferred_unstable",
                        fingerprint=file_fingerprint.digest,
                        probe=None,
                        plan=None,
                        argv=None,
                    )

        if not force:
            cached = self.database.cached_evaluation(file_fingerprint.digest, self.policy.signature_for(preferred_language))
            if cached:
                argv = None
                if cached["plan"].get("needs_processing") or not cached["plan"].get("compliant"):
                    from app.policy import build_ffmpeg_argv
                    plan = RemuxPlan.from_dict(cached["plan"])
                    if plan.needs_processing:
                        argv = build_ffmpeg_argv(plan, target_path, self.cache_path / "report.mkv")
                return EvaluationReport(
                    path=str(target_path),
                    status="cached",
                    fingerprint=file_fingerprint.digest,
                    probe=cached["probe"],
                    plan=cached["plan"],
                    argv=argv,
                )

        probe = self.probe_runner.probe(target_path)
        plan = self.policy.evaluate(probe, preferred_language)

        media_id = self.database.upsert_media_file(
            path=str(target_path),
            library=library,
            size=file_fingerprint.size,
            mtime_ns=file_fingerprint.mtime_ns,
            fingerprint=file_fingerprint.digest,
        )
        self.database.record_evaluation(
            media_file_id=media_id,
            fingerprint=file_fingerprint.digest,
            policy_signature=plan.policy_signature,
            probe=probe.to_dict(),
            plan=plan.to_dict(),
            result="evaluated",
        )
        argv = None
        if plan.needs_processing:
            from app.policy import build_ffmpeg_argv
            argv = build_ffmpeg_argv(plan, target_path, self.cache_path / "report.mkv")
        return EvaluationReport(
            path=str(target_path),
            status="evaluated",
            fingerprint=file_fingerprint.digest,
            probe=probe.to_dict(),
            plan=plan.to_dict(),
            argv=argv,
        )


@dataclass(frozen=True, slots=True)
class QueueResult:
    status: str
    path: str
    plan_id: str | None
    report: EvaluationReport


class QueueService:
    def __init__(
        self, database: Database, evaluator: EvaluationService, catalog: ArrCatalog | None = None
    ) -> None:
        self.database = database
        self.evaluator = evaluator
        self.catalog = catalog

    def identity_for_path(self, path: str) -> ArrIdentity | None:
        return self.catalog.snapshot().get(path) if self.catalog else None

    def evaluate_and_queue(
        self,
        path: str,
        *,
        source: PlanSource,
        identity: ArrIdentity | None,
        force: bool = False,
        require_stability: bool = True,
    ) -> QueueResult:
        language = identity.preferred_language if identity else None
        report = self.evaluator.evaluate(
            path,
            preferred_language=language,
            force=force,
            added_at=identity.date_added if identity else None,
            require_stability=require_stability,
        )
        if report.probe is None or report.plan is None:
            logger.info("media evaluated (skipped: probe/plan missing): file=%s", Path(path).name)
            return QueueResult(report.status, report.path, None, report)
        plan_data = report.plan
        if plan_data.get("policy_exception"):
            logger.info("media evaluated (skipped: policy exception): file=%s reason=%s", Path(path).name, plan_data.get("policy_exception"))
            return QueueResult("policy_exception", report.path, None, report)
        if plan_data.get("compliant"):
            logger.info("media evaluated (skipped: compliant): file=%s", Path(path).name)
            return QueueResult("compliant", report.path, None, report)
        if identity is None and source != PlanSource.MANUAL:
            logger.info("media evaluated (skipped: unmanaged): file=%s", Path(path).name)
            return QueueResult("unmanaged", report.path, None, report)
        media = self.database.media_file(report.path)
        if media is None:
            raise RuntimeError("evaluation did not persist its media row")
        if identity:
            self.database.upsert_media_file(
                path=report.path,
                library=media["library"],
                size=media["size"],
                mtime_ns=media["mtime_ns"],
                fingerprint=media["fingerprint"],
                arr_type=identity.arr_type,
                arr_media_id=identity.media_id,
                arr_file_id=identity.file_id,
            )
        active = self.database.active_plan_for_media(int(media["id"]))
        if active:
            return QueueResult("already_queued", report.path, active["id"], report)
        actions: dict[str, Any] = {
            "stage": "transcode",
            "path": report.path,
            "fingerprint": report.fingerprint,
            "probe": report.probe,
            "plan": {key: value for key, value in plan_data.items() if key != "argv"},
        }
        if identity:
            actions["arr"] = asdict(identity)
        try:
            plan_id = self.database.create_plan(
                media_file_id=int(media["id"]),
                source=source,
                priority=PRIORITIES[source],
                actions=actions,
                state=PlanState.QUEUED,
            )
            logger.info("job queued: plan=%s source=%s file=%s", plan_id, source.value, Path(path).name)
        except sqlite3.IntegrityError:
            active = self.database.active_plan_for_media(int(media["id"]))
            if active is None:
                raise
            plan_id = active["id"]
            return QueueResult("already_queued", report.path, plan_id, report)
        return QueueResult("queued", report.path, plan_id, report)


class JobRunner:
    def __init__(
        self,
        *,
        database: Database,
        cache_root: Path,
        probe_runner: ProbeRunner,
        plex: PlexClient,
        postprocessor: ArrPostProcessor,
        policy: Policy | None = None,
        duration_tolerance_seconds: float = 2.0,
    ) -> None:
        self.database = database
        self.cache_root = cache_root
        self.probe_runner = probe_runner
        self.plex = plex
        self.postprocessor = postprocessor
        self.policy = policy or Policy()
        self.duration_tolerance_seconds = duration_tolerance_seconds

    def run(self, job: dict[str, Any], cancel_event: threading.Event | None = None) -> None:
        actions = job["actions"]
        if actions.get("stage") == "postprocess":
            self._postprocess(job["id"], actions)
            return
        source_path = Path(actions["path"])
        media_file = self.database.media_file(str(source_path))
        if media_file is None:
            raise RuntimeError(f"media file not found in database: {source_path}")
        expected = Fingerprint(
            path=str(source_path),
            size=int(actions["probe"]["size"]),
            mtime_ns=int(media_file["mtime_ns"]),
            digest=actions["fingerprint"],
        )
        probe = MediaProbe.from_dict(actions["probe"])
        plan = RemuxPlan.from_dict(actions["plan"])
        promoter = SafePromoter(
            self.probe_runner,
            duration_tolerance_seconds=self.duration_tolerance_seconds,
            before_promote=lambda: self.plex.assert_path_idle(str(source_path)),
        )
        pipeline = ProcessingPipeline(
            cache_root=self.cache_root,
            executor=FFmpegExecutor(),
            promoter=promoter,
        )
        cache_path = self.cache_root / f"{job['id']}.mkv"
        actions["cache_path"] = str(cache_path)
        self.database.update_plan_actions(job["id"], actions)
        logger.info(
            "FFmpeg started: plan=%s file=%s video_transcode=%s",
            job["id"],
            source_path.name,
            plan.video.transcoded,
        )
        result = pipeline.process(
            plan_id=job["id"],
            source_path=source_path,
            expected_source=expected,
            source_probe=probe,
            plan=plan,
            cancel_event=cancel_event,
            on_progress=lambda progress: logger.debug("FFmpeg progress: plan=%s %s", job["id"], progress),
        )
        logger.info("FFmpeg completed: plan=%s file=%s", job["id"], source_path.name)
        promotion = result.promotion
        actions.update(
            stage="postprocess",
            promotion={
                "final_path": promotion.final_path,
                "original_path": promotion.original_path,
                "backup_path": promotion.backup_path,
                "marker_path": promotion.marker_path,
            },
        )
        self.database.update_plan_actions(job["id"], actions)
        self.database.transition_plan(job["id"], PlanState.POSTPROCESS_PENDING, expected=PlanState.RUNNING)
        self._postprocess(job["id"], actions)

    def _postprocess(self, plan_id: str, actions: dict[str, Any]) -> None:
        identity_data = actions.get("arr")
        promotion = actions["promotion"]
        final_library_path = promotion["final_path"]

        if identity_data:
            identity = ArrIdentity(**identity_data)
            result = self.postprocessor.run(
                arr_type=identity.arr_type,
                media_id=identity.media_id,
                file_id=identity.file_id,
                promoted_library_path=final_library_path,
            )
            final_library_path = result.final_library_path
            preferred_language = identity.preferred_language
            arr_type = identity.arr_type
            arr_media_id = identity.media_id
            arr_file_id = identity.file_id
        else:
            preferred_language = None
            arr_type = None
            arr_media_id = None
            arr_file_id = None

        self.plex.refresh_path(final_library_path)
        final_path = Path(final_library_path)
        final_probe = self.probe_runner.probe(final_path)
        final_plan = self.policy.evaluate(final_probe, preferred_language)
        if not final_plan.compliant:
            raise RuntimeError("promoted output is not compliant after Arr/Plex reconciliation")
        final_fingerprint = fingerprint(final_path)
        media = self.database.media_file(final_library_path)
        if media is None and final_library_path != promotion["final_path"]:
            if self.database.reconcile_media_path(promotion["final_path"], final_library_path):
                media = self.database.media_file(final_library_path)
        if media is None:
            raise RuntimeError("final media path disappeared from dispatcher state")
        media_id = self.database.upsert_media_file(
            path=final_library_path,
            library=media["library"],
            size=final_fingerprint.size,
            mtime_ns=final_fingerprint.mtime_ns,
            fingerprint=final_fingerprint.digest,
            arr_type=arr_type,
            arr_media_id=arr_media_id,
            arr_file_id=arr_file_id,
        )
        self.database.observe_file(
            path=final_fingerprint.path,
            library=media["library"],
            size=final_fingerprint.size,
            mtime_ns=final_fingerprint.mtime_ns,
            required_seconds=0,
        )
        self.database.record_evaluation(
            media_file_id=media_id,
            fingerprint=final_fingerprint.digest,
            policy_signature=final_plan.policy_signature,
            probe=final_probe.to_dict(),
            plan=final_plan.to_dict(),
            result="succeeded",
        )
        complete_extension_migration_paths(
            backup_path=promotion.get("backup_path"),
            marker_path=promotion.get("marker_path"),
            final_path=final_library_path,
        )
        self.database.transition_plan(plan_id, PlanState.SUCCEEDED, expected=PlanState.POSTPROCESS_PENDING)


class DispatcherDaemon:
    def __init__(
        self,
        *,
        database: Database,
        queue: QueueService,
        catalog: ArrCatalog,
        scanner: Any,
        scheduler: AdaptiveScheduler,
        outbox: OutboxWorker,
        job_runner: JobRunner | None,
        execution_enabled: bool,
        automatic_scan_enabled: bool,
        interval_seconds: int,
        cache_root: Path | None = None,
        probe_runner: ProbeRunner | None = None,
        retry_limit: int = 2,
        retry_backoff_base_seconds: int = 60,
        retry_backoff_multiplier: int = 4,
        failure_retry_cooldown_days: int = 7,
        duration_tolerance_seconds: float = 2.0,
        reconcile_deleted_grace_hours: int = 24,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self.database = database
        self.queue = queue
        self.catalog = catalog
        self.scanner = scanner
        self.scheduler = scheduler
        self.outbox = outbox
        self.job_runner = job_runner
        self.execution_enabled = execution_enabled
        self.automatic_scan_enabled = automatic_scan_enabled
        self.interval_seconds = interval_seconds
        self.cache_root = cache_root
        self.probe_runner = probe_runner
        self.retry_limit = retry_limit
        self.retry_backoff_base_seconds = retry_backoff_base_seconds
        self.retry_backoff_multiplier = retry_backoff_multiplier
        self.failure_retry_cooldown_days = failure_retry_cooldown_days
        self.duration_tolerance_seconds = duration_tolerance_seconds
        self.reconcile_deleted_grace_hours = reconcile_deleted_grace_hours
        self.notifier = notifier
        self.arbiter = QueueArbiter(database, scheduler)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._active_plan_id: str | None = None
        self._active_cancel: threading.Event | None = None

    def start(self) -> None:
        now = datetime.now(UTC)
        self._cleanup_orphaned_cache()
        state = self.scheduler.recover_interrupted_scan(now)
        self._recover_interrupted_plans()
        active_window = self.scheduler.policy.active_window(now)
        next_window = active_window.start if active_window else self.scheduler.policy.next_window_start(now)
        days = ",".join(str(day) for day in self.scheduler.policy.days)
        logger.info(
            "dispatcher active: execution=%s automatic_scan=%s days=%s window=%s-%s "
            "next_window=%s quota=%s cooldown_days=%s backlog_active=%s queue=%s",
            self.execution_enabled,
            self.automatic_scan_enabled,
            days,
            self.scheduler.policy.start.isoformat(timespec="minutes"),
            self.scheduler.policy.end.isoformat(timespec="minutes"),
            next_window.isoformat(),
            self.scheduler.policy.max_jobs,
            self.scheduler.policy.empty_scan_cooldown_days,
            state.backlog_active,
            self._queue_summary(),
        )
        self._thread = threading.Thread(target=self._loop, name="dispatcher-daemon", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        logger.info("dispatcher stopping")
        self._stop.set()
        if self._active_cancel is not None:
            self._active_cancel.set()
        if self._thread:
            self._thread.join(timeout=300)

    def cancel(self, plan_id: str) -> bool:
        plan = self.database.plan(plan_id)
        if (
            self._active_plan_id == plan_id
            and self._active_cancel is not None
            and plan is not None
            and plan["state"] == PlanState.RUNNING.value
        ):
            self._active_cancel.set()
            return True
        return self.database.cancel_plan(plan_id)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("dispatcher tick failed")
            self._stop.wait(self.interval_seconds)

    def tick(self) -> None:
        try:
            self.outbox.run_one()
        except Exception:
            logger.exception("outbox item failed and will be retried")
        now = datetime.now(UTC)
        self.database.reopen_due_plans(now)
        if self.automatic_scan_enabled:
            backlog = self.database.count_queued_for_sources((PlanSource.SCAN, PlanSource.RETRY))
            decision = self.scheduler.decide(now, scheduled_backlog=backlog)
            if decision == SchedulerDecision.RUN_SCAN:
                self._scan(now)
        if not self.execution_enabled or self.job_runner is None:
            return
        job = self.database.claim_postprocess("dispatcher") or self.arbiter.claim("dispatcher", now)
        if job is None:
            return
        path = self._job_path(job)
        logger.info(
            "job started: plan=%s source=%s file=%s queue=%s",
            job["id"], job["source"], path.name, self._queue_summary(),
        )
        cancel_event = threading.Event()
        self._active_plan_id = job["id"]
        self._active_cancel = cancel_event
        try:
            self.job_runner.run(job, cancel_event=cancel_event)
            logger.info(
                "job completed: plan=%s file=%s queue=%s",
                job["id"], path.name, self._queue_summary(),
            )
            self._notify(self._job_notification(job))
        except Exception as error:
            current = self.database.plan(job["id"])
            if current and current["state"] == PlanState.RUNNING.value:
                if cancel_event.is_set():
                    self.database.transition_plan(
                        job["id"], PlanState.CANCELLED,
                        expected=PlanState.RUNNING,
                        error=f"{type(error).__name__}: {error}",
                    )
                    logger.info("job interrupted (cancelled): plan=%s file=%s", job["id"], path.name)
                else:
                    self._fail_plan(job["id"], error)
            elif current and current["state"] == PlanState.POSTPROCESS_PENDING.value:
                self.database.transition_plan(
                    job["id"], PlanState.POSTPROCESS_PENDING,
                    expected=PlanState.POSTPROCESS_PENDING,
                    error=f"{type(error).__name__}: {error}",
                )
            logger.exception("job failed: plan=%s file=%s", job["id"], path.name)
            self._notify(self._job_notification(job, error=error))
        finally:
            self._active_plan_id = None
            self._active_cancel = None

    def _scan(self, now: datetime) -> None:
        state = self.scheduler.record_scan_started(now)
        logger.info("scheduled scan started: window=%s", state.scan_in_progress_window)
        self._cleanup_orphaned_cache()
        candidates = 0
        try:
            identities = self.catalog.snapshot()
            seen = set()
            for path in self.scanner.discover():
                try:
                    self.outbox.run_one()
                except Exception:
                    logger.exception("outbox item failed during scan and will be retried")
                identity = identities.get(str(path.resolve(strict=True)))
                seen.add(str(path.resolve(strict=True)))
                result = self.queue.evaluate_and_queue(
                    str(path), source=PlanSource.SCAN, identity=identity
                )
                if result.plan_id and result.status == "queued":
                    candidates += 1
            counts = self.database.reconcile_scan(
                seen, deleted_grace_hours=self.reconcile_deleted_grace_hours
            )
            if any(counts.values()):
                logger.info(
                    "scan reconciled database with disk: %s",
                    ", ".join(f"{key}={value}" for key, value in counts.items()),
                )
        except Exception:
            self.scheduler.recover_interrupted_scan(datetime.now(UTC))
            raise
        state = self.scheduler.record_scan_finished(datetime.now(UTC), candidate_count=candidates)
        logger.info(
            "scheduled scan completed: queued=%s backlog_active=%s next_scan_at=%s queue=%s",
            candidates,
            state.backlog_active,
            state.next_scan_at,
            self._queue_summary(),
        )
        self._notify(
            f"<b>Scan completed</b>\nCandidates queued: {candidates}\n"
            f"Status: {'Backlog active' if state.backlog_active else 'No pending candidates'}"
        )

    def _notify(self, message: str) -> None:
        if self.notifier is None or not self.notifier.enabled:
            return
        try:
            self.notifier.send(message)
        except Exception:
            logger.exception("notification delivery failed")

    def _cleanup_orphaned_cache(self) -> None:
        if self.cache_root is None or not self.cache_root.is_dir():
            return
        active = self.database.active_plan_ids()
        removed = 0
        freed = 0
        for entry in self.cache_root.iterdir():
            if not entry.is_file() or not entry.name.endswith(".mkv"):
                continue
            if entry.stem in active:
                continue
            try:
                size = entry.stat().st_size
                entry.unlink()
                removed += 1
                freed += size
            except OSError:
                logger.warning("could not remove stale cache entry: %s", entry)
        if removed:
            logger.info(
                "temporary files cleaned: removed=%s freed_mb=%s",
                removed,
                freed // (1024 * 1024),
            )

    def _fail_plan(self, plan_id: str, error: Exception) -> None:
        attempt_count = self.database.increment_plan_attempt(plan_id)
        now = datetime.now(UTC)
        message = f"{type(error).__name__}: {error}"
        if attempt_count <= self.retry_limit:
            retry_index = max(attempt_count - 1, 0)
            backoff = self.retry_backoff_base_seconds * (self.retry_backoff_multiplier**retry_index)
            self.database.fail_plan(
                plan_id,
                target=PlanState.RETRY_WAIT,
                next_attempt_at=now + timedelta(seconds=backoff),
                error=message,
            )
            logger.info("job scheduled for retry: plan=%s attempt=%s backoff=%ss", plan_id, attempt_count, backoff)
        else:
            cooldown = timedelta(days=self.failure_retry_cooldown_days)
            self.database.fail_plan(
                plan_id,
                target=PlanState.FAILED,
                next_attempt_at=now + cooldown,
                error=message,
            )
            logger.info("job permanently failed: plan=%s attempt=%s", plan_id, attempt_count)

    def _recover_interrupted_plans(self) -> None:
        now = datetime.now(UTC)
        self.database.reopen_due_plans(now)
        for plan in self.database.stuck_plans():
            if plan["state"] == PlanState.POSTPROCESS_PENDING.value:
                self.database.release_claim(plan["id"], error="interrupted by restart")
                logger.info("released stale postprocess claim: plan=%s", plan["id"])
            elif plan["state"] == PlanState.RUNNING.value:
                self._recover_running(plan)

    def _recover_running(self, plan: dict) -> None:
        plan_id = plan["id"]
        marker = self.database.migration_for_plan(plan_id)
        if marker is not None:
            marker_path = Path(str(marker["marker_path"]))
            if self.probe_runner is None or not marker_path.is_file():
                logger.warning("dropping unrecoverable migration marker: plan=%s", plan_id)
                marker_path.unlink(missing_ok=True)
                self._requeue_running(plan, delete_cache=True)
                return
            result = recover_extension_migration(marker_path, self.probe_runner)
            if result == "promoted":
                actions = dict(plan["actions"])
                actions.update(
                    stage="postprocess",
                    promotion={
                        "final_path": marker["target_path"],
                        "original_path": marker["source_path"],
                        "backup_path": marker["backup_path"],
                        "marker_path": marker["marker_path"],
                    },
                )
                self.database.update_plan_actions(plan_id, actions)
                complete_extension_migration_paths(
                    backup_path=marker["backup_path"],
                    marker_path=marker["marker_path"],
                    final_path=marker["target_path"],
                )
                self.database.transition_plan(
                    plan_id,
                    PlanState.POSTPROCESS_PENDING,
                    expected=PlanState.RUNNING,
                    error="recovered promoted migration",
                )
                logger.info("recovered promoted migration: plan=%s", plan_id)
                return
            logger.info("restored interrupted migration: plan=%s", plan_id)
            self._requeue_running(plan, delete_cache=True)
            return
        self._requeue_running(plan, delete_cache=not self._cache_valid(plan))

    def _cache_valid(self, plan: dict) -> bool:
        if self.cache_root is None or self.probe_runner is None:
            return False
        cache_path = self._plan_cache_path(plan)
        if not cache_path.is_file() or cache_path.stat().st_size <= 0:
            return False
        try:
            source_probe = MediaProbe.from_dict(plan["actions"]["probe"])
            remux_plan = RemuxPlan.from_dict(plan["actions"]["plan"])
            cache_probe = self.probe_runner.probe(cache_path)
        except Exception:
            return False
        try:
            validate_output(
                source_probe, cache_probe, remux_plan, duration_tolerance_seconds=self.duration_tolerance_seconds
            )
        except ValidationError:
            return False
        return True

    def _requeue_running(self, plan: dict, *, delete_cache: bool) -> None:
        plan_id = plan["id"]
        if delete_cache:
            self._delete_cache(plan)
        else:
            actions = dict(plan["actions"])
            actions["cache_path"] = str(self._plan_cache_path(plan))
            self.database.update_plan_actions(plan_id, actions)
        self.database.requeue_plan(
            plan_id,
            source=PlanSource(plan["source"]),
            expected=PlanState.RUNNING,
            error="interrupted by restart",
        )
        logger.info("requeued interrupted running job: plan=%s", plan_id)

    def _plan_cache_path(self, plan: dict) -> Path:
        cached = plan["actions"].get("cache_path")
        if cached:
            return Path(str(cached))
        if self.cache_root is None:
            raise RuntimeError("cache root is not configured")
        return self.cache_root / f"{plan['id']}.mkv"

    def _delete_cache(self, plan: dict) -> None:
        if self.cache_root is None:
            return
        cache_path = self._plan_cache_path(plan)
        try:
            cache_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("could not remove cache entry: %s", cache_path)

    def _queue_summary(self) -> str:
        queue = self.database.status(recent_event_limit=0)["queue"]
        return ",".join(f"{state}={count}" for state, count in sorted(queue.items())) or "empty"

    @staticmethod
    def _job_path(job: dict) -> Path:
        actions = job.get("actions", {})
        return Path(str(actions.get("path") or actions.get("promotion", {}).get("final_path") or "unknown"))

    def _job_notification(self, job: dict, *, error: Exception | None = None) -> str:
        path = self._job_path(job)
        title = re.sub(r"\s+\{(?:tmdb|tvdb)-[^}]+\}$", "", path.parent.name).strip() or path.stem
        criteria = job.get("actions", {}).get("plan", {}).get("criteria") or ()
        labels = {
            "video_incompatible": "Video transcoding (HEVC Main10)",
            "audio_incompatible": "Audio compatibility",
            "audio_original_not_first": "Preferred audio order",
            "duplicate_original_audio_optimized": "Duplicate original audio optimized",
            "stream_limit_exceeded": "Stream limit",
        }
        reasons = ", ".join(labels.get(str(item), str(item).replace("_", " ").title()) for item in criteria)
        if not reasons:
            reasons = "Media compatibility policy"
        lines = [escape(title), f"Reason: {escape(reasons)}"]
        if error is None:
            lines.append("Status: Transcode/remux completed successfully.")
        else:
            lines.append(f"Status: Failed — {escape(type(error).__name__)}")
        return "\n".join(lines)


def import_callback(queue: QueueService):
    def evaluate(payload: dict[str, object]) -> None:
        identity = ArrIdentity(
            arr_type=str(payload["arrType"]),
            media_id=int(payload["mediaId"]),
            file_id=int(payload["fileId"]),
            preferred_language=str(payload["preferredLanguage"])
            if payload.get("preferredLanguage")
            else None,
        )
        source = PlanSource.UPGRADE if str(payload.get("eventType", "")).lower() == "upgrade" else PlanSource.IMPORT
        result = queue.evaluate_and_queue(
            str(payload["path"]), source=source, identity=identity, require_stability=False
        )
        logger.info(
            "Arr import evaluated: service=%s file=%s result=%s plan=%s",
            identity.arr_type,
            Path(result.path).name,
            result.status,
            result.plan_id or "none",
        )

    return evaluate
