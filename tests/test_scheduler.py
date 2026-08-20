from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.database import Database
from app.domain import PlanSource, PlanState
from app.daemon import (
    AdaptiveScheduler,
    QueueArbiter,
    SchedulePolicy,
    SchedulerDecision,
)


TZ = ZoneInfo("America/Sao_Paulo")


def policy(*, days=(0, 1, 2, 3, 4, 5, 6), start=time(3), end=time(6), quota=2, cooldown=7):
    return SchedulePolicy(
        days=days,
        start=start,
        end=end,
        timezone=TZ,
        max_jobs=quota,
        empty_scan_cooldown_days=cooldown,
    )


def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "state.sqlite")
    db.initialize()
    return db


def add_plan(db: Database, name: str, source: PlanSource, priority: int) -> str:
    media_id = db.upsert_media_file(
        path=f"/library/movies/{name}.mkv",
        library="movies",
        size=1,
        mtime_ns=1,
        fingerprint=name,
    )
    return db.create_plan(
        media_file_id=media_id,
        source=source,
        priority=priority,
        state=PlanState.QUEUED,
    )


def test_cross_midnight_window_uses_anchor_day() -> None:
    schedule = policy(days=(0,), start=time(21), end=time(5))
    monday = datetime(2026, 8, 3, 22, tzinfo=TZ)
    tuesday_early = datetime(2026, 8, 4, 2, tzinfo=TZ)
    tuesday_late = datetime(2026, 8, 4, 6, tzinfo=TZ)
    assert schedule.active_window(monday).key == "2026-08-03"
    assert schedule.active_window(tuesday_early).key == "2026-08-03"
    assert schedule.active_window(tuesday_late) is None


def test_full_day_window_when_start_equals_end() -> None:
    schedule = policy(start=time(0), end=time(0))
    midnight = datetime(2026, 8, 6, 0, 0, tzinfo=TZ)
    noon = datetime(2026, 8, 6, 12, 0, tzinfo=TZ)
    late = datetime(2026, 8, 6, 23, 59, tzinfo=TZ)
    for now in (midnight, noon, late):
        window = schedule.active_window(now)
        assert window is not None
        assert window.key == "2026-08-06"
        assert window.start == datetime(2026, 8, 6, 0, 0, tzinfo=TZ)
        assert window.end == datetime(2026, 8, 7, 0, 0, tzinfo=TZ)
    next_day = datetime(2026, 8, 7, 0, 0, tzinfo=TZ)
    assert schedule.active_window(next_day).key == "2026-08-07"
    assert schedule.next_window_start(noon) == datetime(2026, 8, 7, 0, 0, tzinfo=TZ)


def test_initial_scan_backlog_quota_and_restart_persistence(tmp_path: Path) -> None:
    db = database(tmp_path)
    scheduler = AdaptiveScheduler(db, policy(quota=2))
    now = datetime(2026, 8, 3, 3, 30, tzinfo=TZ)
    assert scheduler.decide(now, scheduled_backlog=0) == SchedulerDecision.RUN_SCAN
    scheduler.record_scan_started(now)
    scheduler.record_scan_finished(now, candidate_count=5)
    assert scheduler.decide(now, scheduled_backlog=5) == SchedulerDecision.PROCESS_BACKLOG
    scheduler.record_scheduled_job_started(now)
    scheduler.record_scheduled_job_started(now)

    restarted = AdaptiveScheduler(db, policy(quota=2))
    assert restarted.decide(now, scheduled_backlog=3) == SchedulerDecision.QUOTA_REACHED
    next_day = now + timedelta(days=1)
    assert restarted.decide(next_day, scheduled_backlog=3) == SchedulerDecision.PROCESS_BACKLOG
    assert restarted.state().jobs_started == 0


def test_backlog_drain_schedules_confirmation_next_window(tmp_path: Path) -> None:
    scheduler = AdaptiveScheduler(database(tmp_path), policy())
    now = datetime(2026, 8, 3, 4, tzinfo=TZ)
    scheduler.record_scan_started(now)
    scheduler.record_scan_finished(now, candidate_count=1)
    state = scheduler.record_backlog_drained(now)
    assert datetime.fromisoformat(state.next_scan_at).astimezone(TZ) == datetime(2026, 8, 4, 3, tzinfo=TZ)
    assert scheduler.decide(now, scheduled_backlog=0) == SchedulerDecision.WAITING
    assert scheduler.decide(datetime(2026, 8, 4, 3, tzinfo=TZ), scheduled_backlog=0) == SchedulerDecision.RUN_SCAN


