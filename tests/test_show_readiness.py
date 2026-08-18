"""Fail-closed full-show generation tests.

All database cases use the temporary migrated SQLite fixture from
``tests.conftest``. No production or private-mirror connection is used.
"""
from __future__ import annotations

import json

import pytest

from database import db
from tests.conftest import make_event, make_heat, make_pro_competitor, make_tournament


def _clear_readiness(monkeypatch, calls: list[str] | None = None) -> None:
    report_calls = 0

    def _report(_tournament, _saturday_ids):
        nonlocal report_calls
        report_calls += 1
        if calls is not None:
            calls.append(f'preflight:{report_calls}')
        return {'issues': []}

    monkeypatch.setattr('services.preflight.build_preflight_report', _report)


def test_blocking_codes_are_phase_aware_and_keep_aggregate_contract():
    from services.preflight import (
        BLOCKING_CODES,
        POST_GENERATION_BLOCKING_CODES,
        PRE_GENERATION_BLOCKING_CODES,
    )

    assert 'missing_partner_name' in PRE_GENERATION_BLOCKING_CODES
    assert 'unresolved_partner_name' in PRE_GENERATION_BLOCKING_CODES
    assert {
        'gear_details_not_parsed',
        'gear_unmapped_event_keys',
        'gear_unknown_partner_names',
        'gear_self_reference',
        'gear_partner_mismatch',
        'partnered_axe_pair_state_invalid',
        'partnered_axe_prelims_incomplete',
        'partnered_axe_finals_not_advanced',
    } <= PRE_GENERATION_BLOCKING_CODES
    assert 'invalid_flight_position' in POST_GENERATION_BLOCKING_CODES
    assert PRE_GENERATION_BLOCKING_CODES.isdisjoint(POST_GENERATION_BLOCKING_CODES)
    assert BLOCKING_CODES == (
        PRE_GENERATION_BLOCKING_CODES | POST_GENERATION_BLOCKING_CODES
    )


