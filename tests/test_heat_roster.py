"""The rows are the write target and the JSON is what they render to (D12-C).

Commit A gave ``heat_assignments`` a real reference. This is the commit that
turns the direction around: ``Heat.set_roster`` writes the rows and then renders
``competitors`` and ``stand_assignments`` from them, so the two JSON columns
stop being a record anything is copied FROM and become a view of the table.

``sync_assignments`` survives as the shim for the call sites that still express
a roster change by mutating the JSON. It reads what they wrote, writes the rows,
and renders the JSON back. That round trip is not a no-op, and most of this file
is about the ways it is not.

What is deliberately NOT asserted here: that a reader gets its answer from the
rows. Every reader in this tree still parses the JSON, and moving them is phase
2. The property this file protects is narrower and has to land first, which is
that the JSON cannot say anything the rows do not.
"""
import json

import pytest

from models.heat import BadHeatAssignment, HeatAssignment
from tests.conftest import (
    make_event,
    make_heat,
    make_pro_competitor,
    make_tournament,
)


def _rows(heat):
    return (HeatAssignment.query
            .filter_by(heat_id=heat.id)
            .order_by(HeatAssignment.id)
            .all())


@pytest.fixture
def heat_of_three(db_session):
    """A pro event, three competitors, and an empty heat in it."""
    t = make_tournament(db_session)
    e = make_event(db_session, t, 'Underhand', event_type='pro')
    comps = [make_pro_competitor(db_session, t, f'Pro {n}') for n in 'ABC']
    heat = make_heat(db_session, e, competitors=[])
    return heat, comps


class TestTheJsonIsRenderedFromTheRows:
    """What lands in the columns is what the table says, not what was written."""

    def test_a_stand_for_somebody_not_in_the_heat_is_dropped(self, heat_of_three):
        """The stalest thing this schema has ever carried.

        Nothing has ever removed a stand key when a competitor left a heat
        except the two call sites that remembered to do it by hand. A key for
        somebody who is not in the heat is not a row, so after this commit it is
        not in the column either.
        """
        heat, (a, b, _c) = heat_of_three
        heat.competitors = json.dumps([a.id])
        heat.stand_assignments = json.dumps({str(a.id): 1, str(b.id): 4})

        assert heat.sync_assignments('pro') is True

        assert heat.get_stand_assignments() == {str(a.id): 1}
        assert [r.competitor_id for r in _rows(heat)] == [a.id]

    def test_a_string_id_and_an_int_id_are_one_competitor(self, heat_of_three):
        """`"5"` is not a competitor, it is a spelling of one.

        The refusal for the same competitor twice is built on the resolved key,
        so a heat carrying both spellings raises rather than producing two rows.
        A heat carrying only the string spelling is legal and renders back as
        the integer, because the integer is what the column holds.
        """
        heat, (a, _b, _c) = heat_of_three
        heat.competitors = json.dumps([str(a.id)])
        heat.stand_assignments = json.dumps({str(a.id): 2})

        assert heat.sync_assignments('pro') is True

        assert heat.get_competitors() == [a.id]
        assert heat.competitors == json.dumps([a.id])
        assert [r.competitor_id for r in _rows(heat)] == [a.id]

    def test_the_json_takes_the_row_order_when_they_disagree(self, heat_of_three):
        """The tiebreak that decides which of the two stores is the truth.

        Same competitors, same stands, different running order. Nothing about
        the rows needs rewriting, so the only question is which order survives.
        Before this commit the answer was the JSON's, because the JSON was the
        source. It is the rows' now, and this is the assertion that says so.
        """
        heat, (a, b, _c) = heat_of_three
        heat.competitors = json.dumps([a.id, b.id])
        heat.sync_assignments('pro')
        db_rows = [r.competitor_id for r in _rows(heat)]
        assert db_rows == [a.id, b.id]

        heat.competitors = json.dumps([b.id, a.id])
        assert heat.sync_assignments('pro') is True

        assert heat.get_competitors() == [a.id, b.id]
        assert [r.competitor_id for r in _rows(heat)] == [a.id, b.id]

    def test_a_competitor_with_no_stand_contributes_no_key(self, heat_of_three):
        """`stand_assignments` has never carried an explicit null.

        Rendering one would be a new shape for every reader that does
        ``assignments.get(str(cid))`` and treats a missing key as unassigned,
        because ``None`` and absent would stop meaning the same thing.
        """
        heat, (a, b, _c) = heat_of_three
        heat.competitors = json.dumps([a.id, b.id])
        heat.stand_assignments = json.dumps({str(a.id): 1})

        heat.sync_assignments('pro')

        assert heat.get_stand_assignments() == {str(a.id): 1}
        assert str(b.id) not in heat.stand_assignments

    def test_a_heat_that_already_agrees_is_not_dirtied(self, heat_of_three,
                                                       db_session):
        """A pointless assignment is not free on this model.

        ``Heat`` carries a ``version_id_col``, so writing the same value back
        still bumps the version and still emits an UPDATE. The two bulk sweepers
        walk every heat in a tournament; if a clean visit dirtied the row, a
        sweep would hand a StaleDataError to every other request holding a heat
        it passed.
        """
        heat, (a, _b, _c) = heat_of_three
        heat.competitors = json.dumps([a.id])
        heat.stand_assignments = json.dumps({str(a.id): 1})
        heat.sync_assignments('pro')
        db_session.flush()
        version = heat.version_id

        assert heat.sync_assignments('pro') is False
        db_session.flush()

        assert heat.version_id == version


