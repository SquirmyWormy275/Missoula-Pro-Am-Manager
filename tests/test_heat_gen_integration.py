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
import pytest

from database import db as _db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def app():
    """Create a test Flask app with in-memory SQLite."""
    import os
    os.environ.setdefault('SECRET_KEY', 'test-secret-heatgen')
    os.environ.setdefault('WTF_CSRF_ENABLED', 'False')

    from app import create_app
    _app = create_app()
    _app.config.update({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'WTF_CSRF_ENABLED': False,
        'WTF_CSRF_CHECK_DEFAULT': False,
        'SERVER_NAME': None,
    })

    with _app.app_context():
        _db.create_all()
        yield _app
        _db.session.remove()
        # _db.drop_all() � skipped; in-memory SQLite is discarded on exit


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
    """Springboard: 4 dummies, left-handed cutters grouped together."""

    def test_left_handed_grouped_together(self, db_session):
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
        # Both lefties should be in the same heat
        lefty_heats = set()
        for h in heats:
            comps = set(h.get_competitors())
            for lid in lefties:
                if lid in comps:
                    lefty_heats.add(h.heat_number)

        assert len(lefty_heats) == 1, \
            f'Left-handed cutters spread across heats {lefty_heats}, expected 1'

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

    def test_gear_sharing_fallback_when_unavoidable(self, db_session):
        """When all competitors share gear and there is only 1 heat, fallback places them anyway."""
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

        from services.heat_generator import generate_event_heats
        # Should not raise — fallback places despite conflict
        num = generate_event_heats(ev)

        heats = _all_heats_for_event(ev.id)
        total_assigned = sum(len(h.get_competitors()) for h in heats)
        assert total_assigned == 2  # both placed


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
        assert 'Male Comp' in names
        assert 'Female Comp' not in names

    def test_creates_event_results_for_new_competitors(self, db_session):
        """When no EventResult rows exist, _get_event_competitors creates them."""
        t = _make_tournament(db_session)
        ev = _make_event(db_session, t, name='Underhand', stand_type='underhand')
        c = _make_pro(db_session, t, 'New Pro', gender='M', event_ids=[ev.id])

        from services.heat_generator import _get_event_competitors
        from models import EventResult
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
        # Female competitor should be excluded (gender='M' event)
        _make_college(db_session, t, team, 'F Comp', gender='F',
                      event_ids=['Underhand Speed'])

        from services.heat_generator import generate_event_heats
        num = generate_event_heats(ev)

        assert num == math.ceil(7 / 5)
        heats = _all_heats_for_event(ev.id)
        assigned = _all_competitor_ids_from_heats(heats)
        assert sorted(assigned) == sorted(comp_ids)


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
