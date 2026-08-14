"""Additive persistence contract for STRATHMARK shadow handicap operations.

Every test uses the repository's temporary migrated SQLite database.  The
existing official EventResult columns are treated as immutable comparison
evidence: shadow preparation, receipt persistence, and lifecycle decisions
must never write them.
"""

import hashlib
import json
import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from models import (
    CompetitorExternalIdentity,
    Event,
    EventResult,
    ShadowContextObservation,
    ShadowHandicapRun,
    ShadowOutcomeRevision,
    ShadowReceiptRevision,
    ShadowSettlementOutbox,
)
from services.shadow_handicap_state import (
    ShadowConcurrencyError,
    derive_shadow_status,
    transition_shadow_run,
)
from tests.conftest import make_event, make_pro_competitor, make_tournament


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.fixture()
def shadow_subject(db_session):
    tournament = make_tournament(db_session)
    event = make_event(
        db_session,
        tournament,
        name="Men's Underhand",
        event_type="pro",
        scoring_type="time",
        is_handicap=True,
    )
    competitor = make_pro_competitor(
        db_session,
        tournament,
        name="Local Display Name Only",
        events=[event.name],
    )
    result = EventResult(
        event_id=event.id,
        competitor_id=competitor.id,
        competitor_type="pro",
        competitor_name=competitor.name,
        handicap_factor=17.0,
        predicted_time=41.5,
        status="pending",
    )
    db_session.add(result)
    db_session.flush()
    return tournament, event, competitor, result


def _add_mapping(db_session, competitor, reviewer_id):
    mapping = CompetitorExternalIdentity(
        competitor_uid=competitor.uid,
        namespace="strathmark",
        external_id="strathmark:competitor:8e3ed1a6-3878-4e2f-a34e-aee0ee6867fb",
        status="reviewed",
        reviewed_by_id=reviewer_id,
    )
    db_session.add(mapping)
    db_session.flush()
    return mapping


def _add_run(
    db_session,
    shadow_subject,
    actor_id,
    *,
    lifecycle="prepared",
    run_id="missoula:shadow-run:83f91c0d-e0c8-420c-94b6-65d2ea6bb0d4",
    request_id="missoula:request:f7717fb9-35dc-43d8-b4b9-92cdf36bfd86",
    run_revision="missoula:run-revision:0001",
):
    tournament, event, _competitor, _result = shadow_subject
    input_snapshot_json = _canonical_json(
        {"schema_version": "strathmark.shadow-calculate.v1"}
    )
    run = ShadowHandicapRun(
        run_id=run_id,
        request_id=request_id,
        consumer_id="missoula:consumer:pro-am",
        tournament_id=tournament.id,
        event_id=event.id,
        event_occurrence_id="missoula:event-occurrence:2027-pro-underhand",
        field_run_id="missoula:field-run:2027-pro-underhand-main",
        run_revision=run_revision,
        lifecycle=lifecycle,
        lifecycle_version=1,
        authority="shadow",
        prediction_as_of=date(2027, 5, 8),
        roster_fingerprint="1" * 64,
        schedule_fingerprint="2" * 64,
        wood_fingerprint="3" * 64,
        active_input_fingerprint="4" * 64,
        observation_schema_version="missoula.shadow-observation.v1",
        observation_fingerprint="5" * 64,
        input_snapshot_json=input_snapshot_json,
        input_snapshot_sha256=_sha256(input_snapshot_json),
        created_by_id=actor_id,
    )
    db_session.add(run)
    db_session.flush()
    return run


def test_migration_preserves_official_mark_meaning_and_defaults_to_official(
    db_session,
    shadow_subject,
):
    _tournament, event, _competitor, result = shadow_subject

    assert event.handicap_authority_mode == "official"
    assert uuid.UUID(event.shadow_event_occurrence_id.rsplit(":", 1)[-1]).version == 4
    assert uuid.UUID(event.tournament.shadow_tournament_id.rsplit(":", 1)[-1]).version == 4
    assert result.handicap_factor == 17.0
    assert result.predicted_time == 41.5

    event.handicap_authority_mode = "shadow"
    db_session.flush()

    assert result.handicap_factor == 17.0
    assert result.predicted_time == 41.5
    assert result.mark_assigned_at is None


