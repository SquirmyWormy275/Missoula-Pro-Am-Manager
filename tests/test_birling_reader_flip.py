"""A4 reader contract: Birling rows are the only bracket authority."""
from __future__ import annotations

import json

import pytest

from services import birling_rows as rows
from services.birling_bracket import BirlingBracket
from tests.conftest import (
    make_college_competitor,
    make_event,
    make_team,
    make_tournament,
)


def _world(session, name):
    tournament = make_tournament(session)
    team = make_team(session, tournament)
    people = [make_college_competitor(session, tournament, team, f"Person {i}")
              for i in range(4)]
    event = make_event(session, tournament, name, event_type="college",
                       scoring_type="bracket")
    session.flush()
    return event, people


def _generate(event, people):
    bracket = BirlingBracket(event)
    bracket.generate_bracket([
        {"id": person.id, "name": person.display_name} for person in people
    ])
    return bracket


def test_reader_uses_rows_when_legacy_json_disagrees(db_session):
    event, people = _world(db_session, "RowsWin")
    bracket = _generate(event, people)
    expected = list(bracket.bracket_data["seeding"])
    event.payouts = json.dumps({"seeding": list(reversed(expected))})
    db_session.flush()

    assert BirlingBracket(event).bracket_data["seeding"] == expected


def test_reader_does_not_resurrect_an_unprojected_legacy_document(db_session):
    event, people = _world(db_session, "NoFallback")
    event.payouts = json.dumps({
        "seeding": [person.id for person in people],
        "competitors": [{"id": person.id, "name": person.display_name}
                        for person in people],
        "bracket": {"winners": [[{"match_id": "W1_1"}]]},
    })
    db_session.flush()

    assert BirlingBracket(event).bracket_data == rows.empty_document()


def test_unloadable_rows_raise_instead_of_using_legacy_json(db_session):
    event, people = _world(db_session, "BadRows")
    _generate(event, people)
    event.payouts = json.dumps({"seeding": [person.id for person in people]})
    db_session.flush()
    event.event_type = "pro"
    db_session.flush()

    with pytest.raises(rows.UnloadableBracket):
        BirlingBracket(event)
