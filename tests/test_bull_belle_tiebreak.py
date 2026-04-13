"""
Phase 5 of the V2.8.0 scoring fix — Bull/Belle of the Woods multi-key tiebreak.

The AWFC tiebreak chain when two competitors share the same individual_points:

  1. More 1st-place finishes wins.
  2. If still tied, more 2nd-place finishes wins.
  3. ... continuing through 6th place.
  4. If still tied through all six, it's a coin flip (manual resolution).
     The current implementation falls back to alphabetical name as the
     stand-in and surfaces a `tied_with_next` flag for the UI to render
     a "TIE — manual resolution required" badge.

This file verifies:
  - The single-query SQL ordering produces the correct rank
  - get_bull_belle_with_tiebreak_data returns placement counts + tie flags
  - Gender filtering excludes the wrong gender
  - The chain breaks ties at each level (1st, 2nd, 3rd, ...)
  - Two competitors with identical points + identical placement vectors
    get the tied_with_next flag
"""
import json
import os
from decimal import Decimal

import pytest

from database import db as _db


@pytest.fixture(scope='module')
def app():
    from tests.db_test_utils import create_test_app
    _app, db_path = create_test_app()
    with _app.app_context():
        _seed(_app)
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _seed(app):
    from models import Tournament
    if not Tournament.query.first():
        t = Tournament(name='Phase 5 Test', year=2026, status='setup')
        _db.session.add(t)
    _db.session.commit()


@pytest.fixture(autouse=True)
def db_session(app):
    with app.app_context():
        _db.session.begin_nested()
        yield _db.session
        _db.session.rollback()


@pytest.fixture()
def tournament(db_session):
    from models import Tournament
    return Tournament.query.first()


def _make_team(session, t):
    from models import Team
    team = Team(tournament_id=t.id, team_code='UM-A',
                school_name='University of Montana', school_abbreviation='UM')
    session.add(team)
    session.flush()
    return team


def _make_competitor(session, t, team, name, gender, points, placements=None):
    """Create a competitor with optional placement EventResult rows.

    placements: dict {position_int: count} — creates that many EventResult rows
                with final_position=position_int for the competitor.
    """
    from models.competitor import CollegeCompetitor
    from models.event import Event, EventResult

    c = CollegeCompetitor(
        tournament_id=t.id, team_id=team.id, name=name, gender=gender,
        events_entered=json.dumps([]), status='active',
        individual_points=Decimal(str(points)),
    )
    session.add(c)
    session.flush()

    if placements:
        for pos, count in placements.items():
            for i in range(count):
                # Each placement needs its own event so the unique constraint
                # on (event_id, competitor_id, competitor_type) doesn't fire.
                event = Event(
                    tournament_id=t.id,
                    name=f'Test event for {name} pos{pos}#{i}',
                    event_type='college', gender=gender,
                    scoring_type='time', scoring_order='lowest_wins',
                    stand_type='underhand', max_stands=5,
                    payouts=json.dumps({}), status='completed',
                    is_finalized=True,
                )
                session.add(event)
                session.flush()
                r = EventResult(
                    event_id=event.id, competitor_id=c.id,
                    competitor_type='college', competitor_name=c.name,
                    result_value=20.0, run1_value=20.0,
                    final_position=pos,
                    points_awarded=Decimal('5.00'),  # not used for tiebreak
                    status='completed',
                )
                session.add(r)
        session.flush()
    return c


# ---------------------------------------------------------------------------
# 1. Basic ordering by points
# ---------------------------------------------------------------------------


class TestBasicPointsOrdering:
    """When points differ, the chain doesn't even fire — points wins."""

    def test_higher_points_ranked_first(self, db_session, tournament):
        team = _make_team(db_session, tournament)
        _make_competitor(db_session, tournament, team, 'A', 'M', 30)
        _make_competitor(db_session, tournament, team, 'B', 'M', 20)
        _make_competitor(db_session, tournament, team, 'C', 'M', 10)
        db_session.flush()

        bull = tournament.get_bull_of_woods(10)
        assert [c.name for c in bull] == ['A', 'B', 'C']

    def test_gender_filter_excludes_wrong_gender(self, db_session, tournament):
        team = _make_team(db_session, tournament)
        _make_competitor(db_session, tournament, team, 'Mike', 'M', 25)
        _make_competitor(db_session, tournament, team, 'Mary', 'F', 30)  # Higher
        _make_competitor(db_session, tournament, team, 'Bob', 'M', 20)
        db_session.flush()

        bull = tournament.get_bull_of_woods(10)
        belle = tournament.get_belle_of_woods(10)
        assert [c.name for c in bull] == ['Mike', 'Bob']  # Mary excluded
        assert [c.name for c in belle] == ['Mary']


