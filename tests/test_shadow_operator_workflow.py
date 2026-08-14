"""Operator-facing shadow review, issue, and export workflow."""

import hashlib
import json
from datetime import date
from unittest.mock import patch

import pytest

from database import db
from models import (
    CompetitorExternalIdentity,
    EventResult,
    ShadowFieldReview,
    ShadowHandicapRun,
    ShadowIssueArtifact,
    ShadowReceiptRevision,
    WoodConfig,
)
from services.shadow_handicap_state import transition_shadow_run
from services.shadow_operator import (
    ShadowIssueBlocked,
    ShadowReviewError,
    build_shadow_operator_view,
    build_shadow_schedule_fingerprint,
    issue_shadow_sheet,
    review_shadow_sheet,
    verify_shadow_export,
)
from services.strathmark_shadow import prepare_shadow_run
from tests.conftest import (
    make_event,
    make_event_result,
    make_pro_competitor,
    make_tournament,
)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _receipt_core(run, competitor_ids, marks=None):
    marks = marks or [0, 7]
    return {
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
                "prediction_id": f"strathmark:prediction:{ordinal:04d}",
                "assigned_mark": marks[ordinal],
                "median_seconds": 40.0 + ordinal,
                "interval": {"lower": 30.0, "upper": 55.0, "nominal": 0.9},
                "warnings": [],
            }
            for ordinal, competitor_id in enumerate(competitor_ids)
        ],
    }


