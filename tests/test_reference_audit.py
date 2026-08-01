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
        instead of 45: every college id under the relay was checked against the
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
        """This decides repairable versus regenerate-only, so it is load
        bearing rather than cosmetic. 24 of the 45 production findings are bare
        slots, and for those there is nothing in the blob to repair from."""
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

    def test_every_bracket_slot_shape_is_found(self):
        blob = {'bracket': {'winners': [[{
            'competitor1': 1, 'competitor2': 2, 'winner': 1, 'loser': 2}]]}}
        sites = walk_blob(blob, 'e1.payouts', 'college', 'events.payouts', 1)
        assert len(sites) == 4
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
        here. Every one of the 45 production findings passes it, because each
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

    def test_repairability_splits_on_whether_a_name_was_stored(self, db_session, seeded):
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
        assert summary['repairable_from_blob'] == 1
        assert summary['not_repairable'] == 1
        assert summary[CROSS_KIND] == 2

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
    """All five stores are walked, not just the two that are currently dirty.

    On the production dump every finding sits in events.payouts and
    events.event_state, and heat_assignments, event_results and the heat JSON
    are clean. That is a fact about the reseed's coverage, not a guarantee
    about those stores, so they stay in scope.
    """

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
        of the database being clean, or it can never be turned on while the 45
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
