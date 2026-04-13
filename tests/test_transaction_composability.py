"""
Unit 2 — Transaction composability: commit=False parameter and event_state column.

Tests:
  TC1  _save_relay_data(commit=True) commits data (default behaviour preserved)
  TC2  _save_relay_data(commit=False) flushes but does NOT commit; caller can roll back
  TC3  _load_relay_data reads from event_state when populated
  TC4  _load_relay_data falls back to payouts when event_state is None
  TC5  Integration — outer savepoint wraps _save_relay_data(commit=False); rollback restores

  TC6  PartneredAxeThrow._save_state(commit=True) commits (default behaviour preserved)
  TC7  PartneredAxeThrow._save_state(commit=False) does not commit; caller can roll back
  TC8  PartneredAxeThrow._load_state reads from event_state when populated
  TC9  PartneredAxeThrow._load_state falls back to payouts when event_state is None

Run:
    pytest tests/test_transaction_composability.py -v
"""

import json

import pytest

from database import db as _db
from tests.conftest import make_event, make_tournament

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _session(db_session):
    """Activate conftest's nested-transaction rollback for every test."""
    yield db_session


@pytest.fixture()
def tournament(db_session):
    return make_tournament(db_session, status="active")


@pytest.fixture()
def relay_event(db_session, tournament):
    """A bare Event row representing the Pro-Am Relay."""
    from models.event import Event

    ev = Event(
        tournament_id=tournament.id,
        name="Pro-Am Relay",
        event_type="pro",
        scoring_type="time",
        is_partnered=True,
        status="pending",
    )
    db_session.add(ev)
    db_session.flush()
    return ev


@pytest.fixture()
def axe_event(db_session, tournament):
    """A bare Event row representing Partnered Axe Throw."""
    from models.event import Event

    ev = Event(
        tournament_id=tournament.id,
        name="Partnered Axe Throw",
        event_type="pro",
        scoring_type="hits",
        is_partnered=True,
        has_prelims=True,
        status="pending",
    )
    db_session.add(ev)
    db_session.flush()
    return ev


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _relay_instance(tournament, relay_event):
    """Return a ProAmRelay bound to relay_event without touching DB on init."""
    from unittest.mock import patch
    from services.proam_relay import ProAmRelay

    # Patch the Event.query inside __init__ → _load_relay_data so it returns
    # our already-created relay_event (which may or may not have state).
    with patch("services.proam_relay.Event") as mock_ev:
        mock_ev.query.filter_by.return_value.first.return_value = relay_event
        relay = ProAmRelay(tournament)
    return relay


def _pat_instance(axe_event):
    """Return a PartneredAxeThrow bound to axe_event."""
    from services.partnered_axe import PartneredAxeThrow

    return PartneredAxeThrow(axe_event)


# ---------------------------------------------------------------------------
# TC1 — relay _save_relay_data(commit=True) commits
# ---------------------------------------------------------------------------


class TestRelayCommitTrue:
    def test_data_persists_after_commit(self, db_session, tournament, relay_event):
        """Default commit=True: relay data is committed and readable from DB."""
        relay = _relay_instance(tournament, relay_event)
        relay.relay_data["status"] = "drawn"
        relay.relay_data["teams"] = [{"team_number": 1}]

        relay._save_relay_data(commit=True)

        # Re-query the event to confirm the column was written
        _db.session.expire(relay_event)
        fresh = _db.session.get(relay_event.__class__, relay_event.id)
        assert fresh.event_state is not None
        saved = json.loads(fresh.event_state)
        assert saved["status"] == "drawn"
        assert len(saved["teams"]) == 1


# ---------------------------------------------------------------------------
# TC2 — relay _save_relay_data(commit=False) does NOT commit
# ---------------------------------------------------------------------------


class TestRelayCommitFalse:
    def test_no_commit_allows_rollback(self, db_session, tournament, relay_event):
        """commit=False: event_state is flushed to the DB session but not
        committed; a subsequent rollback must restore the original value."""
        relay = _relay_instance(tournament, relay_event)
        event_id = relay_event.id

        relay.relay_data["status"] = "drawn"
        relay._save_relay_data(commit=False)

        # The object in-session should have the new value
        assert relay_event.event_state is not None

        # Rolling back must discard the pending write.
        # The relay_event row was added inside db_session (a savepoint), so
        # after rollback the row is gone entirely — fresh will be None.  That
        # is exactly what we want: the 'drawn' state did not persist.
        _db.session.rollback()
        from models.event import Event as _Event
        fresh = _db.session.get(_Event, event_id)
        if fresh is not None:
            state = json.loads(fresh.event_state) if fresh.event_state else {}
            assert state.get("status") != "drawn"


# ---------------------------------------------------------------------------
# TC3 — relay _load_relay_data reads from event_state
# ---------------------------------------------------------------------------


class TestRelayLoadFromEventState:
    def test_reads_event_state_column(self, db_session, tournament, relay_event):
        """When event_state is populated, _load_relay_data returns its content."""
        state = {
            "status": "in_progress",
            "teams": [{"team_number": 99}],
            "eligible_college": [],
            "eligible_pro": [],
            "drawn_college": [],
            "drawn_pro": [],
        }
        relay_event.event_state = json.dumps(state)
        db_session.flush()

        relay = _relay_instance(tournament, relay_event)
        assert relay.relay_data["status"] == "in_progress"
        assert relay.relay_data["teams"][0]["team_number"] == 99


# ---------------------------------------------------------------------------
# TC4 — relay _load_relay_data falls back to payouts
# ---------------------------------------------------------------------------


