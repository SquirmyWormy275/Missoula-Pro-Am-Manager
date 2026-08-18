"""Operator-facing shadow review, issue, and export workflow."""

import hashlib
import json
import re
import threading
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from database import db
from models import (
    CompetitorExternalIdentity,
    Event,
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
from services.strathmark_shadow import (
    ShadowUnsupportedEventError,
    _ledger_request_id,
    calculate_or_recover_shadow_run,
    prepare_shadow_run,
)
from tests.conftest import (
    make_event,
    make_event_result,
    make_pro_competitor,
    make_tournament,
)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _receipt_core(run, competitor_ids, marks=None):
    from tests.test_strathmark_shadow_adapter import _receipt_response

    marks = marks or [3, 7]
    core = _receipt_response(run, competitor_ids)["receipt"]["core"]
    for prediction, mark in zip(core["predictions"], marks):
        prediction["assigned_mark"] = mark
    return core


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
    assert decision["recommendations"][0]["assigned_mark"] == 3
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


def test_every_shadow_post_form_declares_a_csrf_token():
    template_path = (
        Path(__file__).resolve().parents[1] / "templates" / "scheduling" / "shadow_marks.html"
    )
    template = template_path.read_text(encoding="utf-8")
    post_forms = re.findall(
        r'<form\b[^>]*method="post"[^>]*>.*?</form>',
        template,
        flags=re.DOTALL | re.IGNORECASE,
    )

    assert len(post_forms) == 9
    assert all('name="csrf_token"' in form for form in post_forms)


def test_shadow_route_accepts_real_csrf_token_and_rejects_missing_token(
    calculated_run,
    auth_client,
    app,
):
    tournament, event, _results, run = calculated_run
    url = f"/scheduling/{tournament.id}/events/{event.id}/shadow-marks"
    old_enabled = app.config.get("WTF_CSRF_ENABLED")
    old_check_default = app.config.get("WTF_CSRF_CHECK_DEFAULT")
    app.config.update(WTF_CSRF_ENABLED=True, WTF_CSRF_CHECK_DEFAULT=True)

    try:
        with (
            patch(
                "routes.scheduling.shadow_marks._remote_status",
                return_value=_ready_status(),
            ),
            patch("routes.scheduling.shadow_marks.build_shadow_standings", return_value=()),
        ):
            page = auth_client.get(url)
            assert page.status_code == 200
            match = re.search(
                r'name="csrf_token"\s+value="([^"]+)"',
                page.get_data(as_text=True),
            )
            assert match is not None

            missing = auth_client.post(
                url,
                data={"action": "not-a-shadow-action"},
                follow_redirects=False,
            )
            assert missing.status_code == 302
            with auth_client.session_transaction() as session:
                assert (
                    "error",
                    "Unknown shadow workflow action.",
                ) not in session.get("_flashes", [])
                session["_flashes"] = []

            accepted = auth_client.post(
                url,
                data={
                    "csrf_token": match.group(1),
                    "action": "not-a-shadow-action",
                },
                follow_redirects=False,
            )
            assert accepted.status_code == 302
            with auth_client.session_transaction() as session:
                assert (
                    "error",
                    "Unknown shadow workflow action.",
                ) in session.get("_flashes", [])
    finally:
        app.config.update(
            WTF_CSRF_ENABLED=old_enabled,
            WTF_CSRF_CHECK_DEFAULT=old_check_default,
        )


def test_shadow_route_handles_type_error_from_numeric_form_conversion(
    calculated_run,
    auth_client,
):
    tournament, event, _results, _run = calculated_run
    url = f"/scheduling/{tournament.id}/events/{event.id}/shadow-marks"

    with patch(
        "routes.scheduling.shadow_marks._record_known_context_task",
        side_effect=TypeError("Malformed numeric context input."),
    ):
        response = auth_client.post(
            url,
            data={"action": "record_context_weather"},
            follow_redirects=False,
        )

    assert response.status_code == 302
    with auth_client.session_transaction() as session:
        assert ("error", "Malformed numeric context input.") in session.get("_flashes", [])


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


def _scorer_client(app, scorer_user):
    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = str(scorer_user.id)
        session["_fresh"] = True
    return client


def _underhand_setup_form(*, authority="shadow", include_authority=True, max_stands=None):
    form = {
        "action_scope": "pro",
        "enable_underhand": "on",
        "enable_underhand_M": "on",
        "handicap_format_underhand": "handicap",
    }
    if include_authority:
        form["handicap_authority_underhand"] = authority
    if max_stands is not None:
        form["stands_underhand"] = str(max_stands)
    return form


def test_scorer_setup_post_cannot_unset_existing_shadow_authority(
    calculated_run,
    app,
    scorer_user,
):
    tournament, event, _results, _run = calculated_run
    client = _scorer_client(app, scorer_user)

    response = client.post(
        f"/scheduling/{tournament.id}/events/setup",
        data=_underhand_setup_form(authority="official"),
        follow_redirects=False,
    )

    assert response.status_code == 403
    db.session.refresh(event)
    assert event.is_handicap is True
    assert event.handicap_authority_mode == "shadow"


def test_scorer_setup_post_preserves_omitted_shadow_authority_on_ordinary_save(
    calculated_run,
    app,
    scorer_user,
    monkeypatch,
):
    tournament, event, _results, _run = calculated_run
    client = _scorer_client(app, scorer_user)
    monkeypatch.setattr(db.session, "commit", db.session.flush)

    response = client.post(
        f"/scheduling/{tournament.id}/events/setup",
        data=_underhand_setup_form(include_authority=False, max_stands=5),
        follow_redirects=False,
    )

    assert response.status_code == 302
    db.session.refresh(event)
    assert event.handicap_authority_mode == "shadow"
    assert event.max_stands == 5


def test_scorer_setup_post_cannot_set_shadow_on_official_event(
    db_session,
    app,
    scorer_user,
):
    tournament = make_tournament(db_session, year=2029)
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
    event.handicap_authority_mode = "official"
    db_session.flush()
    client = _scorer_client(app, scorer_user)

    response = client.post(
        f"/scheduling/{tournament.id}/events/setup",
        data=_underhand_setup_form(authority="shadow"),
        follow_redirects=False,
    )

    assert response.status_code == 403
    db.session.refresh(event)
    assert event.handicap_authority_mode == "official"


def test_scorer_setup_post_cannot_create_shadow_authority(
    db_session,
    app,
    scorer_user,
):
    tournament = make_tournament(db_session, year=2030)
    client = _scorer_client(app, scorer_user)

    response = client.post(
        f"/scheduling/{tournament.id}/events/setup",
        data=_underhand_setup_form(authority="shadow"),
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert Event.query.filter_by(tournament_id=tournament.id, name="Underhand").count() == 0


def test_admin_setup_post_can_create_shadow_authority(
    db_session,
    app,
    admin_user,
    monkeypatch,
):
    tournament = make_tournament(db_session, year=2031)
    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = str(admin_user.id)
        session["_fresh"] = True
    monkeypatch.setattr(db.session, "commit", db.session.flush)

    response = client.post(
        f"/scheduling/{tournament.id}/events/setup",
        data=_underhand_setup_form(authority="shadow"),
        follow_redirects=False,
    )

    assert response.status_code == 302
    event = Event.query.filter_by(
        tournament_id=tournament.id,
        name="Underhand",
        gender="M",
    ).one()
    assert event.is_handicap is True
    assert event.handicap_authority_mode == "shadow"


def test_shadow_post_uses_schedule_guard_and_holds_it_through_commit(app, monkeypatch):
    import routes.scheduling.shadow_marks as shadow_routes
    from services.flight_builder import sqlite_schedule_writer_guard

    with app.app_context():
        if db.engine.dialect.name != "sqlite":
            pytest.skip("SQLite process-lock regression")

    tournament_id = 987654
    event_id = 123456
    run = SimpleNamespace(id=1, lifecycle_version=1)
    tournament = SimpleNamespace(id=tournament_id)
    event = SimpleNamespace(id=event_id)
    first_guard_held = threading.Event()
    release_first_guard = threading.Event()
    commit_entered = threading.Event()
    release_commit = threading.Event()
    contender_acquired = threading.Event()
    request_finished = threading.Event()
    lock_calls = []
    errors = []

    monkeypatch.setattr(shadow_routes, "current_user", SimpleNamespace(id=7))
    monkeypatch.setattr(shadow_routes, "_require_shadow_operator", lambda: None)
    monkeypatch.setattr(shadow_routes, "_load_event", lambda *_args: (tournament, event))
    monkeypatch.setattr(shadow_routes, "_latest_run", lambda *_args: run)
    monkeypatch.setattr(shadow_routes, "transition_shadow_run", lambda *_args, **_kwargs: run)
    monkeypatch.setattr(shadow_routes, "capture_preflight_context", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(shadow_routes, "log_action", lambda *_args, **_kwargs: None)

    def record_parent_lock(tournament_or_id):
        lock_calls.append(int(getattr(tournament_or_id, "id", tournament_or_id)))
        return tournament

    def blocking_commit():
        commit_entered.set()
        if not release_commit.wait(10):
            raise AssertionError("commit release timed out")

    monkeypatch.setattr(shadow_routes, "lock_tournament_schedule", record_parent_lock)
    monkeypatch.setattr(shadow_routes.db.session, "commit", blocking_commit)

    def first_guard_holder():
        try:
            with app.app_context():
                with sqlite_schedule_writer_guard(tournament_id):
                    first_guard_held.set()
                    if not release_first_guard.wait(10):
                        raise AssertionError("first guard release timed out")
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def post_shadow_action():
        try:
            with app.test_request_context(
                f"/scheduling/{tournament_id}/events/{event_id}/shadow-marks",
                method="POST",
                data={"action": "approve_preflight", "expected_version": "1"},
            ):
                shadow_routes.shadow_marks(tournament_id, event_id)
            request_finished.set()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def contender():
        try:
            with app.app_context():
                with sqlite_schedule_writer_guard(tournament_id):
                    contender_acquired.set()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    holder_thread = threading.Thread(target=first_guard_holder)
    request_thread = threading.Thread(target=post_shadow_action)
    contender_thread = threading.Thread(target=contender)
    holder_thread.start()
    assert first_guard_held.wait(10)
    request_thread.start()
    try:
        assert not commit_entered.wait(0.5)
        assert lock_calls == []
    finally:
        release_first_guard.set()

    assert commit_entered.wait(10)
    assert lock_calls == [tournament_id]
    contender_thread.start()
    try:
        assert not contender_acquired.wait(0.5)
    finally:
        release_commit.set()

    holder_thread.join(10)
    request_thread.join(10)
    contender_thread.join(10)

    assert not holder_thread.is_alive()
    assert not request_thread.is_alive()
    assert not contender_thread.is_alive()
    assert request_finished.is_set()
    assert contender_acquired.is_set()
    assert errors == []


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


def test_event_setup_keeps_springboard_official_when_shadow_is_submitted(db_session):
    from routes.scheduling.events import _upsert_event

    tournament = make_tournament(db_session, year=2028)
    event = _upsert_event(
        tournament,
        {
            "name": "Springboard",
            "scoring_type": "time",
            "stand_type": "springboard",
        },
        "pro",
        None,
        False,
        is_handicap=True,
        handicap_authority_mode="shadow",
    )

    assert event.is_handicap is True
    assert event.handicap_authority_mode == "official"


@pytest.mark.parametrize(
    "lifecycle",
    [
        "prepared",
        "preflight-approved",
        "calculated",
        "reviewed",
        "shadow-issued",
        "outcomes-complete",
    ],
)
def test_event_setup_cannot_change_authority_for_any_active_shadow_run(
    calculated_run,
    lifecycle,
):
    from routes.scheduling.events import _upsert_event

    tournament, event, _results, run = calculated_run
    run.lifecycle = lifecycle
    db.session.flush()

    with pytest.raises(ValueError, match="cannot be changed"):
        _upsert_event(
            tournament,
            {
                "name": event.name,
                "scoring_type": "time",
                "stand_type": "underhand",
            },
            event.event_type,
            event.gender,
            False,
            is_handicap=False,
            handicap_authority_mode="official",
        )

    assert event.is_handicap is True
    assert event.handicap_authority_mode == "shadow"


def test_calculation_revalidates_current_shadow_authority(calculated_run):
    _tournament, event, _results, run = calculated_run
    event.handicap_authority_mode = "official"

    with pytest.raises(ShadowUnsupportedEventError, match="not configured"):
        calculate_or_recover_shadow_run(run, client=object())


def test_review_revalidates_current_shadow_authority(calculated_run, admin_user):
    _tournament, event, _results, run = calculated_run
    event.handicap_authority_mode = "official"

    with pytest.raises(ShadowReviewError, match="shadow handicap mode"):
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


def test_issue_revalidates_current_shadow_authority(calculated_run, admin_user):
    _tournament, event, _results, run = calculated_run
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
    event.handicap_authority_mode = "official"

    with pytest.raises(ShadowIssueBlocked, match="shadow handicap mode"):
        issue_shadow_sheet(
            run,
            actor=admin_user,
            expected_version=run.lifecycle_version,
            remote_status=_ready_status(),
        )


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


@pytest.mark.parametrize("lifecycle", ["shadow-issued", "outcomes-complete"])
def test_issued_or_completed_run_cannot_be_superseded(
    calculated_run,
    admin_user,
    lifecycle,
):
    _tournament, event, _results, run = calculated_run
    run.lifecycle = lifecycle
    db.session.flush()

    with pytest.raises(ValueError, match="cannot supersede"):
        prepare_shadow_run(
            event,
            actor=admin_user,
            prediction_as_of=date(2027, 5, 9),
            schedule_fingerprint=build_shadow_schedule_fingerprint(event),
            observation_schema_version="strathmark.shadow-observation-fingerprint.v1",
            observation_fingerprint="2" * 64,
            supersedes_run=run,
        )

    with pytest.raises(ValueError, match="invalid shadow lifecycle transition"):
        transition_shadow_run(
            run,
            expected_version=run.lifecycle_version,
            lifecycle="superseded",
            actor_id=admin_user.id,
            reason_code="post_issue_replacement_forbidden",
        )


def test_issued_run_with_terminal_results_retains_full_frozen_field(calculated_run):
    _tournament, _event, results, run = calculated_run
    run.lifecycle = "shadow-issued"
    results[0].status = "completed"
    results[0].result_value = 42.5
    results[1].status = "dnf"
    db.session.flush()

    view = build_shadow_operator_view(run, remote_status=_ready_status())

    assert not any("roster" in blocker.lower() for blocker in view.blockers)
    assert not any("run order or schedule" in blocker.lower() for blocker in view.blockers)


def test_correction_ui_is_hidden_after_shadow_issue(
    calculated_run,
    auth_client,
):
    tournament, event, _results, run = calculated_run
    run.lifecycle = "shadow-issued"
    db.session.flush()
    url = f"/scheduling/{tournament.id}/events/{event.id}/shadow-marks"

    with patch(
        "routes.scheduling.shadow_marks._remote_status",
        return_value={**_ready_status(), "receipt_freshness": "stale"},
    ):
        response = auth_client.get(url)

    assert response.status_code == 200
    assert "Prepare a corrected field revision" not in response.get_data(as_text=True)


def test_scoring_capture_recovers_legacy_issued_run_after_authority_display_change(
    calculated_run,
    admin_user,
):
    from services.scoring_workflow import _capture_shadow_outcomes

    _tournament, event, results, run = calculated_run
    run.lifecycle = "shadow-issued"
    run.issued_by_id = admin_user.id
    event.handicap_authority_mode = "official"
    results[0].status = "completed"
    results[0].result_value = 42.5
    results[1].status = "dnf"
    db.session.flush()

    _capture_shadow_outcomes(event, admin_user.id)

    assert len(run.outcome_revisions) == 2
    assert run.lifecycle == "outcomes-complete"
