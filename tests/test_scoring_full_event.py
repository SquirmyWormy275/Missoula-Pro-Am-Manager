"""
Tests for scoring all 14 pro events with realistic synthetic data.

Verifies that calculate_positions() produces correct position ordering,
handles DQs, highest_wins events, partnered events, CSV import with DQ
entries, distance format parsing, and tie handling.
"""
import csv
import io

import pytest

from tests.conftest import make_event, make_event_result, make_pro_competitor, make_tournament
from tests.fixtures.synthetic_data import PRO_COMPETITORS, PRO_SCORES

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_pro(name):
    """Look up a pro competitor dict by name from synthetic data."""
    for p in PRO_COMPETITORS:
        if p['name'] == name:
            return p
    raise ValueError(f"Pro competitor '{name}' not found in synthetic data")


def _seed_event_with_scores(db_session, tournament, event_name, scoring_type='time',
                            scoring_order='lowest_wins', stand_type='underhand',
                            is_partnered=False):
    """Create an event and its results from PRO_SCORES, return (event, results_list)."""
    event = make_event(
        db_session, tournament, event_name,
        event_type='pro',
        scoring_type=scoring_type,
        scoring_order=scoring_order,
        stand_type=stand_type,
        is_partnered=is_partnered,
    )

    scores = PRO_SCORES[event_name]
    results = []
    for entry in scores:
        name = entry[0]
        value = entry[1]
        status_raw = entry[2]
        partner = entry[3] if len(entry) > 3 else None

        pro_data = _find_pro(name)
        comp = make_pro_competitor(db_session, tournament, name, gender=pro_data['gender'])

        if status_raw == 'dq':
            result = make_event_result(
                db_session, event, comp,
                result_value=None,
                status='scratched',
                partner_name=partner,
            )
        else:
            result = make_event_result(
                db_session, event, comp,
                result_value=value,
                status='completed',
                partner_name=partner,
            )
        results.append(result)

    db_session.flush()
    return event, results


# ===========================================================================
# Test Classes
# ===========================================================================

class TestProScoringSpringboard:
    """Springboard: 6 competitors, time/lowest_wins. Verify positions 1-6."""

    def test_springboard_positions(self, db_session):
        from services.scoring_engine import calculate_positions

        tournament = make_tournament(db_session)
        event, results = _seed_event_with_scores(
            db_session, tournament, 'Springboard',
            scoring_type='time', scoring_order='lowest_wins',
            stand_type='springboard',
        )

        calculate_positions(event)
        db_session.flush()

        scored = sorted(
            [r for r in event.results.all() if r.status == 'completed'],
            key=lambda r: r.final_position,
        )

        expected_order = [
            ('Finn McCool', 80.0, 1),
            ('Imortal Joe', 94.0, 2),
            ('Alder Johns', 97.0, 3),
            ('Cosmo Cramer', 100.0, 4),
            ('Ben Cambium', 173.0, 5),
            ('Steptoe Edwall', 209.0, 6),
        ]

        assert len(scored) == 6
        for result, (exp_name, exp_val, exp_pos) in zip(scored, expected_order):
            assert result.competitor_name == exp_name
            assert result.result_value == exp_val
            assert result.final_position == exp_pos

    def test_int_springboard_positions(self, db_session):
        from services.scoring_engine import calculate_positions

        tournament = make_tournament(db_session)
        event, results = _seed_event_with_scores(
            db_session, tournament, 'Int 1-Board Springboard',
            scoring_type='time', scoring_order='lowest_wins',
            stand_type='springboard',
        )

        calculate_positions(event)
        db_session.flush()

        scored = sorted(
            [r for r in event.results.all() if r.status == 'completed'],
            key=lambda r: r.final_position,
        )

        expected_order = [
            ('Joe Manyfingers', 73.0, 1),
            ('Marshall Law', 162.0, 2),
            ('Dorian Gray', 189.0, 3),
            ('Wanda Fuca', 314.0, 4),
            ('Caligraphy Jones', 381.0, 5),
        ]

        assert len(scored) == 5
        for result, (exp_name, exp_val, exp_pos) in zip(scored, expected_order):
            assert result.competitor_name == exp_name
            assert result.result_value == exp_val
            assert result.final_position == exp_pos


