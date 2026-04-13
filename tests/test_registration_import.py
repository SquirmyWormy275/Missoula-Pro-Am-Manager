"""
Tests for the enhanced registration import pipeline.

Tests cover:
1. Dirty partner field garbage detection and auto-resolution
2. Name fuzzy matching (exact, prefix, Levenshtein, first-name-only)
3. Gear sharing text parsing (dirty patterns)
4. Gender-event cross-validation
5. Partner reciprocity validation
6. Duplicate detection
7. Import report generation
8. Full pipeline against the real dirty xlsx (if available)
"""

import os

import pytest

from services.registration_import import (
    ImportResult,
    _build_name_index,
    _check_gender_event,
    _classify_partner_value,
    _extract_names_from_text,
    _fuzzy_resolve,
    _guess_equipment,
    _is_equipment_text,
    _parse_dirty_gear_text,
    run_import_pipeline,
    to_entry_dicts,
)

# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

_SAMPLE_NAMES = [
    "Henry Norwood",
    "Joey Long",
    "Brianna Kvinge",
    "Connor Robertson",
    "Chase lee Gundersen",
    "Cody labahn",
    "Mike Johnson",
    "Gillian Shannon",
    "Seth Bergman",
    "Karson Wilson",
    "Erin LaVoie",
    "Shea Warren",
    "David I Moses",
    "Iliana Castro",
    "Jack Love",
]

_NAME_INDEX = _build_name_index(_SAMPLE_NAMES)


# ---------------------------------------------------------------------------
# 1. Partner field garbage detection
# ---------------------------------------------------------------------------


class TestPartnerClassification:
    """Test _classify_partner_value for dirty-file garbage patterns."""

    def test_question_mark(self):
        action, label = _classify_partner_value("?")
        assert action == "needs_partner"
        assert "?" in label

    def test_idk(self):
        action, _ = _classify_partner_value("Idk")
        assert action == "needs_partner"

    def test_idk_uppercase(self):
        action, _ = _classify_partner_value("IDK")
        assert action == "needs_partner"

    def test_lookin(self):
        action, _ = _classify_partner_value("Lookin")
        assert action == "needs_partner"

    def test_looking(self):
        action, _ = _classify_partner_value("Looking")
        assert action == "needs_partner"

    def test_whoever(self):
        action, _ = _classify_partner_value("Whoever")
        assert action == "needs_partner"

    def test_anyone_available(self):
        action, _ = _classify_partner_value("anyone available")
        assert action == "needs_partner"

    def test_no_oarnter(self):
        action, _ = _classify_partner_value("no oarnter")
        assert action == "needs_partner"

    def test_no_partner(self):
        action, _ = _classify_partner_value("no partner")
        assert action == "needs_partner"

    def test_none(self):
        action, _ = _classify_partner_value("none")
        assert action == "needs_partner"

    def test_need_partner(self):
        action, _ = _classify_partner_value("Need partner")
        assert action == "needs_partner"

    def test_needs_partner(self):
        action, _ = _classify_partner_value("Needs partner")
        assert action == "needs_partner"

    def test_tbd(self):
        action, _ = _classify_partner_value("TBD")
        assert action == "needs_partner"

    def test_na(self):
        action, _ = _classify_partner_value("N/A")
        assert action == "needs_partner"

    def test_have_saw_need_partner(self):
        action, label = _classify_partner_value("Have saw, need partner")
        assert action == "needs_partner"
        assert "equipment" in label

    def test_spare(self):
        action, _ = _classify_partner_value("Put me down as a spare if thats okay")
        assert action == "needs_partner"

    def test_real_name(self):
        action, _ = _classify_partner_value("Henry Norwood")
        assert action == "name"

    def test_empty(self):
        action, _ = _classify_partner_value("")
        assert action == "empty"

    def test_whitespace_only(self):
        action, _ = _classify_partner_value("   ")
        assert action == "empty"


