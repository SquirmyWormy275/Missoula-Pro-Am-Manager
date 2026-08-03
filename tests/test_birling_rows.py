"""The runtime birling projector in ``services/birling_rows.py``.

D13-C commit A2. Commit A1 created five tables and backfilled them once, from
inside an Alembic revision. A2 keeps them current: every code path that writes
a birling document into ``events.payouts`` now projects that document onto the
rows in the same transaction. The JSON is still the truth and nothing reads the
rows, which is what makes A2 safe to land before A3 moves the readers.

There are three separate obligations here and they need three shapes of test.

**That the projector and the migration agree, exactly.** The planner in
``services/birling_rows.py`` is a deliberate copy of the one frozen inside
``migrations/versions/u0c4d5e6f7a8_birling_bracket_tables.py``, because an
Alembic revision has to stay runnable against a tree whose application code has
moved on by years and importing live application code into a revision destroys
that property. A copy is only defensible with a proof of equivalence, and
``TestTheTwoPlannersAgree`` is that proof: every malformed document in the
corpus is driven through BOTH implementations and the resulting plans, reasons
and rows alike, must be equal. The corpus is also checked against the reason
strings actually present in the two source files, so a reason added to one
implementation and not the other fails this module rather than surviving to
production.

**That a real save through the real service really writes rows.** Every other
birling test in the tree is mock-based by design, wrapping the service in
``patch("services.birling_bracket.db")``. That makes them fast and makes them
blind to persistence, which is precisely what A2 adds. So the live tests here
drive ``BirlingBracket`` against a real database and diff the rows against the
document the service just wrote.

**That a document the projector cannot resolve leaves NO rows rather than stale
ones.** ``services/reference_gate.py`` forgives pre-existing bad references by
design, so the two damaged era-1 brackets on the production mirror can still be
saved, so this projector will meet them. Absent rows say "there is nothing
here". Stale rows say "this is the bracket". A3 has to be able to tell the
difference without a marker column, and it can: a document that claims a
bracket plus an event with no seed rows is exactly the case A3 must refuse.

A note on isolation. ``BirlingBracket._save_bracket_data`` commits, as it did
before A2, so the live tests here escape the ``db_session`` savepoint and leak
their rows into the module's temporary database. That is pre-existing service
behaviour and not this commit's to change. Every assertion below is therefore
scoped to its own event id and never to a global count.
"""
from __future__ import annotations

import ast
import json
import logging
import pathlib

import pytest

from database import db
from models import (
    BirlingFall,
    BirlingMatch,
    BirlingPlacement,
    BirlingPreSeed,
    BirlingSeed,
)
from services import birling_rows as rows
from services.birling_bracket import BirlingBracket
from tests.conftest import (
    make_college_competitor,
    make_event,
    make_team,
    make_tournament,
)

_MIGRATION = (
    pathlib.Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "u0c4d5e6f7a8_birling_bracket_tables.py"
)


