"""Prospective context capture stays explicit, private, and numerically inactive."""

from datetime import date

import pytest

from database import db
from models import CompetitorExternalIdentity, Heat, HeatAssignment, ProCompetitor, WoodConfig
from services.shadow_context import (
    CONTEXT_SCHEMA_VERSION,
    FACTOR_MATRIX,
    ShadowContextConflict,
    build_context_audit_export,
    capture_event_context,
    capture_preflight_context,
    complete_context_stage,
    context_state_token,
    record_context_observation,
)
from services.shadow_operator import build_shadow_schedule_fingerprint
from services.strathmark_shadow import prepare_shadow_run
from tests.conftest import (
    make_event,
    make_event_result,
    make_pro_competitor,
    make_tournament,
)


@pytest.fixture()
def context_run(db_session, admin_user):
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
    competitor = make_pro_competitor(
        db_session,
        tournament,
        "Local Context Display",
        "M",
        events=[event.name],
    )
    make_event_result(db_session, event, competitor)
    db_session.add(
        CompetitorExternalIdentity(
            competitor_uid=competitor.uid,
            namespace="strathmark",
            external_id="strathmark:competitor:context-1",
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
        observation_fingerprint="8" * 64,
    )
    db_session.flush()
    return tournament, event, run, admin_user


def test_factor_matrix_covers_every_deferred_factor_and_explicit_unknown():
    assert set(FACTOR_MATRIX) == {
        "division",
        "round_heat",
        "venue",
        "lane_stand",
        "issued_run_order",
        "material_identity",
        "material_quality_moisture",
        "weather",
        "equipment_used",
        "rest_fatigue",
        "penalty_nonfinish",
    }
    for factor, spec in FACTOR_MATRIX.items():
        assert spec.factor == factor
        assert spec.stage
        assert spec.subject_types
        assert spec.allowed_sources
        assert spec.explicit_unknown_allowed is True


def test_known_unknown_and_append_only_correction_use_compare_and_swap(context_run):
    _tournament, _event, run, actor = context_run
    immutable_before = (run.active_input_fingerprint, run.observation_fingerprint)
    first = record_context_observation(
        run,
        factor="weather",
        subject_type="time_window",
        subject_id="missoula:time-window:event-start",
        value_state="known",
        value={
            "temperature_f": 71.5,
            "humidity_percent": 32.0,
            "wind_mph": 4.2,
            "precipitation_code": "none",
        },
        source="measured",
        actor=actor,
    )
    unknown = record_context_observation(
        run,
        factor="venue",
        subject_type="tournament_day",
        subject_id="missoula:tournament-day:2027-05-08",
        value_state="unknown",
        value=None,
        source="operator_entered",
        actor=actor,
    )
    assert first.schema_version == CONTEXT_SCHEMA_VERSION
    assert unknown.value_json is None

    with pytest.raises(ShadowContextConflict, match="changed"):
        record_context_observation(
            run,
            factor="weather",
            subject_type="time_window",
            subject_id="missoula:time-window:event-start",
            value_state="known",
            value={
                "temperature_f": 72.0,
                "humidity_percent": 33.0,
                "wind_mph": 4.0,
                "precipitation_code": "none",
            },
            source="measured",
            actor=actor,
        )

    corrected = record_context_observation(
        run,
        factor="weather",
        subject_type="time_window",
        subject_id="missoula:time-window:event-start",
        value_state="known",
        value={
            "temperature_f": 72.0,
            "humidity_percent": 33.0,
            "wind_mph": 4.0,
            "precipitation_code": "none",
        },
        source="measured",
        actor=actor,
        expected_latest_observation_id=first.observation_id,
    )
    db.session.flush()

    assert corrected.corrects_observation_id == first.id
    assert (run.active_input_fingerprint, run.observation_fingerprint) == immutable_before


def test_privacy_shape_and_derived_provenance_fail_closed(context_run):
    _tournament, _event, run, actor = context_run
    with pytest.raises(ValueError, match="unknown field"):
        record_context_observation(
            run,
            factor="equipment_used",
            subject_type="run",
            subject_id="missoula:run:context-1",
            value_state="known",
            value={"equipment_code": "stock-saw", "medical_notes": "private"},
            source="operator_entered",
            actor=actor,
        )

    with pytest.raises(ValueError, match="formula"):
        record_context_observation(
            run,
            factor="rest_fatigue",
            subject_type="entrant_run",
            subject_id="missoula:entrant-run:context-1",
            value_state="known",
            value={"rest_minutes": 45, "observation_code": "typical"},
            source="derived",
            actor=actor,
        )

    derived = record_context_observation(
        run,
        factor="rest_fatigue",
        subject_type="entrant_run",
        subject_id="missoula:entrant-run:context-1",
        value_state="known",
        value={"rest_minutes": 45, "observation_code": "typical"},
        source="derived",
        actor=actor,
        formula="elapsed_minutes_v1",
        source_record_ids=("missoula:result-record:context-1",),
    )
    assert derived.formula == "elapsed_minutes_v1"


def test_scorer_may_capture_result_context_but_cannot_correct_it(
    context_run,
    scorer_user,
):
    _tournament, _event, run, actor = context_run
    first = record_context_observation(
        run,
        factor="equipment_used",
        subject_type="run",
        subject_id="missoula:run:role-check",
        value_state="known",
        value={"equipment_code": "stock-saw"},
        source="operator_entered",
        actor=scorer_user,
    )
    with pytest.raises(ValueError, match="judge or admin"):
        record_context_observation(
            run,
            factor="equipment_used",
            subject_type="run",
            subject_id="missoula:run:role-check",
            value_state="known",
            value={"equipment_code": "modified-saw"},
            source="operator_entered",
            actor=scorer_user,
            expected_latest_observation_id=first.observation_id,
        )
    corrected = record_context_observation(
        run,
        factor="equipment_used",
        subject_type="run",
        subject_id="missoula:run:role-check",
        value_state="known",
        value={"equipment_code": "modified-saw"},
        source="operator_entered",
        actor=actor,
        expected_latest_observation_id=first.observation_id,
    )
    assert corrected.corrects_observation_id == first.id


def test_stage_completion_records_explicit_unknowns_and_event_context(context_run):
    tournament, event, run, actor = context_run
    division = capture_event_context(run, event=event, actor=actor)
    assert division.factor == "division"
    assert division.value_state == "known"

    completed = complete_context_stage(
        run,
        stage="material_preparation",
        actor=actor,
        subjects={
            "material_identity": ("material", "missoula:material:unknown-1"),
            "material_quality_moisture": ("material", "missoula:material:unknown-1"),
        },
    )
    assert {row.factor for row in completed} == {
        "material_identity",
        "material_quality_moisture",
    }
    assert all(row.value_state == "unknown" for row in completed)
    assert tournament.shadow_tournament_id


def test_preflight_autocaptures_order_heat_and_stand_with_stable_ids(context_run):
    _tournament, event, run, actor = context_run
    competitor = ProCompetitor.query.filter_by(tournament_id=event.tournament_id).one()
    heat = Heat(event_id=event.id, heat_number=1, run_number=1, status="pending")
    db.session.add(heat)
    db.session.flush()
    db.session.add(
        HeatAssignment(
            heat_id=heat.id,
            uid=competitor.uid,
            competitor_id=competitor.id,
            competitor_type="pro",
            stand_number=2,
        )
    )
    db.session.flush()

    rows = capture_preflight_context(run, event=event, actor=actor)

    assert {row.factor for row in rows} == {
        "issued_run_order",
        "round_heat",
        "lane_stand",
    }
    stand = next(row for row in rows if row.factor == "lane_stand")
    assert stand.subject_id.startswith("missoula:heat-run:")
    assert stand.value_json == '{"stand_number":2}'


def test_redacted_audit_export_includes_matrix_provenance_and_no_names(context_run):
    _tournament, event, run, actor = context_run
    capture_event_context(run, event=event, actor=actor)
    record_context_observation(
        run,
        factor="venue",
        subject_type="tournament_day",
        subject_id="missoula:tournament-day:2027-05-08",
        value_state="known",
        value={"venue_code": "missoula:venue:arena-1"},
        source="operator_entered",
        actor=actor,
    )

    export = build_context_audit_export(run, actor=actor)
    serialized = export.payload_json.lower()

    assert export.sha256
    assert export.payload["numeric_inactive"] is True
    assert {row["factor"] for row in export.payload["factors"]} == set(FACTOR_MATRIX)
    assert "local context display" not in serialized
    assert "username" not in serialized
    assert "password" not in serialized
    assert actor.shadow_actor_id in serialized


def test_operator_context_task_and_redacted_export_route(context_run, auth_client):
    tournament, event, run, _actor = context_run
    url = f"/scheduling/{tournament.id}/events/{event.id}/shadow-marks"
    page = auth_client.get(url)
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "Prospective context for future seasons" in body
    assert "Explicit unknown" in body
    assert "Numerically inactive" in body

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(db.session, "commit", db.session.flush)
        approved = auth_client.post(
            url,
            data={"action": "approve_preflight", "expected_version": run.lifecycle_version},
            follow_redirects=False,
        )
    assert approved.status_code == 302
    assert any(row.factor == "issued_run_order" for row in run.context_observations)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(db.session, "commit", db.session.flush)
        recorded = auth_client.post(
            url,
            data={
                "action": "record_context_unknown",
                "expected_context_token": context_state_token(run),
                "factor": "venue",
                "subject_type": "tournament_day",
                "subject_id": "missoula:tournament-day:2027-05-08",
            },
            follow_redirects=False,
        )
    assert recorded.status_code == 302
    assert run.context_observations[-1].factor == "venue"
    assert run.context_observations[-1].value_state == "unknown"

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(db.session, "commit", db.session.flush)
        weather = auth_client.post(
            url,
            data={
                "action": "record_context_weather",
                "expected_context_token": context_state_token(run),
                "subject_id": "missoula:time-window:event-start",
                "temperature_f": "71.5",
                "humidity_percent": "32.0",
                "wind_mph": "4.2",
                "precipitation_code": "none",
            },
            follow_redirects=False,
        )
    assert weather.status_code == 302
    assert run.context_observations[-1].factor == "weather"
    assert run.context_observations[-1].value_state == "known"

    exported = auth_client.get(f"{url}/{run.id}/context-export")
    assert exported.status_code == 200
    assert exported.headers["X-Context-Numeric-Active"] == "false"
    assert exported.headers["X-Content-SHA256"]
    payload = exported.get_json()
    assert payload["numeric_inactive"] is True
    assert len(payload["factors"]) == len(FACTOR_MATRIX)
    assert payload["page"]["next_cursor"] is None
    assert auth_client.get(f"{url}/{run.id}/context-export?limit=501").status_code == 400
