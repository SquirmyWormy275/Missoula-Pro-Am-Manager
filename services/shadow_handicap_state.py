"""Optimistic lifecycle decisions and derived shadow status projections."""

from dataclasses import dataclass

from models.shadow_handicap import ShadowLifecycleTransition


class ShadowConcurrencyError(RuntimeError):
    """A state-changing request was based on an obsolete run revision."""


_TRANSITIONS = {
    "draft": {"prepared", "cancelled"},
    "prepared": {"preflight-approved", "cancelled", "superseded"},
    "preflight-approved": {"calculated", "superseded", "cancelled"},
    "calculated": {"reviewed", "superseded", "cancelled"},
    "reviewed": {"shadow-issued", "superseded", "cancelled"},
    "shadow-issued": {"outcomes-complete", "superseded"},
    "outcomes-complete": {"superseded"},
    "superseded": set(),
    "cancelled": set(),
}


@dataclass(frozen=True)
class ShadowStatus:
    lifecycle: str
    trust: str
    mirror: str
    freshness: str
    outcomes: str
    settlement_backlog: int
    ready_for_review: bool


def transition_shadow_run(run, *, expected_version, lifecycle, actor_id, reason_code):
    """Append a decision and advance a run using compare-and-swap semantics."""
    if run.lifecycle_version != expected_version:
        raise ShadowConcurrencyError(
            f"shadow run version conflict: expected {expected_version}, "
            f"current {run.lifecycle_version}"
        )
    if lifecycle not in _TRANSITIONS.get(run.lifecycle, set()):
        raise ValueError(f"invalid shadow lifecycle transition {run.lifecycle!r} -> {lifecycle!r}")
    if not reason_code:
        raise ValueError("reason_code is required for lifecycle decisions")

    next_version = expected_version + 1
    run.transitions.append(
        ShadowLifecycleTransition(
            from_lifecycle=run.lifecycle,
            to_lifecycle=lifecycle,
            run_version=next_version,
            actor_id=actor_id,
            reason_code=reason_code,
        )
    )
    run.lifecycle = lifecycle
    run.lifecycle_version = next_version
    return run


def derive_shadow_status(run, *, current_active_fingerprint):
    """Project independent workflow axes without persisting a misleading enum."""
    receipts = list(run.receipts)
    outbox = list(run.settlement_outbox)
    outcomes = list(run.outcome_revisions)

    trust = "recorded" if receipts else "unrecorded"
    freshness = "current" if current_active_fingerprint == run.active_input_fingerprint else "stale"

    if not outbox:
        mirror = "not-configured"
    elif any(row.delivery_status == "retryable-failed" for row in outbox):
        mirror = "retryable-failed"
    elif any(row.delivery_status == "pending" for row in outbox):
        mirror = "pending"
    else:
        mirror = "recorded"

    latest_by_result = {}
    corrected = False
    for row in outcomes:
        previous = latest_by_result.get(row.event_result_id)
        if previous is None or row.revision > previous.revision:
            latest_by_result[row.event_result_id] = row
        corrected = corrected or row.revision > 1

    if not latest_by_result:
        outcome_axis = "none"
    elif corrected:
        outcome_axis = "corrected"
    else:
        expected = receipts[-1].prediction_count if receipts else len(latest_by_result)
        outcome_axis = "complete" if len(latest_by_result) >= expected else "partial"

    backlog = sum(row.delivery_status != "recorded" for row in outbox)
    ready_for_review = (
        trust == "recorded"
        and freshness == "current"
        and run.lifecycle in {"calculated", "reviewed", "shadow-issued", "outcomes-complete"}
    )
    return ShadowStatus(
        lifecycle=run.lifecycle,
        trust=trust,
        mirror=mirror,
        freshness=freshness,
        outcomes=outcome_axis,
        settlement_backlog=backlog,
        ready_for_review=ready_for_review,
    )
