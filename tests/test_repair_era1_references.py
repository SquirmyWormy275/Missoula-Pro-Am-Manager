"""Tests for scripts/repair_era1_references.py.

This script rewrites recorded competition results by matching names. That is a
worse class of operation than a schema migration: it cannot be verified by
re-running it, and a wrong match produces a plausible answer rather than an
error. So most of what is asserted here is refusal. The happy path is four
tests; the ways the script is required to give up and hand the problem to a
human are the rest.

The one thing not covered here is a real database of era-1 ghosts, because
there is exactly one of those and it is the production mirror. The end-to-end
apply is proven against a clone of it, out of band, and the invariants that
proof relies on (post_check's three defect classes) are exercised below against
synthetic rows.
"""
import json

import pytest

from scripts.repair_era1_references import (
    Refusal,
    _event_rows,
    apply_plan,
    build_plan,
    load_rosters,
    normalize_name,
    post_check,
    repair_blob,
    resolve_target,
    scrub,
)
from services.entity_key import COLLEGE, PRO
from services.reference_audit import (
    CROSS_KIND,
    Finding,
    ReferenceSite,
    audit,
)
from tests.conftest import (
    make_college_competitor,
    make_event,
    make_pro_competitor,
    make_team,
    make_tournament,
)


def _site(raw_id, kind=COLLEGE, name_in_blob=None, name_in_row=None,
          store='events.payouts', row_id=28, path='e28.payouts.seeding[0]',
          kind_source='event_type'):
    return ReferenceSite(
        store=store, row_id=row_id, path=path, raw_id=raw_id,
        kind=kind, kind_source=kind_source,
        name_in_blob=name_in_blob, name_in_row=name_in_row,
    )


def _finding(site, verdict=CROSS_KIND):
    return Finding(site=site, verdict=verdict)


def _rosters(college=(), pro=()):
    """``(by_id, by_name)`` from ``(id, name)`` pairs, same shape as the DB load."""
    by_id = {COLLEGE: dict(college), PRO: dict(pro)}
    by_name = {}
    for kind, rows in ((COLLEGE, college), (PRO, pro)):
        index = {}
        for cid, name in rows:
            index.setdefault(normalize_name(name), []).append(cid)
        by_name[kind] = index
    return by_id, by_name


class TestNormalizeName:
    """The whole matching rule. Anything it folds together, the repair merges."""

    def test_a_trailing_team_designator_is_stripped(self):
        assert normalize_name('Davis Underwood (UM-B)') == 'davis underwood'

    def test_a_designator_in_the_middle_is_not_stripped(self):
        """Only trailing. A parenthesis mid-name is part of the name.

        Stripping anywhere would fold 'Bee (Beatrice) Philips' onto
        'Bee Philips', which is a guess about identity dressed up as
        normalization.
        """
        assert normalize_name('Bee (Beatrice) Philips') == 'bee (beatrice) philips'

    def test_case_and_whitespace_fold(self):
        assert normalize_name('  TYLER   COOK  ') == 'tyler cook'

    def test_none_and_empty_fold_to_empty(self):
        assert normalize_name(None) == ''
        assert normalize_name('') == ''

    def test_initials_do_not_match_a_full_name(self):
        """No edit distance, no nicknames, no initials. A near miss must fail."""
        assert normalize_name('T. Cook') != normalize_name('Tyler Cook')

    def test_a_bare_trailing_paren_group_leaves_nothing(self):
        """Degenerate, but it must not crash and must not become a wildcard."""
        assert normalize_name('(UM-A)') == ''

    def test_only_the_last_designator_goes(self):
        assert normalize_name('Ann (UM) Lee (UM-A)') == 'ann (um) lee'


