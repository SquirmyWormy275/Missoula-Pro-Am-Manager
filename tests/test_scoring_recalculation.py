"""Real score entry, correction and reports on both supported test DB paths.

DOMAIN_CONTRACT.md: each physical run needs evidence; scoring and permitted
corrections must persist before standings/reporting consume them.
"""
import pytest
from sqlalchemy import text

from database import db
from tests.conftest import make_event, make_heat, make_pro_competitor, make_tournament
from tests.db_test_utils import create_test_app, drop_test_db


@pytest.fixture()
def app():
    application, handle = create_test_app(use_migrations=True)
    try:
        with application.app_context():
            yield application
            db.session.remove()
            db.engine.dispose()
    finally:
        drop_test_db(handle)


@pytest.fixture()
def tournament(db_session):
    return make_tournament(db_session)


def _saved_results(event_id):
    # A new connection reads committed database values, bypassing the ORM map.
    with db.engine.connect() as connection:
        return {row['competitor_id']: dict(row) for row in connection.execute(text(
            'SELECT competitor_id, run1_value, run2_value, run3_value, best_run, '
            'result_value, final_position, payout_amount, status FROM event_results '
            'WHERE event_id = :event_id'
        ), {'event_id': event_id}).mappings()}


def _post_heat(client, tournament_id, heat_id, values):
    with db.engine.connect() as connection:
        version = connection.execute(text(
            'SELECT version_id FROM heats WHERE id = :heat_id'
        ), {'heat_id': heat_id}).scalar_one()
    response = client.post(f'/scoring/{tournament_id}/heat/{heat_id}/enter',
                           data={'heat_version': str(version), **values})
    assert response.status_code == 302


def _assert_report(client, tournament_id, event_id, first, second, rendered):
    response = client.get(f'/reporting/{tournament_id}/event/{event_id}/results')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert html.index(f'<strong>{first}</strong>') < html.index(f'<strong>{second}</strong>')
    for value in rendered:
        assert value in html


@pytest.mark.parametrize('kind,order,expected,corrected', [
    ('time', 'lowest_wins', (8.0, 20.0), (10.0, 5.0)),
    ('distance', 'highest_wins', (10.0, 22.0), (30.0, 20.0)),
])
def test_dual_run_entry_and_correction_persist_best_and_report(
        db_session, auth_client, tournament, kind, order, expected, corrected):
    event = make_event(db_session, tournament, 'Synthetic dual run',
                       scoring_type=kind, scoring_order=order, requires_dual_runs=True,
                       payouts={'1': 100, '2': 50})
    competitors = [make_pro_competitor(db_session, tournament, name)
                   for name in ('Synthetic Alpha', 'Synthetic Beta')]
    ids = [competitor.id for competitor in competitors]
    heats = [make_heat(db_session, event, run_number=run, competitors=ids) for run in (1, 2)]
    tournament_id, event_id = tournament.id, event.id
    heat_ids = [heat.id for heat in heats]
    db_session.commit()

    def enter(heat_id, run, values):
        fields = {}
        for competitor_id, value in zip(ids, values):
            fields[f't1_run{run}_{competitor_id}'] = str(value)
            fields[f't2_run{run}_{competitor_id}'] = str(value)
        _post_heat(auth_client, tournament_id, heat_id, fields)

    enter(heat_ids[0], 1, (10.0, 20.0))
    saved = _saved_results(event_id)
    assert [saved[cid]['result_value'] for cid in ids] == [10.0, 20.0]
    assert all(saved[cid]['run2_value'] is None for cid in ids)
    enter(heat_ids[1], 2, (8.0, 22.0))
    saved = _saved_results(event_id)
    assert [saved[cid]['best_run'] for cid in ids] == list(expected)
    assert [saved[cid]['result_value'] for cid in ids] == list(expected)
    winner = 0 if order == 'lowest_wins' else 1
    assert saved[ids[winner]]['final_position'] == 1
    assert saved[ids[winner]]['payout_amount'] == 100
    suffix = 's' if kind == 'time' else ' ft'
    precision = 2 if kind == 'time' else 1
    _assert_report(auth_client, tournament_id, event_id,
                   competitors[winner].name, competitors[1 - winner].name,
                   [f'{value:.{precision}f}{suffix}' for value in expected])

    # Correct an already completed run through the same supported request path.
    enter(heat_ids[1], 2, (30.0, 5.0))
    saved = _saved_results(event_id)
    assert [saved[cid]['best_run'] for cid in ids] == list(corrected)
    assert [saved[cid]['result_value'] for cid in ids] == list(corrected)
    assert saved[ids[1 - winner]]['final_position'] == 1
    assert saved[ids[1 - winner]]['payout_amount'] == 100
    assert saved[ids[winner]]['payout_amount'] == 50
    _assert_report(auth_client, tournament_id, event_id,
                   competitors[1 - winner].name, competitors[winner].name,
                   [f'{value:.{precision}f}{suffix}' for value in corrected])


def test_cumulative_entry_and_zero_correction_persist_total_and_report(
        db_session, auth_client, tournament):
    event = make_event(db_session, tournament, 'Axe Throw', scoring_type='score',
                       scoring_order='highest_wins', requires_triple_runs=True,
                       payouts={'1': 100, '2': 50})
    competitors = [make_pro_competitor(db_session, tournament, name)
                   for name in ('Synthetic Alpha', 'Synthetic Beta')]
    ids = [competitor.id for competitor in competitors]
    heat = make_heat(db_session, event, competitors=ids)
    tournament_id, event_id, heat_id = tournament.id, event.id, heat.id
    db_session.commit()

    def enter(alpha_second):
        fields = {}
        for competitor_id, throws in zip(ids, ((8, alpha_second, 7), (10, 4, 2))):
            fields[f'result_{competitor_id}'] = str(throws[0])
            fields[f'result2_{competitor_id}'] = str(throws[1])
            fields[f'result3_{competitor_id}'] = str(throws[2])
        _post_heat(auth_client, tournament_id, heat_id, fields)

    enter(9)
    saved = _saved_results(event_id)
    assert [saved[cid]['result_value'] for cid in ids] == [24.0, 16.0]
    assert saved[ids[0]]['final_position'] == 1
    _assert_report(auth_client, tournament_id, event_id,
                   'Synthetic Alpha', 'Synthetic Beta', ['24.0', '16.0'])
    enter(0)
    saved = _saved_results(event_id)
    assert saved[ids[0]]['run2_value'] == 0.0
    assert [saved[cid]['result_value'] for cid in ids] == [15.0, 16.0]
    assert saved[ids[1]]['final_position'] == 1
    assert saved[ids[1]]['payout_amount'] == 100
    assert saved[ids[0]]['payout_amount'] == 50
    _assert_report(auth_client, tournament_id, event_id,
                   'Synthetic Beta', 'Synthetic Alpha', ['15.0', '16.0'])
