"""
Unit tests for services/proam_relay.py

Tests focus on pure state management methods.  The __init__ calls
_load_relay_data() which queries Event — we patch that out, then
set relay.relay_data directly before exercising the methods under test.

_save_relay_data() is also patched with patch.object so no DB session
is needed.

Run:  pytest tests/test_proam_relay.py -v
"""
import pytest
from unittest.mock import patch, MagicMock

from services.proam_relay import ProAmRelay


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _relay(status='drawn', teams=None):
    """Construct a ProAmRelay without hitting the DB."""
    with patch('services.proam_relay.Event') as mock_ev:
        mock_ev.query.filter_by.return_value.first.return_value = None
        relay = ProAmRelay(MagicMock())
    relay.relay_data = {
        'status': status,
        'teams': teams if teams is not None else [],
        'eligible_college': [],
        'eligible_pro': [],
        'drawn_college': [],
        'drawn_pro': [],
    }
    return relay


def _make_team(num, total_time=None):
    """Create a realistic relay team dict."""
    return {
        'team_number': num,
        'name': f'Team {num}',
        'pro_members': [],
        'college_members': [],
        'events': {
            'partnered_sawing': {'result': None, 'status': 'pending'},
            'standing_butcher_block': {'result': None, 'status': 'pending'},
            'underhand_butcher_block': {'result': None, 'status': 'pending'},
            'team_axe_throw': {'result': None, 'status': 'pending'},
        },
        'total_time': total_time,
    }


# ---------------------------------------------------------------------------
# get_status / get_teams
# ---------------------------------------------------------------------------

class TestGetters:
    def test_get_status_returns_current_status(self):
        relay = _relay(status='in_progress')
        assert relay.get_status() == 'in_progress'

    def test_get_status_not_drawn(self):
        relay = _relay(status='not_drawn')
        assert relay.get_status() == 'not_drawn'

    def test_get_teams_returns_list(self):
        teams = [_make_team(1), _make_team(2)]
        relay = _relay(teams=teams)
        assert relay.get_teams() == teams

    def test_get_teams_empty(self):
        relay = _relay(teams=[])
        assert relay.get_teams() == []


# ---------------------------------------------------------------------------
# get_results
# ---------------------------------------------------------------------------

class TestGetResults:
    def test_get_results_sorted_ascending_by_total_time(self):
        t1 = _make_team(1, total_time=120.5)
        t2 = _make_team(2, total_time=98.3)
        relay = _relay(teams=[t1, t2])
        results = relay.get_results()
        assert results[0]['team_number'] == 2   # faster time first
        assert results[1]['team_number'] == 1

    def test_get_results_excludes_incomplete_teams(self):
        t1 = _make_team(1, total_time=110.0)
        t2 = _make_team(2, total_time=None)   # not finished
        relay = _relay(teams=[t1, t2])
        results = relay.get_results()
        assert len(results) == 1
        assert results[0]['team_number'] == 1

    def test_get_results_no_completed_teams_returns_empty(self):
        relay = _relay(teams=[_make_team(1), _make_team(2)])
        assert relay.get_results() == []

    def test_get_results_all_teams_complete(self):
        t1 = _make_team(1, total_time=200.0)
        t2 = _make_team(2, total_time=180.0)
        t3 = _make_team(3, total_time=195.0)
        relay = _relay(teams=[t1, t2, t3])
        results = relay.get_results()
        assert len(results) == 3
        assert results[0]['total_time'] == 180.0


# ---------------------------------------------------------------------------
# record_total_time
# ---------------------------------------------------------------------------

class TestRecordTotalTime:
    def test_sets_total_time_on_matching_team(self):
        teams = [_make_team(1), _make_team(2)]
        relay = _relay(teams=teams)
        with patch.object(relay, '_save_relay_data'):
            relay.record_total_time(1, 99.9)
        assert relay.relay_data['teams'][0]['total_time'] == 99.9
        # Team 2 untouched
        assert relay.relay_data['teams'][1]['total_time'] is None

    def test_sets_status_in_progress_after_partial_recording(self):
        teams = [_make_team(1), _make_team(2)]
        relay = _relay(status='drawn', teams=teams)
        with patch.object(relay, '_save_relay_data'):
            relay.record_total_time(1, 110.0)
        assert relay.relay_data['status'] == 'in_progress'

    def test_status_becomes_completed_when_all_teams_have_time(self):
        teams = [_make_team(1), _make_team(2)]
        relay = _relay(teams=teams)
        with patch.object(relay, '_save_relay_data'):
            relay.record_total_time(1, 100.0)
            relay.record_total_time(2, 105.0)
        assert relay.relay_data['status'] == 'completed'

    def test_save_relay_data_called(self):
        teams = [_make_team(1)]
        relay = _relay(teams=teams)
        with patch.object(relay, '_save_relay_data') as mock_save:
            relay.record_total_time(1, 88.0)
        mock_save.assert_called_once()


