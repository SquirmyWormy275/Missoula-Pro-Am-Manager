"""Tournament workflow regressions reproduced during the October 2026 review."""
import json

import pytest

from models import EventResult, User
from services.gear_sharing import (
    build_name_index,
    cleanup_scratched_gear_entries,
    competitors_share_gear_for_event,
    fix_heat_gear_conflicts,
    gear_partner_names,
    parse_gear_sharing_details,
)
from services.heat_generator import HeatGenerationSafetyError, generate_event_heats
from services.scoring_workflow import finalize_event_results, save_heat_results_submission
from tests.conftest import make_event, make_heat, make_pro_competitor, make_tournament


def _save(heat, values):
    return save_heat_results_submission(
        tournament_id=heat.event.tournament_id,
        heat=heat,
        event=heat.event,
        form_data={'heat_version': str(heat.version_id), **values},
        judge_user_id=7,
    )


@pytest.mark.parametrize('status', ['dnf', 'dq', 'scratched'])
@pytest.mark.parametrize('scoring_type', ['time', 'hits', 'score'])
def test_non_finish_without_a_numeric_score_is_recorded(db_session, status, scoring_type):
    tournament = make_tournament(db_session)
    event = make_event(
        db_session, tournament, 'Non-finish', scoring_type=scoring_type,
        requires_triple_runs=scoring_type == 'score',
    )
    competitor = make_pro_competitor(db_session, tournament, 'Non-finisher')
    finisher = make_pro_competitor(db_session, tournament, 'Finisher')
    heat = make_heat(db_session, event, competitors=[competitor.id, finisher.id])
    numeric_values = (
        {f't1_run1_{finisher.id}': '10', f't2_run1_{finisher.id}': '10'}
        if scoring_type == 'time' else {f'result_{finisher.id}': '10'}
    )

    outcome = _save(heat, {
        f'status_{competitor.id}': status,
        f'reason_{competitor.id}': 'Unable to finish',
        **numeric_values,
    })

    row = EventResult.query.filter_by(event_id=event.id, competitor_id=competitor.id).one()
    assert outcome['ok'] is True
    assert row.status == status
    assert row.status_reason == 'Unable to finish'
    assert row.result_value is None
    assert heat.status == 'completed'
    assert event.is_finalized is True


def test_all_non_finishes_can_complete_a_heat_without_invented_times(db_session):
    tournament = make_tournament(db_session)
    event = make_event(db_session, tournament, 'All DNF')
    competitor = make_pro_competitor(db_session, tournament, 'Only entrant')
    heat = make_heat(db_session, event, competitors=[competitor.id])

    outcome = _save(heat, {f'status_{competitor.id}': 'dnf'})

    assert outcome['ok'] is True
    assert heat.status == 'completed'
    assert EventResult.query.filter_by(event_id=event.id).one().result_value is None
    assert event.is_finalized is False


@pytest.mark.parametrize('scoring_type', ['time', 'hits', 'score'])
def test_blank_entrant_keeps_heat_open_until_scored(db_session, scoring_type):
    tournament = make_tournament(db_session)
    event = make_event(
        db_session, tournament, 'Incomplete heat', scoring_type=scoring_type,
        requires_triple_runs=scoring_type == 'score',
    )
    first, second = [
        make_pro_competitor(db_session, tournament, name)
        for name in ('Finished entrant', 'Missing entrant')
    ]
    heat = make_heat(db_session, event, competitors=[first.id, second.id])

    def values(competitor, value):
        if scoring_type == 'time':
            return {f't1_run1_{competitor.id}': value, f't2_run1_{competitor.id}': value}
        return {f'result_{competitor.id}': value}

    outcome = _save(heat, values(first, '10'))

    assert outcome['ok'] is True
    assert outcome['category'] == 'warning'
    assert second.name in outcome['message']
    assert heat.status != 'completed'
    assert event.is_finalized is False
    assert EventResult.query.filter_by(event_id=event.id, competitor_id=first.id).one().status == 'completed'
    assert EventResult.query.filter_by(event_id=event.id, competitor_id=second.id).first() is None

    finalization = finalize_event_results(event=event, tournament_id=tournament.id, judge_user_id=7)
    assert finalization['ok'] is False
    assert event.is_finalized is False

    outcome = _save(heat, values(second, '12'))
    assert outcome['ok'] is True
    assert heat.status == 'completed'
    assert event.is_finalized is True
    assert EventResult.query.filter_by(event_id=event.id).count() == 2


