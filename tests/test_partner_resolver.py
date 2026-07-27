"""
Tests for services/partner_resolver.py.

Covers the extraction of partner-pairing logic from heat_sheets.py (2026-04-20).
Three CRITICAL regression tests assert that the new service produces
byte-for-byte identical output to the pre-extraction inline code in:
  - routes/scheduling/heat_sheets.py:_serialize_heat_detail (old line 147-185)
  - routes/scheduling/heat_sheets.py:heat_sheets route body (old line 234-256)
  - First-name fuzzy fallback logic (inside _lookup_partner_cid)

Run:  pytest tests/test_partner_resolver.py -v
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.partner_resolver import (
    _first_token_alphanum,
    _norm_alphanum,
    lookup_partner_cid,
    pair_competitors_for_heat,
)


def _comp(
    cid: int, name: str, partners: dict | None = None, display_name: str | None = None
):
    """Build a fake competitor ORM stand-in."""
    partners = partners or {}
    return SimpleNamespace(
        id=cid,
        name=name,
        display_name=display_name or name,
        get_partners=lambda p=partners: p,
    )


def _event(
    eid: int = 1,
    name: str = "Jack & Jill Sawing",
    display_name: str | None = None,
    is_partnered: bool = True,
):
    return SimpleNamespace(
        id=eid,
        name=name,
        display_name=display_name or name,
        is_partnered=is_partnered,
    )


# ---------------------------------------------------------------------------
# lookup_partner_cid — unit tests
# ---------------------------------------------------------------------------


class TestLookupPartnerCid:
    def test_empty_partner_string_returns_none(self):
        assert lookup_partner_cid("", {}, 1) is None

    def test_full_name_match(self):
        comps = {1: _comp(1, "Alice"), 2: _comp(2, "Bob Jones")}
        assert lookup_partner_cid("Bob Jones", comps, 1) == 2

    def test_full_name_normalised(self):
        """Punctuation and case are ignored when comparing full names."""
        comps = {1: _comp(1, "Alice"), 2: _comp(2, "Mary-Ann O'Brien")}
        assert lookup_partner_cid("maryann obrien", comps, 1) == 2

    def test_first_name_fallback_unique(self):
        """Form wrote just 'Toby'; roster has one 'Toby Bartsch' → match."""
        comps = {1: _comp(1, "Alice"), 2: _comp(2, "Toby Bartsch")}
        assert lookup_partner_cid("TOBY", comps, 1) == 2

    def test_first_name_fallback_ambiguous_returns_none(self):
        """Two 'Toby's in the pool → unresolvable, return None."""
        comps = {
            1: _comp(1, "Alice"),
            2: _comp(2, "Toby Bartsch"),
            3: _comp(3, "Toby Chen"),
        }
        assert lookup_partner_cid("TOBY", comps, 1) is None

    def test_excludes_self(self):
        """The primary competitor cannot match themselves as their own partner."""
        comps = {1: _comp(1, "Toby Bartsch"), 2: _comp(2, "Other Name")}
        assert lookup_partner_cid("Toby Bartsch", comps, 1) is None

    def test_no_match_returns_none(self):
        comps = {1: _comp(1, "Alice"), 2: _comp(2, "Bob")}
        assert lookup_partner_cid("Charlie Nowhere", comps, 1) is None


# ---------------------------------------------------------------------------
# pair_competitors_for_heat — REGRESSION TESTS
#
# These MUST match the pre-extraction behavior from
# routes/scheduling/heat_sheets.py — byte-for-byte.
# ---------------------------------------------------------------------------


class TestPairCompetitorsForHeat:
    def test_non_partnered_event_one_row_per_cid(self):
        """Simple timed event: every comp_id becomes its own row."""
        ev = _event(is_partnered=False)
        comps = {
            10: _comp(10, "Alice"),
            20: _comp(20, "Bob"),
            30: _comp(30, "Charlie"),
        }
        rows = pair_competitors_for_heat(ev, [10, 20, 30], comps)
        assert [r["primary_comp_id"] for r in rows] == [10, 20, 30]
        assert [r["name"] for r in rows] == ["Alice", "Bob", "Charlie"]
        assert all(r["partner_comp_id"] is None for r in rows)

    def test_partnered_event_both_partners_present(self):
        """Jack & Jill pair: Alice lists Bob; both in heat → one combined row."""
        ev = _event(name="Jack & Jill Sawing", is_partnered=True)
        alice = _comp(10, "Alice Smith", partners={"Jack & Jill Sawing": "Bob Jones"})
        bob = _comp(20, "Bob Jones", partners={"Jack & Jill Sawing": "Alice Smith"})
        comps = {10: alice, 20: bob}
        rows = pair_competitors_for_heat(ev, [10, 20], comps)
        assert len(rows) == 1
        assert rows[0]["primary_comp_id"] == 10
        assert rows[0]["partner_comp_id"] == 20
        assert rows[0]["name"] == "Alice Smith & Bob Jones"

    def test_partnered_event_partner_missing_from_pool(self):
        """Partner name is raw (no competitor id matched) → keep raw name."""
        ev = _event(is_partnered=True)
        alice = _comp(
            10,
            "Alice Smith",
            partners={"Jack & Jill Sawing": "Unknown Partner"},
        )
        comps = {10: alice}
        rows = pair_competitors_for_heat(ev, [10], comps)
        assert len(rows) == 1
        assert rows[0]["name"] == "Alice Smith & Unknown Partner"
        assert rows[0]["partner_comp_id"] is None

    def test_partnered_event_nickname_resolves_to_full_display_name(self):
        """CRITICAL: form wrote 'TOBY' → renders 'Toby Bartsch' (not 'TOBY')."""
        ev = _event(is_partnered=True)
        alice = _comp(
            10,
            "Alice Smith",
            partners={"Jack & Jill Sawing": "TOBY"},
        )
        toby = _comp(20, "Toby Bartsch")
        comps = {10: alice, 20: toby}
        rows = pair_competitors_for_heat(ev, [10, 20], comps)
        assert len(rows) == 1
        assert rows[0]["name"] == "Alice Smith & Toby Bartsch"
        assert rows[0]["partner_comp_id"] == 20

    def test_consumed_set_prevents_double_row(self):
        """Both pair members in heat → Bob's id is consumed, no standalone row."""
        ev = _event(is_partnered=True)
        alice = _comp(10, "Alice", partners={"Jack & Jill Sawing": "Bob"})
        bob = _comp(20, "Bob", partners={"Jack & Jill Sawing": "Alice"})
        comps = {10: alice, 20: bob}
        rows = pair_competitors_for_heat(ev, [10, 20], comps)
        assert [r["primary_comp_id"] for r in rows] == [10]
        assert rows[0]["name"] == "Alice & Bob"

    def test_missing_competitor_emits_unknown_row(self):
        """Heat has a cid that isn't in the lookup (shouldn't normally happen)."""
        ev = _event(is_partnered=False)
        comps = {10: _comp(10, "Alice")}
        rows = pair_competitors_for_heat(ev, [10, 99], comps)
        assert len(rows) == 2
        assert rows[1]["name"] == "Unknown (99)"
        assert rows[1]["competitor"] is None