def _load_migration():
    """The revision module, loaded by path.

    Alembic revisions are not importable as a package. ``upgrade()`` is never
    called here; the schema already exists because the test app ran the chain.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("mig_u0c4d5e6f7a8", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig = _load_migration()


# ---------------------------------------------------------------------------
# Reason strings, read out of the source rather than retyped
# ---------------------------------------------------------------------------

def _reason_strings(path, namespace):
    """Every refusal reason a module can file, read from its own syntax tree.

    Retyping the list here would let the list and the code drift apart quietly,
    which is the exact failure this module exists to prevent one level up. So
    the reasons are parsed out of the source: every string handed to a call
    named ``fail`` or ``add``, plus any module-level string constant handed to
    one by name, which is how ``birling_rows`` files ``NO_POOL`` and how the
    revision's ``_backfill`` files the same reason inline.

    Non-string arguments to ``add`` (``seen.add(uid)`` and friends) are ignored
    because they are not constants and do not resolve to module-level strings.
    """
    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in ("fail", "add"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.add(arg.value)
            elif isinstance(arg, ast.Name):
                value = getattr(namespace, arg.id, None)
                if isinstance(value, str):
                    found.add(value)
    return found


MIG_REASONS = _reason_strings(_MIGRATION, mig)
ROW_REASONS = _reason_strings(pathlib.Path(rows.__file__), rows)

#: Filed by ``_plan_match`` only when two matches land on the same
#: ``(side, round_index, position)``. Unreachable through ``plan_matches``,
#: which derives position from enumeration, so it gets its own direct test
#: rather than a corpus entry.
SLOT_CLASH = "two matches claim one slot"


# ---------------------------------------------------------------------------
# The equivalence corpus
# ---------------------------------------------------------------------------

POOL = {1: 9001, 2: 9002, 3: 9003, 4: 9004}

#: Two ids resolving to one person, which is the era-1 collision in miniature
#: and the only way to reach "a competitor is placed twice".
POOL_TWIN = {**POOL, 9: 9001}


def _match(name, **kw):
    base = {"match_id": name, "competitor1": None, "competitor2": None,
            "winner": None, "loser": None, "falls": []}
    base.update(kw)
    return base


def _winners(*matches):
    return {"bracket": {"winners": [list(matches)]}}


CLEAN = {
    "competitors": [{"id": 1, "name": "A"}, {"id": 2, "name": "B"},
                    {"id": 3, "name": "C"}, {"id": 4, "name": "D"}],
    "seeding": [1, 2, 3, 4],
    "pre_seedings": {"1": 1, "2": 2},
    "current_round": "winners_1",
    "bracket": {
        "winners": [
            [_match("W1_1", competitor1=1, competitor2=4, winner=1, loser=4,
                    falls=[{"fall_number": 1, "winner": 1,
                            "recorded_at": "2026-04-24T10:15:00Z"},
                           {"fall_number": 2, "winner": 1,
                            "recorded_at": "2026-04-24T10:19:30"}]),
             _match("W1_2", competitor1=2, competitor2=3)],
            [_match("W2_1", competitor1=1)],
        ],
        "losers": [[_match("L1_1", competitor1=4)]],
        "finals": _match("F1"),
        "true_finals": dict(_match("F2"), needed=False),
    },
    "placements": {"4": 4},
}

#: ``(label, document, reasons it must file, pool)``. Every planner reason in
#: the source except ``SLOT_CLASH`` and ``NO_POOL`` appears here at least once,
#: and ``test_the_corpus_covers_every_reason_in_the_source`` enforces that.
CORPUS = [
    ("clean", CLEAN, set(), POOL),
    ("seed order is a map",
     {"seeding": {"1": 1}}, {"the seed order is not a list"}, POOL),
    ("entrants disagree with the seed order",
     {"seeding": [1, 2], "competitors": [{"id": 1}, {"id": 3}]},
     {"the entrant list and the seed order name different people"}, POOL),
    ("an entrant that is a bare id",
     {"seeding": [1], "competitors": [1]},
     {"the entrant list holds something that is not a competitor"}, POOL),
    ("an entrant list that is a map",
     {"seeding": [1], "competitors": {"a": 1}},
     {"the entrant list is not a list"}, POOL),
    ("one competitor holding two seeds",
     {"seeding": [1, 1], "competitors": [{"id": 1}]},
     {"a competitor holds two seeds"}, POOL),
    ("an empty required seed slot",
     {"seeding": [1, None], "competitors": []},
     {"a required competitor slot is empty"}, POOL),
    ("a seed reference that is a string",
     {"seeding": [1, "2"], "competitors": [{"id": 1}, {"id": "2"}]},
     {"a competitor reference is not an integer"}, POOL),
    ("a seed reference nobody answers to",
     {"seeding": [1, 99], "competitors": [{"id": 1}, {"id": 99}]},
     {"a competitor reference names nobody in the event pool"}, POOL),
    ("a pre-seeding map that is a list",
     {"pre_seedings": [1, 2]}, {"the pre-seeding map is not a map"}, POOL),
    ("a pre-seeding keyed by a name",
     {"pre_seedings": {"Person A": 1}},
     {"a pre-seeding is keyed by something that is not an id"}, POOL),
    ("a pre-seeding numbered zero",
     {"pre_seedings": {"1": 0}},
     {"a pre-seeding holds something that is not a seed number"}, POOL),
    ("two pre-seedings sharing a number",
     {"pre_seedings": {"1": 1, "2": 1}},
     {"a pre-seeding repeats a competitor or a seed number"}, POOL),
    ("a match slot holding a string",
     {"bracket": {"winners": [["W1_1"]]}},
     {"a match slot holds something that is not a match"}, POOL),
    ("a match with no name",
     _winners({"competitor1": 1}), {"a match has no usable name"}, POOL),
    ("a match name too long for the column",
     _winners(_match("W" * 21)), {"a match has no usable name"}, POOL),
    ("two matches sharing a name",
     _winners(_match("W1_1"), _match("W1_1")),
     {"two matches share a name"}, POOL),
    ("a bracket side that is a map",
     {"bracket": {"winners": {"1": []}}},
     {"a bracket side is not a list of rounds"}, POOL),
    ("a bracket round that is a map",
     {"bracket": {"winners": [{"1": 1}]}},
     {"a bracket round is not a list of matches"}, POOL),
    ("a bracket that is a list",
     {"bracket": [1, 2, 3]}, {"the bracket is not a bracket"}, POOL),
    ("a fall list that is a map",
     _winners(_match("W1_1", falls={"1": 1})),
     {"a match holds a fall list that is not a list"}, POOL),
    ("a fall that is a bare number",
     _winners(_match("W1_1", falls=[7])), {"a fall is not a fall"}, POOL),
    ("a fall numbered in words",
     _winners(_match("W1_1", falls=[{"fall_number": "one", "winner": 1}])),
     {"a fall has no usable number"}, POOL),
    ("a fourth fall in a best of three",
     _winners(_match("W1_1", falls=[{"fall_number": 4, "winner": 1}])),
     {"a fall is numbered outside one to three"}, POOL),
    ("one fall number recorded twice",
     _winners(_match("W1_1", falls=[{"fall_number": 1, "winner": 1},
                                    {"fall_number": 1, "winner": 2}])),
     {"a match records one fall number twice"}, POOL),
    ("a placement map that is a list",
     {"placements": [1]}, {"the placement map is not a map"}, POOL),
    ("a placement keyed by a name",
     {"placements": {"Person A": 1}},
     {"a placement is keyed by something that is not an id"}, POOL),
    ("a placement of zero",
     {"placements": {"1": 0}},
     {"a placement holds something that is not a position"}, POOL),
    ("two ids resolving to one placed competitor",
     {"placements": {"1": 3, "9": 4}},
     {"a competitor is placed twice"}, POOL_TWIN),
]


def _mig_plan(doc, pool):
    """The revision's planner, driven in the order ``_backfill`` drives it."""
    plan = mig._Plan(7, dict(pool))
    mig._plan_seeds(plan, doc)
    mig._plan_pre_seeds(plan, doc)
    mig._plan_matches(plan, doc)
    mig._plan_placements(plan, doc)
    return plan


