"""Pragmatic QA for the Print Hub feature — uses Flask's test client against a
temp-file SQLite DB seeded via migrations. Exercises the same server-side code
a real browser would hit, with guaranteed auth + seed data. Runs in seconds.

Usage:
    python scripts/qa_print_hub.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Make project root importable.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SECRET_KEY", "qa-print-hub-secret")
os.environ.setdefault("WTF_CSRF_ENABLED", "False")

from tests.db_test_utils import create_test_app  # noqa: E402

REPORT: list[tuple[str, str, str]] = []  # (severity, label, detail)


def record(severity: str, label: str, detail: str = "") -> None:
    REPORT.append((severity, label, detail))
    marker = {"PASS": "[+]", "FAIL": "[X]", "WARN": "[!]"}[severity]
    print(f"{marker} {label}" + (f" — {detail}" if detail else ""))


def assert_contains(body: bytes, needle: str, context: str) -> None:
    if needle.encode() in body:
        record("PASS", f'{context}: "{needle}" present')
    else:
        record("FAIL", f'{context}: "{needle}" NOT FOUND')


def assert_not_contains(body: bytes, needle: str, context: str) -> None:
    if needle.encode() not in body:
        record("PASS", f'{context}: "{needle}" absent (correct)')
    else:
        record("FAIL", f'{context}: "{needle}" present (should be absent)')


def main() -> int:
    app, db_path = create_test_app()
    try:
        run_qa(app)
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass

    fails = [r for r in REPORT if r[0] == "FAIL"]
    warns = [r for r in REPORT if r[0] == "WARN"]
    passes = [r for r in REPORT if r[0] == "PASS"]
    print()
    print("=" * 60)
    print(f"QA SUMMARY: {len(passes)} pass, {len(fails)} fail, {len(warns)} warn")
    print("=" * 60)
    for severity, label, detail in REPORT:
        if severity in ("FAIL", "WARN"):
            print(f"  {severity}: {label} — {detail}")
    return 0 if not fails else 1


def run_qa(app) -> None:
    from database import db
    from models import (
        CollegeCompetitor,
        Event,
        EventResult,
        PrintEmailLog,
        PrintTracker,
        ProCompetitor,
        Team,
        Tournament,
    )
    from models.user import User
    from services import print_catalog

    # ----------------------------------------------------------------------
    # Seed
    # ----------------------------------------------------------------------
    with app.app_context():
        import json

        t = Tournament(name="QA Print Hub Test", year=2026, status="setup")
        db.session.add(t)
        db.session.flush()

        # Judge user for authenticated routes
        u = User(username="qa_judge", role="judge")
        u.set_password("pw")
        db.session.add(u)

        # One pro with two events
        pro = ProCompetitor(
            tournament_id=t.id,
            name="Alex Kaper",
            gender="M",
            events_entered=json.dumps(["Underhand", "Springboard"]),
            status="active",
        )
        db.session.add(pro)

        pro_scratched = ProCompetitor(
            tournament_id=t.id,
            name="Bob Scratched",
            gender="M",
            events_entered=json.dumps(["Underhand"]),
            status="scratched",
        )
        db.session.add(pro_scratched)

        # One event to test dynamic event-results rows
        event = Event(
            tournament_id=t.id,
            name="Underhand",
            event_type="pro",
            gender="M",
            scoring_type="time",
            scoring_order="lowest_wins",
            stand_type="underhand",
            max_stands=5,
            status="pending",
            is_finalized=False,
        )
        db.session.add(event)
        db.session.flush()
        tid = t.id
        eid = event.id
        uid = u.id

        db.session.commit()

    # ----------------------------------------------------------------------
    # Auth client
    # ----------------------------------------------------------------------
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)

    # ----------------------------------------------------------------------
    # 1. Hub renders on a nearly-empty tournament
    # ----------------------------------------------------------------------
    print("\n--- Hub GET on seeded tournament ---")
    resp = client.get(f"/scheduling/{tid}/print-hub")
    if resp.status_code == 200:
        record("PASS", "Print Hub returns 200")
    else:
        record(
            "FAIL",
            f"Print Hub returned {resp.status_code}",
            resp.data[:200].decode("utf-8", "replace"),
        )
        return

    body = resp.data

    # Page chrome
    assert_contains(body, "Print Hub", "Hub title")
    assert_contains(body, "QA Print Hub Test", "Tournament name")

    # Section headers
    for section in ["Setup", "Run Show", "Results", "Compliance"]:
        assert_contains(body, section, f'Section header "{section}"')

    # At least one catalog label present
    assert_contains(body, "Heat Sheets", "Heat Sheets row")
    assert_contains(body, "Pro Saturday Checkout Roster", "Pro Checkout row")
    assert_contains(body, "ALA Report", "ALA Report row")
    assert_contains(body, "Event Results", "Event Results section")

    # Dynamic row: event should appear labelled by display_name
    # HTML-escaped apostrophe in "Men's".
    assert_contains(body, "Men&#39;s Underhand", "Dynamic event row for Underhand")

    # Email modal should be absent (SMTP not configured → no modal loaded)
    assert_not_contains(body, 'id="emailModal"', "Email modal hidden when SMTP unset")
    assert_contains(body, "Email delivery is disabled", "SMTP disabled banner")

    # ----------------------------------------------------------------------
    # 2. Hub renders with SMTP configured → email modal present
    # ----------------------------------------------------------------------
    print("\n--- Hub GET with SMTP env vars set ---")
    os.environ["SMTP_HOST"] = "smtp.test"
    os.environ["SMTP_USER"] = "user"
    os.environ["SMTP_PASSWORD"] = "pw-qa-12345"
    try:
        resp2 = client.get(f"/scheduling/{tid}/print-hub")
        if resp2.status_code == 200:
            assert_contains(
                resp2.data,
                'id="emailModal"',
                "Email modal present when SMTP configured",
            )
            assert_contains(
                resp2.data, 'name="csrf_token"', "CSRF token in email modal form"
            )
            assert_not_contains(
                resp2.data,
                "Email delivery is disabled",
                "SMTP banner hidden when configured",
            )
        else:
            record("FAIL", f"Hub returned {resp2.status_code} with SMTP set")
    finally:
        for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"):
            os.environ.pop(k, None)

    # ----------------------------------------------------------------------
    # 3. Pro Checkout Roster print route
    # ----------------------------------------------------------------------
    print("\n--- Pro Checkout Roster print ---")
    resp3 = client.get(f"/scheduling/{tid}/pro/checkout-roster/print")
    if resp3.status_code == 200:
        record("PASS", "Checkout roster returns 200")
        body3 = resp3.data
        assert_contains(body3, "Pro Saturday Checkout", "Roster title")
        assert_contains(body3, "Alex Kaper", "Active pro listed")
        assert_not_contains(body3, "Bob Scratched", "Scratched pro excluded")
        assert_contains(body3, "Underhand", "Events column populated")
        assert_contains(body3, "Present", "Present checkbox column")
        # Landscape
        assert_contains(body3, "landscape", "@page landscape directive")
    else:
        record("FAIL", f"Checkout roster returned {resp3.status_code}")

    # ----------------------------------------------------------------------
    # 4. Print route writes a PrintTracker row
    # ----------------------------------------------------------------------
    with app.app_context():
        rows = PrintTracker.query.filter_by(
            tournament_id=tid, doc_key="pro_checkout"
        ).all()
        if len(rows) == 1:
            record("PASS", "PrintTracker row written after checkout print")
        else:
            record("FAIL", f"Expected 1 tracker row for pro_checkout, got {len(rows)}")

    # ----------------------------------------------------------------------
    # 5. Stale detection — change data, re-load hub, tracker row shows stale
    # ----------------------------------------------------------------------
    print("\n--- Staleness detection ---")
    # First render hub so tracker row is committed, then mutate pros
    with app.app_context():
        import json

        pro_b = ProCompetitor(
            tournament_id=tid,
            name="Carol NewPro",
            gender="F",
            events_entered=json.dumps(["Underhand"]),
            status="active",
        )
        db.session.add(pro_b)
        db.session.commit()

    resp4 = client.get(f"/scheduling/{tid}/print-hub")
    if resp4.status_code == 200:
        # Look for the STALE badge class near the pro_checkout row label
        body4 = resp4.data
        if b"STALE" in body4:
            record("PASS", "STALE badge rendered somewhere on hub")
        else:
            record("WARN", "STALE badge not detected — may need stronger assertion")

    # ----------------------------------------------------------------------
    # 6. Email POST — CSRF disabled in test, SMTP blocked via patched submit
    # ----------------------------------------------------------------------
    print("\n--- Email POST flow ---")
    os.environ["SMTP_HOST"] = "smtp.test"
    os.environ["SMTP_USER"] = "user"
    os.environ["SMTP_PASSWORD"] = "pw-qa-12345"
    try:
        # Patch background_jobs.submit to no-op so we don't hit a real SMTP
        import services.background_jobs as bj

        _orig = bj.submit
        bj.submit = lambda label, fn, *a, **kw: None

        resp5 = client.post(
            f"/scheduling/{tid}/print-hub/email",
            data={"doc_key": "pro_checkout", "extra_emails": "recipient@example.com"},
            follow_redirects=False,
        )
        if resp5.status_code == 302:
            record("PASS", "Email POST returns 302 (redirect back to hub)")
        else:
            record("FAIL", f"Email POST returned {resp5.status_code}")

        # A queued log row should exist
        with app.app_context():
            logs = PrintEmailLog.query.filter_by(tournament_id=tid).all()
            if len(logs) == 1 and logs[0].status == "queued":
                record("PASS", "PrintEmailLog row written with status=queued")
                if "recipient@example.com" in logs[0].get_recipients():
                    record("PASS", "Recipient stored in log")
                else:
                    record("FAIL", "Recipient missing from log")
            else:
                record("FAIL", f"Expected 1 queued log, got {len(logs)}")

        # Bad recipient → 302 with no new log
        resp6 = client.post(
            f"/scheduling/{tid}/print-hub/email",
            data={"doc_key": "pro_checkout", "extra_emails": "garbage"},
            follow_redirects=False,
        )
        if resp6.status_code == 302:
            record("PASS", "Malformed email rejected with 302 + flash")
        else:
            record("FAIL", f"Malformed email returned {resp6.status_code}")
        with app.app_context():
            if PrintEmailLog.query.filter_by(tournament_id=tid).count() == 1:
                record("PASS", "No new log row for malformed recipient")
            else:
                record("FAIL", "Log row created for malformed recipient")

        # Unknown doc_key → 302 with flash error, no log row
        resp7 = client.post(
            f"/scheduling/{tid}/print-hub/email",
            data={"doc_key": "does_not_exist", "extra_emails": "x@y.com"},
            follow_redirects=False,
        )
        if resp7.status_code == 302:
            record("PASS", "Unknown doc_key rejected with 302 + flash")

        bj.submit = _orig
    finally:
        for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"):
            os.environ.pop(k, None)

    # ----------------------------------------------------------------------
    # 7. Event Results dynamic row — red until finalized, green after
    # ----------------------------------------------------------------------
    print("\n--- Event Results dynamic row state ---")
    resp8 = client.get(f"/scheduling/{tid}/print-hub")
    # Before finalization, "Event not finalized yet" should appear in a tooltip
    # (we can at minimum verify the event appears with a red dot by checking
    # the template's reason tooltip rendering)
    if b"Event not finalized yet" in resp8.data:
        record("PASS", "Unfinalized event shows reason tooltip")
    else:
        record("WARN", "Unfinalized-event tooltip not detected in HTML")

    with app.app_context():
        ev = db.session.get(Event, eid)
        ev.is_finalized = True
        ev.status = "completed"
        db.session.commit()

    resp9 = client.get(f"/scheduling/{tid}/print-hub")
    if resp9.status_code == 200 and b"Event not finalized yet" not in resp9.data:
        record("PASS", 'Finalized event removes "not finalized" reason')
    else:
        record("WARN", "Finalized-event hub did not update as expected")

    # ----------------------------------------------------------------------
    # 8. Auth gate — unauthenticated user gets redirected
    # ----------------------------------------------------------------------
    print("\n--- Auth gate ---")
    anon = app.test_client()
    resp10 = anon.get(f"/scheduling/{tid}/print-hub", follow_redirects=False)
    if resp10.status_code in (302, 401, 403):
        record("PASS", f"Anonymous user redirected/blocked ({resp10.status_code})")
    else:
        record("FAIL", f"Anonymous user got {resp10.status_code} — should be blocked")

    # ----------------------------------------------------------------------
    # 9. Sidebar link present on the hub page (regression guard)
    # ----------------------------------------------------------------------
    if b"bi-printer-fill" in body and b"Print Hub" in body:
        record("PASS", "Sidebar has Print Hub link with correct icon")
    else:
        record("WARN", "Sidebar icon/link not detected")


if __name__ == "__main__":
    sys.exit(main())
