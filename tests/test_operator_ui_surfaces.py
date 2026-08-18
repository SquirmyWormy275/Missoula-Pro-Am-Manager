"""Regression contracts for dense race-day operator surfaces."""

from pathlib import Path

import pytest

from models.event import Event
from tests.conftest import (
    make_college_competitor,
    make_event,
    make_event_result,
    make_flight,
    make_heat,
    make_pro_competitor,
    make_team,
    make_tournament,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def operator_data(db_session):
    tournament = make_tournament(db_session, name='Operator Surface Test')
    team = make_team(db_session, tournament)
    college = make_college_competitor(
        db_session, tournament, team, 'College Operator', 'F'
    )
    pro = make_pro_competitor(db_session, tournament, 'Pro Operator', 'M')
    pro_dnf = make_pro_competitor(db_session, tournament, 'DNF Operator', 'M')
    college_event = make_event(
        db_session,
        tournament,
        'Standing Block',
        event_type='college',
        gender='F',
        status='completed',
    )
    pro_event = make_event(
        db_session,
        tournament,
        'Underhand',
        event_type='pro',
        gender='M',
        status='completed',
    )
    flight = make_flight(db_session, tournament, name='Opening Block')
    make_heat(
        db_session,
        pro_event,
        competitors=[pro.id],
        flight_id=flight.id,
        flight_position=1,
        status='pending',
    )
    make_heat(db_session, college_event, competitors=[college.id], status='pending')
    make_event_result(
        db_session,
        college_event,
        college,
        competitor_type='college',
        result_value=25.0,
        final_position=1,
        points_awarded=10,
        status='completed',
    )
    make_event_result(
        db_session,
        pro_event,
        pro,
        result_value=35.0,
        final_position=1,
        status='completed',
    )
    make_event_result(
        db_session,
        pro_event,
        pro_dnf,
        result_value=0.0,
        final_position=None,
        status='dnf',
    )
    return {'tournament': tournament, 'pro_event': pro_event}


def test_event_display_name_does_not_duplicate_existing_gender_prefix():
    mens = Event(
        name="Men's Underhand",
        gender='M',
        event_type='pro',
        tournament_id=1,
        scoring_type='time',
    )
    womens = Event(
        name="Women's Standing Block",
        gender='F',
        event_type='pro',
        tournament_id=1,
        scoring_type='time',
    )

    assert mens.display_name == "Men's Underhand"
    assert womens.display_name == "Women's Standing Block"


def test_show_day_pending_heat_is_labelled_up_next(auth_client, operator_data):
    tournament_id = operator_data['tournament'].id
    response = auth_client.get(f'/scheduling/{tournament_id}/show-day')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'Up Next' in body
    assert 'data-show-day-toolbar' in body


def test_show_day_college_actions_have_a_responsive_layout_contract():
    source = (PROJECT_ROOT / 'templates/scheduling/show_day.html').read_text(
        encoding='utf-8'
    )

    assert 'sd-college-actions' in source
    assert 'padding:2px8px' not in source.replace(' ', '')
    assert '@media (max-width: 575.98px)' in source


def test_all_results_exposes_search_and_collapsible_event_summaries(
    auth_client, operator_data
):
    tournament_id = operator_data['tournament'].id
    response = auth_client.get(f'/reporting/{tournament_id}/all-results')

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'data-results-search' in body
    assert 'data-results-clear' in body
    assert 'data-result-card' in body
    assert 'data-result-details' in body
    assert '<th scope="col">Place</th>' in body
    assert '<th scope="col">Competitor</th>' in body
    assert '<span class="badge bg-secondary">DNF</span>' in body
    assert '0.00s' not in body


def test_scoring_results_uses_dark_status_rows_and_labels_toolbar_actions():
    source = (PROJECT_ROOT / 'templates/scoring/event_results.html').read_text(
        encoding='utf-8'
    )

    assert '.scoring-actions' in source
    assert 'aria-label="Import results"' in source
    assert 'aria-label="Print results"' in source
    assert 'aria-label="Offline scoring"' in source
    assert 'background: #d1e7dd' not in source
    assert 'background: rgba(255,255,255' not in source


def test_non_finisher_zero_is_suppressed_on_scoring_and_report_views(
    auth_client, operator_data
):
    tournament_id = operator_data['tournament'].id
    event_id = operator_data['pro_event'].id

    scoring = auth_client.get(
        f'/scoring/{tournament_id}/event/{event_id}/results'
    )
    report = auth_client.get(
        f'/reporting/{tournament_id}/event/{event_id}/results'
    )

    assert scoring.status_code == 200
    assert report.status_code == 200
    assert 'DNF' in scoring.get_data(as_text=True)
    assert 'DNF' in report.get_data(as_text=True)
    assert '0.00s' not in scoring.get_data(as_text=True)
    assert '0.00s' not in report.get_data(as_text=True)
