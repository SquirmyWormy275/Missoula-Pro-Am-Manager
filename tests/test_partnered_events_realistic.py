"""
Tests for partnered event scoring with realistic data from synthetic_data.py.

Covers Double Buck, Jack & Jill, Partnered Axe Throw (pro), and college
Double Buck F with DQ handling.
"""
import json
import os
import pytest

os.environ.setdefault('SECRET_KEY', 'test-secret-partnered')
os.environ.setdefault('WTF_CSRF_ENABLED', 'False')

from database import db as _db
from tests.conftest import (
    make_tournament, make_pro_competitor, make_event, make_event_result,
    make_team, make_college_competitor,
)


# ---------------------------------------------------------------------------
# Fixtures
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


@pytest.fixture()
def db_session(app):
    from database import db
    with app.app_context():
        db.session.begin_nested()
        yield db.session
        db.session.rollback()


# ============================================================================
# 1. Men's Double Buck — time / lowest_wins, 5 pro pairs
# ============================================================================

class TestDoubleBuckScoring:
    """Score 5 pro Double Buck teams and verify positions."""

    # From PRO_SCORES["Men's Double Buck"]:
    #   Finn McCool & Cosmo Cramer   9.0s  -> 1st
    #   Meau Jeau & Jonathon Wept   10.0s  -> 2nd
    #   Imortal Joe & Joe Manyfingers 11.0s -> 3rd
    #   Carson Mitsubishi & Marshall Law 12.0s -> 4th
    #   Garfield Heathcliff & Dorian Gray 17.0s -> 5th

    PAIRS = [
        ('Finn McCool', 'Cosmo Cramer', 9.0),
        ('Meau Jeau', 'Jonathon Wept', 10.0),
        ('Imortal Joe', 'Joe Manyfingers', 11.0),
        ('Carson Mitsubishi', 'Marshall Law', 12.0),
        ('Garfield Heathcliff', 'Dorian Gray', 17.0),
    ]

    def _build(self, db_session):
        from services.scoring_engine import calculate_positions
        t = make_tournament(db_session, name='DB Test')
        event = make_event(
            db_session, t, name="Men's Double Buck",
            event_type='pro', scoring_type='time',
            scoring_order='lowest_wins', stand_type='saw_hand',
            is_partnered=True,
        )
        results = []
        for name, partner, time_val in self.PAIRS:
            comp = make_pro_competitor(db_session, t, name=name, gender='M')
            r = make_event_result(
                db_session, event, comp,
                result_value=time_val, status='completed',
                partner_name=partner,
            )
            results.append(r)
        db_session.flush()
        calculate_positions(event)
        db_session.flush()
        return event, results

    def test_first_place(self, db_session):
        event, results = self._build(db_session)
        r = [r for r in results if r.competitor_name == 'Finn McCool'][0]
        assert r.final_position == 1

    def test_second_place(self, db_session):
        event, results = self._build(db_session)
        r = [r for r in results if r.competitor_name == 'Meau Jeau'][0]
        assert r.final_position == 2

    def test_positions_sequential(self, db_session):
        event, results = self._build(db_session)
        positions = sorted(r.final_position for r in results)
        assert positions == [1, 2, 3, 4, 5]

    def test_last_place_is_slowest(self, db_session):
        event, results = self._build(db_session)
        r = [r for r in results if r.competitor_name == 'Garfield Heathcliff'][0]
        assert r.final_position == 5

    def test_event_is_finalized(self, db_session):
        event, _ = self._build(db_session)
        assert event.is_finalized is True


# ============================================================================
# 2. Jack & Jill — time / lowest_wins, 7 mixed-gender pro pairs
# ============================================================================