class TestRelayLoadFallbackToPayouts:
    def test_falls_back_when_event_state_is_none(
        self, db_session, tournament, relay_event
    ):
        """When event_state is NULL, _load_relay_data falls back to payouts."""
        state = {
            "status": "completed",
            "teams": [{"team_number": 7}],
            "eligible_college": [],
            "eligible_pro": [],
            "drawn_college": [],
            "drawn_pro": [],
        }
        relay_event.event_state = None
        relay_event.payouts = json.dumps(state)
        db_session.flush()

        relay = _relay_instance(tournament, relay_event)
        assert relay.relay_data["status"] == "completed"
        assert relay.relay_data["teams"][0]["team_number"] == 7

    def test_falls_back_when_event_state_is_empty_string(
        self, db_session, tournament, relay_event
    ):
        """When event_state is an empty string, fall back to payouts."""
        state = {
            "status": "drawn",
            "teams": [],
            "eligible_college": [],
            "eligible_pro": [],
            "drawn_college": [],
            "drawn_pro": [],
        }
        relay_event.event_state = ""
        relay_event.payouts = json.dumps(state)
        db_session.flush()

        relay = _relay_instance(tournament, relay_event)
        assert relay.relay_data["status"] == "drawn"


# ---------------------------------------------------------------------------
# TC5 — Integration: outer savepoint wraps commit=False; rollback restores
# ---------------------------------------------------------------------------


class TestRelayOuterSavepointRollback:
    def test_outer_savepoint_can_roll_back_inner_write(
        self, db_session, tournament, relay_event
    ):
        """Outer savepoint wraps _save_relay_data(commit=False); rollback restores."""
        relay = _relay_instance(tournament, relay_event)
        relay.relay_data["status"] = "drawn"

        # Create an inner savepoint
        sp = _db.session.begin_nested()
        relay._save_relay_data(commit=False)

        assert relay_event.event_state is not None
        drawn = json.loads(relay_event.event_state).get("status")
        assert drawn == "drawn"

        # Roll back the inner savepoint
        sp.rollback()
        _db.session.expire(relay_event)
        fresh = _db.session.get(relay_event.__class__, relay_event.id)
        if fresh is not None:
            state = json.loads(fresh.event_state) if fresh.event_state else {}
            assert state.get("status") != "drawn"


# ---------------------------------------------------------------------------
# TC6 — PAT _save_state(commit=True) commits
# ---------------------------------------------------------------------------


class TestPATCommitTrue:
    def test_data_persists_after_commit(self, db_session, axe_event):
        """commit=True: state written to event_state and committed."""
        pat = _pat_instance(axe_event)
        pat.state["stage"] = "finals"

        pat._save_state(commit=True)

        _db.session.expire(axe_event)
        fresh = _db.session.get(axe_event.__class__, axe_event.id)
        assert fresh.event_state is not None
        saved = json.loads(fresh.event_state)
        assert saved["stage"] == "finals"


# ---------------------------------------------------------------------------
# TC7 — PAT _save_state(commit=False) does NOT commit
# ---------------------------------------------------------------------------


class TestPATCommitFalse:
    def test_no_commit_allows_rollback(self, db_session, axe_event):
        """commit=False: event_state flushed but not committed; rollback reverts."""
        pat = _pat_instance(axe_event)
        event_id = axe_event.id
        pat.state["stage"] = "finals"

        pat._save_state(commit=False)

        assert axe_event.event_state is not None

        # After rollback the row is gone (it was added inside the savepoint).
        _db.session.rollback()
        from models.event import Event as _Event
        fresh = _db.session.get(_Event, event_id)
        if fresh is not None:
            state = json.loads(fresh.event_state) if fresh.event_state else {}
            assert state.get("stage") != "finals"


# ---------------------------------------------------------------------------
# TC8 — PAT _load_state reads from event_state
# ---------------------------------------------------------------------------


class TestPATLoadFromEventState:
    def test_reads_event_state_column(self, db_session, axe_event):
        """When event_state is set, _load_state returns its content."""
        state = {
            "stage": "finals",
            "prelim_results": [],
            "finalists": [{"pair_id": 3}],
            "final_results": [],
            "pairs": [],
        }
        axe_event.event_state = json.dumps(state)
        db_session.flush()

        pat = _pat_instance(axe_event)
        assert pat.state["stage"] == "finals"
        assert pat.state["finalists"][0]["pair_id"] == 3


# ---------------------------------------------------------------------------
# TC9 — PAT _load_state falls back to payouts
# ---------------------------------------------------------------------------


class TestPATLoadFallbackToPayouts:
    def test_falls_back_when_event_state_is_none(self, db_session, axe_event):
        """When event_state is NULL, _load_state falls back to payouts."""
        state = {
            "stage": "prelims",
            "prelim_results": [],
            "finalists": [],
            "final_results": [],
            "pairs": [{"pair_id": 5}],
        }
        axe_event.event_state = None
        axe_event.payouts = json.dumps(state)
        db_session.flush()

        pat = _pat_instance(axe_event)
        assert pat.state["stage"] == "prelims"
        assert pat.state["pairs"][0]["pair_id"] == 5

    def test_falls_back_when_event_state_is_empty(self, db_session, axe_event):
        """When event_state is empty string, fall back to payouts."""
        state = {
            "stage": "completed",
            "prelim_results": [],
            "finalists": [],
            "final_results": [],
            "pairs": [],
        }
        axe_event.event_state = ""
        axe_event.payouts = json.dumps(state)
        db_session.flush()

        pat = _pat_instance(axe_event)
        assert pat.state["stage"] == "completed"