class TestProScoringWithDQ:
    """Hot Saw (1 DQ) and Cookie Stack (8 DQs): verify DQ handling."""

    def test_hot_saw_dq_gets_no_position(self, db_session):
        from services.scoring_engine import calculate_positions

        tournament = make_tournament(db_session)
        event, results = _seed_event_with_scores(
            db_session, tournament, 'Hot Saw',
            scoring_type='time', scoring_order='lowest_wins',
            stand_type='hot_saw',
        )

        calculate_positions(event)
        db_session.flush()

        all_results = event.results.all()
        dqs = [r for r in all_results if r.status == 'scratched']
        completed = [r for r in all_results if r.status == 'completed']

        # One DQ: Carson Mitsubishi
        assert len(dqs) == 1
        assert dqs[0].competitor_name == 'Carson Mitsubishi'
        assert dqs[0].final_position is None

        # Non-DQ competitors are ordered correctly
        completed_sorted = sorted(completed, key=lambda r: r.final_position)
        expected = [
            ('Cosmo Cramer', 4.0, 1),
            ('Alder Johns', 5.0, 2),  # note: actually name says 'Alder Johns' not in hot saw
            ('Finn McCool', 7.0, 3),
            ('Steptoe Edwall', 8.0, 4),
        ]
        # Hot saw has competitors: Cosmo, Alder Johns, Finn, Steptoe, Carson(DQ)
        # Wait - let me check synthetic data: Alder Johns is NOT in Hot Saw events list
        # but PRO_SCORES['Hot Saw'] includes ('Alder Johns', 5.0, 'completed')
        # The score data is authoritative for these tests.

        assert len(completed_sorted) == 4
        for result, (exp_name, exp_val, exp_pos) in zip(completed_sorted, expected):
            assert result.competitor_name == exp_name
            assert result.result_value == exp_val
            assert result.final_position == exp_pos

    def test_cookie_stack_many_dqs(self, db_session):
        from services.scoring_engine import calculate_positions

        tournament = make_tournament(db_session)
        event, results = _seed_event_with_scores(
            db_session, tournament, 'Cookie Stack',
            scoring_type='time', scoring_order='lowest_wins',
            stand_type='cookie_stack',
        )

        calculate_positions(event)
        db_session.flush()

        all_results = event.results.all()
        dqs = [r for r in all_results if r.status == 'scratched']
        completed = [r for r in all_results if r.status == 'completed']

        # 8 DQs
        assert len(dqs) == 8
        for dq_result in dqs:
            assert dq_result.final_position is None

        # 6 completed results, ordered by time ascending
        completed_sorted = sorted(completed, key=lambda r: r.final_position)
        assert len(completed_sorted) == 6

        expected_names = ['Joe Manyfingers', 'Imortal Joe', 'Larry Occidentalis',
                          'Caligraphy Jones', 'Wanda Fuca', 'Dee John']
        for result, exp_name in zip(completed_sorted, expected_names):
            assert result.competitor_name == exp_name

        # Positions should be 1 through 6
        for i, result in enumerate(completed_sorted):
            assert result.final_position == i + 1


class TestProScoringHighestWins:
    """Partnered Axe Throw: score/highest_wins. Position 1 to highest score."""

    def test_axe_throw_highest_score_wins(self, db_session):
        from services.scoring_engine import calculate_positions

        tournament = make_tournament(db_session)
        event, results = _seed_event_with_scores(
            db_session, tournament, 'Partnered Axe Throw',
            scoring_type='score', scoring_order='highest_wins',
            stand_type='axe_throw', is_partnered=True,
        )

        calculate_positions(event)
        db_session.flush()

        completed = sorted(
            [r for r in event.results.all() if r.status == 'completed'],
            key=lambda r: r.final_position,
        )

        # Highest score (23.0) should be position 1
        assert completed[0].competitor_name == 'Cosmo Cramer'
        assert completed[0].result_value == 23.0
        assert completed[0].final_position == 1
        assert completed[0].partner_name == 'Finn McCool'

        # Verify full ordering (descending by score)
        expected = [
            ('Cosmo Cramer', 23.0, 1),
            ('Juicy Crust', 19.0, 2),
            ('Larry Occidentalis', 18.0, 3),
            ('Dee John', 17.0, 4),
            ('Cherry Strawberry', 14.0, 5),
        ]
        for result, (exp_name, exp_val, exp_pos) in zip(completed, expected):
            assert result.competitor_name == exp_name
            assert result.result_value == exp_val
            assert result.final_position == exp_pos


