"""The inverse birling projection in ``services/birling_rows.py``.

D13-C commit A3a. A2 taught every writer of a birling document to project that
document onto five real tables. A3a teaches the tables to produce the document
back. Nothing calls the inverse yet; A3b points the readers at it and A4 drops
the JSON column, and at that point this code is the only thing standing between
a stored bracket and a blank page.

So the obligation here is a round trip, and it is a round trip with five
declared exceptions. ``project`` deliberately drops four fields because they
carry no information (``round`` is the slot spelled differently,
``current_round`` and ``eliminated_position`` are read by nothing in the tree,
and the cached competitor name is a snapshot the foreign key exists to replace)
and it normalises a fifth (``is_bye`` becomes uniform where the generator wrote
it unevenly). Every one of those five is argued in the module docstring of
``services/birling_rows.py``. This module makes each of them a named test, so
that a sixth difference cannot appear quietly by being lumped in with the ones
that were decided on purpose.

The shape of the proof:

**``normalise`` is the contract.** Rather than asserting field by field on a
loaded document, the tests transform the stored document by exactly the five
declared rules and then demand equality with what came back out of the tables.
An undeclared difference therefore fails as an inequality rather than passing
because nobody wrote an assertion for it.

**Refusal is tested per reason.** ``load_document`` raises rather than returns
on five distinct defects. Each gets its own test, and each is produced by
damaging real rows in the way the schema actually permits, not by mocking the
loader.

**Falls and placements get a completed bracket.** ``birling_falls`` and
``birling_placements`` have no production data anywhere in the tree, so a
bracket driven to a champion through the real service is their only coverage on
the inverse just as it was on the forward half.

Isolation note carried over from ``tests/test_birling_rows.py``:
``BirlingBracket._save_bracket_data`` commits and so escapes the ``db_session``
savepoint. Every assertion below is scoped to its own event id.
"""
from __future__ import annotations

import json

import pytest

from database import db
from models import BirlingMatch, BirlingSeed
from services import birling_rows as rows
from services.birling_bracket import BirlingBracket
from tests.conftest import (
    make_college_competitor,
    make_event,
    make_pro_competitor,
    make_team,
    make_tournament,
)

# ---------------------------------------------------------------------------
# A world, a bracket, and the contract the round trip has to meet
# ---------------------------------------------------------------------------

def _world(session, name="Inverse"):
    """A tournament, a team, five competitors and one college bracket event."""
    tour = make_tournament(session)
    team = make_team(session, tour)
    people = [make_college_competitor(session, tour, team, f"Person {letter}")
              for letter in "ABCDE"]
    session.flush()
    event = make_event(session, tour, name, event_type="college",
                       scoring_type="bracket")
    session.flush()
    return tour, team, event, people


def _entrants(people):
    """What the birling route hands the generator.

    ``routes/scheduling/birling.py`` line 181 builds these from
    ``display_name``, so the stored name for a college competitor already
    carries the team code. Built from the bare ``name`` here on purpose, so
    that the live-join rule has something to change and cannot pass by
    coincidence.
    """
    return [{"id": p.id, "name": p.name} for p in people]


def _drive_to_a_champion(event):
    """Record falls until nothing is open.

    Two falls decide a match, so each open match gets two for whoever sits in
    slot one. This is the only way ``birling_falls`` and ``birling_placements``
    acquire rows, on either side of the projection.
    """
    for _pass in range(24):
        open_matches = [m for m in BirlingBracket(event).get_current_matches()
                        if not m.get("winner")]
        if not open_matches:
            return
        live = BirlingBracket(event)
        for match in open_matches:
            winner = match.get("competitor1") or match.get("competitor2")
            if not winner:
                continue
            for _fall in range(2):
                result = live.record_fall(match["match_id"], winner)
                if result.get("match_decided"):
                    break


