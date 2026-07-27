"""
Enhanced registration import pipeline for Google Forms xlsx exports.

Wraps the existing pro_entry_importer.parse_pro_entries() parser and adds:
- Dirty-file support (garbage partner detection, fuzzy matching, dedup)
- Gender-event cross-validation
- Partner reciprocity validation
- Gear sharing cross-validation and bidirectional inference
- Comprehensive structured import report

Usage:
    from services.registration_import import run_import_pipeline

    result = run_import_pipeline(filepath)
    print(result.report_text())
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class CompetitorRecord:
    """Parsed competitor from one xlsx row."""

    row_number: int
    timestamp: str | None
    email: str | None
    full_name: str
    gender: str  # 'M' or 'F'
    mailing_address: str | None
    phone: str | None
    ala_member: bool
    waiver_accepted: bool
    signature: str | None
    notes: str | None
    pro_am_lottery: bool
    events: list[str]
    partners: dict[str, str]  # event_name -> partner_name
    gear_sharing_flag: bool
    gear_sharing_details: str | None
    gear_sharing_records: list[dict]  # parsed gear sharing entries
    springboard_slow_heat: bool
    chopping_fees: int
    other_fees: int
    relay_fee: int
    total_fees: int
    # Import pipeline metadata
    raw_entry: dict | None = None  # original parse_pro_entries() dict


@dataclass
class ImportResult:
    """Full result of the import pipeline."""

    competitors: list[CompetitorRecord] = field(default_factory=list)
    event_signups: list[tuple[str, str]] = field(
        default_factory=list
    )  # (competitor_name, event)
    partner_assignments: list[tuple[str, str, str]] = field(
        default_factory=list
    )  # (competitor, event, partner)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    auto_resolved: list[str] = field(default_factory=list)
    inferred: list[str] = field(default_factory=list)
    fuzzy_matches: list[str] = field(default_factory=list)
    duplicates_removed: list[str] = field(default_factory=list)
    flag_overrides: list[str] = field(default_factory=list)
    unregistered_references: list[str] = field(default_factory=list)

    def report_text(self) -> str:
        """Generate plain-text import report."""
        lines = []
        lines.append("=" * 70)
        lines.append("REGISTRATION IMPORT REPORT")
        lines.append("=" * 70)
        lines.append("")

        # Section 1: Summary
        lines.append("1. SUMMARY")
        lines.append(f"   Total competitors: {len(self.competitors)}")
        lines.append(f"   Total event signups: {len(self.event_signups)}")
        lines.append(f"   Total partner assignments: {len(self.partner_assignments)}")
        needs_partner = [
            (c, ev) for c, ev, p in self.partner_assignments if p == "NEEDS_PARTNER"
        ]
        paired = [
            (c, ev, p) for c, ev, p in self.partner_assignments if p != "NEEDS_PARTNER"
        ]
        lines.append(f"   Paired: {len(paired)}")
        lines.append(f"   Needs partner: {len(needs_partner)}")
        lines.append(f"   Duplicates removed: {len(self.duplicates_removed)}")
        lines.append(f"   Auto-resolved: {len(self.auto_resolved)}")
        lines.append(f"   Fuzzy matches: {len(self.fuzzy_matches)}")
        lines.append(f"   Inferred gear sharing: {len(self.inferred)}")
        lines.append(f"   Warnings: {len(self.warnings)}")
        lines.append(f"   Errors: {len(self.errors)}")
        lines.append("")

        # Section 2: Errors
        if self.errors:
            lines.append("2. ERRORS")
            for e in self.errors:
                lines.append(f"   ERROR: {e}")
            lines.append("")

        # Section 3: Warnings
        if self.warnings:
            lines.append("3. WARNINGS")
            for w in self.warnings:
                lines.append(f"   WARNING: {w}")
            lines.append("")

        # Section 4: Auto-resolved
        if self.auto_resolved:
            lines.append("4. AUTO-RESOLVED (dirty-file patterns)")
            for a in self.auto_resolved:
                lines.append(f"   {a}")
            lines.append("")

        # Section 5: Fuzzy matches
        if self.fuzzy_matches:
            lines.append("5. FUZZY MATCHES")
            for f in self.fuzzy_matches:
                lines.append(f"   {f}")
            lines.append("")

        # Section 6: Inferred gear sharing
        if self.inferred:
            lines.append("6. INFERRED GEAR SHARING")
            for i in self.inferred:
                lines.append(f"   {i}")
            lines.append("")

        # Section 7: Flag overrides
        if self.flag_overrides:
            lines.append("7. FLAG OVERRIDES")
            for f in self.flag_overrides:
                lines.append(f"   {f}")
            lines.append("")

        # Section 8: Unregistered references
        if self.unregistered_references:
            lines.append("8. UNREGISTERED REFERENCES")
            for u in self.unregistered_references:
                lines.append(f"   {u}")
            lines.append("")

        # Section 9: Duplicates removed
        if self.duplicates_removed:
            lines.append("9. DUPLICATES REMOVED")
            for d in self.duplicates_removed:
                lines.append(f"   {d}")
            lines.append("")

        # Section 10: Needs partner roster
        if needs_partner:
            lines.append("10. NEEDS PARTNER ROSTER")
            by_event: dict[str, list[str]] = {}
            for comp_name, event_name in needs_partner:
                by_event.setdefault(event_name, []).append(comp_name)
            for event_name, names in sorted(by_event.items()):
                lines.append(f"    {event_name}:")
                for n in sorted(names):
                    lines.append(f"      - {n}")
            lines.append("")

        # Section 11: Non-reciprocal partnerships
        non_recip = [w for w in self.warnings if "NON-RECIPROCAL" in w.upper()]
        if non_recip:
            lines.append("11. NON-RECIPROCAL PARTNERSHIPS")
            for w in non_recip:
                lines.append(f"    {w}")
            lines.append("")

        lines.append("=" * 70)
        lines.append("END OF REPORT")
        lines.append("=" * 70)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dirty-file garbage patterns for partner fields
# ---------------------------------------------------------------------------

_NEEDS_PARTNER_PATTERNS = [
    (re.compile(r"^\s*\?\s*$"), '"?" -> Needs Partner'),
    (re.compile(r"^\s*(?:idk|IDK|Idk)\s*$", re.IGNORECASE), '"idk" -> Needs Partner'),
    (
        re.compile(r"^\s*(?:lookin|looking)\b", re.IGNORECASE),
        '"Looking" -> Needs Partner',
    ),
    (
        re.compile(r"^\s*(?:whoever|anyone\s*available|anyone)\s*$", re.IGNORECASE),
        '"whoever/anyone" -> Needs Partner',
    ),
    (
        re.compile(r"^\s*(?:no\s*(?:o?a?r?n?t?e?r?|partner)|none)\s*$", re.IGNORECASE),
        '"no partner/none" -> Needs Partner',
    ),
    (
        re.compile(r"^\s*(?:need\s*partner|needs\s*partner)\s*$", re.IGNORECASE),
        '"Needs Partner" (normalized)',
    ),
    (re.compile(r"^\s*(?:tbd|TBD)\s*$", re.IGNORECASE), '"TBD" -> Needs Partner'),
    (re.compile(r"^\s*N/?A\s*$", re.IGNORECASE), '"N/A" -> Needs Partner'),
]

_HAS_SAW_PATTERN = re.compile(r"have\s+saw", re.IGNORECASE)
_SPARE_PATTERN = re.compile(r"spare", re.IGNORECASE)


def _classify_partner_value(raw: str) -> tuple[str, str | None]:
    """Classify a partner field value.

    Returns:
        (action, label) where action is 'needs_partner', 'name', or 'empty'
        and label is a report label or None.
    """
    val = str(raw or "").strip()
    if not val:
        return "empty", None

    for pattern, label in _NEEDS_PARTNER_PATTERNS:
        if pattern.match(val):
            return "needs_partner", f"AUTO-RESOLVED: {label}"

    if _HAS_SAW_PATTERN.search(val):
        return (
            "needs_partner",
            'AUTO-RESOLVED: "Have saw, need partner" -> Needs Partner (note: has equipment)',
        )

    if _SPARE_PATTERN.search(val):
        return (
            "needs_partner",
            "AUTO-RESOLVED: spare request -> Needs Partner (note: available as spare)",
        )

    # Contains "put me down" pattern
    if re.search(r"put\s+me\s+down", val, re.IGNORECASE):
        return (
            "needs_partner",
            f'AUTO-RESOLVED: "{val[:40]}..." -> Needs Partner (spare request)',
        )

    return "name", None


# ---------------------------------------------------------------------------
# Gender-event cross-validation
# ---------------------------------------------------------------------------

_MALE_ONLY_EVENTS = {
    "Men's Underhand",
    "Men's Single Buck",
    "Men's Double Buck",
}
_FEMALE_ONLY_EVENTS = {
    "Women's Underhand",
    "Women's Standing Block",
    "Women's Single Buck",
}
# All other events are gender-neutral (springboard, hot saw, obstacle pole,
# speed climb, cookie stack, partnered axe throw, intermediate springboard,
# jack & jill, pro-am lottery)


def _check_gender_event(gender: str, event_name: str) -> str | None:
    """Return a warning string if there's a gender mismatch, else None."""
    if gender == "F" and event_name in _MALE_ONLY_EVENTS:
        return f"GENDER MISMATCH: Female competitor signed up for {event_name}"
    if gender == "M" and event_name in _FEMALE_ONLY_EVENTS:
        return f"GENDER MISMATCH: Male competitor signed up for {event_name}"
    return None