# ---------------------------------------------------------------------------
# record_event_result
# ---------------------------------------------------------------------------

class TestRecordEventResult:
    def test_records_single_event_result(self):
        teams = [_make_team(1), _make_team(2)]
        relay = _relay(teams=teams)
        with patch.object(relay, '_save_relay_data'):
            relay.record_event_result(1, 'partnered_sawing', 25.0)
        team1 = relay.relay_data['teams'][0]
        assert team1['events']['partnered_sawing']['result'] == 25.0
        assert team1['events']['partnered_sawing']['status'] == 'completed'

    def test_single_event_does_not_set_total_time(self):
        teams = [_make_team(1)]
        relay = _relay(teams=teams)
        with patch.object(relay, '_save_relay_data'):
            relay.record_event_result(1, 'partnered_sawing', 30.0)
        # Not all events complete, so total_time should remain None
        assert relay.relay_data['teams'][0]['total_time'] is None

    def test_all_events_complete_sets_total_time(self):
        teams = [_make_team(1)]
        relay = _relay(teams=teams)
        event_times = {
            'partnered_sawing': 20.0,
            'standing_butcher_block': 15.0,
            'underhand_butcher_block': 18.0,
            'team_axe_throw': 12.0,
        }
        with patch.object(relay, '_save_relay_data'):
            for ev_name, t in event_times.items():
                relay.record_event_result(1, ev_name, t)

        expected_total = sum(event_times.values())
        assert relay.relay_data['teams'][0]['total_time'] == pytest.approx(expected_total)

    def test_status_becomes_in_progress_after_first_result(self):
        teams = [_make_team(1), _make_team(2)]
        relay = _relay(status='drawn', teams=teams)
        with patch.object(relay, '_save_relay_data'):
            relay.record_event_result(1, 'partnered_sawing', 22.0)
        assert relay.relay_data['status'] == 'in_progress'

    def test_status_completed_when_all_teams_and_events_done(self):
        teams = [_make_team(1), _make_team(2)]
        relay = _relay(teams=teams)
        event_names = [
            'partnered_sawing',
            'standing_butcher_block',
            'underhand_butcher_block',
            'team_axe_throw',
        ]
        with patch.object(relay, '_save_relay_data'):
            for team_num in (1, 2):
                for ev in event_names:
                    relay.record_event_result(team_num, ev, 20.0)
        assert relay.relay_data['status'] == 'completed'

    def test_partial_completion_does_not_set_status_completed(self):
        teams = [_make_team(1), _make_team(2)]
        relay = _relay(teams=teams)
        # Only complete all events for team 1
        event_names = [
            'partnered_sawing',
            'standing_butcher_block',
            'underhand_butcher_block',
            'team_axe_throw',
        ]
        with patch.object(relay, '_save_relay_data'):
            for ev in event_names:
                relay.record_event_result(1, ev, 20.0)
        # Team 2 has no events complete, so status should not be 'completed'
        assert relay.relay_data['status'] != 'completed'

    def test_save_called_on_record_event_result(self):
        teams = [_make_team(1)]
        relay = _relay(teams=teams)
        with patch.object(relay, '_save_relay_data') as mock_save:
            relay.record_event_result(1, 'partnered_sawing', 10.0)
        mock_save.assert_called_once()

    def test_other_team_unaffected_by_result(self):
        teams = [_make_team(1), _make_team(2)]
        relay = _relay(teams=teams)
        with patch.object(relay, '_save_relay_data'):
            relay.record_event_result(1, 'partnered_sawing', 25.0)
        # Team 2's event should still be pending
        team2 = relay.relay_data['teams'][1]
        assert team2['events']['partnered_sawing']['result'] is None
        assert team2['events']['partnered_sawing']['status'] == 'pending'
