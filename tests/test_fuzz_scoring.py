"""
Fuzz and edge-case tests for the scoring engine and gear sharing parser.

Uses pytest.mark.parametrize for broad coverage without external libraries.
"""
import json
import os
import pytest

os.environ.setdefault('SECRET_KEY', 'test-secret-fuzz')
os.environ.setdefault('WTF_CSRF_ENABLED', 'False')

from tests.conftest import (
    make_tournament, make_pro_competitor, make_event, make_event_result,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def app():
    from tests.db_test_utils import create_test_app
    from database import db
    _app, db_path = create_test_app()

    with _app.app_context():
        yield _app
        db.session.remove()
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
# 1. _parse_result_value fuzz
# ============================================================================

class TestParseResultValueFuzz:
    """Fuzz _parse_result_value with edge cases and format variations."""

    def _parse(self, raw):
        from services.scoring_engine import _parse_result_value
        return _parse_result_value(raw)

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            self._parse('')

    def test_none_raises(self):
        with pytest.raises(ValueError):
            self._parse(None)

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError):
            self._parse('   ')

    @pytest.mark.parametrize('val', [-1.0, -100, -0.001])
    def test_negative_numbers(self, val):
        result = self._parse(str(val))
        assert result == float(val)

    @pytest.mark.parametrize('val', [999999.99, 1e6, 1e10])
    def test_very_large_numbers(self, val):
        result = self._parse(str(val))
        assert result == float(val)

    def test_zero(self):
        assert self._parse('0') == 0.0

    def test_zero_point_zero(self):
        assert self._parse('0.0') == 0.0

    @pytest.mark.parametrize('dq_str', ['DQ', 'dq', 'DNS', 'DNF', 'DSQ', 'DISQUALIFIED', 'abc', 'N/A', '--'])
    def test_dq_like_strings_raise(self, dq_str):
        with pytest.raises((ValueError, TypeError)):
            self._parse(dq_str)

    # Feet/inches variations
    def test_feet_inches_standard(self):
        # 23'3" = 23*12 + 3 = 279
        result = self._parse("23'3\"")
        assert result == 279.0

    def test_feet_inches_with_space(self):
        result = self._parse("23' 3")
        assert result == pytest.approx(279.0)

    def test_feet_only(self):
        # 23' = 23*12 = 276
        result = self._parse("23'")
        assert result == 276.0

    def test_feet_inches_decimal(self):
        # 10'6.5" = 10*12 + 6.5 = 126.5
        result = self._parse("10'6.5\"")
        assert result == pytest.approx(126.5)

    # Time format
    def test_time_minutes_seconds(self):
        # 1:30 = 90
        result = self._parse('1:30')
        assert result == 90.0

    def test_time_zero_minutes(self):
        # 0:45.5 = 45.5
        result = self._parse('0:45.5')
        assert result == pytest.approx(45.5)

    def test_time_large(self):
        # 5:00 = 300
        result = self._parse('5:00')
        assert result == 300.0

    def test_plain_integer(self):
        assert self._parse('42') == 42.0

    def test_plain_float(self):
        assert self._parse('28.5') == 28.5

    def test_leading_trailing_whitespace(self):
        assert self._parse('  42.0  ') == 42.0


# ============================================================================
# 2. Scoring engine edge cases
# ============================================================================