# ---------------------------------------------------------------------------
# Name fuzzy matching (uses existing gear_sharing utilities + extensions)
# ---------------------------------------------------------------------------


def _build_name_index(names: list[str]) -> dict[str, str]:
    """Build normalized name -> canonical name index."""
    from services.gear_sharing import build_name_index

    return build_name_index(names)


def _fuzzy_resolve(
    raw_name: str, name_index: dict[str, str], result: ImportResult
) -> str:
    """Resolve a partner name with fuzzy matching, logging to result."""
    from services.gear_sharing import normalize_person_name, resolve_partner_name

    candidate = str(raw_name or "").strip()
    if not candidate:
        return ""

    # Exact normalized check
    norm = normalize_person_name(candidate)
    if norm in name_index:
        canonical = name_index[norm]
        if canonical != candidate:
            result.fuzzy_matches.append(
                f"EXACT MATCH: {repr(candidate)} -> {repr(canonical)} (case/whitespace normalized)"
            )
        return canonical

    resolved = resolve_partner_name(candidate, name_index)
    if resolved != candidate:
        result.fuzzy_matches.append(
            f"FUZZY MATCH: {repr(candidate)} resolved to {repr(resolved)}"
        )
        return resolved

    # First-name-only fallback: "Henry" -> "Henry Norwood" when unambiguous
    # Only for single-word candidates with 4+ chars
    candidate_stripped = candidate.strip().rstrip("-")
    if " " not in candidate_stripped and len(candidate_stripped) >= 4:
        first_norm = normalize_person_name(candidate_stripped)
        if len(first_norm) >= 4:
            matches = [
                canonical
                for norm_key, canonical in name_index.items()
                if norm_key.startswith(first_norm) and len(norm_key) > len(first_norm)
            ]
            if len(matches) == 1:
                result.fuzzy_matches.append(
                    f"FIRST-NAME MATCH: {repr(candidate)} resolved to {repr(matches[0])}"
                )
                return matches[0]
            elif len(matches) > 1:
                result.warnings.append(
                    f'AMBIGUOUS FIRST NAME: {repr(candidate)} could be: '
                    f'{", ".join(matches)}'
                )

    return resolved


