"""
Regression tests for the 2026-04-23 partner-pairing fix bundle.

Covers:
  A. Preflight extends partner-pool / unresolved-partner / non-reciprocal
     checks to BOTH pro and college partnered events.
  B. _find_partner uses Levenshtein ≤ 2 fuzzy fallback (matches
     services.excel_io._fuzzy_match_member's existing bar so import-time
     and heat-gen-time matching agree).
  C. pair_competitors_for_heat falls back to the tournament-wide roster
     so partner names always render with their school tag instead of the
     bare partners-JSON string.
  D. _build_partner_units holds back unpaired partnered-event entrants
     (does not place them solo) and records them in unpaired_log.
  E. Preflight reciprocity validator flags A-says-B-but-B-says-C.

Run:
    pytest tests/test_partner_pairing_fixes.py -v
"""

import os

import pytest

from database import db as _db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app():
    from tests.db_test_utils import create_test_app

    _app, db_path = create_test_app()
    with _app.app_context():
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def db_session(app):
    with app.app_context():
        _db.session.begin_nested()
        yield _db.session
        _db.session.rollback()


def _make_tournament(session, name="PartnerFix 2026"):
    from models import Tournament

    t = Tournament(name=name, year=2026, status="college_active")
    session.add(t)
    session.flush()
    return t


def _make_team(session, tournament, code="UM-A", school="UM"):
    from models import Team

    t = Team(
        tournament_id=tournament.id,
        team_code=code,
        school_name=school,
        school_abbreviation=school,
    )
    session.add(t)
    session.flush()
    return t


def _make_college_competitor(session, tournament, team, name, gender="M"):
    from models import CollegeCompetitor

    c = CollegeCompetitor(
        tournament_id=tournament.id,
        team_id=team.id,
        name=name,
        gender=gender,
        status="active",
    )
    session.add(c)
    session.flush()
    return c


def _make_partnered_event(session, tournament, name="Jack & Jill Sawing"):
    from models import Event

    e = Event(
        tournament_id=tournament.id,
        name=name,
        event_type="college",
        scoring_type="time",
        scoring_order="lowest_wins",
        stand_type="saw_hand",
        max_stands=4,
        is_partnered=True,
        partner_gender_requirement="mixed",
    )
    session.add(e)
    session.flush()
    return e


def _enroll_pair(comp_a, comp_b, event):
    """Mutually enrol comp_a and comp_b in event with each other as partner."""
    import json

    comp_a.events_entered = json.dumps([event.name])
    comp_a.partners = json.dumps({event.name: comp_b.name})
    comp_b.events_entered = json.dumps([event.name])
    comp_b.partners = json.dumps({event.name: comp_a.name})


def _enroll_solo(comp, event, partner_name=""):
    """Enrol comp in event listing partner_name (which may be unresolvable)."""
    import json

    comp.events_entered = json.dumps([event.name])
    comp.partners = json.dumps({event.name: partner_name})


# ---------------------------------------------------------------------------
# B — Levenshtein fuzzy fallback in _find_partner
# ---------------------------------------------------------------------------