@pytest.fixture()
def calculated_run(db_session, admin_user):
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
        make_pro_competitor(db_session, tournament, "Local Display One", "M", events=[event.name]),
        make_pro_competitor(db_session, tournament, "Local Display Two", "M", events=[event.name]),
    ]
    results = [make_event_result(db_session, event, row) for row in competitors]
    external_ids = []
    for ordinal, competitor in enumerate(competitors, start=1):
        external_id = f"strathmark:competitor:operator-{ordinal}"
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
    schedule_fingerprint = build_shadow_schedule_fingerprint(event)
    run = prepare_shadow_run(
        event,
        actor=admin_user,
        prediction_as_of=date(2027, 5, 8),
        schedule_fingerprint=schedule_fingerprint,
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
    core = _receipt_core(run, external_ids)
    core_json = _canonical(core)
    run.receipts.append(
        ShadowReceiptRevision(
            revision=1,
            schema_version=core["schema_version"],
            core_json=core_json,
            core_sha256=hashlib.sha256(core_json.encode()).hexdigest(),
            prediction_count=len(external_ids),
            ledger_request_id=f"strathmark:ledger-request:operator-{run.id}",
        )
    )
    transition_shadow_run(
        run,
        expected_version=2,
        lifecycle="calculated",
        actor_id=admin_user.id,
        reason_code="trusted_receipt_recorded",
    )
    db_session.flush()
    return tournament, event, results, run


def _ready_status(*, mirror="not-configured"):
    return {
        "local_trust": "recorded",
        "receipt_freshness": "current",
        "receipt_readiness": "ready",
        "mirror": mirror,
        "mirror_pending_count": 0 if mirror == "recorded" else 1,
    }


def test_review_requires_every_prediction_and_explicitly_accepts_zero(
    calculated_run,
    admin_user,
):
    _tournament, _event, results, run = calculated_run

    with pytest.raises(ShadowReviewError, match="every recommendation"):
        review_shadow_sheet(
            run,
            actor=admin_user,
            expected_version=run.lifecycle_version,
            reviewed_prediction_ids={"strathmark:prediction:0001"},
            remote_status=_ready_status(),
        )

    review = review_shadow_sheet(
        run,
        actor=admin_user,
        expected_version=run.lifecycle_version,
        reviewed_prediction_ids={
            "strathmark:prediction:0000",
            "strathmark:prediction:0001",
        },
        remote_status=_ready_status(mirror="retryable-failed"),
    )
    db.session.flush()

    decision = json.loads(review.decision_json)
    assert decision["recommendations"][0]["assigned_mark"] == 0
    assert decision["recommendations"][0]["reviewed"] is True
    assert run.lifecycle == "reviewed"
    assert ShadowFieldReview.query.filter_by(run_id=run.id).count() == 1
    assert [(row.handicap_factor, row.predicted_time, row.mark_assigned_at) for row in results] == [
        (0.0, None, None),
        (0.0, None, None),
    ]


@pytest.mark.parametrize("role", ["scorer", "registrar", "spectator"])
def test_only_judge_or_admin_may_review(calculated_run, db_session, role):
    from models import User

    _tournament, _event, _results, run = calculated_run
    actor = User(username=f"blocked-{role}", role=role)
    actor.set_password("test-pass")
    db_session.add(actor)
    db_session.flush()

    with pytest.raises(ShadowReviewError, match="judge or admin"):
        review_shadow_sheet(
            run,
            actor=actor,
            expected_version=run.lifecycle_version,
            reviewed_prediction_ids={
                "strathmark:prediction:0000",
                "strathmark:prediction:0001",
            },
            remote_status=_ready_status(),
        )


def test_issue_is_whole_field_checksummed_nonimportable_and_scoring_inert(
    calculated_run,
    admin_user,
):
    tournament, _event, results, run = calculated_run
    review_shadow_sheet(
        run,
        actor=admin_user,
        expected_version=run.lifecycle_version,
        reviewed_prediction_ids={
            "strathmark:prediction:0000",
            "strathmark:prediction:0001",
        },
        remote_status=_ready_status(),
    )

    artifact = issue_shadow_sheet(
        run,
        actor=admin_user,
        expected_version=run.lifecycle_version,
        remote_status=_ready_status(mirror="retryable-failed"),
    )
    db.session.flush()

    exported = verify_shadow_export(artifact)
    assert exported["schema_version"] == "missoula.shadow-sheet-export.v1"
    assert exported["authority"] == "shadow-recommendation-only"
    assert exported["importable"] is False
    assert len(exported["recommendations"]) == 2
    assert exported["receipt_core_sha256"] == run.receipts[-1].core_sha256
    assert artifact.export_sha256 == hashlib.sha256(artifact.export_json.encode()).hexdigest()
    assert run.lifecycle == "shadow-issued"
    assert ShadowIssueArtifact.query.filter_by(run_id=run.id).count() == 1
    serialized = artifact.export_json.lower()
    assert "local display" not in serialized
    assert "handicap_factor" not in serialized
    assert "predicted_time" not in serialized
    assert "mark_assigned_at" not in serialized
    assert tournament.shadow_tournament_id in serialized
    assert [(row.handicap_factor, row.predicted_time, row.mark_assigned_at) for row in results] == [
        (0.0, None, None),
        (0.0, None, None),
    ]


@pytest.mark.parametrize(
    "status, message",
    [
        ({**_ready_status(), "local_trust": "invalid"}, "trusted"),
        ({**_ready_status(), "receipt_freshness": "stale"}, "current"),
        ({**_ready_status(), "receipt_readiness": "not-ready"}, "ready"),
    ],
)
def test_untrusted_stale_or_not_ready_blocks_issue(
    calculated_run,
    admin_user,
    status,
    message,
):
    _tournament, _event, _results, run = calculated_run
    review_shadow_sheet(
        run,
        actor=admin_user,
        expected_version=run.lifecycle_version,
        reviewed_prediction_ids={
            "strathmark:prediction:0000",
            "strathmark:prediction:0001",
        },
        remote_status=_ready_status(),
    )

    with pytest.raises(ShadowIssueBlocked, match=message):
        issue_shadow_sheet(
            run,
            actor=admin_user,
            expected_version=run.lifecycle_version,
            remote_status=status,
        )


def test_local_roster_or_wood_change_becomes_action_required(calculated_run):
    _tournament, event, _results, run = calculated_run
    ready = build_shadow_operator_view(run, remote_status=_ready_status())
    assert ready.primary_summary == "Ready to review"
    assert ready.blockers == ()

    late = EventResult(
        event_id=event.id,
        competitor_id=999999,
        competitor_type="pro",
        competitor_name="Late entrant",
        status="pending",
    )
    db.session.add(late)
    db.session.flush()
    stale = build_shadow_operator_view(run, remote_status=_ready_status())
    assert stale.primary_summary == "Action required"
    assert any("roster" in item.lower() for item in stale.blockers)


def test_mirror_failure_is_advisory_not_issue_blocker(calculated_run, admin_user):
    _tournament, _event, _results, run = calculated_run
    view = build_shadow_operator_view(
        run,
        remote_status=_ready_status(mirror="retryable-failed"),
    )
    assert view.blockers == ()
    assert any("mirror" in item.lower() for item in view.advisories)

    review_shadow_sheet(
        run,
        actor=admin_user,
        expected_version=run.lifecycle_version,
        reviewed_prediction_ids={
            "strathmark:prediction:0000",
            "strathmark:prediction:0001",
        },
        remote_status=_ready_status(mirror="retryable-failed"),
    )
    issue_shadow_sheet(
        run,
        actor=admin_user,
        expected_version=run.lifecycle_version,
        remote_status=_ready_status(mirror="retryable-failed"),
    )
    assert run.lifecycle == "shadow-issued"


def test_concurrent_issue_uses_compare_and_swap(calculated_run, admin_user):
    from services.shadow_handicap_state import ShadowConcurrencyError

    _tournament, _event, _results, run = calculated_run
    review_shadow_sheet(
        run,
        actor=admin_user,
        expected_version=run.lifecycle_version,
        reviewed_prediction_ids={
            "strathmark:prediction:0000",
            "strathmark:prediction:0001",
        },
        remote_status=_ready_status(),
    )
    stale_version = run.lifecycle_version
    issue_shadow_sheet(
        run,
        actor=admin_user,
        expected_version=stale_version,
        remote_status=_ready_status(),
    )

    with pytest.raises(ShadowConcurrencyError):
        issue_shadow_sheet(
            run,
            actor=admin_user,
            expected_version=stale_version,
            remote_status=_ready_status(),
        )


def test_view_copy_separates_blockers_advisories_and_lifecycle(calculated_run):
    _tournament, _event, _results, run = calculated_run
    view = build_shadow_operator_view(
        run,
        remote_status=_ready_status(mirror="pending"),
    )

    assert view.primary_summary == "Ready to review"
    assert view.primary_action == "Review entire sheet"
    assert view.blocker_heading == "Action required before issue"
    assert view.advisory_heading == "Advisories (do not block issue)"
    assert view.lifecycle_heading == "Workflow and audit detail"
    assert view.status_announcement.startswith("Shadow sheet")


def test_authenticated_operator_route_reviews_issues_and_downloads_whole_sheet(
    calculated_run,
    auth_client,
):
    tournament, event, results, run = calculated_run
    url = f"/scheduling/{tournament.id}/events/{event.id}/shadow-marks"
    prediction_ids = ["strathmark:prediction:0000", "strathmark:prediction:0001"]

    with (
        patch(
            "routes.scheduling.shadow_marks._remote_status",
            return_value=_ready_status(mirror="retryable-failed"),
        ),
        patch.object(db.session, "commit", side_effect=db.session.flush),
    ):
        page = auth_client.get(url)
        assert page.status_code == 200
        body = page.get_data(as_text=True)
        assert "Shadow handicap sheet" in body
        assert "Ready to review" in body
        assert "Action required before issue" in body
        assert "Advisories (do not block issue)" in body
        assert "Every row must be reviewed together" in body
        assert 'aria-live="polite"' in body

        reviewed = auth_client.post(
            url,
            data={
                "action": "review",
                "expected_version": str(run.lifecycle_version),
                "review_prediction": prediction_ids,
            },
            follow_redirects=False,
        )
        assert reviewed.status_code == 302
        run = db.session.get(ShadowHandicapRun, run.id)
        assert run.lifecycle == "reviewed"

        issued = auth_client.post(
            url,
            data={"action": "issue", "expected_version": str(run.lifecycle_version)},
            follow_redirects=False,
        )
        assert issued.status_code == 302
        run = db.session.get(ShadowHandicapRun, run.id)
        assert run.lifecycle == "shadow-issued"

    export = auth_client.get(f"{url}/{run.id}/export")
    assert export.status_code == 200
    assert export.headers["X-Shadow-Importable"] == "false"
    assert (
        export.headers["X-Content-SHA256"]
        == hashlib.sha256(export.get_data(as_text=True).strip().encode()).hexdigest()
    )
    payload = export.get_json()
    assert payload["importable"] is False
    assert len(payload["recommendations"]) == 2
    assert [(row.handicap_factor, row.predicted_time, row.mark_assigned_at) for row in results] == [
        (0.0, None, None),
        (0.0, None, None),
    ]


def test_scorer_cannot_open_or_change_shadow_sheet(calculated_run, app, scorer_user):
    tournament, event, _results, run = calculated_run
    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = str(scorer_user.id)
    url = f"/scheduling/{tournament.id}/events/{event.id}/shadow-marks"

    assert client.get(url).status_code == 403
    assert (
        client.post(
            url,
            data={"action": "issue", "expected_version": run.lifecycle_version},
        ).status_code
        == 403
    )


def test_event_setup_persists_explicit_shadow_authority(db_session):
    from routes.scheduling.events import _upsert_event

    tournament = make_tournament(db_session, year=2028)
    event = _upsert_event(
        tournament,
        {
            "name": "Standing Block",
            "scoring_type": "time",
            "stand_type": "standing_block",
        },
        "pro",
        "M",
        False,
        is_handicap=True,
        handicap_authority_mode="shadow",
    )
    db_session.flush()
    assert event.is_handicap is True
    assert event.handicap_authority_mode == "shadow"

    event = _upsert_event(
        tournament,
        {
            "name": "Standing Block",
            "scoring_type": "time",
            "stand_type": "standing_block",
        },
        "pro",
        "M",
        False,
        is_handicap=False,
        handicap_authority_mode="shadow",
    )
    assert event.handicap_authority_mode == "official"


def test_legacy_mark_route_cannot_write_shadow_event(calculated_run, auth_client):
    tournament, event, results, _run = calculated_run
    response = auth_client.post(
        f"/scheduling/{tournament.id}/events/{event.id}/assign-marks",
        data={
            "action": "manual_save",
            f"mark_{results[0].id}": "15",
            f"mark_{results[1].id}": "20",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith(
        f"/scheduling/{tournament.id}/events/{event.id}/shadow-marks"
    )
    assert [(row.handicap_factor, row.predicted_time, row.mark_assigned_at) for row in results] == [
        (0.0, None, None),
        (0.0, None, None),
    ]


def test_changed_cutoff_creates_linked_superseding_request(calculated_run, admin_user):
    _tournament, event, _results, run = calculated_run
    replacement = prepare_shadow_run(
        event,
        actor=admin_user,
        prediction_as_of=date(2027, 5, 9),
        schedule_fingerprint=build_shadow_schedule_fingerprint(event),
        observation_schema_version="strathmark.shadow-observation-fingerprint.v1",
        observation_fingerprint="2" * 64,
        supersedes_run=run,
    )

    assert replacement.supersedes_run_id == run.id
    assert replacement.request_id != run.request_id
    assert replacement.run_revision != run.run_revision
    assert replacement.prediction_as_of != run.prediction_as_of