# ---------------------------------------------------------------------------
# CRITICAL REGRESSION #1 — byte-for-byte match with old
# _serialize_heat_detail inline logic.
#
# Replicate the exact pre-extraction code block and assert the service
# produces identical (name, stand) pairs.  This test would FAIL if
# anyone accidentally changes the pairing rules in partner_resolver.
# ---------------------------------------------------------------------------


def _legacy_serialize_like_heat_sheets(event, comp_ids, comp_lookup, assignments):
    """Inline reproduction of the pre-extraction logic from
    routes/scheduling/heat_sheets.py::_serialize_heat_detail (old lines 147-185).

    Kept here verbatim so the regression test can compare outputs 1:1.
    Any drift between this function and partner_resolver = regression.
    """

    def _norm(v):
        return "".join(ch for ch in str(v or "").lower() if ch.isalnum())

    def _first(v):
        s = str(v or "").strip().lower().split()
        return "".join(ch for ch in (s[0] if s else "") if ch.isalnum())

    def _legacy_lookup_pid(partner_str, comps, self_cid):
        if not partner_str:
            return None
        norm_full = _norm(partner_str)
        if not norm_full:
            return None
        for cid, c in comps.items():
            if cid == self_cid:
                continue
            if _norm(getattr(c, "name", "")) == norm_full:
                return cid
        partner_first = _first(partner_str)
        if not partner_first:
            return None
        matches = [
            cid
            for cid, c in comps.items()
            if cid != self_cid and _first(getattr(c, "name", "")) == partner_first
        ]
        return matches[0] if len(matches) == 1 else None

    is_partnered = bool(getattr(event, "is_partnered", False))
    consumed = set()
    competitors = []
    for comp_id in comp_ids:
        if comp_id in consumed:
            continue
        comp = comp_lookup.get(comp_id)
        name = comp.display_name if comp else f"Unknown ({comp_id})"
        if is_partnered and comp:
            partners = comp.get_partners() if hasattr(comp, "get_partners") else {}
            if isinstance(partners, dict):
                for key in [
                    str(event.id),
                    event.name,
                    event.display_name,
                    event.name.lower(),
                    event.display_name.lower(),
                ]:
                    partner = partners.get(key)
                    if str(partner or "").strip():
                        partner = str(partner).strip()
                        break
                else:
                    partner = ""
                if partner:
                    partner_id = _legacy_lookup_pid(partner, comp_lookup, comp_id)
                    partner_label = (
                        comp_lookup[partner_id].display_name
                        if partner_id and partner_id in comp_lookup
                        else partner
                    )
                    name = f"{name} & {partner_label}"
                    if partner_id and partner_id != comp_id:
                        consumed.add(partner_id)
        competitors.append(
            {
                "name": name,
                "stand": assignments.get(str(comp_id)),
            }
        )
    return competitors