def _shape(plan):
    return {
        "reasons": set(plan.reasons),
        "seeds": list(plan.seeds),
        "pre_seeds": list(plan.pre_seeds),
        "matches": list(plan.matches),
        "placements": list(plan.placements),
    }


class TestTheTwoPlannersAgree:
    """The copy in ``birling_rows`` and the copy in the revision are one thing.

    This is the whole justification for the duplication. If these ever diverge,
    a bracket that upgraded cleanly in A1 could stop projecting at runtime in
    A2, or worse, project differently, and nothing else in the tree would
    notice until a judge did.
    """

    def test_both_files_declare_the_same_reasons(self):
        assert ROW_REASONS == MIG_REASONS

    def test_the_corpus_covers_every_reason_in_the_source(self):
        """A reason nothing exercises is a reason nothing proves equal."""
        covered = set()
        for _label, doc, _expected, pool in CORPUS:
            covered |= _mig_plan(doc, pool).reasons
        covered.add(SLOT_CLASH)
        covered.add(rows.NO_POOL)
        assert covered == MIG_REASONS

    @pytest.mark.parametrize(
        "label,doc,expected,pool", CORPUS,
        ids=[c[0].replace(" ", "_") for c in CORPUS])
    def test_the_plans_are_identical(self, label, doc, expected, pool):
        theirs = _mig_plan(doc, pool)
        mine = rows.plan_document(7, dict(pool), doc)
        assert _shape(mine) == _shape(theirs), label

    @pytest.mark.parametrize(
        "label,doc,expected,pool", CORPUS,
        ids=[c[0].replace(" ", "_") for c in CORPUS])
    def test_each_document_files_the_reason_it_is_here_for(
            self, label, doc, expected, pool):
        """Without this the equivalence above would pass on two planners that
        both did nothing at all."""
        plan = rows.plan_document(7, dict(pool), doc)
        assert expected <= plan.reasons, label
        if not expected:
            assert plan.reasons == set(), label

    def test_a_clean_document_still_produces_all_of_its_rows(self):
        """The other half of the control. Equal empty plans are also equal."""
        plan = rows.plan_document(7, dict(POOL), CLEAN)
        assert plan.reasons == set()
        assert len(plan.seeds) == 4
        assert len(plan.pre_seeds) == 2
        assert len(plan.matches) == 6
        assert sum(len(m["falls"]) for m in plan.matches) == 2
        assert len(plan.placements) == 1

    def test_two_matches_claiming_one_slot_refuse_the_same_way(self):
        """Not reachable through ``plan_matches``, which derives position from
        enumeration, so both planners are driven at the match level directly.
        The guard still earns its place: it is the only thing standing between
        a hand-edited document and a unique constraint violation mid-save."""
        theirs = mig._Plan(7, dict(POOL))
        names, slots = set(), set()
        mig._plan_match(theirs, "winners", 0, 1, _match("A1"), names, slots)
        mig._plan_match(theirs, "winners", 0, 1, _match("A2"), names, slots)

        mine = rows.Plan(7, dict(POOL))
        names, slots = set(), set()
        rows.plan_match(mine, "winners", 0, 1, _match("A1"), names, slots)
        rows.plan_match(mine, "winners", 0, 1, _match("A2"), names, slots)

        assert SLOT_CLASH in mine.reasons
        assert _shape(mine) == _shape(theirs)

    @pytest.mark.parametrize("raw", [
        "2026-04-24T10:15:00Z",
        "2026-04-24T10:15:00",
        "2026-04-24T10:15:00-06:00",
        "not a time",
        "",
        None,
    ])
    def test_the_timestamp_parsers_agree(self, raw):
        assert rows.parse_timestamp(raw) == mig._timestamp(raw)

    @pytest.mark.parametrize("raw", [
        None, "", "{}", "not json", "[1, 2, 3]", '{"bracket": {}}',
    ])
    def test_the_document_parsers_agree(self, raw):
        assert rows.parse_document(raw) == mig._parse(raw)