def normalise(stored, names):
    """The stored document rewritten by the five declared differences.

    This function IS the claim A3a makes. If it needs a sixth rule to make a
    test pass, the inverse has lost something nobody agreed it could lose.

    ``names`` maps bare competitor id to the display name the join will return.
    """
    document = json.loads(json.dumps(stored))

    # 1. The cached name becomes a live join, and 2. the entrant list comes
    #    back in seed order rather than in call order.
    seeding = document.get("seeding") or []
    document["competitors"] = [{"id": cid, "name": names[cid]}
                               for cid in seeding]

    # 3. ``current_round`` is regenerated as the constant it has always held.
    document["current_round"] = "winners_1"

    bracket = document.get("bracket") or {}
    for side in ("winners", "losers"):
        for rnd in bracket.get(side) or []:
            for match in rnd:
                # 4. ``is_bye`` is emitted on both list sides, where
                #    ``_sweep_losers_byes`` only adds it to a losers match
                #    after the fact.
                match["is_bye"] = bool(match.get("is_bye", False))
                # 5. ``eliminated_position`` is regenerated as the None that
                #    its only writer writes.
                if side == "losers":
                    match["eliminated_position"] = None
                else:
                    match.pop("eliminated_position", None)
                # ``round`` is regenerated from the slot. Same value, and it
                # is rewritten here so a stored value that disagreed with its
                # own slot would fail rather than be believed. Spelled out
                # literally rather than through ``rows.round_name``, because an
                # expectation computed by the code under test proves nothing.
                match["round"] = "%s_%d" % (side, rnd_index(bracket, side, rnd) + 1)
    for side in ("finals", "true_finals"):
        match = bracket.get(side)
        if match is None:
            continue
        match["round"] = side
        match.pop("is_bye", None)
        match.pop("eliminated_position", None)
        if side == "true_finals":
            match["needed"] = bool(match.get("needed", False))
        else:
            match.pop("needed", None)

    document["placements"] = {str(k): v
                              for k, v in (document.get("placements") or {}).items()}
    if not document.get("pre_seedings"):
        document.pop("pre_seedings", None)
    else:
        document["pre_seedings"] = {str(k): v
                                    for k, v in document["pre_seedings"].items()}
    return document


def rnd_index(bracket, side, rnd):
    """Which round of its side a round list is, by identity."""
    for index, candidate in enumerate(bracket[side]):
        if candidate is rnd:
            return index
    raise AssertionError("round not in its own side")


def _names(people, team):
    """``{bare id: display name}``, built the way the property builds it."""
    return {p.id: f"{p.name} ({team.team_code})" for p in people}


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------

class TestTheRoundTrip:
    """Forward then inverse is the identity, modulo the five declared rules."""

    @pytest.fixture()
    def generated(self, db_session):
        _tour, team, event, people = _world(db_session, "Generated")
        BirlingBracket(event).generate_bracket(_entrants(people[:4]))
        db.session.flush()
        return team, event, people

    @pytest.fixture()
    def finished(self, db_session):
        _tour, team, event, people = _world(db_session, "Finished")
        BirlingBracket(event).generate_bracket(_entrants(people[:5]))
        _drive_to_a_champion(event)
        db.session.flush()
        return team, event, people

    def test_a_fresh_bracket_survives_it(self, generated):
        team, event, people = generated
        stored = json.loads(event.payouts)
        assert rows.load_document(event) == normalise(stored, _names(people, team))

    def test_a_bracket_driven_to_a_champion_survives_it(self, finished):
        team, event, people = finished
        stored = json.loads(event.payouts)
        assert stored["placements"], "the driver did not finish the bracket"
        assert rows.load_document(event) == normalise(stored, _names(people, team))

    def test_a_five_competitor_bracket_survives_it(self, db_session):
        """Five entrants means byes, which is the shape ``is_bye`` exists for."""
        _tour, team, event, people = _world(db_session, "Byes")
        BirlingBracket(event).generate_bracket(_entrants(people[:5]))
        db.session.flush()
        stored = json.loads(event.payouts)
        assert rows.load_document(event) == normalise(stored, _names(people, team))

    def test_an_event_with_no_rows_gets_the_skeleton(self, db_session):
        _tour, _team, event, _people = _world(db_session, "Bare")
        assert rows.load_document(event) == rows.empty_document()

    def test_the_skeleton_is_the_services_own_skeleton(self, db_session):
        """A3b makes the inverse the replacement for ``_load_bracket_data``'s
        fallback. A reader must not be able to tell which of the two it got."""
        _tour, _team, event, _people = _world(db_session, "Skeleton")
        assert BirlingBracket(event).bracket_data == rows.empty_document()

    def test_falls_come_back_with_their_numbers_and_winners(self, finished):
        _team, event, people = finished
        stored = json.loads(event.payouts)
        loaded = rows.load_document(event)
        stored_falls = _all_falls(stored)
        assert stored_falls, "the driver recorded no falls"
        assert _all_falls(loaded) == stored_falls

    def test_a_fall_timestamp_survives_the_datetime_column(self, finished):
        _team, event, _people = finished
        stored = _all_falls(json.loads(event.payouts))
        loaded = _all_falls(rows.load_document(event))
        assert [f["recorded_at"] for f in loaded] == [f["recorded_at"] for f in stored]
        assert all(f["recorded_at"].endswith("+00:00") for f in loaded)

    def test_placements_come_back_keyed_by_string(self, finished):
        _team, event, _people = finished
        loaded = rows.load_document(event)
        assert loaded["placements"] == json.loads(event.payouts)["placements"]
        assert all(isinstance(k, str) for k in loaded["placements"])


