"""Tests for services/reference_audit.py.

The point of this module is a distinction, not a count: a reference that
resolves to *nobody* and a reference that resolves to the *wrong person* are
different problems with different fixes, and the production data contains only
the second kind. Most of what is asserted here is that the second kind stays
visible as its own thing and never gets folded into the first.
"""
import json

import pytest

from services.entity_key import COLLEGE, PRO
from services.reference_audit import (
    CROSS_KIND,
    DANGLING,
    OK,
    UNKNOWN_KIND,
    ReferenceSite,
    audit,
    check_blob,
    kind_for_path,
    summarize,
    walk_blob,
)
from tests.conftest import (
    make_college_competitor,
    make_event,
    make_heat,
    make_pro_competitor,
    make_team,
    make_tournament,
)


class TestKindInference:
    """Which pool a reference belongs to, and how confident that answer is."""

    def test_path_beats_event_type_for_mixed_events(self):
        """The relay case, which is the reason this function exists.

        Pro-Am Relay is stored with event_type='pro' but its event_state holds
        college_members[].id. An earlier version of this audit trusted
        event_type and reported 64 dangling references on the production dump
        instead of 45 (as counted then): every college id under the relay was
        checked against the
        pro pool and came back unresolved. Nineteen phantom findings from one
        wrong assumption.
        """
        kind, source = kind_for_path(
            'e44.event_state.teams[0].college_members[0]', 'pro')
        assert kind == COLLEGE
        assert source == 'json_path'

    def test_pro_members_under_a_college_event_still_reads_pro(self):
        kind, source = kind_for_path(
            'e12.event_state.teams[0].pro_members[2]', 'college')
        assert kind == PRO
        assert source == 'json_path'

    @pytest.mark.parametrize('event_type,expected', [
        ('college', COLLEGE),
        ('pro', PRO),
        ('proam', PRO),
        (None, PRO),
        ('', PRO),
    ])
    def test_event_type_is_the_fallback_and_says_so(self, event_type, expected):
        kind, source = kind_for_path('e1.payouts.something[0]', event_type)
        assert kind == expected
        assert source == 'event_type'

    def test_kind_source_is_carried_so_callers_can_weigh_it(self):
        """A finding inferred from event_type is weaker evidence than one read
        off a stored discriminator, and the difference has to survive to the
        caller or nobody can tell the two apart in a report."""
        by_path = walk_blob({'college_members': [{'id': 7}]},
                            'e44.event_state', 'pro', 'events.event_state', 44)
        by_type = walk_blob({'entries': [{'id': 7}]},
                            'e44.payouts', 'pro', 'events.payouts', 44)
        assert by_path[0].kind_source == 'json_path'
        assert by_type[0].kind_source == 'event_type'


