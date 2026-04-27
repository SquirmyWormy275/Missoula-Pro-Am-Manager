"""
Tests for the birling bracket at non-power-of-two entrant counts (9, 10, 11).

Context: V2.14.14 rewrote the bracket generator to produce compact brackets
(no power-of-two upsizing).  The race-weekend regression for the Women's
College bracket showed W1_1..W1_7 each with a single seeded name and W1_8
with TWO names stacked in it — classic "seed 9 jammed into slot 8" symptom.

This file locks in the invariant: every first-round match slot must contain
AT MOST ONE competitor id.  Never two.  And the total number of distinct
competitors referenced by round-1 + the seed-1 auto-advanced bye must equal
the field size exactly.

Also renders the blank print template for the N=9 bracket and asserts each
unique competitor name appears in its own bracket-slot cell.

Run:  pytest tests/test_birling_bracket_9.py -v
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from services.birling_bracket import BirlingBracket
from services.birling_print import build_birling_print_context

# ---------------------------------------------------------------------------
# Helpers (mirror tests/test_birling_bracket_12.py style: no DB, mocked)
# ---------------------------------------------------------------------------


def _mock_event(payouts="{}", event_type="college"):
    ev = MagicMock()
    ev.payouts = payouts
    ev.event_type = event_type
    ev.id = 1
    ev.status = "pending"
    ev.scoring_type = "bracket"
    return ev


def _bracket_n(n: int):
    """Create an N-competitor bracket with mocked DB."""
    comps = [{"id": i + 1, "name": f"Seed{i + 1}"} for i in range(n)]
    with patch("services.birling_bracket.db"):
        ev = _mock_event()
        b = BirlingBracket(ev)
        b.generate_bracket(comps)
    return b, comps


# ---------------------------------------------------------------------------
# Invariants for any non-power-of-two field
# ---------------------------------------------------------------------------


class TestCompactShapeInvariants:
    """These invariants must hold for every N >= 2, power-of-two or not.

    The N=9 print-bug this class locks down was a renderer symptom of a
    generator-shape bug: prior to V2.14.14 the generator rounded up to 16
    slots and the last slot ended up with two competitor ids.  Even though
    V2.14.14 fixed the generator, a regression here would silently reappear
    as two-names-in-one-slot on the printed bracket.
    """

    @pytest.mark.parametrize("n", [9, 10, 11, 13, 15])
    def test_no_first_round_slot_has_more_than_one_competitor(self, n):
        b, _ = _bracket_n(n)
        first_round = b.bracket_data["bracket"]["winners"][0]
        # Each match dict has AT MOST competitor1 + competitor2.  The
        # generator must never produce a slot shape that stacks a third
        # competitor anywhere — this is the "W1_8 has two names stacked"
        # symptom at its structural source.
        for m in first_round:
            # competitor1 and competitor2 are the only two slot fields; any
            # extra integer-valued key would be a stacking bug.
            extra_slot_keys = [
                k
                for k, v in m.items()
                if k
                not in (
                    "competitor1",
                    "competitor2",
                    "winner",
                    "loser",
                    "match_id",
                    "round",
                    "falls",
                    "is_bye",
                    "eliminated_position",
                )
                and isinstance(v, int)
            ]
            assert not extra_slot_keys, (
                f"Match {m['match_id']} has unexpected extra int slot(s): "
                f"{extra_slot_keys} — would stack competitors in one slot"
            )

    @pytest.mark.parametrize("n", [9, 10, 11, 13, 15])
    def test_total_round_1_competitors_equals_field_size(self, n):
        b, _ = _bracket_n(n)
        first_round = b.bracket_data["bracket"]["winners"][0]
        ids_present = set()
        for m in first_round:
            for slot in ("competitor1", "competitor2"):
                if m.get(slot) is not None:
                    ids_present.add(m[slot])
        # Every seed must appear exactly once somewhere in round 1 (a bye
        # match counts, since competitor1 is the lone seed).
        assert ids_present == set(range(1, n + 1)), (
            f"N={n}: round-1 competitor ids {sorted(ids_present)} "
            f"!= expected {list(range(1, n + 1))}"
        )

    @pytest.mark.parametrize("n", [9, 10, 11, 13, 15])
    def test_no_duplicate_competitor_in_round_1(self, n):
        b, _ = _bracket_n(n)
        first_round = b.bracket_data["bracket"]["winners"][0]
        seen = []
        for m in first_round:
            for slot in ("competitor1", "competitor2"):
                v = m.get(slot)
                if v is not None:
                    seen.append(v)
        assert len(seen) == len(set(seen)), (
            f"N={n}: round-1 has duplicate competitor ids {seen} — "
            "same seed appears in two slots (stacking bug)"
        )


# ---------------------------------------------------------------------------
# N=9 — specific expected shape
# ---------------------------------------------------------------------------


class TestBracketShape9:
    """9 competitors should produce 5 total round-1 matches:
    one bye (seed 1 auto-advances) + four mirror-paired matches (2v9, 3v8,
    4v7, 5v6).  There must be no eighth match.  There must be no slot with
    two competitor ids packed into a single seed position."""

    def test_round_1_has_exactly_five_matches(self):
        b, _ = _bracket_n(9)
        first_round = b.bracket_data["bracket"]["winners"][0]
        assert len(first_round) == 5, (
            f"Expected 5 round-1 matches for N=9 (1 bye + 4 actual), "
            f"got {len(first_round)}.  If this is 8, the power-of-two "
            "upsize regression is back."
        )

    def test_exactly_one_first_round_bye(self):
        b, _ = _bracket_n(9)
        first_round = b.bracket_data["bracket"]["winners"][0]
        byes = [m for m in first_round if m["is_bye"]]
        assert len(byes) == 1
        assert byes[0]["competitor1"] == 1
        assert byes[0]["competitor2"] is None

    def test_bye_seed_auto_advances(self):
        b, _ = _bracket_n(9)
        first_round = b.bracket_data["bracket"]["winners"][0]
        byes = [m for m in first_round if m["is_bye"]]
        assert (
            byes[0]["winner"] == 1
        ), "Seed 1's W1 bye must auto-advance at generation time."

    def test_mirror_pair_matchups(self):
        b, _ = _bracket_n(9)
        first_round = b.bracket_data["bracket"]["winners"][0]
        non_bye = [m for m in first_round if not m["is_bye"]]
        # Expected mirror pairings after the bye removes seed 1:
        # 2 vs 9, 3 vs 8, 4 vs 7, 5 vs 6
        pairs = {tuple(sorted((m["competitor1"], m["competitor2"]))) for m in non_bye}
        assert pairs == {
            (2, 9),
            (3, 8),
            (4, 7),
            (5, 6),
        }, f"Unexpected N=9 mirror pairs: {pairs}"

    def test_no_w1_8_slot_exists(self):
        """The pre-V2.14.14 power-of-two code generated W1_1..W1_8 for N=9,
        with W1_8 stacking seed 8 AND seed 9.  Assert that match id simply
        does not exist in the compact layout."""
        b, _ = _bracket_n(9)
        first_round = b.bracket_data["bracket"]["winners"][0]
        ids = {m["match_id"] for m in first_round}
        assert "W1_8" not in ids, (
            "W1_8 should not exist for N=9 — compact bracket has 5 slots "
            "(W1_1 bye + W1_2..W1_5)."
        )
        assert "W1_6" not in ids
        assert "W1_7" not in ids

    def test_winners_bracket_round_count(self):
        """N=9 compact: 5 → 3 → 2 → 1 → (none)."""
        b, _ = _bracket_n(9)
        winners = b.bracket_data["bracket"]["winners"]
        counts = [len(r) for r in winners]
        # 5 round-1 matches → ceil(5/2)=3 → ceil(3/2)=2 → ceil(2/2)=1 → stop.
        assert counts == [5, 3, 2, 1], f"Unexpected round counts: {counts}"


# ---------------------------------------------------------------------------
# N=10, N=11 — additional compact shapes
# ---------------------------------------------------------------------------


class TestBracketShape10And11:
    def test_n10_has_five_matches_zero_byes(self):
        """10 is even: no bye, 5 mirror-paired matches."""
        b, _ = _bracket_n(10)
        first_round = b.bracket_data["bracket"]["winners"][0]
        assert len(first_round) == 5
        byes = [m for m in first_round if m["is_bye"]]
        assert len(byes) == 0
        pairs = {
            tuple(sorted((m["competitor1"], m["competitor2"]))) for m in first_round
        }
        assert pairs == {(1, 10), (2, 9), (3, 8), (4, 7), (5, 6)}

    def test_n11_has_six_matches_one_bye(self):
        """11 is odd: 1 bye for seed 1, 5 mirror-paired matches."""
        b, _ = _bracket_n(11)
        first_round = b.bracket_data["bracket"]["winners"][0]
        assert len(first_round) == 6
        byes = [m for m in first_round if m["is_bye"]]
        assert len(byes) == 1
        assert byes[0]["competitor1"] == 1
        non_bye = [m for m in first_round if not m["is_bye"]]
        pairs = {tuple(sorted((m["competitor1"], m["competitor2"]))) for m in non_bye}
        assert pairs == {(2, 11), (3, 10), (4, 9), (5, 8), (6, 7)}


# ---------------------------------------------------------------------------
# Print render: every seed name appears in exactly ONE bracket-slot cell
# ---------------------------------------------------------------------------


class TestPrintRender9:
    """Render the blank-bracket print template with N=9 and assert every
    seed name appears in its own match-slot div — never two names packed
    into the same slot."""

    def _render(self, n: int) -> str:
        # Build bracket (generator) + scrubbed print context (same path as
        # the real /birling/print-blank route).
        comps = [{"id": i + 1, "name": f"Seed{i + 1}"} for i in range(n)]
        with patch("services.birling_bracket.db"):
            ev = _mock_event()
            b = BirlingBracket(ev)
            b.generate_bracket(comps)
        # Round-trip event.payouts through JSON so build_birling_print_context
        # picks up the just-generated state (mock assigns the string).
        ev.payouts = b.event.payouts  # MagicMock captured the commit
        # Actually: generator called _save_bracket_data which set
        # event.payouts = json.dumps(...).  MagicMock records that as a
        # normal attribute write, so ev.payouts is now a string.  Confirm:
        assert isinstance(ev.payouts, str)

        ctx = build_birling_print_context(ev)
        assert ctx is not None

        # Render via Jinja pointed at the project's templates/ dir.
        import os

        proj_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        env = Environment(
            loader=FileSystemLoader(os.path.join(proj_root, "templates")),
            autoescape=select_autoescape(["html"]),
        )
        tmpl = env.get_template("scoring/birling_bracket_print.html")
        # Fake minimal objects with the attributes the template reads.
        fake_event = MagicMock()
        fake_event.display_name = "Women's College Birling"
        return tmpl.render(
            brackets=[{"event": fake_event, "ctx": ctx}],
            year=2026,
        )

    def test_every_seed_appears_in_its_own_slot_cell(self):
        html = self._render(9)
        # Each seed name should appear in one <div class="match-slot">CELL</div>.
        # Count the number of cells containing the literal seed name;
        # never two seed names in one cell.
        # The slot macro renders:
        #   <div class="match-slot">{{ name }}</div>
        # or
        #   <div class="match-slot"><span class="tbd">___</span></div>
        slot_pattern = re.compile(
            r'<div class="match-slot">\s*([^<]*?)\s*</div>', re.DOTALL
        )
        slots = [s.strip() for s in slot_pattern.findall(html)]

        # A slot contains plain text (the seed name), or empty, or nested
        # tags (the TBD case captures empty text here).  Collect the ones
        # that match our seed names.
        seed_slots: dict[str, int] = {}
        for s in slots:
            # If the slot text matches a seed name, count it.
            m = re.fullmatch(r"(Seed\d+)", s)
            if m:
                seed_slots[m.group(1)] = seed_slots.get(m.group(1), 0) + 1

        # Every seed 1..9 must appear at least once.
        for i in range(1, 10):
            assert f"Seed{i}" in seed_slots, (
                f"Seed{i} missing from rendered bracket — "
                f"found only: {sorted(seed_slots)}"
            )
        # And no seed should appear twice (that's the stacking bug).
        dupes = {k: v for k, v in seed_slots.items() if v > 1}
        assert not dupes, (
            f"These seeds appear in more than one slot cell: {dupes} — "
            "stacking bug regression."
        )

    def test_no_slot_contains_two_seed_names_stacked(self):
        """Direct check for the reported symptom: one slot with two names."""
        html = self._render(9)
        slot_pattern = re.compile(r'<div class="match-slot">([^<]*?)</div>', re.DOTALL)
        for raw in slot_pattern.findall(html):
            text = raw.strip()
            # A slot whose plain-text content matches 2+ Seed tokens is the
            # exact bug from race-weekend.
            tokens = re.findall(r"Seed\d+", text)
            assert len(tokens) <= 1, (
                f"Slot contains multiple seed names stacked: "
                f"{tokens!r} in cell: {text!r}"
            )
