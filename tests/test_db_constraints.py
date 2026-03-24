"""
Database constraint enforcement tests.

Verifies that SQLAlchemy/SQLite enforce the data integrity rules that
the application relies on:
  - Foreign key cascades (delete tournament → events/heats/results gone)
  - Unique constraints (duplicate team codes, duplicate event results)
  - Nullable enforcement
  - Cascade delete-orphan behavior

These tests catch silent data corruption that only surfaces in production.

Run:
    pytest tests/test_db_constraints.py -v
    pytest -m integration
"""
from __future__ import annotations

import os
import pytest
from sqlalchemy.exc import IntegrityError
from tests.conftest import (
    make_tournament, make_team, make_college_competitor,
    make_pro_competitor, make_event, make_heat, make_event_result, make_flight,
)

pytestmark = pytest.mark.integration


# ===========================================================================
# FOREIGN KEY CASCADES
# ===========================================================================

class TestForeignKeyCascades:
    """Verify cascade delete from parent to children."""

    def test_delete_tournament_cascades_to_events(self, db_session):
        from models.event import Event
        t = make_tournament(db_session)
        e = make_event(db_session, t, 'Cascade Event', event_type='pro')
        eid = e.id
        db_session.flush()

        db_session.delete(t)
        db_session.flush()

        assert Event.query.get(eid) is None

    def test_delete_tournament_cascades_to_teams(self, db_session):
        from models.team import Team
        t = make_tournament(db_session)
        team = make_team(db_session, t)
        team_id = team.id
        db_session.flush()

        db_session.delete(t)
        db_session.flush()

        assert Team.query.get(team_id) is None

    def test_delete_tournament_cascades_to_competitors(self, db_session):
        from models.competitor import CollegeCompetitor, ProCompetitor
        t = make_tournament(db_session)
        team = make_team(db_session, t)
        cc = make_college_competitor(db_session, t, team, 'College C', 'F')
        pc = make_pro_competitor(db_session, t, 'Pro C', 'M')
        cc_id, pc_id = cc.id, pc.id
        db_session.flush()

        db_session.delete(t)
        db_session.flush()

        assert CollegeCompetitor.query.get(cc_id) is None
        assert ProCompetitor.query.get(pc_id) is None

    def test_delete_event_cascades_to_heats_and_results(self, db_session):
        from models.heat import Heat
        from models.event import EventResult
        t = make_tournament(db_session)
        p = make_pro_competitor(db_session, t, 'Pro X', 'M')
        e = make_event(db_session, t, 'Del Event', event_type='pro')
        h = make_heat(db_session, e, competitors=[p.id])
        r = make_event_result(db_session, e, p, competitor_type='pro',
                              result_value=30.0, status='completed')
        hid, rid = h.id, r.id
        db_session.flush()

        db_session.delete(e)
        db_session.flush()

        assert Heat.query.get(hid) is None
        assert EventResult.query.get(rid) is None


# ===========================================================================
# UNIQUE CONSTRAINTS
# ===========================================================================

class TestUniqueConstraints:
    """Verify unique constraints reject duplicate data."""

    def test_duplicate_team_code_same_tournament_rejected(self, db_session):
        t = make_tournament(db_session)
        make_team(db_session, t, code='UM-A')
        db_session.flush()

        with pytest.raises(IntegrityError):
            make_team(db_session, t, code='UM-A')
            db_session.flush()
        db_session.rollback()

    def test_same_team_code_different_tournaments_allowed(self, db_session):
        t1 = make_tournament(db_session, name='T1', year=2025)
        t2 = make_tournament(db_session, name='T2', year=2026)
        make_team(db_session, t1, code='UM-A')
        make_team(db_session, t2, code='UM-A')
        db_session.flush()  # Should not raise

    @pytest.mark.skipif(
        os.environ.get('TEST_USE_CREATE_ALL') == '1',
        reason='create_all enforces model UniqueConstraint not present in migration chain'
    )
    def test_duplicate_event_result_allowed(self, db_session):
        """EventResult has no unique constraint on (event_id, competitor_id,
        competitor_type) — duplicates are intentionally allowed (e.g. re-entry
        after scratch)."""
        t = make_tournament(db_session)
        p = make_pro_competitor(db_session, t, 'Dup Pro', 'M')
        e = make_event(db_session, t, 'Dup Event', event_type='pro')
        make_event_result(db_session, e, p, competitor_type='pro',
                          result_value=20.0, status='completed')
        db_session.flush()

        make_event_result(db_session, e, p, competitor_type='pro',
                          result_value=22.0, status='completed')
        db_session.flush()  # Should not raise — duplicates are permitted

    @pytest.mark.skipif(
        os.environ.get('TEST_USE_CREATE_ALL') == '1',
        reason='create_all enforces model UniqueConstraint not present in migration chain'
    )
    def test_duplicate_heat_event_run_allowed(self, db_session):
        """Heat has no unique constraint on (event_id, heat_number, run_number)
        — duplicates are permitted by design."""
        t = make_tournament(db_session)
        e = make_event(db_session, t, 'Heat Dup', event_type='pro')
        make_heat(db_session, e, heat_number=1, run_number=1)
        db_session.flush()

        make_heat(db_session, e, heat_number=1, run_number=1)
        db_session.flush()  # Should not raise — duplicates are permitted

    def test_duplicate_username_rejected(self, db_session):
        from models.user import User
        u1 = User(username='unique_test', role='scorer')
        u1.set_password('pass1')
        db_session.add(u1)
        db_session.flush()

        u2 = User(username='unique_test', role='viewer')
        u2.set_password('pass2')
        db_session.add(u2)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()


