"""Atomic synchronous flight-build chains and background spillover feedback."""
from unittest.mock import Mock, patch

import pytest

from database import db
from tests.db_test_utils import create_test_app, drop_test_db


@pytest.fixture(scope='module')
def app():
    test_app, handle = create_test_app()
    with test_app.app_context():
        from models.user import User

        admin = User(username='flight_atomic_admin', role='admin')
        admin.set_password('flight_atomic_password')
        db.session.add(admin)
        db.session.commit()
        test_app.config['_FLIGHT_ATOMIC_ADMIN_ID'] = admin.id
    yield test_app
    with test_app.app_context():
        db.session.remove()
        db.engine.dispose()
    drop_test_db(handle)


@pytest.fixture()
def auth_client(app):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(app.config['_FLIGHT_ATOMIC_ADMIN_ID'])
        session['_fresh'] = True
    return client


def _make_tournament(app, *, with_pro_heat=False):
    with app.app_context():
        from models import Event, Heat, Tournament

        tournament = Tournament(
            name='Atomic Flight Snapshot', year=2027, status='setup',
        )
        db.session.add(tournament)
        db.session.flush()
        if with_pro_heat:
            event = Event(
                tournament_id=tournament.id,
                name='Underhand',
                event_type='pro',
                gender='M',
                scoring_type='time',
                scoring_order='lowest_wins',
                stand_type='underhand',
                max_stands=5,
                status='pending',
            )
            db.session.add(event)
            db.session.flush()
            heat = Heat(event_id=event.id, heat_number=1, run_number=1)
            heat.set_roster('pro', [])
            db.session.add(heat)
        db.session.commit()
        return tournament.id


def _mutating_build(target, *args, **kwargs):
    target.name = 'Partially Committed Build'
    db.session.flush()
    return 1


def _raise_spillover(*args, **kwargs):
    raise RuntimeError('forced spillover failure')


def _raise_saw_assignment(*args, **kwargs):
    raise RuntimeError('forced saw assignment failure')


def _flash_heat_generation_success(*args, **kwargs):
    from flask import flash

    flash('Heats generated successfully.', 'success')


def _raise_after_heat_generation_mutation(target, *args, **kwargs):
    from flask import flash

    target.name = 'Partially Generated Heats'
    db.session.flush()
    flash('Heats generated successfully.', 'success')
    raise RuntimeError('forced heat generation failure')


def _make_chokerman_schedule(app):
    with app.app_context():
        from models import Event, Flight, Heat, Tournament

        tournament = Tournament(
            name='Chokerman Reorder Guard', year=2028, status='setup',
        )
        db.session.add(tournament)
        db.session.flush()
        pro_event = Event(
            tournament_id=tournament.id,
            name='Underhand',
            event_type='pro',
            gender='M',
            scoring_type='time',
            scoring_order='lowest_wins',
            stand_type='underhand',
            max_stands=5,
            status='pending',
        )
        chokerman = Event(
            tournament_id=tournament.id,
            name="Chokerman's Race",
            event_type='college',
            gender='M',
            scoring_type='time',
            scoring_order='lowest_wins',
            stand_type='chokerman',
            max_stands=5,
            status='pending',
        )
        db.session.add_all([pro_event, chokerman])
        db.session.flush()
        first = Flight(tournament_id=tournament.id, flight_number=1)
        last = Flight(tournament_id=tournament.id, flight_number=2)
        db.session.add_all([first, last])
        db.session.flush()

        opener = Heat(
            event_id=pro_event.id, heat_number=1, run_number=1,
            flight_id=first.id, flight_position=1,
        )
        neutral = Heat(
            event_id=pro_event.id, heat_number=2, run_number=1,
            flight_id=last.id, flight_position=1,
        )
        closer = Heat(
            event_id=chokerman.id, heat_number=1, run_number=2,
            flight_id=last.id, flight_position=2,
        )
        for heat in (opener, neutral):
            heat.set_roster('pro', [])
        closer.set_roster('college', [])
        db.session.add_all([opener, neutral, closer])
        db.session.commit()
        return {
            'tournament_id': tournament.id,
            'first_flight_id': first.id,
            'last_flight_id': last.id,
            'opener_id': opener.id,
            'neutral_id': neutral.id,
            'closer_id': closer.id,
        }


def _assert_snapshot_and_flashes(app, client, tournament_id):
    from models import Tournament

    with app.app_context():
        db.session.expire_all()
        tournament = db.session.get(Tournament, tournament_id)
        assert tournament.name == 'Atomic Flight Snapshot'
    with client.session_transaction() as session:
        flashes = list(session.get('_flashes', []))
    success_messages = [message for category, message in flashes if category == 'success']
    error_messages = [message for category, message in flashes if category == 'error']
    assert success_messages == []
    assert error_messages


