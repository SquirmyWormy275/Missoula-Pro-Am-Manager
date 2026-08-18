"""
DB integration tests for heat generation — end-to-end.

The companion file test_heat_generator.py covers pure helper functions.
This file exercises the DB-dependent paths: generate_event_heats(),
_get_event_competitors(), _sort_by_ability(), and the Heat/EventResult
rows they create.

Run:
    pytest tests/test_heat_gen_integration.py -v

Requirements:
    pytest (pip install pytest)
    All app dependencies installed.
"""
import json
import math
import warnings

import pytest
from sqlalchemy.exc import SAWarning

from database import db as _db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def app():
    """Create a test Flask app with temp-file SQLite built via flask db upgrade."""
    import os

    from tests.db_test_utils import create_test_app
    _app, db_path = create_test_app()

    with _app.app_context():
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def db_session(app):
    """Wrap each test in a transaction and roll back afterward."""
    with app.app_context():
        _db.session.begin_nested()
        yield _db.session
        _db.session.rollback()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _make_tournament(session, name='Heat Gen Test 2026', year=2026):
    from models import Tournament
    t = Tournament(name=name, year=year, status='setup')
    session.add(t)
    session.flush()
    return t


def _make_team(session, tournament, code='UM-A', school='University of Montana', abbrev='UM'):
    from models import Team
    t = Team(
        tournament_id=tournament.id,
        team_code=code,
        school_name=school,
        school_abbreviation=abbrev,
    )
    session.add(t)
    session.flush()
    return t


def _make_event(session, tournament, name='Underhand', event_type='pro',
                gender='M', scoring_type='time', stand_type='underhand',
                max_stands=None, is_partnered=False, requires_dual_runs=False,
                partner_gender_requirement=None):
    from models import Event
    e = Event(
        tournament_id=tournament.id,
        name=name,
        event_type=event_type,
        gender=gender,
        scoring_type=scoring_type,
        stand_type=stand_type,
        max_stands=max_stands,
        is_partnered=is_partnered,
        requires_dual_runs=requires_dual_runs,
        partner_gender_requirement=partner_gender_requirement,
    )
    session.add(e)
    session.flush()
    return e


def _make_pro(session, tournament, name, gender='M', event_ids=None,
              is_left_handed=False, slow_heat=False, gear_sharing=None,
              partners=None, status='active'):
    from models import ProCompetitor
    c = ProCompetitor(
        tournament_id=tournament.id,
        name=name,
        gender=gender,
        status=status,
        is_left_handed_springboard=is_left_handed,
        springboard_slow_heat=slow_heat,
    )
    if event_ids:
        c.set_events_entered(event_ids)
    if gear_sharing:
        c.gear_sharing = json.dumps(gear_sharing)
    if partners:
        c.partners = json.dumps(partners)
    session.add(c)
    session.flush()
    return c


def _make_college(session, tournament, team, name, gender='M',
                  event_ids=None, gear_sharing=None, partners=None, status='active'):
    from models import CollegeCompetitor
    c = CollegeCompetitor(
        tournament_id=tournament.id,
        team_id=team.id,
        name=name,
        gender=gender,
        status=status,
    )
    if event_ids:
        c.set_events_entered(event_ids)
    if gear_sharing:
        c.gear_sharing = json.dumps(gear_sharing)
    if partners:
        c.partners = json.dumps(partners)
    session.add(c)
    session.flush()
    return c


def _all_heats_for_event(event_id, run_number=None):
    """Return all Heat rows for the given event, optionally filtered by run."""
    from models import Heat
    q = Heat.query.filter_by(event_id=event_id)
    if run_number is not None:
        q = q.filter_by(run_number=run_number)
    return q.order_by(Heat.heat_number, Heat.run_number).all()


def _all_competitor_ids_from_heats(heats):
    """Flatten all competitor IDs from a list of Heat objects."""
    ids = []
    for h in heats:
        ids.extend(h.get_competitors())
    return ids


# ---------------------------------------------------------------------------
# generate_event_heats — simple time event
# ---------------------------------------------------------------------------

