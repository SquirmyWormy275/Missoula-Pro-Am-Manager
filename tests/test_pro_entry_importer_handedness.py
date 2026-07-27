"""
Handedness + canonical-dedup tests for services/pro_entry_importer.py.

Covers the L/R springboard bug fix (2026-04-20):
- is_left_handed_springboard is correctly captured from the 'Springboard (L)'
  column (and NOT lost in canonical dedup).
- Canonical dedup prevents double-entry and double-fee-charging when the form
  has multiple aliased columns mapping to the same canonical event.
- Sentinel None is used when neither L nor R column exists in the xlsx so
  re-imports do not wipe manually corrected flags.
- compute_review_flags warns when both L and R boxes are checked on the
  same row.

Run:  pytest tests/test_pro_entry_importer_handedness.py -v
"""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from services.pro_entry_importer import compute_review_flags, parse_pro_entries

# Waiver column header (full text is long; starts-with matching is done in parser)
_WAIVER_HEADER = (
    "I know that logging events bear inherent risks. "
    "I consent to participate at my own risk."
)


def _write_form_xlsx(tmp_path: Path, columns: list[str], rows: list[list]) -> str:
    """Write a minimal Google-Forms-style xlsx and return its path as string."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(columns)
    for row in rows:
        ws.append(row)
    fp = tmp_path / "form.xlsx"
    wb.save(fp)
    return str(fp)


def _base_columns(event_columns: list[str]) -> list[str]:
    """
    Return the minimum set of Google Forms columns + the event columns under test.

    Columns match the strings parse_pro_entries looks up via hmap.
    """
    return [
        "Timestamp",
        "Email Address",
        "Full Name",
        "Gender",
        "Mailing Address",
        "Phone Number",
        "Are you a current ALA member?",
        *event_columns,
        "I would like to enter into the Pro-Am lottery",
        "Are you sharing gear?",
        _WAIVER_HEADER,
        "Signature",
    ]


def _row_template(
    name: str = "Alex Kaper",
    email: str = "alex@example.com",
    gender: str = "Male",
    event_answers: list = None,
) -> list:
    """Return a row with base columns filled in and event answers interleaved."""
    return [
        "2026-04-20T10:00:00",  # Timestamp
        email,  # Email Address
        name,  # Full Name
        gender,  # Gender
        "123 Log St",  # Mailing Address
        "5551234567",  # Phone Number
        "Yes",  # ALA member?
        *(event_answers or []),
        "No",  # Pro-Am lottery
        "No",  # gear sharing
        "Yes",  # waiver
        "Alex Kaper",  # signature
    ]


# ---------------------------------------------------------------------------
# Handedness capture
# ---------------------------------------------------------------------------


class TestHandednessCapture:
    def test_springboard_l_only_sets_true(self, tmp_path):
        cols = _base_columns(["Springboard (L)", "Springboard (R)"])
        path = _write_form_xlsx(
            tmp_path,
            cols,
            [
                _row_template(
                    event_answers=["Yes", "No"],
                )
            ],
        )
        entries = parse_pro_entries(path)
        assert len(entries) == 1
        assert entries[0]["is_left_handed_springboard"] is True
        assert entries[0]["events"] == ["Springboard"]

    def test_springboard_r_only_keeps_false(self, tmp_path):
        cols = _base_columns(["Springboard (L)", "Springboard (R)"])
        path = _write_form_xlsx(
            tmp_path,
            cols,
            [
                _row_template(
                    event_answers=["No", "Yes"],
                )
            ],
        )
        entries = parse_pro_entries(path)
        assert len(entries) == 1
        assert entries[0]["is_left_handed_springboard"] is False
        assert entries[0]["events"] == ["Springboard"]

    def test_both_checked_sets_true_and_dedupes_event(self, tmp_path):
        """Both L and R checked → prefer L (True); event + fee dedup to 1."""
        cols = _base_columns(["Springboard (L)", "Springboard (R)"])
        path = _write_form_xlsx(
            tmp_path,
            cols,
            [
                _row_template(
                    event_answers=["Yes", "Yes"],
                )
            ],
        )
        entries = parse_pro_entries(path)
        assert len(entries) == 1
        assert entries[0]["is_left_handed_springboard"] is True
        # Canonical dedup: should NOT be ['Springboard', 'Springboard'].
        assert entries[0]["events"] == ["Springboard"]
        # Fee dedup: $10 chopping, not $20.
        assert entries[0]["chopping_fees"] == 10
        # Raw flags preserved for review-flag detection.
        assert entries[0]["_raw_springboard_l"] is True
        assert entries[0]["_raw_springboard_r"] is True

    def test_neither_checked_defaults_false(self, tmp_path):
        """Form has L/R columns, row checks neither → False (explicit signal)."""
        cols = _base_columns(["Springboard (L)", "Springboard (R)"])
        path = _write_form_xlsx(
            tmp_path,
            cols,
            [
                _row_template(
                    event_answers=["No", "No"],
                )
            ],
        )
        entries = parse_pro_entries(path)
        assert len(entries) == 1
        # Form has the columns, so we have a signal: False (right-handed).
        assert entries[0]["is_left_handed_springboard"] is False
        assert entries[0]["events"] == []

    def test_columns_absent_sets_none_sentinel(self, tmp_path):
        """Form lacks both L and R columns → None sentinel (preserve manual)."""
        # Minimal form with ONLY non-springboard events.
        cols = _base_columns(["Hot Saw"])
        path = _write_form_xlsx(
            tmp_path,
            cols,
            [
                _row_template(
                    event_answers=["Yes"],
                )
            ],
        )
        entries = parse_pro_entries(path)
        assert len(entries) == 1
        # No L/R columns at all → None so confirmer preserves any DB value.
        assert entries[0]["is_left_handed_springboard"] is None


# ---------------------------------------------------------------------------
# Canonical dedup across other event aliases
# ---------------------------------------------------------------------------


class TestCanonicalDedup:
    def test_pro_1board_three_headers_dedup_to_one_entry_one_fee(self, tmp_path):
        """
        _EVENT_MAP has three keys mapping to canonical 'Pro 1-Board':
        'Intermediate 1-Board Springboard', '1-Board Springboard', 'Pro 1-Board'.
        Row with 'Yes' on ALL THREE should produce a single entry + single $10 fee.
        """
        cols = _base_columns(
            [
                "Intermediate 1-Board Springboard",
                "1-Board Springboard",
                "Pro 1-Board",
            ]
        )
        path = _write_form_xlsx(
            tmp_path,
            cols,
            [
                _row_template(
                    event_answers=["Yes", "Yes", "Yes"],
                )
            ],
        )
        entries = parse_pro_entries(path)
        assert len(entries) == 1
        assert entries[0]["events"] == ["Pro 1-Board"]
        assert entries[0]["chopping_fees"] == 10
        assert entries[0]["total_fees"] == 10

    def test_jack_jill_three_headers_dedup_to_one_entry_one_fee(self, tmp_path):
        """
        _EVENT_MAP has 'Jack & Jill', 'Jack & Jill Sawing', 'Jack Jill' → one canonical.
        Row with 'Yes' on all three should produce a single entry + single $5 fee.
        """
        cols = _base_columns(
            [
                "Jack & Jill",
                "Jack & Jill Sawing",
                "Jack Jill",
            ]
        )
        path = _write_form_xlsx(
            tmp_path,
            cols,
            [
                _row_template(
                    event_answers=["Yes", "Yes", "Yes"],
                )
            ],
        )
        entries = parse_pro_entries(path)
        assert len(entries) == 1
        assert entries[0]["events"] == ["Jack & Jill Sawing"]
        assert entries[0]["other_fees"] == 5
        assert entries[0]["total_fees"] == 5


# ---------------------------------------------------------------------------
# compute_review_flags: both-checked conflict
# ---------------------------------------------------------------------------


class TestReviewFlagsConflict:
    def _entry(self, l: bool, r: bool) -> dict:
        """Minimal entry dict with the raw L/R flags required by the flag logic."""
        return {
            "name": "Alex",
            "waiver_accepted": True,
            "partners": {},
            "gear_sharing": False,
            "gear_sharing_details": None,
            "_raw_springboard_l": l,
            "_raw_springboard_r": r,
        }

    def test_both_l_and_r_flags_conflict(self):
        entries = [self._entry(True, True)]
        compute_review_flags(entries)
        assert "CONFLICT: BOTH L AND R SPRINGBOARD CHECKED" in entries[0]["flags"]
        # Should be a warning-level class (not error red, since waiver OK).
        assert entries[0]["flag_class"] == "table-warning"

    def test_only_l_no_conflict(self):
        entries = [self._entry(True, False)]
        compute_review_flags(entries)
        assert "CONFLICT: BOTH L AND R SPRINGBOARD CHECKED" not in entries[0]["flags"]

    def test_only_r_no_conflict(self):
        entries = [self._entry(False, True)]
        compute_review_flags(entries)
        assert "CONFLICT: BOTH L AND R SPRINGBOARD CHECKED" not in entries[0]["flags"]

    def test_neither_no_conflict(self):
        entries = [self._entry(False, False)]
        compute_review_flags(entries)
        assert "CONFLICT: BOTH L AND R SPRINGBOARD CHECKED" not in entries[0]["flags"]
