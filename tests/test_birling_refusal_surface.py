"""A4 writes fail closed without replacing a valid Birling bracket."""
from __future__ import annotations

import copy
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest

from services import birling_rows as rows
from services.birling_bracket import BirlingBracket
from tests.conftest import (
    make_college_competitor,
    make_event,
    make_team,
    make_tournament,
)


def _world(session):
    tournament = make_tournament(session)
    team = make_team(session, tournament)
    people = [make_college_competitor(session, tournament, team, f"Person {i}")
              for i in range(4)]
    event = make_event(session, tournament, "Birling", event_type="college",
                       scoring_type="bracket")
    session.flush()
    bracket = BirlingBracket(event)
    bracket.generate_bracket([
        {"id": person.id, "name": person.display_name} for person in people
    ])
    return event, people, bracket


def _authenticated_client(app, session):
    from models.user import User

    admin = User(username=f'birling_operator_{uuid4().hex}', role='admin')
    admin.set_password('test-password')
    session.add(admin)
    session.flush()
    client = app.test_client()
    with client.session_transaction() as client_session:
        client_session['_user_id'] = str(admin.id)
        client_session['_fresh'] = True
    return client


def test_rejected_document_leaves_last_valid_rows_intact(
        db_session, reference_gate_disarmed):
    event, people, bracket = _world(db_session)
    before = rows.load_document(event)
    broken = copy.deepcopy(bracket.bracket_data)
    broken["seeding"][0] = max(person.id for person in people) + 1000
    broken["competitors"][0]["id"] = broken["seeding"][0]

    with pytest.raises(rows.ProjectionRefused):
        rows.project_document(event, broken)

    assert rows.load_document(event) == before


def test_service_keeps_payout_configuration_while_saving_bracket(db_session):
    event, people, _bracket = _world(db_session)
    event.payouts = '{"1": 500, "2": 250}'
    db_session.commit()

    BirlingBracket(event).generate_bracket([
        {"id": person.id, "name": person.display_name} for person in reversed(people)
    ])

    assert event.payouts == '{"1": 500, "2": 250}'


def test_reset_keeps_payout_configuration(db_session, auth_client):
    event, _people, _bracket = _world(db_session)
    event.payouts = '{"1": 500}'
    db_session.commit()

    response = auth_client.post(
        f"/scheduling/{event.tournament_id}/event/{event.id}/birling/reset",
        data={
            'expected_bracket_digest': BirlingBracket(
                event,
            ).bracket_state_digest(),
        },
    )

    assert response.status_code == 302
    assert event.payouts == '{"1": 500}'
    assert rows.load_document(event) == rows.empty_document()


def _record_direct_result(bracket, winner_index=0):
    match = bracket.get_current_matches()[0]
    bracket.record_direct_winner(
        match['match_id'],
        match[f'competitor{winner_index + 1}'],
        expected_fall_digest=bracket.match_fall_digest(match['match_id']),
    )
    return match['match_id']