def test_run_one_score_does_not_hide_missing_run_two(db_session):
    tournament = make_tournament(db_session)
    event = make_event(db_session, tournament, 'Speed Climb', requires_dual_runs=True)
    first, second = [
        make_pro_competitor(db_session, tournament, name)
        for name in ('Dual first', 'Dual second')
    ]
    run_one = make_heat(db_session, event, competitors=[first.id, second.id])
    run_two = make_heat(db_session, event, run_number=2, competitors=[first.id, second.id])
    _save(run_one, {
        f't{timer}_run1_{competitor.id}': str(10 + index)
        for index, competitor in enumerate((first, second)) for timer in (1, 2)
    })

    outcome = _save(run_two, {f't1_run2_{first.id}': '9', f't2_run2_{first.id}': '9'})

    assert outcome['ok'] is True
    assert run_two.status != 'completed'
    assert event.is_finalized is False
    assert EventResult.query.filter_by(event_id=event.id, competitor_id=second.id).one().run2_value is None

    _save(run_two, {f'status_{second.id}': 'dnf', f'reason_{second.id}': 'Injury'})
    assert run_two.status == 'completed'
    assert event.is_finalized is True


def test_run_one_partial_does_not_account_for_an_unentered_second_run(db_session):
    tournament = make_tournament(db_session)
    event = make_event(db_session, tournament, 'Speed Climb', requires_dual_runs=True)
    first, second = [
        make_pro_competitor(db_session, tournament, name)
        for name in ('Partial dual first', 'Partial dual second')
    ]
    run_one = make_heat(db_session, event, competitors=[first.id, second.id])
    run_two = make_heat(db_session, event, run_number=2, competitors=[first.id, second.id])
    _save(run_one, {
        f't1_run1_{first.id}': '10', f't2_run1_{first.id}': '10',
        f't1_run1_{second.id}': '11',
    })

    outcome = _save(run_two, {f't1_run2_{first.id}': '9', f't2_run2_{first.id}': '9'})

    assert outcome['ok'] is True
    assert second.name in outcome['message']
    assert run_two.status != 'completed'
    assert event.is_finalized is False
    assert finalize_event_results(
        event=event, tournament_id=tournament.id, judge_user_id=7,
    )['ok'] is False

    # A current-run partial still follows the established manual review path.
    _save(run_two, {f't1_run2_{second.id}': '12'})
    assert run_two.status == 'completed'
    assert event.is_finalized is False
    assert finalize_event_results(
        event=event, tournament_id=tournament.id, judge_user_id=7,
    )['ok'] is True


def test_scored_second_run_preserves_a_recorded_first_run_partial(db_session):
    tournament = make_tournament(db_session)
    event = make_event(db_session, tournament, 'Speed Climb', requires_dual_runs=True)
    competitor = make_pro_competitor(db_session, tournament, 'Recorded partial')
    run_one = make_heat(db_session, event, competitors=[competitor.id])
    run_two = make_heat(db_session, event, run_number=2, competitors=[competitor.id])
    _save(run_one, {f't1_run1_{competitor.id}': '10'})

    outcome = _save(run_two, {
        f't1_run2_{competitor.id}': '12', f't2_run2_{competitor.id}': '12',
    })

    row = EventResult.query.filter_by(event_id=event.id, competitor_id=competitor.id).one()
    assert outcome['ok'] is True
    assert row.t1_run1 == 10
    assert row.run1_value is None
    assert row.run2_value == 12
    assert run_one.status == run_two.status == 'completed'
    assert event.is_finalized is True


