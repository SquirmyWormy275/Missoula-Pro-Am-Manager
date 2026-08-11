"""Tests for the first no-data-loss Pro-Am Relay table migration."""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest
import sqlalchemy as sa

from tests.conftest import (
    make_college_competitor,
    make_event,
    make_pro_competitor,
    make_team,
    make_tournament,
)

_MIGRATION = (
    pathlib.Path(__file__).resolve().parent.parent
    / "migrations"
    / "versions"
    / "v1a2b3c4d5e6_relay_tables.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("mig_v1a2b3c4d5e6", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig = _load()


def _relay_document(pro, college):
    return {
        "status": "drawn",
        "teams": [{
            "team_number": 1,
            "name": "Team 1",
            "pro_members": [{"id": pro.id, "name": pro.name, "gender": "M"}],
            "college_members": [{"id": college.id, "name": college.name, "gender": "F"}],
            "events": {
                "partnered_sawing": {"result": 20.0, "status": "completed"},
                "standing_butcher_block": {"result": None, "status": "pending"},
                "underhand_butcher_block": {"result": None, "status": "pending"},
                "team_axe_throw": {"result": None, "status": "pending"},
            },
            "total_time": None,
        }],
    }


def _world(session):
    tournament = make_tournament(session)
    school = make_team(session, tournament)
    pro = make_pro_competitor(session, tournament, "Pro One", gender="M")
    college = make_college_competitor(session, tournament, school, "College One", gender="F")
    session.flush()
    relay_event = make_event(
        session,
        tournament,
        "Pro-Am Relay",
        event_type="pro",
        scoring_type="time",
    )
    relay_event.event_state = json.dumps(_relay_document(pro, college))
    session.flush()
    return relay_event, pro, college


@pytest.mark.usefixtures("reference_gate_disarmed")
class TestRelayTableMigration:
    def test_resolvable_legacy_state_projects_to_team_rows(self, db_session):
        relay_event, pro, college = _world(db_session)

        mig._backfill(db_session.connection())

        state = db_session.connection().execute(sa.text(
            "SELECT id, status FROM relay_states WHERE event_id = :event_id"),
            {"event_id": relay_event.id}).one()
        assert state.status == "drawn"

        members = db_session.connection().execute(sa.text(
            "SELECT uid FROM relay_team_members WHERE relay_state_id = :state_id "
            "ORDER BY uid"), {"state_id": state.id}).scalars().all()
        assert members == sorted([pro.uid, college.uid])

        leg = db_session.connection().execute(sa.text(
            "SELECT event_key, result, status FROM relay_team_events "
            "ORDER BY event_key")).fetchall()
        assert len(leg) == 4
        assert {row[0] for row in leg} == set(mig.RELAY_EVENT_KEYS)
        columns = db_session.connection().execute(sa.text(
            "PRAGMA table_info('relay_team_events')")).fetchall()
        assert "uid" not in {column[1] for column in columns}

    def test_unresolvable_member_leaves_every_table_empty(self, db_session):
        relay_event, pro, college = _world(db_session)
        raw = json.loads(relay_event.event_state)
        raw["teams"][0]["college_members"][0]["id"] = 999999
        relay_event.event_state = json.dumps(raw)
        db_session.flush()

        mig._backfill(db_session.connection())

        for table in mig.TABLES:
            assert db_session.connection().execute(
                sa.text(f"SELECT count(*) FROM {table}")).scalar() == 0