# ---------------------------------------------------------------------------
# 2. Name fuzzy matching
# ---------------------------------------------------------------------------


class TestFuzzyMatching:
    """Test _fuzzy_resolve for various matching patterns."""

    def test_exact_match(self):
        result = ImportResult()
        resolved = _fuzzy_resolve("Henry Norwood", _NAME_INDEX, result)
        assert resolved == "Henry Norwood"

    def test_case_normalized(self):
        result = ImportResult()
        resolved = _fuzzy_resolve("henry norwood", _NAME_INDEX, result)
        assert resolved == "Henry Norwood"

    def test_extra_whitespace(self):
        result = ImportResult()
        resolved = _fuzzy_resolve("  Henry  Norwood  ", _NAME_INDEX, result)
        assert resolved == "Henry Norwood"

    def test_fuzzy_typo(self):
        """Brianna -> Bri (prefix of normalized name)."""
        result = ImportResult()
        resolved = _fuzzy_resolve("Bri Kvinge", _NAME_INDEX, result)
        assert resolved == "Brianna Kvinge"
        assert any("FUZZY" in f for f in result.fuzzy_matches)

    def test_first_name_only(self):
        """Single first name resolves when unambiguous."""
        result = ImportResult()
        resolved = _fuzzy_resolve("Henry", _NAME_INDEX, result)
        assert resolved == "Henry Norwood"
        assert any("FIRST-NAME" in f for f in result.fuzzy_matches)

    def test_first_name_joey(self):
        result = ImportResult()
        resolved = _fuzzy_resolve("Joey", _NAME_INDEX, result)
        assert resolved == "Joey Long"

    def test_first_name_cody(self):
        result = ImportResult()
        resolved = _fuzzy_resolve("Cody", _NAME_INDEX, result)
        assert resolved == "Cody labahn"

    def test_first_name_mike(self):
        result = ImportResult()
        resolved = _fuzzy_resolve("Mike", _NAME_INDEX, result)
        assert resolved == "Mike Johnson"

    def test_short_name_no_match(self):
        """Names under 4 chars should not attempt first-name matching."""
        result = ImportResult()
        resolved = _fuzzy_resolve("she", _NAME_INDEX, result)
        # Should NOT match 'Shea Warren'
        assert resolved == "she"

    def test_unresolvable(self):
        result = ImportResult()
        resolved = _fuzzy_resolve("Nick Barrett", _NAME_INDEX, result)
        # Not in the index, should return as-is
        assert resolved == "Nick Barrett"

    def test_empty_input(self):
        result = ImportResult()
        resolved = _fuzzy_resolve("", _NAME_INDEX, result)
        assert resolved == ""

    def test_levenshtein_close(self):
        """Illiana -> Iliana (Levenshtein distance 1)."""
        result = ImportResult()
        resolved = _fuzzy_resolve("Illiana Castro", _NAME_INDEX, result)
        assert resolved == "Iliana Castro"


# ---------------------------------------------------------------------------
# 3. Equipment/gear text parsing
# ---------------------------------------------------------------------------


class TestEquipmentDetection:
    """Test _is_equipment_text and _guess_equipment."""

    def test_springboard(self):
        assert _is_equipment_text("Springboard")
        assert _guess_equipment("Springboard") == "springboard"

    def test_spring_board_space(self):
        assert _is_equipment_text("Spring board")
        assert _guess_equipment("Spring board") == "springboard"

    def test_crosscut(self):
        assert _is_equipment_text("crosscut")
        assert _guess_equipment("crosscut") == "crosscut saw"

    def test_op(self):
        assert _is_equipment_text("OP")
        assert _guess_equipment("OP") == "obstacle pole chainsaw"

    def test_hotsaw(self):
        assert _is_equipment_text("hotsaw")
        assert _guess_equipment("hotsaw") == "hot saw"

    def test_hot_saw_space(self):
        assert _is_equipment_text("hot saw")
        assert _guess_equipment("hot saw") == "hot saw"

    def test_cookie_stack(self):
        assert _is_equipment_text("cookie stack")
        assert _guess_equipment("Cookie stack saw") == "cookie stack saw"

    def test_single_buck(self):
        assert _is_equipment_text("single buck")
        assert _guess_equipment("single buck") == "single buck saw"

    def test_double_buck(self):
        assert _is_equipment_text("double buck")
        assert _guess_equipment("double buck") == "double buck saw"

    def test_pole_climb(self):
        assert _guess_equipment("pole climb") == "spurs and rope"

    def test_caulks(self):
        assert _is_equipment_text("caulks")
        assert _guess_equipment("caulks") == "caulk boots"

    def test_name_is_not_equipment(self):
        assert not _is_equipment_text("Henry Norwood")

    def test_unknown_text(self):
        assert _guess_equipment("something random") == "unknown"