class TestFindPartnerFuzzy:
    def test_exact_match_still_works(self):
        from services.heat_generator import _find_partner

        pool = [
            {"id": 1, "name": "Jordan Navas", "base_name": "Jordan Navas"},
            {"id": 2, "name": "McKinley Smith", "base_name": "McKinley Smith"},
        ]
        match = _find_partner("McKinley Smith", pool, pool[0])
        assert match is not None and match["id"] == 2

    def test_first_name_fallback_works(self):
        from services.heat_generator import _find_partner

        pool = [
            {"id": 1, "name": "Jordan Navas", "base_name": "Jordan Navas"},
            {"id": 2, "name": "McKinley Smith", "base_name": "McKinley Smith"},
        ]
        match = _find_partner("McKinley", pool, pool[0])
        assert match is not None and match["id"] == 2

    def test_levenshtein_catches_mckinlay_typo(self):
        """The original race-day bug: form said 'McKinlay' but DB has 'McKinley
        Smith'. Pre-fix: _find_partner failed → solo placement."""
        from services.heat_generator import _find_partner

        pool = [
            {"id": 1, "name": "Jordan Navas", "base_name": "Jordan Navas"},
            {"id": 2, "name": "McKinley Smith", "base_name": "McKinley Smith"},
        ]
        # "McKinlay Smith" → "mckinlaysmith" vs "mckinleysmith" = distance 1
        match = _find_partner("McKinlay Smith", pool, pool[0])
        assert match is not None and match["id"] == 2

    def test_levenshtein_refuses_ambiguous_match(self):
        """Don't pick the wrong person when several pool members are similar."""
        from services.heat_generator import _find_partner

        pool = [
            {"id": 1, "name": "Jordan Navas", "base_name": "Jordan Navas"},
            {"id": 2, "name": "Mark Smith", "base_name": "Mark Smith"},
            {"id": 3, "name": "Mary Smith", "base_name": "Mary Smith"},
        ]
        # "Marx Smith" is 1 edit from BOTH "Mark Smith" and "Mary Smith".
        # Both within fuzzy distance → ambiguous → no match (safer than wrong).
        # Note: first-name "Marx" is NOT a single match either (no comp first
        # name == "marx"), so tier 2 also fails. Tier 3 has 2 matches → None.
        match = _find_partner("Marx Smith", pool, pool[0])
        assert match is None, "fuzzy must refuse on ambiguity, got %r" % match

    def test_levenshtein_refuses_too_distant(self):
        """Distance > 2 is too loose for the same-name claim."""
        from services.heat_generator import _find_partner

        pool = [
            {"id": 1, "name": "Jordan Navas", "base_name": "Jordan Navas"},
            {"id": 2, "name": "McKinley Smith", "base_name": "McKinley Smith"},
        ]
        match = _find_partner("Robertson", pool, pool[0])
        assert match is None

    def test_asymmetric_first_name_typo_resolves(self):
        """Race-weekend bug: form wrote 'McKinley', roster has 'Mickinley
        Verhulst'. Full-name distance is 9 (last name adds 8 chars), but
        first-token distance is 1 — tier 4 catches it."""
        from services.heat_generator import _find_partner

        pool = [
            {"id": 1, "name": "Jordan Navas", "base_name": "Jordan Navas"},
            {"id": 2, "name": "Mickinley Verhulst", "base_name": "Mickinley Verhulst"},
            {"id": 3, "name": "Mackenzie Breitner", "base_name": "Mackenzie Breitner"},
        ]
        match = _find_partner("McKinley", pool, pool[0])
        assert match is not None and match["id"] == 2, (
            f"expected Mickinley Verhulst (id=2), got {match}"
        )

    def test_first_token_fuzzy_refuses_ambiguous(self):
        """Two roster members within first-token distance 2 of target → None."""
        from services.heat_generator import _find_partner

        pool = [
            {"id": 1, "name": "Jordan Navas", "base_name": "Jordan Navas"},
            {"id": 2, "name": "Mickinley Verhulst", "base_name": "Mickinley Verhulst"},
            {"id": 3, "name": "Mickinly Johnson", "base_name": "Mickinly Johnson"},
        ]
        match = _find_partner("McKinley", pool, pool[0])
        assert match is None, "ambiguous first-token fuzzy must refuse"


# ---------------------------------------------------------------------------
# D — _build_partner_units skips unpaired by default + populates log
# ---------------------------------------------------------------------------


class TestBuildPartnerUnitsSkipUnpaired:
    def _event_stub(self):
        class _E:
            id = 99
            name = "Jack & Jill Sawing"
            display_name = "Jack & Jill Sawing"
            is_partnered = True
            event_type = "college"

        return _E()

    def test_paired_competitors_form_unit(self):
        from services.heat_generator import _build_partner_units

        event = self._event_stub()
        comps = [
            {
                "id": 1,
                "name": "Jordan",
                "base_name": "Jordan Navas",
                "partner_name": "McKinley Smith",
            },
            {
                "id": 2,
                "name": "McKinley",
                "base_name": "McKinley Smith",
                "partner_name": "Jordan Navas",
            },
        ]
        unpaired_log: list = []
        units = _build_partner_units(comps, event, unpaired_log=unpaired_log)
        assert len(units) == 1
        assert {c["id"] for c in units[0]} == {1, 2}
        assert unpaired_log == []

    def test_unpaired_held_back_default(self):
        """Unresolved partner → held back, NOT placed solo."""
        from services.heat_generator import _build_partner_units

        event = self._event_stub()
        comps = [
            {
                "id": 1,
                "name": "Jordan",
                "base_name": "Jordan Navas",
                "partner_name": "Ghost Person",
            },  # no match in pool
        ]
        unpaired_log: list = []
        units = _build_partner_units(comps, event, unpaired_log=unpaired_log)
        assert units == [], "unpaired competitor must be held back, not placed solo"
        assert len(unpaired_log) == 1
        assert unpaired_log[0]["comp_id"] == 1
        assert unpaired_log[0]["reason"] == "unresolved"
        assert unpaired_log[0]["partner_name"] == "Ghost Person"

    def test_blank_partner_held_back_with_blank_reason(self):
        from services.heat_generator import _build_partner_units

        event = self._event_stub()
        comps = [
            {
                "id": 1,
                "name": "Jordan",
                "base_name": "Jordan Navas",
                "partner_name": "",
            },
        ]
        unpaired_log: list = []
        units = _build_partner_units(comps, event, unpaired_log=unpaired_log)
        assert units == []
        assert unpaired_log[0]["reason"] == "blank"

    def test_self_reference_held_back_with_self_reason(self):
        from services.heat_generator import _build_partner_units

        event = self._event_stub()
        comps = [
            {
                "id": 1,
                "name": "Jordan",
                "base_name": "Jordan Navas",
                "partner_name": "Jordan Navas",
            },
        ]
        unpaired_log: list = []
        units = _build_partner_units(comps, event, unpaired_log=unpaired_log)
        assert units == []
        assert unpaired_log[0]["reason"] == "self_reference"

    def test_skip_unpaired_false_preserves_legacy(self):
        """Opt-in legacy mode places solo (still records the violation)."""
        from services.heat_generator import _build_partner_units

        event = self._event_stub()
        comps = [
            {
                "id": 1,
                "name": "Jordan",
                "base_name": "Jordan Navas",
                "partner_name": "Ghost Person",
            },
        ]
        unpaired_log: list = []
        units = _build_partner_units(
            comps,
            event,
            skip_unpaired=False,
            unpaired_log=unpaired_log,
        )
        assert len(units) == 1 and units[0][0]["id"] == 1
        # Violation still logged so caller can warn even in legacy mode.
        assert len(unpaired_log) == 1