def test_one_click_build_chain_rolls_back_as_one_unit(app, auth_client):
    tournament_id = _make_tournament(app)

    with patch(
        'routes.scheduling.flights._generate_all_heats',
        side_effect=_flash_heat_generation_success,
    ), patch(
        'routes.scheduling.flights._build_pro_flights_if_possible',
        side_effect=_mutating_build,
    ) as build, patch(
        'services.flight_builder.integrate_proam_relay_into_final_flight',
        return_value={'placed': True},
    ) as relay, patch(
        'services.flight_builder.integrate_college_spillover_into_flights',
        side_effect=_raise_spillover,
    ) as spillover:
        response = auth_client.post(
            f'/scheduling/{tournament_id}/flights/one-click-generate',
            follow_redirects=False,
        )

    assert response.status_code in (302, 303)
    assert build.call_args.kwargs['commit'] is False
    assert relay.call_args.kwargs['commit'] is False
    assert spillover.call_args.kwargs['commit'] is False
    _assert_snapshot_and_flashes(app, auth_client, tournament_id)


def test_one_click_heat_generation_failure_rolls_back_as_one_unit(
        app, auth_client):
    tournament_id = _make_tournament(app)

    with patch(
        'routes.scheduling.flights._generate_all_heats',
        side_effect=_raise_after_heat_generation_mutation,
    ), patch(
        'services.flight_builder.build_pro_flights',
    ) as build:
        response = auth_client.post(
            f'/scheduling/{tournament_id}/flights/one-click-generate',
            follow_redirects=False,
        )

    assert response.status_code in (302, 303)
    build.assert_not_called()
    _assert_snapshot_and_flashes(app, auth_client, tournament_id)


def test_one_click_saw_assignment_failure_rolls_back_as_one_unit(
        app, auth_client):
    tournament_id = _make_tournament(app)

    with patch(
        'routes.scheduling.flights._generate_all_heats',
        side_effect=_flash_heat_generation_success,
    ), patch(
        'routes.scheduling.flights._build_pro_flights_if_possible',
        side_effect=_mutating_build,
    ), patch(
        'services.flight_builder.integrate_proam_relay_into_final_flight',
        return_value={'placed': True},
    ), patch(
        'services.flight_builder.integrate_college_spillover_into_flights',
        return_value={'integrated_heats': 0},
    ), patch(
        'services.saw_block_assignment.assign_saw_blocks',
        side_effect=_raise_saw_assignment,
    ):
        response = auth_client.post(
            f'/scheduling/{tournament_id}/flights/one-click-generate',
            follow_redirects=False,
        )

    assert response.status_code in (302, 303)
    _assert_snapshot_and_flashes(app, auth_client, tournament_id)


def test_build_helper_forwards_atomic_commit_flag(app):
    tournament_id = _make_tournament(app, with_pro_heat=True)
    builder = Mock(return_value=1)

    with app.app_context():
        from models import Tournament
        from routes.scheduling import _build_pro_flights_if_possible

        tournament = db.session.get(Tournament, tournament_id)
        result = _build_pro_flights_if_possible(
            tournament, builder, num_flights=2, commit=False,
        )

    assert result == 1
    builder.assert_called_once_with(
        tournament, num_flights=2, commit=False,
    )


def test_manual_build_chain_rolls_back_as_one_unit(app, auth_client):
    tournament_id = _make_tournament(app, with_pro_heat=True)

    with patch(
        'services.flight_builder.build_pro_flights', side_effect=_mutating_build,
    ) as build, patch(
        'services.flight_builder.integrate_proam_relay_into_final_flight',
        return_value={'placed': True},
    ) as relay, patch(
        'services.flight_builder.integrate_college_spillover_into_flights',
        side_effect=_raise_spillover,
    ) as spillover, patch(
        'routes.scheduling.flights.log_action',
    ):
        response = auth_client.post(
            f'/scheduling/{tournament_id}/flights/build',
            data={'flight_sizing_mode': 'count', 'num_flights': '1'},
            follow_redirects=False,
        )

    assert response.status_code in (302, 303)
    assert build.call_args.kwargs['commit'] is False
    assert relay.call_args.kwargs['commit'] is False
    assert spillover.call_args.kwargs['commit'] is False
    _assert_snapshot_and_flashes(app, auth_client, tournament_id)


def test_manual_build_saw_assignment_failure_rolls_back_as_one_unit(
        app, auth_client):
    tournament_id = _make_tournament(app, with_pro_heat=True)

    with patch(
        'services.flight_builder.build_pro_flights', side_effect=_mutating_build,
    ), patch(
        'services.flight_builder.integrate_proam_relay_into_final_flight',
        return_value={'placed': True},
    ), patch(
        'services.flight_builder.integrate_college_spillover_into_flights',
        return_value={'integrated_heats': 0},
    ), patch(
        'services.saw_block_assignment.assign_saw_blocks',
        side_effect=_raise_saw_assignment,
    ), patch(
        'routes.scheduling.flights.log_action',
    ):
        response = auth_client.post(
            f'/scheduling/{tournament_id}/flights/build',
            data={'flight_sizing_mode': 'count', 'num_flights': '1'},
            follow_redirects=False,
        )

    assert response.status_code in (302, 303)
    _assert_snapshot_and_flashes(app, auth_client, tournament_id)