# ---------------------------------------------------------------------------
# Live projection through the real service
# ---------------------------------------------------------------------------

def _world(session, name="Birling"):
    """A tournament, a team, five competitors and one college bracket event."""
    tour = make_tournament(session)
    team = make_team(session, tour)
    people = [make_college_competitor(session, tour, team, f"Person {letter}")
              for letter in "ABCDE"]
    session.flush()
    event = make_event(session, tour, name, event_type="college",
                       scoring_type="bracket")
    session.flush()
    return tour, event, people


def _entrants(people):
    return [{"id": p.id, "name": p.name} for p in people]


def _rows_for(event_id):
    """Every row attributable to one event, per table.

    ``birling_falls`` hangs off its match, so it is reached through the match
    rather than the event. Counting it any other way would let a leaked fall
    row pass an assertion that claims an event contributed nothing.
    """
    matches = BirlingMatch.query.filter_by(event_id=event_id).all()
    match_ids = [m.id for m in matches]
    falls = (BirlingFall.query.filter(BirlingFall.match_row_id.in_(match_ids))
             .all() if match_ids else [])
    return {
        "seeds": BirlingSeed.query.filter_by(event_id=event_id).all(),
        "pre_seeds": BirlingPreSeed.query.filter_by(event_id=event_id).all(),
        "matches": matches,
        "falls": falls,
        "placements": BirlingPlacement.query.filter_by(event_id=event_id).all(),
    }