# ---------------------------------------------------------------------------
# C — pair_competitors_for_heat roster fallback
# ---------------------------------------------------------------------------


class TestPairCompetitorsRosterFallback:
    def test_partner_in_heat_uses_heat_lookup_display_name(self, db_session):
        from services.partner_resolver import pair_competitors_for_heat

        t = _make_tournament(db_session)
        team = _make_team(db_session, t)
        a = _make_college_competitor(db_session, t, team, "Jordan Navas", "M")
        b = _make_college_competitor(db_session, t, team, "McKinley Smith", "F")
        ev = _make_partnered_event(db_session, t)
        _enroll_pair(a, b, ev)

        rows = pair_competitors_for_heat(
            ev,
            [a.id, b.id],
            {a.id: a, b.id: b},
            roster_lookup=None,
        )
        assert len(rows) == 1
        # Both names with school tags via display_name.
        assert "Jordan Navas (UM-A)" in rows[0]["name"]
        assert "McKinley Smith (UM-A)" in rows[0]["name"]

    def test_partner_in_other_heat_falls_back_to_roster(self, db_session):
        """When the partner landed in a different heat, render still gets the
        school tag via the tournament-wide roster lookup."""
        from services.partner_resolver import pair_competitors_for_heat

        t = _make_tournament(db_session)
        team = _make_team(db_session, t)
        team_b = _make_team(db_session, t, code="UM-B")
        a = _make_college_competitor(db_session, t, team, "Jordan Navas", "M")
        b = _make_college_competitor(db_session, t, team_b, "McKinley Smith", "F")
        ev = _make_partnered_event(db_session, t)
        _enroll_pair(a, b, ev)

        # a is in this heat alone; b is "in another heat" not in comp_lookup.
        roster = {a.id: a, b.id: b}
        rows = pair_competitors_for_heat(
            ev,
            [a.id],
            {a.id: a},
            roster_lookup=roster,
        )
        assert len(rows) == 1
        assert "Jordan Navas (UM-A)" in rows[0]["name"]
        assert (
            "McKinley Smith (UM-B)" in rows[0]["name"]
        ), f"Roster fallback failed; got {rows[0]['name']!r}"

    def test_partner_not_in_roster_falls_back_to_raw_string(self, db_session):
        """Raw-string fallback preserved when even roster doesn't know."""
        from services.partner_resolver import pair_competitors_for_heat

        t = _make_tournament(db_session)
        team = _make_team(db_session, t)
        a = _make_college_competitor(db_session, t, team, "Jordan Navas", "M")
        ev = _make_partnered_event(db_session, t)
        _enroll_solo(a, ev, partner_name="Ghost Person")

        rows = pair_competitors_for_heat(
            ev,
            [a.id],
            {a.id: a},
            roster_lookup={a.id: a},
        )
        assert len(rows) == 1
        assert "Ghost Person" in rows[0]["name"]


# ---------------------------------------------------------------------------
# A + E — Preflight extends to college, flags unresolved + non-reciprocal
# ---------------------------------------------------------------------------