class TestResolveTarget:
    """Four ways to refuse, one way to resolve."""

    def test_it_resolves_a_named_reference(self):
        by_id, by_name = _rosters(college=[(100085, 'Davis Underwood')])
        site = _site(9, name_in_blob='Davis Underwood (UM-B)')
        assert resolve_target(site, by_id, by_name) == 100085

    def test_the_position_name_beats_the_row_name(self):
        """``name_in_blob`` is the name stored at the position itself.

        ``name_in_row`` is a fallback assembled from the blob's competitors[]
        array. When both exist and disagree, the position wins, because that is
        the value a human reading the results would have seen.
        """
        by_id, by_name = _rosters(
            college=[(100085, 'Davis Underwood'), (100073, 'Bee Philips')])
        site = _site(9, name_in_blob='Davis Underwood (UM-B)',
                     name_in_row='Bee Philips')
        assert resolve_target(site, by_id, by_name) == 100085

    def test_a_bare_slot_falls_back_to_the_row_name(self):
        by_id, by_name = _rosters(college=[(100085, 'Davis Underwood')])
        site = _site(9, name_in_blob=None, name_in_row='Davis Underwood (UM-B)')
        assert resolve_target(site, by_id, by_name) == 100085

    def test_it_refuses_an_unusable_discipline(self):
        by_id, by_name = _rosters(college=[(100085, 'Davis Underwood')])
        site = _site(9, kind='relay', name_in_blob='Davis Underwood')
        with pytest.raises(Refusal, match='unusable discipline'):
            resolve_target(site, by_id, by_name)

    def test_it_refuses_a_nameless_reference(self):
        by_id, by_name = _rosters(college=[(100085, 'Davis Underwood')])
        with pytest.raises(Refusal, match='no name anywhere'):
            resolve_target(_site(9), by_id, by_name)

    def test_it_refuses_a_name_that_matches_nobody(self):
        by_id, by_name = _rosters(college=[(100085, 'Davis Underwood')])
        site = _site(9, name_in_blob='Nobody At All')
        with pytest.raises(Refusal, match='matches no live'):
            resolve_target(site, by_id, by_name)

    def test_it_refuses_a_name_that_matches_two_people(self):
        """Two competitors of the same name is the case that must never be
        guessed at, and the refusal has to name both so a human can pick."""
        by_id, by_name = _rosters(
            college=[(100085, 'Davis Underwood'), (100099, 'Davis Underwood')])
        site = _site(9, name_in_blob='Davis Underwood (UM-B)')
        with pytest.raises(Refusal) as exc:
            resolve_target(site, by_id, by_name)
        assert '100085' in str(exc.value)
        assert '100099' in str(exc.value)

    def test_it_resolves_in_the_pool_the_kind_names(self):
        """A college reference must not resolve against the pro roster even
        when only the pro roster carries the name. That cross-pool read is the
        defect being repaired, not a fallback."""
        by_id, by_name = _rosters(pro=[(24, 'Davis Underwood')])
        site = _site(9, kind=COLLEGE, name_in_blob='Davis Underwood')
        with pytest.raises(Refusal, match='matches no live college'):
            resolve_target(site, by_id, by_name)


class TestBuildPlan:
    def test_it_plans_a_resolvable_finding(self):
        by_id, by_name = _rosters(college=[(100085, 'Davis Underwood')])
        site = _site(9, name_in_blob='Davis Underwood (UM-B)')
        plan, refusals, details = build_plan([_finding(site)], by_id, by_name)
        assert plan == {('events.payouts', 28): {
            'e28.payouts.seeding[0]': 100085}}
        assert refusals == []
        assert details == [(site, 100085)]

    def test_it_refuses_a_store_it_cannot_rewrite(self):
        """heat_assignments and friends are plain columns, not JSON blobs.

        There are none in any mirror, so the repair path for them is untested,
        and an untested rewrite of results is worse than an honest refusal.
        """
        by_id, by_name = _rosters(college=[(100085, 'Davis Underwood')])
        site = _site(9, store='heat_assignments', row_id=3,
                     name_in_blob='Davis Underwood')
        plan, refusals, details = build_plan([_finding(site)], by_id, by_name)
        assert plan == {}
        assert len(refusals) == 1
        assert 'not a JSON event store' in refusals[0][1]

    def test_it_refuses_one_id_resolving_two_ways_in_one_blob(self):
        """The same stale integer named two different people in one blob.

        Whichever way it went, half the references would be wrong, and there is
        no way to tell from inside the blob which half.
        """
        by_id, by_name = _rosters(
            college=[(100085, 'Davis Underwood'), (100073, 'Bee Philips')])
        first = _site(9, name_in_blob='Davis Underwood',
                      path='e28.payouts.seeding[0]')
        second = _site(9, name_in_blob='Bee Philips',
                       path='e28.payouts.seeding[1]')
        plan, refusals, details = build_plan(
            [_finding(first), _finding(second)], by_id, by_name)
        assert plan == {('events.payouts', 28): {
            'e28.payouts.seeding[0]': 100085}}
        assert len(refusals) == 1
        assert 'already resolved to 100085' in refusals[0][1]

    def test_it_refuses_two_ids_collapsing_onto_one_person(self):
        """Two competitors cannot become one. Where they would, a placing or a
        bracket slot silently disappears, which is the failure mode this whole
        repair exists to undo."""
        by_id, by_name = _rosters(college=[(100085, 'Davis Underwood')])
        first = _site(9, name_in_blob='Davis Underwood',
                      path='e28.payouts.seeding[0]')
        second = _site(11, name_in_blob='DAVIS  UNDERWOOD (UM-B)',
                       path='e28.payouts.seeding[1]')
        plan, refusals, details = build_plan(
            [_finding(first), _finding(second)], by_id, by_name)
        assert len(refusals) == 1
        assert 'two competitors cannot become one' in refusals[0][1]
        assert refusals[0][0] is None

    def test_the_same_id_twice_with_the_same_target_is_fine(self):
        """A competitor appears at many positions in one blob. That is normal
        and must not read as a conflict."""
        by_id, by_name = _rosters(college=[(100085, 'Davis Underwood')])
        first = _site(9, name_in_blob='Davis Underwood',
                      path='e28.payouts.seeding[0]')
        second = _site(9, name_in_blob='Davis Underwood (UM-B)',
                       path='e28.payouts.bracket.winners[0][0].winner')
        plan, refusals, details = build_plan(
            [_finding(first), _finding(second)], by_id, by_name)
        assert refusals == []
        assert len(plan[('events.payouts', 28)]) == 2

    def test_plans_for_different_rows_do_not_collide(self):
        by_id, by_name = _rosters(college=[(100085, 'Davis Underwood')])
        first = _site(9, name_in_blob='Davis Underwood', row_id=28,
                      path='e28.payouts.seeding[0]')
        second = _site(9, name_in_blob='Davis Underwood', row_id=29,
                       store='events.event_state',
                       path='e29.event_state.seeding[0]')
        plan, refusals, _details = build_plan(
            [_finding(first), _finding(second)], by_id, by_name)
        assert refusals == []
        assert set(plan) == {('events.payouts', 28),
                             ('events.event_state', 29)}


