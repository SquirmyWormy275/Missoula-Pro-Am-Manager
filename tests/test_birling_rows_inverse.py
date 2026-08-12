"""Row-to-document reconstruction used by the Birling service."""
from __future__ import annotations

from services import birling_rows as rows
from services.birling_bracket import BirlingBracket
from tests.conftest import (
    make_college_competitor,
    make_event,
    make_team,
    make_tournament,
)


def _world(session, count=4):
    tournament = make_tournament(session)
    team = make_team(session, tournament)
    people = [make_college_competitor(session, tournament, team, f"Person {i}")
              for i in range(count)]
    event = make_event(session, tournament, "Birling", event_type="college",
                       scoring_type="bracket")
    session.flush()
    return team, event, people


def _generate(event, people):
    bracket = BirlingBracket(event)
    bracket.generate_bracket([
        {"id": person.id, "name": person.display_name} for person in people
    ])
    return bracket


def _all_matches(document):
    bracket = document["bracket"]
    out = []
    for side in ("winners", "losers"):
        for round_matches in bracket[side]:
            out.extend(round_matches)
    for side in ("finals", "true_finals"):
        if bracket[side]:
            out.append(bracket[side])
    return out


def test_generated_bracket_round_trips_from_rows(db_session):
    _team, event, people = _world(db_session)
    expected = _generate(event, people).bracket_data

    loaded = rows.load_document(event)

    assert loaded["seeding"] == expected["seeding"]
    assert [match["match_id"] for match in _all_matches(loaded)] == [
        match["match_id"] for match in _all_matches(expected)
    ]
    assert loaded["competitors"] == expected["competitors"]


def test_live_competitor_name_is_rebuilt_from_identity_join(db_session):
    team, event, people = _world(db_session)
    _generate(event, people)
    people[0].name = "Renamed"
    db_session.flush()

    name = next(person["name"] for person in rows.load_document(event)["competitors"]
                if person["id"] == people[0].id)

    assert name == f"Renamed ({team.team_code})"


def test_pre_seeds_round_trip_without_a_bracket(db_session):
    _team, event, people = _world(db_session)
    pre_seeds = {str(people[0].id): 1, str(people[2].id): 4}

    rows.replace_pre_seedings(event, pre_seeds)
    db_session.flush()

    assert rows.load_document(event)["pre_seedings"] == pre_seeds


def test_pre_seed_update_does_not_rewrite_bracket_rows(db_session):
    _team, event, people = _world(db_session)
    _generate(event, people)
    match_ids = [match["match_id"] for match in _all_matches(rows.load_document(event))]

    rows.replace_pre_seedings(event, {str(people[1].id): 1})
    db_session.flush()

    assert [match["match_id"] for match in _all_matches(rows.load_document(event))] == match_ids
    assert rows.load_pre_seedings(event) == {str(people[1].id): 1}