class TestPreflightCollegePartnerChecks:
    def test_college_odd_pool_flagged(self, db_session):
        """Pre-fix: only pro events were checked for odd pool. Now both."""
        from services.preflight import build_preflight_report

        t = _make_tournament(db_session)
        team = _make_team(db_session, t)
        a = _make_college_competitor(db_session, t, team, "A One", "M")
        b = _make_college_competitor(db_session, t, team, "B Two", "F")
        c = _make_college_competitor(db_session, t, team, "C Three", "M")
        ev = _make_partnered_event(db_session, t)
        _enroll_pair(a, b, ev)
        # c is enrolled solo with no partner → odd pool of 3.
        _enroll_solo(c, ev, partner_name="")
        db_session.flush()

        report = build_preflight_report(t, saturday_college_event_ids=[])
        codes = [i["code"] for i in report["issues"]]
        assert (
            "odd_partner_pool" in codes
        ), f"college odd-pool not flagged; got codes {codes}"

    def test_unresolved_partner_flagged_and_held_back_competitors_listed(
        self, db_session
    ):
        from services.preflight import build_preflight_report

        t = _make_tournament(db_session)
        team = _make_team(db_session, t)
        a = _make_college_competitor(db_session, t, team, "Jordan Navas", "M")
        b = _make_college_competitor(db_session, t, team, "McKinley Smith", "F")
        ev = _make_partnered_event(db_session, t)
        # a points at a name no one has and that doesn't fuzzy-match McKinley.
        _enroll_solo(a, ev, partner_name="Ghost Person")
        _enroll_pair(b, b, ev)  # b lists themselves so they're enrolled solo
        # Reset b's partners to point at a so we get a clean unresolved on a.
        import json

        b.partners = json.dumps({ev.name: "Jordan Navas"})
        db_session.flush()

        report = build_preflight_report(t, saturday_college_event_ids=[])
        unresolved = [
            i for i in report["issues"] if i["code"] == "unresolved_partner_name"
        ]
        assert unresolved, "Unresolved-partner check did not fire. Codes: " + str(
            [i["code"] for i in report["issues"]]
        )
        affected = [u["competitor_name"] for u in unresolved[0]["unresolved"]]
        assert any(
            "Jordan" in name for name in affected
        ), f"Jordan should be in unresolved list; got {affected}"

    def test_non_reciprocal_partnership_flagged(self, db_session):
        """A says B is partner, but B says someone else."""
        import json

        from services.preflight import build_preflight_report

        t = _make_tournament(db_session)
        team = _make_team(db_session, t)
        a = _make_college_competitor(db_session, t, team, "Alice One", "F")
        b = _make_college_competitor(db_session, t, team, "Bob Two", "M")
        c = _make_college_competitor(db_session, t, team, "Carol Three", "F")
        d = _make_college_competitor(db_session, t, team, "Dave Four", "M")
        ev = _make_partnered_event(db_session, t)
        # All four enrolled. Alice says Bob, Bob says Dave (not Alice). C+D pair.
        a.events_entered = json.dumps([ev.name])
        a.partners = json.dumps({ev.name: "Bob Two"})
        b.events_entered = json.dumps([ev.name])
        b.partners = json.dumps({ev.name: "Dave Four"})
        c.events_entered = json.dumps([ev.name])
        c.partners = json.dumps({ev.name: "Dave Four"})
        d.events_entered = json.dumps([ev.name])
        d.partners = json.dumps({ev.name: "Carol Three"})
        db_session.flush()

        report = build_preflight_report(t, saturday_college_event_ids=[])
        non_recip = [
            i for i in report["issues"] if i["code"] == "non_reciprocal_partnership"
        ]
        assert non_recip, "non_reciprocal_partnership did not fire. Codes: " + str(
            [i["code"] for i in report["issues"]]
        )
        # Either Alice→Bob or Bob→Dave should be flagged (the broken side).
        names = [n["competitor_name"] for n in non_recip[0]["non_reciprocal"]]
        assert any(
            "Alice" in n or "Bob" in n for n in names
        ), f"expected Alice or Bob in non-reciprocal list; got {names}"

    def test_reciprocal_pair_passes_clean(self, db_session):
        """Two cleanly-paired competitors should produce zero new issues."""
        from services.preflight import build_preflight_report

        t = _make_tournament(db_session)
        team = _make_team(db_session, t)
        a = _make_college_competitor(db_session, t, team, "Jordan Navas", "M")
        b = _make_college_competitor(db_session, t, team, "McKinley Smith", "F")
        ev = _make_partnered_event(db_session, t)
        _enroll_pair(a, b, ev)
        db_session.flush()

        report = build_preflight_report(t, saturday_college_event_ids=[])
        codes = [i["code"] for i in report["issues"]]
        assert "unresolved_partner_name" not in codes
        assert "non_reciprocal_partnership" not in codes
        assert "self_reference_partner" not in codes
        assert "odd_partner_pool" not in codes
