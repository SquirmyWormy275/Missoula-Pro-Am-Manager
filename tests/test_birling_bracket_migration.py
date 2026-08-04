"""The birling backfill in ``u0c4d5e6f7a8``, exercised for real.

D13-C commit A1. The revision creates five tables and fills them from the JSON
document in ``events.payouts``. Both production brackets carry era-1 ghost
references, so on every mirror in the parity rig the loader correctly writes
nothing at all, which means the mirrors prove the refusal path and prove
nothing whatever about the loading path. This module is where the loading path
is proved.

Three things are tested and each needs a different shape of test:

**That a resolvable document round-trips.** Built synthetically, because no
real bracket in the tree resolves and no real bracket has ever recorded a fall
or a placement. Those two tables have no production data behind them at all and
this module is their only coverage.

**That an unresolvable document contributes nothing and breaks nothing.** The
event is skipped whole, not partly, and its JSON is left exactly as it was
found. A half-loaded bracket is worse than an unloaded one and the assertion
that says so is the count of rows across all five tables, not just the table
the bad reference sat in.

**That the planner refuses what it says it refuses.** Pure, no database. Each
refusal is a class of malformed document someone could hand this loader, and
the reason it files is the string the operator reads at upgrade time.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import pathlib

import pytest
import sqlalchemy as sa

from tests.conftest import (
    make_college_competitor,
    make_event,
    make_team,
    make_tournament,
)

pytestmark = pytest.mark.filterwarnings(
    "error:The default datetime adapter is deprecated:DeprecationWarning"
)

_MIGRATION = (
    pathlib.Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "u0c4d5e6f7a8_birling_bracket_tables.py"
)


def _load():
    """The revision module, loaded by path.

    Alembic revisions are not importable as a package, so this is how the
    helpers inside one get called directly. ``upgrade()`` is not called here;
    the schema already exists because the test app ran the whole chain.
    """
    spec = importlib.util.spec_from_file_location("mig_u0c4d5e6f7a8", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig = _load()

TABLES = ("birling_seeds", "birling_pre_seeds", "birling_matches",
          "birling_falls", "birling_placements")


def _typed(sql, **types):
    """``sa.text`` with result types attached to the columns that need them.

    A bare textual statement carries no type information, so the value that
    comes back is whatever the driver hands over. SQLite gives a boolean as 0
    or 1 and a DateTime as a string; psycopg gives both natively. A test that
    asserted on the raw value would therefore be asserting something different
    on each of the three unit lanes, and the two that matter here are exactly
    the two the loader has to get right: ``needed``, where false and null are
    different claims, and ``recorded_at``, where the whole point is that the
    offset was folded away before the write.

    Naming the type makes the row identical on every lane, which is what these
    assertions want to be about.
    """
    return sa.text(sql).columns(**types)


def _counts(session, event_id):
    """Rows attributable to one event, per table.

    ``birling_falls`` hangs off a match rather than an event, so it is counted
    through the join. Counting it any other way would let a leaked fall row
    pass a test that claims an event contributed nothing.
    """
    conn = session.connection()
    out = {}
    for table in ("birling_seeds", "birling_pre_seeds", "birling_matches",
                  "birling_placements"):
        out[table] = conn.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE event_id = :e"),
            {"e": event_id}).scalar()
    out["birling_falls"] = conn.execute(sa.text(
        "SELECT count(*) FROM birling_falls f "
        "JOIN birling_matches m ON m.id = f.match_row_id "
        "WHERE m.event_id = :e"), {"e": event_id}).scalar()
    return out


def _world(session):
    """A tournament, a team and five college competitors named A through E."""
    tour = make_tournament(session)
    team = make_team(session, tour)
    people = {}
    for letter in "ABCDE":
        comp = make_college_competitor(session, tour, team, f"Person {letter}")
        people[letter] = comp
    session.flush()
    return tour, team, people


def _bracket_doc(people):
    """A four-entrant bracket that resolves completely.

    Small enough to assert on exhaustively and complete enough to touch every
    branch of the loader: a bye, a decided match with falls, an undecided later
    round, both finals, and a placement map.
    """
    a, b, c, d = (people[k].id for k in "ABCD")
    return {
        "competitors": [
            {"id": a, "name": "Person A"},
            {"id": b, "name": "Person B"},
            {"id": c, "name": "Person C"},
            {"id": d, "name": "Person D"},
        ],
        "seeding": [a, b, c, d],
        "pre_seedings": {str(a): 1, str(b): 2},
        "current_round": "winners_1",
        "bracket": {
            "winners": [
                [
                    {"match_id": "W1_1", "round": "winners_1",
                     "competitor1": a, "competitor2": None,
                     "winner": a, "loser": None, "falls": [], "is_bye": True},
                    {"match_id": "W1_2", "round": "winners_1",
                     "competitor1": b, "competitor2": c,
                     "winner": b, "loser": c,
                     "falls": [
                         {"fall_number": 1, "winner": b,
                          "recorded_at": "2026-04-24T10:15:00Z"},
                         {"fall_number": 2, "winner": b,
                          "recorded_at": "2026-04-24T10:19:30"},
                     ]},
                ],
                [
                    {"match_id": "W2_1", "round": "winners_2",
                     "competitor1": a, "competitor2": b,
                     "winner": None, "loser": None, "falls": []},
                ],
            ],
            "losers": [
                [
                    {"match_id": "L1_1", "round": "losers_1",
                     "competitor1": c, "competitor2": d,
                     "winner": None, "loser": None, "falls": [],
                     "eliminated_position": None},
                ],
            ],
            "finals": {"match_id": "F1", "round": "finals",
                       "competitor1": None, "competitor2": None,
                       "winner": None, "loser": None, "falls": []},
            "true_finals": {"match_id": "F2", "round": "true_finals",
                            "competitor1": None, "competitor2": None,
                            "winner": None, "loser": None, "falls": [],
                            "needed": False},
        },
        "placements": {str(d): 4},
    }


class TestRevisionIdentity:
    """The revision sits where the chain says it does."""

    def test_it_declares_its_own_identity(self):
        assert mig.revision == "u0c4d5e6f7a8"
        assert mig.down_revision == "t9b3c4d5e6f7"

    def test_it_has_both_directions(self):
        assert callable(mig.upgrade)
        assert callable(mig.downgrade)

    def test_its_id_type_matches_the_models(self):
        """A drift here is a column type mismatch the integrity suite catches
        later and much less legibly, so it is worth catching at the source."""
        from models._types import BIG_ID
        assert str(mig.BIG_ID) == str(BIG_ID)


class TestTablesExist:
    """All five tables are real and empty on a fresh database."""

    @pytest.mark.parametrize("table", TABLES)
    def test_the_table_is_queryable(self, db_session, table):
        assert db_session.connection().execute(
            sa.text(f"SELECT count(*) FROM {table}")).scalar() == 0

    @pytest.mark.parametrize("table", TABLES)
    def test_the_table_is_registered_on_the_metadata(self, table):
        from database import db
        assert table in db.metadata.tables


class TestAResolvableBracketRoundTrips:
    """The loading path, which no mirror in the rig can exercise."""

    @pytest.fixture()
    def loaded(self, db_session):
        tour, _team, people = _world(db_session)
        event = make_event(db_session, tour, "Birling", event_type="college",
                           scoring_type="bracket",
                           payouts=_bracket_doc(people))
        db_session.flush()
        mig._backfill(db_session.connection())
        return event, people

    def test_every_table_gets_the_rows_it_should(self, db_session, loaded):
        event, _people = loaded
        assert _counts(db_session, event.id) == {
            "birling_seeds": 4,
            "birling_pre_seeds": 2,
            "birling_matches": 6,
            "birling_falls": 2,
            "birling_placements": 1,
        }

    def test_seeds_carry_the_seed_order(self, db_session, loaded):
        event, people = loaded
        rows = db_session.connection().execute(sa.text(
            "SELECT seed_number, uid FROM birling_seeds WHERE event_id = :e "
            "ORDER BY seed_number"), {"e": event.id}).fetchall()
        assert [tuple(r) for r in rows] == [
            (1, people["A"].uid), (2, people["B"].uid),
            (3, people["C"].uid), (4, people["D"].uid)]

    def test_pre_seeds_are_their_own_table(self, db_session, loaded):
        """A pre-seeding is an input to generation, not a seed, and the two
        must not be conflated even when they agree."""
        event, people = loaded
        rows = db_session.connection().execute(sa.text(
            "SELECT uid, seed_number FROM birling_pre_seeds "
            "WHERE event_id = :e ORDER BY seed_number"), {"e": event.id})
        assert [tuple(r) for r in rows] == [
            (people["A"].uid, 1), (people["B"].uid, 2)]

    def test_a_bye_keeps_its_empty_second_slot(self, db_session, loaded):
        event, people = loaded
        row = db_session.connection().execute(_typed(
            "SELECT side, round_index, position, competitor1_uid, "
            "competitor2_uid, winner_uid, loser_uid, is_bye, needed "
            "FROM birling_matches WHERE event_id = :e AND match_id = 'W1_1'",
            is_bye=sa.Boolean(), needed=sa.Boolean()),
            {"e": event.id}).fetchone()
        assert tuple(row) == ("winners", 0, 1, people["A"].uid, None,
                              people["A"].uid, None, True, None)

    def test_a_decided_match_keeps_its_winner_and_loser(self, db_session, loaded):
        event, people = loaded
        row = db_session.connection().execute(_typed(
            "SELECT competitor1_uid, competitor2_uid, winner_uid, loser_uid, "
            "is_bye FROM birling_matches "
            "WHERE event_id = :e AND match_id = 'W1_2'",
            is_bye=sa.Boolean()),
            {"e": event.id}).fetchone()
        assert tuple(row) == (people["B"].uid, people["C"].uid,
                              people["B"].uid, people["C"].uid, False)

    def test_an_undecided_later_round_keeps_its_empty_result(self, db_session,
                                                             loaded):
        """The slot exists, the outcome does not. Both facts have to survive."""
        event, people = loaded
        row = db_session.connection().execute(sa.text(
            "SELECT round_index, position, competitor1_uid, winner_uid "
            "FROM birling_matches WHERE event_id = :e AND match_id = 'W2_1'"),
            {"e": event.id}).fetchone()
        assert tuple(row) == (1, 1, people["A"].uid, None)

    def test_the_losers_side_is_its_own_side(self, db_session, loaded):
        event, _people = loaded
        row = db_session.connection().execute(sa.text(
            "SELECT side, round_index, position FROM birling_matches "
            "WHERE event_id = :e AND match_id = 'L1_1'"),
            {"e": event.id}).fetchone()
        assert tuple(row) == ("losers", 0, 1)

    def test_both_finals_sit_at_round_zero_position_one(self, db_session, loaded):
        event, _people = loaded
        rows = db_session.connection().execute(sa.text(
            "SELECT match_id, side, round_index, position FROM birling_matches "
            "WHERE event_id = :e AND side IN ('finals', 'true_finals') "
            "ORDER BY match_id"), {"e": event.id}).fetchall()
        assert [tuple(r) for r in rows] == [
            ("F1", "finals", 0, 1), ("F2", "true_finals", 0, 1)]

    def test_needed_is_false_on_the_true_finals_and_null_everywhere_else(
            self, db_session, loaded):
        """False and null are different claims: false says the question of a
        second grand final was asked and answered no, null says it does not
        apply. Every match but one is the second case."""
        event, _people = loaded
        rows = dict(db_session.connection().execute(_typed(
            "SELECT match_id, needed FROM birling_matches WHERE event_id = :e",
            needed=sa.Boolean()),
            {"e": event.id}).fetchall())
        assert rows["F2"] is False
        assert rows["F1"] is None
        assert rows["W1_1"] is None
        assert rows["L1_1"] is None

    def test_falls_hang_off_their_match_in_order(self, db_session, loaded):
        event, people = loaded
        rows = db_session.connection().execute(sa.text(
            "SELECT f.fall_number, f.winner_uid, f.recorded_at "
            "FROM birling_falls f JOIN birling_matches m ON m.id = f.match_row_id "
            "WHERE m.event_id = :e AND m.match_id = 'W1_2' "
            "ORDER BY f.fall_number"), {"e": event.id}).fetchall()
        assert [(r[0], r[1]) for r in rows] == [
            (1, people["B"].uid), (2, people["B"].uid)]

    def test_a_trailing_z_becomes_naive_utc(self, db_session, loaded):
        """The blob writes both spellings and the column is naive, so an
        offset that survived into the column would be an hour of drift on a
        race-day timeline."""
        event, _people = loaded
        rows = db_session.connection().execute(_typed(
            "SELECT f.fall_number, f.recorded_at "
            "FROM birling_falls f JOIN birling_matches m ON m.id = f.match_row_id "
            "WHERE m.event_id = :e ORDER BY f.fall_number",
            recorded_at=sa.DateTime()),
            {"e": event.id}).fetchall()
        stamps = {r[0]: r[1] for r in rows}
        assert stamps[1] == datetime.datetime(2026, 4, 24, 10, 15, 0)
        assert stamps[2] == datetime.datetime(2026, 4, 24, 10, 19, 30)
        assert stamps[1].tzinfo is None

    def test_placements_survive(self, db_session, loaded):
        event, people = loaded
        rows = db_session.connection().execute(sa.text(
            "SELECT uid, position FROM birling_placements WHERE event_id = :e"),
            {"e": event.id}).fetchall()
        assert [tuple(r) for r in rows] == [(people["D"].uid, 4)]

    def test_the_json_is_not_touched(self, db_session, loaded):
        """A1 leaves the blob as the truth. A loader that also rewrote it would
        be commit A4 arriving three commits early."""
        event, people = loaded
        stored = db_session.connection().execute(sa.text(
            "SELECT payouts FROM events WHERE id = :e"),
            {"e": event.id}).scalar()
        assert json.loads(stored) == _bracket_doc(people)


@pytest.mark.usefixtures("reference_gate_disarmed")
class TestAnUnresolvableBracketContributesNothing:
    """The skip path, which is what every mirror in the rig actually takes."""

    @pytest.fixture()
    def ghosted(self, db_session):
        tour, _team, people = _world(db_session)
        doc = _bracket_doc(people)
        # One era-1 ghost, in the place they really appear: a bare id in the
        # seed order and the entrant list that names nobody alive.
        ghost = 999999
        doc["seeding"][2] = ghost
        doc["competitors"][2]["id"] = ghost
        event = make_event(db_session, tour, "Birling", event_type="college",
                           scoring_type="bracket", payouts=doc)
        db_session.flush()
        raw_before = db_session.connection().execute(sa.text(
            "SELECT payouts FROM events WHERE id = :e"),
            {"e": event.id}).scalar()
        mig._backfill(db_session.connection())
        return event, raw_before

    def test_no_table_gets_a_single_row(self, db_session, ghosted):
        """All five, not just the one the ghost sat in. The event's pre-seed
        map resolves perfectly and must still not load, because a bracket that
        is half present is worse than one that is absent."""
        event, _raw = ghosted
        assert _counts(db_session, event.id) == {
            "birling_seeds": 0, "birling_pre_seeds": 0, "birling_matches": 0,
            "birling_falls": 0, "birling_placements": 0}

    def test_the_json_is_byte_identical_afterwards(self, db_session, ghosted):
        event, raw_before = ghosted
        raw_after = db_session.connection().execute(sa.text(
            "SELECT payouts FROM events WHERE id = :e"),
            {"e": event.id}).scalar()
        assert raw_after == raw_before

    def test_it_names_the_reason_an_operator_can_act_on(self, db_session):
        plan = mig._Plan(1, {})
        plan._ref(999999, required=True)
        assert plan.reasons == {
            "a competitor reference names nobody in the event pool"}


@pytest.mark.usefixtures("reference_gate_disarmed")
class TestOneBadEventDoesNotStopAGoodOne:
    """The skip is per event. A single ghost must not cost the whole show."""

    def test_the_good_one_loads_and_the_bad_one_does_not(self, db_session):
        tour, _team, people = _world(db_session)
        good = make_event(db_session, tour, "Birling", event_type="college",
                          scoring_type="bracket", payouts=_bracket_doc(people))
        bad_doc = _bracket_doc(people)
        bad_doc["seeding"][0] = 999999
        bad_doc["competitors"][0]["id"] = 999999
        bad = make_event(db_session, tour, "Birling B", event_type="college",
                         scoring_type="bracket", payouts=bad_doc)
        db_session.flush()

        mig._backfill(db_session.connection())

        assert _counts(db_session, good.id)["birling_seeds"] == 4
        assert _counts(db_session, bad.id)["birling_seeds"] == 0
        assert _counts(db_session, bad.id)["birling_matches"] == 0


class TestDocumentsThatAreNotBrackets:
    """Everything else in the events table has to pass through untouched."""

    def test_an_ordinary_event_contributes_nothing(self, db_session):
        tour, _team, _people = _world(db_session)
        event = make_event(db_session, tour, "Men's Underhand",
                           event_type="college")
        db_session.flush()
        mig._backfill(db_session.connection())
        assert set(_counts(db_session, event.id).values()) == {0}

    def test_a_payouts_column_holding_real_payouts_contributes_nothing(
            self, db_session):
        """``payouts`` is named for something and sometimes holds it. Keying
        the loader on the document rather than on ``scoring_type`` only works
        if a money document is not mistaken for a bracket."""
        tour, _team, _people = _world(db_session)
        event = make_event(db_session, tour, "Stock Saw", event_type="college",
                           payouts={"1": 250, "2": 150})
        db_session.flush()
        mig._backfill(db_session.connection())
        assert set(_counts(db_session, event.id).values()) == {0}

    def test_malformed_json_does_not_stop_the_upgrade(self, db_session):
        tour, _team, _people = _world(db_session)
        event = make_event(db_session, tour, "Birling", event_type="college",
                           scoring_type="bracket")
        db_session.flush()
        db_session.connection().execute(
            sa.text("UPDATE events SET payouts = :p WHERE id = :e"),
            {"p": "{not json at all", "e": event.id})
        mig._backfill(db_session.connection())
        assert set(_counts(db_session, event.id).values()) == {0}

    def test_a_pre_seeding_map_alone_loads_without_a_bracket(self, db_session):
        """Routinely the real state: a school has stated its running order and
        nobody has generated anything yet."""
        tour, _team, people = _world(db_session)
        event = make_event(
            db_session, tour, "Birling", event_type="college",
            scoring_type="bracket",
            payouts={"pre_seedings": {str(people["A"].id): 1,
                                      str(people["B"].id): 2}})
        db_session.flush()
        mig._backfill(db_session.connection())
        counts = _counts(db_session, event.id)
        assert counts["birling_pre_seeds"] == 2
        assert counts["birling_seeds"] == 0
        assert counts["birling_matches"] == 0


class TestPlannerRefusals:
    """Every way a document can be malformed, and the reason it earns.

    Pure. A ``_Plan`` needs only an ``event_id`` and a pool, so none of this
    touches a database, and each case names the reason string rather than
    merely asserting that something failed.
    """

    POOL = {1: 1001, 2: 1002, 3: 1003}

    def _plan(self):
        return mig._Plan(7, dict(self.POOL))

    def test_a_seed_order_that_is_not_a_list(self):
        plan = self._plan()
        mig._plan_seeds(plan, {"seeding": {"1": 1}})
        assert "the seed order is not a list" in plan.reasons

    def test_an_entrant_list_that_disagrees_with_the_seed_order(self):
        plan = self._plan()
        mig._plan_seeds(plan, {"seeding": [1, 2],
                               "competitors": [{"id": 1}, {"id": 3}]})
        assert ("the entrant list and the seed order name different people"
                in plan.reasons)

    def test_an_entrant_that_is_not_a_competitor(self):
        plan = self._plan()
        mig._plan_seeds(plan, {"seeding": [1], "competitors": [1]})
        assert ("the entrant list holds something that is not a competitor"
                in plan.reasons)

    def test_a_competitor_holding_two_seeds(self):
        plan = self._plan()
        mig._plan_seeds(plan, {"seeding": [1, 1], "competitors": [{"id": 1}]})
        assert "a competitor holds two seeds" in plan.reasons

    def test_an_empty_required_slot(self):
        plan = self._plan()
        mig._plan_seeds(plan, {"seeding": [1, None], "competitors": []})
        assert "a required competitor slot is empty" in plan.reasons

    def test_a_reference_that_is_a_boolean(self):
        """``True`` is an ``int`` in Python and would resolve to competitor 1
        if the check were the obvious one."""
        plan = self._plan()
        assert plan._ref(True, required=True) is None
        assert "a competitor reference is not an integer" in plan.reasons

    def test_a_reference_that_is_a_string(self):
        plan = self._plan()
        assert plan._ref("1", required=True) is None
        assert "a competitor reference is not an integer" in plan.reasons

    def test_a_pre_seeding_keyed_by_a_name(self):
        plan = self._plan()
        mig._plan_pre_seeds(plan, {"pre_seedings": {"Person A": 1}})
        assert ("a pre-seeding is keyed by something that is not an id"
                in plan.reasons)

    def test_a_pre_seeding_numbered_zero(self):
        plan = self._plan()
        mig._plan_pre_seeds(plan, {"pre_seedings": {"1": 0}})
        assert ("a pre-seeding holds something that is not a seed number"
                in plan.reasons)

    def test_a_pre_seeding_that_repeats_a_number(self):
        plan = self._plan()
        mig._plan_pre_seeds(plan, {"pre_seedings": {"1": 1, "2": 1}})
        assert ("a pre-seeding repeats a competitor or a seed number"
                in plan.reasons)

    def test_a_pre_seeding_map_that_is_not_a_map(self):
        plan = self._plan()
        mig._plan_pre_seeds(plan, {"pre_seedings": [1, 2]})
        assert "the pre-seeding map is not a map" in plan.reasons

    def test_a_match_with_no_name(self):
        plan = self._plan()
        mig._plan_matches(plan, {"bracket": {"winners": [[{"competitor1": 1}]]}})
        assert "a match has no usable name" in plan.reasons

    def test_a_match_name_too_long_for_the_column(self):
        """20 characters is the column. A refusal here beats a truncation or a
        DataError halfway through an upgrade."""
        plan = self._plan()
        mig._plan_matches(plan, {"bracket": {
            "winners": [[{"match_id": "W" * 21}]]}})
        assert "a match has no usable name" in plan.reasons

    def test_two_matches_sharing_a_name(self):
        plan = self._plan()
        mig._plan_matches(plan, {"bracket": {"winners": [
            [{"match_id": "W1_1"}, {"match_id": "W1_1"}]]}})
        assert "two matches share a name" in plan.reasons

    def test_a_match_slot_that_is_not_a_match(self):
        plan = self._plan()
        mig._plan_matches(plan, {"bracket": {"winners": [["W1_1"]]}})
        assert "a match slot holds something that is not a match" in plan.reasons

    def test_a_bracket_side_that_is_not_a_list_of_rounds(self):
        plan = self._plan()
        mig._plan_matches(plan, {"bracket": {"winners": {"1": []}}})
        assert "a bracket side is not a list of rounds" in plan.reasons

    def test_a_bracket_that_is_not_a_bracket(self):
        plan = self._plan()
        mig._plan_matches(plan, {"bracket": [1, 2, 3]})
        assert "the bracket is not a bracket" in plan.reasons

    def test_a_fourth_fall(self):
        """Birling is best of three and the check constraint says so. Refusing
        in the planner is what turns an IntegrityError mid-upgrade into a
        skipped event and a printed reason."""
        plan = self._plan()
        mig._plan_matches(plan, {"bracket": {"winners": [[{
            "match_id": "W1_1",
            "falls": [{"fall_number": 4, "winner": 1}]}]]}})
        assert "a fall is numbered outside one to three" in plan.reasons

    def test_a_fall_number_recorded_twice(self):
        plan = self._plan()
        mig._plan_matches(plan, {"bracket": {"winners": [[{
            "match_id": "W1_1",
            "falls": [{"fall_number": 1, "winner": 1},
                      {"fall_number": 1, "winner": 2}]}]]}})
        assert "a match records one fall number twice" in plan.reasons

    def test_a_fall_with_no_winner(self):
        plan = self._plan()
        mig._plan_matches(plan, {"bracket": {"winners": [[{
            "match_id": "W1_1", "falls": [{"fall_number": 1}]}]]}})
        assert "a required competitor slot is empty" in plan.reasons

    def test_a_fall_list_that_is_not_a_list(self):
        plan = self._plan()
        mig._plan_matches(plan, {"bracket": {"winners": [[{
            "match_id": "W1_1", "falls": {"1": 1}}]]}})
        assert "a match holds a fall list that is not a list" in plan.reasons

    def test_a_fall_that_is_not_a_fall(self):
        plan = self._plan()
        mig._plan_matches(plan, {"bracket": {"winners": [[{
            "match_id": "W1_1", "falls": [7]}]]}})
        assert "a fall is not a fall" in plan.reasons

    def test_a_placement_keyed_by_a_name(self):
        plan = self._plan()
        mig._plan_placements(plan, {"placements": {"Person A": 1}})
        assert ("a placement is keyed by something that is not an id"
                in plan.reasons)

    def test_a_placement_of_zero(self):
        plan = self._plan()
        mig._plan_placements(plan, {"placements": {"1": 0}})
        assert "a placement holds something that is not a position" in plan.reasons

    def test_a_competitor_placed_twice(self):
        plan = self._plan()
        plan.pool[9] = 1001
        mig._plan_placements(plan, {"placements": {"1": 3, "9": 4}})
        assert "a competitor is placed twice" in plan.reasons

    def test_two_competitors_sharing_a_position_is_allowed(self):
        """Deliberate, and the one place this planner is looser than it looks.
        The grand finals write 1 and 2 while the losers bracket has already
        written the same numbers counting down from the field size. That
        collision is real, it is in production, and it is the service's to
        reconcile. A planner that refused it would refuse a finished bracket."""
        plan = self._plan()
        mig._plan_placements(plan, {"placements": {"1": 1, "2": 1}})
        assert plan.reasons == set()
        assert len(plan.placements) == 2

    def test_a_clean_document_files_no_reasons(self):
        """The control. Without it every assertion above passes on a planner
        that failed unconditionally."""
        plan = self._plan()
        doc = {"seeding": [1, 2], "competitors": [{"id": 1}, {"id": 2}],
               "pre_seedings": {"1": 1},
               "bracket": {"winners": [[{"match_id": "W1_1",
                                         "competitor1": 1, "competitor2": 2,
                                         "winner": 1, "loser": 2,
                                         "falls": [{"fall_number": 1,
                                                    "winner": 1}]}]]},
               "placements": {"2": 2}}
        mig._plan_seeds(plan, doc)
        mig._plan_pre_seeds(plan, doc)
        mig._plan_matches(plan, doc)
        mig._plan_placements(plan, doc)
        assert plan.reasons == set()
        assert len(plan.seeds) == 2
        assert len(plan.pre_seeds) == 1
        assert len(plan.matches) == 1
        assert len(plan.matches[0]["falls"]) == 1
        assert len(plan.placements) == 1


class TestTimestampParsing:
    """``recorded_at`` is the only value in the document that is not an id."""

    def test_it_reads_a_trailing_z_as_utc(self):
        assert mig._timestamp("2026-04-24T10:15:00Z") == datetime.datetime(
            2026, 4, 24, 10, 15, 0)

    def test_it_leaves_a_naive_stamp_alone(self):
        assert mig._timestamp("2026-04-24T10:15:00") == datetime.datetime(
            2026, 4, 24, 10, 15, 0)

    def test_it_converts_an_offset_to_utc_and_drops_the_offset(self):
        """The column is naive. Storing the local wall clock would put a fall
        an hour off on the race-day timeline."""
        assert mig._timestamp("2026-04-24T10:15:00-06:00") == datetime.datetime(
            2026, 4, 24, 16, 15, 0)

    def test_garbage_becomes_none_rather_than_raising(self):
        assert mig._timestamp("not a time") is None
        assert mig._timestamp(None) is None
        assert mig._timestamp("") is None
