"""Transactional shadow outcomes and durable numeric settlement delivery."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, exists, or_
from sqlalchemy.orm import aliased

from database import db
from models import (
    CollegeCompetitor,
    CompetitorExternalIdentity,
    Event,
    EventResult,
    ProCompetitor,
    ShadowHandicapRun,
    ShadowOutcomeRevision,
    ShadowSettlementOutbox,
    User,
)
from services.shadow_context import capture_outcome_context
from services.shadow_handicap_state import transition_shadow_run
from services.strathmark_shadow import (
    ShadowAdapterError,
    ShadowIdentityError,
    ShadowRemoteError,
    StrathmarkShadowClient,
    load_local_shadow_receipt,
)
from services.time_utils import utc_now_naive

OUTCOME_SCHEMA_VERSION = "strathmark.shadow-numeric-outcome.v1"
OUTBOX_SCHEMA_VERSION = "missoula.shadow-settlement-outbox.v1"
MAX_SETTLEMENT_SECONDS = 300.0
CORRECTION_REASON_CODES = frozenset(
    {
        "official_time_corrected",
        "official_classification_corrected",
        "timing_record_replaced",
    }
)


class ShadowOutcomeConflict(ValueError):
    """The operator attempted to correct an outcome from a stale view."""


@dataclass(frozen=True)
class OutcomeCaptureResult:
    run_id: int | None
    outcome_count: int
    numeric_action_count: int
    outbox_id: int | None


@dataclass(frozen=True)
class SettlementDeliveryResult:
    attempted: int
    recorded: int
    retryable_failed: int
    remaining_eligible: int
    status: str


def capture_shadow_outcome_revisions(
    event: Event,
    *,
    actor_id: int | None,
    source: str = "official_scoring",
    reason_code: str | None = None,
    run: ShadowHandicapRun | None = None,
) -> OutcomeCaptureResult:
    """Append operational outcomes and delivery intent in the caller's transaction."""

    if reason_code is not None and reason_code not in CORRECTION_REASON_CODES:
        raise ValueError("choose a valid outcome correction reason")
    if run is None:
        run = (
            ShadowHandicapRun.query.filter(
                ShadowHandicapRun.event_id == event.id,
                ShadowHandicapRun.lifecycle.in_(("shadow-issued", "outcomes-complete")),
            )
            .order_by(ShadowHandicapRun.created_at.desc(), ShadowHandicapRun.id.desc())
            .first()
        )
        if run is None and event.handicap_authority_mode != "shadow":
            return OutcomeCaptureResult(None, 0, 0, None)
    elif run.event_id != event.id or run.lifecycle not in {
        "shadow-issued",
        "outcomes-complete",
    }:
        raise ValueError("shadow run does not match an issued event")
    if run is None or not run.receipts:
        return OutcomeCaptureResult(None, 0, 0, None)
    actor_id = actor_id or run.issued_by_id
    actor = db.session.get(User, actor_id) if actor_id is not None else None
    if actor is None:
        return OutcomeCaptureResult(run.id, 0, 0, None)
    receipt = load_local_shadow_receipt(run)
    if receipt is None:
        return OutcomeCaptureResult(run.id, 0, 0, None)

    predictions = {
        row["competitor_id"]: row
        for row in receipt.core.get("predictions", [])
        if isinstance(row, dict) and isinstance(row.get("competitor_id"), str)
    }
    results_by_external = _result_external_identity_map(event)
    latest_by_result = _latest_outcomes(run)
    created: list[ShadowOutcomeRevision] = []
    numeric_actions: list[dict] = []
    action_kinds: list[str] = []
    replacement = False

    for external_id, prediction in predictions.items():
        result = results_by_external.get(external_id)
        if result is None:
            continue
        classification, raw_elapsed = _classify_result(result)
        previous = latest_by_result.get(result.id)
        if previous is not None and _same_outcome(previous, classification, raw_elapsed, result):
            continue

        revision = 1 if previous is None else previous.revision + 1
        outcome_id = f"missoula:outcome-revision:{uuid.uuid4()}"
        outcome = ShadowOutcomeRevision(
            outcome_revision_id=outcome_id,
            revision=revision,
            supersedes_outcome_revision_id=previous.id if previous is not None else None,
            classification=classification,
            raw_elapsed_seconds=raw_elapsed,
            official_value=float(result.result_value) if result.result_value is not None else None,
            penalty_applied=classification == "penalty",
            source=source,
            actor_id=actor_id,
            reason_code=reason_code
            or ("official_finalization" if previous is None else "official_result_correction"),
        )
        outcome.event_result_id = result.id
        run.outcome_revisions.append(outcome)
        created.append(outcome)
        capture_outcome_context(run, outcome=outcome, actor=actor)

        prior = [row for row in run.outcome_revisions if row.event_result_id == result.id][:-1]
        expected_revision, numeric_active = _numeric_state(prior)
        eligible = _eligible_numeric(classification, raw_elapsed)
        numeric = None
        if eligible:
            numeric = {
                "prediction_id": prediction["prediction_id"],
                "competitor_id": external_id,
                "event_code": receipt.core["event_code"],
                "expected_revision": expected_revision,
                "action": "settle",
                "actual_time": raw_elapsed,
            }
            replacement = replacement or (previous is not None and not numeric_active)
        elif numeric_active:
            numeric = {
                "prediction_id": prediction["prediction_id"],
                "competitor_id": external_id,
                "event_code": receipt.core["event_code"],
                "expected_revision": expected_revision,
                "action": "void",
                "actual_time": None,
            }
        if numeric is not None:
            numeric_actions.append(numeric)
            action_kinds.append(numeric["action"])

    outbox = None
    if numeric_actions:
        if "void" in action_kinds:
            reason_code = "retract_invalid_numeric_evidence"
        elif replacement:
            reason_code = "valid_replacement"
        elif any(row["expected_revision"] > 0 for row in numeric_actions):
            reason_code = "corrected_time"
        else:
            reason_code = None
        # The durable outbox row is intentionally anchored to one of the
        # immutable outcome revisions created in this transaction.  The
        # original schema enforces that linkage so an outbox payload can never
        # exist without its local operational evidence.
        batch_id = created[0].outcome_revision_id
        payload = {
            "schema_version": OUTCOME_SCHEMA_VERSION,
            "consumer_id": run.consumer_id,
            "request_id": run.request_id,
            "run_revision": run.run_revision,
            "outcome_revision_id": batch_id,
            "reason_code": reason_code,
            "revisions": numeric_actions,
            "timeout_ms": 5000,
        }
        payload_json = _canonical_json(payload)
        outbox = ShadowSettlementOutbox(
            outbox_id=f"missoula:settlement-outbox:{uuid.uuid4()}",
            outcome_revision_id=batch_id,
            schema_version=OUTBOX_SCHEMA_VERSION,
            action="void" if "void" in action_kinds else "settle",
            payload_json=payload_json,
            payload_sha256=_digest(payload_json),
            actor_id=actor_id,
            delivery_actor_id=_select_delivery_actor(run, actor).id,
            delivery_status="pending",
            attempt_count=0,
        )
        run.settlement_outbox.append(outbox)

    if created:
        db.session.flush()
        terminal_statuses = {"completed", "scratched", "dnf", "dq"}
        frozen_field_complete = all(
            external_id in results_by_external
            and results_by_external[external_id].status in terminal_statuses
            for external_id in predictions
        )
        if run.lifecycle == "shadow-issued" and frozen_field_complete:
            transition_shadow_run(
                run,
                expected_version=run.lifecycle_version,
                lifecycle="outcomes-complete",
                actor_id=actor_id,
                reason_code="official_outcomes_complete",
            )
        db.session.flush()
    return OutcomeCaptureResult(
        run.id,
        len(created),
        len(numeric_actions),
        outbox.id if outbox is not None else None,
    )