class TestJackAndJillScoring:
    """Score 7 Jack & Jill pairs (mixed gender) and verify order."""

    # From PRO_SCORES["Jack & Jill"]:
    #   Salix Amygdaloides & Meau Jeau        10.0  -> 1st
    #   Caligraphy Jones & Alder Johns         11.0  -> 2nd
    #   Olive Oyle & Finn McCool               12.0  -> 3rd
    #   Wanda Fuca & Joe Manyfingers           14.0  -> 4th
    #   Cherry Strawberry & Larry Occidentalis 19.0  -> 5th
    #   Juicy Crust & Garfield Heathcliff      23.0  -> 6th
    #   Ameriga Vespucci & Dorian Gray         25.0  -> 7th

    PAIRS = [
        ('Salix Amygdaloides', 'F', 'Meau Jeau', 10.0),
        ('Caligraphy Jones', 'F', 'Alder Johns', 11.0),
        ('Olive Oyle', 'F', 'Finn McCool', 12.0),
        ('Wanda Fuca', 'F', 'Joe Manyfingers', 14.0),
        ('Cherry Strawberry', 'F', 'Larry Occidentalis', 19.0),
        ('Juicy Crust', 'F', 'Garfield Heathcliff', 23.0),
        ('Ameriga Vespucci', 'F', 'Dorian Gray', 25.0),
    ]

    def _build(self, db_session):
        from services.scoring_engine import calculate_positions
        t = make_tournament(db_session, name='JJ Test')
        event = make_event(
            db_session, t, name='Jack & Jill',
            event_type='pro', scoring_type='time',
            scoring_order='lowest_wins', stand_type='saw_hand',
            is_partnered=True,
        )
        results = []
        for name, gender, partner, time_val in self.PAIRS:
            comp = make_pro_competitor(db_session, t, name=name, gender=gender)
            r = make_event_result(
                db_session, event, comp,
                result_value=time_val, status='completed',
                partner_name=partner,
            )
            results.append(r)
        db_session.flush()
        calculate_positions(event)
        db_session.flush()
        return event, results

    def test_first_place_is_fastest(self, db_session):
        _, results = self._build(db_session)
        first = [r for r in results if r.final_position == 1][0]
        assert first.competitor_name == 'Salix Amygdaloides'

    def test_last_place_is_slowest(self, db_session):
        _, results = self._build(db_session)
        last = [r for r in results if r.final_position == 7][0]
        assert last.competitor_name == 'Ameriga Vespucci'

    def test_all_seven_positions(self, db_session):
        _, results = self._build(db_session)
        positions = sorted(r.final_position for r in results)
        assert positions == [1, 2, 3, 4, 5, 6, 7]

    def test_scoring_order_monotonic(self, db_session):
        _, results = self._build(db_session)
        by_pos = sorted(results, key=lambda r: r.final_position)
        times = [r.result_value for r in by_pos]
        assert times == sorted(times), 'Times should be monotonically increasing'

    def test_partner_names_preserved(self, db_session):
        _, results = self._build(db_session)
        partner_map = {r.competitor_name: r.partner_name for r in results}
        assert partner_map['Olive Oyle'] == 'Finn McCool'
        assert partner_map['Caligraphy Jones'] == 'Alder Johns'


# ============================================================================
# 3. Partnered Axe Throw — score / highest_wins, 5 pairs
# ============================================================================