def _all_falls(document):
    """Every fall in a document, in match order then fall order."""
    out = []
    bracket = document.get("bracket") or {}
    for side in ("winners", "losers"):
        for rnd in bracket.get(side) or []:
            for match in rnd:
                out.extend(sorted(match.get("falls") or [],
                                  key=lambda f: f["fall_number"]))
    for side in ("finals", "true_finals"):
        match = bracket.get(side)
        if match:
            out.extend(sorted(match.get("falls") or [],
                              key=lambda f: f["fall_number"]))
    return out


# ---------------------------------------------------------------------------
# The five declared differences, one test apiece
# ---------------------------------------------------------------------------

class TestWhatTheInverseRegenerates:
    """Each of the four dropped fields and the one normalised one.

    These are the tests that would have to be deleted, not merely edited, if
    somebody decided a dropped field mattered after all. That is the point of
    stating them separately from the round trip.
    """

    @pytest.fixture()
    def generated(self, db_session):
        _tour, team, event, people = _world(db_session, "Regenerated")
        BirlingBracket(event).generate_bracket(_entrants(people[:4]))
        db.session.flush()
        return team, event, people

    def test_round_is_rebuilt_from_the_slot(self, generated):
        _team, event, _people = generated
        loaded = rows.load_document(event)
        for index, rnd in enumerate(loaded["bracket"]["winners"]):
            assert all(m["round"] == f"winners_{index + 1}" for m in rnd)
        for index, rnd in enumerate(loaded["bracket"]["losers"]):
            assert all(m["round"] == f"losers_{index + 1}" for m in rnd)

    def test_round_survives_a_stored_value_that_lied(self, generated):
        """``round`` is derived, so a document whose stored string disagreed
        with its own position comes back agreeing. Nothing reads it, which is
        why this is a repair rather than a loss."""
        _team, event, _people = generated
        doc = json.loads(event.payouts)
        doc["bracket"]["winners"][0][0]["round"] = "losers_9"
        event.payouts = json.dumps(doc)
        rows.project(event)
        db.session.flush()
        assert rows.load_document(event)["bracket"]["winners"][0][0]["round"] == "winners_1"

    def test_current_round_is_the_constant(self, generated):
        _team, event, _people = generated
        assert rows.load_document(event)["current_round"] == "winners_1"

    def test_eliminated_position_is_none_on_every_losers_match(self, generated):
        _team, event, _people = generated
        losers = [m for rnd in rows.load_document(event)["bracket"]["losers"]
                  for m in rnd]
        assert losers
        assert all(m["eliminated_position"] is None for m in losers)

    def test_eliminated_position_is_absent_from_a_winners_match(self, generated):
        _team, event, _people = generated
        winners = [m for rnd in rows.load_document(event)["bracket"]["winners"]
                   for m in rnd]
        assert all("eliminated_position" not in m for m in winners)

    def test_is_bye_is_on_every_match_of_both_list_sides(self, db_session):
        """The generator writes it on winners always and on losers only when
        ``_sweep_losers_byes`` fires. A match shape that depends on the
        bracket's history is worse than one that does not, and every reader in
        the tree reaches it with ``.get``."""
        _tour, _team, event, people = _world(db_session, "Uniform")
        BirlingBracket(event).generate_bracket(_entrants(people[:4]))
        db.session.flush()
        bracket = rows.load_document(event)["bracket"]
        for side in ("winners", "losers"):
            for rnd in bracket[side]:
                assert all(isinstance(m["is_bye"], bool) for m in rnd)

    def test_is_bye_is_absent_from_the_grand_finals(self, db_session):
        """A grand final cannot be a bye, and claiming it is not one is still a
        claim. The generator does not write the key there and neither does
        this."""
        _tour, _team, event, people = _world(db_session, "NoByeFinals")
        BirlingBracket(event).generate_bracket(_entrants(people[:4]))
        _drive_to_a_champion(event)
        db.session.flush()
        bracket = rows.load_document(event)["bracket"]
        assert bracket["finals"] is not None
        assert "is_bye" not in bracket["finals"]

    def test_the_name_comes_from_the_join_and_not_from_the_document(self, db_session):
        """The one visible behaviour change in the whole commit. A competitor
        renamed after a bracket was generated reads correctly on a page that
        used to show the old name."""
        _tour, team, event, people = _world(db_session, "Renamed")
        BirlingBracket(event).generate_bracket(_entrants(people[:4]))
        db.session.flush()

        subject = people[0]
        stored_name = next(c["name"] for c in json.loads(event.payouts)["competitors"]
                           if c["id"] == subject.id)
        subject.name = "Renamed Person"
        db.session.flush()

        loaded_name = next(c["name"] for c in rows.load_document(event)["competitors"]
                           if c["id"] == subject.id)
        assert loaded_name != stored_name
        assert loaded_name == f"Renamed Person ({team.team_code})"

    def test_the_name_is_the_display_name_and_not_the_bare_name(self, db_session):
        """``routes/scheduling/birling.py`` builds the stored list from
        ``display_name``, so the join has to use the same property or the team
        code would vanish off every bracket page in the tournament."""
        _tour, team, event, people = _world(db_session, "Displayed")
        BirlingBracket(event).generate_bracket(_entrants(people[:4]))
        db.session.flush()
        loaded = rows.load_document(event)["competitors"]
        assert all(n["name"].endswith(f"({team.team_code})") for n in loaded)

    def test_a_pro_bracket_takes_the_pro_display_name(self, db_session):
        """``ProCompetitor.display_name`` is the bare name, with no team."""
        tour = make_tournament(db_session)
        people = [make_pro_competitor(db_session, tour, f"Pro {letter}")
                  for letter in "ABCD"]
        db_session.flush()
        event = make_event(db_session, tour, "ProBirling", event_type="pro",
                           scoring_type="bracket")
        db_session.flush()
        BirlingBracket(event).generate_bracket(_entrants(people))
        db.session.flush()
        loaded = rows.load_document(event)["competitors"]
        assert sorted(n["name"] for n in loaded) == sorted(p.name for p in people)

    def test_the_entrant_list_comes_back_in_seed_order(self, db_session):
        _tour, _team, event, people = _world(db_session, "Ordered")
        entrants = _entrants(people[:4])
        BirlingBracket(event).generate_bracket(entrants)
        db.session.flush()
        loaded = rows.load_document(event)
        assert [c["id"] for c in loaded["competitors"]] == loaded["seeding"]


