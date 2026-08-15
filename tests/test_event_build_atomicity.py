"""Transaction-boundary regressions for active Events schedule actions."""

from unittest.mock import patch

import pytest

from database import db
from tests.db_test_utils import create_test_app, drop_test_db


@pytest.fixture(scope='module')
def app():
    app, db_handle = create_test_app()
    with app.app_context():
        yield app
        db.session.remove()
    drop_test_db(db_handle)


@pytest.fixture()
def scheduling_state(app, request):
    from models import Event, Flight, Heat, Tournament

    with app.app_context():
        tournament = Tournament(
            name=f'Events Atomicity {request.node.name}',
            year=2027,
            status='pro_active',
        )
        db.session.add(tournament)
        db.session.flush()

        event = Event(
            tournament_id=tournament.id,
            name='Underhand',
            event_type='pro',
            scoring_type='time',
            scoring_order='lowest_wins',
            stand_type='underhand',
            max_stands=4,
        )
        original_flight = Flight(
            tournament_id=tournament.id,
            flight_number=1,
            status='pending',
        )
        db.session.add_all([event, original_flight])
        db.session.flush()

        heat = Heat(
            event_id=event.id,
            heat_number=1,
            run_number=1,
            status='pending',
            flight_id=original_flight.id,
            flight_position=1,
        )
        db.session.add(heat)
        db.session.commit()

        yield {
            'tournament_id': tournament.id,
            'heat_id': heat.id,
            'original_flight_id': original_flight.id,
        }
        db.session.rollback()


def _run_action(
    app, state, action, *, spillover_fails, saw_assignment_fails=False,
):
    from flask import session

    from models import Flight, Heat, Tournament
    from routes.scheduling.events import _handle_event_list_post

    calls = {'build': [], 'relay': [], 'spillover': []}

    def generate_event_heats(_event):
        return 0

    def build_pro_flights(tournament, **kwargs):
        calls['build'].append(kwargs)
        replacement = Flight(
            tournament_id=tournament.id,
            flight_number=2,
            status='pending',
        )
        db.session.add(replacement)
        db.session.flush()
        heat = db.session.get(Heat, state['heat_id'])
        heat.flight_id = replacement.id
        heat.flight_position = 1
        db.session.flush()
        return 2

    def integrate_relay(_tournament, **kwargs):
        calls['relay'].append(kwargs)
        return {'placed': True}

    def integrate_spillover(_tournament, _event_ids, **kwargs):
        calls['spillover'].append(kwargs)
        if spillover_fails:
            raise RuntimeError('forced spillover failure')
        return {
            'integrated_heats': 0,
            'events': 0,
            'message': 'No college spillover heats needed integration.',
            'ignored_non_college_event_ids': [],
            'unavoidable_stand_conflicts': [],
        }

    with app.test_request_context(
            f'/scheduling/{state["tournament_id"]}/events',
            method='POST',
            data={'action': action}):
        tournament = db.session.get(Tournament, state['tournament_id'])
        real_commit = db.session.commit
        with patch(
            'services.flight_builder.integrate_proam_relay_into_final_flight',
            side_effect=integrate_relay,
        ), patch(
            'services.saw_block_assignment.assign_saw_blocks',
            side_effect=(
                RuntimeError('forced saw assignment failure')
                if saw_assignment_fails else None
            ),
        ), patch(
            'routes.scheduling.events.invalidate_tournament_caches',
        ), patch(
            'routes.scheduling.events.db.session.commit',
            side_effect=real_commit,
        ) as commit:
            _handle_event_list_post(
                tournament,
                [],
                generate_event_heats,
                build_pro_flights,
                integrate_spillover,
            )
        flashes = list(session.get('_flashes', []))

    db.session.expire_all()
    heat = db.session.get(Heat, state['heat_id'])
    flights = Flight.query.filter_by(
        tournament_id=state['tournament_id'],
    ).order_by(Flight.flight_number).all()
    return calls, commit.call_count, flashes, heat, flights


@pytest.mark.parametrize('action', ['generate_all', 'rebuild_flights'])
def test_active_event_build_chain_commits_once_after_all_steps(
        app, scheduling_state, action):
    calls, commit_count, flashes, heat, flights = _run_action(
        app, scheduling_state, action, spillover_fails=False,
    )

    assert calls == {
        'build': [{'commit': False}],
        'relay': [{'commit': False}],
        'spillover': [{'commit': False}],
    }
    assert commit_count == 1
    assert [flight.flight_number for flight in flights] == [1, 2]
    assert heat.flight_id == flights[1].id
    assert any(category == 'success' for category, _message in flashes)