class TestScoringEngineEdgeCases:
    """Edge cases for calculate_positions."""

    def test_all_dq_event(self, db_session):
        """When all competitors are DQ, no positions assigned and event not finalized."""
        from services.scoring_engine import calculate_positions
        t = make_tournament(db_session, name='AllDQ')
        event = make_event(db_session, t, name='AllDQ Event', event_type='pro',
                           scoring_type='time', scoring_order='lowest_wins')
        for i in range(3):
            comp = make_pro_competitor(db_session, t, name=f'DQComp{i}')
            make_event_result(db_session, event, comp, result_value=None, status='scratched')
        db_session.flush()
        calculate_positions(event)
        db_session.flush()
        assert event.is_finalized is False
        for r in event.results.all():
            assert r.final_position is None

    def test_single_competitor(self, db_session):
        """Single competitor gets position 1."""
        from services.scoring_engine import calculate_positions
        t = make_tournament(db_session, name='Solo')
        event = make_event(db_session, t, name='Solo Event', event_type='pro',
                           scoring_type='time', scoring_order='lowest_wins')
        comp = make_pro_competitor(db_session, t, name='Only One')
        make_event_result(db_session, event, comp, result_value=50.0, status='completed')
        db_session.flush()
        calculate_positions(event)
        db_session.flush()
        results = event.results.all()
        assert len(results) == 1
        assert results[0].final_position == 1
        assert event.is_finalized is True

    def test_100_competitors(self, db_session):
        """Generate 100 competitors and verify all get unique sequential positions."""
        from services.scoring_engine import calculate_positions
        t = make_tournament(db_session, name='Big100')
        event = make_event(db_session, t, name='Big Event', event_type='pro',
                           scoring_type='time', scoring_order='lowest_wins')
        for i in range(100):
            comp = make_pro_competitor(db_session, t, name=f'Comp_{i:03d}')
            # Each competitor gets a unique time: 10.0 + i*0.5
            make_event_result(db_session, event, comp,
                              result_value=10.0 + i * 0.5, status='completed')
        db_session.flush()
        calculate_positions(event)
        db_session.flush()
        results = event.results.all()
        positions = sorted(r.final_position for r in results)
        assert positions == list(range(1, 101))

    def test_all_tied_lowest_wins(self, db_session):
        """When all competitors tie, all share position 1."""
        from services.scoring_engine import calculate_positions
        t = make_tournament(db_session, name='AllTied')
        event = make_event(db_session, t, name='Tied Event', event_type='pro',
                           scoring_type='time', scoring_order='lowest_wins')
        for i in range(5):
            comp = make_pro_competitor(db_session, t, name=f'Tied_{i}')
            make_event_result(db_session, event, comp,
                              result_value=25.0, status='completed')
        db_session.flush()
        calculate_positions(event)
        db_session.flush()
        for r in event.results.all():
            assert r.final_position == 1

    def test_all_tied_highest_wins(self, db_session):
        """Highest_wins tie: all share position 1."""
        from services.scoring_engine import calculate_positions
        t = make_tournament(db_session, name='AllTiedHW')
        event = make_event(db_session, t, name='Tied HW', event_type='pro',
                           scoring_type='score', scoring_order='highest_wins')
        for i in range(4):
            comp = make_pro_competitor(db_session, t, name=f'TiedHW_{i}')
            make_event_result(db_session, event, comp,
                              result_value=100.0, status='completed')
        db_session.flush()
        calculate_positions(event)
        db_session.flush()
        for r in event.results.all():
            assert r.final_position == 1

    def test_mix_completed_and_dq(self, db_session):
        """Only completed results get positions; DQ results are skipped."""
        from services.scoring_engine import calculate_positions
        t = make_tournament(db_session, name='MixDQ')
        event = make_event(db_session, t, name='Mix DQ', event_type='pro',
                           scoring_type='time', scoring_order='lowest_wins')

        comp1 = make_pro_competitor(db_session, t, name='Good1')
        make_event_result(db_session, event, comp1, result_value=10.0, status='completed')
        comp2 = make_pro_competitor(db_session, t, name='Good2')
        make_event_result(db_session, event, comp2, result_value=20.0, status='completed')
        comp3 = make_pro_competitor(db_session, t, name='DQComp')
        make_event_result(db_session, event, comp3, result_value=None, status='scratched')

        db_session.flush()
        calculate_positions(event)
        db_session.flush()

        results = {r.competitor_name: r for r in event.results.all()}
        assert results['Good1'].final_position == 1
        assert results['Good2'].final_position == 2
        assert results['DQComp'].final_position is None


# ============================================================================
# 3. Gear sharing parser fuzz
# ============================================================================

class TestGearSharingParserFuzz:
    """Fuzz parse_gear_sharing_details with unusual inputs."""

    def _parse(self, text, event_pool=None, name_index=None, self_name='', entered=None):
        from services.gear_sharing import parse_gear_sharing_details
        return parse_gear_sharing_details(
            text,
            event_pool=event_pool or [],
            name_index=name_index or {},
            self_name=self_name,
            entered_event_names=entered,
        )

    def test_empty_string(self):
        gear_map, warnings = self._parse('')
        assert gear_map == {}
        assert 'missing_details' in warnings

    def test_none_input(self):
        gear_map, warnings = self._parse(None)
        assert gear_map == {}
        assert 'missing_details' in warnings

    def test_whitespace_only(self):
        gear_map, warnings = self._parse('   \t\n  ')
        assert gear_map == {}
        assert 'missing_details' in warnings

    def test_very_long_string(self):
        """500 chars of nonsense should not crash."""
        text = 'x' * 500
        gear_map, warnings = self._parse(text)
        # No known names -> partner_not_resolved
        assert 'partner_not_resolved' in warnings

    def test_no_known_names(self):
        gear_map, warnings = self._parse(
            'Some random text that matches nobody',
            name_index={'johndoe': 'John Doe', 'janesmith': 'Jane Smith'},
        )
        # Parser may return partner_not_resolved or events_not_resolved
        assert len(warnings) > 0

    def test_event_codes_only_no_partner(self):
        """Event codes without a recognizable name -> partner not resolved."""
        gear_map, warnings = self._parse('SB, UH')
        assert 'partner_not_resolved' in warnings

    @pytest.mark.parametrize('text', [
        '!@#$%^&*()',
        '<script>alert("xss")</script>',
        'DROP TABLE events;--',
        '""" triple quotes """',
        "name's apostrophe",
    ])
    def test_special_characters(self, text):
        """Special characters should not crash the parser."""
        gear_map, warnings = self._parse(text)
        assert isinstance(gear_map, dict)
        assert isinstance(warnings, list)

    @pytest.mark.parametrize('text', [
        '\u00e9\u00e8\u00ea',  # accented e variants
        '\u4e2d\u6587',        # Chinese characters
        '\U0001f332',          # tree emoji
    ])
    def test_unicode(self, text):
        """Unicode input should not crash the parser."""
        gear_map, warnings = self._parse(text)
        assert isinstance(gear_map, dict)
        assert isinstance(warnings, list)

    def test_known_partner_resolved(self):
        """When a known name is in the text, partner should be resolved."""
        name_index = {'johndoe': 'John Doe', 'janesmith': 'Jane Smith'}
        gear_map, warnings = self._parse(
            'John Doe - UH, SB',
            name_index=name_index,
            self_name='Jane Smith',
        )
        # Partner should be resolved (no partner_not_resolved warning)
        assert 'partner_not_resolved' not in warnings