# ---------------------------------------------------------------------------
# Dirty gear sharing text parsing helpers
# ---------------------------------------------------------------------------

_EQUIPMENT_ALIASES = {
    "crosscut": ("crosscut saw", None),
    "xcut": ("crosscut saw", None),
    "op": ("obstacle pole chainsaw", "Obstacle Pole"),
    "j&j": ("jack and jill saw", "Jack & Jill"),
    "hotsaw": ("hot saw", "Hot Saw"),
    "hot saw": ("hot saw", "Hot Saw"),
    "spring board": ("springboard", "Springboard"),
    "springboard": ("springboard", "Springboard"),
    "singlebuck": ("single buck saw", None),
    "single buck": ("single buck saw", None),
    "double buck": ("double buck saw", "Men's Double Buck"),
    "pole climb": ("spurs and rope", "Speed Climb"),
    "speed climb": ("spurs and rope", "Speed Climb"),
    "caulks": ("caulk boots", None),
    "corks": ("caulk boots", None),
    "spurs": ("climbing spurs", "Speed Climb"),
    "rope": ("climbing rope", "Speed Climb"),
    "cookie stack": ("cookie stack saw", "Cookie Stack"),
    "chainsaw": ("chainsaw", None),
    "saw": ("saw", None),
}

# Conversational filler to strip
_FILLER_RE = re.compile(
    r"\b(?:me\s+and|sharing\s+a?|sharing\s+with|borrowing|will\s+need\s+to\s+share\s+a?)\b",
    re.IGNORECASE,
)


