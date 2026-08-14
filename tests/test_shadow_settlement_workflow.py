"""Transactional operational outcomes and STRATHMARK numeric settlement outbox."""

import hashlib
import json
from datetime import date
from unittest.mock import patch

import pytest

from database import db
from models import (
    CompetitorExternalIdentity,
    ShadowHandicapRun,
    ShadowReceiptRevision,
    ShadowSettlementOutbox,
    WoodConfig,
)
from services.scoring_workflow import finalize_event_results
from services.shadow_handicap_state import transition_shadow_run
from services.shadow_operator import build_shadow_schedule_fingerprint
from services.shadow_settlement import (
    ShadowOutcomeConflict,
    build_shadow_standings,
    capture_shadow_outcome_revisions,
    deliver_shadow_settlement_outbox,
    outcome_state_token,
    reconcile_shadow_outcomes,
)
from services.strathmark_shadow import ShadowRemoteUnavailable, prepare_shadow_run
from tests.conftest import (
    make_event,
    make_event_result,
    make_pro_competitor,
    make_tournament,
)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@pytest.fixture()
def issued_shadow_run(db_session, admin_user):
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
        make_pro_competitor(db_session, tournament, "Private One", "M", events=[event.name]),
        make_pro_competitor(db_session, tournament, "Private Two", "M", events=[event.name]),
    ]
    results = [make_event_result(db_session, event, row) for row in competitors]
    external_ids = []
    for ordinal, competitor in enumerate(competitors, start=1):
        external_id = f"strathmark:competitor:settlement-{ordinal}"
        external_ids.append(external_id)
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
    run = prepare_shadow_run(
        event,
        actor=admin_user,
        prediction_as_of=date(2027, 5, 8),
        schedule_fingerprint=build_shadow_schedule_fingerprint(event),
        observation_schema_version="strathmark.shadow-observation-fingerprint.v1",
        observation_fingerprint="2" * 64,
    )
    transition_shadow_run(
        run,
        expected_version=1,
        lifecycle="preflight-approved",
        actor_id=admin_user.id,
        reason_code="preflight_approved",
    )
    core = {
        "schema_version": "strathmark.shadow-receipt-core.v1",
        "consumer_id": run.consumer_id,
        "request_id": run.request_id,
        "run_revision": run.run_revision,
        "event_code": "UH",
        "active_input": {"fingerprint": run.active_input_fingerprint},
        "predictions": [
            {
                "ordinal": ordinal,
                "competitor_id": competitor_id,
                "prediction_id": f"strathmark:prediction:settlement-{ordinal}",
                "assigned_mark": ordinal,
                "median_seconds": 40.0 + ordinal,
            }
            for ordinal, competitor_id in enumerate(external_ids)
        ],
    }
    core_json = _canonical(core)
    run.receipts.append(
        ShadowReceiptRevision(
            revision=1,
            schema_version=core["schema_version"],
            core_json=core_json,
            core_sha256=hashlib.sha256(core_json.encode()).hexdigest(),
            prediction_count=2,
            ledger_request_id=f"strathmark:ledger-request:settlement-{run.id}",
        )
    )
    transition_shadow_run(
        run,
        expected_version=2,
        lifecycle="calculated",
        actor_id=admin_user.id,
        reason_code="trusted_receipt_recorded",
    )
    transition_shadow_run(
        run,
        expected_version=3,
        lifecycle="reviewed",
        actor_id=admin_user.id,
        reason_code="whole_field_reviewed",
    )
    transition_shadow_run(
        run,
        expected_version=4,
        lifecycle="shadow-issued",
        actor_id=admin_user.id,
        reason_code="whole_field_shadow_issued",
    )
    run.reviewed_by_id = admin_user.id
    run.issued_by_id = admin_user.id
    db_session.flush()
    return tournament, event, results, run, admin_user


