"""SQLite database persistence, schema migrations, outbox events, and state tracking.

Contract:
  - Responsibility: Manage the SQLite storage layer for media files, evaluation cache,
    plan queues, job transitions, migrations, outbox operations, and scheduling state.
  - Invariants:
      * State transitions for plans strictly mirror `ALLOWED_TRANSITIONS`.
      * Evaluation caching is indexed by media file fingerprint and policy signature SHA-256.
      * Schema migrations are executed idempotently in sequence.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from app.domain import ALLOWED_TRANSITIONS, InvalidTransition, PlanSource, PlanState
from app.config import is_secret_key


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE media_files (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            library TEXT NOT NULL CHECK (library IN ('movies', 'series')),
            arr_type TEXT,
            arr_media_id INTEGER,
            arr_file_id INTEGER,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            fingerprint TEXT NOT NULL,
            policy_signature TEXT,
            state TEXT NOT NULL DEFAULT 'discovered',
            last_evaluated_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE evaluations (
            id INTEGER PRIMARY KEY,
            media_file_id INTEGER NOT NULL REFERENCES media_files(id) ON DELETE CASCADE,
            fingerprint TEXT NOT NULL,
            policy_signature TEXT NOT NULL,
            probe_json TEXT NOT NULL,
            criteria_json TEXT NOT NULL,
            result TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX evaluations_media_file_idx ON evaluations(media_file_id, created_at DESC);

        CREATE TABLE plans (
            id TEXT PRIMARY KEY,
            media_file_id INTEGER NOT NULL REFERENCES media_files(id) ON DELETE CASCADE,
            source TEXT NOT NULL,
            priority INTEGER NOT NULL,
            state TEXT NOT NULL,
            actions_json TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            claimed_by TEXT,
            claimed_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX plans_queue_idx ON plans(state, priority, created_at);
        CREATE UNIQUE INDEX plans_one_active_per_media_idx
            ON plans(media_file_id)
            WHERE state IN ('candidate', 'queued', 'running', 'deferred', 'retry_wait', 'postprocess_pending');

        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            state TEXT NOT NULL,
            window_key TEXT,
            quota INTEGER,
            summary_json TEXT NOT NULL DEFAULT '{}',
            started_at TEXT NOT NULL,
            finished_at TEXT
        );

        CREATE TABLE scheduler_state (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            event_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            subject_type TEXT,
            subject_id TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX events_created_idx ON events(created_at DESC);

        CREATE TABLE outbox (
            id TEXT PRIMARY KEY,
            operation TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """,
    ),
    (
        2,
        """
        CREATE TABLE file_observations (
            path TEXT PRIMARY KEY,
            library TEXT NOT NULL CHECK (library IN ('movies', 'series')),
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );
        """,
    ),
    (
        3,
        """
        ALTER TABLE outbox ADD COLUMN dedupe_key TEXT;
        CREATE UNIQUE INDEX outbox_dedupe_idx
            ON outbox(dedupe_key) WHERE dedupe_key IS NOT NULL;
        CREATE INDEX outbox_due_idx ON outbox(state, next_attempt_at, created_at);

        CREATE TABLE inbound_events (
            event_key TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE migrations (
            plan_id TEXT PRIMARY KEY,
            source_path TEXT NOT NULL,
            target_path TEXT NOT NULL,
            backup_path TEXT,
            marker_path TEXT,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX migrations_paths_idx ON migrations(source_path, target_path, state);
        """,
    ),
    (
        4,
        """
        CREATE TABLE IF NOT EXISTS quality_assessments (
            id TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            overall_score INTEGER NOT NULL,
            confidence REAL NOT NULL,
            generation TEXT NOT NULL,
            recommendation TEXT NOT NULL,
            assessment_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS quality_assessments_path_idx ON quality_assessments(path, created_at DESC);
        """,
    ),
)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            for version, sql in MIGRATIONS:
                if version in applied:
                    continue
                conn.executescript(
                    f"""
                    BEGIN IMMEDIATE;
                    {sql}
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES ({int(version)}, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
                    COMMIT;
                    """
                )
        finally:
            conn.close()

    @contextmanager
    def connection(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()

    def health(self) -> dict[str, Any]:
        with self.connection() as conn:
            version = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] or 0
            counts = {
                row["state"]: row["count"]
                for row in conn.execute("SELECT state, count(*) AS count FROM plans GROUP BY state")
            }
        return {"database": "ok", "schemaVersion": version, "plans": counts}

    def upsert_media_file(
        self,
        *,
        path: str,
        library: str,
        size: int,
        mtime_ns: int,
        fingerprint: str,
        arr_type: str | None = None,
        arr_media_id: int | None = None,
        arr_file_id: int | None = None,
    ) -> int:
        now = utc_now()
        with self.connection(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO media_files(
                    path, library, arr_type, arr_media_id, arr_file_id, size, mtime_ns,
                    fingerprint, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    library=excluded.library,
                    arr_type=coalesce(excluded.arr_type, media_files.arr_type),
                    arr_media_id=coalesce(excluded.arr_media_id, media_files.arr_media_id),
                    arr_file_id=coalesce(excluded.arr_file_id, media_files.arr_file_id),
                    size=excluded.size,
                    mtime_ns=excluded.mtime_ns,
                    fingerprint=excluded.fingerprint,
                    state=CASE WHEN media_files.state='deleted' THEN 'discovered' ELSE media_files.state END,
                    updated_at=excluded.updated_at
                """,
                (
                    path,
                    library,
                    arr_type,
                    arr_media_id,
                    arr_file_id,
                    size,
                    mtime_ns,
                    fingerprint,
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT id FROM media_files WHERE path=?", (path,)).fetchone()
            return int(row["id"])

    def observe_file(
        self,
        *,
        path: str,
        library: str,
        size: int,
        mtime_ns: int,
        required_seconds: int,
        now: datetime | None = None,
    ) -> bool:
        observed_at = now or datetime.now(UTC)
        timestamp = observed_at.isoformat()
        with self.connection(immediate=True) as conn:
            row = conn.execute(
                "SELECT size,mtime_ns,first_seen_at FROM file_observations WHERE path=?",
                (path,),
            ).fetchone()
            if row is None or (row["size"], row["mtime_ns"]) != (size, mtime_ns):
                conn.execute(
                    """
                    INSERT INTO file_observations(path,library,size,mtime_ns,first_seen_at,last_seen_at)
                    VALUES (?,?,?,?,?,?)
                    ON CONFLICT(path) DO UPDATE SET
                        library=excluded.library,
                        size=excluded.size,
                        mtime_ns=excluded.mtime_ns,
                        first_seen_at=excluded.first_seen_at,
                        last_seen_at=excluded.last_seen_at
                    """,
                    (path, library, size, mtime_ns, timestamp, timestamp),
                )
                return required_seconds == 0
            conn.execute(
                "UPDATE file_observations SET last_seen_at=? WHERE path=?",
                (timestamp, path),
            )
            first_seen = datetime.fromisoformat(row["first_seen_at"])
            return (observed_at - first_seen).total_seconds() >= required_seconds

    def record_evaluation(
        self,
        *,
        media_file_id: int,
        fingerprint: str,
        policy_signature: str,
        probe: dict[str, Any],
        plan: dict[str, Any],
        result: str,
    ) -> int:
        now = utc_now()
        with self.connection(immediate=True) as conn:
            cursor = conn.execute(
                """
                INSERT INTO evaluations(
                    media_file_id,fingerprint,policy_signature,probe_json,criteria_json,result,created_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    media_file_id,
                    fingerprint,
                    policy_signature,
                    json.dumps(probe, separators=(",", ":")),
                    json.dumps(plan, separators=(",", ":")),
                    result,
                    now,
                ),
            )
            media_state = result if result in {"compliant", "policy_exception", "succeeded"} else "discovered"
            conn.execute(
                """UPDATE media_files SET policy_signature=?,state=?,last_evaluated_at=?,updated_at=?
                   WHERE id=?""",
                (policy_signature, media_state, now, now, media_file_id),
            )
            return int(cursor.lastrowid)

    def create_plan(
        self,
        *,
        media_file_id: int,
        source: PlanSource,
        priority: int,
        actions: dict[str, Any] | None = None,
        state: PlanState = PlanState.CANDIDATE,
    ) -> str:
        plan_id = str(uuid.uuid4())
        now = utc_now()
        with self.connection(immediate=True) as conn:
            conn.execute(
                """INSERT INTO plans(
                    id, media_file_id, source, priority, state, actions_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan_id,
                    media_file_id,
                    source.value,
                    priority,
                    state.value,
                    json.dumps(actions or {}, separators=(",", ":")),
                    now,
                    now,
                ),
            )
        return plan_id

    def transition_plan(
        self,
        plan_id: str,
        target: PlanState,
        *,
        expected: PlanState | None = None,
        error: str | None = None,
    ) -> None:
        with self.connection(immediate=True) as conn:
            row = conn.execute("SELECT state FROM plans WHERE id=?", (plan_id,)).fetchone()
            if row is None:
                raise KeyError(plan_id)
            current = PlanState(row["state"])
            if expected is not None and current != expected:
                raise InvalidTransition(f"expected {expected.value}, found {current.value}")
            if target not in ALLOWED_TRANSITIONS[current]:
                raise InvalidTransition(f"cannot transition {current.value} -> {target.value}")
            conn.execute(
                """UPDATE plans SET state=?, last_error=?, claimed_by=NULL, claimed_at=NULL,
                   updated_at=? WHERE id=?""",
                (target.value, error, utc_now(), plan_id),
            )

    def claim_next(self, worker_id: str) -> dict[str, Any] | None:
        return self.claim_next_for_sources(worker_id, None)

    def claim_next_for_sources(
        self,
        worker_id: str,
        sources: tuple[PlanSource, ...] | None,
    ) -> dict[str, Any] | None:
        with self.connection(immediate=True) as conn:
            source_filter = ""
            parameters: list[Any] = []
            if sources is not None:
                if not sources:
                    return None
                placeholders = ",".join("?" for _ in sources)
                source_filter = f" AND source IN ({placeholders})"
                parameters.extend(source.value for source in sources)
            row = conn.execute(
                f"""
                SELECT * FROM plans
                WHERE state='queued'
                {source_filter}
                ORDER BY priority ASC, created_at ASC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
            if row is None:
                return None
            now = utc_now()
            updated = conn.execute(
                """UPDATE plans SET state='running', claimed_by=?, claimed_at=?, updated_at=?
                   WHERE id=? AND state='queued'""",
                (worker_id, now, now, row["id"]),
            )
            if updated.rowcount != 1:
                return None
            result = dict(row)
            result.update(state=PlanState.RUNNING.value, claimed_by=worker_id, claimed_at=now)
            result["actions"] = json.loads(result.pop("actions_json"))
            return result

    def claim_postprocess(self, worker_id: str) -> dict[str, Any] | None:
        with self.connection(immediate=True) as conn:
            row = conn.execute(
                """SELECT * FROM plans WHERE state='postprocess_pending' AND claimed_by IS NULL
                   ORDER BY updated_at LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            now = utc_now()
            updated = conn.execute(
                """UPDATE plans SET claimed_by=?,claimed_at=?,updated_at=?
                   WHERE id=? AND state='postprocess_pending' AND claimed_by IS NULL""",
                (worker_id, now, now, row["id"]),
            )
            if updated.rowcount != 1:
                return None
            result = dict(row)
            result.update(claimed_by=worker_id, claimed_at=now)
            result["actions"] = json.loads(result.pop("actions_json"))
            return result

    def count_queued_for_sources(self, sources: tuple[PlanSource, ...]) -> int:
        if not sources:
            return 0
        placeholders = ",".join("?" for _ in sources)
        with self.connection() as conn:
            return int(
                conn.execute(
                    f"SELECT count(*) FROM plans WHERE state='queued' AND source IN ({placeholders})",
                    tuple(source.value for source in sources),
                ).fetchone()[0]
            )

    def get_scheduler_value(self, key: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT value_json FROM scheduler_state WHERE key=?",
                (key,),
            ).fetchone()
        return json.loads(row["value_json"]) if row else None

    def set_scheduler_value(self, key: str, value: dict[str, Any]) -> None:
        now = utc_now()
        with self.connection(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO scheduler_state(key,value_json,updated_at) VALUES (?,?,?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at
                """,
                (key, json.dumps(value, separators=(",", ":")), now),
            )

    def scheduler_state(self) -> dict[str, Any] | None:
        return self.get_scheduler_value("scheduler_state")

    def set_scheduler_state(self, value: dict[str, Any]) -> None:
        self.set_scheduler_value("scheduler_state", value)

    def claim_immediate(self, worker_id: str) -> dict[str, Any] | None:
        return self.claim_next_for_sources(
            worker_id, (PlanSource.MANUAL, PlanSource.IMPORT, PlanSource.UPGRADE)
        )

    def claim_scheduled(self, worker_id: str) -> dict[str, Any] | None:
        return self.claim_next_for_sources(worker_id, (PlanSource.SCAN, PlanSource.RETRY))

    def status(self, *, recent_event_limit: int = 20) -> dict[str, Any]:
        with self.connection() as conn:
            queue = {
                row["state"]: row["count"]
                for row in conn.execute("SELECT state, count(*) AS count FROM plans GROUP BY state")
            }
            running = conn.execute(
                "SELECT id, media_file_id, source, state, claimed_at FROM plans WHERE state='running' LIMIT 1"
            ).fetchone()
            events = [
                {
                    **dict(row),
                    "payload": json.loads(row["payload_json"]),
                }
                for row in conn.execute(
                    """SELECT id,event_type,severity,subject_type,subject_id,payload_json,created_at
                       FROM events ORDER BY id DESC LIMIT ?""",
                    (recent_event_limit,),
                )
            ]
            for event in events:
                event.pop("payload_json", None)
        return {"queue": queue, "running": dict(running) if running else None, "recentEvents": events}

    def append_event(
        self,
        event_type: str,
        *,
        severity: str = "info",
        subject_type: str | None = None,
        subject_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        with self.connection(immediate=True) as conn:
            cursor = conn.execute(
                """INSERT INTO events(
                    event_type,severity,subject_type,subject_id,payload_json,created_at
                ) VALUES (?,?,?,?,?,?)""",
                (
                    event_type,
                    severity,
                    subject_type,
                    subject_id,
                    json.dumps(_sanitize_payload(payload or {}), separators=(",", ":")),
                    utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def accept_inbound_event(
        self,
        *,
        event_key: str,
        event_type: str,
        payload: dict[str, Any],
        operations: tuple[tuple[str, dict[str, Any], str], ...] = (),
    ) -> bool:
        """Persist an inbound event and its work atomically; duplicate delivery is harmless."""
        now = utc_now()
        sanitized = _sanitize_payload(payload)
        with self.connection(immediate=True) as conn:
            inserted = conn.execute(
                """INSERT OR IGNORE INTO inbound_events(event_key,event_type,payload_json,created_at)
                   VALUES (?,?,?,?)""",
                (event_key, event_type, json.dumps(sanitized, separators=(",", ":")), now),
            )
            if inserted.rowcount != 1:
                return False
            for operation, operation_payload, dedupe_key in operations:
                conn.execute(
                    """INSERT INTO outbox(
                        id,operation,payload_json,state,dedupe_key,created_at,updated_at
                    ) VALUES (?,?,?,'pending',?,?,?)""",
                    (
                        str(uuid.uuid4()),
                        operation,
                        json.dumps(_sanitize_payload(operation_payload), separators=(",", ":")),
                        dedupe_key,
                        now,
                        now,
                    ),
                )
        return True

    def claim_outbox(self, worker_id: str, *, now: datetime | None = None) -> dict[str, Any] | None:
        timestamp = (now or datetime.now(UTC)).isoformat()
        with self.connection(immediate=True) as conn:
            row = conn.execute(
                """SELECT * FROM outbox
                   WHERE state IN ('pending','retry_wait')
                     AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                   ORDER BY created_at,id LIMIT 1""",
                (timestamp,),
            ).fetchone()
            if row is None:
                return None
            updated = conn.execute(
                """UPDATE outbox SET state='running',attempt_count=attempt_count+1,
                   updated_at=? WHERE id=? AND state IN ('pending','retry_wait')""",
                (timestamp, row["id"]),
            )
            if updated.rowcount != 1:
                return None
            result = dict(row)
            result["state"] = "running"
            result["attempt_count"] = int(result["attempt_count"]) + 1
            result["worker_id"] = worker_id
            result["payload"] = json.loads(result.pop("payload_json"))
            return result

    def complete_outbox(self, item_id: str) -> None:
        self.finish_outbox(item_id)

    def fail_outbox(
        self,
        item_id: str,
        *,
        max_attempts: int = 5,
        retry_backoff_base_seconds: int = 10,
        retry_backoff_multiplier: int = 4,
        error: str | None = None,
    ) -> None:
        with self.connection() as conn:
            row = conn.execute("SELECT attempt_count FROM outbox WHERE id=?", (item_id,)).fetchone()
        attempts = int(row["attempt_count"]) if row else 1
        if attempts >= max_attempts:
            self.finish_outbox(item_id, error=error, give_up=True)
        else:
            delay = retry_backoff_base_seconds * (retry_backoff_multiplier ** max(attempts - 1, 0))
            self.finish_outbox(
                item_id,
                error=error,
                next_attempt_at=datetime.now(UTC) + timedelta(seconds=delay),
            )

    def finish_outbox(
        self,
        item_id: str,
        *,
        error: str | None = None,
        next_attempt_at: datetime | None = None,
        give_up: bool = False,
    ) -> None:
        state = "done" if (error is None or give_up) else "retry_wait"
        with self.connection(immediate=True) as conn:
            updated = conn.execute(
                """UPDATE outbox SET state=?,last_error=?,next_attempt_at=?,updated_at=?
                   WHERE id=? AND state='running'""",
                (
                    state,
                    error,
                    next_attempt_at.isoformat() if next_attempt_at else None,
                    utc_now(),
                    item_id,
                ),
            )
            if updated.rowcount != 1:
                raise KeyError(item_id)

    def register_migration(
        self,
        *,
        plan_id: str,
        source_path: str,
        target_path: str,
        backup_path: str | None = None,
        marker_path: str | None = None,
        state: str = "active",
    ) -> None:
        now = utc_now()
        with self.connection(immediate=True) as conn:
            conn.execute(
                """INSERT INTO migrations(
                    plan_id,source_path,target_path,backup_path,marker_path,state,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(plan_id) DO UPDATE SET
                    source_path=excluded.source_path,target_path=excluded.target_path,
                    backup_path=excluded.backup_path,marker_path=excluded.marker_path,
                    state=excluded.state,updated_at=excluded.updated_at""",
                (plan_id, source_path, target_path, backup_path, marker_path, state, now, now),
            )

    def get_cached_evaluation(
        self, fingerprint: str, policy_signature: str
    ) -> dict[str, Any] | None:
        """Fetch the most recent evaluation matching file fingerprint and policy signature.

        Contract:
          - Returns parsed probe and plan dictionaries if found, otherwise None.
        """
        with self.connection() as conn:
            row = conn.execute(
                """SELECT e.*, m.path, m.library FROM evaluations e
                   JOIN media_files m ON m.id = e.media_file_id
                   WHERE e.fingerprint = ? AND e.policy_signature = ?
                   ORDER BY e.created_at DESC LIMIT 1""",
                (fingerprint, policy_signature),
            ).fetchone()
            if row is None:
                return None
            return {
                "fingerprint": row["fingerprint"],
                "policy_signature": row["policy_signature"],
                "result": row["result"],
                "probe": json.loads(row["probe_json"]),
                "plan": json.loads(row["criteria_json"]),
            }

    cached_evaluation = get_cached_evaluation

    def migration_for_path(self, path: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """SELECT * FROM migrations
                   WHERE state IN ('active','arr_pending') AND (source_path=? OR target_path=?)
                   ORDER BY updated_at DESC LIMIT 1""",
                (path, path),
            ).fetchone()
        return dict(row) if row else None

    def reconcile_media_path(self, old_path: str, new_path: str) -> bool:
        with self.connection(immediate=True) as conn:
            existing = conn.execute(
                "SELECT id, state FROM media_files WHERE path=?", (new_path,)
            ).fetchone()
            source = conn.execute("SELECT id FROM media_files WHERE path=?", (old_path,)).fetchone()
            if source is None:
                return False
            if existing is not None and existing["id"] != source["id"]:
                if existing["state"] == "deleted":
                    conn.execute("DELETE FROM media_files WHERE id=?", (existing["id"],))
                else:
                    raise sqlite3.IntegrityError(f"media path already tracked: {new_path}")
            conn.execute(
                "UPDATE media_files SET path=?,updated_at=? WHERE id=?",
                (new_path, utc_now(), source["id"]),
            )
            return True

    def reconcile_scan(
        self, seen_paths: set[str], *, deleted_grace_hours: int = 24
    ) -> dict[str, int]:
        """Reconcile the database with the current on-disk inventory.

        Marks tracked files absent from disk as deleted (cancelling active plans),
        hard-deletes deleted rows that are still absent and older than the grace
        period, and keeps only the most recent terminal plan per media file. If the
        seen inventory is far smaller than the tracked set (e.g. a library root is
        temporarily unavailable) the reconciliation is skipped entirely.
        """
        now = utc_now()
        counts = {"marked_deleted": 0, "purged_deleted": 0, "pruned_plans": 0}
        with self.connection(immediate=True) as conn:
            conn.execute("CREATE TEMP TABLE _seen_paths (path TEXT PRIMARY KEY)")
            conn.executemany(
                "INSERT OR IGNORE INTO _seen_paths(path) VALUES (?)",
                ((path,) for path in seen_paths),
            )
            tracked = conn.execute(
                "SELECT count(*) FROM media_files WHERE state != 'deleted'"
            ).fetchone()[0]
            if tracked and len(seen_paths) * 2 < tracked:
                conn.execute("DROP TABLE _seen_paths")
                return counts
            for row in conn.execute(
                """SELECT id FROM media_files
                   WHERE state != 'deleted'
                     AND path NOT IN (SELECT path FROM _seen_paths)"""
            ):
                conn.execute(
                    """UPDATE plans SET state='cancelled',claimed_by=NULL,claimed_at=NULL,
                       last_error='source deleted',updated_at=?
                       WHERE media_file_id=? AND state IN (
                         'candidate','queued','running','deferred','retry_wait','postprocess_pending'
                       )""",
                    (now, row["id"]),
                )
                conn.execute(
                    "UPDATE media_files SET state='deleted',updated_at=? WHERE id=?",
                    (now, row["id"]),
                )
                counts["marked_deleted"] += 1
            cutoff = (datetime.now(UTC) - timedelta(hours=deleted_grace_hours)).isoformat()
            cursor = conn.execute(
                """DELETE FROM media_files
                   WHERE state='deleted' AND updated_at < ?
                     AND path NOT IN (SELECT path FROM _seen_paths)""",
                (cutoff,),
            )
            counts["purged_deleted"] = cursor.rowcount
            cursor = conn.execute(
                """DELETE FROM plans
                   WHERE state IN ('succeeded','failed','cancelled')
                     AND id NOT IN (
                       SELECT id FROM (
                         SELECT id, ROW_NUMBER() OVER (
                           PARTITION BY media_file_id ORDER BY created_at DESC, id DESC
                         ) AS rn FROM plans
                         WHERE state IN ('succeeded','failed','cancelled')
                       ) WHERE rn = 1
                     )"""
            )
            counts["pruned_plans"] = cursor.rowcount
            conn.execute("DROP TABLE _seen_paths")
        return counts

    def mark_media_deleted(self, path: str) -> int:
        now = utc_now()
        with self.connection(immediate=True) as conn:
            rows = conn.execute("SELECT id FROM media_files WHERE path=?", (path,)).fetchall()
            for row in rows:
                conn.execute(
                    """UPDATE plans SET state='cancelled',claimed_by=NULL,claimed_at=NULL,
                       last_error='source deleted',updated_at=?
                       WHERE media_file_id=? AND state IN (
                         'candidate','queued','running','deferred','retry_wait','postprocess_pending'
                       )""",
                    (now, row["id"]),
                )
            updated = conn.execute(
                "UPDATE media_files SET state='deleted',updated_at=? WHERE path=?",
                (now, path),
            )
            return int(updated.rowcount)

    def media_file(self, path: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM media_files WHERE path=?", (path,)).fetchone()
        return dict(row) if row else None

    def plan(self, plan_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["actions"] = json.loads(result.pop("actions_json"))
        return result

    def active_plan_for_media(self, media_file_id: int) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """SELECT * FROM plans WHERE media_file_id=? AND state IN (
                     'candidate','queued','running','deferred','retry_wait','postprocess_pending'
                   ) ORDER BY created_at LIMIT 1""",
                (media_file_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["actions"] = json.loads(result.pop("actions_json"))
        return result

    def active_plan_ids(self) -> set[str]:
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT id FROM plans WHERE state IN (
                     'candidate','queued','running','deferred','retry_wait','postprocess_pending'
                   )"""
            ).fetchall()
        return {str(row["id"]) for row in rows}

    def list_plans(self, state: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as conn:
            if state:
                rows = conn.execute(
                    """SELECT p.*, m.path as media_path, m.library FROM plans p
                       JOIN media_files m ON p.media_file_id = m.id
                       WHERE p.state=?
                       ORDER BY p.priority ASC, p.created_at DESC LIMIT ?""",
                    (state, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT p.*, m.path as media_path, m.library FROM plans p
                       JOIN media_files m ON p.media_file_id = m.id
                       ORDER BY p.created_at DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
        result = [dict(row) for row in rows]
        for plan in result:
            if "actions_json" in plan and plan["actions_json"]:
                plan["actions"] = json.loads(plan.pop("actions_json"))
        return result

    def prioritize_plan_now(self, plan_id: str) -> bool:
        """Elevates a plan to priority 0 (immediate run) and ensures it is in QUEUED state."""
        now = utc_now()
        with self.connection(immediate=True) as conn:
            updated = conn.execute(
                """UPDATE plans SET
                     priority=0,
                     state='queued',
                     next_attempt_at=NULL,
                     updated_at=?
                   WHERE id=? AND state IN ('queued','deferred','retry_wait')""",
                (now, plan_id),
            )
            return updated.rowcount == 1

    def stuck_plans(self) -> list[dict[str, Any]]:
        """Plans left claimed by a worker that no longer exists."""
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT * FROM plans
                   WHERE state IN ('running','postprocess_pending') AND claimed_by IS NOT NULL"""
            ).fetchall()
        result = [dict(row) for row in rows]
        for plan in result:
            plan["actions"] = json.loads(plan.pop("actions_json"))
        return result

    def requeue_plan(
        self,
        plan_id: str,
        *,
        source: PlanSource,
        error: str | None = None,
        expected: PlanState | None = None,
        next_attempt_at: datetime | None = None,
    ) -> None:
        """Release an interrupted plan back into the queue."""
        with self.connection(immediate=True) as conn:
            row = conn.execute("SELECT state FROM plans WHERE id=?", (plan_id,)).fetchone()
            if row is None:
                raise KeyError(plan_id)
            current = PlanState(row["state"])
            if expected is not None and current != expected:
                raise InvalidTransition(f"expected {expected.value}, found {current.value}")
            if PlanState.QUEUED not in ALLOWED_TRANSITIONS[current]:
                raise InvalidTransition(f"cannot requeue {current.value} -> queued")
            conn.execute(
                """UPDATE plans SET state='queued', source=?, next_attempt_at=?, last_error=?,
                   claimed_by=NULL, claimed_at=NULL, updated_at=? WHERE id=?""",
                (
                    source.value,
                    next_attempt_at.isoformat() if next_attempt_at else None,
                    error,
                    utc_now(),
                    plan_id,
                ),
            )

    def release_claim(self, plan_id: str, *, error: str | None = None) -> None:
        """Release a postprocess claim so the plan can be re-claimed."""
        with self.connection(immediate=True) as conn:
            updated = conn.execute(
                """UPDATE plans SET claimed_by=NULL, claimed_at=NULL, last_error=?, updated_at=?
                   WHERE id=? AND state='postprocess_pending'""",
                (error, utc_now(), plan_id),
            )
            if updated.rowcount != 1:
                raise KeyError(plan_id)

    def reopen_due_plans(self, now: datetime) -> int:
        """Requeue quick retries and cooldown-expired failed plans that are due."""
        timestamp = now.isoformat()
        reopened = 0
        with self.connection(immediate=True) as conn:
            due = conn.execute(
                """SELECT id, state FROM plans
                   WHERE state IN ('retry_wait','failed')
                     AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?""",
                (timestamp,),
            ).fetchall()
            for row in due:
                if row["state"] == PlanState.RETRY_WAIT.value:
                    conn.execute(
                        """UPDATE plans SET state='queued', next_attempt_at=NULL, updated_at=?
                           WHERE id=?""",
                        (utc_now(), row["id"]),
                    )
                else:
                    conn.execute(
                        """UPDATE plans SET state='queued', source='retry', attempt_count=0,
                           next_attempt_at=NULL, updated_at=? WHERE id=?""",
                        (utc_now(), row["id"]),
                    )
                reopened += 1
        return reopened

    def increment_plan_attempt(self, plan_id: str) -> int:
        with self.connection(immediate=True) as conn:
            conn.execute(
                "UPDATE plans SET attempt_count=attempt_count+1, updated_at=? WHERE id=?",
                (utc_now(), plan_id),
            )
            row = conn.execute("SELECT attempt_count FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise KeyError(plan_id)
        return int(row["attempt_count"])

    def fail_plan(
        self,
        plan_id: str,
        *,
        target: PlanState,
        next_attempt_at: datetime,
        error: str,
    ) -> None:
        """Move a running plan to a retry/failed state with a scheduled next attempt."""
        with self.connection(immediate=True) as conn:
            row = conn.execute("SELECT state FROM plans WHERE id=?", (plan_id,)).fetchone()
            if row is None:
                raise KeyError(plan_id)
            current = PlanState(row["state"])
            if current != PlanState.RUNNING:
                raise InvalidTransition(f"expected running, found {current.value}")
            if target not in ALLOWED_TRANSITIONS[PlanState.RUNNING]:
                raise InvalidTransition(f"cannot transition running -> {target.value}")
            conn.execute(
                """UPDATE plans SET state=?, next_attempt_at=?, last_error=?, claimed_by=NULL,
                   claimed_at=NULL, updated_at=? WHERE id=?""",
                (target.value, next_attempt_at.isoformat(), error, utc_now(), plan_id),
            )

    def migration_for_plan(self, plan_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """SELECT * FROM migrations
                   WHERE plan_id=? AND state IN ('active','arr_pending')
                   ORDER BY updated_at DESC LIMIT 1""",
                (plan_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_plan_actions(self, plan_id: str, actions: dict[str, Any]) -> None:
        with self.connection(immediate=True) as conn:
            updated = conn.execute(
                "UPDATE plans SET actions_json=?,updated_at=? WHERE id=?",
                (json.dumps(actions, separators=(",", ":")), utc_now(), plan_id),
            )
            if updated.rowcount != 1:
                raise KeyError(plan_id)

    def cancel_plan(self, plan_id: str) -> bool:
        with self.connection(immediate=True) as conn:
            updated = conn.execute(
                """UPDATE plans SET state='cancelled',updated_at=?,claimed_by=NULL,claimed_at=NULL
                   WHERE id=? AND state IN (
                     'candidate','queued','deferred','retry_wait'
                   )""",
                (utc_now(), plan_id),
            )
            return updated.rowcount == 1

    def list_media(self, *, state: str | None = None, query: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if state:
            clauses.append("m.state=?")
            parameters.append(state)
        if query:
            clauses.append("lower(m.path) LIKE ?")
            parameters.append(f"%{query.lower()}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(min(max(limit, 1), 500))
        with self.connection() as conn:
            rows = conn.execute(
                f"""SELECT m.*,p.id AS active_plan_id,p.state AS active_plan_state
                    FROM media_files m LEFT JOIN plans p ON p.media_file_id=m.id AND p.state IN (
                      'candidate','queued','running','deferred','retry_wait','postprocess_pending'
                    ) {where} ORDER BY m.updated_at DESC LIMIT ?""",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def record_quality_assessment(self, data: dict[str, Any]) -> None:
        with self.connection(immediate=True) as conn:
            assessment_id = str(uuid.uuid4())
            safe_data = json.loads(json.dumps(data, default=str))
            conn.execute(
                """INSERT OR REPLACE INTO quality_assessments
                   (id, path, overall_score, confidence, generation, recommendation, assessment_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    assessment_id,
                    str(data["path"]),
                    data["overall_score"],
                    data["confidence"],
                    data.get("generation", "unknown"),
                    data.get("recommendation", "keep"),
                    json.dumps(safe_data, separators=(",", ":")),
                    data.get("assessed_at") or utc_now(),
                ),
            )

    def list_quality_assessments(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT * FROM quality_assessments ORDER BY created_at DESC LIMIT ?""",
                (min(max(limit, 1), 200),),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["assessment"] = json.loads(item["assessment_json"])
            results.append(item)
        return results

    def quality_stats(self) -> dict[str, Any]:
        with self.connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM quality_assessments").fetchone()[0]
            legacy = conn.execute(
                "SELECT COUNT(*) FROM quality_assessments WHERE generation='gen_legacy_local'"
            ).fetchone()[0]
            avg_score = conn.execute(
                "SELECT AVG(overall_score) FROM quality_assessments"
            ).fetchone()[0]
        return {
            "auditedCount": total,
            "legacyCount": legacy,
            "avgScore": round(avg_score, 1) if avg_score is not None else 0,
        }


def _sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>" if is_secret_key(str(key)) else _sanitize_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_payload(item) for item in value]
    return value