class TestDirtyGearParsing:
    """Test _parse_dirty_gear_text with real dirty-file patterns."""

    def test_first_name_dash_equipment(self):
        """'Henry- crosscut' pattern."""
        result = ImportResult()
        records = _parse_dirty_gear_text(
            "Henry- crosscut", "Gillian Shannon", _SAMPLE_NAMES, _NAME_INDEX, result
        )
        assert len(records) >= 1
        # Should have resolved Henry to Henry Norwood
        all_partners = [p for r in records for p in r.get("partners", [])]
        assert "Henry Norwood" in all_partners or any(
            "Henry" in f for f in result.fuzzy_matches
        )

    def test_name_dash_equipment_reversed(self):
        """'Spring board, Seth Bergman' pattern."""
        result = ImportResult()
        records = _parse_dirty_gear_text(
            "Spring board, Seth Bergman",
            "Seth Buckman",
            _SAMPLE_NAMES,
            _NAME_INDEX,
            result,
        )
        assert len(records) >= 1

    def test_conversational(self):
        """'Me and Connor sharing a hotsaw' pattern."""
        result = ImportResult()
        records = _parse_dirty_gear_text(
            "Me and Connor sharing a hotsaw",
            "Chase lee Gundersen",
            _SAMPLE_NAMES,
            _NAME_INDEX,
            result,
        )
        assert len(records) >= 1
        found_hotsaw = any("hot saw" in r.get("equipment", "") for r in records)
        assert found_hotsaw

    def test_colon_pattern(self):
        """'cookie stack: cody labahn owen vrendenburg john orm' pattern."""
        result = ImportResult()
        records = _parse_dirty_gear_text(
            "cookie stack: cody labahn owen vrendenburg john orm",
            "Chrissy Marcellus",
            _SAMPLE_NAMES,
            _NAME_INDEX,
            result,
        )
        assert len(records) >= 1
        assert any("cookie" in r.get("equipment", "") for r in records)

    def test_parenthetical_grouping(self):
        """'(Karson Wilson & Chris Graham) - M. Double Buck' pattern."""
        result = ImportResult()
        records = _parse_dirty_gear_text(
            "(Karson Wilson & Chris Graham) - M. Double Buck, "
            "(Karson W. & Emma Macon) - Jack & Jill",
            "Jack Love",
            _SAMPLE_NAMES,
            _NAME_INDEX,
            result,
        )
        assert len(records) >= 2

    def test_conditional_language(self):
        """Conditional language should be flagged."""
        result = ImportResult()
        records = _parse_dirty_gear_text(
            "Possibly Chrissy's single saw if ours is not finished",
            "Seth Bergman",
            _SAMPLE_NAMES,
            _NAME_INDEX,
            result,
        )
        assert all(r.get("conditional") for r in records)

    def test_multiple_sharing_partners(self):
        """'Mason Banks - Single Buck, Nick Barrett - Single Buck'."""
        result = ImportResult()
        records = _parse_dirty_gear_text(
            "Mason Banks - Single Buck, Nick Barrett - Single Buck, Ben Lee - Single Buck",
            "Owen Vredenburg",
            _SAMPLE_NAMES,
            _NAME_INDEX,
            result,
        )
        # Should parse multiple records
        assert len(records) >= 2

    def test_equipment_and_caulks(self):
        """'Ripley Orr - OP (chainsaw and caulks)' pattern."""
        result = ImportResult()
        records = _parse_dirty_gear_text(
            "Ripley Orr - OP (chainsaw and caulks)",
            "May Brown",
            _SAMPLE_NAMES + ["Ripley Orr"],
            _build_name_index(_SAMPLE_NAMES + ["Ripley Orr"]),
            result,
        )
        assert len(records) >= 1

    def test_empty_text(self):
        result = ImportResult()
        records = _parse_dirty_gear_text("", "Test", _SAMPLE_NAMES, _NAME_INDEX, result)
        assert records == []


