"""
Point Calculator / Scoring Engine tests — college placement points,
team point aggregation, individual standings, and team standings.

The public surface tested here is the three functions re-exported by
services/point_calculator.py:
    get_individual_standings
    get_team_standings
    recalculate_all_team_points

These delegate to services/scoring_engine.py. We also exercise the
underlying calculate_positions() flow for college events to verify the
full placement-points pipeline (1st=10, 2nd=7, 3rd=5, 4th=3, 5th=2,
6th=1).

Run:
    pytest tests/test_point_calculator.py -v

Requirements:
    pytest (pip install pytest)
    All app dependencies installed.
"""
import os

import pytest

from database import db as _db

# ---------------------------------------------------------------------------
# Fixtures (same pattern as test_woodboss.py)
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def app():
    """Test Flask app with temp-file SQLite built via flask db upgrade."""
    from tests.db_test_utils import create_test_app
    _app, db_path = create_test_app()

    with _app.app_context():
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def db_session(app):
    """Wrap each test in a transaction and roll back afterward."""
    with app.app_context():
        _db.session.begin_nested()
        yield _db.session
        _db.session.rollback()


@pytest.fixture()
def tournament(db_session):
    """Create a fresh tournament."""
    from models import Tournament
    t = Tournament(name='Points Test 2026', year=2026, status='setup')
    db_session.add(t)
    db_session.flush()
    return t


def _make_team(db_session, tournament, team_code, school_name, school_abbr):
    """Helper: create a Team."""
    from models import Team
    t = Team(
        tournament_id=tournament.id,
        team_code=team_code,
        school_name=school_name,
        school_abbreviation=school_abbr,
    )
    db_session.add(t)
    db_session.flush()
    return t


def _make_college_competitor(db_session, tournament, team, name, gender):
    """Helper: create an active CollegeCompetitor."""
    from models import CollegeCompetitor
    c = CollegeCompetitor(
        tournament_id=tournament.id,
        team_id=team.id,
        name=name,
        gender=gender,
        status='active',
        individual_points=0,
    )
    db_session.add(c)
    db_session.flush()
    return c


def _make_college_event(db_session, tournament, name, gender,
                        scoring_type='time', stand_type='underhand',
                        scoring_order='lowest_wins'):
    """Helper: create a college Event."""
    from models import Event
    e = Event(
        tournament_id=tournament.id,
        name=name,
        event_type='college',
        gender=gender,
        scoring_type=scoring_type,
        stand_type=stand_type,
        scoring_order=scoring_order,
    )
    db_session.add(e)
    db_session.flush()
    return e


def _make_result(db_session, event, competitor, result_value, status='completed'):
    """Helper: create an EventResult."""
    from models.event import EventResult
    r = EventResult(
        event_id=event.id,
        competitor_id=competitor.id,
        competitor_type='college',
        competitor_name=competitor.name,
        result_value=result_value,
        status=status,
    )
    db_session.add(r)
    db_session.flush()
    return r


# ---------------------------------------------------------------------------
# get_individual_standings
# ---------------------------------------------------------------------------