def test_finalization_captures_all_outcomes_and_only_eligible_numeric_evidence(
    issued_shadow_run,
):
    _tournament, event, results, run, actor = issued_shadow_run
    results[0].status = "completed"
    results[0].result_value = 42.5
    results[1].status = "dnf"
    results[1].result_value = None

    captured = capture_shadow_outcome_revisions(event, actor_id=actor.id)
    db.session.flush()

    assert captured.outcome_count == 2
    assert captured.numeric_action_count == 1
    assert run.lifecycle == "outcomes-complete"
    latest = sorted(run.outcome_revisions, key=lambda row: row.event_result_id)
    assert [row.classification for row in latest] == ["valid_finish", "dnf"]
    assert latest[0].raw_elapsed_seconds == 42.5
    assert latest[1].raw_elapsed_seconds is None
    assert len(run.settlement_outbox) == 1
    payload = json.loads(run.settlement_outbox[0].payload_json)
    assert payload["schema_version"] == "strathmark.shadow-numeric-outcome.v1"
    assert payload["reason_code"] is None
    assert payload["revisions"] == [
        {
            "action": "settle",
            "actual_time": 42.5,
            "competitor_id": "strathmark:competitor:settlement-1",
            "event_code": "UH",
            "expected_revision": 0,
            "prediction_id": "strathmark:prediction:settlement-0",
        }
    ]
    serialized = run.settlement_outbox[0].payload_json.lower()
    assert "private one" not in serialized
    assert "status_reason" not in serialized


def test_scoring_finalization_commits_outcomes_and_outbox_without_official_marks(
    issued_shadow_run,
    monkeypatch,
):
    tournament, event, results, run, actor = issued_shadow_run
    for offset, result in enumerate(results):
        result.status = "completed"
        result.result_value = 42.5 + offset
    official_before = [
        (result.handicap_factor, result.predicted_time, result.mark_assigned_at)
        for result in results
    ]

    # Preserve the suite's rollback isolation while exercising the production
    # transaction boundary.  The workflow's commit is represented by a flush
    # here so this test cannot leak rows into the next test's SQLite database.
    monkeypatch.setattr(db.session, "commit", db.session.flush)
    outcome = finalize_event_results(
        event=event,
        tournament_id=tournament.id,
        judge_user_id=actor.id,
    )

    assert outcome["ok"] is True
    assert event.is_finalized is True
    assert run.lifecycle == "outcomes-complete"
    assert len(run.outcome_revisions) == 2
    assert len(run.settlement_outbox) == 1
    assert run.settlement_outbox[0].delivery_status == "pending"
    assert [
        (result.handicap_factor, result.predicted_time, result.mark_assigned_at)
        for result in results
    ] == official_before


def test_exact_recapture_is_idempotent(issued_shadow_run):
    _tournament, event, results, run, actor = issued_shadow_run
    for offset, result in enumerate(results):
        result.status = "completed"
        result.result_value = 41.0 + offset
    first = capture_shadow_outcome_revisions(event, actor_id=actor.id)
    second = capture_shadow_outcome_revisions(event, actor_id=actor.id)

    assert first.outcome_count == 2
    assert second.outcome_count == 0
    assert len(run.outcome_revisions) == 2
    assert len(run.settlement_outbox) == 1


def test_partial_field_records_context_but_is_not_outcomes_complete(issued_shadow_run):
    _tournament, event, results, run, actor = issued_shadow_run
    results[0].status = "completed"
    results[0].result_value = 40.0
    results[1].status = "partial"
    results[1].result_value = None

    capture_shadow_outcome_revisions(event, actor_id=actor.id)

    assert run.lifecycle == "shadow-issued"
    assert [row.classification for row in run.outcome_revisions] == [
        "valid_finish",
        "timing_failure",
    ]

    results[1].status = "dnf"
    capture_shadow_outcome_revisions(event, actor_id=actor.id)
    assert run.lifecycle == "outcomes-complete"