# ---------------------------------------------------------------------------
# 2. Tiebreak chain at the 1st-place level
# ---------------------------------------------------------------------------


class TestTiebreakBy1stPlaceCount:
    """Equal points → more 1st-place finishes wins."""

    def test_more_first_place_finishes_wins(self, db_session, tournament):
        team = _make_team(db_session, tournament)
        # Both have 25 points, but A has 2x first place, B has 1.
        _make_competitor(db_session, tournament, team, 'A', 'M', 25,
                         placements={1: 2, 2: 1, 3: 0, 4: 0, 5: 0, 6: 0})
        _make_competitor(db_session, tournament, team, 'B', 'M', 25,
                         placements={1: 1, 2: 2, 3: 1, 4: 0, 5: 0, 6: 0})
        db_session.flush()

        bull = tournament.get_bull_of_woods(10)
        assert [c.name for c in bull] == ['A', 'B']


class TestTiebreakBy2ndPlaceCount:
    """Equal points + equal 1st-place count → more 2nd-place wins."""

    def test_more_second_place_wins(self, db_session, tournament):
        team = _make_team(db_session, tournament)
        _make_competitor(db_session, tournament, team, 'A', 'M', 22,
                         placements={1: 1, 2: 2, 3: 0, 4: 0, 5: 0, 6: 0})
        _make_competitor(db_session, tournament, team, 'B', 'M', 22,
                         placements={1: 1, 2: 1, 3: 3, 4: 0, 5: 0, 6: 0})
        db_session.flush()

        bull = tournament.get_bull_of_woods(10)
        assert [c.name for c in bull] == ['A', 'B']


class TestTiebreakBy3rdPlaceCount:
    """Equal through 1st + 2nd → 3rd-place count breaks the tie."""

    def test_more_third_place_wins(self, db_session, tournament):
        team = _make_team(db_session, tournament)
        _make_competitor(db_session, tournament, team, 'A', 'M', 20,
                         placements={1: 1, 2: 1, 3: 2, 4: 0, 5: 0, 6: 0})
        _make_competitor(db_session, tournament, team, 'B', 'M', 20,
                         placements={1: 1, 2: 1, 3: 1, 4: 5, 5: 0, 6: 0})
        db_session.flush()

        bull = tournament.get_bull_of_woods(10)
        assert [c.name for c in bull] == ['A', 'B']


# ---------------------------------------------------------------------------
# 3. Tied through the entire chain → flag for manual resolution
# ---------------------------------------------------------------------------


class TestUnbreakableTie:
    """Identical points + identical placement vectors → tied_with_next flag."""

    def test_unbreakable_tie_sets_flag(self, db_session, tournament):
        team = _make_team(db_session, tournament)
        # Identical placement vectors → chain can't break the tie.
        _make_competitor(db_session, tournament, team, 'A', 'M', 25,
                         placements={1: 1, 2: 1, 3: 1, 4: 0, 5: 0, 6: 0})
        _make_competitor(db_session, tournament, team, 'B', 'M', 25,
                         placements={1: 1, 2: 1, 3: 1, 4: 0, 5: 0, 6: 0})
        # A solo competitor at higher points so they're not flagged.
        _make_competitor(db_session, tournament, team, 'Z-Higher', 'M', 100,
                         placements={1: 5, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0})
        db_session.flush()

        data = tournament.get_bull_belle_with_tiebreak_data('M', 10)
        assert len(data) == 3
        # Z-Higher leads (no tie)
        assert data[0]['competitor'].name == 'Z-Higher'
        assert data[0]['tied_with_next'] is False
        # A and B are tied through the chain.  A wins by name (alphabetical).
        # The tied_with_next flag is set on the first of the two.
        assert data[1]['competitor'].name == 'A'
        assert data[1]['tied_with_next'] is True
        assert data[2]['competitor'].name == 'B'
        assert data[2]['tied_with_next'] is False  # last row, nothing to tie with

    def test_chain_breakable_ties_dont_set_flag(self, db_session, tournament):
        """Equal points but the chain breaks the tie → no flag."""
        team = _make_team(db_session, tournament)
        _make_competitor(db_session, tournament, team, 'A', 'M', 25,
                         placements={1: 2, 2: 1, 3: 0, 4: 0, 5: 0, 6: 0})
        _make_competitor(db_session, tournament, team, 'B', 'M', 25,
                         placements={1: 1, 2: 2, 3: 1, 4: 0, 5: 0, 6: 0})
        db_session.flush()

        data = tournament.get_bull_belle_with_tiebreak_data('M', 10)
        # A wins by 1st-place count.  No flag — the chain broke the tie.
        assert data[0]['competitor'].name == 'A'
        assert data[0]['tied_with_next'] is False
        assert data[1]['competitor'].name == 'B'
        assert data[1]['tied_with_next'] is False


