"""Release-level dress rehearsal for the scoring-inert STRATHMARK shadow path."""

from __future__ import annotations

import importlib.metadata
import json
import os
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
    from tests.test_strathmark_shadow_adapter import _receipt_response

    return _receipt_response(run, competitor_ids)


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

    prediction_ids = {row["prediction_id"] for row in first_receipt.core["predictions"]}
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
    from tests.test_strathmark_shadow_adapter import _numeric_outcome_response

    transport.queue(
        "/v1/shadow/outcomes/apply",
        _numeric_outcome_response(
            run,
            actor,
            json.loads(run.settlement_outbox[-1].payload_json),
        ),
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
        _numeric_outcome_response(
            run,
            actor,
            correction_payload,
        ),
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
    mark_workflow = (ROOT / "docs" / "MARK_ASSIGNMENT_WORKFLOW.md").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    verifier_requirements = (ROOT / "requirements-shadow-verifier.txt").read_text(encoding="utf-8")
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
    assert "strathmark[api]" in verifier_requirements.lower()
    assert "@da5c44d07311b226c1e9842104477efaf61253fa" in verifier_requirements
    verifier_install = "pip install -r requirements.txt -r requirements-shadow-verifier.txt"
    test_job = workflow.split("  test:\n", 1)[1].split("\n  postgres-smoke:\n", 1)[0]
    unit_postgres_job = workflow.split("  unit-postgres:\n", 1)[1].split("\n  lint:\n", 1)[0]
    assert verifier_install in test_job
    assert verifier_install in unit_postgres_job
    assert [
        line.strip() for line in requirements.splitlines() if line.strip() == "jsonschema==4.25.1"
    ] == ["jsonschema==4.25.1"]
    assert importlib.metadata.version("jsonschema") == "4.25.1"


def test_installed_contract_rehearsal_executes_status_and_idempotent_numeric_outcome(
    monkeypatch,
):
    from scripts import verify_strathmark_shadow_contract as verifier

    monkeypatch.setattr(
        verifier,
        "_installed_distribution_provenance",
        lambda: {
            "commit_id": verifier.EXPECTED_STRATHMARK_COMMIT,
            "direct_url": "https://github.com/SquirmyWormy275/STRATHMARK",
            "distribution_path": "isolated-unit-test",
        },
    )

    cwd_before = Path.cwd()
    artifact_values = {
        "STRATHMARK_PREDICTION_ENGINE": "legacy",
        "STRATHMARK_PREDICTION_CORE_ARTIFACT": "C:/production/core.json",
        "STRATHMARK_PREDICTION_RESIDUAL_ARTIFACT": "C:/production/residual",
    }
    for name, value in artifact_values.items():
        monkeypatch.setenv(name, value)
    result = verifier.verify()

    assert Path.cwd() == cwd_before
    assert {name: os.environ[name] for name in artifact_values} == artifact_values
    assert result["distribution_commit"] == "da5c44d07311b226c1e9842104477efaf61253fa"
    assert result["status_readiness"] == "ready"
    assert result["numeric_outcome"] == "recorded"
    assert result["numeric_outcome_retry"] == "duplicate"
    assert result["network_used"] is False
    assert result["production_data_used"] is False
    assert all(result["data_isolation"].values())
    assert result["data_isolation"]["temporary_missoula_database"] is True
    assert result["data_isolation"]["missoula_database_migrated"] is True
    assert result["data_isolation"]["calculate_payload_from_missoula_builder"] is True
    assert result["data_isolation"]["numeric_payload_from_missoula_builder"] is True
