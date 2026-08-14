"""Release-level dress rehearsal for the scoring-inert STRATHMARK shadow path."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from database import db
from models import CompetitorExternalIdentity, ShadowHandicapRun, WoodConfig
from services.shadow_context import (
    FACTOR_MATRIX,
    build_context_audit_export,
    capture_event_context,
    capture_preflight_context,
    context_state_token,
    record_context_observation,
)
from services.shadow_handicap_state import transition_shadow_run
from services.shadow_operator import (
    build_shadow_schedule_fingerprint,
    issue_shadow_sheet,
    review_shadow_sheet,
    verify_shadow_export,
)
from services.shadow_settlement import (
    capture_shadow_outcome_revisions,
    deliver_shadow_settlement_outbox,
    outcome_state_token,
    reconcile_shadow_outcomes,
)
from services.strathmark_shadow import (
    ShadowClientConfig,
    ShadowRemoteNotFound,
    StrathmarkShadowClient,
    calculate_or_recover_shadow_run,
    prepare_shadow_run,
)
from tests.conftest import (
    make_event,
    make_event_result,
    make_pro_competitor,
    make_tournament,
)

ROOT = Path(__file__).resolve().parents[1]
READY_STATUS = {
    "local_trust": "recorded",
    "receipt_freshness": "current",
    "receipt_readiness": "ready",
    "mirror": "retryable-failed",
    "mirror_pending_count": 1,
}


class RehearsalTransport:
    """Deterministic transport that keeps every network boundary in-process."""

    def __init__(self):
        self.calls: list[dict] = []
        self.responses: dict[str, list[object]] = {}

    def queue(self, path: str, *responses: object) -> None:
        self.responses.setdefault(path, []).extend(responses)

    def post(self, *, base_url, path, payload, headers, timeout_seconds):
        self.calls.append(
            {
                "base_url": base_url,
                "path": path,
                "payload": payload,
                "headers": headers,
                "timeout_seconds": timeout_seconds,
            }
        )
        queued = self.responses.get(path, [])
        if not queued:
            raise AssertionError(f"unexpected STRATHMARK call: {path}")
        response = queued.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _client(transport: RehearsalTransport) -> StrathmarkShadowClient:
    return StrathmarkShadowClient(
        ShadowClientConfig(
            base_url="http://127.0.0.1:8000",
            consumer_id="missoula:service:shadow",
            service_token="release-rehearsal-service-token",
            attestation_key="release-rehearsal-attestation-key",
        ),
        transport=transport,
    )


def _calculate_response(run: ShadowHandicapRun, competitor_ids: list[str]) -> dict:
    core = {
        "schema_version": "strathmark.shadow-receipt-core.v1",
        "consumer_id": run.consumer_id,
        "request_id": run.request_id,
        "run_revision": run.run_revision,
        "event_code": "UH",
        "active_input": {"fingerprint": "a" * 64},
        "predictions": [
            {
                "ordinal": ordinal,
                "competitor_id": competitor_id,
                "prediction_id": f"strathmark:prediction:rehearsal-{ordinal}",
                "assigned_mark": mark,
                "median_seconds": 40.0 + ordinal,
                "interval": {"lower": 30.0, "upper": 55.0, "nominal": 0.9},
                "warnings": [],
            }
            for ordinal, (competitor_id, mark) in enumerate(zip(competitor_ids, (0, 7)))
        ],
    }
    core_json = json.dumps(core, sort_keys=True, separators=(",", ":"))
    receipt = {
        "core_json": core_json,
        "core": core,
        "status": {"trust": "recorded", "freshness": "current"},
    }
    return {
        "schema_version": "strathmark.shadow-calculate-response.v1",
        "trusted": True,
        "receipt": receipt,
        "status": dict(READY_STATUS),
        "draft_predictions": [],
    }


@pytest.fixture()
def rehearsal_field(db_session, admin_user):
    tournament = make_tournament(db_session, year=2027)
    event = make_event(
        db_session,
        tournament,
        name="Underhand",
        event_type="pro",
        gender="M",
        stand_type="underhand",
        scoring_type="time",
        is_handicap=True,
    )
    event.handicap_authority_mode = "shadow"
    competitors = [
        make_pro_competitor(db_session, tournament, "Private A", "M", events=[event.name]),
        make_pro_competitor(db_session, tournament, "Private B", "M", events=[event.name]),
    ]
    results = [make_event_result(db_session, event, row) for row in competitors]
    competitor_ids = []
    for ordinal, competitor in enumerate(competitors, start=1):
        external_id = f"strathmark:competitor:release-{ordinal}"
        competitor_ids.append(external_id)
        db_session.add(
            CompetitorExternalIdentity(
                competitor_uid=competitor.uid,
                namespace="strathmark",
                external_id=external_id,
                status="reviewed",
                reviewed_by_id=admin_user.id,
            )
        )
    db_session.add(
        WoodConfig(
            tournament_id=tournament.id,
            config_key="block_underhand_pro_M",
            species="Western White Pine",
            size_value=13.0,
            size_unit="in",
        )
    )
    db_session.flush()
    return tournament, event, results, competitor_ids, admin_user


def test_complete_shadow_dress_rehearsal_is_atomic_recoverable_and_scoring_inert(
    rehearsal_field,
):
    tournament, event, results, competitor_ids, actor = rehearsal_field
    official_before = [
        (row.handicap_factor, row.predicted_time, row.mark_assigned_at) for row in results
    ]

    run = prepare_shadow_run(
        event,
        actor=actor,
        prediction_as_of=date(2027, 5, 8),
        schedule_fingerprint=build_shadow_schedule_fingerprint(event),
        observation_schema_version="strathmark.shadow-observation-fingerprint.v1",
        observation_fingerprint="2" * 64,
    )
    capture_event_context(run, event=event, actor=actor)
    transition_shadow_run(
        run,
        expected_version=run.lifecycle_version,
        lifecycle="preflight-approved",
        actor_id=actor.id,
        reason_code="release_rehearsal_preflight",
    )
    capture_preflight_context(run, event=event, actor=actor)

    transport = RehearsalTransport()
    transport.queue("/v1/shadow/receipts/lookup", ShadowRemoteNotFound("missing"))
    transport.queue("/v1/shadow/calculate", _calculate_response(run, competitor_ids))
    client = _client(transport)
    first_receipt = calculate_or_recover_shadow_run(run, client=client)
    db.session.flush()

    restarted_transport = RehearsalTransport()
    restarted_receipt = calculate_or_recover_shadow_run(run, client=_client(restarted_transport))
    assert restarted_receipt.core_json == first_receipt.core_json
    assert restarted_transport.calls == []

    prediction_ids = {
        row["prediction_id"] for row in first_receipt.core["predictions"]
    }
    review_shadow_sheet(
        run,
        actor=actor,
        expected_version=run.lifecycle_version,
        reviewed_prediction_ids=prediction_ids,
        remote_status=READY_STATUS,
    )
    artifact = issue_shadow_sheet(
        run,
        actor=actor,
        expected_version=run.lifecycle_version,
        remote_status=READY_STATUS,
    )
    export = verify_shadow_export(artifact)
    assert export["authority"] == "shadow-recommendation-only"
    assert export["importable"] is False
    assert len(export["recommendations"]) == 2

    record_context_observation(
        run,
        factor="venue",
        subject_type="tournament_day",
        subject_id="missoula:tournament-day:2027-05-09",
        value_state="known",
        value={"venue_code": "missoula:venue:fairgrounds"},
        source="operator_entered",
        actor=actor,
        expected_context_token=context_state_token(run),
    )
    record_context_observation(
        run,
        factor="weather",
        subject_type="field",
        subject_id=run.field_run_id,
        value_state="unknown",
        value=None,
        source="operator_entered",
        actor=actor,
        expected_context_token=context_state_token(run),
    )

    results[0].status = "completed"
    results[0].result_value = 42.5
    results[1].status = "dnf"
    results[1].result_value = None
    captured = capture_shadow_outcome_revisions(event, actor_id=actor.id)
    assert captured.outcome_count == 2
    assert captured.numeric_action_count == 1
    assert run.lifecycle == "outcomes-complete"

    transport.queue(
        "/v1/shadow/outcomes/apply",
        {
            "schema_version": "strathmark.shadow-numeric-outcome-response.v1",
            "outcome": {"status": "recorded"},
        },
    )
    delivered = deliver_shadow_settlement_outbox(client=client, commit=False)
    assert (delivered.recorded, delivered.retryable_failed) == (1, 0)

    correction_token = outcome_state_token(run)
    results[0].status = "dq"
    results[0].result_value = None
    corrected = reconcile_shadow_outcomes(
        run,
        event=event,
        actor=actor,
        expected_outcome_token=correction_token,
        reason_code="official_classification_corrected",
    )
    assert corrected.numeric_action_count == 1
    correction_payload = json.loads(run.settlement_outbox[-1].payload_json)
    assert correction_payload["revisions"][0]["action"] == "void"
    transport.queue(
        "/v1/shadow/outcomes/apply",
        {
            "schema_version": "strathmark.shadow-numeric-outcome-response.v1",
            "outcome": {"status": "recorded"},
        },
    )
    redelivery = deliver_shadow_settlement_outbox(client=client, commit=False)
    assert redelivery.recorded == 1

    context_export = build_context_audit_export(run, actor=actor)
    assert context_export.payload["numeric_inactive"] is True
    assert {row["factor"] for row in context_export.payload["factors"]} == set(FACTOR_MATRIX)
    serialized_context = context_export.payload_json.lower()
    assert "private a" not in serialized_context
    assert "private b" not in serialized_context
    assert [
        (row.handicap_factor, row.predicted_time, row.mark_assigned_at) for row in results
    ] == official_before
    assert [call["path"] for call in transport.calls] == [
        "/v1/shadow/receipts/lookup",
        "/v1/shadow/calculate",
        "/v1/shadow/outcomes/apply",
        "/v1/shadow/outcomes/apply",
    ]


def test_release_docs_and_dependency_comments_describe_the_real_shadow_authority():
    mark_workflow = (ROOT / "docs" / "MARK_ASSIGNMENT_WORKFLOW.md").read_text(
        encoding="utf-8"
    )
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    release = (ROOT / "docs" / "RELEASE_CHECKLIST.md").read_text(encoding="utf-8")
    rollback = (ROOT / "docs" / "ROLLBACK_SOP.md").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    for phrase in (
        "shadow recommendation only",
        "exclusive UTC",
        "receipt lookup",
        "whole-field",
        "numeric settlement",
        "official mark fields",
    ):
        assert phrase.lower() in mark_workflow.lower()
    assert "Manual > LLM" not in mark_workflow
    assert "Ollama" not in mark_workflow
    assert "prediction cascade" not in requirements.lower()
    assert "STRATHMARK shadow" in release
    assert "STRATHMARK shadow" in rollback
    assert "python scripts/verify_strathmark_shadow_contract.py" in workflow
    assert "@9c021c5" not in requirements