class TestGenerateSimpleTimeEvent:
    """Underhand event, 5 stands, 8 pro competitors."""

    def test_correct_number_of_heats(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Underhand', stand_type='underhand',
                         max_stands=5)
        for i in range(8):
            _make_pro(db_session, t, f'Pro {i}', gender='M', event_ids=[ev.id])

        from services.heat_generator import generate_event_heats
        num = generate_event_heats(ev)

        assert num == math.ceil(8 / 5)  # 2 heats
        heats = _all_heats_for_event(ev.id)
        assert len(heats) == 2

    def test_all_competitors_assigned(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Underhand', stand_type='underhand',
                         max_stands=5)
        comp_ids = []
        for i in range(8):
            c = _make_pro(db_session, t, f'Pro {i}', gender='M', event_ids=[ev.id])
            comp_ids.append(c.id)

        from services.heat_generator import generate_event_heats
        generate_event_heats(ev)

        heats = _all_heats_for_event(ev.id)
        assigned = _all_competitor_ids_from_heats(heats)
        assert sorted(assigned) == sorted(comp_ids)

    def test_no_duplicates(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Underhand', stand_type='underhand',
                         max_stands=5)
        for i in range(8):
            _make_pro(db_session, t, f'Pro {i}', gender='M', event_ids=[ev.id])

        from services.heat_generator import generate_event_heats
        generate_event_heats(ev)

        heats = _all_heats_for_event(ev.id)
        assigned = _all_competitor_ids_from_heats(heats)
        assert len(assigned) == len(set(assigned))

    def test_stand_assignments_within_max(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Underhand', stand_type='underhand',
                         max_stands=5)
        for i in range(8):
            _make_pro(db_session, t, f'Pro {i}', gender='M', event_ids=[ev.id])

        from services.heat_generator import generate_event_heats
        generate_event_heats(ev)

        heats = _all_heats_for_event(ev.id)
        for h in heats:
            comps = h.get_competitors()
            assert len(comps) <= 5
            assignments = h.get_stand_assignments()
            for stand_num in assignments.values():
                assert 1 <= stand_num <= 5

    def test_event_status_set_to_in_progress(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Underhand', stand_type='underhand',
                         max_stands=5)
        for i in range(3):
            _make_pro(db_session, t, f'Pro {i}', gender='M', event_ids=[ev.id])

        from services.heat_generator import generate_event_heats
        generate_event_heats(ev)

        assert ev.status == 'in_progress'

    def test_gear_conflict_expands_heat_count_without_a_warning(self, db_session):
        """A feasible gear conflict expands instead of producing an unsafe heat."""
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Underhand', stand_type='underhand',
                         max_stands=2)
        first = _make_pro(db_session, t, 'First', event_ids=[ev.id])
        second = _make_pro(db_session, t, 'Second', event_ids=[ev.id])

        from services.heat_generator import generate_event_heats, get_last_gear_violations
        generate_event_heats(ev)

        first.gear_sharing = json.dumps({str(ev.id): 'Second'})
        second.gear_sharing = json.dumps({str(ev.id): 'First'})
        db_session.flush()

        generate_event_heats(ev)

        heats = _all_heats_for_event(ev.id)
        assert len(heats) == 2
        assert sorted(_all_competitor_ids_from_heats(heats)) == sorted([first.id, second.id])
        assert all(len(heat.get_competitors()) == 1 for heat in heats)
        assert get_last_gear_violations(ev.id) == []


# ---------------------------------------------------------------------------
# generate_event_heats — dual-run event (Speed Climb)
# ---------------------------------------------------------------------------

