"""Whole-field operator decisions for scoring-inert STRATHMARK shadow sheets."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from database import db
from models import (
    Event,
    EventResult,
    Heat,
    ShadowFieldReview,
    ShadowHandicapRun,
    ShadowIssueArtifact,
    User,
)
from services.shadow_handicap_state import ShadowConcurrencyError, transition_shadow_run
from services.strathmark_shadow import (
    ShadowIdentityError,
    ShadowReceiptIntegrityError,
    _reviewed_competitors,
    _sha256,
    _wood_payload,
    load_local_shadow_receipt,
)
from services.time_utils import utc_now_naive

REVIEW_SCHEMA_VERSION = "missoula.shadow-field-review.v1"
EXPORT_SCHEMA_VERSION = "missoula.shadow-sheet-export.v1"


class ShadowReviewError(ValueError):
    """An operator review is incomplete, unauthorized, or stale."""


class ShadowIssueBlocked(ValueError):
    """A shadow sheet cannot be issued until its blocking evidence is corrected."""


@dataclass(frozen=True)
class ShadowOperatorView:
    primary_summary: str
    primary_action: str
    blocker_heading: str
    advisory_heading: str
    lifecycle_heading: str
    status_announcement: str
    blockers: tuple[str, ...]
    advisories: tuple[str, ...]
    predictions: tuple[Mapping[str, Any], ...]
    lifecycle: str


def build_shadow_schedule_fingerprint(event: Event) -> str:
    """Hash the ordered field, heat/run layout, stands, and flight order."""

    results = (
        EventResult.query.filter_by(event_id=event.id)
        .filter(EventResult.status == "pending")
        .order_by(EventResult.id)
        .all()
    )
    heats = Heat.query.filter_by(event_id=event.id).order_by(
        Heat.heat_number,
        Heat.run_number,
        Heat.id,
    )
    projection = {
        "schema_version": "missoula.shadow-schedule-fingerprint.v1",
        "event_id": event.shadow_event_occurrence_id,
        "field": [
            {
                "event_result_id": row.id,
                "competitor_id": row.competitor_id,
                "status": row.status,
            }
            for row in results
        ],
        "heats": [
            {
                "heat_number": heat.heat_number,
                "run_number": heat.run_number,
                "flight_id": heat.flight_id,
                "flight_position": heat.flight_position,
                "assignments": [
                    {
                        "competitor_uid": assignment.uid,
                        "stand_number": assignment.stand_number,
                    }
                    for assignment in heat.assignments
                ],
            }
            for heat in heats
        ],
    }
    return _sha256(projection)


def build_shadow_operator_view(
    run: ShadowHandicapRun,
    *,
    remote_status: Mapping[str, Any] | None,
) -> ShadowOperatorView:
    """Build one blocker-first, advisory-second operator projection."""

    blockers: list[str] = []
    advisories: list[str] = []
    predictions: tuple[Mapping[str, Any], ...] = ()

    try:
        receipt = load_local_shadow_receipt(run)
    except ShadowReceiptIntegrityError:
        receipt = None
        blockers.append("The saved STRATHMARK receipt failed its integrity check.")
    if receipt is None:
        blockers.append("A trusted STRATHMARK receipt must be recorded before review.")
    else:
        predictions = tuple(receipt.core.get("predictions", ()))

    event = db.session.get(Event, run.event_id)
    if event is None:
        blockers.append("The event no longer exists.")
    else:
        try:
            results = (
                EventResult.query.filter_by(event_id=event.id)
                .filter(EventResult.status == "pending")
                .order_by(EventResult.id)
                .all()
            )
            current_roster = _sha256(_reviewed_competitors(event, results))
            if not hmac.compare_digest(current_roster, run.roster_fingerprint):
                blockers.append("The field roster changed; prepare a new shadow run.")
        except ShadowIdentityError:
            blockers.append("The field roster has an unresolved reviewed identity mapping.")
        try:
            current_wood = _sha256(_wood_payload(event))
            if not hmac.compare_digest(current_wood, run.wood_fingerprint):
                blockers.append("The wood specification changed; prepare a new shadow run.")
        except (TypeError, ValueError):
            blockers.append("The current wood specification is missing or invalid.")
        current_schedule = build_shadow_schedule_fingerprint(event)
        if not hmac.compare_digest(current_schedule, run.schedule_fingerprint):
            blockers.append("The run order or schedule changed; prepare a new shadow run.")

    status = dict(remote_status or {})
    if not status:
        blockers.append("Confirm current STRATHMARK receipt status before review or issue.")
    else:
        trust = status.get("local_trust", status.get("trust"))
        freshness = status.get("receipt_freshness", status.get("freshness"))
        readiness = status.get("receipt_readiness", "ready" if trust == "recorded" else None)
        if trust != "recorded":
            blockers.append("The STRATHMARK receipt is not trusted.")
        if freshness != "current":
            blockers.append("The STRATHMARK receipt is not current.")
        if readiness != "ready":
            blockers.append("STRATHMARK does not report the receipt ready for review.")
        mirror = status.get("mirror", "not-configured")
        if mirror in {"pending", "retryable-failed", "permanent-failed"}:
            advisories.append(
                "Cloud mirror delivery is not complete; the durable local receipt remains usable."
            )
        drift = status.get("drift_calibration_advisory")
        if drift and drift not in {"healthy", "not-evaluated"}:
            advisories.append(f"Model monitoring advisory: {drift}.")

    if run.lifecycle == "calculated" and not blockers:
        primary_summary = "Ready to review"
        primary_action = "Review entire sheet"
    elif run.lifecycle == "reviewed" and not blockers:
        primary_summary = "Ready to issue"
        primary_action = "Issue entire shadow sheet"
    elif run.lifecycle in {"shadow-issued", "outcomes-complete"}:
        primary_summary = "Shadow sheet issued"
        primary_action = "Download checksummed export"
    else:
        primary_summary = "Action required"
        primary_action = "Resolve blockers"

    return ShadowOperatorView(
        primary_summary=primary_summary,
        primary_action=primary_action,
        blocker_heading="Action required before issue",
        advisory_heading="Advisories (do not block issue)",
        lifecycle_heading="Workflow and audit detail",
        status_announcement=f"Shadow sheet: {primary_summary}.",
        blockers=tuple(dict.fromkeys(blockers)),
        advisories=tuple(dict.fromkeys(advisories)),
        predictions=predictions,
        lifecycle=run.lifecycle,
    )


def review_shadow_sheet(
    run: ShadowHandicapRun,
    *,
    actor: User,
    expected_version: int,
    reviewed_prediction_ids: set[str],
    remote_status: Mapping[str, Any],
) -> ShadowFieldReview:
    """Record one explicit, whole-field review, including zero recommendations."""

    _require_operator(actor, ShadowReviewError)
    _require_version(run, expected_version)
    if run.lifecycle != "calculated":
        raise ShadowReviewError("only a calculated shadow sheet can be reviewed")
    view = build_shadow_operator_view(run, remote_status=remote_status)
    if view.blockers:
        raise ShadowReviewError("; ".join(view.blockers))
    receipt = load_local_shadow_receipt(run)
    if receipt is None:
        raise ShadowReviewError("trusted receipt is missing")

    expected_ids = [row.get("prediction_id") for row in view.predictions]
    if set(expected_ids) != set(reviewed_prediction_ids) or len(expected_ids) != len(
        reviewed_prediction_ids
    ):
        raise ShadowReviewError("review must explicitly accept every recommendation")
    recommendations = []
    for prediction in view.predictions:
        mark = prediction.get("assigned_mark")
        if isinstance(mark, bool) or not isinstance(mark, (int, float)) or not math.isfinite(mark):
            raise ShadowReviewError("receipt contains an invalid assigned mark")
        recommendations.append(
            {
                "ordinal": prediction.get("ordinal"),
                "prediction_id": prediction["prediction_id"],
                "competitor_id": prediction.get("competitor_id"),
                "assigned_mark": mark,
                "reviewed": True,
            }
        )
    decision = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "run_id": run.run_id,
        "run_revision": run.run_revision,
        "receipt_core_sha256": run.receipts[-1].core_sha256,
        "recommendations": recommendations,
    }
    decision_json = _canonical_json(decision)
    row = ShadowFieldReview(
        review_id=f"missoula:shadow-review:{uuid.uuid4()}",
        schema_version=REVIEW_SCHEMA_VERSION,
        receipt_core_sha256=run.receipts[-1].core_sha256,
        decision_json=decision_json,
        decision_sha256=_digest_text(decision_json),
        prediction_count=len(recommendations),
        actor_id=actor.id,
    )
    run.field_reviews.append(row)
    run.reviewed_by_id = actor.id
    run.reviewed_at = utc_now_naive()
    transition_shadow_run(
        run,
        expected_version=expected_version,
        lifecycle="reviewed",
        actor_id=actor.id,
        reason_code="whole_field_reviewed",
    )
    db.session.flush()
    return row


def issue_shadow_sheet(
    run: ShadowHandicapRun,
    *,
    actor: User,
    expected_version: int,
    remote_status: Mapping[str, Any],
) -> ShadowIssueArtifact:
    """Atomically freeze one non-importable export and issue the entire field."""

    _require_operator(actor, ShadowIssueBlocked)
    _require_version(run, expected_version)
    if run.lifecycle != "reviewed":
        raise ShadowIssueBlocked("the entire sheet must be reviewed before issue")
    view = build_shadow_operator_view(run, remote_status=remote_status)
    if view.blockers:
        raise ShadowIssueBlocked("; ".join(view.blockers))
    receipt = load_local_shadow_receipt(run)
    if receipt is None or not run.field_reviews:
        raise ShadowIssueBlocked("trusted receipt and whole-field review are required")
    review = run.field_reviews[-1]
    if review.receipt_core_sha256 != run.receipts[-1].core_sha256:
        raise ShadowIssueBlocked("review no longer matches the trusted receipt")
    if _digest_text(review.decision_json) != review.decision_sha256:
        raise ShadowIssueBlocked("saved review failed its integrity check")
    event = db.session.get(Event, run.event_id)
    if event is None:
        raise ShadowIssueBlocked("the event no longer exists")

    issue_id = f"missoula:shadow-issue:{uuid.uuid4()}"
    export = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "authority": "shadow-recommendation-only",
        "importable": False,
        "issue_id": issue_id,
        "consumer_id": run.consumer_id,
        "tournament_id": event.tournament.shadow_tournament_id,
        "event_occurrence_id": run.event_occurrence_id,
        "field_run_id": run.field_run_id,
        "run_id": run.run_id,
        "run_revision": run.run_revision,
        "receipt_core_sha256": run.receipts[-1].core_sha256,
        "review_decision_sha256": review.decision_sha256,
        "prediction_as_of": run.prediction_as_of.isoformat(),
        "recommendations": [
            {
                "ordinal": prediction.get("ordinal"),
                "competitor_id": prediction.get("competitor_id"),
                "prediction_id": prediction.get("prediction_id"),
                "recommended_start_seconds": prediction.get("assigned_mark"),
                "median_elapsed_seconds": prediction.get("median_seconds"),
                "interval": prediction.get("interval"),
                "warnings": prediction.get("warnings", []),
            }
            for prediction in view.predictions
        ],
    }
    export_json = _canonical_json(export)
    artifact = ShadowIssueArtifact(
        issue_id=issue_id,
        schema_version=EXPORT_SCHEMA_VERSION,
        receipt_core_sha256=run.receipts[-1].core_sha256,
        review_decision_sha256=review.decision_sha256,
        export_json=export_json,
        export_sha256=_digest_text(export_json),
        prediction_count=len(view.predictions),
        actor_id=actor.id,
    )
    run.issue_artifacts.append(artifact)
    run.issued_by_id = actor.id
    run.issued_at = utc_now_naive()
    transition_shadow_run(
        run,
        expected_version=expected_version,
        lifecycle="shadow-issued",
        actor_id=actor.id,
        reason_code="whole_field_shadow_issued",
    )
    db.session.flush()
    return artifact


def verify_shadow_export(artifact: ShadowIssueArtifact) -> Mapping[str, Any]:
    """Verify and decode an immutable operator export before download."""

    if not hmac.compare_digest(_digest_text(artifact.export_json), artifact.export_sha256):
        raise ShadowIssueBlocked("saved shadow export failed its integrity check")
    try:
        value = json.loads(artifact.export_json)
    except json.JSONDecodeError as exc:
        raise ShadowIssueBlocked("saved shadow export is invalid JSON") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != EXPORT_SCHEMA_VERSION
        or value.get("importable") is not False
        or value.get("issue_id") != artifact.issue_id
    ):
        raise ShadowIssueBlocked("saved shadow export contract mismatch")
    return value


def _require_operator(actor: User, error_type: type[ValueError]) -> None:
    if actor.role not in {User.ROLE_ADMIN, User.ROLE_JUDGE}:
        raise error_type("judge or admin role is required for shadow decisions")


def _require_version(run: ShadowHandicapRun, expected_version: int) -> None:
    if run.lifecycle_version != expected_version:
        raise ShadowConcurrencyError(
            f"shadow run version conflict: expected {expected_version}, current {run.lifecycle_version}"
        )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "EXPORT_SCHEMA_VERSION",
    "REVIEW_SCHEMA_VERSION",
    "ShadowIssueBlocked",
    "ShadowOperatorView",
    "ShadowReviewError",
    "build_shadow_operator_view",
    "build_shadow_schedule_fingerprint",
    "issue_shadow_sheet",
    "review_shadow_sheet",
    "verify_shadow_export",
]
