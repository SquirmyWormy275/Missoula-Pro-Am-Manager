"""A4 writes fail closed without replacing a valid Birling bracket."""
from __future__ import annotations

import copy

import pytest

from services import birling_rows as rows
from services.birling_bracket import BirlingBracket
from tests.conftest import (
    make_college_competitor,
    make_event,
    make_team,
    make_tournament,
)


def _world(session):
    tournament = make_tournament(session)
    team = make_team(session, tournament)
    people = [make_college_competitor(session, tournament, team, f"Person {i}")
              for i in range(4)]
    event = make_event(session, tournament, "Birling", event_type="college",
                       scoring_type="bracket")
    session.flush()
    bracket = BirlingBracket(event)
    bracket.generate_bracket([
        {"id": person.id, "name": person.display_name} for person in people
    ])
    return event, people, bracket


def test_rejected_document_leaves_last_valid_rows_intact(
        db_session, reference_gate_disarmed):
    event, people, bracket = _world(db_session)
    before = rows.load_document(event)
    broken = copy.deepcopy(bracket.bracket_data)
    broken["seeding"][0] = max(person.id for person in people) + 1000
    broken["competitors"][0]["id"] = broken["seeding"][0]

    with pytest.raises(rows.ProjectionRefused):
        rows.project_document(event, broken)

    assert rows.load_document(event) == before


def test_service_keeps_payout_configuration_while_saving_bracket(db_session):
    event, people, _bracket = _world(db_session)
    event.payouts = '{"1": 500, "2": 250}'
    db_session.commit()

    BirlingBracket(event).generate_bracket([
        {"id": person.id, "name": person.display_name} for person in reversed(people)
    ])

    assert event.payouts == '{"1": 500, "2": 250}'


def test_reset_keeps_payout_configuration(db_session, auth_client):
    event, _people, _bracket = _world(db_session)
    event.payouts = '{"1": 500}'
    db_session.commit()

    response = auth_client.post(
        f"/scheduling/{event.tournament_id}/event/{event.id}/birling/reset")

    assert response.status_code == 302
    assert event.payouts == '{"1": 500}'
    assert rows.load_document(event) == rows.empty_document()