class TestWalkBlob:
    def test_named_entries_carry_the_name_and_bare_slots_do_not(self):
        """`name_in_blob` is a fact about the position, not about the data.

        A bracket slot is a bare integer no matter how recoverable it turns out
        to be. Keeping that separate from `name_in_row` is what stopped the two
        questions getting confused a second time.
        """
        blob = {
            'competitors': [{'id': 9, 'name': 'Davis Underwood (UM-B)'}],
            'bracket': {'winners': [[{'competitor1': 9, 'competitor2': 11,
                                      'name': 'Match 1'}]]},
        }
        sites = walk_blob(blob, 'e28.payouts', 'college', 'events.payouts', 28)
        by_path = {s.path: s for s in sites}

        named = by_path['e28.payouts.competitors[0].id']
        assert named.name_in_blob == 'Davis Underwood (UM-B)'

        # The dict holding the match has a `name`, but it names the MATCH, not
        # the competitor. A bare slot must never inherit it.
        bare = by_path['e28.payouts.bracket.winners[0][0].competitor1']
        assert bare.name_in_blob is None

    def test_a_bare_slot_is_recoverable_from_its_sibling_competitors_entry(self):
        """The correction that changed the plan.

        The first version of this module asked only whether a position carried
        a name of its own. That reported the 24 production bracket slots as
        unrecoverable, which turned "repair the 2026 college birling brackets"
        into "reconstruct the recorded results" and put a question to the
        operator that did not need asking. Every one of those ids is sitting in
        the same blob's `competitors[]` array with the name attached.

        Competitor 11 is the control: it appears only in the match, so it stays
        unrecoverable and the index is not just returning true for everything.
        """
        blob = {
            'competitors': [{'id': 9, 'name': 'Davis Underwood (UM-B)'}],
            'bracket': {'winners': [[{'competitor1': 9, 'competitor2': 11}]]},
        }
        by_path = {s.path: s for s in walk_blob(
            blob, 'e28.payouts', 'college', 'events.payouts', 28)}

        recoverable = by_path['e28.payouts.bracket.winners[0][0].competitor1']
        assert recoverable.name_in_blob is None
        assert recoverable.name_in_row == 'Davis Underwood (UM-B)'

        control = by_path['e28.payouts.bracket.winners[0][0].competitor2']
        assert control.name_in_row is None

    def test_the_name_index_does_not_borrow_a_match_name(self):
        """`{"competitor1": 9, "name": "Match 1"}` must not register 9 as being
        called "Match 1". Only the `id` form contributes to the index."""
        blob = {'bracket': {'winners': [[{'competitor1': 9, 'name': 'Match 1'}]]}}
        sites = walk_blob(blob, 'e1.payouts', 'college', 'events.payouts', 1)
        assert [s.name_in_row for s in sites] == [None]

    def test_an_id_with_two_names_in_one_blob_is_not_guessed_at(self):
        """A Pro-Am Relay blob holds `eligible_college` and `eligible_pro` side
        by side, both id-keyed from 1, so one integer legitimately names two
        different people. Event 44 of the 2026 dump has four of these. Taking
        whichever was walked last would hand back a real, wrong human, which is
        the failure this module exists to report, not to commit."""
        blob = {
            'eligible_college': [{'id': 8, 'name': 'Alpine Griffin'}],
            'eligible_pro': [{'id': 8, 'name': 'Erin LaVoie'}],
            'bracket': {'winners': [[{'competitor1': 8}]]},
        }
        sites = walk_blob(blob, 'e44.event_state', 'college',
                          'events.event_state', 44)
        bare = [s for s in sites if s.name_in_blob is None]
        assert bare, 'the bracket slot should still be audited'
        assert all(s.name_in_row is None for s in bare)

    def test_an_id_named_the_same_way_twice_is_still_resolved(self):
        """The same competitor listed in two arrays of one blob, which is the
        normal case, must not be mistaken for a collision and dropped."""
        blob = {
            'eligible_college': [{'id': 8, 'name': 'Alpine Griffin'}],
            'drawn_college': [{'id': 8, 'name': 'Alpine Griffin'}],
            'bracket': {'winners': [[{'competitor1': 8}]]},
        }
        sites = walk_blob(blob, 'e44.event_state', 'college',
                          'events.event_state', 44)
        bare = [s for s in sites if s.name_in_blob is None]
        assert [s.name_in_row for s in bare] == ['Alpine Griffin']

    def test_every_bracket_slot_shape_is_found(self):
        """`eliminated` is in this list because the reseed remapper rewrites it.
        It was missing from the first version of this module, which is the class
        of gap the remapper ratchet below exists to stop."""
        blob = {'bracket': {'winners': [[{
            'competitor1': 1, 'competitor2': 2, 'winner': 1, 'loser': 2,
            'eliminated': 2}]]}}
        sites = walk_blob(blob, 'e1.payouts', 'college', 'events.payouts', 1)
        assert len(sites) == 5
        assert {s.raw_id for s in sites} == {1, 2}

    def test_booleans_are_not_competitor_references(self):
        """bool is a subclass of int in Python, so `{"id": True}` would sail
        straight through an isinstance check and be audited as competitor 1."""
        sites = walk_blob({'flags': [{'id': True}]},
                          'e1.payouts', 'college', 'events.payouts', 1)
        assert sites == []

    def test_non_integer_ids_are_ignored(self):
        """Some blobs key on strings. Those are not competitor references and
        guessing at them would manufacture findings."""
        sites = walk_blob({'entries': [{'id': 'team-a'}, {'id': None}]},
                          'e1.payouts', 'college', 'events.payouts', 1)
        assert sites == []

    def test_empty_blob_is_not_an_error(self):
        assert walk_blob({}, 'e1.payouts', 'college', 'events.payouts', 1) == []
        assert walk_blob([], 'e1.payouts', 'college', 'events.payouts', 1) == []