def test_destructive_forms_render_current_state_digests(app, db_session):
    auth_client = _authenticated_client(app, db_session)
    event, _people, bracket = _world(db_session)
    match_id = _record_direct_result(bracket)

    response = auth_client.get(
        f'/scheduling/{event.tournament_id}/event/{event.id}/birling'
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    persisted = BirlingBracket(event)
    assert (
        f'name="expected_undo_digest" '
        f'value="{persisted.match_fall_digest(match_id)}"'
    ) in html
    rendered_reset_digest = re.search(
        r'name="expected_bracket_digest" value="([0-9a-f]+)"',
        html,
    )
    assert rendered_reset_digest is not None
    assert rendered_reset_digest.group(1) == persisted.bracket_state_digest()


@pytest.mark.parametrize('action', ['undo', 'reset'])
def test_destructive_action_without_digest_is_retryable_and_does_not_mutate(
    action, app, db_session,
):
    auth_client = _authenticated_client(app, db_session)
    event, _people, bracket = _world(db_session)
    match_id = _record_direct_result(bracket)
    before = rows.load_document(event)
    data = {'match_id': match_id} if action == 'undo' else {}

    response = auth_client.post(
        f'/scheduling/{event.tournament_id}/event/{event.id}/birling/{action}',
        data=data,
    )

    assert response.status_code == 409
    assert response.headers['X-Retryable'] == 'true'
    assert rows.load_document(event) == before


def test_stale_undo_does_not_remove_a_newer_match_result(app, db_session):
    auth_client = _authenticated_client(app, db_session)
    event, _people, bracket = _world(db_session)
    match_id = _record_direct_result(bracket, winner_index=0)
    stale_digest = BirlingBracket(event).match_fall_digest(match_id)

    first_undo = auth_client.post(
        f'/scheduling/{event.tournament_id}/event/{event.id}/birling/undo',
        data={
            'match_id': match_id,
            'expected_undo_digest': stale_digest,
        },
    )
    refreshed = BirlingBracket(event)
    match = refreshed._find_match(match_id)
    refreshed.record_direct_winner(
        match_id,
        match['competitor2'],
        expected_fall_digest=refreshed.match_fall_digest(match_id),
    )

    stale_undo = auth_client.post(
        f'/scheduling/{event.tournament_id}/event/{event.id}/birling/undo',
        data={
            'match_id': match_id,
            'expected_undo_digest': stale_digest,
        },
    )

    assert first_undo.status_code == 302
    assert stale_undo.status_code == 409
    assert stale_undo.headers['X-Retryable'] == 'true'
    assert BirlingBracket(event)._find_match(match_id)['winner'] == match['competitor2']


def test_fresh_no_js_undo_form_clears_the_result(app, db_session):
    auth_client = _authenticated_client(app, db_session)
    event, _people, bracket = _world(db_session)
    match_id = _record_direct_result(bracket)
    persisted = BirlingBracket(event)

    response = auth_client.post(
        f'/scheduling/{event.tournament_id}/event/{event.id}/birling/undo',
        data={
            'match_id': match_id,
            'expected_undo_digest': persisted.match_fall_digest(match_id),
        },
    )

    assert response.status_code == 302
    match = BirlingBracket(event)._find_match(match_id)
    assert match['winner'] is None
    assert match['loser'] is None
    assert match['falls'] == []


def test_concurrent_undo_submissions_apply_exactly_once(app, db_session):
    event, _people, bracket = _world(db_session)
    match_id = _record_direct_result(bracket)
    expected_digest = BirlingBracket(event).match_fall_digest(match_id)
    clients = [
        _authenticated_client(app, db_session),
        _authenticated_client(app, db_session),
    ]
    db_session.commit()
    barrier = Barrier(2)
    url = f'/scheduling/{event.tournament_id}/event/{event.id}/birling/undo'

    def submit(client):
        barrier.wait(timeout=5)
        return client.post(url, data={
            'match_id': match_id,
            'expected_undo_digest': expected_digest,
        }).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(submit, clients))

    assert sorted(statuses) == [302, 409]
    match = BirlingBracket(event)._find_match(match_id)
    assert match['winner'] is None
    assert match['loser'] is None
    assert match['falls'] == []


def test_stale_reset_preserves_a_newer_fall(app, db_session):
    auth_client = _authenticated_client(app, db_session)
    event, _people, bracket = _world(db_session)
    stale_digest = bracket.bracket_state_digest()
    match = bracket.get_current_matches()[0]
    bracket.record_fall(
        match['match_id'],
        match['competitor1'],
        expected_fall_digest=bracket.match_fall_digest(match['match_id']),
    )

    response = auth_client.post(
        f'/scheduling/{event.tournament_id}/event/{event.id}/birling/reset',
        data={'expected_bracket_digest': stale_digest},
    )

    assert response.status_code == 409
    assert response.headers['X-Retryable'] == 'true'
    persisted = BirlingBracket(event)._find_match(match['match_id'])
    assert [fall['winner'] for fall in persisted['falls']] == [
        match['competitor1'],
    ]