def test_preflight_json_separates_input_blockers_from_generated_show_blockers(
    db_session, auth_client, monkeypatch
):
    tournament = make_tournament(db_session, name='Phase-Aware Preflight JSON')
    report = {
        'issue_count': 2,
        'severity': {'high': 2, 'medium': 0, 'low': 0},
        'has_blockers': True,
        'blocking': [
            {'code': 'missing_partner_name'},
            {'code': 'invalid_flight_position'},
        ],
        'pre_generation_blocking': [{'code': 'missing_partner_name'}],
        'post_generation_blocking': [{'code': 'invalid_flight_position'}],
        'issues': [
            {
                'severity': 'high',
                'code': 'missing_partner_name',
                'title': 'Partner declaration is blank',
                'detail': 'Repair the pair.',
                'autofix': False,
            },
            {
                'severity': 'high',
                'code': 'invalid_flight_position',
                'title': 'Generated flight position is invalid',
                'detail': 'Rebuild the generated show.',
                'autofix': False,
            },
        ],
    }
    monkeypatch.setattr(
        'services.preflight.build_preflight_report', lambda *_args: report
    )

    response = auth_client.get(
        f'/scheduling/{tournament.id}/preflight-json'
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['has_blockers'] is True
    assert payload['blocking_count'] == 2
    assert payload['pre_generation_has_blockers'] is True
    assert payload['pre_generation_blocking_count'] == 1
    assert payload['post_generation_has_blockers'] is True
    assert payload['post_generation_blocking_count'] == 1


def test_blank_partner_declarations_block_before_generation(db_session):
    from services.preflight import (
        build_preflight_report,
        get_pre_generation_blocking_issues,
    )

    tournament = make_tournament(db_session, name='Blank Partner Gate')
    event = make_event(
        db_session,
        tournament,
        'Jack & Jill Sawing',
        is_partnered=True,
    )
    make_pro_competitor(
        db_session, tournament, 'Alex Blank', gender='F', events=[event.name]
    )
    make_pro_competitor(
        db_session, tournament, 'Jordan Blank', gender='M', events=[event.name]
    )

    report = build_preflight_report(tournament)
    blockers = get_pre_generation_blocking_issues(report)

    missing = [issue for issue in blockers if issue['code'] == 'missing_partner_name']
    assert len(missing) == 1
    assert missing[0]['event_ids'] == [event.id]
    assert {row['competitor_name'] for row in missing[0]['missing']} == {
        'Alex Blank',
        'Jordan Blank',
    }


def test_partnered_axe_state_pairs_do_not_require_duplicate_partner_fields(db_session):
    from services.preflight import build_preflight_report

    tournament = make_tournament(db_session, name='Partnered Axe State Preflight')
    event = make_event(
        db_session,
        tournament,
        'Partnered Axe Throw',
        is_partnered=True,
        has_prelims=True,
        stand_type='axe_throw',
        scoring_type='score',
        scoring_order='highest_wins',
    )
    first = make_pro_competitor(
        db_session, tournament, 'Axe Partner A', events=[event.id]
    )
    second = make_pro_competitor(
        db_session, tournament, 'Axe Partner B', events=[event.id]
    )
    pair = {
        'pair_id': 1,
        'competitor1': {'id': first.id, 'name': first.name},
        'competitor2': {'id': second.id, 'name': second.name},
        'prelim_score': None,
        'final_score': None,
        'final_position': None,
    }
    event.event_state = json.dumps({
        'stage': 'prelims',
        'pairs': [pair],
        'prelim_results': [],
        'finalists': [],
        'final_results': [],
    })
    db_session.flush()

    report = build_preflight_report(tournament)
    codes = {issue['code'] for issue in report['issues']}

    assert 'missing_partner_name' not in codes
    assert 'unresolved_partner_name' not in codes
    assert 'non_reciprocal_partnership' not in codes
    assert 'invalid_partner_gender' not in codes
    assert 'partnered_axe_prelims_incomplete' in codes


def test_partnered_axe_preflight_validates_authoritative_pair_coverage(db_session):
    from services.preflight import (
        build_preflight_report,
        get_pre_generation_blocking_issues,
    )

    tournament = make_tournament(db_session, name='Partnered Axe Pair Coverage')
    event = make_event(
        db_session,
        tournament,
        'Partnered Axe Throw',
        is_partnered=True,
        has_prelims=True,
        stand_type='axe_throw',
        scoring_type='score',
        scoring_order='highest_wins',
    )
    first = make_pro_competitor(
        db_session, tournament, 'Registered Axe Partner', events=[event.id]
    )
    make_pro_competitor(
        db_session, tournament, 'Unpaired Axe Entrant', events=[event.id]
    )
    event.event_state = json.dumps({
        'stage': 'prelims',
        'pairs': [{
            'pair_id': 1,
            'competitor1': {'id': first.id, 'name': first.name},
            'competitor2': {'id': first.id, 'name': first.name},
            'prelim_score': None,
            'final_score': None,
            'final_position': None,
        }],
        'prelim_results': [],
        'finalists': [],
        'final_results': [],
    })
    db_session.flush()

    blockers = get_pre_generation_blocking_issues(
        build_preflight_report(tournament)
    )
    assert 'partnered_axe_pair_state_invalid' in {
        issue['code'] for issue in blockers
    }


def test_partnered_axe_preflight_requires_advancing_scored_prelims(db_session):
    from services.preflight import (
        build_preflight_report,
        get_pre_generation_blocking_issues,
    )

    tournament = make_tournament(db_session, name='Partnered Axe Advance Gate')
    event = make_event(
        db_session,
        tournament,
        'Partnered Axe Throw',
        is_partnered=True,
        has_prelims=True,
        stand_type='axe_throw',
        scoring_type='score',
        scoring_order='highest_wins',
    )
    first = make_pro_competitor(
        db_session, tournament, 'Scored Axe A', events=[event.id]
    )
    second = make_pro_competitor(
        db_session, tournament, 'Scored Axe B', events=[event.id]
    )
    pair = {
        'pair_id': 1,
        'competitor1': {'id': first.id, 'name': first.name},
        'competitor2': {'id': second.id, 'name': second.name},
        'prelim_score': 12,
        'final_score': None,
        'final_position': None,
    }
    event.event_state = json.dumps({
        'stage': 'prelims',
        'pairs': [pair],
        'prelim_results': [pair],
        'finalists': [],
        'final_results': [],
    })
    db_session.flush()

    blockers = get_pre_generation_blocking_issues(
        build_preflight_report(tournament)
    )
    assert 'partnered_axe_finals_not_advanced' in {
        issue['code'] for issue in blockers
    }


def test_partnered_axe_preflight_does_not_reopen_completed_pair_state(db_session):
    from services.preflight import (
        build_preflight_report,
        get_pre_generation_blocking_issues,
    )

    tournament = make_tournament(db_session, name='Completed Partnered Axe Gate')
    event = make_event(
        db_session,
        tournament,
        'Partnered Axe Throw',
        is_partnered=True,
        has_prelims=True,
        stand_type='axe_throw',
        scoring_type='score',
        scoring_order='highest_wins',
        status='completed',
    )
    event.is_finalized = True
    make_pro_competitor(
        db_session, tournament, 'Historical Axe Entrant', events=[event.id]
    )
    event.event_state = json.dumps({
        'stage': 'completed',
        'pairs': [],
        'prelim_results': [],
        'finalists': [],
        'final_results': [],
    })
    db_session.flush()

    codes = {
        issue['code']
        for issue in get_pre_generation_blocking_issues(
            build_preflight_report(tournament)
        )
    }
    assert 'partnered_axe_pair_state_invalid' not in codes
    assert 'partnered_axe_prelims_incomplete' not in codes
    assert 'partnered_axe_finals_not_advanced' not in codes


def test_full_show_service_runs_every_phase_in_order(db_session, monkeypatch):
    from models import Heat
    from services.schedule_generation import generate_tournament_schedule_artifacts

    tournament = make_tournament(db_session, name='Ordered Full Show')
    event = make_event(db_session, tournament, 'Underhand')
    calls: list[str] = []
    _clear_readiness(monkeypatch, calls)

    def _generate(candidate, *, allow_flight_replacement=False):
        assert allow_flight_replacement is True
        calls.append(f'heat:{candidate.id}')
        db.session.add(Heat(event_id=candidate.id, heat_number=1, run_number=1))
        db.session.flush()
        return 1

    monkeypatch.setattr('services.heat_generator.generate_event_heats', _generate)
    monkeypatch.setattr(
        'services.flight_builder.build_pro_flights',
        lambda *_args, **_kwargs: calls.append('flights') or 1,
    )
    monkeypatch.setattr(
        'services.flight_builder.integrate_proam_relay_into_final_flight',
        lambda *_args, **_kwargs: calls.append('relay') or {'placed': True},
    )
    monkeypatch.setattr(
        'services.flight_builder.integrate_college_spillover_into_flights',
        lambda *_args, **_kwargs: calls.append('spillover')
        or {'integrated_heats': 2},
    )
    monkeypatch.setattr(
        'services.saw_block_assignment.assign_saw_blocks',
        lambda *_args, **_kwargs: calls.append('saw_blocks')
        or {'heats_updated': 3},
    )

    result = generate_tournament_schedule_artifacts(tournament.id)

    assert result['ok'] is True
    assert result['generated'] == 1
    assert result['flights'] == 1
    assert result['relay']['placed'] is True
    assert result['spillover']['integrated_heats'] == 2
    assert result['saw_blocks']['heats_updated'] == 3
    assert calls == [
        'preflight:1',
        f'heat:{event.id}',
        'flights',
        'relay',
        'spillover',
        'saw_blocks',
        'preflight:2',
    ]


def test_pre_generation_blocker_prevents_any_schedule_mutation(
    db_session, monkeypatch
):
    from services.schedule_generation import (
        ScheduleReadinessError,
        generate_tournament_schedule_artifacts,
    )

    tournament = make_tournament(db_session, name='Blocked Full Show')
    event = make_event(db_session, tournament, 'Partnered Event', is_partnered=True)
    existing = make_heat(db_session, event, heat_number=7)
    db_session.commit()

    report = {
        'issues': [{
            'code': 'missing_partner_name',
            'title': 'Partner declaration is blank',
            'detail': 'Partnered Event: Alex Blank needs a partner.',
            'event_ids': [event.id],
        }]
    }
    monkeypatch.setattr(
        'services.preflight.build_preflight_report', lambda *_args: report
    )
    monkeypatch.setattr(
        'services.heat_generator.generate_event_heats',
        lambda *_args, **_kwargs: pytest.fail('generation ran despite readiness blocker'),
    )

    with pytest.raises(ScheduleReadinessError) as exc_info:
        generate_tournament_schedule_artifacts(tournament.id)

    assert exc_info.value.phase == 'pre_generation'
    assert exc_info.value.issue_codes == ('missing_partner_name',)
    assert 'Preflight' in str(exc_info.value)
    assert db.session.get(type(existing), existing.id) is existing
    assert existing.heat_number == 7


def test_full_show_preserves_finalized_and_scored_events(db_session, monkeypatch):
    from models import EventResult, Heat
    from services.schedule_generation import generate_tournament_schedule_artifacts

    tournament = make_tournament(db_session, name='Protected History')
    finalized = make_event(db_session, tournament, 'Finalized', event_type='college')
    finalized.is_finalized = True
    scored = make_event(db_session, tournament, 'Scored', event_type='college')
    pending = make_event(db_session, tournament, 'Pending', event_type='college')
    finalized_heat = make_heat(db_session, finalized, heat_number=4)
    scored_heat = make_heat(db_session, scored, heat_number=5)
    db_session.add(EventResult(
        event_id=scored.id,
        competitor_id=123,
        competitor_type='college',
        competitor_name='Historical Competitor',
        status='completed',
        result_value=10.0,
    ))
    db_session.flush()
    _clear_readiness(monkeypatch)
    generated_ids: list[int] = []

    def _generate(event, *, allow_flight_replacement=False):
        generated_ids.append(event.id)
        db.session.add(Heat(event_id=event.id, heat_number=1, run_number=1))
        db.session.flush()
        return 1

    monkeypatch.setattr('services.heat_generator.generate_event_heats', _generate)
    monkeypatch.setattr(
        'services.saw_block_assignment.assign_saw_blocks',
        lambda *_args, **_kwargs: {'heats_updated': 0},
    )

    result = generate_tournament_schedule_artifacts(tournament.id)

    assert generated_ids == [pending.id]
    assert {item['event_id'] for item in result['protected']} == {
        finalized.id,
        scored.id,
    }
    assert db.session.get(Heat, finalized_heat.id).heat_number == 4
    assert db.session.get(Heat, scored_heat.id).heat_number == 5


def test_partnered_axe_prelim_scores_allow_final_card_build_until_finals_start(
    db_session,
    monkeypatch,
):
    from models import EventResult, Heat
    from services.flight_builder import _prepare_partnered_axe_show_heats
    from services.schedule_generation import (
        _protected_event_reason,
        generate_tournament_schedule_artifacts,
    )

    tournament = make_tournament(db_session, name='Partnered Axe Final Card')
    axe_event = make_event(
        db_session,
        tournament,
        'Partnered Axe Throw',
        is_partnered=True,
        has_prelims=True,
        stand_type='axe_throw',
        scoring_type='score',
        scoring_order='highest_wins',
    )
    pairs = []
    for index in range(4):
        first = make_pro_competitor(
            db_session,
            tournament,
            f'Axe {index}A',
            events=[axe_event.id],
        )
        second = make_pro_competitor(
            db_session,
            tournament,
            f'Axe {index}B',
            events=[axe_event.id],
        )
        pair = {
            'pair_id': index + 1,
            'competitor1': {'id': first.id, 'name': first.name},
            'competitor2': {'id': second.id, 'name': second.name},
            'prelim_score': 20 - index,
            'final_score': None,
            'final_position': None,
        }
        pairs.append(pair)
        for competitor in (first, second):
            db_session.add(EventResult(
                event_id=axe_event.id,
                competitor_id=competitor.id,
                competitor_type='pro',
                competitor_name=competitor.name,
                result_value=pair['prelim_score'],
                status='completed',
            ))
    axe_event.event_state = json.dumps({
        'stage': 'finals',
        'pairs': pairs,
        'prelim_results': pairs,
        'finalists': pairs,
        'final_results': [],
    })

    normal_event = make_event(
        db_session,
        tournament,
        'Underhand',
        stand_type='underhand',
        max_stands=2,
    )
    make_pro_competitor(
        db_session,
        tournament,
        'Regular Show Cutter',
        events=[normal_event.id],
    )
    db_session.flush()
    _clear_readiness(monkeypatch)

    assert _protected_event_reason(axe_event) is None

    def _build_flights(*_args, **_kwargs):
        _prepare_partnered_axe_show_heats(axe_event)
        return 1

    monkeypatch.setattr('services.flight_builder.build_pro_flights', _build_flights)
    monkeypatch.setattr(
        'services.flight_builder.integrate_proam_relay_into_final_flight',
        lambda *_args, **_kwargs: {'placed': False, 'reason': 'not_configured'},
    )
    monkeypatch.setattr(
        'services.flight_builder.integrate_college_spillover_into_flights',
        lambda *_args, **_kwargs: {'integrated_heats': 0},
    )
    monkeypatch.setattr(
        'services.saw_block_assignment.assign_saw_blocks',
        lambda *_args, **_kwargs: {'heats_updated': 0},
    )

    result = generate_tournament_schedule_artifacts(tournament.id)

    assert axe_event.id not in {item['event_id'] for item in result['protected']}
    assert Heat.query.filter_by(event_id=axe_event.id, run_number=1).count() == 4

    state = json.loads(axe_event.event_state)
    state['finalists'][0]['final_score'] = 25
    axe_event.event_state = json.dumps(state)
    db_session.flush()
    assert _protected_event_reason(axe_event) == 'scored finals'


def test_post_generation_blocker_rolls_back_the_entire_build(
    db_session, monkeypatch
):
    from models import Heat
    from services.schedule_generation import (
        ScheduleReadinessError,
        generate_tournament_schedule_artifacts,
    )

    tournament = make_tournament(db_session, name='Post-Build Rollback')
    event = make_event(db_session, tournament, 'College Underhand', event_type='college')
    existing = make_heat(db_session, event, heat_number=9)
    existing_id = existing.id
    db_session.commit()
    report_count = 0

    def _report(*_args):
        nonlocal report_count
        report_count += 1
        if report_count == 1:
            return {'issues': []}
        return {'issues': [{
            'code': 'invalid_flight_position',
            'title': 'Generated flight position is invalid',
            'detail': 'College Underhand has an invalid generated position.',
            'event_id': event.id,
        }]}

    def _replace(candidate, *, allow_flight_replacement=False):
        current = Heat.query.filter_by(event_id=candidate.id).one()
        current.heat_number = 1
        db.session.flush()
        return 1

    monkeypatch.setattr('services.preflight.build_preflight_report', _report)
    monkeypatch.setattr('services.heat_generator.generate_event_heats', _replace)
    monkeypatch.setattr(
        'services.saw_block_assignment.assign_saw_blocks',
        lambda *_args, **_kwargs: {'heats_updated': 0},
    )

    with pytest.raises(ScheduleReadinessError) as exc_info:
        generate_tournament_schedule_artifacts(tournament.id)

    assert exc_info.value.phase == 'post_generation'
    restored = db.session.get(Heat, existing_id)
    assert restored is not None
    assert restored.heat_number == 9


def test_one_click_route_delegates_to_full_show_service(
    db_session, auth_client, monkeypatch
):
    tournament = make_tournament(db_session, name='Delegated One Click')
    calls: list[int] = []

    def _generate(tournament_id, **_kwargs):
        calls.append(tournament_id)
        return {
            'ok': True,
            'generated': 0,
            'skipped': [],
            'protected': [],
            'flights': None,
            'relay': {'placed': False},
            'spillover': {'integrated_heats': 0},
            'saw_blocks': {'heats_updated': 0},
        }

    monkeypatch.setattr(
        'routes.scheduling.flights.generate_tournament_schedule_artifacts',
        _generate,
    )

    response = auth_client.post(
        f'/scheduling/{tournament.id}/flights/one-click-generate',
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)
    assert calls == [tournament.id]


def test_run_show_route_delegates_to_full_show_service(
    db_session, auth_client, monkeypatch
):
    tournament = make_tournament(db_session, name='Delegated Run Show')
    calls: list[tuple[int, dict | None]] = []

    def _generate(tournament_id, *, flight_sizing=None):
        calls.append((tournament_id, flight_sizing))
        return {
            'ok': True,
            'generated': 0,
            'skipped_events': [],
            'protected': [],
            'flights': None,
            'relay': {'placed': False},
            'spillover': {'integrated_heats': 0},
            'build_diff': {
                'before_flight_count': 0,
                'after_flight_count': 0,
                'total_heats': 0,
            },
        }

    monkeypatch.setattr(
        'routes.scheduling.events.generate_tournament_schedule_artifacts',
        _generate,
    )

    response = auth_client.post(
        f'/scheduling/{tournament.id}/events',
        data={
            'action': 'generate_all',
            'flight_sizing_mode': 'count',
            'num_flights': '3',
        },
        follow_redirects=False,
    )

    assert response.status_code in (302, 303)
    assert calls == [(
        tournament.id,
        {
            'mode': 'count',
            'target_minutes_per_flight': 60,
            'minutes_per_heat': 5.5,
            'num_flights': 3,
            'requested_num_flights': 3,
        },
    )]


def test_async_route_submits_the_shared_full_show_service(
    db_session, auth_client, monkeypatch
):
    from services.schedule_generation import generate_tournament_schedule_artifacts

    tournament = make_tournament(db_session, name='Delegated Background Show')
    submitted = {}

    def _submit(label, fn, *args, metadata=None, **kwargs):
        submitted.update({
            'label': label,
            'fn': fn,
            'args': args,
            'metadata': metadata,
            'kwargs': kwargs,
        })
        return 'job-123'

    monkeypatch.setattr('routes.scheduling.preflight.submit_job', _submit)

    response = auth_client.post(
        f'/scheduling/{tournament.id}/events/generate-async'
    )

    assert response.status_code == 202
    assert submitted == {
        'label': f'generate_all:{tournament.id}',
        'fn': generate_tournament_schedule_artifacts,
        'args': (tournament.id,),
        'metadata': {
            'tournament_id': tournament.id,
            'kind': 'generate_all',
        },
        'kwargs': {},
    }


def test_job_polling_reports_legacy_ok_false_payload_as_failed(
    db_session, auth_client, monkeypatch
):
    tournament = make_tournament(db_session, name='Failed Background Show')
    monkeypatch.setattr(
        'services.background_jobs.get',
        lambda _job_id: {
            'id': 'job-failed',
            'status': 'completed',
            'result': {
                'ok': False,
                'message': 'Schedule readiness blocked. Open Preflight.',
            },
            'error': None,
            'metadata': {'tournament_id': tournament.id},
        },
    )

    response = auth_client.get(
        f'/scheduling/{tournament.id}/events/job-status/job-failed'
    )

    assert response.status_code == 200
    assert response.get_json()['status'] == 'failed'
    assert 'Preflight' in response.get_json()['error']