class TestBareReferenceContainers:
    """Containers that hold competitor ids with no key on the id itself.

    These are the five the first version of this module missed. They are not
    exotic: `seeding` alone accounts for 10 of the production findings, and it
    was invisible because a bare integer inside a list has no key to match on.
    """

    def test_seeding_is_a_list_of_bare_competitor_ids(self):
        blob = {'competitors': [{'id': 9, 'name': 'Davis Underwood'}],
                'seeding': [9, 44, 42]}
        sites = walk_blob(blob, 'e28.payouts', 'college', 'events.payouts', 28)
        seeds = [s for s in sites if '.seeding[' in s.path]
        assert [s.raw_id for s in seeds] == [9, 44, 42]
        assert seeds[0].name_in_row == 'Davis Underwood'
        assert seeds[0].name_in_blob is None

    def test_falls_inside_a_match_is_a_list_of_bare_ids(self):
        blob = {'bracket': {'winners': [[{'falls': [7, 8]}]]}}
        sites = walk_blob(blob, 'e1.payouts', 'college', 'events.payouts', 1)
        assert {s.raw_id for s in sites} == {7, 8}

    @pytest.mark.parametrize('key', ['pre_seedings', 'placements'])
    def test_id_keyed_dicts_are_audited_on_the_key(self, key):
        """JSON object keys are strings, so these ids arrive as `"31"`."""
        blob = {key: {'31': 1, '33': 2}}
        sites = walk_blob(blob, 'e1.payouts', 'college', 'events.payouts', 1)
        assert sorted(s.raw_id for s in sites) == [31, 33]

    def test_a_non_numeric_key_is_skipped_not_guessed_at(self):
        blob = {'placements': {'31': 1, 'unseeded': 2}}
        sites = walk_blob(blob, 'e1.payouts', 'college', 'events.payouts', 1)
        assert [s.raw_id for s in sites] == [31]

    def test_integer_lists_that_are_not_references_are_left_alone(self):
        """An allowlist, not "walk every int list". Entry fees, event ids and
        team numbers are integers and none of them address a competitor."""
        blob = {'events_entered': [1, 2, 3], 'entry_fees': {'1': 25},
                'teams': [{'team_number': 4}]}
        assert walk_blob(blob, 'e1.payouts', 'pro', 'events.payouts', 1) == []

    def test_covers_every_container_the_reseed_remapper_rewrites(self):
        """Drift ratchet against scripts/reseed_college_ids.py.

        That remapper is the authoritative list of which JSON positions hold a
        competitor id, because it is the code that has to rewrite all of them
        when college ids move. This module was built by looking at what happened
        to be broken instead, and missed five of them. Reading the remapper's
        own constants means a key added there fails here until it is audited.

        Structural keys are listed explicitly rather than filtered by heuristic,
        so adding one is a visible decision rather than a silent widening.
        """
        from scripts.reseed_college_ids import remap_bracket_payouts
        from services.reference_audit import _BARE_LIST_KEYS, _ID_KEYED_DICT_KEYS, _REFERENCE_KEYS

        structural = {
            'bracket', 'winners', 'losers', 'finals', 'true_finals',
            'competitors', '{}',
        }

        def string_consts(code):
            out = set()
            for const in code.co_consts:
                if isinstance(const, str):
                    out.add(const)
                elif isinstance(const, tuple):
                    out |= {c for c in const if isinstance(c, str)}
                elif hasattr(const, 'co_consts'):
                    out |= string_consts(const)
            return out

        keys = {c for c in string_consts(remap_bracket_payouts.__code__)
                if c and '.' not in c and '<' not in c}
        audited = set(_REFERENCE_KEYS) | set(_BARE_LIST_KEYS) \
            | set(_ID_KEYED_DICT_KEYS)

        unaudited = keys - audited - structural
        assert not unaudited, (
            f'scripts/reseed_college_ids.py rewrites {sorted(unaudited)} as a '
            f'competitor reference and services/reference_audit.py does not '
            f'audit it. Add it to _REFERENCE_KEYS, _BARE_LIST_KEYS or '
            f'_ID_KEYED_DICT_KEYS, or to `structural` here if it is not a '
            f'reference.')