# ---------------------------------------------------------------------------
# 4. Placement counts surfaced in the result rows
# ---------------------------------------------------------------------------


class TestPlacementCountsInResult:
    def test_placement_counts_match_event_results(self, db_session, tournament):
        team = _make_team(db_session, tournament)
        _make_competitor(db_session, tournament, team, 'A', 'M', 30,
                         placements={1: 3, 2: 1, 3: 0, 4: 1, 5: 0, 6: 1})
        db_session.flush()

        data = tournament.get_bull_belle_with_tiebreak_data('M', 10)
        assert len(data) == 1
        assert data[0]['placements'] == {1: 3, 2: 1, 3: 0, 4: 1, 5: 0, 6: 1}


# ---------------------------------------------------------------------------
# 5. Empty / null edge cases
# ---------------------------------------------------------------------------


class TestEmptyAndNullCases:
    def test_competitor_with_zero_points_appears_at_end(self, db_session, tournament):
        team = _make_team(db_session, tournament)
        _make_competitor(db_session, tournament, team, 'A', 'M', 10,
                         placements={1: 1, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0})
        _make_competitor(db_session, tournament, team, 'B', 'M', 0)  # No results
        db_session.flush()

        bull = tournament.get_bull_of_woods(10)
        assert [c.name for c in bull] == ['A', 'B']

    def test_no_competitors_returns_empty(self, db_session, tournament):
        bull = tournament.get_bull_of_woods(10)
        assert bull == []
        belle = tournament.get_belle_of_woods(10)
        assert belle == []
        data = tournament.get_bull_belle_with_tiebreak_data('M', 10)
        assert data == []


# ---------------------------------------------------------------------------
# 6. Scratch filter — Unit 6
# ---------------------------------------------------------------------------


class TestScratchedCompetitorExcluded:
    """Scratched competitors must NOT appear in Bull/Belle standings."""

    def test_scratched_competitor_excluded(self, db_session, tournament):
        """A competitor with status='scratched' is excluded from standings."""
        from models.competitor import CollegeCompetitor

        team = _make_team(db_session, tournament)
        active = _make_competitor(db_session, tournament, team, 'Active', 'M', 40)
        # Create a scratched competitor directly (bypass helper which sets 'active').
        scratched = CollegeCompetitor(
            tournament_id=tournament.id,
            team_id=team.id,
            name='Scratched',
            gender='M',
            events_entered='[]',
            status='scratched',
            individual_points=50,  # higher points — must NOT appear
        )
        db_session.add(scratched)
        db_session.flush()

        bull = tournament.get_bull_of_woods(10)
        names = [c.name for c in bull]
        assert 'Scratched' not in names
        assert 'Active' in names

    def test_active_competitor_with_points_appears(self, db_session, tournament):
        """An active competitor appears normally in standings."""
        team = _make_team(db_session, tournament)
        _make_competitor(db_session, tournament, team, 'Visible', 'M', 35)
        db_session.flush()

        bull = tournament.get_bull_of_woods(10)
        names = [c.name for c in bull]
        assert 'Visible' in names

    def test_standings_page_no_unfinalized_indicator_when_all_finalized(
        self, db_session, tournament
    ):
        """When all events are finalized, unfinalized_events list is empty."""
        from models.event import Event, EventResult

        team = _make_team(db_session, tournament)
        event = Event(
            tournament_id=tournament.id,
            name='Finalized Event',
            event_type='college',
            gender='M',
            scoring_type='time',
            scoring_order='lowest_wins',
            stand_type='underhand',
            max_stands=5,
            payouts='{}',
            status='completed',
            is_finalized=True,
        )
        db_session.add(event)
        db_session.flush()
        comp = _make_competitor(db_session, tournament, team, 'Comp A', 'M', 20)
        result = EventResult(
            event_id=event.id,
            competitor_id=comp.id,
            competitor_type='college',
            competitor_name=comp.name,
            result_value=25.0,
            run1_value=25.0,
            final_position=1,
            points_awarded=5,
            status='completed',
        )
        db_session.add(result)
        db_session.flush()

        from models.event import Event as Ev
        from models.event import EventResult as ER
        unfinalized = (
            db_session.query(Ev)
            .filter(
                Ev.tournament_id == tournament.id,
                Ev.is_finalized == False,  # noqa: E712
            )
            .filter(
                db_session.query(ER)
                .filter(ER.event_id == Ev.id, ER.status == 'completed')
                .exists()
            )
            .all()
        )
        assert unfinalized == []