class TestSetRoster:
    """The write target itself, called the way phase 2 callers will call it."""

    def test_it_writes_the_rows_and_renders_both_columns(self, heat_of_three):
        heat, (a, b, _c) = heat_of_three

        assert heat.set_roster('pro', [a.id, b.id],
                               {a.id: 1, b.id: 2}) is True

        assert [(r.competitor_id, r.stand_number) for r in _rows(heat)] == [
            (a.id, 1), (b.id, 2)]
        assert heat.get_competitors() == [a.id, b.id]
        assert heat.get_stand_assignments() == {str(a.id): 1, str(b.id): 2}

    def test_stands_may_be_keyed_by_int_or_by_str(self, heat_of_three):
        """Callers hold both. The JSON dict is str-keyed because JSON has no
        integer keys; everything that has not been through JSON yet is int."""
        heat, (a, _b, _c) = heat_of_three

        heat.set_roster('pro', [a.id], {str(a.id): 7})
        assert [r.stand_number for r in _rows(heat)] == [7]

        assert heat.set_roster('pro', [a.id], {a.id: 7}) is False

    def test_a_json_only_repair_still_reports_a_repair(self, heat_of_three):
        """The return value answers "did I change this heat", not "did I
        change the table".

        A heat whose rows are right and whose JSON disagrees is exactly as
        broken to a reader as the reverse, and until phase 2 lands most readers
        are still reading the JSON. A sweeper that fixed one should say it did.
        """
        heat, (a, _b, _c) = heat_of_three
        heat.set_roster('pro', [a.id], {a.id: 1})
        heat.stand_assignments = json.dumps({str(a.id): 9})

        assert heat.set_roster('pro', [a.id], {a.id: 1}) is True

        assert heat.get_stand_assignments() == {str(a.id): 1}

    def test_an_empty_roster_clears_both_stores(self, heat_of_three):
        heat, (a, _b, _c) = heat_of_three
        heat.set_roster('pro', [a.id], {a.id: 1})

        assert heat.set_roster('pro', []) is True

        assert _rows(heat) == []
        assert heat.get_competitors() == []
        assert heat.get_stand_assignments() == {}

    def test_a_refusal_leaves_the_json_alone_too(self, heat_of_three):
        """Fail-closed has to cover both stores now that one renders the other.

        A refusal that had already rewritten the JSON would leave the heat
        describing a roster its rows never accepted, which is the exact
        divergence this commit exists to make impossible.
        """
        heat, (a, _b, _c) = heat_of_three
        heat.set_roster('pro', [a.id], {a.id: 1})
        before_json = (heat.competitors, heat.stand_assignments)
        before_rows = [(r.competitor_id, r.uid, r.stand_number)
                       for r in _rows(heat)]

        with pytest.raises(BadHeatAssignment):
            heat.set_roster('pro', [a.id, 999003])

        assert (heat.competitors, heat.stand_assignments) == before_json
        assert [(r.competitor_id, r.uid, r.stand_number)
                for r in _rows(heat)] == before_rows


