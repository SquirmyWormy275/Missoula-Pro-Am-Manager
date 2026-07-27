"""
Tests for scripts/repair_springboard_handedness.py.

Covers the one-time repair workflow that retroactively applies the L/R
springboard importer fix to competitors that were imported under the broken
parser.  The repair script re-parses a given xlsx with the fixed parser and:
  1. Updates ProCompetitor.is_left_handed_springboard from the form signal.
  2. Regenerates pro springboard heats where safe (skipping finalized events
     and events with heats already in_progress / completed).

These tests use the same xlsx-fixture pattern as test_pro_entry_importer_handedness.

Run:  pytest tests/test_repair_springboard_handedness.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import openpyxl
import pytest

from scripts.repair_springboard_handedness import repair
from tests.conftest import (
    make_event,
    make_pro_competitor,
    make_tournament,
)

_WAIVER_HEADER = (
    "I know that logging events bear inherent risks. "
    "I consent to participate at my own risk."
)


def _write_form_xlsx(tmp_path: Path, rows: list[dict]) -> str:
    """
    Write a minimal Google-Forms-style xlsx with Springboard L/R columns.
    Each row dict provides: name, email, l ('Yes'/'No'), r ('Yes'/'No').
    """
    cols = [
        "Timestamp",
        "Email Address",
        "Full Name",
        "Gender",
        "Mailing Address",
        "Phone Number",
        "Are you a current ALA member?",
        "Springboard (L)",
        "Springboard (R)",
        "I would like to enter into the Pro-Am lottery",
        "Are you sharing gear?",
        _WAIVER_HEADER,
        "Signature",
    ]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(cols)
    for row in rows:
        ws.append(
            [
                "2026-04-20T10:00:00",
                row["email"],
                row["name"],
                row.get("gender", "Male"),
                "123 Log St",
                "5551234567",
                "Yes",
                row.get("l", "No"),
                row.get("r", "No"),
                "No",
                "No",
                "Yes",
                row["name"],
            ]
        )
    fp = tmp_path / "repair.xlsx"
    wb.save(fp)
    return str(fp)


class TestRepairFlagUpdate:
    def test_repair_sets_true_when_xlsx_has_l_checked(self, db_session, tmp_path):
        tournament = make_tournament(db_session)
        # Pre-existing competitor imported under the broken parser: LH = False.
        comp = make_pro_competitor(
            db_session,
            tournament,
            name="Alex Kaper",
            is_left_handed_springboard=False,
        )
        comp.email = "alex@example.com"
        db_session.flush()

        xlsx = _write_form_xlsx(
            tmp_path,
            [
                {
                    "name": "Alex Kaper",
                    "email": "alex@example.com",
                    "l": "Yes",
                    "r": "No",
                },
            ],
        )

        summary = repair(tournament.id, xlsx, dry_run=False)

        # The repair reports one flag flip from False -> True.
        assert len(summary["flag_updates"]) == 1
        assert summary["flag_updates"][0]["email"] == "alex@example.com"
        assert summary["flag_updates"][0]["old"] is False
        assert summary["flag_updates"][0]["new"] is True

        # DB reflects the update.
        db_session.refresh(comp)
        assert comp.is_left_handed_springboard is True

    def test_repair_no_change_when_already_correct(self, db_session, tmp_path):
        tournament = make_tournament(db_session)
        comp = make_pro_competitor(
            db_session,
            tournament,
            name="Alex Kaper",
            is_left_handed_springboard=True,
        )
        comp.email = "alex@example.com"
        db_session.flush()

        xlsx = _write_form_xlsx(
            tmp_path,
            [
                {
                    "name": "Alex Kaper",
                    "email": "alex@example.com",
                    "l": "Yes",
                    "r": "No",
                },
            ],
        )

        summary = repair(tournament.id, xlsx, dry_run=False)

        # Already True, so no flag update reported.
        assert summary["flag_updates"] == []

    def test_repair_dry_run_reports_changes_but_commits_nothing(
        self, db_session, tmp_path
    ):
        tournament = make_tournament(db_session)
        comp = make_pro_competitor(
            db_session,
            tournament,
            name="Alex Kaper",
            is_left_handed_springboard=False,
        )
        comp.email = "alex@example.com"
        db_session.flush()

        xlsx = _write_form_xlsx(
            tmp_path,
            [
                {
                    "name": "Alex Kaper",
                    "email": "alex@example.com",
                    "l": "Yes",
                    "r": "No",
                },
            ],
        )

        summary = repair(tournament.id, xlsx, dry_run=True)

        # Reports the change...
        assert len(summary["flag_updates"]) == 1
        # ...but does NOT mutate the DB.
        db_session.refresh(comp)
        assert comp.is_left_handed_springboard is False

    def test_repair_skips_unmatched_emails(self, db_session, tmp_path):
        tournament = make_tournament(db_session)

        xlsx = _write_form_xlsx(
            tmp_path,
            [
                {"name": "Ghost", "email": "ghost@example.com", "l": "Yes", "r": "No"},
            ],
        )

        summary = repair(tournament.id, xlsx, dry_run=False)

        assert "ghost@example.com" in summary["unmatched_emails"]
        assert summary["flag_updates"] == []

    def test_repair_skips_finalized_springboard_event(self, db_session, tmp_path):
        tournament = make_tournament(db_session)
        comp = make_pro_competitor(
            db_session,
            tournament,
            name="Alex Kaper",
            is_left_handed_springboard=False,
        )
        comp.email = "alex@example.com"

        # Finalized pro springboard event — must NOT be regenerated.
        event = make_event(
            db_session,
            tournament,
            name="Springboard",
            event_type="pro",
            stand_type="springboard",
        )
        event.is_finalized = True
        db_session.flush()

        xlsx = _write_form_xlsx(
            tmp_path,
            [
                {
                    "name": "Alex Kaper",
                    "email": "alex@example.com",
                    "l": "Yes",
                    "r": "No",
                },
            ],
        )

        summary = repair(tournament.id, xlsx, dry_run=False)

        # Flag still updated, but heat regeneration skipped with a clear reason.
        assert len(summary["flag_updates"]) == 1
        skipped = {s["event"]: s["reason"] for s in summary["skipped_events"]}
        assert "Springboard" in skipped
        assert "finalized" in skipped["Springboard"].lower()
