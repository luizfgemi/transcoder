"""Domain entities, lifecycle states, and state-machine transitions.

Contract:
  - Responsibility: Define central lifecycle enums (`MediaState`, `PlanState`, `PlanSource`),
    active/terminal state partitions, and the strict state transition table `ALLOWED_TRANSITIONS`.
  - Invariants: Transitions between plan states must strictly adhere to `ALLOWED_TRANSITIONS`;
    terminal states (SUCCEEDED, COMPLIANT, POLICY_EXCEPTION, CANCELLED) have no forward transitions
    except FAILED which may transition back to QUEUED/CANCELLED on retry.
"""

from __future__ import annotations

from enum import StrEnum


class MediaState(StrEnum):
    """Lifecycle state of a tracked media file on disk."""
    DISCOVERED = "discovered"
    COMPLIANT = "compliant"
    SUCCEEDED = "succeeded"
    POLICY_EXCEPTION = "policy_exception"
    DELETED = "deleted"


class PlanState(StrEnum):
    """Lifecycle execution state of an evaluation/transcode plan."""
    CANDIDATE = "candidate"
    QUEUED = "queued"
    RUNNING = "running"
    DEFERRED = "deferred"
    RETRY_WAIT = "retry_wait"
    POSTPROCESS_PENDING = "postprocess_pending"
    SUCCEEDED = "succeeded"
    COMPLIANT = "compliant"
    POLICY_EXCEPTION = "policy_exception"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PlanSource(StrEnum):
    MANUAL = "manual"
    IMPORT = "import"
    UPGRADE = "upgrade"
    SCAN = "scan"
    RETRY = "retry"


ACTIVE_PLAN_STATES = frozenset(
    {
        PlanState.CANDIDATE,
        PlanState.QUEUED,
        PlanState.RUNNING,
        PlanState.DEFERRED,
        PlanState.RETRY_WAIT,
        PlanState.POSTPROCESS_PENDING,
    }
)

TERMINAL_PLAN_STATES = frozenset(set(PlanState) - ACTIVE_PLAN_STATES)

ALLOWED_TRANSITIONS: dict[PlanState, frozenset[PlanState]] = {
    PlanState.CANDIDATE: frozenset(
        {PlanState.QUEUED, PlanState.COMPLIANT, PlanState.POLICY_EXCEPTION, PlanState.CANCELLED}
    ),
    PlanState.QUEUED: frozenset({PlanState.RUNNING, PlanState.DEFERRED, PlanState.CANCELLED}),
    PlanState.RUNNING: frozenset(
        {
            PlanState.QUEUED,
            PlanState.RETRY_WAIT,
            PlanState.DEFERRED,
            PlanState.POSTPROCESS_PENDING,
            PlanState.SUCCEEDED,
            PlanState.FAILED,
            PlanState.CANCELLED,
        }
    ),
    PlanState.DEFERRED: frozenset({PlanState.QUEUED, PlanState.CANCELLED}),
    PlanState.RETRY_WAIT: frozenset({PlanState.QUEUED, PlanState.FAILED, PlanState.CANCELLED}),
    PlanState.POSTPROCESS_PENDING: frozenset(
        {PlanState.POSTPROCESS_PENDING, PlanState.SUCCEEDED, PlanState.CANCELLED}
    ),
    PlanState.FAILED: frozenset({PlanState.QUEUED, PlanState.CANCELLED}),
    PlanState.SUCCEEDED: frozenset(),
    PlanState.COMPLIANT: frozenset(),
    PlanState.POLICY_EXCEPTION: frozenset(),
    PlanState.CANCELLED: frozenset(),
}


class InvalidTransition(ValueError):
    pass