class TestScrub:
    """The check that says the repair moved ids and nothing else."""

    def _blob(self, seeding, placements=None):
        return {
            'competitors': [{'id': seeding[0], 'name': 'Davis Underwood'},
                            {'id': seeding[1], 'name': 'Tyler Cook'}],
            'seeding': list(seeding),
            'placements': placements or {str(seeding[0]): 1,
                                         str(seeding[1]): 2},
        }

    def _scrub(self, blob):
        return scrub(blob, 'e28.payouts', 'college')

    def test_blobs_differing_only_in_ids_scrub_equal(self):
        assert self._scrub(self._blob((9, 2))) == \
            self._scrub(self._blob((100085, 100077)))

    def test_a_changed_placing_survives_the_scrub(self):
        before = self._blob((9, 2))
        after = self._blob((100085, 100077), placements={'100085': 2,
                                                         '100077': 1})
        assert self._scrub(before) != self._scrub(after)

    def test_a_changed_name_survives_the_scrub(self):
        before = self._blob((9, 2))
        after = self._blob((100085, 100077))
        after['competitors'][0]['name'] = 'Somebody Else'
        assert self._scrub(before) != self._scrub(after)

    def test_reordering_the_references_survives_the_scrub(self):
        """Ordinals are assigned in traversal order, so a swap shows up as the
        ordinals landing in different places, not as two equal blobs."""
        before = self._blob((9, 2))
        after = self._blob((9, 2))
        after['seeding'] = [2, 9]
        after['competitors'].reverse()
        assert self._scrub(before) != self._scrub(after)

    def test_it_does_not_mutate_its_input(self):
        blob = self._blob((9, 2))
        original = json.loads(json.dumps(blob))
        self._scrub(blob)
        assert blob == original


class TestRepairBlob:
    def _blob(self):
        return {
            'competitors': [{'id': 9, 'name': 'Davis Underwood (UM-B)'},
                            {'id': 2, 'name': 'Tyler Cook (UM-A)'}],
            'seeding': [9, 2],
        }

    def test_it_leaves_the_original_alone(self):
        blob = self._blob()
        original = json.loads(json.dumps(blob))
        repair_blob(blob, 'e28.payouts', 'college', 'events.payouts', 28,
                    {'e28.payouts.seeding[0]': 100085})
        assert blob == original

    def test_it_rewrites_only_the_planned_paths(self):
        after, changes = repair_blob(
            self._blob(), 'e28.payouts', 'college', 'events.payouts', 28,
            {'e28.payouts.seeding[0]': 100085})
        assert after['seeding'] == [100085, 2]
        assert after['competitors'][0]['id'] == 9
        assert len(changes) == 1

    def test_a_plan_naming_no_real_path_changes_nothing(self):
        after, changes = repair_blob(
            self._blob(), 'e28.payouts', 'college', 'events.payouts', 28,
            {'e28.payouts.nowhere': 100085})
        assert changes == []
        assert after == self._blob()