def outcome_state_token(run: ShadowHandicapRun) -> str:
    """Return a deterministic compare-and-swap token for current outcome evidence."""

    projection = [
        {
            "event_result_id": event_result_id,
            "outcome_revision_id": row.outcome_revision_id,
            "revision": row.revision,
        }
        for event_result_id, row in sorted(_latest_outcomes(run).items())
    ]
    return _digest(_canonical_json(projection))


def reconcile_shadow_outcomes(
    run: ShadowHandicapRun,
    *,
    event: Event,
    actor: User,
    expected_outcome_token: str,
    reason_code: str,
) -> OutcomeCaptureResult:
    """Append operator-confirmed corrections without changing official results."""

    if actor.role not in {User.ROLE_ADMIN, User.ROLE_JUDGE}:
        raise ValueError("judge or admin role is required for outcome correction")
    if reason_code not in CORRECTION_REASON_CODES:
        raise ValueError("choose a valid outcome correction reason")
    current_token = outcome_state_token(run)
    if not isinstance(expected_outcome_token, str) or not hmac.compare_digest(
        current_token,
        expected_outcome_token,
    ):
        raise ShadowOutcomeConflict(
            "outcome evidence changed while this page was open; reload and review again"
        )
    return capture_shadow_outcome_revisions(
        event,
        actor_id=actor.id,
        source="operator_reconciliation",
        reason_code=reason_code,
        run=run,
    )


