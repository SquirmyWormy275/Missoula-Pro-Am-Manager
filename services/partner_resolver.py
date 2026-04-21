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

    Full normalized-name match first; first-name fallback if exactly one comp
    matches by first token.  Returns None on ambiguous or no match.

    Args:
        partner_str: Partner name as written on the form (possibly nickname
            or single token).
        comps: Mapping of competitor_id -> competitor ORM object (must expose
            a `.name` attribute).
        self_cid: Competitor id to exclude from the search (so a pair doesn't
            match to itself).
    """
    if not partner_str:
        return None
    norm_full = _norm_alphanum(partner_str)
    if not norm_full:
        return None

    # Full match
    for cid, c in comps.items():
        if cid == self_cid:
            continue
        if _norm_alphanum(getattr(c, "name", "")) == norm_full:
            return cid

    # First-name fallback — one-match only
    partner_first = _first_token_alphanum(partner_str)
    if not partner_first:
        return None
    matches = [
        cid
        for cid, c in comps.items()
        if cid != self_cid
        and _first_token_alphanum(getattr(c, "name", "")) == partner_first
    ]
    return matches[0] if len(matches) == 1 else None


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

    Args:
        event: Event this heat belongs to (needs .id, .name, .display_name,
            .is_partnered).
        comp_ids: Ordered list of competitor IDs on the heat.
        comp_lookup: Dict competitor_id -> ORM object (must expose .name and
            .display_name and .get_partners()).

    Returns:
        List of dicts, one per unique pair/competitor:
            {
              'primary_comp_id': int,        # first id in the pair as listed
              'partner_comp_id': int | None, # matched partner id, or None
              'name': str,                   # "Alice" or "Alice & Bob"
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
                # Prefer matched comp's display_name so nicknames like "TOBY"
                # render as "Toby Bartsch".
                partner_label = (
                    comp_lookup[partner_cid].display_name
                    if partner_cid and partner_cid in comp_lookup
                    else partner
                )
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
