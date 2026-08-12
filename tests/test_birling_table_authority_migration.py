"""The A4 data migration refuses unsafe cleanup and preserves payouts."""
from __future__ import annotations

import copy
import importlib.util
import json
import pathlib

import pytest

from services import birling_rows as rows
from services.birling_bracket import BirlingBracket
from tests.conftest import (
    make_college_competitor,
    make_event,
    make_team,
    make_tournament,
)

_PATH = (pathlib.Path(__file__).resolve().parent.parent / "migrations" / "versions"
         / "w2b3c4d5e6f7_birling_table_authority.py")


def _migration():
    spec = importlib.util.spec_from_file_location("mig_w2b3c4d5e6f7", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    return event, bracket


def test_revision_identity():
    migration = _migration()
    assert migration.revision == "w2b3c4d5e6f7"
    assert migration.down_revision == "v1a2b3c4d5e6"


def test_upgrade_removes_state_and_keeps_numeric_payouts(db_session, monkeypatch):
    migration = _migration()
    event, bracket = _world(db_session)
    legacy = copy.deepcopy(bracket.bracket_data)
    legacy["1"] = 500
    event.payouts = json.dumps(legacy)
    db_session.flush()
    monkeypatch.setattr(migration.op, "get_bind", lambda: db_session.connection())

    migration.upgrade()
    db_session.expire(event)

    assert json.loads(event.payouts) == {"1": 500}
    assert rows.load_document(event)["seeding"] == bracket.bracket_data["seeding"]


def test_upgrade_refuses_unprojected_legacy_state(db_session, monkeypatch):
    migration = _migration()
    event, bracket = _world(db_session)
    legacy = json.dumps(bracket.bracket_data)
    rows.clear_event(event.id)
    event.payouts = legacy
    db_session.flush()
    monkeypatch.setattr(migration.op, "get_bind", lambda: db_session.connection())

    with pytest.raises(RuntimeError, match="legacy state without"):
        migration.upgrade()
    assert event.payouts == legacy