class TestProScoringPartneredEvents:
    """Double Buck and Jack & Jill: verify partnered entries score correctly."""

    def test_double_buck_partnered(self, db_session):
        from services.scoring_engine import calculate_positions

        tournament = make_tournament(db_session)
        event, results = _seed_event_with_scores(
            db_session, tournament, "Men's Double Buck",
            scoring_type='time', scoring_order='lowest_wins',
            stand_type='saw_hand', is_partnered=True,
        )

        calculate_positions(event)
        db_session.flush()

        completed = sorted(
            [r for r in event.results.all() if r.status == 'completed'],
            key=lambda r: r.final_position,
        )

        assert len(completed) == 5

        # Verify partner names are stored
        assert completed[0].competitor_name == 'Finn McCool'
        assert completed[0].partner_name == 'Cosmo Cramer'
        assert completed[0].result_value == 9.0
        assert completed[0].final_position == 1

        # Verify full ordering
        expected = [
            ('Finn McCool', 9.0, 1, 'Cosmo Cramer'),
            ('Meau Jeau', 10.0, 2, 'Jonathon Wept'),
            ('Imortal Joe', 11.0, 3, 'Joe Manyfingers'),
            ('Carson Mitsubishi', 12.0, 4, 'Marshall Law'),
            ('Garfield Heathcliff', 17.0, 5, 'Dorian Gray'),
        ]
        for result, (name, val, pos, partner) in zip(completed, expected):
            assert result.competitor_name == name
            assert result.result_value == val
            assert result.final_position == pos
            assert result.partner_name == partner

    def test_jack_and_jill_partnered(self, db_session):
        from services.scoring_engine import calculate_positions

        tournament = make_tournament(db_session)
        event, results = _seed_event_with_scores(
            db_session, tournament, 'Jack & Jill',
            scoring_type='time', scoring_order='lowest_wins',
            stand_type='saw_hand', is_partnered=True,
        )

        calculate_positions(event)
        db_session.flush()

        completed = sorted(
            [r for r in event.results.all() if r.status == 'completed'],
            key=lambda r: r.final_position,
        )

        assert len(completed) == 7

        # First place
        assert completed[0].competitor_name == 'Salix Amygdaloides'
        assert completed[0].partner_name == 'Meau Jeau'
        assert completed[0].result_value == 10.0
        assert completed[0].final_position == 1


class TestCsvImportWithDQ:
    """Test import_results_from_csv() with CSV text containing DQ entries."""

    def test_csv_import_dq_entries(self, db_session):
        from services.scoring_engine import import_results_from_csv

        tournament = make_tournament(db_session)
        event = make_event(
            db_session, tournament, 'Hot Saw Test',
            event_type='pro', scoring_type='time', scoring_order='lowest_wins',
            stand_type='hot_saw',
        )

        # Create competitors in the tournament AND enroll them in this
        # event — the CSV importer now requires the competitor to be
        # entered in the event before accepting a result for them.
        db_session.flush()  # ensure event.id is populated
        comp1 = make_pro_competitor(db_session, tournament, 'Alice Smith', gender='F', events=[event.id])
        comp2 = make_pro_competitor(db_session, tournament, 'Bob Jones', gender='M', events=[event.id])
        comp3 = make_pro_competitor(db_session, tournament, 'Charlie Brown', gender='M', events=[event.id])
        db_session.flush()

        csv_text = """competitor_name,result,status
Alice Smith,4.5,
Bob Jones,DQ,
Charlie Brown,7.2,
"""

        result = import_results_from_csv(event, csv_text)

        assert result['imported'] == 3
        assert result['skipped'] == 0
        assert result['errors'] == []

        all_results = event.results.all()
        assert len(all_results) == 3

        by_name = {r.competitor_name: r for r in all_results}

        # Alice: completed
        assert by_name['Alice Smith'].result_value == 4.5
        assert by_name['Alice Smith'].status == 'completed'

        # Bob: DQ
        assert by_name['Bob Jones'].result_value is None
        assert by_name['Bob Jones'].status == 'scratched'

        # Charlie: completed
        assert by_name['Charlie Brown'].result_value == 7.2
        assert by_name['Charlie Brown'].status == 'completed'

    def test_csv_import_dns_dnf(self, db_session):
        from services.scoring_engine import import_results_from_csv

        tournament = make_tournament(db_session)
        event = make_event(
            db_session, tournament, 'Speed Test',
            event_type='pro', scoring_type='time', scoring_order='lowest_wins',
        )

        db_session.flush()
        comp1 = make_pro_competitor(db_session, tournament, 'Runner One', gender='M', events=[event.id])
        comp2 = make_pro_competitor(db_session, tournament, 'Runner Two', gender='M', events=[event.id])
        db_session.flush()

        csv_text = """competitor_name,result
Runner One,DNS
Runner Two,DNF
"""

        result = import_results_from_csv(event, csv_text)
        assert result['imported'] == 2

        all_results = event.results.all()
        by_name = {r.competitor_name: r for r in all_results}

        assert by_name['Runner One'].status == 'dnf'
        assert by_name['Runner One'].result_value is None
        assert by_name['Runner Two'].status == 'dnf'
        assert by_name['Runner Two'].result_value is None


