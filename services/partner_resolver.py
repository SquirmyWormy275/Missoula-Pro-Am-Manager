"""
Partner resolution for partnered events (Jack & Jill, Double Buck, Peavey,
Pulp Toss, Partnered Axe Throw).

Partners are stored as NAME STRINGS, not FKs — `competitor.partners` is a JSON
dict keyed `event_id → partner_name` (per CLAUDE.md Data Model). Rendering a
heat for a partnered event therefore requires a name-string → competitor lookup
against the active-competitor pool, with a first-name fuzzy fallback for the
case where a judge typed "Toby" on the form and the roster has "Toby Bartsch".

Three callers needed this same logic before this module existed:
    1. routes/scheduling/heat_sheets.py:_serialize_heat_detail (hydration)
    2. routes/scheduling/heat_sheets.py:heat_sheets route (print page)
    3. services/video_judge_export.py (PR C — new)

The first two were inline duplicates; this module is where they now resolve.
Behavior must match the pre-extraction output byte-for-byte — the CRITICAL
regression tests in tests/test_partner_resolver.py enforce that.
"""

from __future__ import annotations

from models.event import Event


def _norm_alphanum(v) -> str:
    """Lowercase and strip non-alphanumeric characters for robust name matching."""
    return "".join(ch for ch in str(v or "").lower() if ch.isalnum())


def _first_token_alphanum(v) -> str:
    """First whitespace-delimited token, lowercase, alphanumeric only."""
    tokens = str(v or "").strip().lower().split()
    return "".join(ch for ch in (tokens[0] if tokens else "") if ch.isalnum())


def lookup_partner_cid(
    partner_str: str,
    comps: dict,
    self_cid: int,
) -> int | None:
    """Find a competitor id in `comps` whose name matches `partner_str`.

    Three-tier ladder via services.name_match.find_partner_match:
      1. Exact normalized-name match.
      2. First-token (first-name) match — one match only.
      3. Levenshtein ≤ 2 fuzzy match — one match only.

    Tier 3 catches typos like "McKinlay" → "McKinley" so the heat-sheet
    render can still pair-render even when the form had a misspelling.
    Returns None on ambiguous or no match.

    Args:
        partner_str: Partner name as written on the form (possibly nickname
            or single token).
        comps: Mapping of competitor_id -> competitor ORM object (must expose
            a `.name` attribute).
        self_cid: Competitor id to exclude from the search (so a pair doesn't
            match to itself).
    """
    from services.name_match import find_partner_match

    if not partner_str or not comps:
        return None

    # Pre-filter out self so name_match doesn't have to introspect tuple
    # entries. Yields a list of (cid, comp) the matcher walks via its
    # name_getter callable; matched entry is returned, we extract cid.
    pool_items = [(cid, c) for cid, c in comps.items() if cid != self_cid]

    matched = find_partner_match(
        partner_str,
        pool_items,
        name_getter=lambda item: getattr(item[1], "name", ""),
        exclude_key=None,
    )
    return matched[0] if matched is not None else None


def _resolve_partner_name_local(competitor, event: Event) -> str:
    """Return the partner name string for `competitor` in `event`, or ''.

    Mirror of routes/scheduling/__init__.py::_resolve_partner_name so this
    module has no cross-blueprint import dependency.  Keys tried in order:
    event.id (str), event.name, event.display_name, event.name.lower(),
    event.display_name.lower().
    """
    partners = competitor.get_partners() if hasattr(competitor, "get_partners") else {}
    if not isinstance(partners, dict):
        return ""
    candidates = [
        str(event.id),
        event.name,
        event.display_name,
        event.name.lower(),
        event.display_name.lower(),
    ]
    for key in candidates:
        value = partners.get(key)
        if str(value or "").strip():
            return str(value).strip()
    return ""


def pair_competitors_for_heat(
    event: Event,
    comp_ids: list,
    comp_lookup: dict,
    roster_lookup: dict | None = None,
) -> list[dict]:
    """Collapse a heat's competitor id list into one row per competitor/pair.

    For partnered events, each pair member is rendered as "Alice & Bob" on a
    single row and the partner's id is added to a `consumed` set so it doesn't
    re-render as its own row.  For non-partnered events, returns one row per
    competitor id in the order they appear.

    Behavior matches the pre-extraction logic in routes/scheduling/heat_sheets.py
    (both _serialize_heat_detail and the heat_sheets route body) byte-for-byte.
    If the partner name is only fuzzily matched, the matched competitor's
    display_name is used in preference to the raw form string so nicknames
    like "TOBY" render as "Toby Bartsch".

    Tournament-roster fallback (2026-04-23): when ``roster_lookup`` is
    supplied AND the partner can't be resolved against the heat's
    ``comp_lookup`` (because partner-resolution failed at heat-gen and the
    partner wound up in a different heat OR was held back), the matcher
    re-tries against the broader roster so the rendered string at least
    carries the partner's school tag (e.g. ``"Jordan Navas (UM-A) & McKinley
    Smith (UM-B)"``) instead of the bare partner string from the form
    (e.g. ``"Jordan Navas (UM-A) & McKinley"``). The ``partner_comp_id``
    field still reports the in-heat resolution; the broader-lookup label only
    affects display.

    Args:
        event: Event this heat belongs to (needs .id, .name, .display_name,
            .is_partnered).
        comp_ids: Ordered list of competitor IDs on the heat.
        comp_lookup: Dict competitor_id -> ORM object (must expose .name and
            .display_name and .get_partners()).
        roster_lookup: Optional dict competitor_id -> ORM object covering the
            full tournament roster (or at least every competitor enrolled in
            this event). When supplied, used as a fallback display source
            when the heat-local lookup fails. None preserves legacy raw-string
            fallback.

    Returns:
        List of dicts, one per unique pair/competitor:
            {
              'primary_comp_id': int,        # first id in the pair as listed
              'partner_comp_id': int | None, # matched partner id IN THIS HEAT, or None
              'name': str,                   # "Alice" or "Alice & Bob (TEAM)"
              'competitor': ORM | None,      # primary comp object (or None)
            }
    """
    is_partnered = bool(getattr(event, "is_partnered", False))
    rows: list[dict] = []
    consumed: set = set()

    for comp_id in comp_ids:
        if comp_id in consumed:
            continue
        comp = comp_lookup.get(comp_id)
        name = comp.display_name if comp else f"Unknown ({comp_id})"
        partner_cid: int | None = None

        if is_partnered and comp:
            partner = _resolve_partner_name_local(comp, event)
            if partner:
                partner_cid = lookup_partner_cid(partner, comp_lookup, comp_id)
                if partner_cid and partner_cid in comp_lookup:
                    # Heat-local match wins — full pair render with both school tags.
                    partner_label = comp_lookup[partner_cid].display_name
                elif roster_lookup:
                    # Partner not in this heat. Try the tournament-wide roster so
                    # the school tag still renders. Does NOT update partner_cid
                    # because the pair is split across heats — caller may want
                    # to know that.
                    roster_match_cid = lookup_partner_cid(
                        partner, roster_lookup, comp_id,
                    )
                    if roster_match_cid and roster_match_cid in roster_lookup:
                        partner_label = roster_lookup[roster_match_cid].display_name
                    else:
                        partner_label = partner
                else:
                    partner_label = partner
                name = f"{name} & {partner_label}"
                if partner_cid and partner_cid != comp_id:
                    consumed.add(partner_cid)

        rows.append(
            {
                "primary_comp_id": comp_id,
                "partner_comp_id": partner_cid,
                "name": name,
                "competitor": comp,
            }
        )

    return rows