def test_finish_to_dq_appends_void_with_reason_and_expected_revision(issued_shadow_run):
    _tournament, event, results, run, actor = issued_shadow_run
    results[0].status = "completed"
    results[0].result_value = 39.2
    results[1].status = "dnf"
    capture_shadow_outcome_revisions(event, actor_id=actor.id)

    results[0].status = "dq"
    results[0].status_reason = "official decision stays local"
    results[0].result_value = None
    correction = capture_shadow_outcome_revisions(event, actor_id=actor.id)

    assert correction.outcome_count == 1
    assert len(run.settlement_outbox) == 2
    payload = json.loads(run.settlement_outbox[-1].payload_json)
    assert payload["reason_code"] == "retract_invalid_numeric_evidence"
    assert payload["revisions"] == [
        {
            "action": "void",
            "actual_time": None,
            "competitor_id": "strathmark:competitor:settlement-1",
            "event_code": "UH",
            "expected_revision": 1,
            "prediction_id": "strathmark:prediction:settlement-0",
        }
    ]
    revisions = [row for row in run.outcome_revisions if row.event_result_id == results[0].id]
    assert [row.revision for row in revisions] == [1, 2]
    assert revisions[-1].supersedes_outcome_revision_id == revisions[0].id


def test_dnf_to_valid_finish_is_first_numeric_revision_and_valid_replacement(
    issued_shadow_run,
):
    _tournament, event, results, run, actor = issued_shadow_run
    results[0].status = "dnf"
    results[1].status = "dnf"
    capture_shadow_outcome_revisions(event, actor_id=actor.id)
    assert run.settlement_outbox == []

    results[0].status = "completed"
    results[0].result_value = 44.1
    capture_shadow_outcome_revisions(event, actor_id=actor.id)

    payload = json.loads(run.settlement_outbox[-1].payload_json)
    assert payload["reason_code"] == "valid_replacement"
    assert payload["revisions"][0]["expected_revision"] == 0
    assert payload["revisions"][0]["action"] == "settle"


class FakeOutcomeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def apply_outcome(self, run, actor, payload):
        self.calls.append((run.run_id, actor.shadow_actor_id, payload))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_worker_retries_exact_payload_after_restart_without_losing_intent(
    issued_shadow_run,
):
    _tournament, event, results, run, actor = issued_shadow_run
    results[0].status = "completed"
    results[0].result_value = 41.3
    results[1].status = "dnf"
    capture_shadow_outcome_revisions(event, actor_id=actor.id)
    row = run.settlement_outbox[-1]
    original_payload = row.payload_json

    offline = FakeOutcomeClient([ShadowRemoteUnavailable("offline")])
    first = deliver_shadow_settlement_outbox(client=offline, limit=10, commit=False)
    assert first.retryable_failed == 1
    assert row.delivery_status == "retryable-failed"
    assert row.payload_json == original_payload

    restarted = FakeOutcomeClient(
        [
            {
                "schema_version": "strathmark.shadow-numeric-outcome-response.v1",
                "outcome": {"status": "recorded"},
            }
        ]
    )
    second = deliver_shadow_settlement_outbox(client=restarted, limit=10, commit=False)
    assert second.recorded == 1
    assert row.delivery_status == "recorded"
    assert restarted.calls[0][2] == json.loads(original_payload)


def test_shadow_finalization_never_calls_legacy_supabase_sync(issued_shadow_run):
    from routes.scoring import _push_strathmark_results

    tournament, event, _results, _run, _actor = issued_shadow_run
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "services.strathmark_sync.push_pro_event_results",
            lambda *_args, **_kwargs: pytest.fail("legacy sync must remain inactive"),
        )
        _push_strathmark_results(event, tournament.id)


def test_outbox_payload_digest_detects_local_tamper(issued_shadow_run):
    _tournament, event, results, run, actor = issued_shadow_run
    results[0].status = "completed"
    results[0].result_value = 41.3
    results[1].status = "dnf"
    capture_shadow_outcome_revisions(event, actor_id=actor.id)
    row = run.settlement_outbox[-1]
    row.payload_json = row.payload_json.replace("41.3", "41.4")

    with pytest.raises(ValueError, match="digest"):
        deliver_shadow_settlement_outbox(
            client=FakeOutcomeClient([]),
            limit=10,
            commit=False,
        )