# ---------------------------------------------------------------------------
# 4. Gender-event cross-validation
# ---------------------------------------------------------------------------


class TestGenderEventValidation:
    """Test _check_gender_event rules."""

    def test_male_in_mens_event(self):
        assert _check_gender_event("M", "Men's Underhand") is None

    def test_female_in_womens_event(self):
        assert _check_gender_event("F", "Women's Underhand") is None

    def test_female_in_mens_event(self):
        result = _check_gender_event("F", "Men's Underhand")
        assert result is not None
        assert "GENDER MISMATCH" in result

    def test_male_in_womens_event(self):
        result = _check_gender_event("M", "Women's Standing Block")
        assert result is not None
        assert "GENDER MISMATCH" in result

    def test_neutral_event_male(self):
        assert _check_gender_event("M", "Hot Saw") is None

    def test_neutral_event_female(self):
        assert _check_gender_event("F", "Cookie Stack") is None

    def test_jack_jill_male(self):
        assert _check_gender_event("M", "Jack & Jill") is None

    def test_jack_jill_female(self):
        assert _check_gender_event("F", "Jack & Jill") is None


# ---------------------------------------------------------------------------
# 5. Full pipeline against real dirty xlsx
# ---------------------------------------------------------------------------

_DIRTY_FILE = os.path.join(
    os.path.expanduser("~"),
    "Desktop",
    "entries",
    "Pro Entries",
    "Missoula Pro-Am 2026 (dirty).xlsx",
)


@pytest.mark.skipif(
    not os.path.exists(_DIRTY_FILE), reason="Dirty xlsx file not found at expected path"
)
class TestDirtyFilePipeline:
    """Integration tests against the real dirty xlsx file."""

    def test_pipeline_no_errors(self):
        result = run_import_pipeline(_DIRTY_FILE)
        assert len(result.errors) == 0

    def test_competitor_count(self):
        """48 rows - 1 duplicate (Dwight Severson) = 47 competitors."""
        result = run_import_pipeline(_DIRTY_FILE)
        assert len(result.competitors) == 47

    def test_duplicate_detection(self):
        result = run_import_pipeline(_DIRTY_FILE)
        assert len(result.duplicates_removed) >= 1
        assert any("Dwight Severson" in d for d in result.duplicates_removed)

    def test_auto_resolved_garbage(self):
        """Multiple garbage patterns should be auto-resolved."""
        result = run_import_pipeline(_DIRTY_FILE)
        assert len(result.auto_resolved) >= 10

    def test_ben_whelan_lookin(self):
        result = run_import_pipeline(_DIRTY_FILE)
        lookin_resolved = [a for a in result.auto_resolved if "whelan" in a.lower()]
        assert len(lookin_resolved) >= 2  # At least DB and J&J

    def test_fuzzy_matches(self):
        result = run_import_pipeline(_DIRTY_FILE)
        assert len(result.fuzzy_matches) >= 5

    def test_bri_kvinge_fuzzy(self):
        result = run_import_pipeline(_DIRTY_FILE)
        assert any(
            "Bri Kvinge" in f and "Brianna Kvinge" in f for f in result.fuzzy_matches
        )

    def test_partner_reciprocity_warnings(self):
        result = run_import_pipeline(_DIRTY_FILE)
        non_recip = [w for w in result.warnings if "NON-RECIPROCAL" in w]
        assert len(non_recip) >= 2

    def test_eric_hoberg_unregistered(self):
        result = run_import_pipeline(_DIRTY_FILE)
        hoberg_refs = [u for u in result.unregistered_references if "Hoberg" in u]
        assert len(hoberg_refs) >= 1

    def test_inferred_gear_sharing(self):
        result = run_import_pipeline(_DIRTY_FILE)
        assert len(result.inferred) >= 20

    def test_flag_overrides(self):
        result = run_import_pipeline(_DIRTY_FILE)
        assert len(result.flag_overrides) >= 5

    def test_report_generation(self):
        result = run_import_pipeline(_DIRTY_FILE)
        report = result.report_text()
        assert "REGISTRATION IMPORT REPORT" in report
        assert "SUMMARY" in report
        assert "NEEDS PARTNER ROSTER" in report

    def test_to_entry_dicts(self):
        """Verify conversion back to entry dicts for DB commit."""
        result = run_import_pipeline(_DIRTY_FILE)
        entries = to_entry_dicts(result)
        assert len(entries) == len(result.competitors)
        for entry in entries:
            assert "name" in entry
            assert "events" in entry
            assert "partners" in entry


