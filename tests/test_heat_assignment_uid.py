"""The uid that s8a0b2c3d4e5 put on heat_assignments, and what refuses it.

D12-C commit A gives ``heat_assignments`` a real reference: a NOT NULL ``uid``
with a foreign key onto ``competitors.uid``, unique within a heat. It does not
move any reader across yet. What it changes today is that a heat can no longer
name a competitor who is not there, or name one twice, and that the failure
says which heat instead of arriving as a driver IntegrityError from wherever
the next autoflush happens to be.

Why this file exists separately from test_db_constraints.py: the constraint is
the boring half. The interesting half is ``Heat.set_roster`` refusing in Python
first, and refusing BEFORE it deletes anything, which is the only version of
this that is safe to hit during a live show.

D12-C commit F2: every test below used to reach the refusal through
``sync_assignments``, seeding the roster into the ``competitors`` column with
``make_heat(..., seat=False)`` and letting the shim read it back out. The shim
is deleted and the column has no readers, so each one now passes the same
roster straight to ``set_roster``. The refusals are the same refusals; they
were always raised by ``_resolve_assignment_uids``, one call below where the
shim stopped.
"""
import pytest

from models.heat import BadHeatAssignment, HeatAssignment
from services.entity_key import EntityKey, resolve_uid
from tests.conftest import (
    ensure_competitors,
    make_college_competitor,
    make_event,
    make_heat,
    make_pro_competitor,
    make_team,
    make_tournament,
)


def _rows(heat):
    return HeatAssignment.query.filter_by(heat_id=heat.id).all()


class TestUidIsWritten:
    """The happy path, on both pools."""

    def test_pro_heat_gets_the_spine_uid(self, db_session):
        t = make_tournament(db_session)
        e = make_event(db_session, t, 'Underhand', event_type='pro')
        a = make_pro_competitor(db_session, t, 'Pro A')
        b = make_pro_competitor(db_session, t, 'Pro B')
        heat = make_heat(db_session, e)

        assert heat.set_roster('pro', [a.id, b.id],
                               {str(a.id): 1, str(b.id): 2}) is True
        db_session.flush()

        assert {r.uid for r in _rows(heat)} == {a.uid, b.uid}
        assert {r.competitor_id for r in _rows(heat)} == {a.id, b.id}

    def test_college_heat_gets_the_spine_uid(self, db_session):
        t = make_tournament(db_session)
        team = make_team(db_session, t)
        e = make_event(db_session, t, 'Single Buck', event_type='college')
        a = make_college_competitor(db_session, t, team, 'College A')
        heat = make_heat(db_session, e)

        assert heat.set_roster('college', [a.id], {str(a.id): 1}) is True
        db_session.flush()

        assert [r.uid for r in _rows(heat)] == [a.uid]

    def test_the_same_integer_in_the_two_pools_resolves_apart(self, db_session):
        """The whole reason the uid exists.

        The pro and college id sequences overlap: on the pre-reseed production
        mirror 188 of 379 assignment rows carry a competitor_id present in both
        pools. Before this column the row was pointed at the right human only
        by an unconstrained VARCHAR(20). This is that case, built on purpose.
        """
        t = make_tournament(db_session)
        team = make_team(db_session, t)
        pro_event = make_event(db_session, t, 'Underhand', event_type='pro')
        col_event = make_event(db_session, t, 'Single Buck',
                               event_type='college')

        shared_id = 4242
        ensure_competitors(db_session, t, [shared_id], 'pro')
        ensure_competitors(db_session, t, [shared_id], 'college', team=team)

        pro_heat = make_heat(db_session, pro_event, heat_number=1)
        col_heat = make_heat(db_session, col_event, heat_number=2)
        pro_heat.set_roster('pro', [shared_id])
        col_heat.set_roster('college', [shared_id])
        db_session.flush()

        pro_uid = _rows(pro_heat)[0].uid
        col_uid = _rows(col_heat)[0].uid
        assert pro_uid != col_uid
        assert pro_uid == resolve_uid(
            db_session, EntityKey.from_legacy(shared_id, 'pro'))
        assert col_uid == resolve_uid(
            db_session, EntityKey.from_legacy(shared_id, 'college'))


class TestRefusals:
    """The five ways a caller can describe a heat the database will not hold."""

    def test_a_competitor_that_does_not_exist(self, db_session):
        t = make_tournament(db_session)
        e = make_event(db_session, t, 'Underhand', event_type='pro')
        heat = make_heat(db_session, e)

        with pytest.raises(BadHeatAssignment) as exc:
            heat.set_roster('pro', [999001])

        assert '999001' in str(exc.value)
        assert f'heat {heat.id}' in str(exc.value)

    def test_the_same_competitor_twice_in_one_heat(self, db_session):
        t = make_tournament(db_session)
        e = make_event(db_session, t, 'Underhand', event_type='pro')
        a = make_pro_competitor(db_session, t, 'Pro A')
        heat = make_heat(db_session, e)

        with pytest.raises(BadHeatAssignment) as exc:
            heat.set_roster('pro', [a.id, a.id])

        assert 'more than once' in str(exc.value)

    def test_the_same_competitor_twice_under_two_spellings(self, db_session):
        """`5` and `"5"` are two entries naming one competitor.

        Checking the raw values would let this pair through to the unique
        constraint as an IntegrityError from the driver. The check is made on
        the resolved EntityKey instead, which is what makes this a named error.
        Callers hand `set_roster` both spellings: a form post arrives as
        strings and the generators pass ints.
        """
        t = make_tournament(db_session)
        e = make_event(db_session, t, 'Underhand', event_type='pro')
        a = make_pro_competitor(db_session, t, 'Pro A')
        heat = make_heat(db_session, e)

        with pytest.raises(BadHeatAssignment) as exc:
            heat.set_roster('pro', [a.id, str(a.id)])

        assert 'more than once' in str(exc.value)

    def test_an_id_that_is_not_usable_as_a_reference(self, db_session):
        t = make_tournament(db_session)
        e = make_event(db_session, t, 'Underhand', event_type='pro')
        heat = make_heat(db_session, e)

        with pytest.raises(BadHeatAssignment) as exc:
            heat.set_roster('pro', ['not-a-number'])

        assert 'not usable' in str(exc.value)

    def test_a_null_id(self, db_session):
        t = make_tournament(db_session)
        e = make_event(db_session, t, 'Underhand', event_type='pro')
        heat = make_heat(db_session, e)

        with pytest.raises(BadHeatAssignment) as exc:
            heat.set_roster('pro', [None])

        assert 'null competitor id' in str(exc.value)