def _parse_dirty_gear_text(
    text: str,
    competitor_name: str,
    all_names: list[str],
    name_index: dict[str, str],
    result: ImportResult,
) -> list[dict]:
    """Parse dirty gear sharing text into structured records.

    Returns list of dicts with keys: equipment, event_hint, partners, conditional.
    """
    text = str(text or "").strip()
    if not text:
        return []

    records = []
    is_conditional = bool(
        re.search(
            r"\b(?:sometimes|possibly|may\s+be|tbd|likely|unsure|if\s+ours|currently)\b",
            text,
            re.IGNORECASE,
        )
    )

    # Try parenthetical grouping: "(Name1 & Name2) - Event"
    paren_matches = re.findall(r"\(([^)]+)\)\s*[-—:]\s*([^,(]+)", text)
    if paren_matches:
        for names_str, event_str in paren_matches:
            names = re.split(r"\s*[&,]\s*", names_str.strip())
            partners = []
            for n in names:
                n = n.strip()
                if n:
                    resolved = _fuzzy_resolve(n, name_index, result)
                    partners.append(resolved)
            records.append(
                {
                    "equipment": "saw",
                    "event_hint": event_str.strip(),
                    "partners": partners,
                    "conditional": is_conditional,
                }
            )
        return records

    # Try "event: name1 name2 name3" pattern (e.g., "cookie stack: cody labahn owen...")
    colon_match = re.match(r"^([^:]+):\s*(.+)$", text)
    if colon_match:
        event_part = colon_match.group(1).strip()
        names_part = colon_match.group(2).strip()
        # Split on known delimiters or try to extract names
        partners = _extract_names_from_text(
            names_part, competitor_name, all_names, name_index, result
        )
        if partners:
            records.append(
                {
                    "equipment": _guess_equipment(event_part),
                    "event_hint": event_part,
                    "partners": partners,
                    "conditional": is_conditional,
                }
            )
            return records

    # Try comma/semicolon separated segments
    segments = re.split(r"[;]|\bSharing\b", text)
    if len(segments) <= 1:
        # Try splitting on comma but only if it looks like multiple sharing items
        # (not "Name, Equipment" which is a single item)
        comma_parts = [p.strip() for p in text.split(",") if p.strip()]
        if len(comma_parts) >= 2:
            # Determine if comma-separated items are name+equipment or multiple items
            segments = comma_parts
        else:
            segments = [text]

    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue

        # Strip conversational filler
        cleaned = _FILLER_RE.sub("", segment).strip()
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        if not cleaned:
            continue

        # Try "Name - Equipment" or "Equipment - Name" pattern
        dash_parts = re.split(r"\s*[-—]\s*", cleaned, maxsplit=1)
        if len(dash_parts) == 2:
            part_a, part_b = dash_parts
            # Determine which is name, which is equipment
            a_is_equip = _is_equipment_text(part_a)
            b_is_equip = _is_equipment_text(part_b)

            if a_is_equip and not b_is_equip:
                equipment = part_a
                partner_text = part_b
            elif b_is_equip and not a_is_equip:
                equipment = part_b
                partner_text = part_a
            elif not a_is_equip and not b_is_equip:
                # Both look like names — first is probably the name
                partner_text = part_a
                equipment = part_b
            else:
                # Both look like equipment — treat whole segment as one item
                partner_text = ""
                equipment = cleaned

            partners = []
            if partner_text:
                resolved = _fuzzy_resolve(partner_text.strip(), name_index, result)
                if resolved:
                    partners.append(resolved)

            records.append(
                {
                    "equipment": _guess_equipment(equipment),
                    "event_hint": equipment.strip(),
                    "partners": partners,
                    "conditional": is_conditional,
                }
            )
        else:
            # Single segment — try to find name and equipment
            partners = _extract_names_from_text(
                cleaned, competitor_name, all_names, name_index, result
            )
            equipment = _guess_equipment(cleaned)
            records.append(
                {
                    "equipment": equipment,
                    "event_hint": cleaned,
                    "partners": partners,
                    "conditional": is_conditional,
                }
            )

    return records