def _counts(event_id):
    return {k: len(v) for k, v in _rows_for(event_id).items()}


class TestARealSaveWritesRows:
    """``BirlingBracket`` against a real database, not a MagicMock.

    Every other birling test in the tree neuters persistence on purpose. This
    class is the only place the service is allowed to touch a session, and it
    is therefore the only place A2's actual claim is tested.
    """

    @pytest.fixture()
    def generated(self, db_session):
        _tour, event, people = _world(db_session, "Generated")
        entrants = _entrants(people[:4])
        bracket = BirlingBracket(event)
        bracket.generate_bracket(entrants)
        return event, people, bracket

    def test_the_seed_rows_are_the_seed_order(self, generated):
        event, people, bracket = generated
        by_id = {p.id: p.uid for p in people}
        expected = [by_id[i] for i in bracket.bracket_data["seeding"]]
        seeds = sorted(_rows_for(event.id)["seeds"],
                       key=lambda s: s.seed_number)
        assert [s.seed_number for s in seeds] == list(range(1, len(expected) + 1))
        assert [s.uid for s in seeds] == expected

    def test_every_match_in_the_document_has_a_row(self, generated):
        event, _people, bracket = generated
        doc = bracket.bracket_data["bracket"]
        named = set()
        for side in ("winners", "losers"):
            for rnd in doc[side]:
                named |= {m["match_id"] for m in rnd}
        for side in ("finals", "true_finals"):
            if doc.get(side):
                named.add(doc[side]["match_id"])
        assert {m.match_id for m in _rows_for(event.id)["matches"]} == named

    def test_the_json_is_still_the_truth(self, generated):
        """A2 projects. It does not take the document away from anybody."""
        event, _people, bracket = generated
        stored = json.loads(event.payouts)
        assert stored["seeding"] == bracket.bracket_data["seeding"]
        assert "bracket" in stored

    def test_a_fall_becomes_a_fall_row(self, generated):
        event, people, bracket = generated
        by_id = {p.id: p.uid for p in people}
        match = next(m for m in bracket.bracket_data["bracket"]["winners"][0]
                     if m["competitor1"] and m["competitor2"])
        bracket.record_fall(match["match_id"], match["competitor1"])

        row = next(m for m in _rows_for(event.id)["matches"]
                   if m.match_id == match["match_id"])
        assert [f.fall_number for f in row.falls] == [1]
        assert row.falls[0].winner_uid == by_id[match["competitor1"]]
        assert row.falls[0].recorded_at is not None

    def test_a_decided_match_carries_its_winner_and_its_placements(self, generated):
        event, people, bracket = generated
        by_id = {p.id: p.uid for p in people}
        match = next(m for m in bracket.bracket_data["bracket"]["winners"][0]
                     if m["competitor1"] and m["competitor2"])
        winner, loser = match["competitor1"], match["competitor2"]
        bracket.record_match_result(match["match_id"], winner)

        row = next(m for m in _rows_for(event.id)["matches"]
                   if m.match_id == match["match_id"])
        assert row.winner_uid == by_id[winner]
        assert row.loser_uid == by_id[loser]

        placements = json.loads(event.payouts).get("placements", {})
        rowed = {p.uid: p.position for p in _rows_for(event.id)["placements"]}
        assert rowed == {by_id[int(k)]: v for k, v in placements.items()}

    def test_an_undo_takes_the_result_back_off_the_rows(self, generated):
        """The projection is rebuilt whole on every save, so an undo needs no
        undo of its own. This is the assertion that says so."""
        event, _people, bracket = generated
        match = next(m for m in bracket.bracket_data["bracket"]["winners"][0]
                     if m["competitor1"] and m["competitor2"])
        bracket.record_match_result(match["match_id"], match["competitor1"])
        bracket.undo_match_result(match["match_id"])

        row = next(m for m in _rows_for(event.id)["matches"]
                   if m.match_id == match["match_id"])
        assert row.winner_uid is None
        assert row.loser_uid is None
        assert row.falls == []

    def test_regenerating_with_a_new_order_does_not_collide(self, db_session):
        """The reason ``project`` flushes between the clear and the write.

        Within one flush SQLAlchemy emits a mapper's inserts before its
        deletes, so a re-projection that reuses a seed number would hit
        ``uq_birling_seeds_event_seed`` against the row it is replacing. Every
        seed number is reused here, which is what a judge reseeding a bracket
        does.
        """
        _tour, event, people = _world(db_session, "Reseeded")
        by_id = {p.id: p.uid for p in people}
        entrants = _entrants(people[:4])

        bracket = BirlingBracket(event)
        bracket.generate_bracket(entrants)
        first = [s.uid for s in sorted(_rows_for(event.id)["seeds"],
                                       key=lambda s: s.seed_number)]

        BirlingBracket(event).generate_bracket(list(reversed(entrants)))
        second = [s.uid for s in sorted(_rows_for(event.id)["seeds"],
                                        key=lambda s: s.seed_number)]

        assert second == list(reversed(first))
        assert set(second) == {by_id[p.id] for p in people[:4]}

    def test_a_bigger_field_replaces_a_smaller_one_completely(self, db_session):
        """A regenerate that shrinks the field must not leave the extra
        competitor holding a seed nobody gave them."""
        _tour, event, people = _world(db_session, "Resized")
        bracket = BirlingBracket(event)
        bracket.generate_bracket(_entrants(people[:5]))
        assert _counts(event.id)["seeds"] == 5

        BirlingBracket(event).generate_bracket(_entrants(people[:3]))
        assert _counts(event.id)["seeds"] == 3