# ===========================================================================
# DATA INTEGRITY
# ===========================================================================

class TestDataIntegrity:
    """Verify model methods maintain data consistency."""

    def test_team_recalculate_points(self, db_session):
        t = make_tournament(db_session)
        team = make_team(db_session, t)
        c1 = make_college_competitor(db_session, t, team, 'A', 'M')
        c2 = make_college_competitor(db_session, t, team, 'B', 'F')
        c1.individual_points = 15
        c2.individual_points = 22
        db_session.flush()

        result = team.recalculate_points()
        assert result == 37
        assert team.total_points == 37

    def test_team_recalculate_ignores_scratched(self, db_session):
        t = make_tournament(db_session)
        team = make_team(db_session, t)
        c1 = make_college_competitor(db_session, t, team, 'Active', 'M')
        c2 = make_college_competitor(db_session, t, team, 'Gone', 'M',
                                      status='scratched')
        c1.individual_points = 10
        c2.individual_points = 50
        db_session.flush()

        team.recalculate_points()
        assert team.total_points == 10  # scratched excluded

    def test_team_is_valid_checks_gender_counts(self, db_session):
        t = make_tournament(db_session)
        team = make_team(db_session, t)
        # Only 1 male — should be invalid
        make_college_competitor(db_session, t, team, 'M1', 'M')
        make_college_competitor(db_session, t, team, 'F1', 'F')
        make_college_competitor(db_session, t, team, 'F2', 'F')
        db_session.flush()

        assert team.is_valid is False  # Need 2 males

    def test_team_is_valid_with_full_roster(self, db_session):
        t = make_tournament(db_session)
        team = make_team(db_session, t)
        make_college_competitor(db_session, t, team, 'M1', 'M')
        make_college_competitor(db_session, t, team, 'M2', 'M')
        make_college_competitor(db_session, t, team, 'F1', 'F')
        make_college_competitor(db_session, t, team, 'F2', 'F')
        db_session.flush()

        assert team.is_valid is True

    def test_team_validation_errors_round_trip(self, db_session):
        t = make_tournament(db_session)
        team = make_team(db_session, t)

        errors = [{'type': 'missing_gender', 'msg': 'Need 2 females'}]
        team.set_validation_errors(errors)
        db_session.flush()

        assert team.status == 'invalid'
        assert team.get_validation_errors() == errors

        # Clear errors
        team.set_validation_errors([])
        assert team.status == 'active'

    def test_event_result_unique_constraint_across_types(self, db_session):
        """Same competitor_id but different competitor_type should be allowed."""
        t = make_tournament(db_session)
        team = make_team(db_session, t)
        cc = make_college_competitor(db_session, t, team, 'Cross', 'M')
        pc = make_pro_competitor(db_session, t, 'Cross Pro', 'M')

        # Deliberately use the same competitor_id value but different types
        e = make_event(db_session, t, 'Cross Event', event_type='pro')
        from models.event import EventResult
        r1 = EventResult(event_id=e.id, competitor_id=cc.id,
                         competitor_type='college', competitor_name='Cross')
        r2 = EventResult(event_id=e.id, competitor_id=cc.id,
                         competitor_type='pro', competitor_name='Cross Pro')
        db_session.add_all([r1, r2])
        db_session.flush()  # Should not raise — different competitor_type

    def test_heat_sync_assignments(self, db_session):
        """Heat.sync_assignments rebuilds HeatAssignment rows from JSON."""
        from models.heat import HeatAssignment
        t = make_tournament(db_session)
        e = make_event(db_session, t, 'Sync Event', event_type='pro')
        h = make_heat(db_session, e, competitors=[10, 20, 30],
                      stand_assignments={'10': 1, '20': 2, '30': 3})
        db_session.flush()

        h.sync_assignments('pro')
        db_session.flush()

        rows = HeatAssignment.query.filter_by(heat_id=h.id).all()
        assert len(rows) == 3
        comp_ids = {r.competitor_id for r in rows}
        assert comp_ids == {10, 20, 30}

    def test_flight_heat_count(self, db_session):
        t = make_tournament(db_session)
        e = make_event(db_session, t, 'Flight Count', event_type='pro')
        f = make_flight(db_session, t)
        make_heat(db_session, e, heat_number=1, flight_id=f.id, flight_position=1)
        make_heat(db_session, e, heat_number=2, flight_id=f.id, flight_position=2)
        db_session.flush()

        assert f.heat_count == 2
        assert f.event_variety == 1  # same event