class TestGetIndividualStandings:
    """Tests for get_individual_standings()."""

    def test_empty_tournament(self, db_session, tournament):
        """No competitors returns empty list."""
        from services.point_calculator import get_individual_standings
        standings = get_individual_standings(tournament.id)
        assert standings == []

    def test_single_competitor(self, db_session, tournament):
        """One competitor is ranked 1st."""
        from services.point_calculator import get_individual_standings
        team = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        c = _make_college_competitor(db_session, tournament, team, 'Alice', 'F')
        c.individual_points = 10
        db_session.flush()

        standings = get_individual_standings(tournament.id)
        assert len(standings) == 1
        rank, comp = standings[0]
        assert rank == 1
        assert comp.id == c.id

    def test_ranked_by_points_descending(self, db_session, tournament):
        """Competitors are ranked highest points first."""
        from services.point_calculator import get_individual_standings
        team = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        c1 = _make_college_competitor(db_session, tournament, team, 'Alice', 'F')
        c2 = _make_college_competitor(db_session, tournament, team, 'Bob', 'M')
        c3 = _make_college_competitor(db_session, tournament, team, 'Carol', 'F')
        c1.individual_points = 5
        c2.individual_points = 17
        c3.individual_points = 10
        db_session.flush()

        standings = get_individual_standings(tournament.id)
        names = [comp.name for _, comp in standings]
        assert names == ['Bob', 'Carol', 'Alice']

    def test_tied_competitors_share_rank(self, db_session, tournament):
        """Competitors with equal points share the same rank."""
        from services.point_calculator import get_individual_standings
        team = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        c1 = _make_college_competitor(db_session, tournament, team, 'Alice', 'F')
        c2 = _make_college_competitor(db_session, tournament, team, 'Bob', 'M')
        c3 = _make_college_competitor(db_session, tournament, team, 'Carol', 'F')
        c1.individual_points = 10
        c2.individual_points = 10
        c3.individual_points = 5
        db_session.flush()

        standings = get_individual_standings(tournament.id)
        ranks = [rank for rank, _ in standings]
        # Two tied at 10 share rank 1, then Carol is rank 3 (not 2)
        assert ranks == [1, 1, 3]

    def test_gender_filter(self, db_session, tournament):
        """Gender parameter filters to only that gender."""
        from services.point_calculator import get_individual_standings
        team = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        c1 = _make_college_competitor(db_session, tournament, team, 'Alice', 'F')
        c2 = _make_college_competitor(db_session, tournament, team, 'Bob', 'M')
        c3 = _make_college_competitor(db_session, tournament, team, 'Carol', 'F')
        c1.individual_points = 10
        c2.individual_points = 17
        c3.individual_points = 5
        db_session.flush()

        standings = get_individual_standings(tournament.id, gender='F')
        names = [comp.name for _, comp in standings]
        assert 'Bob' not in names
        assert len(standings) == 2
        assert names[0] == 'Alice'
        assert names[1] == 'Carol'

    def test_limit_parameter(self, db_session, tournament):
        """Limit returns only top N competitors."""
        from services.point_calculator import get_individual_standings
        team = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        for i, name in enumerate(['A', 'B', 'C', 'D', 'E']):
            c = _make_college_competitor(db_session, tournament, team, name, 'M')
            c.individual_points = (5 - i) * 3
        db_session.flush()

        standings = get_individual_standings(tournament.id, limit=3)
        assert len(standings) == 3
        # Highest points first
        assert standings[0][1].name == 'A'

    def test_scratched_excluded(self, db_session, tournament):
        """Scratched competitors do not appear in standings."""
        from services.point_calculator import get_individual_standings
        team = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        c1 = _make_college_competitor(db_session, tournament, team, 'Alice', 'F')
        c1.individual_points = 10
        c2 = _make_college_competitor(db_session, tournament, team, 'Bob', 'M')
        c2.individual_points = 20
        c2.status = 'scratched'
        db_session.flush()

        standings = get_individual_standings(tournament.id)
        names = [comp.name for _, comp in standings]
        assert 'Bob' not in names
        assert len(standings) == 1

    def test_zero_points_included(self, db_session, tournament):
        """Active competitors with 0 points still appear in standings."""
        from services.point_calculator import get_individual_standings
        team = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        c1 = _make_college_competitor(db_session, tournament, team, 'Alice', 'F')
        c1.individual_points = 0
        db_session.flush()

        standings = get_individual_standings(tournament.id)
        assert len(standings) == 1
        assert standings[0][0] == 1


# ---------------------------------------------------------------------------
# get_team_standings
# ---------------------------------------------------------------------------