class TestClassification:
    """The whole reason this module exists: dangling and wrong-person are not
    the same finding."""

    @pytest.fixture()
    def seeded(self, db_session):
        """A tournament containing one deliberate id collision.

        A fresh test database restarts both id sequences at 1, which is the
        same condition production reached by a different route: the era-1
        reseed moved college ids to 29-92 while pro ids stayed at 1-49, so
        every stale college reference now lands on a live pro.

        Here: exactly one college competitor exists (id 1) and two pros exist
        (ids 1 and 2). A college-context reference to 2 therefore resolves to
        a live PRO, and a reference to 999 resolves to nobody.
        """
        tournament = make_tournament(db_session)
        team = make_team(db_session, tournament)
        college = make_college_competitor(db_session, tournament, team,
                                          'Real College Person')
        pro_one = make_pro_competitor(db_session, tournament, 'Pro One')
        pro_two = make_pro_competitor(db_session, tournament, 'Ghost Collides')
        db_session.flush()
        # The collision is the whole point of the fixture, so state it rather
        # than assume it. Sequences do not roll back between tests, so which
        # integers these are depends on run order and on the engine; what must
        # hold is that the collider is a pro and is NOT also a college id.
        assert pro_two.id != college.id, (
            'fixture degenerate: the collider id is also a live college id, '
            'so a college-context reference to it would correctly resolve'
        )
        return {
            'tournament': tournament,
            'college_id': college.id,
            'pro_ids': (pro_one.id, pro_two.id),
        }

    def test_cross_kind_is_reported_as_its_own_verdict_with_a_name(self, db_session, seeded):
        collider = seeded['pro_ids'][1]
        make_event(db_session, seeded['tournament'], 'College Birling',
                   event_type='college',
                   payouts={'competitors': [{'id': collider,
                                             'name': 'Whoever This Was'}]})
        db_session.flush()

        findings = audit(db_session)
        assert len(findings) == 1
        finding = findings[0]
        assert finding.verdict == CROSS_KIND
        assert finding.site.raw_id == collider
        # The person whose name a wrong-discriminator read would print.
        assert finding.collides_with == 'Ghost Collides'

    def test_truly_absent_id_is_dangling_not_cross_kind(self, db_session, seeded):
        make_event(db_session, seeded['tournament'], 'College Birling',
                   event_type='college',
                   payouts={'competitors': [{'id': 999, 'name': 'Nobody'}]})
        db_session.flush()

        findings = audit(db_session)
        assert len(findings) == 1
        assert findings[0].verdict == DANGLING
        assert findings[0].collides_with is None

    def test_resolving_reference_is_not_reported(self, db_session, seeded):
        make_event(db_session, seeded['tournament'], 'College Birling',
                   event_type='college',
                   payouts={'competitors': [{'id': seeded['college_id'],
                                             'name': 'Real College Person'}]})
        db_session.flush()

        assert audit(db_session) == []
        assert [f.verdict for f in audit(db_session, include_ok=True)] == [OK]

    def test_a_naive_existence_check_would_pass_the_ghost(self, db_session, seeded):
        """The finding that decides the design of the write-time gate.

        "Does this competitor id exist" is the obvious gate and it is useless
        here. Every one of the 55 production findings passes it, because each
        id does exist, in the other table. Asserting that directly so that a
        future simplification of the gate breaks this test instead of
        production.
        """
        collider = seeded['pro_ids'][1]
        make_event(db_session, seeded['tournament'], 'College Birling',
                   event_type='college',
                   payouts={'competitors': [{'id': collider, 'name': 'x'}]})
        db_session.flush()

        import sqlalchemy as sa
        exists_somewhere = db_session.execute(sa.text(
            'SELECT count(*) FROM ('
            '  SELECT id FROM college_competitors WHERE id = :i'
            '  UNION ALL SELECT id FROM pro_competitors WHERE id = :i) x'
        ), {'i': collider}).scalar()

        assert exists_somewhere > 0, 'the naive gate sees this id as valid'
        assert audit(db_session)[0].verdict == CROSS_KIND, \
            'the kind-aware gate catches it'

    def test_repairability_follows_the_blob_not_the_position(self, db_session, seeded):
        """A bare slot whose id is named elsewhere in the same blob is
        repairable. This is the production shape: `competitors[]` carries the
        roster with names, the bracket carries the same ids bare, and both got
        the same stale numbers from the same reseed."""
        collider = seeded['pro_ids'][1]
        make_event(
            db_session, seeded['tournament'], 'College Birling',
            event_type='college',
            payouts={
                'competitors': [{'id': collider, 'name': 'Recoverable Name'}],
                'bracket': {'winners': [[{'competitor1': collider}]]},
            })
        db_session.flush()

        findings = audit(db_session)
        assert len(findings) == 2
        summary = summarize(findings)
        assert summary['repairable_from_blob'] == 2
        assert summary['not_repairable'] == 0
        assert summary[CROSS_KIND] == 2

    def test_a_slot_with_no_name_anywhere_is_not_repairable(self, db_session, seeded):
        """The counterweight. If the id is bare and the blob never names it,
        repairability must report false, or the flag means nothing."""
        collider = seeded['pro_ids'][1]
        make_event(
            db_session, seeded['tournament'], 'College Birling',
            event_type='college',
            payouts={'bracket': {'winners': [[{'competitor1': collider}]]}})
        db_session.flush()

        summary = summarize(audit(db_session))
        assert summary['repairable_from_blob'] == 0
        assert summary['not_repairable'] == 1

    def test_unknown_discriminator_is_its_own_verdict(self, db_session, seeded):
        """A garbage competitor_type is worse than a garbage id: the id may be
        perfectly good and there is no way to check. It must not be counted as
        a stale reference."""
        site = ReferenceSite(store='heat_assignments', row_id=1,
                             path='x', raw_id=1, kind='masters',
                             kind_source='column')
        from services.reference_audit import _Pools
        assert _Pools(db_session).judge(site).verdict == UNKNOWN_KIND