@pytest.mark.parametrize('action', ['generate_all', 'rebuild_flights'])
def test_active_event_build_chain_rolls_back_forced_spillover_failure(
        app, scheduling_state, action):
    calls, commit_count, flashes, heat, flights = _run_action(
        app, scheduling_state, action, spillover_fails=True,
    )

    assert calls == {
        'build': [{'commit': False}],
        'relay': [{'commit': False}],
        'spillover': [{'commit': False}],
    }
    assert commit_count == 0
    assert [flight.id for flight in flights] == [
        scheduling_state['original_flight_id'],
    ]
    assert heat.flight_id == scheduling_state['original_flight_id']
    assert heat.flight_position == 1
    assert not [message for category, message in flashes if category == 'success']
    assert any(
        category == 'error' and 'rolled back' in message.lower()
        for category, message in flashes
    )


@pytest.mark.parametrize('action', ['generate_all', 'rebuild_flights'])
def test_saw_assignment_failure_rolls_back_generated_schedule(
        app, scheduling_state, action):
    _calls, commit_count, flashes, heat, flights = _run_action(
        app,
        scheduling_state,
        action,
        spillover_fails=False,
        saw_assignment_fails=True,
    )

    assert commit_count == 0
    assert [flight.id for flight in flights] == [
        scheduling_state['original_flight_id'],
    ]
    assert heat.flight_id == scheduling_state['original_flight_id']
    assert any(
        category == 'error' and 'rolled back' in message.lower()
        for category, message in flashes
    )


def test_standalone_spillover_chain_commits_once_after_relay(app, scheduling_state):
    from flask import session

    from models import Tournament
    from routes.scheduling.events import _handle_event_list_post

    calls = {'relay': [], 'spillover': []}

    def integrate_relay(_tournament, **kwargs):
        calls['relay'].append(kwargs)
        return {'placed': True}

    def integrate_spillover(_tournament, _event_ids, **kwargs):
        calls['spillover'].append(kwargs)
        return {
            'integrated_heats': 0,
            'events': 0,
            'message': 'No college spillover heats needed integration.',
            'ignored_non_college_event_ids': [],
            'unavoidable_stand_conflicts': [],
        }

    with app.test_request_context(
            f'/scheduling/{scheduling_state["tournament_id"]}/events',
            method='POST',
            data={'action': 'integrate_spillover'}):
        tournament = db.session.get(Tournament, scheduling_state['tournament_id'])
        real_commit = db.session.commit
        with patch(
            'services.flight_builder.integrate_proam_relay_into_final_flight',
            side_effect=integrate_relay,
        ), patch(
            'services.saw_block_assignment.trigger_saw_block_recompute',
        ), patch(
            'routes.scheduling.events.invalidate_tournament_caches',
        ), patch(
            'routes.scheduling.events.db.session.commit',
            side_effect=real_commit,
        ) as commit:
            _handle_event_list_post(
                tournament,
                [],
                lambda _event: 0,
                lambda _tournament, **_kwargs: 0,
                integrate_spillover,
            )
        flashes = list(session.get('_flashes', []))

    assert calls == {
        'relay': [{'commit': False}],
        'spillover': [{'commit': False}],
    }
    assert commit.call_count == 1
    assert any(category == 'success' for category, _message in flashes)


def test_standalone_spillover_rolls_back_relay_and_success_flash(
        app, scheduling_state):
    from flask import flash, session

    from models import Heat, Tournament
    from routes.scheduling.events import _handle_event_list_post

    calls = {'relay': [], 'spillover': []}

    def integrate_relay(_tournament, **kwargs):
        calls['relay'].append(kwargs)
        heat = db.session.get(Heat, scheduling_state['heat_id'])
        heat.flight_position = 2
        db.session.flush()
        flash('Relay integration complete.', 'success')
        return {'placed': True}

    def integrate_spillover(_tournament, _event_ids, **kwargs):
        calls['spillover'].append(kwargs)
        raise RuntimeError('forced standalone spillover failure')

    with app.test_request_context(
            f'/scheduling/{scheduling_state["tournament_id"]}/events',
            method='POST',
            data={'action': 'integrate_spillover'}):
        tournament = db.session.get(Tournament, scheduling_state['tournament_id'])
        with patch(
            'services.flight_builder.integrate_proam_relay_into_final_flight',
            side_effect=integrate_relay,
        ):
            _handle_event_list_post(
                tournament,
                [],
                lambda _event: 0,
                lambda _tournament, **_kwargs: 0,
                integrate_spillover,
            )
        flashes = list(session.get('_flashes', []))

    db.session.expire_all()
    heat = db.session.get(Heat, scheduling_state['heat_id'])
    assert calls == {
        'relay': [{'commit': False}],
        'spillover': [{'commit': False}],
    }
    assert heat.flight_position == 1
    assert not [message for category, message in flashes if category == 'success']
    assert any(
        category == 'error' and 'failed' in message.lower()
        for category, message in flashes
    )