@pytest.fixture()
def birling_logger_awake():
    """Wake the projector's logger back up.

    ``migrations/env.py`` line 63 calls ``logging.config.fileConfig`` without
    ``disable_existing_loggers=False``, which is the default and which disables
    every logger that exists at that moment. The default unit lane builds its
    database by running the whole migration chain in process, and
    ``services.birling_rows`` is imported at collection time, well before that
    happens. So by the time any test runs, ``rows.logger.disabled`` is True and
    ``caplog`` sees nothing at all.

    This is an artifact of migrating in process. A real deployment runs
    ``flask db upgrade`` in its own process and its application loggers are
    untouched. It is worked around here rather than fixed, because
    ``migrations/env.py`` is a committed file and nobody has approved changing
    it. It is filed as a finding.
    """
    was = rows.logger.disabled
    rows.logger.disabled = False
    yield
    rows.logger.disabled = was


class TestADocumentThatWillNotProject:
    """The case the reference gate guarantees will happen in production.

    ``check_pending`` subtracts references that were already bad before the
    write, so the two era-1 brackets on the production mirror can still be
    saved. This projector therefore meets unresolvable documents by design and
    what it does about them is a decision, not an accident.
    """

    def test_it_leaves_no_rows_at_all(self, db_session, reference_gate_disarmed,
                                      birling_logger_awake, caplog):
        _tour, event, people = _world(db_session, "Ghosted")
        BirlingBracket(event).generate_bracket(_entrants(people[:4]))
        assert _counts(event.id)["seeds"] == 4

        ghost = max(p.id for p in people) + 5000
        doc = json.loads(event.payouts)
        doc["seeding"][0] = ghost
        doc["competitors"][0]["id"] = ghost
        event.payouts = json.dumps(doc)

        with caplog.at_level(logging.WARNING, logger="services.birling_rows"):
            plan = rows.project(event)
        db.session.flush()

        assert plan.reasons
        assert _counts(event.id) == {"seeds": 0, "pre_seeds": 0, "matches": 0,
                                     "falls": 0, "placements": 0}
        assert "was not projected" in caplog.text

    def test_it_does_not_raise_and_does_not_touch_the_json(
            self, db_session, reference_gate_disarmed):
        """Race-day rule. A bracket a judge is running is not taken away to
        protect a table nobody is reading yet. The refusal arrives in A3."""
        _tour, event, people = _world(db_session, "Untouched")
        BirlingBracket(event).generate_bracket(_entrants(people[:4]))

        doc = json.loads(event.payouts)
        doc["seeding"][0] = max(p.id for p in people) + 5000
        blob = json.dumps(doc)
        event.payouts = blob

        rows.project(event)
        assert event.payouts == blob

    def test_an_event_that_stops_being_a_bracket_loses_its_rows(self, db_session):
        """``payouts`` is still named for prize money and still sometimes holds
        it. A document that is no longer a bracket must not leave a bracket
        behind."""
        _tour, event, people = _world(db_session, "Demoted")
        BirlingBracket(event).generate_bracket(_entrants(people[:4]))
        assert _counts(event.id)["seeds"] == 4

        event.payouts = json.dumps({"1st": 500, "2nd": 250})
        rows.project(event)
        db.session.flush()
        assert _counts(event.id) == {"seeds": 0, "pre_seeds": 0, "matches": 0,
                                     "falls": 0, "placements": 0}

    def test_an_event_naming_no_competitor_pool_is_refused(self):
        """``events.event_type`` is constrained to ``('college', 'pro')`` and
        both are pooled, so no row in the schema can reach this branch today.
        It is a guard against a third kind of event arriving and being silently
        projected against an empty pool, which would resolve nothing and file
        the wrong reason. Driven on a stand-in rather than an ``Event``,
        because the constraint means a real one cannot be built this way.
        """
        class _Unpooled:
            id = None
            event_type = "relay"
            payouts = json.dumps({"seeding": [1, 2]})

        plan = rows.project(_Unpooled())
        assert plan.reasons == {rows.NO_POOL}
        assert plan.seeds == []