class TestTheAssignmentsRelationship:
    """``Heat.assignments`` is the collection ``set_roster`` writes through.

    Commit B made the rows the write target while still reaching them with
    ``HeatAssignment.query.filter_by(heat_id=...)``, which cannot see a heat
    that has no id yet, cannot be eager-loaded, and leaves the heat's own
    collection stale after a rebuild. This is the commit that gives the two
    tables a relationship and routes the rebuild through it.
    """

    def test_the_collection_is_the_rows_in_running_order(self, heat_of_three):
        """Ordered by row id, which is insert order, which is running order.

        There is no position column. The judge sheet prints the collection in
        the order it comes back in, so the ordering is load-bearing and belongs
        in a test rather than in the reader that happens to depend on it.
        """
        heat, (a, b, c) = heat_of_three

        heat.set_roster('pro', [c.id, a.id, b.id])

        assert [r.competitor_id for r in heat.assignments] == [c.id, a.id, b.id]
        assert [r.competitor_id for r in _rows(heat)] == [c.id, a.id, b.id]

    def test_the_collection_is_current_after_a_rebuild(self, heat_of_three,
                                                        db_session):
        """The stale-collection case the query-based version could not fix.

        A rebuild that deleted rows out from under a loaded collection left the
        heat holding objects the database no longer had. Writing through the
        collection is what makes a second read agree with the first.
        """
        heat, (a, b, _c) = heat_of_three
        heat.set_roster('pro', [a.id, b.id])
        db_session.flush()
        assert len(heat.assignments) == 2

        heat.set_roster('pro', [b.id])
        db_session.flush()

        assert [r.competitor_id for r in heat.assignments] == [b.id]
        assert [r.competitor_id for r in _rows(heat)] == [b.id]

    def test_a_competitor_who_stays_does_not_collide_with_himself(
            self, heat_of_three, db_session):
        """The reason the rebuild flushes between the delete and the insert.

        ``uq_heat_assignments_heat_uid`` is not deferrable, and SQLAlchemy's
        unit of work emits INSERTs before DELETEs. A rebuild that handed the
        collection both at once would try to insert a second row for every
        competitor who is in the old roster and the new one, which on the
        commonest edit of all, a stand change, is all of them.
        """
        heat, (a, b, _c) = heat_of_three
        heat.set_roster('pro', [a.id, b.id], {a.id: 1, b.id: 2})
        db_session.flush()

        assert heat.set_roster('pro', [a.id, b.id], {a.id: 2, b.id: 1}) is True
        db_session.flush()

        assert [(r.competitor_id, r.stand_number) for r in _rows(heat)] == [
            (a.id, 2), (b.id, 1)]

    def test_a_competitor_returning_to_his_old_position_gets_it(
            self, heat_of_three, db_session):
        """Scratch-undo puts a competitor back at the index he was scratched
        from, which is a reorder AND a membership change at once.

        ``services/scratch_cascade.py`` restores by
        ``comp_ids.insert(idx, competitor_id)``. Every competitor after him is
        in both the old roster and the new one, at a different position, so the
        rebuild has to survive reinserting all of them. It is also the case
        that proves the roster order, not the sorted comparison order, is what
        reaches the rows.

        A reorder with no membership change is a different matter and this
        method still declines to do one; see
        ``test_the_json_takes_the_row_order_when_they_disagree``.
        """
        heat, (a, b, c) = heat_of_three
        heat.set_roster('pro', [a.id, c.id])
        db_session.flush()

        assert heat.set_roster('pro', [a.id, b.id, c.id]) is True
        db_session.flush()

        assert [r.competitor_id for r in _rows(heat)] == [a.id, b.id, c.id]
        assert heat.get_competitors() == [a.id, b.id, c.id]

    def test_a_heat_can_be_rostered_before_it_has_an_id(self, heat_of_three,
                                                         db_session):
        """``services/heat_generator.py`` builds a heat, fills its roster, and
        only then adds it to the session.

        The query-based rebuild could not have served that order: with
        ``self.id`` still None it would have filtered on NULL, found nothing,
        and written rows carrying a null ``heat_id``. Phase 2 converts that
        module, so this has to work first.
        """
        from models.event import Event
        from models.heat import Heat

        heat, (a, b, _c) = heat_of_three
        fresh = Heat(event_id=db_session.get(Event, heat.event_id).id,
                     heat_number=7, run_number=1)

        assert fresh.set_roster('pro', [a.id, b.id], {a.id: 3}) is True
        assert fresh.id is None

        db_session.add(fresh)
        db_session.flush()

        assert fresh.id is not None
        assert [(r.heat_id, r.competitor_id, r.stand_number)
                for r in _rows(fresh)] == [(fresh.id, a.id, 3),
                                           (fresh.id, b.id, None)]

    def test_deleting_a_heat_takes_its_rows_with_it(self, heat_of_three,
                                                     db_session):
        """delete-orphan, so no caller has to remember the child table.

        ``heat_assignments.heat_id`` is a real foreign key, so a heat deleted
        without its rows is not a leak, it is an IntegrityError at flush. Four
        call sites clear the rows by hand to avoid exactly that.
        """
        heat, (a, b, _c) = heat_of_three
        heat.set_roster('pro', [a.id, b.id])
        db_session.flush()
        heat_id = heat.id

        db_session.delete(heat)
        db_session.flush()

        assert HeatAssignment.query.filter_by(heat_id=heat_id).count() == 0

    def test_a_dropped_competitor_leaves_no_orphan_row(self, heat_of_three,
                                                        db_session):
        """Detaching a row from the collection deletes it rather than nulling
        its ``heat_id``, which the column would refuse anyway."""
        heat, (a, b, c) = heat_of_three
        heat.set_roster('pro', [a.id, b.id, c.id])
        db_session.flush()

        heat.set_roster('pro', [a.id])
        db_session.flush()

        assert HeatAssignment.query.filter_by(heat_id=heat.id).count() == 1
        assert [r.competitor_id for r in heat.assignments] == [a.id]