def test_duplicate_birling_fall_submission_does_not_add_a_second_fall(
    app, db_session,
):
    auth_client = _authenticated_client(app, db_session)

    event, _people, bracket = _world(db_session)
    match = bracket.get_current_matches()[0]
    expected_fall_digest = bracket.match_fall_digest(match['match_id'])
    payload = {
        'match_id': match['match_id'],
        'fall_winner_id': str(match['competitor1']),
        'expected_fall_digest': expected_fall_digest,
    }
    url = (
        f'/scheduling/{event.tournament_id}/event/{event.id}/birling/fall'
    )

    first = auth_client.post(url, data=payload)
    duplicate = auth_client.post(url, data=payload)

    assert first.status_code == 302
    assert duplicate.status_code == 409
    assert duplicate.headers['X-Retryable'] == 'true'
    persisted = BirlingBracket(event)._find_match(match['match_id'])
    assert len(persisted['falls']) == 1
    with auth_client.session_transaction() as session:
        assert any(
            'fall state changed since this page was loaded' in message
            for _, message in session['_flashes']
        )


def test_direct_winner_form_renders_current_fall_digest(
    app, db_session,
):
    auth_client = _authenticated_client(app, db_session)
    event, _people, bracket = _world(db_session)
    match = bracket.get_current_matches()[0]
    expected_fall_digest = bracket.match_fall_digest(match['match_id'])

    response = auth_client.get(
        f'/scheduling/{event.tournament_id}/event/{event.id}/birling'
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert html.count(
        f'name="expected_fall_digest" value="{expected_fall_digest}"'
    ) == 3


def test_stale_direct_winner_after_fall_returns_retryable_conflict(
    app, db_session,
):
    auth_client = _authenticated_client(app, db_session)
    event, _people, bracket = _world(db_session)
    match = bracket.get_current_matches()[0]
    match_id = match['match_id']
    direct_winner_id = match['competitor2']
    fall_winner_id = match['competitor1']
    stale_digest = bracket.match_fall_digest(match_id)

    fall_response = auth_client.post(
        f'/scheduling/{event.tournament_id}/event/{event.id}/birling/fall',
        data={
            'match_id': match_id,
            'fall_winner_id': str(fall_winner_id),
            'expected_fall_digest': stale_digest,
        },
    )
    stale_response = auth_client.post(
        f'/scheduling/{event.tournament_id}/event/{event.id}/birling/record',
        data={
            'match_id': match_id,
            'winner_id': str(direct_winner_id),
            'expected_fall_digest': stale_digest,
        },
    )

    assert fall_response.status_code == 302
    assert stale_response.status_code == 409
    assert stale_response.headers['X-Retryable'] == 'true'

    persisted = BirlingBracket(event)._find_match(match_id)
    assert persisted['winner'] is None
    assert persisted['loser'] is None
    assert [fall['winner'] for fall in persisted['falls']] == [fall_winner_id]


def test_direct_winner_without_fall_digest_is_rejected(app, db_session):
    auth_client = _authenticated_client(app, db_session)
    event, _people, bracket = _world(db_session)
    match = bracket.get_current_matches()[0]

    response = auth_client.post(
        f'/scheduling/{event.tournament_id}/event/{event.id}/birling/record',
        data={
            'match_id': match['match_id'],
            'winner_id': str(match['competitor1']),
        },
    )

    assert response.status_code == 409
    assert response.headers['X-Retryable'] == 'true'
    persisted = BirlingBracket(event)._find_match(match['match_id'])
    assert persisted['winner'] is None
    assert persisted['loser'] is None
    assert persisted['falls'] == []


def test_direct_winner_with_current_digest_preserves_no_js_flow(
    app, db_session,
):
    auth_client = _authenticated_client(app, db_session)
    event, _people, bracket = _world(db_session)
    match = bracket.get_current_matches()[0]
    winner_id = match['competitor1']

    response = auth_client.post(
        f'/scheduling/{event.tournament_id}/event/{event.id}/birling/record',
        data={
            'match_id': match['match_id'],
            'winner_id': str(winner_id),
            'expected_fall_digest': bracket.match_fall_digest(match['match_id']),
        },
    )

    assert response.status_code == 302
    persisted = BirlingBracket(event)._find_match(match['match_id'])
    assert persisted['winner'] == winner_id
    assert [fall['winner'] for fall in persisted['falls']] == [
        winner_id,
        winner_id,
    ]
