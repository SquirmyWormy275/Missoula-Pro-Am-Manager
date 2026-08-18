"""Operator-facing coverage for college spillover fallback warnings."""
from unittest.mock import patch


def _make_tournament(db_session):
    from models import Tournament

    tournament = Tournament(
        name='Spillover Feedback Test', year=2027, status='pro_active',
    )
    db_session.add(tournament)
    db_session.flush()
    return tournament


def _conflict_result():
    return {
        'integrated_heats': 1,
        'events': 1,
        'message': 'College spillover heats integrated into flights.',
        'ignored_non_college_event_ids': [],
        'unavoidable_stand_conflicts': [{
            'heat_ids': (10, 11),
            'stand_types': ('obstacle_pole', 'speed_climb'),
            'gap': 2,
        }],
    }


def _assert_preflight_warning(client):
    with client.session_transaction() as session:
        flashes = session.get('_flashes', [])
    warning_messages = [message for category, message in flashes if category == 'warning']
    assert any('judge review' in message.lower() for message in warning_messages)
    assert any('preflight check' in message.lower() for message in warning_messages)


def test_feedback_payload_preserves_unavoidable_conflicts():
    from routes.scheduling.spillover_feedback import spillover_result_payload

    result = _conflict_result()
    assert spillover_result_payload(result) == result


def test_event_page_integration_flashes_unavoidable_conflict(
        db_session, auth_client):
    tournament = _make_tournament(db_session)

    with patch(
        'services.flight_builder.integrate_proam_relay_into_final_flight',
        return_value={'placed': False},
    ), patch(
        'services.flight_builder.integrate_college_spillover_into_flights',
        return_value=_conflict_result(),
    ), patch('services.saw_block_assignment.trigger_saw_block_recompute'), patch(
        'routes.scheduling.events.db.session.commit', side_effect=db_session.flush,
    ):
        response = auth_client.post(
            f'/scheduling/{tournament.id}/events',
            data={'action': 'integrate_spillover'},
            follow_redirects=False,
        )

    assert response.status_code in (302, 303)
    _assert_preflight_warning(auth_client)
    with auth_client.session_transaction() as session:
        success_messages = [
            message for category, message in session.get('_flashes', [])
            if category == 'success'
        ]
    assert len(success_messages) == 1


def test_one_click_generation_flashes_unavoidable_conflict(
        db_session, auth_client):
    tournament = _make_tournament(db_session)
    summary = {
        'ok': True,
        'generated': 0,
        'skipped_events': [],
        'protected': [],
        'flights': 2,
        'relay': {'placed': False},
        'spillover': _conflict_result(),
    }

    with patch(
        'routes.scheduling.flights.generate_tournament_schedule_artifacts',
        return_value=summary,
    ), patch('routes.scheduling.flights.log_action'):
        response = auth_client.post(
            f'/scheduling/{tournament.id}/flights/one-click-generate',
            follow_redirects=False,
        )

    assert response.status_code in (302, 303)
    _assert_preflight_warning(auth_client)


def test_preflight_autofix_flashes_unavoidable_conflict(
        db_session, auth_client):
    tournament = _make_tournament(db_session)
    autofix_result = {
        'gear_parsed': {'parsed': 0},
        'gear_pairs_completed': 0,
        'partner_summary': {'assigned_pairs': 0},
        'spillover': _conflict_result(),
        'relay': {'placed': False},
    }

    with patch(
        'routes.scheduling.preflight.run_preflight_autofix',
        return_value=autofix_result,
    ), patch('routes.scheduling.preflight.log_action'), patch(
        'routes.scheduling.preflight.db.session.commit', side_effect=db_session.flush,
    ):
        response = auth_client.post(
            f'/scheduling/{tournament.id}/preflight',
            data={'action': 'autofix'},
            follow_redirects=False,
        )

    assert response.status_code in (302, 303)
    _assert_preflight_warning(auth_client)
    with auth_client.session_transaction() as session:
        success_messages = [
            message for category, message in session.get('_flashes', [])
            if category == 'success'
        ]
    assert len(success_messages) == 1