class TestGetTeamStandings:
    """Tests for get_team_standings()."""

    def test_empty_tournament(self, db_session, tournament):
        """No teams returns empty list."""
        from services.point_calculator import get_team_standings
        standings = get_team_standings(tournament.id)
        assert standings == []

    def test_single_team(self, db_session, tournament):
        """One team is ranked 1st."""
        from services.point_calculator import get_team_standings
        team = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        team.total_points = 42
        db_session.flush()

        standings = get_team_standings(tournament.id)
        assert len(standings) == 1
        rank, t = standings[0]
        assert rank == 1
        assert t.id == team.id

    def test_ranked_by_points_descending(self, db_session, tournament):
        """Teams are ranked highest points first."""
        from services.point_calculator import get_team_standings
        t1 = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        t2 = _make_team(db_session, tournament, 'MSU-A', 'Montana State University', 'MSU')
        t3 = _make_team(db_session, tournament, 'CSU-A', 'Colorado State University', 'CSU')
        t1.total_points = 30
        t2.total_points = 50
        t3.total_points = 40
        db_session.flush()

        standings = get_team_standings(tournament.id)
        codes = [t.team_code for _, t in standings]
        assert codes == ['MSU-A', 'CSU-A', 'UM-A']

    def test_tied_teams_share_rank(self, db_session, tournament):
        """Teams with equal points share the same rank."""
        from services.point_calculator import get_team_standings
        t1 = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        t2 = _make_team(db_session, tournament, 'MSU-A', 'Montana State University', 'MSU')
        t3 = _make_team(db_session, tournament, 'CSU-A', 'Colorado State University', 'CSU')
        t1.total_points = 40
        t2.total_points = 40
        t3.total_points = 20
        db_session.flush()

        standings = get_team_standings(tournament.id)
        ranks = [rank for rank, _ in standings]
        assert ranks == [1, 1, 3]

    def test_limit_parameter(self, db_session, tournament):
        """Limit returns only top N teams."""
        from services.point_calculator import get_team_standings
        for i, code in enumerate(['UM-A', 'MSU-A', 'CSU-A', 'UI-A']):
            t = _make_team(db_session, tournament, code, f'School {code}', code.split('-')[0])
            t.total_points = (4 - i) * 10
        db_session.flush()

        standings = get_team_standings(tournament.id, limit=2)
        assert len(standings) == 2
        assert standings[0][1].team_code == 'UM-A'

    def test_inactive_teams_excluded(self, db_session, tournament):
        """Only active teams appear in standings."""
        from services.point_calculator import get_team_standings
        t1 = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        t2 = _make_team(db_session, tournament, 'MSU-A', 'Montana State University', 'MSU')
        t1.total_points = 30
        t2.total_points = 50
        t2.status = 'invalid'
        db_session.flush()

        standings = get_team_standings(tournament.id)
        codes = [t.team_code for _, t in standings]
        assert 'MSU-A' not in codes
        assert len(standings) == 1


# ---------------------------------------------------------------------------
# recalculate_all_team_points
# ---------------------------------------------------------------------------