class TestPartneredAxeThrowScoring:
    """Verify highest_wins scoring for Partnered Axe Throw."""

    # From PRO_SCORES["Partnered Axe Throw"]:
    #   Cosmo Cramer & Finn McCool            23.0  -> 1st (highest)
    #   Juicy Crust & Garfield Heathcliff     19.0  -> 2nd
    #   Larry Occidentalis & Steptoe Edwall   18.0  -> 3rd
    #   Dee John & Carson Mitsubishi          17.0  -> 4th
    #   Cherry Strawberry & Epinephrine Needel 14.0 -> 5th (lowest)

    PAIRS = [
        ('Cosmo Cramer', 'Finn McCool', 23.0),
        ('Juicy Crust', 'Garfield Heathcliff', 19.0),
        ('Larry Occidentalis', 'Steptoe Edwall', 18.0),
        ('Dee John', 'Carson Mitsubishi', 17.0),
        ('Cherry Strawberry', 'Epinephrine Needel', 14.0),
    ]

    def _build(self, db_session):
        from services.scoring_engine import calculate_positions
        t = make_tournament(db_session, name='PAT Test')
        event = make_event(
            db_session, t, name='Partnered Axe Throw',
            event_type='pro', scoring_type='score',
            scoring_order='highest_wins', stand_type='axe_throw',
            is_partnered=True,
        )
        results = []
        for name, partner, score_val in self.PAIRS:
            comp = make_pro_competitor(db_session, t, name=name)
            r = make_event_result(
                db_session, event, comp,
                result_value=score_val, status='completed',
                partner_name=partner,
            )
            results.append(r)
        db_session.flush()
        calculate_positions(event)
        db_session.flush()
        return event, results

    def test_highest_score_first(self, db_session):
        _, results = self._build(db_session)
        first = [r for r in results if r.final_position == 1][0]
        assert first.competitor_name == 'Cosmo Cramer'
        assert first.result_value == 23.0

    def test_lowest_score_last(self, db_session):
        _, results = self._build(db_session)
        last = [r for r in results if r.final_position == 5][0]
        assert last.competitor_name == 'Cherry Strawberry'
        assert last.result_value == 14.0

    def test_positions_descending_by_score(self, db_session):
        _, results = self._build(db_session)
        by_pos = sorted(results, key=lambda r: r.final_position)
        scores = [r.result_value for r in by_pos]
        assert scores == sorted(scores, reverse=True)

    def test_partner_on_first_place(self, db_session):
        _, results = self._build(db_session)
        first = [r for r in results if r.final_position == 1][0]
        assert first.partner_name == 'Finn McCool'

    def test_event_finalized(self, db_session):
        event, _ = self._build(db_session)
        assert event.is_finalized is True
        assert event.status == 'completed'


# ============================================================================
# 4. Partner name preservation after calculate_positions()
# ============================================================================

class TestPartnerNameOnResult:
    """Ensure partner_name survives scoring recalculation."""

    def test_partner_name_preserved_after_scoring(self, db_session):
        from services.scoring_engine import calculate_positions
        t = make_tournament(db_session, name='PN Test')
        event = make_event(
            db_session, t, name='Test Partnered',
            event_type='pro', scoring_type='time',
            scoring_order='lowest_wins', stand_type='saw_hand',
            is_partnered=True,
        )
        comp = make_pro_competitor(db_session, t, name='Alice')
        r = make_event_result(
            db_session, event, comp,
            result_value=15.0, status='completed',
            partner_name='Bob',
        )
        db_session.flush()
        calculate_positions(event)
        db_session.flush()
        assert r.partner_name == 'Bob'

    def test_partner_name_none_when_not_set(self, db_session):
        from services.scoring_engine import calculate_positions
        t = make_tournament(db_session, name='PN2 Test')
        event = make_event(
            db_session, t, name='Solo Event',
            event_type='pro', scoring_type='time',
            scoring_order='lowest_wins', stand_type='underhand',
        )
        comp = make_pro_competitor(db_session, t, name='Solo Sam')
        r = make_event_result(
            db_session, event, comp,
            result_value=20.0, status='completed',
        )
        db_session.flush()
        calculate_positions(event)
        db_session.flush()
        assert r.partner_name is None

    def test_multiple_partners_all_preserved(self, db_session):
        from services.scoring_engine import calculate_positions
        t = make_tournament(db_session, name='PN3 Test')
        event = make_event(
            db_session, t, name='Multi Partner',
            event_type='pro', scoring_type='time',
            scoring_order='lowest_wins', stand_type='saw_hand',
            is_partnered=True,
        )
        pairs = [('A', 'PartnerA', 10.0), ('B', 'PartnerB', 20.0), ('C', 'PartnerC', 30.0)]
        results = []
        for name, partner, val in pairs:
            comp = make_pro_competitor(db_session, t, name=name)
            r = make_event_result(
                db_session, event, comp,
                result_value=val, status='completed',
                partner_name=partner,
            )
            results.append(r)
        db_session.flush()
        calculate_positions(event)
        db_session.flush()
        for r, (_, partner, _) in zip(results, pairs):
            assert r.partner_name == partner


# ============================================================================
# 5. College Double Buck F — 6 teams, one DQ pair, team points
# ============================================================================