class TestCsvImportDistanceFormat:
    """Test _parse_result_value() with various formats."""

    def test_feet_inches(self):
        from services.scoring_engine import _parse_result_value

        # 23'3" = 23*12 + 3 = 279 inches
        assert _parse_result_value("23'3\"") == 279.0

    def test_feet_only(self):
        from services.scoring_engine import _parse_result_value

        # 23' = 23*12 = 276 inches
        assert _parse_result_value("23'") == 276.0

    def test_minutes_seconds(self):
        from services.scoring_engine import _parse_result_value

        # 2:30.5 = 2*60 + 30.5 = 150.5 seconds
        assert _parse_result_value('2:30.5') == 150.5

    def test_plain_number(self):
        from services.scoring_engine import _parse_result_value

        assert _parse_result_value('28.0') == 28.0
        assert _parse_result_value('94') == 94.0

    def test_empty_raises(self):
        from services.scoring_engine import _parse_result_value

        with pytest.raises(ValueError):
            _parse_result_value('')

    def test_feet_inches_with_spaces(self):
        from services.scoring_engine import _parse_result_value

        # 23' 3 (no closing quote)
        assert _parse_result_value("23' 3") == 279.0

    def test_minutes_seconds_whole(self):
        from services.scoring_engine import _parse_result_value

        # 1:00 = 60 seconds
        assert _parse_result_value('1:00') == 60.0


class TestScoringTies:
    """Two competitors with the same time get the same position."""

    def test_tied_competitors_same_position(self, db_session):
        from services.scoring_engine import calculate_positions

        tournament = make_tournament(db_session)
        event = make_event(
            db_session, tournament, 'Tie Test Event',
            event_type='pro', scoring_type='time', scoring_order='lowest_wins',
            stand_type='underhand',
        )

        comp_a = make_pro_competitor(db_session, tournament, 'Competitor A', gender='M')
        comp_b = make_pro_competitor(db_session, tournament, 'Competitor B', gender='M')
        comp_c = make_pro_competitor(db_session, tournament, 'Competitor C', gender='M')

        make_event_result(db_session, event, comp_a, result_value=25.0, status='completed')
        make_event_result(db_session, event, comp_b, result_value=25.0, status='completed')
        make_event_result(db_session, event, comp_c, result_value=30.0, status='completed')

        db_session.flush()

        calculate_positions(event)
        db_session.flush()

        results = {r.competitor_name: r for r in event.results.all()}

        # A and B should both be position 1
        assert results['Competitor A'].final_position == 1
        assert results['Competitor B'].final_position == 1
        # C should be position 3 (not 2, since positions 1 and 1 are taken)
        assert results['Competitor C'].final_position == 3

    def test_tied_highest_wins(self, db_session):
        from services.scoring_engine import calculate_positions

        tournament = make_tournament(db_session)
        event = make_event(
            db_session, tournament, 'Score Tie Event',
            event_type='pro', scoring_type='score', scoring_order='highest_wins',
            stand_type='axe_throw',
        )

        comp_a = make_pro_competitor(db_session, tournament, 'Thrower A', gender='M')
        comp_b = make_pro_competitor(db_session, tournament, 'Thrower B', gender='M')
        comp_c = make_pro_competitor(db_session, tournament, 'Thrower C', gender='M')

        make_event_result(db_session, event, comp_a, result_value=50.0, status='completed')
        make_event_result(db_session, event, comp_b, result_value=50.0, status='completed')
        make_event_result(db_session, event, comp_c, result_value=40.0, status='completed')

        db_session.flush()

        calculate_positions(event)
        db_session.flush()

        results = {r.competitor_name: r for r in event.results.all()}

        # A and B tied for 1st
        assert results['Thrower A'].final_position == 1
        assert results['Thrower B'].final_position == 1
        # C should be position 3
        assert results['Thrower C'].final_position == 3