def test_shadow_standings_are_separate_from_championship_results(issued_shadow_run):
    _tournament, event, results, run, actor = issued_shadow_run
    results[0].status = "completed"
    results[0].result_value = 42.5
    results[0].final_position = 2
    results[1].status = "completed"
    results[1].result_value = 43.0
    results[1].final_position = 1
    capture_shadow_outcome_revisions(event, actor_id=actor.id)

    standings = build_shadow_standings(run)

    assert [row["official_position"] for row in standings] == [2, 1]
    assert [row["shadow_elapsed_seconds"] for row in standings] == [42.5, 42.0]
    assert [row["shadow_rank"] for row in standings] == [2, 1]
    assert [row["residual_seconds"] for row in standings] == [2.5, 2.0]
    assert [row["classification"] for row in standings] == ["valid_finish", "valid_finish"]
    assert [(row.handicap_factor, row.predicted_time, row.mark_assigned_at) for row in results] == [
        (0.0, None, None),
        (0.0, None, None),
    ]


def test_operator_reconciliation_requires_reason_and_compare_and_swap(issued_shadow_run):
    _tournament, event, results, run, actor = issued_shadow_run
    results[0].status = "completed"
    results[0].result_value = 39.2
    results[1].status = "dnf"
    capture_shadow_outcome_revisions(event, actor_id=actor.id)
    expected_token = outcome_state_token(run)
    results[0].status = "dq"
    results[0].result_value = None
    preview = build_shadow_standings(run)
    assert preview[0]["settlement_effect"] == "void"
    assert preview[0]["history"][0]["classification"] == "valid_finish"

    with pytest.raises(ValueError, match="reason"):
        reconcile_shadow_outcomes(
            run,
            event=event,
            actor=actor,
            expected_outcome_token=expected_token,
            reason_code="",
        )

    correction = reconcile_shadow_outcomes(
        run,
        event=event,
        actor=actor,
        expected_outcome_token=expected_token,
        reason_code="official_classification_corrected",
    )
    assert correction.outcome_count == 1
    assert run.outcome_revisions[-1].reason_code == "official_classification_corrected"
    assert json.loads(run.settlement_outbox[-1].payload_json)["revisions"][0]["action"] == "void"

    with pytest.raises(ShadowOutcomeConflict, match="changed"):
        reconcile_shadow_outcomes(
            run,
            event=event,
            actor=actor,
            expected_outcome_token=expected_token,
            reason_code="official_classification_corrected",
        )


def test_operator_page_shows_prior_evidence_and_reconciles_with_confirmation(
    issued_shadow_run,
    auth_client,
):
    tournament, event, results, run, actor = issued_shadow_run
    results[0].status = "completed"
    results[0].result_value = 39.2
    results[1].status = "dnf"
    capture_shadow_outcome_revisions(event, actor_id=actor.id)
    url = f"/scheduling/{tournament.id}/events/{event.id}/shadow-marks"

    with patch(
        "routes.scheduling.shadow_marks._remote_status",
        return_value={
            "local_trust": "recorded",
            "receipt_freshness": "current",
            "receipt_readiness": "ready",
            "mirror": "not-configured",
        },
    ):
        page = auth_client.get(url)
        body = page.get_data(as_text=True)
        assert page.status_code == 200
        assert "Shadow comparison standings" in body
        assert "Read-only official context" in body
        assert "Private One" in body
        assert "39.2" in body
        assert "Reason for reconciliation" in body
        assert "This never changes official results" in body

        expected_token = outcome_state_token(run)
        results[0].status = "dq"
        results[0].result_value = None
        with patch.object(db.session, "commit", side_effect=db.session.flush):
            response = auth_client.post(
                url,
                data={
                    "action": "reconcile_outcomes",
                    "expected_outcome_token": expected_token,
                    "reason_code": "official_classification_corrected",
                    "confirm_outcome_reconciliation": "yes",
                },
                follow_redirects=False,
            )
        assert response.status_code == 302
        assert run.outcome_revisions[-1].reason_code == "official_classification_corrected"
        assert json.loads(run.settlement_outbox[-1].payload_json)["revisions"][0]["action"] == "void"