class TestCollegePartneredScoring:
    """College Double Buck F with DQ pair placed last and team points."""

    # From COLLEGE_SCORES["Double Buck F"]:
    #   Green Grass (TCU-A) & Abigail Crease    74.0  -> 1st  10pts
    #   Jo March (CCU-A) & Polisa Wurst          79.0  -> 2nd   7pts
    #   Kum Pon Nent (CMC-A) & Maxine Stonk     117.0  -> 3rd   5pts
    #   Calamari Nodules (CMC-C) & Rose Mary    148.0  -> 4th   3pts
    #   Helen Oftroy (CMC-B) & Carmen Sandiego  150.0  -> 5th   2pts
    #   Ben Wise (JT-A) & Gronald Grop          DQ     -> 6th   1pt

    ENTRIES = [
        ('Green Grass', 'TCU-A', 'TCU', 'Abigail Crease', 74.0, 'completed'),
        ('Jo March', 'CCU-A', 'CCU', 'Polisa Wurst', 79.0, 'completed'),
        ('Kum Pon Nent', 'CMC-A', 'CMC', 'Maxine Stonk', 117.0, 'completed'),
        ('Calamari Nodules', 'CMC-C', 'CMC', 'Rose Mary', 148.0, 'completed'),
        ('Helen Oftroy', 'CMC-B', 'CMC', 'Carmen Sandiego', 150.0, 'completed'),
        ('Ben Wise', 'JT-A', 'JT', 'Gronald Grop', None, 'scratched'),
    ]

    def _build(self, db_session):
        from services.scoring_engine import calculate_positions
        t = make_tournament(db_session, name='CDB Test')
        event = make_event(
            db_session, t, name='Double Buck F',
            event_type='college', gender='F',
            scoring_type='time', scoring_order='lowest_wins',
            stand_type='saw_hand', is_partnered=True,
        )
        teams_cache = {}
        results = []
        for name, team_code, abbrev, partner, time_val, status in self.ENTRIES:
            if team_code not in teams_cache:
                teams_cache[team_code] = make_team(
                    db_session, t, code=team_code,
                    school=f'{abbrev} School', abbrev=abbrev,
                )
            team = teams_cache[team_code]
            comp = make_college_competitor(
                db_session, t, team, name=name, gender='F',
            )
            r = make_event_result(
                db_session, event, comp, competitor_type='college',
                result_value=time_val, status=status,
                partner_name=partner,
            )
            results.append(r)
        db_session.flush()
        calculate_positions(event)
        db_session.flush()
        return event, results

    def test_first_place(self, db_session):
        _, results = self._build(db_session)
        completed = [r for r in results if r.status == 'completed']
        first = [r for r in completed if r.final_position == 1]
        assert len(first) == 1
        assert first[0].competitor_name == 'Green Grass'

    def test_dq_pair_not_placed(self, db_session):
        """DQ/scratched pair should not receive a position (only completed results ranked)."""
        _, results = self._build(db_session)
        dq = [r for r in results if r.competitor_name == 'Ben Wise'][0]
        # Scratched results are excluded from completed list, so no position
        assert dq.final_position is None

    def test_completed_positions_correct(self, db_session):
        _, results = self._build(db_session)
        completed = [r for r in results if r.status == 'completed']
        by_pos = sorted(completed, key=lambda r: r.final_position)
        expected_names = ['Green Grass', 'Jo March', 'Kum Pon Nent',
                          'Calamari Nodules', 'Helen Oftroy']
        actual_names = [r.competitor_name for r in by_pos]
        assert actual_names == expected_names

    def test_first_place_gets_10_points(self, db_session):
        _, results = self._build(db_session)
        first = [r for r in results if r.competitor_name == 'Green Grass'][0]
        assert first.points_awarded == 10

    def test_fifth_place_gets_2_points(self, db_session):
        _, results = self._build(db_session)
        fifth = [r for r in results if r.competitor_name == 'Helen Oftroy'][0]
        assert fifth.points_awarded == 2

    def test_dq_gets_zero_points(self, db_session):
        _, results = self._build(db_session)
        dq = [r for r in results if r.competitor_name == 'Ben Wise'][0]
        assert dq.points_awarded == 0

    def test_partner_names_on_college_results(self, db_session):
        _, results = self._build(db_session)
        partner_map = {r.competitor_name: r.partner_name for r in results}
        assert partner_map['Green Grass'] == 'Abigail Crease'
        assert partner_map['Ben Wise'] == 'Gronald Grop'