def test_completed_background_build_surfaces_spillover_warnings(app, auth_client):
    tournament_id = _make_tournament(app)
    job = {
        'status': 'completed',
        'metadata': {
            'kind': 'build_pro_flights',
            'tournament_id': tournament_id,
        },
        'result': {
            'flights_built': 2,
            'relay': {'placed': False},
            'spillover': {
                'integrated_heats': 1,
                'ignored_non_college_event_ids': [91],
                'unavoidable_stand_conflicts': [{
                    'heat_ids': (10, 11),
                    'stand_types': ('obstacle_pole', 'speed_climb'),
                    'gap': 2,
                }],
            },
        },
    }

    with patch('routes.reporting.get_job', return_value=job):
        response = auth_client.get(
            f'/reporting/{tournament_id}/jobs/atomic-job',
            follow_redirects=False,
        )

    assert response.status_code in (302, 303)
    with auth_client.session_transaction() as session:
        flashes = list(session.get('_flashes', []))
    warning_messages = [message for category, message in flashes if category == 'warning']
    assert any('Ignored 1 selected event' in message for message in warning_messages)
    assert any('judge review' in message.lower() for message in warning_messages)


def test_async_build_captures_one_spillover_config_snapshot(app, auth_client):
    tournament_id = _make_tournament(app, with_pro_heat=True)
    with app.app_context():
        from models import Tournament

        tournament = db.session.get(Tournament, tournament_id)
        tournament.set_schedule_config({
            'saturday_college_event_ids': [71, 72],
        })
        db.session.commit()

    captured = {}

    def submit_spy(kind, func, *args, metadata=None):
        captured.update({
            'kind': kind,
            'func': func,
            'args': args,
            'metadata': metadata,
        })
        return 'snapshot-job'

    with patch(
        'routes.scheduling.flights.submit_job', side_effect=submit_spy,
    ):
        response = auth_client.post(
            f'/scheduling/{tournament_id}/flights/build',
            data={
                'flight_sizing_mode': 'count',
                'num_flights': '2',
                'run_async': '1',
            },
            follow_redirects=False,
        )

    assert response.status_code in (302, 303)
    assert captured['kind'] == 'build_pro_flights'
    assert captured['args'][0] == tournament_id
    assert captured['args'][2] == [71, 72]


def test_single_flight_reorder_cannot_move_chokerman_ahead_of_tail(
        app, auth_client):
    schedule = _make_chokerman_schedule(app)

    response = auth_client.post(
        f"/scheduling/{schedule['tournament_id']}/flights/"
        f"{schedule['last_flight_id']}/reorder",
        json={'heat_ids': [schedule['closer_id'], schedule['neutral_id']]},
    )

    assert response.status_code == 409
    assert response.get_json()['code'] == 'chokerman_closer'
    with app.app_context():
        from models import Heat

        closer = db.session.get(Heat, schedule['closer_id'])
        neutral = db.session.get(Heat, schedule['neutral_id'])
        assert neutral.flight_position == 1
        assert closer.flight_position == 2


def test_bulk_reorder_cannot_move_chokerman_out_of_last_flight(
        app, auth_client):
    schedule = _make_chokerman_schedule(app)

    response = auth_client.post(
        f"/scheduling/{schedule['tournament_id']}/flights/bulk-reorder",
        json={'flights': [
            {
                'flight_id': schedule['first_flight_id'],
                'heat_ids': [schedule['opener_id'], schedule['closer_id']],
            },
            {
                'flight_id': schedule['last_flight_id'],
                'heat_ids': [schedule['neutral_id']],
            },
        ]},
    )

    assert response.status_code == 409
    assert response.get_json()['code'] == 'chokerman_closer'
    with app.app_context():
        from models import Heat

        closer = db.session.get(Heat, schedule['closer_id'])
        assert closer.flight_id == schedule['last_flight_id']
        assert closer.flight_position == 2


def test_manual_reorder_is_blocked_after_any_flight_starts(app, auth_client):
    schedule = _make_chokerman_schedule(app)
    with app.app_context():
        from models import Flight

        active = db.session.get(Flight, schedule['first_flight_id'])
        active.status = 'in_progress'
        db.session.commit()

    response = auth_client.post(
        f"/scheduling/{schedule['tournament_id']}/flights/"
        f"{schedule['last_flight_id']}/reorder",
        json={'heat_ids': [schedule['neutral_id'], schedule['closer_id']]},
    )

    assert response.status_code == 409
    assert response.get_json()['code'] == 'active_flight'
    with app.app_context():
        from models import Heat

        closer = db.session.get(Heat, schedule['closer_id'])
        assert closer.flight_position == 2