class TestPreSeedsComeFromTheRankingsPage:
    """``pre_seedings`` is the one part of a birling document ``BirlingBracket``
    never writes.

    The register's phrasing for A2 is that ``BirlingBracket`` dual-writes, and
    for four of the five tables that is exactly where the write happens. But
    ``payouts['pre_seedings']`` is written only by
    ``routes/scheduling/ability_rankings.py``, which never constructs a
    ``BirlingBracket`` at all. A projector living on the service would have
    shipped ``birling_pre_seeds`` as a table live code never fills, so the
    projector is a free function and both writers call it. This class is the
    evidence that the second writer works.
    """

    def test_the_route_writes_pre_seed_rows(self, app, db_session, auth_client):
        from flask import url_for

        tour = make_tournament(db_session)
        team = make_team(db_session, tour)
        people = [make_college_competitor(db_session, tour, team, f"Chopper {i}")
                  for i in range(4)]
        db_session.flush()
        event = make_event(db_session, tour, "College Birling",
                           event_type="college", scoring_type="bracket")
        db_session.flush()

        schools = {"Alpha": [people[0].id, people[1].id],
                   "Bravo": [people[2].id, people[3].id]}
        with app.test_request_context():
            url = url_for("scheduling.ability_rankings", tournament_id=tour.id)
        response = auth_client.post(
            url, data={f"birling_schools_{event.id}": json.dumps(schools)},
            follow_redirects=True)
        assert response.status_code == 200

        stored = json.loads(event.payouts)["pre_seedings"]
        rowed = {p.uid: p.seed_number
                 for p in _rows_for(event.id)["pre_seeds"]}
        by_id = {p.id: p.uid for p in people}
        assert rowed == {by_id[int(k)]: v for k, v in stored.items()}
        assert sorted(rowed.values()) == [1, 2, 3, 4]

    def test_a_pre_seeding_alone_becomes_rows_with_no_bracket_present(
            self, db_session):
        """A pre-seeding is an input to generation and routinely exists before
        any bracket does. That is the structural fact the blob obscured by
        keeping both in one document, and it is why ``birling_pre_seeds`` is
        its own table rather than a nullable column on ``birling_seeds``."""
        _tour, event, people = _world(db_session, "PreSeeded")
        by_id = {p.id: p.uid for p in people}
        event.payouts = json.dumps(
            {"pre_seedings": {str(people[0].id): 1, str(people[1].id): 2}})
        rows.project(event)
        db.session.flush()

        counts = _counts(event.id)
        assert counts["pre_seeds"] == 2
        assert counts["seeds"] == 0
        rowed = {p.uid: p.seed_number for p in _rows_for(event.id)["pre_seeds"]}
        assert rowed == {by_id[people[0].id]: 1, by_id[people[1].id]: 2}

    def test_the_first_generate_keeps_the_pre_seedings_once_rows_exist(
            self, db_session):
        """The c63 defect, and the one arrangement in which it stops firing.

        Written in an earlier session to pin the defect. It now records the
        opposite outcome, and the change of outcome is the point.

        ``BirlingBracket._stored_document`` returns the stored document only if
        it carries a ``bracket`` key, and otherwise throws the whole thing away
        and starts from a fresh skeleton. The production order of operations is
        exactly the losing one: the ability rankings page writes
        ``pre_seedings`` onto an event that has no bracket yet, and before A3b
        the first generate then silently dropped it. What was lost is the
        record of what each school actually asked for, which is what a later
        regenerate would need.

        A3b flipped the reader onto the row tables, and the rows do not have
        that hole: the pre-seed rows load whether or not a bracket sits beside
        them. So on any event the ability rankings page has touched, and that
        page has projected rows alongside the JSON since A2, the pre-seedings
        now survive the first generate.

        This is behaviour A3b happens to change, not the c63 fix. c63 stays
        open. ``_stored_document`` is untouched and still discards a
        bracket-less payload whole, so an event whose rows were never projected
        still loses its pre-seedings through the fallback path. Fixing that is
        a modification nobody has approved.
        """
        _tour, event, people = _world(db_session, "Clobbered")
        pre = {str(people[0].id): 1, str(people[1].id): 2}
        event.payouts = json.dumps({"pre_seedings": pre})
        rows.project(event)
        db.session.flush()
        assert _counts(event.id)["pre_seeds"] == 2

        BirlingBracket(event).generate_bracket(_entrants(people[:4]))

        assert json.loads(event.payouts)["pre_seedings"] == pre
        counts = _counts(event.id)
        assert counts["pre_seeds"] == 2
        assert counts["seeds"] == 4

    def test_a_pre_seeding_added_after_a_bracket_exists_does_survive(
            self, db_session):
        """The other half of the same fact, and the reason the defect above is
        a loader problem and not a projector one. Once the document carries a
        ``bracket`` key the loader keeps it whole, so a regenerate leaves the
        pre-seedings and their rows standing. The projector rebuilds the entire
        event from the document on every save, which is what this asserts: the
        pre-seed rows are still there after a save that never mentioned them.
        """
        _tour, event, people = _world(db_session, "Layered")
        by_id = {p.id: p.uid for p in people}
        entrants = _entrants(people[:4])
        BirlingBracket(event).generate_bracket(entrants)

        doc = json.loads(event.payouts)
        doc["pre_seedings"] = {str(people[0].id): 1, str(people[1].id): 2}
        event.payouts = json.dumps(doc)
        rows.project(event)
        db.session.flush()
        assert _counts(event.id)["pre_seeds"] == 2

        BirlingBracket(event).generate_bracket(entrants)

        assert "pre_seedings" in json.loads(event.payouts)
        rowed = {p.uid: p.seed_number for p in _rows_for(event.id)["pre_seeds"]}
        assert rowed == {by_id[people[0].id]: 1, by_id[people[1].id]: 2}
        assert _counts(event.id)["seeds"] == 4