def build_shadow_standings(run: ShadowHandicapRun) -> tuple[dict, ...]:
    """Project non-authoritative shadow metrics beside read-only official context."""

    event = db.session.get(Event, run.event_id)
    receipt = load_local_shadow_receipt(run)
    if event is None or receipt is None:
        return ()
    results = _result_external_identity_map(event)
    latest = _latest_outcomes(run)
    delivery_status = (
        run.settlement_outbox[-1].delivery_status if run.settlement_outbox else "not-required"
    )
    rows: list[dict] = []
    for prediction in sorted(
        receipt.core.get("predictions", ()),
        key=lambda value: value.get("ordinal", 0),
    ):
        external_id = prediction.get("competitor_id")
        result = results.get(external_id)
        if result is None:
            continue
        history = sorted(
            (row for row in run.outcome_revisions if row.event_result_id == result.id),
            key=lambda row: row.revision,
        )
        recorded = latest.get(result.id)
        classification, raw_elapsed = _classify_result(result)
        changed = recorded is None or not _same_outcome(
            recorded,
            classification,
            raw_elapsed,
            result,
        )
        _expected_revision, numeric_active = _numeric_state(history)
        eligible = _eligible_numeric(classification, raw_elapsed)
        if not changed:
            effect = "recorded"
        elif eligible and numeric_active:
            effect = "settle-correction"
        elif eligible:
            effect = "settle"
        elif numeric_active:
            effect = "void"
        else:
            effect = "operational-only"
        mark = prediction.get("assigned_mark")
        median = prediction.get("median_seconds")
        shadow_elapsed = (
            raw_elapsed - float(mark)
            if raw_elapsed is not None
            and isinstance(mark, (int, float))
            and not isinstance(mark, bool)
            else None
        )
        residual = (
            raw_elapsed - float(median)
            if raw_elapsed is not None
            and isinstance(median, (int, float))
            and not isinstance(median, bool)
            else None
        )
        rows.append(
            {
                "ordinal": prediction.get("ordinal"),
                "competitor_id": external_id,
                "display_name": result.competitor_name,
                "official_status": result.status,
                "official_position": result.final_position,
                "official_value": (
                    float(result.result_value) if result.result_value is not None else None
                ),
                "classification": classification,
                "raw_elapsed_seconds": raw_elapsed,
                "recommended_start_seconds": mark,
                "median_seconds": median,
                "shadow_elapsed_seconds": shadow_elapsed,
                "residual_seconds": residual,
                "shadow_rank": None,
                "recorded_revision": recorded.revision if recorded is not None else None,
                "settlement_effect": effect,
                "delivery_status": delivery_status,
                "history": tuple(
                    {
                        "revision": item.revision,
                        "classification": item.classification,
                        "raw_elapsed_seconds": item.raw_elapsed_seconds,
                        "reason_code": item.reason_code,
                        "created_at": item.created_at,
                    }
                    for item in history
                ),
            }
        )
    ranked = sorted(
        (row for row in rows if row["shadow_elapsed_seconds"] is not None),
        key=lambda row: (row["shadow_elapsed_seconds"], row["ordinal"]),
    )
    for rank, row in enumerate(ranked, start=1):
        row["shadow_rank"] = rank
    return tuple(rows)


