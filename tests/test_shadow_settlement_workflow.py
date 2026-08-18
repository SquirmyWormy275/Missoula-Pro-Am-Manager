"""Transactional operational outcomes and STRATHMARK numeric settlement outbox."""

import hashlib
import json
import os
import queue
import threading
from datetime import date, timedelta
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
from services.scoring_engine import calculate_positions, validate_finalization
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
from services.strathmark_shadow import (
    ShadowRemoteUnavailable,
    _ledger_request_id,
    prepare_shadow_run,
)
from services.time_utils import utc_now_naive
from tests.conftest import (
    make_event,
    make_event_result,
    make_heat,
    make_pro_competitor,
    make_tournament,
)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _seed_issued_shadow_run(db_session, admin_user):
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
    from tests.test_strathmark_shadow_adapter import _receipt_response

    core = _receipt_response(run, external_ids)["receipt"]["core"]
    for ordinal, prediction in enumerate(core["predictions"]):
        prediction["prediction_id"] = f"strathmark:prediction:settlement-{ordinal}"
    core_json = _canonical(core)
    run.receipts.append(
        ShadowReceiptRevision(
            revision=1,
            schema_version=core["schema_version"],
            core_json=core_json,
            core_sha256=hashlib.sha256(core_json.encode()).hexdigest(),
            prediction_count=2,
            ledger_request_id=_ledger_request_id(run.consumer_id, run.request_id),
        )
    )
    run.active_input_fingerprint = core["active_input"]["fingerprint"]
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


@pytest.fixture()
def issued_shadow_run(db_session, admin_user):
    return _seed_issued_shadow_run(db_session, admin_user)


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
    outcome_context = [row for row in run.context_observations if row.factor == "penalty_nonfinish"]
    assert [json.loads(row.value_json)["classification"] for row in outcome_context] == [
        "none",
        "dnf",
    ]
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


def test_existing_issued_run_settles_even_if_legacy_setup_changed_display_authority(
    issued_shadow_run,
):
    _tournament, event, results, run, actor = issued_shadow_run
    event.handicap_authority_mode = "official"
    results[0].status = "completed"
    results[0].result_value = 42.5
    results[1].status = "dnf"

    captured = capture_shadow_outcome_revisions(event, actor_id=actor.id)

    assert captured.run_id == run.id
    assert captured.outcome_count == 2
    assert run.lifecycle == "outcomes-complete"


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


def test_scoring_finalization_commits_with_a_blocked_shadow_delivery_principal(
    issued_shadow_run,
    scorer_user,
    monkeypatch,
):
    tournament, event, results, run, issuer = issued_shadow_run
    issuer.is_active_user = False
    run.reviewed_by_id = None
    run.created_by_id = issuer.id
    for offset, result in enumerate(results):
        result.status = "completed"
        result.result_value = 42.5 + offset

    monkeypatch.setattr(db.session, "commit", db.session.flush)
    outcome = finalize_event_results(
        event=event,
        tournament_id=tournament.id,
        judge_user_id=scorer_user.id,
    )

    assert outcome["ok"] is True
    assert event.is_finalized is True
    assert run.lifecycle == "outcomes-complete"
    assert len(run.outcome_revisions) == 2
    assert len(run.settlement_outbox) == 1
    blocked = run.settlement_outbox[0]
    assert blocked.actor_id == scorer_user.id
    assert blocked.delivery_actor_id == scorer_user.id
    assert blocked.delivery_status == "pending"

    client = FakeOutcomeClient([])
    delivery = deliver_shadow_settlement_outbox(client=client, commit=False)
    assert delivery.retryable_failed == 1
    assert client.calls == []
    assert blocked.delivery_status == "retryable-failed"


def test_shadow_authority_ranks_raw_time_and_ignores_preserved_official_marks(
    issued_shadow_run,
):
    _tournament, event, results, _run, _actor = issued_shadow_run
    results[0].status = "completed"
    results[0].result_value = 50.0
    results[0].handicap_factor = 0.0
    results[1].status = "completed"
    results[1].result_value = 51.0
    results[1].handicap_factor = 10.0
    official_before = [row.handicap_factor for row in results]

    calculate_positions(event)

    assert [row.final_position for row in results] == [1, 2]
    assert [row.handicap_factor for row in results] == official_before
    assert all(
        "start mark" not in issue["message"].lower() for issue in validate_finalization(event)
    )


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