# ---------------------------------------------------------------------------
# Refusal
# ---------------------------------------------------------------------------

class TestTheInverseRefuses:
    """Where the forward half files a reason, the inverse raises.

    Refusing to save loses a judge's work, so ``project`` never raises.
    Refusing to load loses a page, and the alternative is a document built
    around a reference that resolved to the wrong human, in the same layout as
    the right one. That is the failure the era-1 reseed inflicted once and it
    is the failure these tables exist to prevent.
    """

    @pytest.fixture()
    def generated(self, db_session):
        _tour, team, event, people = _world(db_session, "Refused")
        BirlingBracket(event).generate_bracket(_entrants(people[:4]))
        db.session.flush()
        return team, event, people

    def test_an_unresolvable_reference_raises(self, generated):
        """Produced by moving the event to the other roster, which is legal
        under the ``event_type`` check constraint and leaves every foreign key
        intact. The uids are real; they are simply not this pool's."""
        _team, event, _people = generated
        event.event_type = "pro"
        db.session.flush()
        with pytest.raises(rows.UnloadableBracket) as caught:
            rows.load_document(event)
        assert str(caught.value) == rows.UNRESOLVABLE

    def test_an_event_naming_no_pool_raises_when_it_has_rows(self, generated):
        _team, event, _people = generated

        class _Unpooled:
            id = event.id
            event_type = "relay"

        with pytest.raises(rows.UnloadableBracket) as caught:
            rows.load_document(_Unpooled())
        assert str(caught.value) == rows.NO_POOL

    def test_an_event_naming_no_pool_and_holding_no_rows_is_empty(self, db_session):
        """No rows means no references, so there is nothing to fail to
        resolve. An unpooled event with an empty bracket is still an empty
        bracket."""
        _tour, _team, event, _people = _world(db_session, "UnpooledBare")

        class _Unpooled:
            id = event.id
            event_type = "relay"

        assert rows.load_document(_Unpooled()) == rows.empty_document()

    def test_a_gap_in_the_seed_numbers_raises(self, generated):
        """Renumbering silently would hand every competitor below the gap
        somebody else's seed."""
        _team, event, _people = generated
        last = (BirlingSeed.query.filter_by(event_id=event.id)
                .order_by(BirlingSeed.seed_number.desc()).first())
        last.seed_number = last.seed_number + 3
        db.session.flush()
        with pytest.raises(rows.UnloadableBracket) as caught:
            rows.load_document(event)
        assert str(caught.value) == rows.SEED_GAP

    def test_a_gap_in_the_round_indices_raises(self, generated):
        """Reindexing silently would move every match in the side up a round,
        which is a different tournament."""
        _team, event, _people = generated
        moved = BirlingMatch.query.filter_by(event_id=event.id, side="winners",
                                             round_index=1).all()
        assert moved, "a four competitor bracket has a second winners round"
        for row in moved:
            row.round_index = 6
        db.session.flush()
        with pytest.raises(rows.UnloadableBracket) as caught:
            rows.load_document(event)
        assert str(caught.value) == rows.ROUND_GAP

    def test_a_gap_in_the_positions_raises(self, generated):
        """Position is the pairing. Closing a gap silently would repair the
        count and lose which match was which."""
        _team, event, _people = generated
        row = BirlingMatch.query.filter_by(event_id=event.id, side="winners",
                                           round_index=0, position=2).first()
        assert row is not None
        row.position = 4
        db.session.flush()
        with pytest.raises(rows.UnloadableBracket) as caught:
            rows.load_document(event)
        assert str(caught.value) == rows.POSITION_GAP

    def test_a_grand_final_outside_its_slot_raises(self, db_session):
        _tour, _team, event, people = _world(db_session, "Displaced")
        BirlingBracket(event).generate_bracket(_entrants(people[:4]))
        _drive_to_a_champion(event)
        db.session.flush()
        row = BirlingMatch.query.filter_by(event_id=event.id, side="finals").first()
        assert row is not None
        row.round_index = 2
        db.session.flush()
        with pytest.raises(rows.UnloadableBracket) as caught:
            rows.load_document(event)
        assert str(caught.value) == rows.FINALS_SHAPE

    def test_two_grand_finals_raise(self, db_session):
        _tour, _team, event, people = _world(db_session, "Doubled")
        BirlingBracket(event).generate_bracket(_entrants(people[:4]))
        _drive_to_a_champion(event)
        db.session.flush()
        db.session.add(BirlingMatch(event_id=event.id, match_id="F1B",
                                    side="finals", round_index=0, position=2,
                                    is_bye=False))
        db.session.flush()
        with pytest.raises(rows.UnloadableBracket) as caught:
            rows.load_document(event)
        assert str(caught.value) == rows.FINALS_SHAPE

    def test_a_side_the_document_has_no_home_for_raises(self, monkeypatch):
        """Unreachable through the schema: ``birling_matches`` carries a check
        constraint naming the four sides. Guarded anyway and tested on a
        stand-in, because dropping a side quietly is how a bracket loses half
        of itself without anybody noticing."""
        class _Row:
            side = "consolation"
            round_index = 0
            position = 1
            match_id = "C1"
            competitor1_uid = competitor2_uid = winner_uid = loser_uid = None
            is_bye = False
            needed = None
            falls = ()

        class _Event:
            id = -1
            event_type = "college"

        monkeypatch.setattr(rows, "load_rows",
                            lambda event_id: ([], [], [_Row()], []))
        with pytest.raises(rows.UnloadableBracket) as caught:
            rows.load_document(_Event())
        assert str(caught.value) == rows.FINALS_SHAPE

    def test_a_document_the_forward_half_refused_leaves_nothing_to_load(
            self, db_session, reference_gate_disarmed):
        """The two halves compose into the behaviour A3b needs. ``project``
        refuses and writes no rows; ``load_document`` then returns the skeleton
        rather than raising, because absent rows are not a damaged bracket.
        Telling that apart from an event that never had one is ``is_projected``
        and is the reader's call."""
        _tour, _team, event, people = _world(db_session, "Ghosted")
        BirlingBracket(event).generate_bracket(_entrants(people[:4]))

        doc = json.loads(event.payouts)
        doc["seeding"][0] = max(p.id for p in people) + 5000
        doc["competitors"][0]["id"] = doc["seeding"][0]
        event.payouts = json.dumps(doc)
        with pytest.raises(rows.ProjectionRefused) as caught:
            rows.project(event)
        db.session.flush()

        assert caught.value.reasons
        assert rows.load_document(event) == rows.empty_document()
        assert rows.is_projected(event.id) is False