def deliver_shadow_settlement_outbox(
    *,
    client: StrathmarkShadowClient,
    limit: int = 25,
    commit: bool = True,
) -> SettlementDeliveryResult:
    """Deliver a bounded oldest-first batch; exact payloads survive restart."""

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("settlement delivery limit must be between 1 and 100")
    now = utc_now_naive()
    older = aliased(ShadowSettlementOutbox)
    earlier_unrecorded = exists().where(
        and_(
            older.run_id == ShadowSettlementOutbox.run_id,
            older.id < ShadowSettlementOutbox.id,
            older.delivery_status != "recorded",
        )
    )
    eligible = ShadowSettlementOutbox.query.filter(
        or_(
            ShadowSettlementOutbox.delivery_status == "pending",
            and_(
                ShadowSettlementOutbox.delivery_status == "retryable-failed",
                or_(
                    ShadowSettlementOutbox.next_attempt_at.is_(None),
                    ShadowSettlementOutbox.next_attempt_at <= now,
                ),
            ),
        ),
        ~earlier_unrecorded,
    ).order_by(ShadowSettlementOutbox.id)
    dialect = db.session.get_bind().dialect.name
    if commit and dialect == "postgresql":
        rows = []
    else:
        rows = eligible.limit(limit).all()

    attempted = recorded = failed = 0

    def deliver_one(row):
        nonlocal attempted, recorded, failed
        if not hmac.compare_digest(_digest(row.payload_json), row.payload_sha256):
            raise ValueError("settlement outbox payload digest mismatch")
        payload = json.loads(row.payload_json)
        if payload.get("schema_version") != OUTCOME_SCHEMA_VERSION:
            raise ValueError("settlement outbox payload schema mismatch")
        attempted += 1
        row.attempt_count += 1
        attempted_at = utc_now_naive()
        row.last_attempt_at = attempted_at
        try:
            response = client.apply_outcome(row.run, _delivery_actor(row), payload)
            outcome = response.get("outcome") if isinstance(response, dict) else None
            if not isinstance(outcome, dict) or outcome.get("status") not in {
                "recorded",
                "duplicate",
            }:
                raise ShadowRemoteError("STRATHMARK did not record the numeric outcome")
        except ShadowAdapterError:
            row.delivery_status = "retryable-failed"
            row.next_attempt_at = attempted_at + timedelta(
                seconds=_retry_delay_seconds(row.attempt_count)
            )
            failed += 1
        else:
            row.delivery_status = "recorded"
            row.delivered_at = utc_now_naive()
            row.next_attempt_at = None
            recorded += 1

    if commit and dialect == "postgresql":
        # Claim exactly one row per transaction.  The lock remains held over
        # the bounded remote call; another worker skips it and cannot attest
        # the same immutable payload concurrently.
        for _ in range(limit):
            row = eligible.with_for_update(skip_locked=True).first()
            if row is None:
                break
            deliver_one(row)
            db.session.commit()
    else:
        for row in rows:
            deliver_one(row)
            if commit:
                db.session.commit()
        if not commit:
            db.session.flush()
    remaining_eligible = eligible.count()
    status = "failed" if failed else "incomplete" if remaining_eligible else "complete"
    return SettlementDeliveryResult(
        attempted,
        recorded,
        failed,
        remaining_eligible,
        status,
    )


def _select_delivery_actor(run: ShadowHandicapRun, local_actor: User) -> User:
    """Freeze a principal without letting optional shadow capture block scoring.

    Prefer the attributable active admin/judge chain. If none exists, retain
    the local actor as an explicitly blocked, auditable delivery principal.
    ``_delivery_actor`` rejects that row before attestation or transport.
    """

    candidates = [local_actor]
    candidates.extend(
        db.session.get(User, actor_id) if actor_id is not None else None
        for actor_id in (
            run.issued_by_id,
            run.reviewed_by_id,
            run.created_by_id,
        )
    )
    seen: set[int] = set()
    for actor in candidates:
        if actor is None or actor.id in seen:
            continue
        seen.add(actor.id)
        if actor.role not in {User.ROLE_ADMIN, User.ROLE_JUDGE}:
            continue
        if not bool(actor.is_active_user):
            continue
        return actor
    return local_actor