def test_parsed_multi_partner_gear_can_generate_safe_heats(db_session):
    tournament = make_tournament(db_session)
    event = make_event(db_session, tournament, 'Underhand', max_stands=3)
    owner, first, second = [
        make_pro_competitor(db_session, tournament, name, events=[event.name])
        for name in ('Gear owner', 'Gear first', 'Gear second')
    ]
    parsed, warnings = parse_gear_sharing_details(
        'SHARING Underhand with Gear first and Gear second', [event],
        build_name_index(c.name for c in (owner, first, second)),
        self_name=owner.name, entered_event_names=[event.name],
    )
    assert not warnings
    owner.gear_sharing = json.dumps(parsed)
    generate_event_heats(event)

    rosters = [set(heat.get_competitors()) for heat in event.heats.all()]
    assert set().union(*rosters) == {owner.id, first.id, second.id}
    for partner in (first, second):
        assert all(not {owner.id, partner.id} <= roster for roster in rosters)


@pytest.mark.parametrize('bad_partner', ['Unknown person', 'Gear owner'])
def test_invalid_name_inside_multi_partner_gear_preserves_heats(db_session, bad_partner):
    tournament = make_tournament(db_session)
    event = make_event(db_session, tournament, 'Underhand')
    owner = make_pro_competitor(
        db_session, tournament, 'Gear owner', events=[event.name],
        gear_sharing={str(event.id): f'partners:Valid partner|{bad_partner}'},
    )
    partner = make_pro_competitor(db_session, tournament, 'Valid partner', events=[event.name])
    heat = make_heat(db_session, event, competitors=[owner.id, partner.id])
    original_id, original_roster = heat.id, heat.get_competitors()

    with pytest.raises(HeatGenerationSafetyError, match='gear declaration'):
        generate_event_heats(event)

    assert [h.id for h in event.heats.all()] == [original_id]
    assert heat.get_competitors() == original_roster


def test_inline_gear_edit_preserves_the_partners_existing_declarations(app, db_session):
    tournament = make_tournament(db_session)
    event = make_event(db_session, tournament, 'Underhand')
    owner, first, second, newcomer = [
        make_pro_competitor(db_session, tournament, name, events=[event.name])
        for name in ('Inline owner', 'Inline first', 'Inline second', 'Inline newcomer')
    ]
    parsed, _ = parse_gear_sharing_details(
        'SHARING Underhand with Inline first and Inline second', [event],
        build_name_index(c.name for c in (owner, first, second, newcomer)),
        self_name=owner.name, entered_event_names=[event.name],
    )
    owner.gear_sharing = json.dumps(parsed)
    admin = User(username=f'review_admin_{tournament.id}', role='admin')
    admin.set_password('review_password')
    db_session.add(admin)
    db_session.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(admin.id)

    response = client.post(f'/registration/{tournament.id}/pro/gear-sharing/update-ajax', data={
        'competitor_id': newcomer.id, 'event_key': str(event.id), 'partner_name': owner.name,
    })

    assert response.status_code == 200
    db_session.refresh(owner)
    assert set(gear_partner_names(owner.get_gear_sharing()[str(event.id)])) == {
        first.name, second.name, newcomer.name,
    }
    for partner in (first, second, newcomer):
        assert competitors_share_gear_for_event(
            owner.name, owner.get_gear_sharing(), partner.name, partner.get_gear_sharing(), event,
        )


@pytest.mark.parametrize('using', [False, True])
def test_scratch_cleanup_removes_only_the_scratched_name(db_session, using):
    tournament = make_tournament(db_session)
    event = make_event(db_session, tournament, 'Underhand')
    prefix = 'using:' if using else ''
    owner = make_pro_competitor(
        db_session, tournament, 'Cleanup owner',
        gear_sharing={str(event.id): f'{prefix}partners:Scratched partner|Active partner'},
    )
    scratched = make_pro_competitor(db_session, tournament, 'Scratched partner', status='scratched')
    active = make_pro_competitor(db_session, tournament, 'Active partner')

    result = cleanup_scratched_gear_entries(tournament, scratched_competitor=scratched)

    assert result == {'cleaned': 1, 'affected': [owner.name]}
    assert owner.get_gear_sharing()[str(event.id)] == prefix + active.name
    assert cleanup_scratched_gear_entries(tournament)['cleaned'] == 0

    active.status = 'scratched'
    cleanup_scratched_gear_entries(tournament)
    assert owner.get_gear_sharing() == {}