# ---------------------------------------------------------------------------
# is_projected
# ---------------------------------------------------------------------------

class TestIsProjected:
    """The question A3b asks before it decides what to show.

    A document that claims a bracket plus an event with no rows is either a
    save the projector refused or a backfill that skipped the event. Either way
    the projector will not decide what a page does about it, because a
    projector that decided it would be deciding for readers it has never met.
    """

    def test_a_generated_bracket_is_projected(self, db_session):
        _tour, _team, event, people = _world(db_session, "Projected")
        BirlingBracket(event).generate_bracket(_entrants(people[:4]))
        db.session.flush()
        assert rows.is_projected(event.id) is True

    def test_an_event_with_no_bracket_is_not(self, db_session):
        _tour, _team, event, _people = _world(db_session, "Unprojected")
        assert rows.is_projected(event.id) is False

    def test_pre_seeds_alone_count(self, db_session):
        """The rankings page writes ``pre_seedings`` before any bracket exists,
        so an event can be projected without having a single match."""
        _tour, _team, event, people = _world(db_session, "PreOnly")
        event.payouts = json.dumps({"pre_seedings": {str(people[0].id): 1}})
        rows.project(event)
        db.session.flush()
        assert rows.is_projected(event.id) is True

    def test_an_event_that_stops_being_a_bracket_stops_being_projected(self, db_session):
        _tour, _team, event, people = _world(db_session, "Demoted")
        BirlingBracket(event).generate_bracket(_entrants(people[:4]))
        event.payouts = json.dumps({"1st": 500})
        rows.project(event)
        db.session.flush()
        assert rows.is_projected(event.id) is False


