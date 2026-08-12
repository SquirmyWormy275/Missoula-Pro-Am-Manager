"""``Heat.set_roster`` is the only way a roster is written (D12-C).

Commit A gave ``heat_assignments`` a real reference. Commit C turned the
direction around, so ``set_roster`` wrote the rows and rendered the two JSON
columns from them. Commit E moved every reader onto the rows and F2 deleted
the last things that read the columns, including ``sync_assignments``, the
shim this file was half about.

So the file lost the assertions that compared the two stores against each
other, and the class that pinned the shim's behaviour against ``set_roster``'s.
What is left is the write path itself: what it normalises, what it refuses,
and what the ``assignments`` collection looks like afterwards. Those never
depended on there being a second copy, which is why they survive the second
copy being deleted.
"""
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


class TestTheRosterIsNormalisedOnWrite:
    """A heat ends up holding what the table says, not what the caller passed.

    These were written against ``sync_assignments``, which read a roster out
    of the JSON columns and wrote it to the rows. F2 deleted it, so each one
    now passes the same malformed roster to ``set_roster`` directly. The
    normalisation being measured is the same normalisation; it always lived
    in ``set_roster`` and the shim only ever fed it.
    """

    def test_a_stand_for_somebody_not_in_the_heat_is_dropped(self, heat_of_three):
        """The stalest thing this schema has ever carried.

        Nothing has ever removed a stand key when a competitor left a heat
        except the two call sites that remembered to do it by hand. A key for
        somebody who is not in the heat is not a row, so after this commit it is
        not in the column either.
        """
        heat, (a, b, _c) = heat_of_three

        assert heat.set_roster('pro', [a.id],
                               {str(a.id): 1, str(b.id): 4}) is True

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

        assert heat.set_roster('pro', [str(a.id)], {str(a.id): 2}) is True

        assert heat.get_competitors() == [a.id]
        assert [r.competitor_id for r in _rows(heat)] == [a.id]

    # D12-C commit F2: `test_the_json_takes_the_row_order_when_they_disagree`
    # stood here. It gave a heat one running order in the rows and the reverse
    # in the JSON and asserted the rows won, which was the assertion that named
    # which of the two stores was the truth. There is one store, so there is no
    # disagreement to adjudicate and nothing for the test to say.

    def test_a_competitor_with_no_stand_contributes_no_key(self, heat_of_three):
        """`stand_assignments` has never carried an explicit null.

        Rendering one would be a new shape for every reader that does
        ``assignments.get(str(cid))`` and treats a missing key as unassigned,
        because ``None`` and absent would stop meaning the same thing.
        """
        heat, (a, b, _c) = heat_of_three

        heat.set_roster('pro', [a.id, b.id], {str(a.id): 1})

        assert heat.get_stand_assignments() == {str(a.id): 1}
        assert str(b.id) not in heat.get_stand_assignments()

    def test_a_heat_that_already_agrees_is_not_dirtied(self, heat_of_three,
                                                       db_session):
        """A pointless assignment is not free on this model.

        ``Heat`` carries a ``version_id_col``, so writing the same value back
        still bumps the version and still emits an UPDATE. The two bulk sweepers
        that made this urgent are gone as of F2, but the hazard is not theirs
        alone: a drag that lands a competitor back where he started, or any
        route that re-seats a heat it did not actually change, would hand a
        StaleDataError to every other request holding that heat.
        """
        heat, (a, _b, _c) = heat_of_three
        heat.set_roster('pro', [a.id], {str(a.id): 1})
        db_session.flush()
        version = heat.version_id

        assert heat.set_roster('pro', [a.id], {str(a.id): 1}) is False
        db_session.flush()

        assert heat.version_id == version

    def test_a_real_roster_change_advances_the_heat_version(self, heat_of_three,
                                                             db_session):
        """A roster rewrite must invalidate another editor's stale heat view."""
        heat, (a, b, _c) = heat_of_three
        heat.set_roster('pro', [a.id], {str(a.id): 1})
        db_session.flush()
        version = heat.version_id

        assert heat.set_roster('pro', [a.id, b.id], {str(a.id): 1, str(b.id): 2})
        db_session.flush()

        assert heat.version_id == version + 1


class TestSetRoster:
    """The write target itself, called the way phase 2 callers will call it."""

    def test_it_writes_the_rows(self, heat_of_three):
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

    # D12-C commit F2: `test_a_json_only_repair_still_reports_a_repair` stood
    # here. It corrupted `stand_assignments` behind the model's back and
    # asserted `set_roster` returned True on the next call, because a heat
    # whose JSON disagreed with its rows was broken to the readers that were
    # still parsing the JSON. Commit E moved the last of those readers, so a
    # column that disagrees with the rows was invisible, and F3 deleted it. The
    # return value's real contract, "did this call change anything", is
    # asserted by the not-dirtied test above and the empty-roster test below.

    def test_an_empty_roster_clears_the_heat(self, heat_of_three):
        heat, (a, _b, _c) = heat_of_three
        heat.set_roster('pro', [a.id], {a.id: 1})

        assert heat.set_roster('pro', []) is True

        assert _rows(heat) == []
        assert heat.get_competitors() == []
        assert heat.get_stand_assignments() == {}

    def test_a_refusal_leaves_the_rows_alone(self, heat_of_three):
        """Fail-closed. A partially applied roster is worse than a refused one.

        ``set_roster`` resolves every id before it writes anything, so one bad
        reference in a list of eight has to abort the whole call. A refusal
        that had already deleted the old rows would leave the heat empty and
        the operator staring at a heat he did not empty.
        """
        heat, (a, _b, _c) = heat_of_three
        heat.set_roster('pro', [a.id], {a.id: 1})
        before_rows = [(r.competitor_id, r.uid, r.stand_number)
                       for r in _rows(heat)]

        with pytest.raises(BadHeatAssignment):
            heat.set_roster('pro', [a.id, 999003])

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


# D12-C commit F2: `TestSyncAssignmentsIsNowAShim` stood here, two tests deep.
# One asserted the shim wrote what the JSON currently said; the other asserted
# a heat put through the shim was byte-for-byte indistinguishable from a heat
# put through `set_roster`, which was the guard that kept the conversion of
# the ~19 legacy call sites mechanical. Commit E converted the last of them
# and F2 deleted the shim, so both tests are asserting things about a method
# that is not there. The behaviour they pinned did not move anywhere: it was
# always `set_roster`'s, and `set_roster` is what the rest of this file tests.
