import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.database import Database
from app.domain import InvalidTransition, PlanSource, PlanState


def make_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "state.sqlite")
    db.initialize()
    return db


def media(db: Database, name: str) -> int:
    return db.upsert_media_file(
        path=f"/library/movies/{name}.mkv",
        library="movies",
        size=100,
        mtime_ns=200,
        fingerprint=f"fp-{name}",
    )


def test_migrations_are_idempotent_and_wal_enabled(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    db.initialize()
    assert db.health()["schemaVersion"] == 4
    with sqlite3.connect(db.path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_reconcile_refuses_active_destination(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    media(db, "promoted")
    media(db, "stale")
    with pytest.raises(sqlite3.IntegrityError):
        db.reconcile_media_path("/library/movies/promoted.mkv", "/library/movies/stale.mkv")


def test_reconcile_replaces_deleted_destination(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    source_id = media(db, "promoted")
    stale_id = media(db, "stale")
    stale_path = "/library/movies/stale.mkv"
    db.mark_media_deleted(stale_path)
    assert db.reconcile_media_path("/library/movies/promoted.mkv", stale_path) is True
    assert db.media_file("/library/movies/promoted.mkv") is None
    row = db.media_file(stale_path)
    assert row is not None
    assert row["id"] == source_id
    assert row["id"] != stale_id


def test_reconcile_returns_false_when_source_missing(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    assert db.reconcile_media_path("/library/movies/none.mkv", "/library/movies/nowhere.mkv") is False


def test_reconcile_scan_marks_missing_and_purges_old_deleted(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    kept = media(db, "kept")
    missing = media(db, "missing")
    db.create_plan(media_file_id=missing, source=PlanSource.SCAN, priority=30, state=PlanState.QUEUED)
    counts = db.reconcile_scan({"/library/movies/kept.mkv"})
    assert counts["marked_deleted"] == 1
    assert counts["pruned_plans"] == 0
    assert db.media_file("/library/movies/missing.mkv")["state"] == "deleted"
    with db.connection() as conn:
        assert conn.execute(
            "SELECT state FROM plans WHERE media_file_id=?", (missing,)
        ).fetchone()["state"] == "cancelled"
    with db.connection() as conn:
        conn.execute(
            "UPDATE media_files SET updated_at=? WHERE id=?",
            ("2020-01-01T00:00:00+00:00", missing),
        )
    counts = db.reconcile_scan({"/library/movies/kept.mkv"})
    assert counts["purged_deleted"] == 1
    assert db.media_file("/library/movies/missing.mkv") is None


def test_reconcile_scan_keeps_only_latest_terminal_plan(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    file_id = media(db, "one")
    old = db.create_plan(media_file_id=file_id, source=PlanSource.SCAN, priority=30, state=PlanState.FAILED)
    new = db.create_plan(
        media_file_id=file_id, source=PlanSource.MANUAL, priority=20, state=PlanState.SUCCEEDED
    )
    with db.connection() as conn:
        conn.execute(
            "UPDATE plans SET created_at=? WHERE id=?",
            ("2020-01-01T00:00:00+00:00", old),
        )
    counts = db.reconcile_scan({"/library/movies/one.mkv"})
    assert counts["pruned_plans"] == 1
    with db.connection() as conn:
        ids = [row["id"] for row in conn.execute("SELECT id FROM plans WHERE media_file_id=?", (file_id,))]
    assert ids == [new]


def test_reconcile_scan_bails_on_shrunk_inventory(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    for name in ("a", "b", "c", "d", "e"):
        media(db, name)
    counts = db.reconcile_scan({"/library/movies/a.mkv"})
    assert counts == {"marked_deleted": 0, "purged_deleted": 0, "pruned_plans": 0}
    assert db.media_file("/library/movies/b.mkv")["state"] != "deleted"


def test_upsert_revives_deleted_row(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    file_id = media(db, "gone")
    db.mark_media_deleted("/library/movies/gone.mkv")
    assert db.media_file("/library/movies/gone.mkv")["state"] == "deleted"
    revived = db.upsert_media_file(
        path="/library/movies/gone.mkv", library="movies", size=200, mtime_ns=300, fingerprint="fp-gone-2"
    )
    assert revived == file_id
    assert db.media_file("/library/movies/gone.mkv")["state"] == "discovered"


def test_only_one_active_plan_per_media(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    media_id = media(db, "one")
    db.create_plan(media_file_id=media_id, source=PlanSource.SCAN, priority=30)
    with pytest.raises(sqlite3.IntegrityError):
        db.create_plan(media_file_id=media_id, source=PlanSource.IMPORT, priority=20)


def test_claim_orders_by_priority_and_is_transactional(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    low = db.create_plan(
        media_file_id=media(db, "low"), source=PlanSource.SCAN, priority=30, state=PlanState.QUEUED
    )
    high = db.create_plan(
        media_file_id=media(db, "high"), source=PlanSource.MANUAL, priority=10, state=PlanState.QUEUED
    )
    claimed = db.claim_next("worker-1")
    assert claimed is not None
    assert claimed["id"] == high
    assert claimed["state"] == "running"
    assert db.claim_next("worker-2")["id"] == low


def test_invalid_transition_rolls_back(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    plan = db.create_plan(
        media_file_id=media(db, "transition"),
        source=PlanSource.SCAN,
        priority=30,
        state=PlanState.QUEUED,
    )
    with pytest.raises(InvalidTransition):
        db.transition_plan(plan, PlanState.SUCCEEDED)
    assert db.status()["queue"] == {"queued": 1}


def test_active_plan_ids_only_include_unfinished_plans(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    queued = db.create_plan(
        media_file_id=media(db, "queued"), source=PlanSource.SCAN, priority=30
    )
    done = db.create_plan(
        media_file_id=media(db, "done"), source=PlanSource.MANUAL, priority=10, state=PlanState.QUEUED
    )
    db.transition_plan(done, PlanState.RUNNING)
    db.transition_plan(done, PlanState.SUCCEEDED)
    assert db.active_plan_ids() == {queued}


def test_events_are_structured(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    db.append_event("startup", payload={"mode": "test", "nested": {"authToken": "must-not-leak"}})
    event = db.status()["recentEvents"][0]
    assert event["event_type"] == "startup"
    assert event["payload"] == {"mode": "test", "nested": {"authToken": "<redacted>"}}


def test_inbound_event_and_outbox_are_atomic_and_idempotent(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    operation = ("evaluate_import", {"path": "/library/movies/A.mkv"}, "import:one")
    assert db.accept_inbound_event(
        event_key="event:one", event_type="import", payload={"path": "A"}, operations=(operation,)
    )
    assert not db.accept_inbound_event(
        event_key="event:one", event_type="import", payload={"path": "A"}, operations=(operation,)
    )
    item = db.claim_outbox("worker")
    assert item is not None
    assert item["operation"] == "evaluate_import"
    assert item["attempt_count"] == 1
    db.finish_outbox(item["id"])
    assert db.claim_outbox("worker") is None


def test_migration_lookup_covers_old_and_new_paths(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    db.register_migration(
        plan_id="plan", source_path="/library/movies/A.mp4", target_path="/library/movies/A.mkv"
    )
    assert db.migration_for_path("/library/movies/A.mp4")["plan_id"] == "plan"
    assert db.migration_for_path("/library/movies/A.mkv")["plan_id"] == "plan"


def test_stuck_plans_finds_claimed_running_and_postprocess(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    running = db.create_plan(
        media_file_id=media(db, "running"), source=PlanSource.SCAN, priority=30, state=PlanState.QUEUED
    )
    assert db.claim_next("worker")["id"] == running
    post = db.create_plan(
        media_file_id=media(db, "post"), source=PlanSource.MANUAL, priority=10, state=PlanState.QUEUED
    )
    db.transition_plan(post, PlanState.RUNNING)
    db.transition_plan(post, PlanState.POSTPROCESS_PENDING)
    db.claim_postprocess("worker")
    assert sorted(plan["id"] for plan in db.stuck_plans()) == sorted([running, post])


def test_requeue_plan_releases_claim_and_restores_queue(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    plan = db.create_plan(
        media_file_id=media(db, "requeue"), source=PlanSource.SCAN, priority=30, state=PlanState.QUEUED
    )
    db.claim_next("worker")
    db.requeue_plan(plan, source=PlanSource.RETRY, expected=PlanState.RUNNING, error="crash")
    row = db.plan(plan)
    assert row["state"] == "queued"
    assert row["source"] == "retry"
    assert row["claimed_by"] is None
    assert db.claim_next("worker-2")["id"] == plan


def test_requeue_plan_rejects_unknown_state(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    plan = db.create_plan(
        media_file_id=media(db, "done"), source=PlanSource.SCAN, priority=30, state=PlanState.QUEUED
    )
    db.transition_plan(plan, PlanState.RUNNING)
    db.transition_plan(plan, PlanState.SUCCEEDED)
    with pytest.raises(InvalidTransition):
        db.requeue_plan(plan, source=PlanSource.RETRY, expected=PlanState.SUCCEEDED)


def test_fail_plan_sets_retry_state_and_next_attempt(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    plan = db.create_plan(
        media_file_id=media(db, "fail"), source=PlanSource.SCAN, priority=30, state=PlanState.QUEUED
    )
    db.claim_next("worker")
    db.fail_plan(
        plan,
        target=PlanState.RETRY_WAIT,
        next_attempt_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        error="boom",
    )
    row = db.plan(plan)
    assert row["state"] == "retry_wait"
    assert row["next_attempt_at"] == "2026-01-01T00:00:00+00:00"
    assert row["claimed_by"] is None
    assert row["last_error"] == "boom"


def test_increment_plan_attempt_counts_failures(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    plan = db.create_plan(
        media_file_id=media(db, "attempts"), source=PlanSource.SCAN, priority=30, state=PlanState.QUEUED
    )
    assert db.increment_plan_attempt(plan) == 1
    assert db.increment_plan_attempt(plan) == 2


def test_reopen_due_plans_requeues_retry_wait_and_expired_failed(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    now = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)

    def running_plan(name: str) -> str:
        plan_id = db.create_plan(
            media_file_id=media(db, name), source=PlanSource.SCAN, priority=30, state=PlanState.QUEUED
        )
        db.claim_next("worker")
        return plan_id

    retry = running_plan("retry")
    db.fail_plan(retry, target=PlanState.RETRY_WAIT, next_attempt_at=now, error="boom")
    failed = running_plan("failed")
    for _ in range(3):
        db.increment_plan_attempt(failed)
    db.fail_plan(failed, target=PlanState.FAILED, next_attempt_at=now, error="boom")
    pending = running_plan("pending")
    db.fail_plan(pending, target=PlanState.FAILED, next_attempt_at=now + timedelta(days=1), error="boom")

    assert db.reopen_due_plans(now) == 2
    retry_row = db.plan(retry)
    failed_row = db.plan(failed)
    assert retry_row["state"] == "queued"
    assert retry_row["next_attempt_at"] is None
    assert retry_row["source"] == "scan"
    assert failed_row["state"] == "queued"
    assert failed_row["source"] == "retry"
    assert failed_row["attempt_count"] == 0
    assert db.plan(pending)["state"] == "failed"


def test_reopen_due_plans_ignores_plans_not_yet_due(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    now = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    plan = db.create_plan(
        media_file_id=media(db, "later"), source=PlanSource.SCAN, priority=30, state=PlanState.QUEUED
    )
    db.claim_next("worker")
    db.fail_plan(plan, target=PlanState.FAILED, next_attempt_at=now, error="boom")
    assert db.reopen_due_plans(now - timedelta(seconds=1)) == 0
    assert db.plan(plan)["state"] == "failed"


def test_migration_lookup_by_plan_id(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    db.register_migration(
        plan_id="p1", source_path="/library/movies/A.mp4", target_path="/library/movies/A.mkv"
    )
    assert db.migration_for_plan("p1")["plan_id"] == "p1"
    assert db.migration_for_plan("missing") is None
