"""
Shared name-matching helpers for partner resolution.

Three call sites needed the same matching ladder before this module existed:
  - services.heat_generator._find_partner    (snake-draft pair builder)
  - services.partner_resolver.lookup_partner_cid (heat-sheet render)
  - services.preflight.build_preflight_report (unresolved-partner detection)

Heat-gen and render had two slightly different implementations that disagreed
on edge cases (heat-gen had no fuzz, render had no fuzz, but excel_io.py at
import time DID have fuzz with a comment "handles typos like McKinley/Mickinley"
— so a typoed name resolved at import survived to heat-gen with the fuzz still
in place, but a manually-entered typo was never caught). This module unifies
the ladder so every consumer applies the same bar.

The matching ladder, in order:
  1. Exact normalized full-name match   (alphanumeric, lowercase)
  2. First-token (first-name) match     — one-match only, refuses on ambiguity
  3. Levenshtein distance ≤ MAX_FUZZY   — one-match only, refuses on ambiguity

Levenshtein cap is intentionally tight (2 edits): catches "Mckinley/McKinlay/
Mickinley" but refuses "Mark/Mary" (1 edit, too loose for the same-name claim).
The one-match-only rule on tier 2/3 prevents silently picking the wrong person
when several pool members are similar.
"""

from __future__ import annotations

MAX_FUZZY_DISTANCE = 2


def normalize_alphanum(value) -> str:
    """Lowercase + alphanumeric-only. Robust to spaces, punctuation, case."""
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def first_token(value) -> str:
    """First whitespace-delimited token, alphanumeric-lowercased."""
    tokens = str(value or "").strip().lower().split()
    return "".join(ch for ch in (tokens[0] if tokens else "") if ch.isalnum())


def levenshtein(a: str, b: str) -> int:
    """Standard iterative Levenshtein edit distance.

    Plain-Python implementation (no external dep). O(len(a) * len(b)) time,
    O(min(len(a), len(b))) space. Roster sizes are small (≤200 entrants per
    event), so we don't need numpy or Cython.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    # Ensure b is the shorter of the two for the rolling-row optimization.
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr[j] = min(
                curr[j - 1] + 1,  # insertion
                prev[j] + 1,  # deletion
                prev[j - 1] + cost,  # substitution
            )
        prev = curr
    return prev[-1]


def find_partner_match(
    partner_name: str,
    pool: list,
    name_getter,
    exclude_key=None,
    *,
    enable_fuzzy: bool = True,
) -> object | None:
    """Find one entry in `pool` whose name matches `partner_name`.

    Args:
        partner_name: Raw partner string from the form / partners JSON.
        pool: Iterable of entries (dicts, ORM objects, anything).
        name_getter: callable(entry) -> name string. For ORM objects this is
            usually ``lambda c: c.name``; for dicts ``lambda d: d['name']``.
            Called once per entry per tier.
        exclude_key: An entry-identity to skip (so a competitor doesn't pair
            to themselves). Compared with ``is`` AND ``==`` against each entry
            so callers can pass either an id (compared with ``==`` to the
            entry's id field) OR the entry object itself.
        enable_fuzzy: When False, only tiers 1 and 2 run. Used by tests and
            future "strict mode" toggles. Default True.

    Returns:
        The matched entry (the original object from `pool`), or None on no
        match / ambiguous match at the fuzzy tier.
    """
    if not partner_name:
        return None
    target_full = normalize_alphanum(partner_name)
    if not target_full:
        return None

    def _entries():
        """Yield (entry, normalized_name, first_name) for non-excluded pool entries."""
        for entry in pool:
            # Allow exclusion by either object identity or by id field equality.
            if exclude_key is not None:
                # Direct identity / equality check.
                if entry is exclude_key or entry == exclude_key:
                    continue
                # If entry is a dict with 'id', compare against that.
                if isinstance(entry, dict) and entry.get("id") == exclude_key:
                    continue
                # If entry has an .id attribute, compare against that.
                if hasattr(entry, "id") and getattr(entry, "id") == exclude_key:
                    continue
            raw = name_getter(entry) or ""
            yield entry, normalize_alphanum(raw), first_token(raw)

    # Tier 1: exact normalized full-name match.
    for entry, norm, _first in _entries():
        if norm == target_full:
            return entry

    # Tier 2: first-token match — one match only.
    target_first = first_token(partner_name)
    if target_first:
        first_matches = [
            entry for entry, _norm, first in _entries() if first == target_first
        ]
        if len(first_matches) == 1:
            return first_matches[0]

    if not enable_fuzzy:
        return None

    # Tier 3: Levenshtein ≤ MAX_FUZZY_DISTANCE on full normalized name —
    # one match only. Catches typos like "McKinlay" vs "McKinley".
    fuzzy_matches = [
        entry
        for entry, norm, _first in _entries()
        if norm and levenshtein(norm, target_full) <= MAX_FUZZY_DISTANCE
    ]
    if len(fuzzy_matches) == 1:
        return fuzzy_matches[0]
    return None