class TestStoreCoverage:
    """All six stores are walked, not just the two that are currently dirty.

    On the production dump every finding sits in events.payouts and
    events.event_state, and heat_assignments, event_results and the heat JSON
    are clean. That is a fact about the reseed's coverage, not a guarantee
    about those stores, so they stay in scope.
    """

    # Four tables, six stores: heats and events each carry two independent
    # reference columns. Named here rather than derived, so that adding a store
    # to collect_sites is a deliberate act with a docstring edit attached.
    DOCUMENTED_STORES = {
        'heat_assignments',
        'event_results',
        'heats.competitors',
        'heats.stand_assignments',
        'events.payouts',
        'events.event_state',
    }

    def test_the_walked_stores_are_exactly_the_documented_ones(self, db_session):
        """Pin the store set in both directions.

        A store that stops being walked fails this, and so does one that starts
        being walked without the module docstring following. That docstring also
        carries the list of stores which exist in the schema and are NOT walked
        (``pro_event_ranks``, ``users``, ``tournament_event``, ``audit_logs``).
        That list came from enumerating information_schema, and three of those
        four tables are empty in the 2026 dump, so no amount of staring at
        production data would have produced it.
        """
        from models.event import EventResult
        from models.heat import HeatAssignment

        tournament = make_tournament(db_session)
        event = make_event(db_session, tournament, 'Underhand',
                           event_type='pro')
        heat = make_heat(db_session, event, competitors=[901],
                         stand_assignments={'902': 1})
        db_session.add(HeatAssignment(
            heat_id=heat.id, competitor_id=903, competitor_type=PRO,
            stand_number=1))
        db_session.add(EventResult(
            event_id=event.id, competitor_id=904, competitor_type=PRO,
            competitor_name='Ghost Four', status='pending',
            points_awarded=0, payout_amount=0.0))
        event.payouts = json.dumps(
            {'bracket': {'winners': [[{'competitor1': 905}]]}})
        event.event_state = json.dumps(
            {'teams': [{'id': 906, 'name': 'Ghost Six'}]})
        db_session.flush()

        stores = {f.site.store for f in audit(db_session)}
        assert stores == self.DOCUMENTED_STORES

    def test_heat_json_references_are_audited(self, db_session):
        tournament = make_tournament(db_session)
        make_pro_competitor(db_session, tournament, 'Pro One')
        event = make_event(db_session, tournament, 'Underhand', event_type='pro')
        make_heat(db_session, event, competitors=[999],
                  stand_assignments={'999': 1})
        db_session.flush()

        findings = audit(db_session)
        stores = {f.site.store for f in findings}
        assert 'heats.competitors' in stores
        assert 'heats.stand_assignments' in stores
        assert all(f.verdict == DANGLING for f in findings)

    def test_stand_assignment_keys_that_are_not_numbers_are_skipped(self, db_session):
        tournament = make_tournament(db_session)
        event = make_event(db_session, tournament, 'Underhand', event_type='pro')
        make_heat(db_session, event, competitors=[],
                  stand_assignments={'not-an-id': 1})
        db_session.flush()

        assert [f for f in audit(db_session)
                if f.site.store == 'heats.stand_assignments'] == []

    def test_unparseable_blob_reports_nothing_rather_than_guessing(self, db_session):
        """Deliberate narrowing, documented at _loads().

        A blob that will not parse has no references to report. Detecting the
        corruption itself belongs to the schema validation work, and counting
        it here would hide a structural problem inside a number everyone reads
        as "stale ids".
        """
        tournament = make_tournament(db_session)
        event = make_event(db_session, tournament, 'Birling',
                           event_type='college')
        event.payouts = '{not json at all'
        db_session.flush()

        assert audit(db_session) == []