class TestRegressionAgainstLegacyLogic:
    """CRITICAL: partner_resolver output must match pre-extraction logic exactly."""

    def _compare(self, event, comp_ids, comps, assignments):
        legacy = _legacy_serialize_like_heat_sheets(event, comp_ids, comps, assignments)
        new = [
            {
                "name": row["name"],
                "stand": assignments.get(str(row["primary_comp_id"])),
            }
            for row in pair_competitors_for_heat(event, comp_ids, comps)
        ]
        assert new == legacy, (
            f"\nLegacy:  {legacy}\nNew:     {new}\n"
            "partner_resolver diverged from pre-extraction behavior."
        )

    def test_regression_simple_partnered_heat(self):
        """CRITICAL: Alice + Bob pair matches legacy output."""
        ev = _event(name="Jack & Jill Sawing", is_partnered=True)
        alice = _comp(10, "Alice", partners={"Jack & Jill Sawing": "Bob"})
        bob = _comp(20, "Bob", partners={"Jack & Jill Sawing": "Alice"})
        comps = {10: alice, 20: bob}
        assignments = {"10": 1, "20": 1}
        self._compare(ev, [10, 20], comps, assignments)

    def test_regression_nickname_fuzzy_match(self):
        """CRITICAL: 'TOBY' → 'Toby Bartsch' resolution matches legacy."""
        ev = _event(name="Double Buck", is_partnered=True)
        alice = _comp(10, "Alice", partners={"Double Buck": "TOBY"})
        toby = _comp(20, "Toby Bartsch")
        comps = {10: alice, 20: toby}
        assignments = {"10": 2, "20": 2}
        self._compare(ev, [10, 20], comps, assignments)

    def test_regression_partner_not_in_pool(self):
        """CRITICAL: unresolved partner name renders raw — matches legacy."""
        ev = _event(name="Jack & Jill Sawing", is_partnered=True)
        alice = _comp(10, "Alice", partners={"Jack & Jill Sawing": "Random Person"})
        comps = {10: alice}
        assignments = {"10": 3}
        self._compare(ev, [10], comps, assignments)

    def test_regression_non_partnered_event_pass_through(self):
        """Non-partnered: every comp is its own row, unchanged."""
        ev = _event(is_partnered=False)
        comps = {
            10: _comp(10, "Alice"),
            20: _comp(20, "Bob"),
            30: _comp(30, "Charlie"),
        }
        assignments = {"10": 1, "20": 2, "30": 3}
        self._compare(ev, [10, 20, 30], comps, assignments)

    def test_regression_event_lookup_by_id_key(self):
        """Partner stored under str(event.id) key (preferred lookup order)."""
        ev = _event(eid=42, name="Pulp Toss", is_partnered=True)
        alice = _comp(10, "Alice", partners={"42": "Bob"})  # id key, not name
        bob = _comp(20, "Bob", partners={"42": "Alice"})
        comps = {10: alice, 20: bob}
        assignments = {"10": 1, "20": 1}
        self._compare(ev, [10, 20], comps, assignments)


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


class TestNormalisationHelpers:
    def test_norm_alphanum_drops_punctuation(self):
        assert _norm_alphanum("O'Brien, Mary-Anne") == "obrienmaryanne"

    def test_norm_alphanum_none(self):
        assert _norm_alphanum(None) == ""

    def test_first_token_alphanum_basic(self):
        assert _first_token_alphanum("Toby Bartsch") == "toby"

    def test_first_token_alphanum_empty(self):
        assert _first_token_alphanum("") == ""

    def test_first_token_alphanum_none(self):
        assert _first_token_alphanum(None) == ""