def test_reviewed_external_identity_is_stable_and_conflicts_fail_closed(
    db_session,
    shadow_subject,
    admin_user,
):
    _tournament, _event, competitor, _result = shadow_subject
    mapping = _add_mapping(db_session, competitor, admin_user.id)

    assert mapping.external_id.startswith("strathmark:competitor:")
    assert mapping.reviewed_at is not None
    assert admin_user.shadow_actor_id.startswith("missoula:operator:")
    assert uuid.UUID(admin_user.shadow_actor_id.rsplit(":", 1)[-1]).version == 4

    duplicate = CompetitorExternalIdentity(
        competitor_uid=competitor.uid,
        namespace="strathmark",
        external_id="strathmark:competitor:another",
        status="reviewed",
        reviewed_by_id=admin_user.id,
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_shadow_run_and_receipt_are_field_atomic_and_scoring_inert(
    db_session,
    shadow_subject,
    admin_user,
):
    _tournament, _event, competitor, result = shadow_subject
    _add_mapping(db_session, competitor, admin_user.id)
    run = _add_run(db_session, shadow_subject, admin_user.id)
    core = _canonical_json(
        {
            "schema_version": "strathmark.shadow-receipt-core.v1",
            "request_id": run.request_id,
            "run_revision": run.run_revision,
            "predictions": [
                {
                    "competitor_id": ("strathmark:competitor:8e3ed1a6-3878-4e2f-a34e-aee0ee6867fb"),
                    "prediction_id": "strathmark:prediction:4f9504a2",
                    "assigned_mark": 17,
                    "median_seconds": 41.5,
                }
            ],
        }
    )
    receipt = ShadowReceiptRevision(
        run_id=run.id,
        revision=1,
        schema_version="strathmark.shadow-receipt-core.v1",
        core_json=core,
        core_sha256=_sha256(core),
        prediction_count=1,
        ledger_request_id="strathmark:ledger-request:4f9504a2",
    )
    db_session.add(receipt)
    db_session.flush()

    status = derive_shadow_status(run, current_active_fingerprint="4" * 64)

    assert status.lifecycle == "prepared"
    assert status.trust == "recorded"
    assert status.freshness == "current"
    assert status.outcomes == "none"
    assert result.handicap_factor == 17.0
    assert result.predicted_time == 41.5
    assert result.mark_assigned_at is None


def test_receipt_and_context_rows_are_append_only(
    db_session,
    shadow_subject,
    admin_user,
):
    run = _add_run(db_session, shadow_subject, admin_user.id)
    core = _canonical_json({"schema_version": "strathmark.shadow-receipt-core.v1"})
    receipt = ShadowReceiptRevision(
        run_id=run.id,
        revision=1,
        schema_version="strathmark.shadow-receipt-core.v1",
        core_json=core,
        core_sha256=_sha256(core),
        prediction_count=0,
        ledger_request_id="strathmark:ledger-request:append-only",
    )
    observation = ShadowContextObservation(
        observation_id="missoula:observation:venue:0001",
        run_id=run.id,
        schema_version="missoula.shadow-observation.v1",
        subject_type="field_run",
        subject_id=run.field_run_id,
        factor="venue",
        value_state="unknown",
        value_json=None,
        source="operator_entered",
        actor_id=admin_user.id,
    )
    db_session.add_all([receipt, observation])
    db_session.flush()

    receipt.prediction_count = 9
    observation.value_state = "known"
    observation.value_json = _canonical_json({"code": "changed"})

    with pytest.raises(ValueError, match="append-only"):
        db_session.flush()


def test_database_trigger_rejects_direct_receipt_rewrite(
    db_session,
    shadow_subject,
    admin_user,
):
    run = _add_run(db_session, shadow_subject, admin_user.id)
    core = _canonical_json({"schema_version": "strathmark.shadow-receipt-core.v1"})
    receipt = ShadowReceiptRevision(
        run_id=run.id,
        revision=1,
        schema_version="strathmark.shadow-receipt-core.v1",
        core_json=core,
        core_sha256=_sha256(core),
        prediction_count=0,
        ledger_request_id="strathmark:ledger-request:direct-rewrite",
    )
    db_session.add(receipt)
    db_session.flush()

    with pytest.raises(DBAPIError, match="append-only"):
        db_session.execute(
            text("UPDATE shadow_receipt_revisions SET prediction_count = 9 WHERE id = :receipt_id"),
            {"receipt_id": receipt.id},
        )


def test_context_requires_explicit_unknown_or_structured_known_value(
    db_session,
    shadow_subject,
    admin_user,
):
    run = _add_run(db_session, shadow_subject, admin_user.id)
    invalid = ShadowContextObservation(
        observation_id="missoula:observation:lane:0001",
        run_id=run.id,
        schema_version="missoula.shadow-observation.v1",
        subject_type="competitor_run",
        subject_id="missoula:competitor-run:0001",
        factor="lane_stand",
        value_state="unknown",
        value_json=_canonical_json({"lane": 3}),
        source="system_recorded",
        actor_id=admin_user.id,
    )
    db_session.add(invalid)
    with pytest.raises((IntegrityError, ValueError)):
        db_session.flush()


def test_lifecycle_transition_uses_optimistic_concurrency(
    db_session,
    shadow_subject,
    admin_user,
):
    run = _add_run(db_session, shadow_subject, admin_user.id)

    transition_shadow_run(
        run,
        expected_version=1,
        lifecycle="preflight-approved",
        actor_id=admin_user.id,
        reason_code="preflight_approved",
    )
    assert run.lifecycle == "preflight-approved"
    assert run.lifecycle_version == 2
    assert len(run.transitions) == 1

    with pytest.raises(ShadowConcurrencyError):
        transition_shadow_run(
            run,
            expected_version=1,
            lifecycle="calculated",
            actor_id=admin_user.id,
            reason_code="calculate",
        )


def test_latest_outcome_revision_drives_derived_outcome_axis_and_outbox(
    db_session,
    shadow_subject,
    admin_user,
):
    _tournament, _event, _competitor, result = shadow_subject
    run = _add_run(db_session, shadow_subject, admin_user.id, lifecycle="shadow-issued")
    outcome = ShadowOutcomeRevision(
        outcome_revision_id="missoula:outcome-revision:0001",
        run_id=run.id,
        event_result_id=result.id,
        revision=1,
        classification="valid_finish",
        raw_elapsed_seconds=39.25,
        official_value=39.25,
        penalty_applied=False,
        source="judge_entry",
        actor_id=admin_user.id,
        reason_code="initial_result",
    )
    payload = _canonical_json(
        {
            "schema_version": "missoula.shadow-settlement-outbox.v1",
            "outcome_revision_id": outcome.outcome_revision_id,
            "action": "settle",
        }
    )
    outbox = ShadowSettlementOutbox(
        outbox_id="missoula:settlement-outbox:0001",
        run_id=run.id,
        outcome_revision_id=outcome.outcome_revision_id,
        schema_version="missoula.shadow-settlement-outbox.v1",
        action="settle",
        payload_json=payload,
        payload_sha256=_sha256(payload),
        actor_id=admin_user.id,
        delivery_status="pending",
    )
    db_session.add_all([outcome, outbox])
    db_session.flush()

    status = derive_shadow_status(run, current_active_fingerprint="4" * 64)

    assert status.lifecycle == "shadow-issued"
    assert status.outcomes == "complete"
    assert status.settlement_backlog == 1
    assert status.mirror == "pending"
    assert result.status == "pending"
    assert result.result_value is None


def test_completed_official_result_is_unchanged_by_shadow_supersession(
    db_session,
    shadow_subject,
    admin_user,
):
    _tournament, _event, _competitor, result = shadow_subject
    result.status = "completed"
    result.result_value = 39.25
    result.final_position = 1
    db_session.flush()
    official_before = (
        result.status,
        result.result_value,
        result.final_position,
        result.handicap_factor,
        result.predicted_time,
    )

    first = _add_run(db_session, shadow_subject, admin_user.id)
    superseding = _add_run(
        db_session,
        shadow_subject,
        admin_user.id,
        run_id="missoula:shadow-run:2cb670fc-6134-42fd-a132-bd3e15a51906",
        request_id="missoula:request:45fbb986-a0df-42f5-9153-2bfd8758bdca",
        run_revision="missoula:run-revision:0002",
    )
    superseding.supersedes_run_id = first.id
    first.lifecycle = "superseded"
    first.lifecycle_version += 1
    db_session.flush()

    assert (
        result.status,
        result.result_value,
        result.final_position,
        result.handicap_factor,
        result.predicted_time,
    ) == official_before
    assert superseding.authority == "shadow"