def _delivery_actor(row: ShadowSettlementOutbox) -> User:
    """Revalidate only the principal frozen with the immutable delivery intent."""

    actor = row.delivery_actor
    if actor is None:
        raise ShadowIdentityError("shadow settlement has no frozen delivery principal")
    if actor.role not in {User.ROLE_ADMIN, User.ROLE_JUDGE}:
        raise ShadowIdentityError("shadow settlement delivery principal is unauthorized")
    if not bool(actor.is_active_user):
        raise ShadowIdentityError("shadow settlement delivery principal is disabled")
    return actor


def _retry_delay_seconds(attempt_count: int) -> int:
    return min(300, 5 * (2 ** min(max(attempt_count - 1, 0), 6)))


def _result_external_identity_map(event: Event) -> dict[str, EventResult]:
    results = EventResult.query.filter_by(event_id=event.id).order_by(EventResult.id).all()
    model = CollegeCompetitor if event.event_type == "college" else ProCompetitor
    competitors = model.query.filter(model.id.in_([row.competitor_id for row in results])).all()
    by_id = {row.id: row for row in competitors}
    uids = [row.uid for row in competitors]
    mappings = CompetitorExternalIdentity.query.filter(
        CompetitorExternalIdentity.competitor_uid.in_(uids),
        CompetitorExternalIdentity.namespace == "strathmark",
        CompetitorExternalIdentity.status == "reviewed",
    ).all()
    by_uid = {row.competitor_uid: row.external_id for row in mappings}
    return {
        by_uid[by_id[result.competitor_id].uid]: result
        for result in results
        if result.competitor_id in by_id and by_id[result.competitor_id].uid in by_uid
    }


def _latest_outcomes(run: ShadowHandicapRun) -> dict[int, ShadowOutcomeRevision]:
    latest: dict[int, ShadowOutcomeRevision] = {}
    for row in run.outcome_revisions:
        current = latest.get(row.event_result_id)
        if current is None or row.revision > current.revision:
            latest[row.event_result_id] = row
    return latest


def _classify_result(result: EventResult) -> tuple[str, float | None]:
    status = result.status
    if status == "completed":
        raw = float(result.result_value) if result.result_value is not None else None
        if raw is not None and math.isfinite(raw) and raw > 0:
            return "valid_finish", raw
        return "timing_failure", None
    return {
        "scratched": "scratch",
        "dnf": "dnf",
        "dq": "dq",
        "partial": "timing_failure",
        "pending": "timing_failure",
    }.get(status, "no_contest"), None


def _same_outcome(previous, classification, raw_elapsed, result) -> bool:
    official = float(result.result_value) if result.result_value is not None else None
    return (
        previous.classification == classification
        and previous.raw_elapsed_seconds == raw_elapsed
        and previous.official_value == official
        and previous.penalty_applied == (classification == "penalty")
    )


def _eligible_numeric(classification: str, raw_elapsed: float | None) -> bool:
    return (
        classification == "valid_finish"
        and raw_elapsed is not None
        and 0 < raw_elapsed <= MAX_SETTLEMENT_SECONDS
    )


def _numeric_state(rows: list[ShadowOutcomeRevision]) -> tuple[int, bool]:
    revision = 0
    active = False
    for row in sorted(rows, key=lambda item: item.revision):
        eligible = _eligible_numeric(row.classification, row.raw_elapsed_seconds)
        if eligible:
            revision += 1
            active = True
        elif active:
            revision += 1
            active = False
    return revision, active


def _canonical_json(value) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "CORRECTION_REASON_CODES",
    "MAX_SETTLEMENT_SECONDS",
    "OUTBOX_SCHEMA_VERSION",
    "OUTCOME_SCHEMA_VERSION",
    "OutcomeCaptureResult",
    "ShadowOutcomeConflict",
    "SettlementDeliveryResult",
    "build_shadow_standings",
    "capture_shadow_outcome_revisions",
    "deliver_shadow_settlement_outbox",
    "outcome_state_token",
    "reconcile_shadow_outcomes",
]
