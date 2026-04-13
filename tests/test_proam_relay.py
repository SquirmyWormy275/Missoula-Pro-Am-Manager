"""
Unit tests for services/proam_relay.py and relay payout routes.

Tests focus on pure state management methods.  The __init__ calls
_load_relay_data() which queries Event — we patch that out, then
set relay.relay_data directly before exercising the methods under test.

_save_relay_data() is also patched with patch.object so no DB session
is needed.

Run:  pytest tests/test_proam_relay.py -v
"""
import json
import os
from unittest.mock import MagicMock, patch

import pytest

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


# ---------------------------------------------------------------------------
# Event.uses_payouts_for_state — relay must return False (state moved to event_state)
# ---------------------------------------------------------------------------

class TestUsesPayoutsForState:
    def _make_event(self, name='Test', has_prelims=False, scoring_type='time'):
        from unittest.mock import MagicMock
        from models.event import Event
        ev = MagicMock()
        ev.name = name
        ev.has_prelims = has_prelims
        ev.scoring_type = scoring_type
        # Evaluate the actual property logic against the mock
        ev.uses_payouts_for_state = Event.uses_payouts_for_state.fget(ev)
        return ev

    def test_relay_returns_false(self):
        ev = self._make_event(name='Pro-Am Relay', scoring_type='time')
        assert ev.uses_payouts_for_state is False

    def test_bracket_returns_true(self):
        ev = self._make_event(scoring_type='bracket')
        assert ev.uses_payouts_for_state is True

    def test_has_prelims_returns_true(self):
        ev = self._make_event(has_prelims=True)
        assert ev.uses_payouts_for_state is True

    def test_ordinary_event_returns_false(self):
        ev = self._make_event(name='Underhand Speed', scoring_type='time')
        assert ev.uses_payouts_for_state is False


# ---------------------------------------------------------------------------
# Relay payout routes (integration — uses Flask test client)
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def relay_app():
    from tests.db_test_utils import create_test_app
    _app, db_path = create_test_app()
    _app.config['SESSION_PROTECTION'] = None

    with _app.app_context():
        _seed_relay_db(_app)
        yield _app
        from database import db as _db
        _db.session.remove()

    try:
        os.unlink(db_path)
    except OSError:
        pass


def _seed_relay_db(app):
    from database import db as _db
    from models import Tournament
    from models.event import Event
    from models.user import User

    if not User.query.filter_by(username='relay_admin').first():
        u = User(username='relay_admin', role='admin')
        u.set_password('relay_pass')
        _db.session.add(u)

    if not Tournament.query.filter_by(name='Relay Payout Test Tournament').first():
        t = Tournament(name='Relay Payout Test Tournament', year=2026, status='setup')
        _db.session.add(t)

    _db.session.flush()
    t = Tournament.query.filter_by(name='Relay Payout Test Tournament').first()

    if not Event.query.filter_by(tournament_id=t.id, name='Pro-Am Relay').first():
        ev = Event(
            tournament_id=t.id,
            name='Pro-Am Relay',
            event_type='pro',
            scoring_type='time',
            scoring_order='lowest_wins',
            is_partnered=True,
            payouts=json.dumps({}),
            status='pending',
        )
        _db.session.add(ev)

    _db.session.commit()


@pytest.fixture()
def relay_auth_client(relay_app):
    c = relay_app.test_client(use_cookies=True)
    c.post('/auth/login', data={
        'username': 'relay_admin',
        'password': 'relay_pass',
    }, follow_redirects=True)
    return c


@pytest.fixture()
def relay_tid(relay_app):
    with relay_app.app_context():
        from models import Tournament
        t = Tournament.query.filter_by(name='Relay Payout Test Tournament').first()
        return t.id


class TestRelayPayoutsGet:
    def test_get_returns_200(self, relay_auth_client, relay_tid):
        resp = relay_auth_client.get(
            f'/tournament/{relay_tid}/proam-relay/payouts'
        )
        assert resp.status_code == 200

    def test_get_shows_form(self, relay_auth_client, relay_tid):
        resp = relay_auth_client.get(
            f'/tournament/{relay_tid}/proam-relay/payouts'
        )
        assert b'payout_1' in resp.data

    def test_get_prefills_existing_payouts(self, relay_app, relay_auth_client, relay_tid):
        from database import db as _db
        from models.event import Event
        with relay_app.app_context():
            ev = Event.query.filter_by(
                tournament_id=relay_tid, name='Pro-Am Relay'
            ).first()
            ev.payouts = json.dumps({'1': 500.0, '2': 300.0})
            _db.session.commit()

        resp = relay_auth_client.get(
            f'/tournament/{relay_tid}/proam-relay/payouts'
        )
        assert b'500' in resp.data
        assert b'300' in resp.data

    def test_no_relay_event_returns_404(self, relay_app, relay_auth_client):
        with relay_app.app_context():
            from models import Tournament
            from database import db as _db
            t = Tournament(name='Empty Tournament', year=2026, status='setup')
            _db.session.add(t)
            _db.session.commit()
            empty_tid = t.id

        resp = relay_auth_client.get(
            f'/tournament/{empty_tid}/proam-relay/payouts'
        )
        assert resp.status_code == 404


class TestRelayPayoutsPost:
    def test_save_happy_path_three_positions(self, relay_app, relay_auth_client, relay_tid):
        resp = relay_auth_client.post(
            f'/tournament/{relay_tid}/proam-relay/payouts',
            data={
                'payout_1': '1000.00',
                'payout_2': '600.00',
                'payout_3': '300.00',
            },
            follow_redirects=False,
        )
        # POST-redirect-GET
        assert resp.status_code == 302

        with relay_app.app_context():
            from models.event import Event
            ev = Event.query.filter_by(
                tournament_id=relay_tid, name='Pro-Am Relay'
            ).first()
            saved = json.loads(ev.payouts)
        assert saved['1'] == 1000.0
        assert saved['2'] == 600.0
        assert saved['3'] == 300.0

    def test_negative_amount_clamped_to_zero(self, relay_app, relay_auth_client, relay_tid):
        resp = relay_auth_client.post(
            f'/tournament/{relay_tid}/proam-relay/payouts',
            data={'payout_1': '-50.00'},
            follow_redirects=False,
        )
        assert resp.status_code == 302

        with relay_app.app_context():
            from models.event import Event
            ev = Event.query.filter_by(
                tournament_id=relay_tid, name='Pro-Am Relay'
            ).first()
            saved = json.loads(ev.payouts)
        assert saved.get('1', 0.0) == 0.0

    def test_non_numeric_input_flashes_error(self, relay_auth_client, relay_tid):
        resp = relay_auth_client.post(
            f'/tournament/{relay_tid}/proam-relay/payouts',
            data={'payout_1': 'abc'},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b'Invalid' in resp.data or b'invalid' in resp.data

    def test_post_no_relay_event_returns_404(self, relay_app, relay_auth_client):
        with relay_app.app_context():
            from models import Tournament
            from database import db as _db
            t = Tournament(name='No Relay POST', year=2026, status='setup')
            _db.session.add(t)
            _db.session.commit()
            no_relay_tid = t.id

        resp = relay_auth_client.post(
            f'/tournament/{no_relay_tid}/proam-relay/payouts',
            data={'payout_1': '100'},
        )
        assert resp.status_code == 404