class TestSyncAssignmentsIsNowAShim:
    """The ~19 legacy call sites keep working, through one code path."""

    def test_it_writes_what_the_json_currently_says(self, heat_of_three):
        heat, (a, b, _c) = heat_of_three
        heat.competitors = json.dumps([a.id, b.id])
        heat.stand_assignments = json.dumps({str(a.id): 3, str(b.id): 4})

        assert heat.sync_assignments('pro') is True

        assert [(r.competitor_id, r.stand_number) for r in _rows(heat)] == [
            (a.id, 3), (b.id, 4)]

    def test_it_lands_a_heat_exactly_where_set_roster_lands_one(self, db_session,
                                                                heat_of_three):
        """Not a reimplementation. If these two ever diverge, the shim has grown
        a behaviour of its own and the conversion of the remaining call sites
        stops being mechanical.

        Two heats in the same event, given the same roster by the two different
        routes, have to end up indistinguishable.
        """
        from models.event import Event

        heat, (a, b, _c) = heat_of_three
        twin = make_heat(db_session, db_session.get(Event, heat.event_id),
                         heat_number=2)

        heat.competitors = json.dumps([a.id, b.id])
        heat.stand_assignments = json.dumps({str(b.id): 5})
        assert heat.sync_assignments('pro') is True

        assert twin.set_roster('pro', [a.id, b.id], {b.id: 5}) is True

        assert (twin.competitors, twin.stand_assignments) == (
            heat.competitors, heat.stand_assignments)
        assert ([(r.competitor_id, r.uid, r.stand_number) for r in _rows(twin)]
                == [(r.competitor_id, r.uid, r.stand_number)
                    for r in _rows(heat)])