# ---------------------------------------------------------------------------
# pre_seedings
# ---------------------------------------------------------------------------

class TestPreSeedings:
    """The one key the inverse emits conditionally.

    An empty map is a claim that the rankings page ran and found nobody. The
    document has only ever carried this key when the page wrote it, so absence
    is the honest answer when there are no rows.
    """

    def test_they_come_back_keyed_by_string(self, db_session):
        _tour, _team, event, people = _world(db_session, "PreSeeded")
        pre = {str(p.id): index for index, p in enumerate(people[:3], start=1)}
        event.payouts = json.dumps({"pre_seedings": pre})
        rows.project(event)
        db.session.flush()
        assert rows.load_document(event)["pre_seedings"] == pre

    def test_the_key_is_absent_when_no_rows_exist(self, db_session):
        _tour, _team, event, people = _world(db_session, "NoPreSeeds")
        BirlingBracket(event).generate_bracket(_entrants(people[:4]))
        db.session.flush()
        assert "pre_seedings" not in rows.load_document(event)

    def test_sparse_seed_numbers_are_not_a_gap(self, db_session):
        """Unlike ``seeding``, a pre-seeding map is whatever the judge dragged.
        Numbers 1, 4 and 9 are a legitimate map and not a damaged one."""
        _tour, _team, event, people = _world(db_session, "Sparse")
        pre = {str(people[0].id): 1, str(people[1].id): 4, str(people[2].id): 9}
        event.payouts = json.dumps({"pre_seedings": pre})
        rows.project(event)
        db.session.flush()
        assert rows.load_document(event)["pre_seedings"] == pre