# ---------------------------------------------------------------------------
# 6. Import report
# ---------------------------------------------------------------------------


class TestImportReport:
    """Test ImportResult.report_text() generation."""

    def test_empty_report(self):
        result = ImportResult()
        report = result.report_text()
        assert "REGISTRATION IMPORT REPORT" in report
        assert "Total competitors: 0" in report

    def test_report_with_warnings(self):
        result = ImportResult()
        result.warnings.append("Test warning")
        report = result.report_text()
        assert "WARNINGS" in report
        assert "Test warning" in report

    def test_report_with_errors(self):
        result = ImportResult()
        result.errors.append("Test error")
        report = result.report_text()
        assert "ERRORS" in report
        assert "Test error" in report

    def test_report_with_auto_resolved(self):
        result = ImportResult()
        result.auto_resolved.append('AUTO-RESOLVED: "?" -> Needs Partner')
        report = result.report_text()
        assert "AUTO-RESOLVED" in report

    def test_report_needs_partner_roster(self):
        result = ImportResult()
        result.partner_assignments.append(
            ("Alice", "Men's Double Buck", "NEEDS_PARTNER")
        )
        result.partner_assignments.append(("Bob", "Men's Double Buck", "NEEDS_PARTNER"))
        report = result.report_text()
        assert "NEEDS PARTNER ROSTER" in report
        assert "Men's Double Buck" in report


# ---------------------------------------------------------------------------
# 7. Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Test edge cases in the pipeline."""

    def test_classify_empty_string(self):
        action, _ = _classify_partner_value("")
        assert action == "empty"

    def test_classify_none(self):
        action, _ = _classify_partner_value(None)
        assert action == "empty"

    def test_fuzzy_resolve_empty(self):
        result = ImportResult()
        assert _fuzzy_resolve("", _NAME_INDEX, result) == ""

    def test_fuzzy_resolve_none(self):
        result = ImportResult()
        assert _fuzzy_resolve(None, _NAME_INDEX, result) == ""

    def test_name_index_dedup(self):
        """Name index should handle duplicate names without error."""
        index = _build_name_index(["Alice Smith", "Alice Smith", "Bob Jones"])
        assert len(index) == 2

    def test_gender_event_unknown_event(self):
        """Unknown events should not trigger gender warnings."""
        assert _check_gender_event("M", "Mystery Event") is None
        assert _check_gender_event("F", "Mystery Event") is None
