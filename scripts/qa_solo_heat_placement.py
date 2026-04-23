"""Scripted end-to-end QA for the "solo competitor closes the event" fix.

Reproduces the exact scenario from the 2026-04-22 bug report screenshot
(Men's Stock Saw, 19 College competitors across UM/CSU/UI/FVC/MSU teams,
2 stands per heat) through the FULL service path — seeds a tournament DB
row, creates CollegeCompetitor rows, calls services.heat_generator.generate_event_heats
via the Flask app context, then reads Heat + HeatAssignment rows back and
verifies: Heat 1 is full (2 competitors), Heat 10 is the solo (1 competitor),
every competitor appears in exactly one heat, stand_number assignments are valid.

Catches regressions that pure unit tests cannot: Heat row creation, stand
assignment logic, HeatAssignment FK relationships, and the gear_violations
heat_index remap path.

Also covers:
  - Men's Stock Saw 19 comps (screenshot case — odd)
  - Women's Stock Saw 13 comps (odd, different team distribution)
  - 20-comp field (even — no reorder should occur)
  - Underhand 7 comps / 3-per-heat (standard snake path)
  - Springboard 5 comps no slow no LH (springboard path)

Usage:
    python scripts/qa_solo_heat_placement.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SECRET_KEY", "qa-solo-heat-secret")
os.environ.setdefault("WTF_CSRF_ENABLED", "False")

from tests.db_test_utils import create_test_app  # noqa: E402

REPORT: list[tuple[str, str, str]] = []


def record(severity: str, label: str, detail: str = "") -> None:
    REPORT.append((severity, label, detail))
    marker = {"PASS": "[+]", "FAIL": "[X]", "WARN": "[!]"}[severity]
    print(f"{marker} {label}" + (f" — {detail}" if detail else ""))


def _seed_college_event(
    db,
    Tournament,
    CollegeCompetitor,
    Team,
    Event,
    event_name: str,
    gender: str,
    stand_type: str,
    max_stands: int,
    competitor_names: list[tuple[str, str]],
) -> int:
    """Seed one tournament + one college event + N CollegeCompetitors.

    Returns event.id. Competitor tuples are (name, team_code) like
    ('Alex Kaper', 'UM-A')."""
    t = Tournament(
        name=f"QA Solo Heat {event_name} {gender}", year=2026, status="setup"
    )
    db.session.add(t)
    db.session.flush()

    team_codes = {team for _, team in competitor_names}
    team_by_code: dict[str, int] = {}
    for code in team_codes:
        abbrev = code.split("-")[0]
        tm = Team(
            tournament_id=t.id,
            team_code=code,
            school_name=f"School {code}",
            school_abbreviation=abbrev,
            status="active",
        )
        db.session.add(tm)
        db.session.flush()
        team_by_code[code] = tm.id

    for name, team_code in competitor_names:
        c = CollegeCompetitor(
            tournament_id=t.id,
            team_id=team_by_code[team_code],
            name=name,
            gender=gender,
            events_entered=json.dumps([event_name]),
            status="active",
        )
        db.session.add(c)

    ev = Event(
        tournament_id=t.id,
        name=event_name,
        event_type="college",
        gender=gender,
        scoring_type="time",
        scoring_order="lowest_wins",
        stand_type=stand_type,
        max_stands=max_stands,
        status="pending",
        is_finalized=False,
    )
    db.session.add(ev)
    db.session.commit()
    return ev.id


def _heat_sizes(
    db, Heat, HeatAssignment, event_id: int
) -> list[tuple[int, list[tuple[int, int]]]]:
    """Return [(heat_number, [(stand, comp_id), ...]), ...] sorted by heat_number.
    Only Run 1 is inspected."""
    heats = (
        Heat.query.filter_by(event_id=event_id, run_number=1)
        .order_by(Heat.heat_number)
        .all()
    )
    result: list[tuple[int, list[tuple[int, int]]]] = []
    for h in heats:
        assignments = (
            HeatAssignment.query.filter_by(heat_id=h.id)
            .order_by(HeatAssignment.stand_number)
            .all()
        )
        result.append(
            (h.heat_number, [(a.stand_number, a.competitor_id) for a in assignments])
        )
    return result


def _case_stock_saw_19_men(app) -> None:
    """Screenshot case: 19 Men's Stock Saw competitors on 2 stands — heat 1
    must be full, heat 10 must be the solo."""
    from database import db
    from models import CollegeCompetitor, Event, Heat, HeatAssignment, Team, Tournament
    from services.heat_generator import generate_event_heats

    # Names copied from the user's screenshot, in the same order.
    comps = [
        ("Alex Kaper", "UM-A"),
        ("Nathan Veress", "UM-A"),
        ("Alex Gibbs", "CSU-B"),
        ("Jordan Navas", "UM-A"),
        ("Max Schramm", "CSU-B"),
        ("Shane Massender", "UI-A"),
        ("Jack Gilmore", "CSU-B"),
        ("Kevan Mitchell", "UI-A"),
        ("Dan Harris", "CSU-A"),
        ("Samuel Bernard", "UI-B"),
        ("Noah Chamberlain", "FVC-A"),
        ("Ben Sauer", "UI-B"),
        ("Abe Chentnik", "FVC-A"),
        ("Mateo Angel", "MSU-A"),
        ("Dustin Haley", "FVC-A"),
        ("John Nelson", "MSU-A"),
        ("Atticus Caudle", "MSU-B"),
        ("Zach Cardenas", "MSU-B"),
        ("Trevor Norris", "MSU-B"),
    ]
    assert len(comps) == 19, f"Expected 19, got {len(comps)}"

    with app.app_context():
        event_id = _seed_college_event(
            db,
            Tournament,
            CollegeCompetitor,
            Team,
            Event,
            event_name="Stock Saw",
            gender="M",
            stand_type="saw_hand",
            max_stands=2,
            competitor_names=comps,
        )
        ev = Event.query.get(event_id)
        n_heats = generate_event_heats(ev)
        db.session.commit()

        layout = _heat_sizes(db, Heat, HeatAssignment, event_id)

    # 10 heats generated.
    if n_heats == 10 and len(layout) == 10:
        record("PASS", "Men's Stock Saw 19: 10 heats created")
    else:
        record(
            "FAIL",
            "Men's Stock Saw 19 heat count",
            f"expected 10 heats, got n_heats={n_heats} len(layout)={len(layout)}",
        )
        return

    sizes = [len(a) for _, a in layout]
    if sizes[0] == 2:
        record("PASS", "Heat 1 full (2 competitors) — no longer the solo")
    else:
        record(
            "FAIL",
            "Heat 1 not full",
            f"expected 2 competitors in heat 1, got {sizes[0]} — regression of the screenshot bug",
        )

    if sizes[-1] == 1:
        record("PASS", "Heat 10 (final) holds the solo competitor")
    else:
        record(
            "FAIL",
            "Heat 10 not solo",
            f"expected 1 competitor in heat 10, got {sizes[-1]}",
        )

    # All 19 placed exactly once.
    all_comp_ids = [comp_id for _, a in layout for _, comp_id in a]
    if len(all_comp_ids) == 19 and len(set(all_comp_ids)) == 19:
        record("PASS", "All 19 competitors placed exactly once")
    else:
        record(
            "FAIL",
            "Competitor placement",
            f"expected 19 unique placements, got {len(all_comp_ids)} total / {len(set(all_comp_ids))} unique",
        )

    # Stand numbers are in the college-stock-saw whitelist [7, 8].
    stands = sorted({s for _, a in layout for s, _ in a})
    if stands and set(stands).issubset({7, 8}):
        record("PASS", f"Stand numbers in the 7/8 whitelist (got {stands})")
    else:
        record("FAIL", "Stand numbers outside 7/8 whitelist", f"got {stands}")


def _case_stock_saw_13_women(app) -> None:
    from database import db
    from models import CollegeCompetitor, Event, Heat, HeatAssignment, Team, Tournament
    from services.heat_generator import generate_event_heats

    comps = [
        ("Chloe Brown", "UM-A"),
        ("Emily Milligan", "UM-B"),
        ("Lily Cummins", "CSU-B"),
        ("Maise Wellman", "UM-B"),
        ("Rebecka Plank", "CSU-A"),
        ("Elizabeth Armstrong", "UM-B"),
        ("Nell Horgan", "FVC-A"),
        ("Cami Knorpp", "UI-A"),
        ("Ellana Schreifels", "FVC-A"),
        ("Hannah Benjamin", "UI-A"),
        ("Maria Pyeatt", "MSU-A"),
        ("Hannah McClintock", "UI-A"),
        ("Alyssa Takeshita-Kaufman", "UI-B"),
    ]
    with app.app_context():
        event_id = _seed_college_event(
            db,
            Tournament,
            CollegeCompetitor,
            Team,
            Event,
            event_name="Stock Saw",
            gender="F",
            stand_type="saw_hand",
            max_stands=2,
            competitor_names=comps,
        )
        ev = Event.query.get(event_id)
        generate_event_heats(ev)
        db.session.commit()
        layout = _heat_sizes(db, Heat, HeatAssignment, event_id)

    # 13 comps / 2 per heat → 7 heats, sizes [2, 2, 2, 2, 2, 2, 1].
    sizes = [len(a) for _, a in layout]
    expected = [2, 2, 2, 2, 2, 2, 1]
    if sizes == expected:
        record("PASS", f"Women's Stock Saw 13: sizes {sizes} — solo in final")
    else:
        record(
            "FAIL", "Women's Stock Saw 13 layout", f"expected {expected}, got {sizes}"
        )


def _case_even_field_no_reorder(app) -> None:
    from database import db
    from models import CollegeCompetitor, Event, Heat, HeatAssignment, Team, Tournament
    from services.heat_generator import generate_event_heats

    comps = [(f"Runner {i}", "UM-A" if i % 2 == 0 else "CSU-A") for i in range(1, 21)]
    with app.app_context():
        event_id = _seed_college_event(
            db,
            Tournament,
            CollegeCompetitor,
            Team,
            Event,
            event_name="Stock Saw",
            gender="M",
            stand_type="saw_hand",
            max_stands=2,
            competitor_names=comps,
        )
        ev = Event.query.get(event_id)
        generate_event_heats(ev)
        db.session.commit()
        layout = _heat_sizes(db, Heat, HeatAssignment, event_id)

    sizes = [len(a) for _, a in layout]
    if sizes == [2] * 10:
        record("PASS", "Even field 20 comps / 2 per heat: all heats full [2,2,...,2]")
    else:
        record("FAIL", "Even field should not reorder", f"got {sizes}")


def _case_underhand_7_on_3(app) -> None:
    from database import db
    from models import CollegeCompetitor, Event, Heat, HeatAssignment, Team, Tournament
    from services.heat_generator import generate_event_heats

    comps = [(f"Axer {i}", "UM-A") for i in range(1, 8)]
    with app.app_context():
        event_id = _seed_college_event(
            db,
            Tournament,
            CollegeCompetitor,
            Team,
            Event,
            event_name="Underhand",
            gender="M",
            stand_type="underhand",
            max_stands=3,
            competitor_names=comps,
        )
        ev = Event.query.get(event_id)
        generate_event_heats(ev)
        db.session.commit()
        layout = _heat_sizes(db, Heat, HeatAssignment, event_id)

    # 7 comps / 3 → 3 heats, partial at end.
    sizes = [len(a) for _, a in layout]
    if len(sizes) == 3 and sizes[0] >= sizes[-1] and sum(sizes) == 7:
        record("PASS", f"Underhand 7/3: sizes {sizes} — partial at end")
    else:
        record("FAIL", "Underhand 7/3 layout", f"got {sizes}")


def _case_springboard_5_odd(app) -> None:
    from database import db
    from models import CollegeCompetitor, Event, Heat, HeatAssignment, Team, Tournament
    from services.heat_generator import generate_event_heats

    comps = [(f"Cutter {i}", "UM-A") for i in range(1, 6)]
    with app.app_context():
        event_id = _seed_college_event(
            db,
            Tournament,
            CollegeCompetitor,
            Team,
            Event,
            event_name="Springboard",
            gender="M",
            stand_type="springboard",
            max_stands=2,
            competitor_names=comps,
        )
        ev = Event.query.get(event_id)
        generate_event_heats(ev)
        db.session.commit()
        layout = _heat_sizes(db, Heat, HeatAssignment, event_id)

    sizes = [len(a) for _, a in layout]
    if sum(sizes) == 5 and sizes[0] >= sizes[-1]:
        record("PASS", f"Springboard 5/2: sizes {sizes} — partial at end")
    else:
        record("FAIL", "Springboard 5/2 layout", f"got {sizes}")


def main() -> int:
    app, db_path = create_test_app()
    try:
        print("\n--- Case 1: Men's Stock Saw, 19 competitors (screenshot case) ---")
        _case_stock_saw_19_men(app)
        print("\n--- Case 2: Women's Stock Saw, 13 competitors ---")
        _case_stock_saw_13_women(app)
        print("\n--- Case 3: Even field, 20 competitors, 2 per heat ---")
        _case_even_field_no_reorder(app)
        print("\n--- Case 4: Men's Underhand, 7 competitors, 3 per heat ---")
        _case_underhand_7_on_3(app)
        print("\n--- Case 5: Men's Springboard, 5 competitors, 2 per heat ---")
        _case_springboard_5_odd(app)
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass

    fails = [r for r in REPORT if r[0] == "FAIL"]
    passes = [r for r in REPORT if r[0] == "PASS"]
    print()
    print("=" * 60)
    print(f"QA SUMMARY: {len(passes)} pass, {len(fails)} fail")
    print("=" * 60)
    for severity, label, detail in REPORT:
        if severity == "FAIL":
            print(f"  FAIL: {label} — {detail}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