class TestGateForm:
    def test_check_blob_flags_before_the_write(self, db_session):
        tournament = make_tournament(db_session)
        pro = make_pro_competitor(db_session, tournament, 'Pro One')
        db_session.flush()

        # Read the id back rather than assuming 1. Sequences do not roll back
        # with the transaction, so on the Postgres lane this value depends on
        # how many tests ran before this one.
        proposed = {'competitors': [{'id': pro.id, 'name': 'Pro One'}]}
        assert check_blob(db_session, proposed, 'pending', 'college') != [], \
            'this id is a pro; a college blob referencing it is a wrong-person write'

        assert check_blob(db_session, proposed, 'pending', 'pro') == []

    def test_check_blob_needs_no_other_rows(self, db_session):
        """The gate validates the value in hand. It must not depend on the rest
        of the database being clean, or it can never be turned on while the 55
        known findings are still sitting there."""
        tournament = make_tournament(db_session)
        pro = make_pro_competitor(db_session, tournament, 'Pro One')
        event = make_event(db_session, tournament, 'Dirty', event_type='college',
                           payouts={'competitors': [{'id': pro.id, 'name': 'x'}]})
        db_session.flush()
        assert event.id is not None
        assert audit(db_session), 'precondition: the database is dirty'

        assert check_blob(db_session, {'competitors': []}, 'pending', 'college') == []


class TestSummarize:
    def test_empty_summary_still_has_every_key(self):
        """A report that renders `summary['cross_kind']` must not KeyError on a
        clean database, which is the one case nobody tests by hand."""
        summary = summarize([])
        assert summary['total'] == 0
        assert summary[CROSS_KIND] == 0
        assert summary[DANGLING] == 0
        assert summary[UNKNOWN_KIND] == 0
        assert summary['by_store'] == {}

    def test_counts_are_partitioned_not_overlapping(self, db_session):
        tournament = make_tournament(db_session)
        pro = make_pro_competitor(db_session, tournament, 'Pro One')
        make_event(db_session, tournament, 'Birling', event_type='college',
                   payouts={'competitors': [{'id': pro.id, 'name': 'named'}],
                            'bracket': {'winners': [[{'competitor1': 999998,
                                                      'competitor2': 999999}]]}})
        db_session.flush()

        summary = summarize(audit(db_session))
        assert summary['total'] == 3
        assert summary[CROSS_KIND] == 1
        assert summary[DANGLING] == 2
        assert summary[CROSS_KIND] + summary[DANGLING] == summary['total']
        assert (summary['repairable_from_blob'] + summary['not_repairable']
                == summary['total'])


def test_module_is_read_only(db_session):
    """audit() must not write. It is going to be pointed at production dumps
    and at the live race-day database, and a reporting tool that mutates is a
    reporting tool nobody is allowed to run when it matters.
    """
    tournament = make_tournament(db_session)
    pro = make_pro_competitor(db_session, tournament, 'Pro One')
    make_event(db_session, tournament, 'Birling', event_type='college',
               payouts={'competitors': [{'id': pro.id, 'name': 'x'}]})
    db_session.commit()

    import sqlalchemy as sa
    before = db_session.execute(sa.text(
        'SELECT payouts FROM events')).scalar()
    audit(db_session)
    check_blob(db_session, json.loads(before), 'pending', 'college')
    after = db_session.execute(sa.text('SELECT payouts FROM events')).scalar()

    assert after == before
    assert not db_session.dirty
    assert not db_session.new
    assert not db_session.deleted