class TestRecalculateAllTeamPoints:
    """Tests for recalculate_all_team_points()."""

    def test_recalculates_from_member_points(self, db_session, tournament):
        """Team total_points is recomputed from its members' individual_points."""
        from services.point_calculator import recalculate_all_team_points
        team = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        c1 = _make_college_competitor(db_session, tournament, team, 'Alice', 'F')
        c2 = _make_college_competitor(db_session, tournament, team, 'Bob', 'M')
        c1.individual_points = 10
        c2.individual_points = 7
        # Set stale total
        team.total_points = 0
        db_session.flush()

        recalculate_all_team_points(tournament.id)
        assert team.total_points == 17

    def test_multiple_teams_all_updated(self, db_session, tournament):
        """All teams in the tournament are recalculated."""
        from services.point_calculator import recalculate_all_team_points
        t1 = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        t2 = _make_team(db_session, tournament, 'MSU-A', 'Montana State University', 'MSU')
        c1 = _make_college_competitor(db_session, tournament, t1, 'Alice', 'F')
        c2 = _make_college_competitor(db_session, tournament, t2, 'Bob', 'M')
        c1.individual_points = 10
        c2.individual_points = 5
        t1.total_points = 0
        t2.total_points = 0
        db_session.flush()

        recalculate_all_team_points(tournament.id)
        assert t1.total_points == 10
        assert t2.total_points == 5

    def test_scratched_members_excluded(self, db_session, tournament):
        """Scratched members do not contribute to team total."""
        from services.point_calculator import recalculate_all_team_points
        team = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        c1 = _make_college_competitor(db_session, tournament, team, 'Alice', 'F')
        c2 = _make_college_competitor(db_session, tournament, team, 'Bob', 'M')
        c1.individual_points = 10
        c2.individual_points = 7
        c2.status = 'scratched'
        db_session.flush()

        recalculate_all_team_points(tournament.id)
        assert team.total_points == 10

    def test_empty_team_gets_zero(self, db_session, tournament):
        """A team with no members recalculates to 0."""
        from services.point_calculator import recalculate_all_team_points
        team = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        team.total_points = 99
        db_session.flush()

        recalculate_all_team_points(tournament.id)
        assert team.total_points == 0


# ---------------------------------------------------------------------------
# calculate_positions — college placement points pipeline
# ---------------------------------------------------------------------------