def test_late_terminal_entrant_cannot_mask_a_missing_frozen_prediction(
    issued_shadow_run,
    db_session,
):
    tournament, event, results, run, actor = issued_shadow_run
    results[0].status = "completed"
    results[0].result_value = 42.5
    results[1].status = "pending"
    late_competitor = make_pro_competitor(
        db_session,
        tournament,
        "Late Entrant",
        "M",
        events=[event.name],
    )
    late_result = make_event_result(db_session, event, late_competitor)
    late_result.status = "completed"
    late_result.result_value = 43.0
    db_session.add(
        CompetitorExternalIdentity(
            competitor_uid=late_competitor.uid,
            namespace="strathmark",
            external_id="strathmark:competitor:late-entrant",
            status="reviewed",
            reviewed_by_id=actor.id,
        )
    )
    db_session.flush()

    capture_shadow_outcome_revisions(event, actor_id=actor.id)

    assert run.lifecycle == "shadow-issued"


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


def test_worker_retries_exact_payload_after_cooldown_and_accepts_duplicate(
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
    assert row.next_attempt_at > utc_now_naive()

    from tests.test_strathmark_shadow_adapter import _numeric_outcome_response

    restarted = FakeOutcomeClient(
        [
            _numeric_outcome_response(
                run,
                actor,
                json.loads(original_payload),
                status="duplicate",
            )
        ]
    )
    immediate = deliver_shadow_settlement_outbox(client=restarted, limit=10, commit=False)
    assert immediate.attempted == 0
    assert restarted.calls == []

    row.next_attempt_at = utc_now_naive() - timedelta(seconds=1)
    after_cooldown = deliver_shadow_settlement_outbox(
        client=restarted,
        limit=10,
        commit=False,
    )
    assert after_cooldown.recorded == 1
    assert row.delivery_status == "recorded"
    assert restarted.calls[0][2] == json.loads(original_payload)


def test_bounded_delivery_reports_remaining_eligible_backlog(issued_shadow_run):
    _tournament, event, results, run, actor = issued_shadow_run
    results[0].status = "completed"
    results[0].result_value = 41.3
    results[1].status = "dnf"
    capture_shadow_outcome_revisions(event, actor_id=actor.id)
    first_payload = json.loads(run.settlement_outbox[-1].payload_json)
    results[1].status = "completed"
    results[1].result_value = 43.1
    capture_shadow_outcome_revisions(event, actor_id=actor.id)
    second_payload = json.loads(run.settlement_outbox[-1].payload_json)
    from tests.test_strathmark_shadow_adapter import _numeric_outcome_response

    client = FakeOutcomeClient(
        [
            _numeric_outcome_response(run, actor, first_payload),
            _numeric_outcome_response(run, actor, second_payload),
        ]
    )

    first = deliver_shadow_settlement_outbox(client=client, limit=1, commit=False)
    assert first.status == "incomplete"
    assert first.remaining_eligible == 1
    assert first.recorded == 1

    second = deliver_shadow_settlement_outbox(client=client, limit=1, commit=False)
    assert second.status == "complete"
    assert second.remaining_eligible == 0
    assert second.recorded == 1


def test_newer_revision_waits_behind_earlier_backoff_for_the_same_run(
    issued_shadow_run,
):
    _tournament, event, results, run, actor = issued_shadow_run
    results[0].status = "completed"
    results[0].result_value = 41.3
    results[1].status = "dnf"
    capture_shadow_outcome_revisions(event, actor_id=actor.id)
    earlier = run.settlement_outbox[-1]
    earlier.delivery_status = "retryable-failed"
    earlier.next_attempt_at = utc_now_naive() + timedelta(minutes=5)

    results[0].result_value = 40.9
    capture_shadow_outcome_revisions(event, actor_id=actor.id)
    newer = run.settlement_outbox[-1]
    client = FakeOutcomeClient([])

    result = deliver_shadow_settlement_outbox(client=client, limit=10, commit=False)

    assert result.attempted == 0
    assert result.remaining_eligible == 0
    assert client.calls == []
    assert earlier.delivery_status == "retryable-failed"
    assert newer.delivery_status == "pending"


def test_scorer_authorship_uses_issued_principal_for_remote_delivery(
    issued_shadow_run,
    scorer_user,
):
    _tournament, event, results, run, issuer = issued_shadow_run
    results[0].status = "completed"
    results[0].result_value = 41.3
    results[1].status = "dnf"
    capture_shadow_outcome_revisions(event, actor_id=scorer_user.id)
    from tests.test_strathmark_shadow_adapter import _numeric_outcome_response

    client = FakeOutcomeClient(
        [
            _numeric_outcome_response(
                run,
                issuer,
                json.loads(run.settlement_outbox[-1].payload_json),
            )
        ]
    )

    result = deliver_shadow_settlement_outbox(client=client, limit=10, commit=False)

    assert result.recorded == 1
    assert run.settlement_outbox[-1].actor_id == scorer_user.id
    assert run.settlement_outbox[-1].delivery_actor_id == issuer.id
    assert client.calls[0][1] == issuer.shadow_actor_id


def test_authorized_local_actor_is_frozen_as_delivery_principal(
    issued_shadow_run,
    judge_user,
):
    _tournament, event, results, run, _issuer = issued_shadow_run
    results[0].status = "completed"
    results[0].result_value = 41.3
    results[1].status = "dnf"

    capture_shadow_outcome_revisions(event, actor_id=judge_user.id)

    row = run.settlement_outbox[-1]
    assert row.actor_id == judge_user.id
    assert row.delivery_actor_id == judge_user.id


def test_inactive_local_actor_falls_back_to_active_issuer(
    issued_shadow_run,
    judge_user,
):
    _tournament, event, results, run, issuer = issued_shadow_run
    judge_user.is_active_user = False
    results[0].status = "completed"
    results[0].result_value = 41.3
    results[1].status = "dnf"

    capture_shadow_outcome_revisions(event, actor_id=judge_user.id)

    row = run.settlement_outbox[-1]
    assert row.actor_id == judge_user.id
    assert row.delivery_actor_id == issuer.id


def test_disabled_delivery_principal_fails_closed_without_remote_attestation(
    issued_shadow_run,
):
    _tournament, event, results, run, issuer = issued_shadow_run
    results[0].status = "completed"
    results[0].result_value = 41.3
    results[1].status = "dnf"
    capture_shadow_outcome_revisions(event, actor_id=issuer.id)
    assert run.settlement_outbox[-1].delivery_actor_id == issuer.id
    issuer.is_active_user = False
    client = FakeOutcomeClient([])

    result = deliver_shadow_settlement_outbox(client=client, limit=10, commit=False)

    assert result.retryable_failed == 1
    assert result.recorded == 0
    assert client.calls == []
    assert run.settlement_outbox[-1].delivery_status == "retryable-failed"


def test_revoked_frozen_delivery_principal_never_switches_to_another_operator(
    issued_shadow_run,
    judge_user,
):
    _tournament, event, results, run, issuer = issued_shadow_run
    results[0].status = "completed"
    results[0].result_value = 41.3
    results[1].status = "dnf"
    capture_shadow_outcome_revisions(event, actor_id=issuer.id)
    issuer.role = "scorer"
    run.reviewed_by_id = judge_user.id
    client = FakeOutcomeClient([])

    result = deliver_shadow_settlement_outbox(client=client, limit=10, commit=False)

    assert result.retryable_failed == 1
    assert client.calls == []
    assert run.settlement_outbox[-1].delivery_actor_id == issuer.id


@pytest.mark.skipif(
    os.environ.get("PROAM_UNIT_PG") != "1",
    reason="requires the isolated PROAM_UNIT_PG PostgreSQL clone",
)
def test_postgres_workers_skip_a_locked_delivery_instead_of_double_sending():
    from models import User
    from tests.db_test_utils import create_test_app, drop_test_db

    app, db_handle = create_test_app()
    entered_remote_call = threading.Event()
    release_remote_call = threading.Event()
    calls = []
    outcomes = queue.Queue()

    class BlockingOutcomeClient:
        def apply_outcome(self, run, actor, payload):
            calls.append((run.id, actor.id, payload["outcome_revision_id"]))
            entered_remote_call.set()
            assert release_remote_call.wait(timeout=10)
            return {"outcome": {"status": "recorded"}}

    def worker():
        try:
            with app.app_context():
                outcomes.put(
                    deliver_shadow_settlement_outbox(
                        client=BlockingOutcomeClient(),
                        limit=1,
                        commit=True,
                    )
                )
                db.session.remove()
        except BaseException as exc:  # surfaced in the parent test thread
            outcomes.put(exc)

    try:
        with app.app_context():
            issuer = User(username="pg_shadow_issuer", role="admin")
            issuer.set_password("isolated-postgres-proof")
            db.session.add(issuer)
            db.session.flush()
            _tournament, event, results, run, _actor = _seed_issued_shadow_run(
                db.session,
                issuer,
            )
            results[0].status = "completed"
            results[0].result_value = 41.3
            results[1].status = "dnf"
            capture_shadow_outcome_revisions(event, actor_id=issuer.id)
            outbox_id = run.settlement_outbox[-1].id
            db.session.commit()
            db.session.remove()

        first = threading.Thread(target=worker, daemon=True)
        second = threading.Thread(target=worker, daemon=True)
        first.start()
        assert entered_remote_call.wait(timeout=10)
        second.start()
        second.join(timeout=10)
        assert not second.is_alive(), "SKIP LOCKED worker waited on the claimed row"

        release_remote_call.set()
        first.join(timeout=10)
        assert not first.is_alive(), "delivery worker did not finish after remote release"

        worker_results = [outcomes.get_nowait(), outcomes.get_nowait()]
        errors = [value for value in worker_results if isinstance(value, BaseException)]
        assert errors == []
        assert sorted(value.attempted for value in worker_results) == [0, 1]
        assert sum(value.recorded for value in worker_results) == 1
        assert len(calls) == 1
        with app.app_context():
            row = db.session.get(ShadowSettlementOutbox, outbox_id)
            assert row.delivery_status == "recorded"
            assert row.attempt_count == 1
            db.session.remove()
            db.engine.dispose()
    finally:
        release_remote_call.set()
        drop_test_db(db_handle)


def test_scoring_routes_never_drain_remote_outbox_inline(
    issued_shadow_run,
    auth_client,
    db_session,
):
    from routes import scoring

    tournament, event, results, _run, _actor = issued_shadow_run
    for offset, result in enumerate(results):
        result.status = "completed"
        result.result_value = 42.5 + offset
    heat = make_heat(
        db_session,
        event,
        competitors=[row.competitor_id for row in results],
    )

    with (
        patch(
            "services.shadow_settlement.deliver_shadow_settlement_outbox",
            side_effect=AssertionError("remote delivery must not run in a scoring request"),
        ) as delivery,
        patch.object(
            scoring,
            "_save_heat_results_submission",
            return_value={
                "ok": True,
                "message": "saved",
                "redirect_url": "/",
                "category": "success",
                "status_code": 200,
            },
        ),
        patch.object(db.session, "commit", side_effect=db.session.flush),
    ):
        finalized = auth_client.post(
            f"/scoring/{tournament.id}/event/{event.id}/finalize",
            follow_redirects=False,
        )
        entered = auth_client.post(
            f"/scoring/{tournament.id}/heat/{heat.id}/enter",
            follow_redirects=False,
        )

    assert finalized.status_code == 302
    assert entered.status_code == 302
    delivery.assert_not_called()


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
    assert [row["shadow_elapsed_seconds"] for row in standings] == [39.5, 39.0]
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
        with (
            patch.object(db.session, "commit", side_effect=db.session.flush),
            patch(
                "routes.scheduling.shadow_marks._deliver_pending_outcomes",
                create=True,
            ) as request_path_drain,
        ):
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
        request_path_drain.assert_not_called()
        assert run.outcome_revisions[-1].reason_code == "official_classification_corrected"
        assert (
            json.loads(run.settlement_outbox[-1].payload_json)["revisions"][0]["action"] == "void"
        )
