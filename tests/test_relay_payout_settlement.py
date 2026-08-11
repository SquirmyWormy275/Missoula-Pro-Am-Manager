"""Relay payout ranking and team-level settlement coverage."""

import pytest

from database import db
from models.relay import RelayState, RelayTeam
from services.proam_relay import ProAmRelay, relay_payout_summary
from tests.conftest import make_event, make_pro_competitor, make_tournament


def _completed_relay(session, tournament, payouts=None):
    event = make_event(
        session,
        tournament,
        'Pro-Am Relay',
        event_type='pro',
        scoring_type='time',
        payouts=payouts or {'1': 500.0, '2': 250.0},
        status='completed',
    )
    state = RelayState(event_id=event.id, status='completed')
    session.add(state)
    session.flush()
    first = RelayTeam(
        relay_state_id=state.id,
        team_number=1,
        name='Team One',
        total_time=85.2,
    )
    second = RelayTeam(
        relay_state_id=state.id,
        team_number=2,
        name='Team Two',
        total_time=79.4,
    )
    session.add_all([first, second])
    session.flush()
    return event, state, first, second


def test_relay_payout_summary_ranks_completed_teams_and_uses_team_settlement(db_session):
    tournament = make_tournament(db_session)
    _, _, first, second = _completed_relay(db_session, tournament)
    first.payout_settled = True
    db_session.commit()

    summary = relay_payout_summary(tournament)

    assert [row['team'].id for row in summary['rows']] == [second.id, first.id]
    assert [row['placement'] for row in summary['rows']] == [1, 2]
    assert [row['payout_amount'] for row in summary['rows']] == [500.0, 250.0]
    assert summary['total_owed'] == pytest.approx(750.0)
    assert summary['total_settled'] == pytest.approx(250.0)
    assert summary['total_outstanding'] == pytest.approx(500.0)


def test_relay_payout_summary_excludes_provisional_results(db_session):
    tournament = make_tournament(db_session)
    _, state, _, _ = _completed_relay(db_session, tournament)
    state.status = 'in_progress'
    db_session.commit()

    assert relay_payout_summary(tournament)['rows'] == []


def test_relay_resave_keeps_settlement_for_same_team_number(db_session):
    tournament = make_tournament(db_session)
    _, state, first, _ = _completed_relay(db_session, tournament)
    first.payout_settled = True
    db_session.commit()

    relay = ProAmRelay(tournament)
    relay.relay_data = {
        'status': 'completed',
        'teams': [{
            'team_number': 1,
            'name': 'Team One',
            'pro_members': [],
            'college_members': [],
            'events': {
                key: {'result': 20.0, 'status': 'completed'}
                for key in ProAmRelay.RELAY_EVENTS
            },
            'total_time': 80.0,
        }],
    }
    relay._save_relay_data()

    fresh = RelayTeam.query.filter_by(relay_state_id=state.id, team_number=1).one()
    assert fresh.payout_settled is True


def test_relay_resave_resets_settlement_when_roster_changes(db_session):
    tournament = make_tournament(db_session)
    _, state, first, _ = _completed_relay(db_session, tournament)
    replacement = make_pro_competitor(db_session, tournament, 'Replacement Pro')
    first.payout_settled = True
    db_session.commit()

    relay = ProAmRelay(tournament)
    relay.relay_data = {
        'status': 'completed',
        'teams': [{
            'team_number': 1,
            'name': 'Team One',
            'pro_members': [{'id': replacement.id}],
            'college_members': [],
            'events': {
                key: {'result': 20.0, 'status': 'completed'}
                for key in ProAmRelay.RELAY_EVENTS
            },
            'total_time': 80.0,
        }],
    }
    relay._save_relay_data()

    fresh = RelayTeam.query.filter_by(relay_state_id=state.id, team_number=1).one()
    assert fresh.payout_settled is False


def test_report_includes_relay_rows_and_toggle_settles_team(auth_client, db_session):
    tournament = make_tournament(db_session)
    _, _, _, payable_team = _completed_relay(db_session, tournament)
    db.session.commit()

    page = auth_client.get(f'/reporting/{tournament.id}/pro/payouts')
    assert page.status_code == 200
    assert b'Pro-Am Relay Team Payouts' in page.data
    assert b'Team Two' in page.data
    assert b'$500.00' in page.data

    printable = auth_client.get(f'/reporting/{tournament.id}/pro/payouts/print')
    assert printable.status_code == 200
    assert b'Pro-Am Relay Team Payouts' in printable.data

    ops_dashboard = auth_client.get(f'/tournament/{tournament.id}/ops-dashboard')
    assert ops_dashboard.status_code == 200
    assert b'$750.00' in ops_dashboard.data

    response = auth_client.post(
        f'/tournament/{tournament.id}/proam-relay/team/{payable_team.id}/toggle-settled',
        headers={'X-Requested-With': 'XMLHttpRequest'},
    )

    assert response.status_code == 200
    assert response.get_json() == {'ok': True, 'settled': True}
    assert db.session.get(RelayTeam, payable_team.id).payout_settled is True

    state = RelayState.query.filter_by(event_id=payable_team.relay_state.event_id).one()
    state.status = 'in_progress'
    db.session.commit()

    response = auth_client.post(
        f'/tournament/{tournament.id}/proam-relay/team/{payable_team.id}/toggle-settled',
        headers={'X-Requested-With': 'XMLHttpRequest'},
    )

    assert response.status_code == 404