class TestCalculatePositionsCollege:
    """Tests for calculate_positions() with college events — verifying the
    full placement-points pipeline (1st=10, 2nd=7, 3rd=5, 4th=3, 5th=2, 6th=1).
    """

    def test_standard_placement_points(self, db_session, tournament):
        """Top 6 receive correct placement points."""
        from services.scoring_engine import calculate_positions
        team = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        event = _make_college_event(db_session, tournament, 'Underhand Speed', 'M')

        competitors = []
        # Create 6 competitors with times 10.0, 20.0, ..., 60.0
        for i in range(6):
            c = _make_college_competitor(db_session, tournament, team, f'Comp{i+1}', 'M')
            competitors.append(c)
            _make_result(db_session, event, c, (i + 1) * 10.0)

        calculate_positions(event)

        results = event.results.order_by('final_position').all()
        expected_points = [10, 7, 5, 3, 2, 1]
        for r, expected in zip(results, expected_points):
            assert r.points_awarded == expected

    def test_seventh_place_gets_zero(self, db_session, tournament):
        """7th place and beyond receive 0 points."""
        from services.scoring_engine import calculate_positions
        team = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        event = _make_college_event(db_session, tournament, 'Underhand Speed', 'M')

        for i in range(8):
            c = _make_college_competitor(db_session, tournament, team, f'Comp{i+1}', 'M')
            _make_result(db_session, event, c, (i + 1) * 10.0)

        calculate_positions(event)

        results = event.results.order_by('final_position').all()
        for r in results:
            if r.final_position > 6:
                assert r.points_awarded == 0

    def test_positions_lowest_wins(self, db_session, tournament):
        """For time-based events (lowest_wins), lowest result_value gets 1st."""
        from services.scoring_engine import calculate_positions
        team = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        event = _make_college_event(db_session, tournament, 'Underhand Speed', 'M',
                                    scoring_type='time', scoring_order='lowest_wins')

        c1 = _make_college_competitor(db_session, tournament, team, 'Fast', 'M')
        c2 = _make_college_competitor(db_session, tournament, team, 'Slow', 'M')
        _make_result(db_session, event, c1, 15.0)
        _make_result(db_session, event, c2, 30.0)

        calculate_positions(event)

        r1 = event.results.filter_by(competitor_id=c1.id).first()
        r2 = event.results.filter_by(competitor_id=c2.id).first()
        assert r1.final_position == 1
        assert r2.final_position == 2
        assert r1.points_awarded == 10
        assert r2.points_awarded == 7

    def test_positions_highest_wins(self, db_session, tournament):
        """For score-based events (highest_wins), highest result_value gets 1st."""
        from services.scoring_engine import calculate_positions
        team = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        event = _make_college_event(db_session, tournament, 'Axe Throw', 'M',
                                    scoring_type='score', scoring_order='highest_wins')

        c1 = _make_college_competitor(db_session, tournament, team, 'High', 'M')
        c2 = _make_college_competitor(db_session, tournament, team, 'Low', 'M')
        _make_result(db_session, event, c1, 25.0)
        _make_result(db_session, event, c2, 10.0)

        calculate_positions(event)

        r1 = event.results.filter_by(competitor_id=c1.id).first()
        r2 = event.results.filter_by(competitor_id=c2.id).first()
        assert r1.final_position == 1
        assert r2.final_position == 2

    def test_tied_competitors_share_position(self, db_session, tournament):
        """Tied competitors share the same position; next position is skipped."""
        from services.scoring_engine import calculate_positions
        team = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        event = _make_college_event(db_session, tournament, 'Underhand Speed', 'M')

        c1 = _make_college_competitor(db_session, tournament, team, 'A', 'M')
        c2 = _make_college_competitor(db_session, tournament, team, 'B', 'M')
        c3 = _make_college_competitor(db_session, tournament, team, 'C', 'M')
        _make_result(db_session, event, c1, 20.0)
        _make_result(db_session, event, c2, 20.0)
        _make_result(db_session, event, c3, 30.0)

        calculate_positions(event)

        r1 = event.results.filter_by(competitor_id=c1.id).first()
        r2 = event.results.filter_by(competitor_id=c2.id).first()
        r3 = event.results.filter_by(competitor_id=c3.id).first()
        # Both tied at 20.0 share position 1
        assert r1.final_position == 1
        assert r2.final_position == 1
        # Next is position 3 (position 2 skipped)
        assert r3.final_position == 3

    def test_tied_competitors_both_get_same_points(self, db_session, tournament):
        """Tied competitors at position N both receive the points for position N."""
        from services.scoring_engine import calculate_positions
        team = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        event = _make_college_event(db_session, tournament, 'Underhand Speed', 'M')

        c1 = _make_college_competitor(db_session, tournament, team, 'A', 'M')
        c2 = _make_college_competitor(db_session, tournament, team, 'B', 'M')
        _make_result(db_session, event, c1, 20.0)
        _make_result(db_session, event, c2, 20.0)

        calculate_positions(event)

        r1 = event.results.filter_by(competitor_id=c1.id).first()
        r2 = event.results.filter_by(competitor_id=c2.id).first()
        # Both get 1st place points
        assert r1.points_awarded == 10
        assert r2.points_awarded == 10

    def test_dnf_excluded_from_positions(self, db_session, tournament):
        """DNF results are not assigned a position."""
        from services.scoring_engine import calculate_positions
        team = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        event = _make_college_event(db_session, tournament, 'Underhand Speed', 'M')

        c1 = _make_college_competitor(db_session, tournament, team, 'Finisher', 'M')
        c2 = _make_college_competitor(db_session, tournament, team, 'DNF', 'M')
        _make_result(db_session, event, c1, 20.0, status='completed')
        _make_result(db_session, event, c2, None, status='dnf')

        calculate_positions(event)

        r1 = event.results.filter_by(competitor_id=c1.id).first()
        r2 = event.results.filter_by(competitor_id=c2.id).first()
        assert r1.final_position == 1
        assert r1.points_awarded == 10
        # DNF has no position
        assert r2.final_position is None

    def test_no_completed_results_resets_event(self, db_session, tournament):
        """If no results are completed, event status goes back to in_progress."""
        from services.scoring_engine import calculate_positions
        team = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        event = _make_college_event(db_session, tournament, 'Underhand Speed', 'M')

        c1 = _make_college_competitor(db_session, tournament, team, 'Pending', 'M')
        _make_result(db_session, event, c1, None, status='pending')

        calculate_positions(event)

        assert event.status == 'in_progress'
        assert event.is_finalized is False

    def test_points_accumulate_across_events(self, db_session, tournament):
        """A competitor's individual_points accumulates across multiple events."""
        from services.scoring_engine import calculate_positions
        team = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        event1 = _make_college_event(db_session, tournament, 'Underhand Speed', 'M')
        event2 = _make_college_event(db_session, tournament, 'Standing Block Speed', 'M',
                                     stand_type='standing_block')

        c = _make_college_competitor(db_session, tournament, team, 'Alice', 'M')
        _make_result(db_session, event1, c, 15.0)
        _make_result(db_session, event2, c, 20.0)

        calculate_positions(event1)
        calculate_positions(event2)

        # 1st in both events = 10 + 10 = 20
        assert c.individual_points == 20

    def test_team_points_updated_after_calculate(self, db_session, tournament):
        """Team total_points is updated when calculate_positions finishes."""
        from services.scoring_engine import calculate_positions
        team = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        event = _make_college_event(db_session, tournament, 'Underhand Speed', 'M')

        c1 = _make_college_competitor(db_session, tournament, team, 'A', 'M')
        c2 = _make_college_competitor(db_session, tournament, team, 'B', 'M')
        _make_result(db_session, event, c1, 15.0)
        _make_result(db_session, event, c2, 20.0)

        calculate_positions(event)

        # 1st=10, 2nd=7 → team total = 17
        assert team.total_points == 17

    def test_idempotent_recalculation(self, db_session, tournament):
        """Calling calculate_positions twice produces the same result."""
        from services.scoring_engine import calculate_positions
        team = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        event = _make_college_event(db_session, tournament, 'Underhand Speed', 'M')

        c = _make_college_competitor(db_session, tournament, team, 'Alice', 'M')
        _make_result(db_session, event, c, 15.0)

        calculate_positions(event)
        assert c.individual_points == 10

        calculate_positions(event)
        assert c.individual_points == 10

    def test_event_marked_finalized(self, db_session, tournament):
        """After calculate_positions, event.is_finalized is True."""
        from services.scoring_engine import calculate_positions
        team = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        event = _make_college_event(db_session, tournament, 'Underhand Speed', 'M')
        c = _make_college_competitor(db_session, tournament, team, 'A', 'M')
        _make_result(db_session, event, c, 15.0)

        calculate_positions(event)

        assert event.is_finalized is True
        assert event.status == 'completed'