def test_empty_scan_uses_calendar_day_cooldown_and_missed_window(tmp_path: Path) -> None:
    scheduler = AdaptiveScheduler(database(tmp_path), policy(days=(0, 2), cooldown=7))
    monday = datetime(2026, 8, 3, 3, 10, tzinfo=TZ)
    scheduler.record_scan_started(monday)
    state = scheduler.record_scan_finished(monday, candidate_count=0)
    assert datetime.fromisoformat(state.next_scan_at).astimezone(TZ) == datetime(2026, 8, 10, 3, tzinfo=TZ)
    assert scheduler.decide(datetime(2026, 8, 10, 4, tzinfo=TZ), scheduled_backlog=0) == SchedulerDecision.RUN_SCAN

    second_scan = datetime(2026, 8, 10, 4, tzinfo=TZ)
    scheduler.record_scan_started(second_scan)
    scheduler.record_scan_finished(second_scan, candidate_count=0)
    # Server misses Monday and returns Tuesday: Wednesday is the next allowed window.
    assert scheduler.decide(datetime(2026, 8, 18, 12, tzinfo=TZ), scheduled_backlog=0) == SchedulerDecision.OUTSIDE_WINDOW
    assert scheduler.decide(datetime(2026, 8, 19, 3, tzinfo=TZ), scheduled_backlog=0) == SchedulerDecision.RUN_SCAN


def test_zero_quota_is_unlimited(tmp_path: Path) -> None:
    scheduler = AdaptiveScheduler(database(tmp_path), policy(quota=0))
    now = datetime(2026, 8, 3, 3, 30, tzinfo=TZ)
    for _ in range(100):
        scheduler.record_scheduled_job_started(now)
    assert scheduler.decide(now, scheduled_backlog=1) == SchedulerDecision.PROCESS_BACKLOG


def test_queue_arbiter_claims_manual_outside_window_and_scheduled_inside(tmp_path: Path) -> None:
    db = database(tmp_path)
    scheduler = AdaptiveScheduler(db, policy(quota=1))
    arbiter = QueueArbiter(db, scheduler)
    scheduled_id = add_plan(db, "scheduled", PlanSource.SCAN, 30)
    manual_id = add_plan(db, "manual", PlanSource.MANUAL, 10)
    outside = datetime(2026, 8, 3, 12, tzinfo=TZ)
    assert arbiter.claim("worker", outside)["id"] == manual_id
    assert arbiter.claim("worker", outside) is None
    inside = datetime(2026, 8, 4, 3, 30, tzinfo=TZ)
    assert arbiter.claim("worker", inside)["id"] == scheduled_id
    assert scheduler.state().jobs_started == 1


def test_scan_runs_at_most_once_per_window(tmp_path: Path) -> None:
    scheduler = AdaptiveScheduler(database(tmp_path), policy())
    now = datetime(2026, 8, 3, 3, 30, tzinfo=TZ)
    scheduler.record_scan_started(now)
    scheduler.record_scan_finished(now, candidate_count=0)
    assert scheduler.decide(now + timedelta(minutes=10), scheduled_backlog=0) == SchedulerDecision.WAITING


def test_scan_can_finish_after_window_and_restart_recovers_interruption(tmp_path: Path) -> None:
    db = database(tmp_path)
    scheduler = AdaptiveScheduler(db, policy())
    start = datetime(2026, 8, 3, 5, 59, tzinfo=TZ)
    scheduler.record_scan_started(start)
    assert scheduler.decide(start, scheduled_backlog=0) == SchedulerDecision.WAITING
    state = scheduler.record_scan_finished(datetime(2026, 8, 3, 6, 30, tzinfo=TZ), candidate_count=1)
    assert state.last_scan_window == "2026-08-03"
    assert state.backlog_active

    next_day = datetime(2026, 8, 4, 3, 5, tzinfo=TZ)
    scheduler.record_scan_started(next_day)
    restarted = AdaptiveScheduler(db, policy())
    restarted.recover_interrupted_scan(next_day + timedelta(minutes=1))
    assert restarted.decide(next_day + timedelta(minutes=1), scheduled_backlog=0) == SchedulerDecision.RUN_SCAN
