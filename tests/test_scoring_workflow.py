from __future__ import annotations

from decimal import Decimal

import pytest

from models.event import EventResult
from services.scoring_workflow import finalize_event_results, save_heat_results_submission
from tests.conftest import (
    make_college_competitor,
    make_event,
    make_event_result,
    make_heat,
    make_team,
    make_tournament,
)


def test_save_heat_results_submission_detects_stale_heat_version(db_session):
    tournament = make_tournament(db_session)
    team = make_team(db_session, tournament)
    competitor = make_college_competitor(db_session, tournament, team, 'Stale Judge')
    event = make_event(
        db_session,
        tournament,
        'Standing Block Hard Hit',
        event_type='college',
        scoring_type='hits',
        scoring_order='highest_wins',
    )
    heat = make_heat(
        db_session,
        event,
        competitors=[competitor.id],
        stand_assignments={str(competitor.id): 1},
    )

    outcome = save_heat_results_submission(
        tournament_id=tournament.id,
        heat=heat,
        event=event,
        form_data={
            'heat_version': str(heat.version_id + 1),
            f'result_{competitor.id}': '7',
            f'status_{competitor.id}': 'completed',
        },
        judge_user_id=99,
    )

    assert outcome['ok'] is False
    assert outcome['status_code'] == 409
    assert outcome['redirect_kind'] == 'heat_entry'


def test_save_heat_results_submission_persists_results_and_undo_token(db_session):
    tournament = make_tournament(db_session)
    team = make_team(db_session, tournament)
    competitor = make_college_competitor(db_session, tournament, team, 'Scored Sam')
    event = make_event(
        db_session,
        tournament,
        'Standing Block Hard Hit',
        event_type='college',
        scoring_type='hits',
        scoring_order='highest_wins',
    )
    heat = make_heat(
        db_session,
        event,
        competitors=[competitor.id],
        stand_assignments={str(competitor.id): 1},
    )

    outcome = save_heat_results_submission(
        tournament_id=tournament.id,
        heat=heat,
        event=event,
        form_data={
            'heat_version': str(heat.version_id),
            f'result_{competitor.id}': '9',
            f'status_{competitor.id}': 'completed',
        },
        judge_user_id=7,
    )

    row = EventResult.query.filter_by(
        event_id=event.id,
        competitor_id=competitor.id,
        competitor_type='college',
    ).one()

    assert outcome['ok'] is True
    assert outcome['redirect_kind'] == 'event_results'
    assert outcome['undo_heat_id'] == heat.id
    assert outcome['undo_token']['event_id'] == event.id
    assert row.result_value == Decimal('9.00')
    assert heat.status == 'completed'


def test_save_heat_results_submission_keeps_scores_when_auto_finalize_fails(db_session, monkeypatch):
    tournament = make_tournament(db_session)
    team = make_team(db_session, tournament)
    first = make_college_competitor(db_session, tournament, team, 'A')
    second = make_college_competitor(db_session, tournament, team, 'B')
    event = make_event(
        db_session,
        tournament,
        'Single Buck',
        event_type='college',
        scoring_type='time',
        scoring_order='lowest_wins',
    )
    heat = make_heat(
        db_session,
        event,
        competitors=[first.id, second.id],
        stand_assignments={str(first.id): 1, str(second.id): 2},
    )

    def _boom(_event):
        raise RuntimeError('boom')

    monkeypatch.setattr('services.scoring_engine.calculate_positions', _boom)

    outcome = save_heat_results_submission(
        tournament_id=tournament.id,
        heat=heat,
        event=event,
        form_data={
            'heat_version': str(heat.version_id),
            f't1_run1_{first.id}': '12.0',
            f't2_run1_{first.id}': '12.4',
            f'status_{first.id}': 'completed',
            f't1_run1_{second.id}': '13.0',
            f't2_run1_{second.id}': '13.2',
            f'status_{second.id}': 'completed',
        },
        judge_user_id=5,
    )

    rows = EventResult.query.filter_by(event_id=event.id).order_by(EventResult.competitor_id).all()

    assert outcome['ok'] is True
    assert outcome['category'] == 'warning'
    assert float(rows[0].result_value) == pytest.approx(12.2)
    assert float(rows[1].result_value) == pytest.approx(13.1)
    assert event.is_finalized is False


def test_finalize_event_results_returns_warnings_and_finalizes(db_session):
    tournament = make_tournament(db_session)
    team = make_team(db_session, tournament)
    first = make_college_competitor(db_session, tournament, team, 'Final A')
    second = make_college_competitor(db_session, tournament, team, 'Final B')
    event = make_event(
        db_session,
        tournament,
        'Single Buck',
        event_type='college',
        scoring_type='time',
        scoring_order='lowest_wins',
    )
    make_event_result(
        db_session,
        event,
        first,
        competitor_type='college',
        result_value=10.0,
        run1_value=10.0,
        status='completed',
    )
    make_event_result(
        db_session,
        event,
        second,
        competitor_type='college',
        result_value=12.0,
        run1_value=12.0,
        status='completed',
    )

    outcome = finalize_event_results(
        event=event,
        tournament_id=tournament.id,
        judge_user_id=3,
    )

    rows = EventResult.query.filter_by(event_id=event.id).order_by(EventResult.final_position).all()
    assert outcome['ok'] is True
    assert isinstance(outcome['warnings'], list)
    assert rows[0].competitor_id == first.id
    assert rows[0].final_position == 1
    assert rows[1].final_position == 2