# ---------------------------------------------------------------------------
# Cross-team point aggregation
# ---------------------------------------------------------------------------

class TestCrossTeamPointAggregation:
    """Verify team standings reflect multi-team competition."""

    def test_multi_team_standings(self, db_session, tournament):
        """Teams from different schools accumulate points separately."""
        from services.point_calculator import get_team_standings
        from services.scoring_engine import calculate_positions

        t1 = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        t2 = _make_team(db_session, tournament, 'MSU-A', 'Montana State University', 'MSU')

        event = _make_college_event(db_session, tournament, 'Underhand Speed', 'M')

        # UM competitor finishes 1st (10 pts), MSU finishes 2nd (7 pts)
        c1 = _make_college_competitor(db_session, tournament, t1, 'UM-Alice', 'M')
        c2 = _make_college_competitor(db_session, tournament, t2, 'MSU-Bob', 'M')
        _make_result(db_session, event, c1, 10.0)
        _make_result(db_session, event, c2, 20.0)

        calculate_positions(event)

        standings = get_team_standings(tournament.id)
        assert len(standings) == 2
        # UM should be first with 10 pts
        assert standings[0][1].team_code == 'UM-A'
        assert standings[0][1].total_points == 10
        # MSU second with 7 pts
        assert standings[1][1].team_code == 'MSU-A'
        assert standings[1][1].total_points == 7

    def test_multi_event_team_accumulation(self, db_session, tournament):
        """Team points accumulate across multiple events."""
        from services.point_calculator import get_team_standings
        from services.scoring_engine import calculate_positions

        t1 = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        t2 = _make_team(db_session, tournament, 'MSU-A', 'Montana State University', 'MSU')

        event1 = _make_college_event(db_session, tournament, 'Underhand Speed', 'M')
        event2 = _make_college_event(db_session, tournament, 'Standing Block Speed', 'M',
                                     stand_type='standing_block')

        c1 = _make_college_competitor(db_session, tournament, t1, 'UM-Alice', 'M')
        c2 = _make_college_competitor(db_session, tournament, t2, 'MSU-Bob', 'M')

        # Event 1: UM wins (10pts), MSU 2nd (7pts)
        _make_result(db_session, event1, c1, 10.0)
        _make_result(db_session, event1, c2, 20.0)
        calculate_positions(event1)

        # Event 2: MSU wins (10pts), UM 2nd (7pts)
        _make_result(db_session, event2, c2, 10.0)
        _make_result(db_session, event2, c1, 20.0)
        calculate_positions(event2)

        standings = get_team_standings(tournament.id)
        # Both should have 17 pts (10 + 7)
        points = {t.team_code: t.total_points for _, t in standings}
        assert points['UM-A'] == 17
        assert points['MSU-A'] == 17

    def test_multiple_members_contribute(self, db_session, tournament):
        """Multiple members on the same team all contribute to team total."""
        from services.point_calculator import get_team_standings
        from services.scoring_engine import calculate_positions

        team = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        event = _make_college_event(db_session, tournament, 'Underhand Speed', 'M')

        c1 = _make_college_competitor(db_session, tournament, team, 'A', 'M')
        c2 = _make_college_competitor(db_session, tournament, team, 'B', 'M')
        c3 = _make_college_competitor(db_session, tournament, team, 'C', 'M')
        _make_result(db_session, event, c1, 10.0)
        _make_result(db_session, event, c2, 20.0)
        _make_result(db_session, event, c3, 30.0)

        calculate_positions(event)

        # 1st=10, 2nd=7, 3rd=5 → team = 22
        standings = get_team_standings(tournament.id)
        assert standings[0][1].total_points == 22