class TestAgainstADatabase:
    """The session-facing half: roster load, apply, and the post-check."""

    def _ghosted(self, db_session):
        """A college event whose payouts address a college competitor by a
        live PRO id, which is the exact shape of the production defect.

        The asymmetry is built by populating the pro pool deeper than the
        college pool and then picking a pro id the college pool does not have,
        rather than by writing an id in by hand. Sequences do not roll back and
        a hardcoded id is a test that passes once.
        """
        tournament = make_tournament(db_session)
        team = make_team(db_session, tournament)
        college = make_college_competitor(db_session, tournament, team,
                                          name='Davis Underwood')
        pros = [make_pro_competitor(db_session, tournament, name=name)
                for name in ('Tyler Cook', 'Bee Philips', 'Ann Lee')]
        db_session.flush()

        live_college = {college.id}
        ghost = next(pro for pro in pros if pro.id not in live_college)

        event = make_event(db_session, tournament, name='Birling',
                           event_type='college', payouts={
                               'competitors': [{'id': ghost.id,
                                                'name': 'Davis Underwood'}],
                               'seeding': [ghost.id],
                           })
        db_session.flush()
        return tournament, college, ghost, event

    def test_load_rosters_indexes_both_pools_by_normalized_name(self, db_session):
        tournament = make_tournament(db_session)
        team = make_team(db_session, tournament)
        college = make_college_competitor(db_session, tournament, team,
                                          name='Davis Underwood')
        pro = make_pro_competitor(db_session, tournament, name='Tyler Cook')
        db_session.flush()

        by_id, by_name = load_rosters(db_session)
        assert by_id[COLLEGE][college.id] == 'Davis Underwood'
        assert by_id[PRO][pro.id] == 'Tyler Cook'
        assert by_name[COLLEGE]['davis underwood'] == [college.id]
        assert by_name[PRO]['tyler cook'] == [pro.id]

    def test_a_ghost_is_planned_and_applied_and_audits_clean(self, db_session):
        tournament, college, pro, event = self._ghosted(db_session)

        findings = audit(db_session)
        assert findings, 'the fixture is supposed to be broken'

        by_id, by_name = load_rosters(db_session)
        plan, refusals, _details = build_plan(findings, by_id, by_name)
        assert refusals == []

        events = _event_rows(db_session, {row_id for _s, row_id in plan})
        applied = apply_plan(db_session, plan, events)
        assert post_check(db_session, plan, applied, findings) == []

        db_session.flush()
        stored = json.loads(db_session.execute(
            _payouts_query(), {'id': event.id}).scalar())
        assert stored['seeding'] == [college.id]
        assert stored['competitors'][0]['id'] == college.id

    def test_the_repaired_blob_keeps_everything_that_is_not_an_id(self, db_session):
        tournament, college, pro, event = self._ghosted(db_session)
        before = json.loads(db_session.execute(
            _payouts_query(), {'id': event.id}).scalar())

        findings = audit(db_session)
        by_id, by_name = load_rosters(db_session)
        plan, _refusals, _details = build_plan(findings, by_id, by_name)
        events = _event_rows(db_session, {row_id for _s, row_id in plan})
        apply_plan(db_session, plan, events)
        db_session.flush()

        after = json.loads(db_session.execute(
            _payouts_query(), {'id': event.id}).scalar())
        assert scrub(before, f'e{event.id}.payouts', 'college') == \
            scrub(after, f'e{event.id}.payouts', 'college')
        assert after['competitors'][0]['name'] == 'Davis Underwood'

    def test_a_repair_that_moved_something_else_raises_before_the_update(
            self, db_session):
        """apply_plan's own guard, forced.

        The plan names a path the traversal will not reach, so the count check
        fires. Nothing reaches the database.
        """
        tournament, college, pro, event = self._ghosted(db_session)
        plan = {('events.payouts', event.id): {
            f'e{event.id}.payouts.not_a_real_path': college.id}}
        events = _event_rows(db_session, {event.id})
        with pytest.raises(SystemExit, match='plan and the blob disagree'):
            apply_plan(db_session, plan, events)

    def test_post_check_reports_a_finding_left_behind(self, db_session):
        """Apply only half the plan and the post-check has to notice.

        This is the defect class that decides whether --apply commits, so it
        gets a test that does not depend on apply_plan being correct.
        """
        tournament, college, pro, event = self._ghosted(db_session)
        findings = audit(db_session)
        plan = {('events.payouts', event.id): {}}
        defects = post_check(db_session, plan, {}, findings)
        assert defects
        assert any('still' in defect for defect in defects)


def _payouts_query():
    import sqlalchemy as sa
    return sa.text('SELECT payouts FROM events WHERE id = :id')