class TestFailClosed:
    """A refusal leaves the heat exactly as it was, not half rebuilt.

    This is the property that matters at 7am on show day. The rows a heat
    already has are the rows the stand crew is reading off a printout, and a
    write that deleted them before discovering it could not replace them would
    be worse than one that never ran.
    """

    def test_existing_rows_survive_a_refusal(self, db_session):
        t = make_tournament(db_session)
        e = make_event(db_session, t, 'Underhand', event_type='pro')
        a = make_pro_competitor(db_session, t, 'Pro A')
        heat = make_heat(db_session, e)
        heat.set_roster('pro', [a.id], {str(a.id): 1})
        db_session.flush()
        before = [(r.uid, r.competitor_id, r.stand_number) for r in _rows(heat)]
        assert before

        with pytest.raises(BadHeatAssignment):
            heat.set_roster('pro', [a.id, 999002])

        after = [(r.uid, r.competitor_id, r.stand_number) for r in _rows(heat)]
        assert after == before

    def test_an_empty_heat_is_not_a_refusal(self, db_session):
        """19 of the 173 heats on the production mirror are empty."""
        t = make_tournament(db_session)
        e = make_event(db_session, t, 'Underhand', event_type='pro')
        heat = make_heat(db_session, e, competitors=[])

        assert heat.set_roster('pro', []) is False
        assert _rows(heat) == []


class TestDriftDetection:
    """A row whose uid disagrees with its legacy pair is drift, and resyncs.

    Before this column there was nothing for the pair to disagree with, so the
    sorted-list comparison could not see this state at all. It can now, and
    that matters because the return value is what a caller uses to tell a heat
    it changed from a heat it re-seated identically: a heat whose uid is wrong
    points at the wrong human, and a re-seat that reported no change would
    leave it pointing there.
    """

    def test_a_wrong_uid_is_rewritten(self, db_session):
        t = make_tournament(db_session)
        e = make_event(db_session, t, 'Underhand', event_type='pro')
        a = make_pro_competitor(db_session, t, 'Pro A')
        b = make_pro_competitor(db_session, t, 'Pro B')
        heat = make_heat(db_session, e)
        heat.set_roster('pro', [a.id])
        db_session.flush()

        row = _rows(heat)[0]
        row.uid = b.uid
        db_session.flush()

        assert heat.set_roster('pro', [a.id]) is True
        db_session.flush()
        assert [r.uid for r in _rows(heat)] == [a.uid]

    def test_a_correct_heat_reports_no_rewrite(self, db_session):
        t = make_tournament(db_session)
        e = make_event(db_session, t, 'Underhand', event_type='pro')
        a = make_pro_competitor(db_session, t, 'Pro A')
        heat = make_heat(db_session, e)
        assert heat.set_roster('pro', [a.id], {str(a.id): 3}) is True
        db_session.flush()

        assert heat.set_roster('pro', [a.id], {str(a.id): 3}) is False


class TestSchema:
    """The constraints exist on whichever engine the lane is running."""

    def test_uid_is_not_nullable_and_references_the_spine(self, db_session):
        from sqlalchemy import inspect

        insp = inspect(db_session.get_bind())
        cols = {c['name']: c for c in insp.get_columns('heat_assignments')}
        assert cols['uid']['nullable'] is False

        targets = {
            (fk['referred_table'], tuple(fk['referred_columns']))
            for fk in insp.get_foreign_keys('heat_assignments')
        }
        assert ('competitors', ('uid',)) in targets
        assert ('heats', ('id',)) in targets, (
            'the pre-existing heat_id foreign key must survive the SQLite '
            'batch rebuild'
        )

    def test_a_competitor_appears_in_a_heat_once(self, db_session):
        from sqlalchemy import inspect

        insp = inspect(db_session.get_bind())
        uniques = {
            (u['name'], tuple(u['column_names']))
            for u in insp.get_unique_constraints('heat_assignments')
        }
        assert ('uq_heat_assignments_heat_uid', ('heat_id', 'uid')) in uniques

    def test_uid_is_indexed_on_its_own(self, db_session):
        """Phase 2 asks "every heat this competitor is in" constantly.

        A composite index led by heat_id cannot answer that without a scan.
        """
        from sqlalchemy import inspect

        insp = inspect(db_session.get_bind())
        indexes = {tuple(i['column_names'])
                   for i in insp.get_indexes('heat_assignments')}
        assert ('uid',) in indexes