# ---------------------------------------------------------------------------
# Bull / Belle of the Woods (top individual per gender)
# ---------------------------------------------------------------------------

class TestBullBelleOfTheWoods:
    """Verify individual standings correctly identify top male and female."""

    def test_bull_of_the_woods(self, db_session, tournament):
        """Top male competitor by individual_points."""
        from services.point_calculator import get_individual_standings
        from services.scoring_engine import calculate_positions

        team = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        event = _make_college_event(db_session, tournament, 'Underhand Speed', 'M')

        c1 = _make_college_competitor(db_session, tournament, team, 'Bull', 'M')
        c2 = _make_college_competitor(db_session, tournament, team, 'Other', 'M')
        _make_result(db_session, event, c1, 10.0)
        _make_result(db_session, event, c2, 20.0)

        calculate_positions(event)

        standings = get_individual_standings(tournament.id, gender='M', limit=1)
        assert len(standings) == 1
        assert standings[0][1].name == 'Bull'

    def test_belle_of_the_woods(self, db_session, tournament):
        """Top female competitor by individual_points."""
        from services.point_calculator import get_individual_standings
        from services.scoring_engine import calculate_positions

        team = _make_team(db_session, tournament, 'UM-A', 'University of Montana', 'UM')
        event = _make_college_event(db_session, tournament, 'Underhand Speed', 'F')

        c1 = _make_college_competitor(db_session, tournament, team, 'Belle', 'F')
        c2 = _make_college_competitor(db_session, tournament, team, 'Other', 'F')
        _make_result(db_session, event, c1, 10.0)
        _make_result(db_session, event, c2, 20.0)

        calculate_positions(event)

        standings = get_individual_standings(tournament.id, gender='F', limit=1)
        assert len(standings) == 1
        assert standings[0][1].name == 'Belle'
