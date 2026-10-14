"""STRATHMARK clock contract: cutting duration + start mark = common finish.

Owner reaffirmed the shared contract on 2026-10-13. No historical data fixture
or backfill is used. These are new synthetic score submissions.
"""
from datetime import datetime

import pytest
from sqlalchemy import text

from database import db
from services.scoring_engine import live_standings_data
from tests.conftest import make_event, make_event_result, make_heat, make_pro_competitor
from tests.test_scoring_recalculation import app, tournament  # noqa: F401


@pytest.mark.parametrize('durations,positions', [
    ((60, 45, 30), (1, 1, 1)),
    ((62, 44, 29), (3, 1, 1)),
    ((58, 46, 31), (1, 2, 2)),
])
def test_raw_timer_submission_uses_common_finish_and_persists_reports(
        db_session, auth_client, tournament, durations, positions):
    event = make_event(db_session, tournament, 'Underhand', is_handicap=True,
                       stand_type='underhand', payouts={'1': 100, '2': 50, '3': 25})
    competitors = [make_pro_competitor(db_session, tournament, name)
                   for name in ('Synthetic Front', 'Synthetic Middle', 'Synthetic Back')]
    ids = [competitor.id for competitor in competitors]
    for competitor, mark in zip(competitors, (3, 18, 33)):
        row = make_event_result(db_session, event, competitor)
        row.handicap_factor = mark
        row.mark_assigned_at = datetime(2026, 10, 13)
    heat = make_heat(db_session, event, competitors=ids)
    tid, eid, hid, version = tournament.id, event.id, heat.id, heat.version_id
    db_session.commit()
    payload = {'heat_version': str(version)}
    for competitor, duration in zip(ids, durations):
        payload.update({f't1_run1_{competitor}': str(duration - .1),
                        f't2_run1_{competitor}': str(duration + .1)})
    response = auth_client.post(f'/scoring/{tid}/heat/{hid}/enter', data=payload)
    assert response.status_code == 302
    with db.engine.connect() as connection:
        saved = connection.execute(text(
            'SELECT result_value,final_position,payout_amount FROM event_results '
            'WHERE event_id=:event ORDER BY id'), {'event': eid}).all()
    assert [float(row.result_value) for row in saved] == list(durations)
    assert [row.final_position for row in saved] == list(positions)
    # Existing pro tie policy awards the configured shared-position payout.
    assert [float(row.payout_amount) for row in saved] == [
        {1: 100., 2: 50., 3: 25.}[position] for position in positions]
    report = auth_client.get(f'/reporting/{tid}/event/{eid}/results')
    assert report.status_code == 200
    for duration in durations:
        assert f'{duration:.2f}s' in report.get_data(as_text=True)


def test_finalized_live_feed_keeps_recorded_placements(db_session, tournament):
    """Reading a previously finalized event must not decide its winners again."""
    event = make_event(db_session, tournament, 'Underhand', is_handicap=True,
                       stand_type='underhand')
    for index, (duration, mark, position) in enumerate(((60, 3, 3), (45, 18, 2), (30, 33, 1))):
        competitor = make_pro_competitor(db_session, tournament, f'Synthetic History {index}')
        result = make_event_result(db_session, event, competitor,
                                   result_value=duration, status='completed')
        result.handicap_factor = mark
        result.final_position = position
    event.is_finalized = True
    db_session.flush()
    output = live_standings_data(event)
    assert [(row['competitor_name'], row['position']) for row in output['rows']] == [
        ('Synthetic History 2', 1), ('Synthetic History 1', 2), ('Synthetic History 0', 3)]