def _is_equipment_text(text: str) -> bool:
    """Return True if text looks like an equipment reference."""
    low = text.strip().lower()
    equip_words = [
        "springboard",
        "spring board",
        "saw",
        "crosscut",
        "hotsaw",
        "hot saw",
        "chainsaw",
        "caulks",
        "spurs",
        "rope",
        "singlebuck",
        "single buck",
        "double buck",
        "doublebuck",
        "op",
        "j&j",
        "cookie stack",
        "obstacle pole",
        "pole climb",
        "speed climb",
    ]
    return any(w in low for w in equip_words)


def _guess_equipment(text: str) -> str:
    """Map text to a canonical equipment name."""
    low = text.strip().lower()
    # Check aliases
    for alias, (canonical, _) in _EQUIPMENT_ALIASES.items():
        if alias in low:
            return canonical
    return "unknown"


def _extract_names_from_text(
    text: str,
    self_name: str,
    all_names: list[str],
    name_index: dict[str, str],
    result: ImportResult,
) -> list[str]:
    """Try to extract person names from messy text."""
    from services.gear_sharing import normalize_person_name

    self_norm = normalize_person_name(self_name)
    found = []

    # First try: look for known names in the text
    for norm_name, canonical in name_index.items():
        if norm_name and len(norm_name) >= 4 and norm_name != self_norm:
            if norm_name in normalize_person_name(text):
                found.append(canonical)

    if found:
        return found

    # Second try: split text into potential name tokens and fuzzy match
    # Remove equipment words first
    cleaned = re.sub(
        r"\b(?:saw|springboard|spring\s+board|crosscut|hotsaw|hot\s+saw|"
        r"chainsaw|caulks|spurs|rope|op|obstacle\s+pole|cookie\s+stack|"
        r"single\s+buck|double\s+buck|j&j|jack\s+and\s+jill|speed\s+climb|"
        r"pole\s+climb|and|for|with|sharing|borrowing|the|a|an)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()

    # Try to find 2-word name patterns (First Last)
    words = cleaned.split()
    i = 0
    while i < len(words) - 1:
        candidate = f"{words[i]} {words[i+1]}"
        resolved = _fuzzy_resolve(candidate, name_index, result)
        norm_resolved = normalize_person_name(resolved)
        if norm_resolved in name_index and norm_resolved != self_norm:
            found.append(resolved)
            i += 2
            continue
        i += 1

    # Try single words as first names (minimum 4 chars to avoid false positives
    # like "she" matching "Shea")
    if not found:
        for word in words:
            word = word.strip().rstrip("-")
            if len(word) < 4:
                continue
            word_low = normalize_person_name(word)
            if len(word_low) < 4:
                continue
            # Check if it's a first name of exactly one person
            first_name_matches = [
                canonical
                for norm, canonical in name_index.items()
                if norm.startswith(word_low)
                and norm != self_norm
                and len(word_low) >= 4
            ]
            if len(first_name_matches) == 1:
                found.append(first_name_matches[0])
                result.fuzzy_matches.append(
                    f"FIRST-NAME MATCH: {repr(word)} resolved to {repr(first_name_matches[0])}"
                )
            elif len(first_name_matches) > 1:
                result.warnings.append(
                    f'AMBIGUOUS FIRST NAME: {repr(word)} could be: {", ".join(first_name_matches)}'
                )

    return found


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------


def run_import_pipeline(filepath: str) -> ImportResult:
    """Run the full import pipeline on an xlsx file.

    This wraps parse_pro_entries() and adds all validation/cross-validation.
    """
    result = ImportResult()

    # Step 1: Parse with existing parser
    try:
        from services.pro_entry_importer import parse_pro_entries

        raw_entries = parse_pro_entries(filepath)
    except Exception as exc:
        result.errors.append(f"Failed to parse xlsx: {exc}")
        return result

    if not raw_entries:
        result.errors.append("No entries found in file.")
        return result

    # Step 2: Deduplication (keep latest by timestamp for same email)
    entries = _deduplicate(raw_entries, result)

    # Step 3: Build name index from all entries
    all_names = [
        str(e.get("name", "")).strip()
        for e in entries
        if str(e.get("name", "")).strip()
    ]
    name_index = _build_name_index(all_names)

    # Step 4: Process each entry into CompetitorRecord
    for row_num, entry in enumerate(entries, start=1):
        try:
            comp = _process_entry(entry, row_num, name_index, all_names, result)
            result.competitors.append(comp)

            # Collect event signups
            for event_name in comp.events:
                result.event_signups.append((comp.full_name, event_name))
                # Gender cross-validation
                warning = _check_gender_event(comp.gender, event_name)
                if warning:
                    result.warnings.append(f"{comp.full_name}: {warning}")

            # Collect partner assignments
            for event_name, partner_name in comp.partners.items():
                result.partner_assignments.append(
                    (comp.full_name, event_name, partner_name)
                )

        except Exception as exc:
            result.errors.append(f"Row {row_num}: failed to process entry: {exc}")

    # Step 5: Cross-validation
    _validate_partner_reciprocity(result)
    _validate_gear_sharing(result, name_index)
    _infer_gear_from_partnerships(result)
    _reconcile_gear_flags(result)
    _check_unregistered_references(result)

    return result


def _deduplicate(entries: list[dict], result: ImportResult) -> list[dict]:
    """Remove duplicate entries, keeping the latest by timestamp per email."""
    by_email: dict[str, list[dict]] = {}
    no_email: list[dict] = []

    for entry in entries:
        email = str(entry.get("email") or "").strip().lower()
        if email:
            by_email.setdefault(email, []).append(entry)
        else:
            no_email.append(entry)

    deduped = list(no_email)
    for email, group in by_email.items():
        if len(group) == 1:
            deduped.append(group[0])
        else:
            # Sort by timestamp, keep latest
            def _ts_key(e):
                ts = e.get("submission_timestamp", "")
                if not ts:
                    return ""
                return str(ts)

            group.sort(key=_ts_key)
            kept = group[-1]
            deduped.append(kept)
            for dropped in group[:-1]:
                result.duplicates_removed.append(
                    f'DUPLICATE: {dropped.get("name")} ({email}) submitted '
                    f'{dropped.get("submission_timestamp", "unknown")} '
                    f'— kept later entry from {kept.get("submission_timestamp", "unknown")}'
                )

    # Also check for duplicate names with different emails
    name_groups: dict[str, list[dict]] = {}
    for entry in deduped:
        name_key = str(entry.get("name", "")).strip().lower()
        if name_key:
            name_groups.setdefault(name_key, []).append(entry)

    for name_key, group in name_groups.items():
        if len(group) > 1:
            emails = [str(e.get("email", "no email")) for e in group]
            result.warnings.append(
                f'DUPLICATE NAME: "{group[0].get("name")}" appears {len(group)} times '
                f'with emails: {", ".join(emails)} — may be different people or same person with multiple emails'
            )

    return deduped


def _process_entry(
    entry: dict,
    row_num: int,
    name_index: dict[str, str],
    all_names: list[str],
    result: ImportResult,
) -> CompetitorRecord:
    """Convert a raw parsed entry into a CompetitorRecord with dirty-file handling."""
    name = str(entry.get("name", "")).strip()

    # Gender
    gender = str(entry.get("gender", "")).strip().upper()
    if gender not in ("M", "F"):
        result.warnings.append(
            f'{name}: Invalid gender "{gender}" — needs manual review'
        )

    # Waiver check
    if not entry.get("waiver_accepted"):
        result.warnings.append(f"{name}: Waiver NOT accepted — needs attention")

    # Process partner fields with dirty-file detection
    partners = {}
    raw_partners = entry.get("partners", {})

    for event_name, raw_partner in raw_partners.items():
        action, label = _classify_partner_value(raw_partner)
        if action == "needs_partner":
            partners[event_name] = "NEEDS_PARTNER"
            if label:
                result.auto_resolved.append(f"{name} ({event_name}): {label}")
        elif action == "name":
            resolved = _fuzzy_resolve(raw_partner, name_index, result)
            partners[event_name] = resolved
        # 'empty' -> skip

    # Parse gear sharing text
    gear_records = []
    if entry.get("gear_sharing_details"):
        gear_records = _parse_dirty_gear_text(
            entry["gear_sharing_details"], name, all_names, name_index, result
        )

    return CompetitorRecord(
        row_number=row_num,
        timestamp=entry.get("submission_timestamp"),
        email=entry.get("email"),
        full_name=name,
        gender=gender,
        mailing_address=entry.get("mailing_address"),
        phone=entry.get("phone"),
        ala_member=bool(entry.get("ala_member")),
        waiver_accepted=bool(entry.get("waiver_accepted")),
        signature=entry.get("waiver_signature"),
        notes=entry.get("notes"),
        pro_am_lottery=bool(entry.get("relay_lottery")),
        events=entry.get("events", []),
        partners=partners,
        gear_sharing_flag=bool(entry.get("gear_sharing")),
        gear_sharing_details=entry.get("gear_sharing_details"),
        gear_sharing_records=gear_records,
        springboard_slow_heat=bool(entry.get("springboard_slow_heat")),
        chopping_fees=entry.get("chopping_fees", 0),
        other_fees=entry.get("other_fees", 0),
        relay_fee=entry.get("relay_fee", 0),
        total_fees=entry.get("total_fees", 0),
        raw_entry=entry,
    )


def _validate_partner_reciprocity(result: ImportResult):
    """Check that partnerships are reciprocal."""
    # Build lookup: name -> CompetitorRecord
    by_name: dict[str, CompetitorRecord] = {}
    for comp in result.competitors:
        by_name[comp.full_name.strip().lower()] = comp

    for comp in result.competitors:
        for event_name, partner_name in comp.partners.items():
            if partner_name == "NEEDS_PARTNER":
                continue

            partner_key = partner_name.strip().lower()
            partner_comp = by_name.get(partner_key)
            if partner_comp is None:
                # Partner not in the import data
                result.warnings.append(
                    f"NON-RECIPROCAL: {comp.full_name} lists {partner_name} as "
                    f"{event_name} partner, but {partner_name} is not in the import data"
                )
                continue

            # Check if partner lists this competitor back
            partner_partner = partner_comp.partners.get(event_name, "")
            if partner_partner == "NEEDS_PARTNER":
                # Partner needs a partner — not a conflict, just informational
                continue

            if partner_partner:
                partner_partner_key = partner_partner.strip().lower()
                if partner_partner_key != comp.full_name.strip().lower():
                    result.warnings.append(
                        f"NON-RECIPROCAL: {comp.full_name} lists {partner_name} for "
                        f"{event_name}, but {partner_name} lists {partner_partner} instead"
                    )
            # If partner has no entry for this event, that's OK (they might not
            # have the event column filled or it's a different event mapping)


def _validate_gear_sharing(result: ImportResult, name_index: dict[str, str]):
    """Validate gear sharing consistency."""
    for comp in result.competitors:
        # Check flag vs details mismatch
        has_records = bool(comp.gear_sharing_records)
        if comp.gear_sharing_flag and not has_records and not comp.gear_sharing_details:
            result.warnings.append(
                f"{comp.full_name}: gear sharing flag is Yes but no details provided"
            )
        elif not comp.gear_sharing_flag and comp.gear_sharing_details:
            result.warnings.append(
                f"{comp.full_name}: gear sharing flag is No but details text present: "
                f'"{str(comp.gear_sharing_details)[:60]}..."'
            )

        # Flag conditional records
        for rec in comp.gear_sharing_records:
            if rec.get("conditional"):
                partner_str = ", ".join(rec.get("partners", []))
                result.warnings.append(
                    f"CONDITIONAL: {comp.full_name} gear sharing with {partner_str} "
                    f'for {rec.get("event_hint", "unknown")} — needs confirmation'
                )


def _infer_gear_from_partnerships(result: ImportResult):
    """Infer gear sharing from partner assignments for paired events."""
    # Paired events that inherently require shared equipment
    _PAIRED_GEAR = {
        "Jack & Jill Sawing": "Jack & Jill saw",
        "Men's Double Buck": "Double Buck saw",
        "Partnered Axe Throw": "axes",
    }

    by_name: dict[str, CompetitorRecord] = {}
    for comp in result.competitors:
        by_name[comp.full_name.strip().lower()] = comp

    for comp in result.competitors:
        for event_name, partner_name in comp.partners.items():
            if partner_name == "NEEDS_PARTNER":
                continue

            # Check if this is a paired event with implicit gear sharing
            equipment = _PAIRED_GEAR.get(event_name)
            if not equipment:
                continue

            # Check if this sharing is already explicitly recorded
            already_recorded = False
            for rec in comp.gear_sharing_records:
                partner_list = [p.strip().lower() for p in rec.get("partners", [])]
                if partner_name.strip().lower() in partner_list:
                    already_recorded = True
                    break

            if not already_recorded:
                result.inferred.append(
                    f"INFERRED: {comp.full_name} shares {equipment} with {partner_name} "
                    f"(inferred from {event_name} partner assignment)"
                )


def _reconcile_gear_flags(result: ImportResult):
    """Override gear sharing flags based on actual sharing data."""
    for comp in result.competitors:
        has_any_sharing = bool(comp.gear_sharing_records)
        # Also check if they have partner assignments for paired events
        paired_events = {
            "Jack & Jill Sawing",
            "Men's Double Buck",
            "Partnered Axe Throw",
        }
        has_paired = any(
            ev in paired_events and partner != "NEEDS_PARTNER"
            for ev, partner in comp.partners.items()
        )

        if (has_any_sharing or has_paired) and not comp.gear_sharing_flag:
            reason_parts = []
            if has_any_sharing:
                reason_parts.append("has gear sharing details")
            if has_paired:
                paired_list = [
                    f"{ev} with {p}"
                    for ev, p in comp.partners.items()
                    if ev in paired_events and p != "NEEDS_PARTNER"
                ]
                reason_parts.append(f'paired: {", ".join(paired_list)}')
            result.flag_overrides.append(
                f"FLAG OVERRIDE: {comp.full_name} sharing_gear changed No -> Yes "
                f'({"; ".join(reason_parts)})'
            )
            comp.gear_sharing_flag = True

        if comp.gear_sharing_flag and not has_any_sharing and not has_paired:
            if not comp.gear_sharing_details:
                result.warnings.append(
                    f"{comp.full_name}: gear flag Yes but no sharing data found"
                )


def _check_unregistered_references(result: ImportResult):
    """Check for references to people not in the import data."""
    registered_names = {comp.full_name.strip().lower() for comp in result.competitors}

    # Check gear sharing records
    for comp in result.competitors:
        for rec in comp.gear_sharing_records:
            for partner in rec.get("partners", []):
                if partner.strip().lower() not in registered_names:
                    result.unregistered_references.append(
                        f"UNREGISTERED: {repr(partner)} referenced by {comp.full_name} "
                        f'in gear sharing ({rec.get("event_hint", "unknown")}) '
                        f"but not found in registration data"
                    )

    # Check partner assignments
    for comp in result.competitors:
        for event_name, partner_name in comp.partners.items():
            if partner_name == "NEEDS_PARTNER":
                continue
            if partner_name.strip().lower() not in registered_names:
                # Already covered by reciprocity check, but add to unregistered list
                if not any(partner_name in u for u in result.unregistered_references):
                    result.unregistered_references.append(
                        f"UNREGISTERED: {repr(partner_name)} listed as {event_name} partner "
                        f"by {comp.full_name} but not found in registration data"
                    )


# ---------------------------------------------------------------------------
# Convenience: convert ImportResult back to entry dicts for DB commit
# ---------------------------------------------------------------------------


def to_entry_dicts(result: ImportResult) -> list[dict]:
    """Convert ImportResult competitors back to entry dicts compatible with
    the existing confirm_pro_entries() route.

    This allows the enhanced pipeline to feed into the existing DB commit flow.
    """
    entries = []
    for comp in result.competitors:
        # Convert partners back, replacing NEEDS_PARTNER with empty
        clean_partners = {}
        for event_name, partner in comp.partners.items():
            if partner != "NEEDS_PARTNER":
                clean_partners[event_name] = partner

        entry = comp.raw_entry.copy() if comp.raw_entry else {}
        entry.update(
            {
                "name": comp.full_name,
                "email": comp.email,
                "gender": comp.gender,
                "mailing_address": comp.mailing_address,
                "phone": comp.phone,
                "ala_member": comp.ala_member,
                "events": comp.events,
                "relay_lottery": comp.pro_am_lottery,
                "partners": clean_partners,
                "gear_sharing": comp.gear_sharing_flag,
                "gear_sharing_details": comp.gear_sharing_details,
                "waiver_accepted": comp.waiver_accepted,
                "waiver_signature": comp.signature,
                "notes": comp.notes,
                "springboard_slow_heat": comp.springboard_slow_heat,
                "chopping_fees": comp.chopping_fees,
                "other_fees": comp.other_fees,
                "relay_fee": comp.relay_fee,
                "total_fees": comp.total_fees,
                "submission_timestamp": comp.timestamp,
            }
        )
        entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# CLI entry point for testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m services.registration_import <path_to_xlsx>")
        sys.exit(1)

    filepath = sys.argv[1]
    result = run_import_pipeline(filepath)
    print(result.report_text())
    sys.exit(1 if result.errors else 0)