class TestGenerateDualRunEvent:
    """Speed Climb: 2 poles, requires_dual_runs."""

    def test_run1_and_run2_heats_created(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Speed Climb', stand_type='speed_climb',
                         max_stands=2, requires_dual_runs=True)
        for i in range(6):
            _make_pro(db_session, t, f'Climber {i}', gender='M', event_ids=[ev.id])

        from services.heat_generator import generate_event_heats
        num = generate_event_heats(ev)

        run1 = _all_heats_for_event(ev.id, run_number=1)
        run2 = _all_heats_for_event(ev.id, run_number=2)
        assert len(run1) == num
        assert len(run2) == num
        assert num == math.ceil(6 / 2)

    def test_same_competitors_in_both_runs(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Speed Climb', stand_type='speed_climb',
                         max_stands=2, requires_dual_runs=True)
        for i in range(6):
            _make_pro(db_session, t, f'Climber {i}', gender='M', event_ids=[ev.id])

        from services.heat_generator import generate_event_heats
        generate_event_heats(ev)

        run1 = _all_heats_for_event(ev.id, run_number=1)
        run2 = _all_heats_for_event(ev.id, run_number=2)

        for h1, h2 in zip(run1, run2):
            assert h1.heat_number == h2.heat_number
            assert sorted(h1.get_competitors()) == sorted(h2.get_competitors())

    def test_stand_assignments_swapped_between_runs(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Speed Climb', stand_type='speed_climb',
                         max_stands=2, requires_dual_runs=True)
        for i in range(4):
            _make_pro(db_session, t, f'Climber {i}', gender='M', event_ids=[ev.id])

        from services.heat_generator import generate_event_heats
        generate_event_heats(ev)

        run1 = _all_heats_for_event(ev.id, run_number=1)
        run2 = _all_heats_for_event(ev.id, run_number=2)

        for h1, h2 in zip(run1, run2):
            a1 = h1.get_stand_assignments()
            a2 = h2.get_stand_assignments()
            # Run 2 stands should be the reverse of run 1
            if len(a1) == 2:
                stands_r1 = list(a1.values())
                stands_r2 = list(a2.values())
                assert stands_r1 == list(reversed(stands_r2))

    def test_solo_heat_changes_physical_stand_between_runs(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(
            db_session,
            t,
            name='Speed Climb',
            stand_type='speed_climb',
            max_stands=2,
            requires_dual_runs=True,
        )
        climber = _make_pro(
            db_session,
            t,
            'Solo Climber',
            gender='M',
            event_ids=[ev.id],
        )

        from services.heat_generator import generate_event_heats
        generate_event_heats(ev)

        run1 = _all_heats_for_event(ev.id, run_number=1)[0]
        run2 = _all_heats_for_event(ev.id, run_number=2)[0]
        assert run1.get_stand_assignments()[str(climber.id)] == 1
        assert run2.get_stand_assignments()[str(climber.id)] == 2


# ---------------------------------------------------------------------------
# generate_event_heats — partnered event (Double Buck)
# ---------------------------------------------------------------------------

class TestGeneratePartneredEvent:
    """Double Buck: partnered, same-gender, 8 saw stands."""

    def test_partner_units_kept_together(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Double Buck', stand_type='saw_hand',
                         max_stands=4, is_partnered=True,
                         partner_gender_requirement='same')

        # Create 4 pairs (8 competitors), each pair references the other
        pairs = [
            ('Alice A', 'Alice B'),
            ('Bob A', 'Bob B'),
            ('Carol A', 'Carol B'),
            ('Dan A', 'Dan B'),
        ]
        for name_a, name_b in pairs:
            _make_pro(db_session, t, name_a, gender='M',
                      event_ids=[ev.id],
                      partners={str(ev.id): name_b})
            _make_pro(db_session, t, name_b, gender='M',
                      event_ids=[ev.id],
                      partners={str(ev.id): name_a})

        from services.heat_generator import generate_event_heats
        generate_event_heats(ev)

        heats = _all_heats_for_event(ev.id)
        from models import ProCompetitor
        id_to_name = {c.id: c.name for c in ProCompetitor.query.filter_by(tournament_id=t.id).all()}

        # For each heat, verify partners are in the same heat
        for h in heats:
            names_in_heat = {id_to_name[cid] for cid in h.get_competitors()}
            for name_a, name_b in pairs:
                if name_a in names_in_heat:
                    assert name_b in names_in_heat, \
                        f'{name_a} in heat but partner {name_b} is not'


# ---------------------------------------------------------------------------
# generate_event_heats — springboard (left-handed grouping)
# ---------------------------------------------------------------------------

class TestGenerateSpringboardHeats:
    """Springboard: 4 dummies, left-handed cutters spread ONE per heat.

    Rule (2026-04-20): only one physical LH springboard dummy on site, so at
    most one LH cutter per heat.  Spread LH cutters one per heat 0..N-1.
    Expand the event when the original heat count has too few dummy time slots.
    """

    def test_left_handed_spread_one_per_heat(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Springboard', stand_type='springboard',
                         max_stands=4, gender=None)

        # 2 left-handed + 6 right-handed = 8 competitors, need 2 heats
        lefties = []
        for i in range(2):
            c = _make_pro(db_session, t, f'Lefty {i}', gender='M',
                          event_ids=[ev.id], is_left_handed=True)
            lefties.append(c.id)
        for i in range(6):
            _make_pro(db_session, t, f'Righty {i}', gender='M',
                      event_ids=[ev.id], is_left_handed=False)

        from services.heat_generator import generate_event_heats
        generate_event_heats(ev)

        heats = _all_heats_for_event(ev.id, run_number=1)
        # Each lefty should land in a DIFFERENT heat (spread, not clustered).
        lefty_heats = set()
        for h in heats:
            comps = set(h.get_competitors())
            for lid in lefties:
                if lid in comps:
                    lefty_heats.add(h.heat_number)

        assert len(lefty_heats) == 2, (
            f'Left-handed cutters clustered in heats {lefty_heats}; '
            'each LH cutter should land in its own heat.'
        )

    def test_stand_assignments_within_4_dummies(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Springboard', stand_type='springboard',
                         max_stands=4, gender=None)
        for i in range(6):
            _make_pro(db_session, t, f'SB {i}', gender='M', event_ids=[ev.id])

        from services.heat_generator import generate_event_heats
        generate_event_heats(ev)

        heats = _all_heats_for_event(ev.id)
        for h in heats:
            assert len(h.get_competitors()) <= 4
            for stand_num in h.get_stand_assignments().values():
                assert 1 <= stand_num <= 4


# ---------------------------------------------------------------------------
# Gear-sharing conflict avoidance
# ---------------------------------------------------------------------------

class TestGearSharingConflictAvoidance:
    """Gear-sharing competitors should not be placed in the same heat."""

    def test_gear_sharing_partners_separated(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Underhand', stand_type='underhand',
                         max_stands=5)

        # 6 competitors: Alpha shares gear with Beta for this event
        alpha = _make_pro(db_session, t, 'Alpha', gender='M',
                          event_ids=[ev.id],
                          gear_sharing={str(ev.id): 'Beta'})
        beta = _make_pro(db_session, t, 'Beta', gender='M',
                         event_ids=[ev.id],
                         gear_sharing={str(ev.id): 'Alpha'})
        for i in range(4):
            _make_pro(db_session, t, f'Other {i}', gender='M', event_ids=[ev.id])

        from services.heat_generator import generate_event_heats
        generate_event_heats(ev)

        heats = _all_heats_for_event(ev.id, run_number=1)
        for h in heats:
            comps = set(h.get_competitors())
            # Alpha and Beta should not both be in the same heat
            assert not (alpha.id in comps and beta.id in comps), \
                'Gear-sharing partners Alpha and Beta are in the same heat'

    def test_college_team_suffixes_do_not_hide_named_gear_conflicts(self, db_session):
        t = _make_tournament(db_session)
        team = _make_team(db_session, t)
        ev = _make_event(
            db_session,
            t,
            name='Underhand',
            event_type='college',
            gender='M',
            stand_type='underhand',
            max_stands=2,
        )
        first = _make_college(
            db_session,
            t,
            team,
            'Alice',
            event_ids=[ev.id],
            gear_sharing={str(ev.id): 'Bob'},
        )
        second = _make_college(
            db_session,
            t,
            team,
            'Bob',
            event_ids=[ev.id],
            gear_sharing={str(ev.id): 'Alice'},
        )

        from services.heat_generator import generate_event_heats
        generate_event_heats(ev)

        rosters = [set(heat.get_competitors()) for heat in _all_heats_for_event(ev.id)]
        assert len(rosters) == 2
        assert all(not ({first.id, second.id} <= roster) for roster in rosters)

    @pytest.mark.parametrize(
        ('gear_sharing', 'details'),
        [
            ({'Underhand typo': 'Declaration B'}, ''),
            ({'__current_event__': 'Missing Person'}, ''),
            ({}, 'SHARING Underhand axe with Missing Person'),
        ],
        ids=[
            'unmapped-current-event-key',
            'unknown-current-event-partner',
            'unparsed-current-event-details',
        ],
    )
    def test_invalid_event_gear_declaration_preserves_existing_layout(
        self,
        db_session,
        gear_sharing,
        details,
    ):
        t = _make_tournament(db_session)
        ev = _make_event(
            db_session,
            t,
            name='Underhand',
            stand_type='underhand',
            max_stands=2,
        )
        first = _make_pro(
            db_session,
            t,
            'Declaration A',
            event_ids=[ev.id],
            gear_sharing=gear_sharing,
        )
        second = _make_pro(
            db_session,
            t,
            'Declaration B',
            event_ids=[ev.id],
        )
        if '__current_event__' in gear_sharing:
            first.gear_sharing = json.dumps({
                str(ev.id): gear_sharing['__current_event__'],
            })
        first.gear_sharing_details = details

        from models import Heat
        old_heat = Heat(event_id=ev.id, heat_number=8, run_number=1)
        old_heat.set_roster(
            'pro',
            [first.id, second.id],
            {first.id: 1, second.id: 2},
        )
        db_session.add(old_heat)
        db_session.flush()
        old_heat_id = old_heat.id

        from services.heat_generator import (
            HeatGenerationSafetyError,
            generate_event_heats,
        )
        with pytest.raises(HeatGenerationSafetyError, match='gear declaration'):
            generate_event_heats(ev)

        remaining = _all_heats_for_event(ev.id)
        assert [heat.id for heat in remaining] == [old_heat_id]
        assert remaining[0].heat_number == 8
        assert remaining[0].get_competitors() == [first.id, second.id]

    def test_invalid_gear_declaration_for_another_event_does_not_block(
        self,
        db_session,
    ):
        t = _make_tournament(db_session)
        underhand = _make_event(
            db_session,
            t,
            name='Underhand',
            stand_type='underhand',
            max_stands=2,
        )
        _make_event(
            db_session,
            t,
            name='Hot Saw',
            stand_type='hot_saw',
            max_stands=2,
        )
        competitor = _make_pro(
            db_session,
            t,
            'Scoped Declaration',
            event_ids=[underhand.id],
            gear_sharing={'Hot Saw typo': 'Missing Person'},
        )

        from services.heat_generator import generate_event_heats
        generate_event_heats(underhand)

        heats = _all_heats_for_event(underhand.id)
        assert len(heats) == 1
        assert heats[0].get_competitors() == [competitor.id]

    def test_minimal_heat_conflict_expands_to_a_safe_layout(self, db_session):
        """Two sharers on a five-stand event run in separate heats."""
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Underhand', stand_type='underhand',
                         max_stands=5)

        # 2 competitors that share gear — only 1 heat needed
        _make_pro(db_session, t, 'Sharer A', gender='M',
                  event_ids=[ev.id],
                  gear_sharing={str(ev.id): 'Sharer B'})
        _make_pro(db_session, t, 'Sharer B', gender='M',
                  event_ids=[ev.id],
                  gear_sharing={str(ev.id): 'Sharer A'})

        from services.heat_generator import generate_event_heats, get_last_gear_violations
        generate_event_heats(ev)

        heats = _all_heats_for_event(ev.id)
        assert len(heats) == 2
        assert all(len(heat.get_competitors()) == 1 for heat in heats)
        assert get_last_gear_violations(ev.id) == []

    def test_partnered_gear_group_expands_without_splitting_pairs(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(
            db_session,
            t,
            name='Double Buck',
            stand_type='saw_hand',
            max_stands=4,
            is_partnered=True,
            partner_gender_requirement='same',
        )
        pairs = [('Pair A1', 'Pair A2'), ('Pair B1', 'Pair B2')]
        expected_pairs = []
        for first_name, second_name in pairs:
            first = _make_pro(
                db_session,
                t,
                first_name,
                event_ids=[ev.id],
                partners={str(ev.id): second_name},
                gear_sharing={str(ev.id): 'group:shared-saw'},
            )
            second = _make_pro(
                db_session,
                t,
                second_name,
                event_ids=[ev.id],
                partners={str(ev.id): first_name},
                gear_sharing={str(ev.id): 'group:shared-saw'},
            )
            expected_pairs.append({first.id, second.id})

        from services.heat_generator import generate_event_heats
        generate_event_heats(ev)

        heat_rosters = [set(heat.get_competitors()) for heat in _all_heats_for_event(ev.id)]
        assert len(heat_rosters) == 2
        assert set(map(frozenset, heat_rosters)) == set(map(frozenset, expected_pairs))

    def test_dual_run_expansion_keeps_run_rosters_mirrored(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(
            db_session,
            t,
            name='Speed Climb',
            stand_type='speed_climb',
            max_stands=2,
            requires_dual_runs=True,
        )
        first = _make_pro(
            db_session,
            t,
            'Climber A',
            event_ids=[ev.id],
            gear_sharing={str(ev.id): 'Climber B'},
        )
        second = _make_pro(
            db_session,
            t,
            'Climber B',
            event_ids=[ev.id],
            gear_sharing={str(ev.id): 'Climber A'},
        )

        from services.heat_generator import generate_event_heats
        generate_event_heats(ev)

        run1 = _all_heats_for_event(ev.id, run_number=1)
        run2 = _all_heats_for_event(ev.id, run_number=2)
        assert len(run1) == len(run2) == 2
        assert [heat.get_competitors() for heat in run1] == [
            heat.get_competitors() for heat in run2
        ]
        assert sorted(_all_competitor_ids_from_heats(run1)) == sorted([first.id, second.id])

    def test_invalid_partner_candidate_preserves_existing_layout(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(
            db_session,
            t,
            name='Double Buck',
            stand_type='saw_hand',
            max_stands=4,
            is_partnered=True,
            partner_gender_requirement='same',
        )
        first = _make_pro(
            db_session,
            t,
            'Valid A',
            event_ids=[ev.id],
            partners={str(ev.id): 'Valid B'},
        )
        second = _make_pro(
            db_session,
            t,
            'Valid B',
            event_ids=[ev.id],
            partners={str(ev.id): 'Valid A'},
        )
        _make_pro(db_session, t, 'Unpaired', event_ids=[ev.id])

        from models import Heat
        old_heat = Heat(event_id=ev.id, heat_number=1, run_number=1)
        old_heat.set_roster('pro', [first.id, second.id], {first.id: 1, second.id: 1})
        db_session.add(old_heat)
        db_session.flush()
        old_heat_id = old_heat.id
        old_roster = old_heat.get_competitors()

        from services.heat_generator import HeatGenerationSafetyError, generate_event_heats
        with pytest.raises(HeatGenerationSafetyError, match='partner'):
            generate_event_heats(ev)

        remaining = _all_heats_for_event(ev.id)
        assert [heat.id for heat in remaining] == [old_heat_id]
        assert remaining[0].get_competitors() == old_roster

    def test_invalid_partner_gender_preserves_existing_layout(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(
            db_session,
            t,
            name='Jack & Jill Sawing',
            stand_type='saw_hand',
            max_stands=4,
            is_partnered=True,
            partner_gender_requirement='mixed',
        )
        first = _make_pro(
            db_session,
            t,
            'Same Gender A',
            gender='M',
            event_ids=[ev.id],
            partners={str(ev.id): 'Same Gender B'},
        )
        second = _make_pro(
            db_session,
            t,
            'Same Gender B',
            gender='M',
            event_ids=[ev.id],
            partners={str(ev.id): 'Same Gender A'},
        )

        from models import Heat
        old_heat = Heat(event_id=ev.id, heat_number=6, run_number=1)
        old_heat.set_roster(
            'pro',
            [first.id, second.id],
            {first.id: 1, second.id: 1},
        )
        db_session.add(old_heat)
        db_session.flush()
        old_heat_id = old_heat.id

        from services.heat_generator import (
            HeatGenerationSafetyError,
            generate_event_heats,
        )
        with pytest.raises(HeatGenerationSafetyError, match='mixed-gender'):
            generate_event_heats(ev)

        remaining = _all_heats_for_event(ev.id)
        assert [heat.id for heat in remaining] == [old_heat_id]
        assert remaining[0].heat_number == 6
        assert remaining[0].get_competitors() == [first.id, second.id]

    def test_final_candidate_validation_precedes_heat_deletion(self, db_session, monkeypatch):
        t = _make_tournament(db_session)
        ev = _make_event(
            db_session,
            t,
            name='Underhand',
            stand_type='underhand',
            max_stands=2,
        )
        first = _make_pro(
            db_session,
            t,
            'Unsafe A',
            event_ids=[ev.id],
            gear_sharing={str(ev.id): 'Unsafe B'},
        )
        second = _make_pro(
            db_session,
            t,
            'Unsafe B',
            event_ids=[ev.id],
            gear_sharing={str(ev.id): 'Unsafe A'},
        )

        from models import Heat
        old_heat = Heat(event_id=ev.id, heat_number=9, run_number=1)
        old_heat.set_roster('pro', [first.id], {first.id: 1})
        db_session.add(old_heat)
        db_session.flush()
        old_heat_id = old_heat.id

        def unsafe_candidate(competitors, *_args, **_kwargs):
            return [competitors]

        monkeypatch.setattr(
            'services.heat_generator._generate_standard_heats',
            unsafe_candidate,
        )

        from services.heat_generator import HeatGenerationSafetyError, generate_event_heats
        with pytest.raises(HeatGenerationSafetyError, match='gear'):
            generate_event_heats(ev)

        remaining = _all_heats_for_event(ev.id)
        assert [heat.id for heat in remaining] == [old_heat_id]
        assert remaining[0].get_competitors() == [first.id]


# ---------------------------------------------------------------------------
# Idempotent re-generation
# ---------------------------------------------------------------------------

class TestRegeneration:
    """Re-generating heats deletes old ones first (idempotent)."""

    def test_regeneration_replaces_old_heats(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Underhand', stand_type='underhand',
                         max_stands=5)
        for i in range(4):
            _make_pro(db_session, t, f'Pro {i}', gender='M', event_ids=[ev.id])

        from services.heat_generator import generate_event_heats
        generate_event_heats(ev)

        heats_first = _all_heats_for_event(ev.id)
        first_ids = {h.id for h in heats_first}
        assert len(heats_first) > 0

        # Reset status so we can re-generate
        ev.status = 'pending'
        db_session.flush()

        with warnings.catch_warnings():
            warnings.simplefilter('error', SAWarning)
            generate_event_heats(ev)

        heats_second = _all_heats_for_event(ev.id)

        # Regeneration should produce heats (IDs may be reused by SQLite).
        assert len(heats_second) > 0
        assert len(heats_second) == len(heats_first)

    def test_regeneration_preserves_competitor_count(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Underhand', stand_type='underhand',
                         max_stands=5)
        comp_ids = []
        for i in range(7):
            c = _make_pro(db_session, t, f'Pro {i}', gender='M', event_ids=[ev.id])
            comp_ids.append(c.id)

        from services.heat_generator import generate_event_heats
        generate_event_heats(ev)

        ev.status = 'pending'
        db_session.flush()
        generate_event_heats(ev)

        heats = _all_heats_for_event(ev.id)
        assigned = _all_competitor_ids_from_heats(heats)
        assert sorted(assigned) == sorted(comp_ids)


# ---------------------------------------------------------------------------
# _get_event_competitors
# ---------------------------------------------------------------------------

class TestGetEventCompetitors:
    """DB-level tests for _get_event_competitors."""

    def test_returns_active_pro_competitors(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Underhand', stand_type='underhand')
        c1 = _make_pro(db_session, t, 'Active Pro', gender='M', event_ids=[ev.id])

        from services.heat_generator import _get_event_competitors
        comps = _get_event_competitors(ev)

        assert len(comps) == 1
        assert comps[0]['id'] == c1.id
        assert comps[0]['name'] == 'Active Pro'

    def test_excludes_scratched_competitors(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Underhand', stand_type='underhand')
        _make_pro(db_session, t, 'Active Pro', gender='M', event_ids=[ev.id])
        _make_pro(db_session, t, 'Scratched Pro', gender='M',
                  event_ids=[ev.id], status='scratched')

        from services.heat_generator import _get_event_competitors
        comps = _get_event_competitors(ev)

        names = [c['name'] for c in comps]
        assert 'Active Pro' in names
        assert 'Scratched Pro' not in names

    def test_excludes_competitors_not_entered(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Underhand', stand_type='underhand')
        other_ev = _make_event(db_session, t, name='Standing Block',
                               stand_type='standing_block')
        _make_pro(db_session, t, 'Entered', gender='M', event_ids=[ev.id])
        _make_pro(db_session, t, 'Not Entered', gender='M', event_ids=[other_ev.id])

        from services.heat_generator import _get_event_competitors
        comps = _get_event_competitors(ev)

        names = [c['name'] for c in comps]
        assert 'Entered' in names
        assert 'Not Entered' not in names

    def test_pro_event_resolves_by_id(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Underhand', stand_type='underhand')
        c = _make_pro(db_session, t, 'By ID', gender='M', event_ids=[ev.id])

        from services.heat_generator import _get_event_competitors
        comps = _get_event_competitors(ev)

        assert len(comps) == 1
        assert comps[0]['id'] == c.id

    def test_college_event_resolves_by_name(self, db_session):
        t = _make_tournament(db_session)
        team = _make_team(db_session, t)
        ev = _make_event(db_session, t, name='Underhand Speed',
                         event_type='college', gender='M',
                         stand_type='underhand')
        c = _make_college(db_session, t, team, 'College Alice', gender='M',
                          event_ids=['Underhand Speed'])

        from services.heat_generator import _get_event_competitors
        comps = _get_event_competitors(ev)

        assert len(comps) == 1
        assert comps[0]['id'] == c.id

    def test_college_gender_filter(self, db_session):
        t = _make_tournament(db_session)
        team = _make_team(db_session, t)
        ev_m = _make_event(db_session, t, name='Underhand Speed',
                           event_type='college', gender='M',
                           stand_type='underhand')
        _make_college(db_session, t, team, 'Male Comp', gender='M',
                      event_ids=['Underhand Speed'])
        _make_college(db_session, t, team, 'Female Comp', gender='F',
                      event_ids=['Underhand Speed'])

        from services.heat_generator import _get_event_competitors
        comps = _get_event_competitors(ev_m)

        names = [c['name'] for c in comps]
        assert 'Male Comp (UM-A)' in names
        assert 'Female Comp' not in [n.split(' (')[0] for n in names]

    def test_creates_event_results_for_new_competitors(self, db_session):
        """When no EventResult rows exist, _get_event_competitors creates them."""
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Underhand', stand_type='underhand')
        c = _make_pro(db_session, t, 'New Pro', gender='M', event_ids=[ev.id])

        from models import EventResult
        from services.heat_generator import _get_event_competitors
        assert EventResult.query.filter_by(event_id=ev.id).count() == 0

        _get_event_competitors(ev)

        results = EventResult.query.filter_by(event_id=ev.id).all()
        assert len(results) == 1
        assert results[0].competitor_id == c.id
        assert results[0].competitor_type == 'pro'


# ---------------------------------------------------------------------------
# _sort_by_ability
# ---------------------------------------------------------------------------

class TestSortByAbility:
    """Tests for ability-rank sorting before the snake draft."""

    def test_ranked_competitors_come_first(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Underhand', stand_type='underhand')

        c1 = _make_pro(db_session, t, 'Unranked A', gender='M', event_ids=[ev.id])
        c2 = _make_pro(db_session, t, 'Ranked B', gender='M', event_ids=[ev.id])
        c3 = _make_pro(db_session, t, 'Ranked A', gender='M', event_ids=[ev.id])

        from models.pro_event_rank import ProEventRank
        db_session.add(ProEventRank(
            tournament_id=t.id, competitor_id=c3.id,
            event_category='underhand', rank=1))
        db_session.add(ProEventRank(
            tournament_id=t.id, competitor_id=c2.id,
            event_category='underhand', rank=2))
        db_session.flush()

        comps = [
            {'id': c1.id, 'name': 'Unranked A'},
            {'id': c2.id, 'name': 'Ranked B'},
            {'id': c3.id, 'name': 'Ranked A'},
        ]

        from services.heat_generator import _sort_by_ability
        sorted_comps = _sort_by_ability(comps, ev)

        # Ranked A (rank 1) first, then Ranked B (rank 2), then Unranked A
        assert sorted_comps[0]['id'] == c3.id
        assert sorted_comps[1]['id'] == c2.id
        assert sorted_comps[2]['id'] == c1.id

    def test_unranked_placed_after_ranked(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Underhand', stand_type='underhand')

        c1 = _make_pro(db_session, t, 'Unranked X', gender='M', event_ids=[ev.id])
        c2 = _make_pro(db_session, t, 'Unranked Y', gender='M', event_ids=[ev.id])
        c3 = _make_pro(db_session, t, 'Ranked Z', gender='M', event_ids=[ev.id])

        from models.pro_event_rank import ProEventRank
        db_session.add(ProEventRank(
            tournament_id=t.id, competitor_id=c3.id,
            event_category='underhand', rank=1))
        db_session.flush()

        comps = [
            {'id': c1.id, 'name': 'Unranked X'},
            {'id': c2.id, 'name': 'Unranked Y'},
            {'id': c3.id, 'name': 'Ranked Z'},
        ]

        from services.heat_generator import _sort_by_ability
        sorted_comps = _sort_by_ability(comps, ev)

        assert sorted_comps[0]['id'] == c3.id
        # Unranked sorted alphabetically as secondary
        assert sorted_comps[1]['name'] == 'Unranked X'
        assert sorted_comps[2]['name'] == 'Unranked Y'

    def test_no_ranks_preserves_order(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Underhand', stand_type='underhand')

        c1 = _make_pro(db_session, t, 'Zane', gender='M', event_ids=[ev.id])
        c2 = _make_pro(db_session, t, 'Alice', gender='M', event_ids=[ev.id])

        comps = [
            {'id': c1.id, 'name': 'Zane'},
            {'id': c2.id, 'name': 'Alice'},
        ]

        from services.heat_generator import _sort_by_ability
        sorted_comps = _sort_by_ability(comps, ev)

        # No ranks at all: original list returned
        assert sorted_comps[0]['id'] == c1.id
        assert sorted_comps[1]['id'] == c2.id

    def test_college_event_skips_ranking(self, db_session):
        """Ability ranking only applies to pro events."""
        t = _make_tournament(db_session)
        team = _make_team(db_session, t)
        ev = _make_event(db_session, t, name='Underhand Speed',
                         event_type='college', gender='M',
                         stand_type='underhand')

        c1 = _make_college(db_session, t, team, 'Zane', gender='M',
                           event_ids=['Underhand Speed'])
        c2 = _make_college(db_session, t, team, 'Alice', gender='M',
                           event_ids=['Underhand Speed'])

        comps = [
            {'id': c1.id, 'name': 'Zane'},
            {'id': c2.id, 'name': 'Alice'},
        ]

        from services.heat_generator import _sort_by_ability
        sorted_comps = _sort_by_ability(comps, ev)

        # College — no sorting applied, original order preserved
        assert sorted_comps[0]['id'] == c1.id
        assert sorted_comps[1]['id'] == c2.id


# ---------------------------------------------------------------------------
# generate_event_heats — no competitors raises ValueError
# ---------------------------------------------------------------------------

class TestNoCompetitorsError:
    """generate_event_heats raises ValueError when no competitors exist."""

    def test_raises_on_empty_event(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Underhand', stand_type='underhand')

        from services.heat_generator import generate_event_heats
        with pytest.raises(ValueError, match='No competitors'):
            generate_event_heats(ev)


# ---------------------------------------------------------------------------
# generate_event_heats — college event end-to-end
# ---------------------------------------------------------------------------

class TestGenerateCollegeEvent:
    """College Underhand Speed: 5 stands, mixed teams, gender-filtered."""

    def test_college_heat_generation(self, db_session):
        t = _make_tournament(db_session)
        team = _make_team(db_session, t)
        ev = _make_event(db_session, t, name='Underhand Speed',
                         event_type='college', gender='M',
                         stand_type='underhand', max_stands=5)

        comp_ids = []
        for i in range(7):
            c = _make_college(db_session, t, team, f'M Comp {i}', gender='M',
                              event_ids=['Underhand Speed'])
            comp_ids.append(c.id)
        # A competitor who is not entered in this event remains out of its heats.
        _make_college(db_session, t, team, 'F Comp', gender='F')

        from services.heat_generator import generate_event_heats
        num = generate_event_heats(ev)

        assert num == math.ceil(7 / 5)
        heats = _all_heats_for_event(ev.id)
        assigned = _all_competitor_ids_from_heats(heats)
        assert sorted(assigned) == sorted(comp_ids)

    def test_wrong_gender_entrant_blocks_instead_of_being_omitted(self, db_session):
        t = _make_tournament(db_session)
        team = _make_team(db_session, t)
        ev = _make_event(
            db_session,
            t,
            name='Underhand Speed',
            event_type='college',
            gender='M',
            stand_type='underhand',
            max_stands=5,
        )
        _make_college(
            db_session,
            t,
            team,
            'Wrong Division',
            gender='F',
            event_ids=['Underhand Speed'],
        )

        from services.heat_generator import HeatGenerationSafetyError, generate_event_heats
        with pytest.raises(HeatGenerationSafetyError, match='event gender'):
            generate_event_heats(ev)

        assert _all_heats_for_event(ev.id) == []


# ---------------------------------------------------------------------------
# HeatAssignment sync
# ---------------------------------------------------------------------------

class TestHeatAssignmentSync:
    """Verify HeatAssignment rows are created for generated heats."""

    def test_heat_assignments_created(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Underhand', stand_type='underhand',
                         max_stands=5)
        for i in range(4):
            _make_pro(db_session, t, f'Pro {i}', gender='M', event_ids=[ev.id])

        from services.heat_generator import generate_event_heats
        generate_event_heats(ev)

        from models import HeatAssignment
        heats = _all_heats_for_event(ev.id)
        for h in heats:
            comp_ids = h.get_competitors()
            assignments = HeatAssignment.query.filter_by(heat_id=h.id).all()
            assigned_ids = [a.competitor_id for a in assignments]
            assert sorted(assigned_ids) == sorted(comp_ids)
            for a in assignments:
                assert a.competitor_type == 'pro'


# ---------------------------------------------------------------------------
# Large competitor pool
# ---------------------------------------------------------------------------

class TestLargeCompetitorPool:
    """Verify correctness with many competitors (20+)."""

    def test_20_competitors_5_stands(self, db_session):
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Standing Block', stand_type='standing_block',
                         max_stands=5)
        comp_ids = []
        for i in range(20):
            c = _make_pro(db_session, t, f'Pro {i:02d}', gender='M', event_ids=[ev.id])
            comp_ids.append(c.id)

        from services.heat_generator import generate_event_heats
        num = generate_event_heats(ev)

        assert num == 4  # ceil(20/5)
        heats = _all_heats_for_event(ev.id)
        assert len(heats) == 4
        assigned = _all_competitor_ids_from_heats(heats)
        assert sorted(assigned) == sorted(comp_ids)
        assert len(assigned) == len(set(assigned))

        # Each heat should have exactly 5 (evenly divisible)
        for h in heats:
            assert len(h.get_competitors()) == 5