# ============================================================================
# 4. normalize_person_name fuzz
# ============================================================================

class TestNormalizePersonNameFuzz:
    """Fuzz normalize_person_name with edge cases."""

    def _norm(self, value):
        from services.gear_sharing import normalize_person_name
        return normalize_person_name(value)

    def test_none(self):
        assert self._norm(None) == ''

    def test_empty_string(self):
        assert self._norm('') == ''

    def test_whitespace_only(self):
        assert self._norm('   \t  ') == ''

    def test_numbers_only(self):
        result = self._norm('12345')
        assert result == '12345'

    def test_standard_name(self):
        assert self._norm('John Doe') == 'johndoe'

    def test_unicode_accents(self):
        # Accented characters should be preserved (lowercased)
        result = self._norm('Jos\u00e9 Garc\u00eda')
        # normalize_person_name strips non-alphanumeric, but accented chars
        # are not [a-z0-9], so they get stripped
        assert 'jos' in result

    def test_very_long_string(self):
        long_name = 'A' * 1000
        result = self._norm(long_name)
        assert len(result) == 1000
        assert result == 'a' * 1000

    def test_all_special_chars(self):
        assert self._norm('!@#$%^&*()') == ''

    def test_mixed_case(self):
        assert self._norm('JoHn DoE') == 'johndoe'

    def test_hyphenated_name(self):
        # Hyphens stripped, letters kept
        assert self._norm('Mary-Jane Watson') == 'maryjanewatson'

    def test_numeric_in_name(self):
        assert self._norm('Player1') == 'player1'

    @pytest.mark.parametrize('value', [0, 123, 45.6, True, False])
    def test_non_string_types(self, value):
        """Non-string inputs should be coerced via str()."""
        result = self._norm(value)
        assert isinstance(result, str)


# ============================================================================
# 5. infer_equipment_categories fuzz
# ============================================================================

class TestInferEquipmentCategoriesFuzz:
    """Fuzz infer_equipment_categories with various inputs."""

    def _infer(self, text):
        from services.gear_sharing import infer_equipment_categories
        return infer_equipment_categories(text)

    def test_empty_string(self):
        assert self._infer('') == set()

    def test_none(self):
        assert self._infer(None) == set()

    def test_single_buck(self):
        cats = self._infer('single buck')
        assert 'crosscut' in cats

    def test_double_buck(self):
        cats = self._infer('double buck')
        assert 'crosscut' in cats

    def test_jack_and_jill(self):
        cats = self._infer('jack & jill')
        assert 'crosscut' in cats

    def test_jack_and_jill_spelled_out(self):
        cats = self._infer('jack and jill')
        assert 'crosscut' in cats

    def test_hot_saw(self):
        cats = self._infer('hot saw')
        assert 'chainsaw' in cats

    def test_chainsaw(self):
        cats = self._infer('chainsaw')
        assert 'chainsaw' in cats

    def test_springboard(self):
        cats = self._infer('springboard')
        assert 'springboard' in cats

    def test_board_alone(self):
        cats = self._infer('board')
        assert 'springboard' in cats

    def test_multiple_categories(self):
        cats = self._infer('single buck, hot saw, springboard')
        assert cats == {'crosscut', 'chainsaw', 'springboard'}

    def test_no_match(self):
        cats = self._infer('obstacle pole speed climb')
        assert cats == set()

    def test_case_insensitive(self):
        cats = self._infer('SINGLE BUCK')
        assert 'crosscut' in cats

    def test_partial_match_no_false_positive(self):
        # "buck" alone should not match - needs "single buck" or "double buck"
        cats = self._infer('buck')
        # The function checks for exact substring matches of 'single buck', 'double buck', etc.
        assert 'crosscut' not in cats

    def test_crosscut_direct(self):
        cats = self._infer('crosscut')
        assert 'crosscut' in cats

    @pytest.mark.parametrize('text', [
        '!@#$%',
        '\u00e9\u00e8\u00ea',
        'x' * 500,
        '123456',
    ])
    def test_garbage_inputs_return_empty(self, text):
        cats = self._infer(text)
        assert isinstance(cats, set)
        # No false positives from nonsense
        assert len(cats) == 0

    def test_power_saw(self):
        cats = self._infer('power saw')
        assert 'chainsaw' in cats

    def test_hand_saw(self):
        cats = self._infer('hand saw')
        assert 'crosscut' in cats

    def test_handsaw_no_space(self):
        cats = self._infer('handsaw')
        assert 'crosscut' in cats