@pytest.mark.parametrize('stand_four_occupied', [False, True])
def test_gear_repair_respects_the_left_handed_dummy(db_session, stand_four_occupied):
    tournament = make_tournament(db_session)
    event = make_event(db_session, tournament, 'Springboard', stand_type='springboard', max_stands=4)
    first = make_pro_competitor(db_session, tournament, 'Right-hand source',
        gear_sharing={str(event.id): 'Left-hand mover'})
    mover = make_pro_competitor(db_session, tournament, 'Left-hand mover', is_left_handed_springboard=True)
    target_comp = make_pro_competitor(db_session, tournament, 'Target cutter',
        is_left_handed_springboard=stand_four_occupied)
    source = make_heat(db_session, event, competitors=[first.id, mover.id],
        stand_assignments={str(first.id): 1, str(mover.id): 4})
    target = make_heat(db_session, event, heat_number=2, competitors=[target_comp.id],
        stand_assignments={str(target_comp.id): 4 if stand_four_occupied else 1})
    before = [(heat.get_competitors(), heat.get_stand_assignments()) for heat in (source, target)]

    result = fix_heat_gear_conflicts(tournament)

    if stand_four_occupied:
        assert result['fixed'] == 0
        assert result['failed']
        assert [(h.get_competitors(), h.get_stand_assignments()) for h in (source, target)] == before
    else:
        assert result == {'fixed': 1, 'failed': []}
        assert source.get_competitors() == [first.id]
        assert target.get_stand_assignments()[str(mover.id)] == 4


def test_inline_gear_edit_joins_an_existing_group_without_erasing_it(app, db_session):
    tournament = make_tournament(db_session)
    event = make_event(db_session, tournament, 'Group sharing')
    owner = make_pro_competitor(db_session, tournament, 'Group owner',
        gear_sharing={str(event.id): 'group:shared-axe'})
    other_member = make_pro_competitor(db_session, tournament, 'Existing group member',
        gear_sharing={str(event.id): 'group:shared-axe'})
    newcomer = make_pro_competitor(db_session, tournament, 'New group member')
    admin = User(username=f'review_group_admin_{tournament.id}', role='admin')
    admin.set_password('review_password')
    db_session.add(admin)
    db_session.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(admin.id)

    response = client.post(f'/registration/{tournament.id}/pro/gear-sharing/update-ajax', data={
        'competitor_id': newcomer.id, 'event_key': str(event.id), 'partner_name': owner.name,
    })

    assert response.status_code == 200
    for competitor in (owner, other_member, newcomer):
        db_session.refresh(competitor)
        assert competitor.get_gear_sharing()[str(event.id)] == 'group:shared-axe'
    assert competitors_share_gear_for_event(
        newcomer.name, newcomer.get_gear_sharing(), other_member.name, other_member.get_gear_sharing(), event,
    )


def test_gear_repair_preserves_dual_run_stand_alternation(db_session):
    tournament = make_tournament(db_session)
    event = make_event(db_session, tournament, 'Speed Climb', stand_type='speed_climb',
        max_stands=2, requires_dual_runs=True)
    first = make_pro_competitor(db_session, tournament, 'Dual repair first',
        gear_sharing={str(event.id): 'Dual repair mover'})
    mover = make_pro_competitor(db_session, tournament, 'Dual repair mover')
    make_heat(db_session, event, competitors=[mover.id], status='completed',
        stand_assignments={str(mover.id): 1})
    source = make_heat(db_session, event, run_number=2, competitors=[first.id, mover.id],
        stand_assignments={str(first.id): 1, str(mover.id): 2})
    target = make_heat(db_session, event, heat_number=2, run_number=2)

    result = fix_heat_gear_conflicts(tournament)

    assert result == {'fixed': 1, 'failed': []}
    assert mover.id not in source.get_competitors()
    assert target.get_stand_assignments()[str(mover.id)] == 2