# ---------------------------------------------------------------------------
# The pure helpers
# ---------------------------------------------------------------------------

class TestThePureHelpers:
    """No database. These are the parts a reader can reason about by hand."""

    def test_round_name_is_one_based_on_the_list_sides(self):
        assert rows.round_name("winners", 0) == "winners_1"
        assert rows.round_name("losers", 3) == "losers_4"

    def test_round_name_ignores_the_index_on_the_singletons(self):
        assert rows.round_name("finals", 0) == "finals"
        assert rows.round_name("true_finals", 7) == "true_finals"

    def test_round_name_inverts_the_generators_own_spelling(self):
        """``_generate_winners_bracket`` writes ``f'winners_{n}'`` counting
        from one and ``plan_matches`` stores ``enumerate`` counting from zero.
        This is the only place that offset is reconciled."""
        for index in range(6):
            assert rows.round_name("winners", index) == f"winners_{index + 1}"

    def test_a_timestamp_survives_the_column_round_trip(self):
        original = "2026-04-24T17:03:09.123456+00:00"
        assert rows.format_timestamp(rows.parse_timestamp(original)) == original

    def test_a_naive_stored_timestamp_comes_back_as_utc(self):
        from datetime import datetime

        assert rows.format_timestamp(datetime(2026, 4, 25, 9, 0, 0)) == \
            "2026-04-25T09:00:00+00:00"

    def test_a_missing_timestamp_stays_missing(self):
        assert rows.format_timestamp(None) is None

    def test_the_skeleton_carries_exactly_five_keys(self):
        assert sorted(rows.empty_document()) == [
            "bracket", "competitors", "current_round", "placements", "seeding"]

    def test_the_skeleton_is_a_fresh_object_every_time(self):
        """Handed to a service that mutates it in place. A shared default would
        leak one event's bracket into the next one's page."""
        first = rows.empty_document()
        first["bracket"]["winners"].append(["contamination"])
        assert rows.empty_document()["bracket"]["winners"] == []

    def test_the_reverse_pool_refuses_an_unknown_event_type(self):
        assert rows.reverse_pool_for("relay", {1, 2}) is None

    def test_the_reverse_pool_asks_nothing_when_nothing_is_referenced(self):
        assert rows.reverse_pool_for("college", set()) == {}
        assert rows.reverse_pool_for("college", {None}) == {}
