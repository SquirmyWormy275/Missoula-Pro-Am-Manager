"""
Heat sheet and day schedule print routes, plus schedule hydration helpers.
"""

from flask import redirect, render_template, session, url_for

import config
from config import DAY_SPLIT_EVENT_NAMES
from database import db
from models import Event, EventResult, Flight, Heat, Tournament
from models.competitor import CollegeCompetitor, ProCompetitor

from . import _load_competitor_lookup, _resolve_partner_name, scheduling_bp


def _norm_alphanum(v) -> str:
    return "".join(ch for ch in str(v or "").lower() if ch.isalnum())


def _first_token_alphanum(v) -> str:
    s = str(v or "").strip().lower().split()
    return "".join(ch for ch in (s[0] if s else "") if ch.isalnum())


def _lookup_partner_cid(partner_str: str, comps: dict, self_cid: int) -> int | None:
    """Find the competitor id in `comps` that matches `partner_str`.

    Full normalized name first; first-name fallback if exactly one comp matches.
    Returns None on ambiguous / no match.
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
    # First-name fallback
    partner_first = _first_token_alphanum(partner_str)
    if not partner_first:
        return None
    matches = [cid for cid, c in comps.items()
               if cid != self_cid
               and _first_token_alphanum(getattr(c, "name", "")) == partner_first]
    return matches[0] if len(matches) == 1 else None


def _stand_label(stand_type: str | None, stand_number) -> str:
    """Return the physical stand label from STAND_CONFIGS, or fall back to raw number."""
    if stand_number is None:
        return "?"
    cfg = config.STAND_CONFIGS.get(stand_type or "", {})
    labels = cfg.get("labels", [])
    try:
        idx = int(stand_number) - 1
        if 0 <= idx < len(labels):
            return labels[idx]
    except (ValueError, TypeError):
        pass
    return str(stand_number)


def _hydrate_schedule_for_display(tournament: Tournament, schedule: dict) -> dict:
    """Attach heat + stand assignment details to schedule entries for display/print."""
    return {
        "friday_day": _hydrate_schedule_entries(
            tournament, schedule.get("friday_day", []), day="friday"
        ),
        "friday_feature": _hydrate_schedule_entries(
            tournament, schedule.get("friday_feature", []), day="friday"
        ),
        "saturday_show": _hydrate_schedule_entries(
            tournament, schedule.get("saturday_show", []), day="saturday"
        ),
    }


def _hydrate_schedule_entries(
    tournament: Tournament, entries: list, day: str = ""
) -> list:
    hydrated = []
    for item in entries:
        event = Event.query.get(item.get("event_id")) if item.get("event_id") else None
        detail_heats = []
        is_bracket = False
        is_partnered = False
        bracket_competitors = []

        if event:
            is_bracket = event.scoring_type == "bracket"
            is_partnered = bool(getattr(event, "is_partnered", False))

            if is_bracket:
                bracket_competitors = _get_bracket_competitors(event)
            elif item.get("heat_id"):
                heat = Heat.query.get(item["heat_id"])
                if heat:
                    detail_heats = [_serialize_heat_detail(tournament, event, heat)]
            else:
                event_heats = event.heats.order_by(
                    Heat.heat_number, Heat.run_number
                ).all()
                # Day-split filtering: Friday shows only Run 1, Saturday shows only Run 2
                if event.requires_dual_runs and event.name in DAY_SPLIT_EVENT_NAMES:
                    if day == "friday":
                        event_heats = [h for h in event_heats if h.run_number == 1]
                    elif day == "saturday" or item.get("is_run2"):
                        event_heats = [h for h in event_heats if h.run_number == 2]
                detail_heats = [
                    _serialize_heat_detail(tournament, event, h) for h in event_heats
                ]

        hydrated.append(
            {
                **item,
                "heats": detail_heats,
                "is_bracket": is_bracket,
                "is_partnered": is_partnered,
                "bracket_competitors": bracket_competitors,
            }
        )
    return hydrated


def _get_bracket_competitors(event: Event) -> list[str]:
    """Return a flat list of competitor display names for a bracket event."""
    try:
        from services.birling_bracket import BirlingBracket

        bb = BirlingBracket(event)
        bdata = bb.bracket_data
        return [
            c.get("name", f"ID:{c.get('id')}") for c in bdata.get("competitors", [])
        ]
    except Exception:
        all_ids = []
        for heat in event.heats.all():
            all_ids.extend(heat.get_competitors())
        comp_lookup = _load_competitor_lookup(event, all_ids)
        return [comp_lookup[cid].display_name for cid in all_ids if cid in comp_lookup]


def _serialize_heat_detail(tournament: Tournament, event: Event, heat: Heat) -> dict:
    assignments = heat.get_stand_assignments()
    comp_ids = heat.get_competitors()
    comp_lookup = _load_competitor_lookup(event, comp_ids)
    stand_type = event.stand_type
    is_partnered = bool(getattr(event, "is_partnered", False))

    consumed = set()
    competitors = []
    for comp_id in comp_ids:
        if comp_id in consumed:
            continue
        comp = comp_lookup.get(comp_id)
        name = comp.display_name if comp else f"Unknown ({comp_id})"
        if is_partnered and comp:
            partner = _resolve_partner_name(comp, event)
            if partner:
                partner_id = _lookup_partner_cid(partner, comp_lookup, comp_id)
                # If we matched a real competitor (even fuzzily), prefer their
                # display_name so a nickname like "TOBY" renders as "Toby Bartsch".
                partner_label = (comp_lookup[partner_id].display_name
                                 if partner_id and partner_id in comp_lookup
                                 else partner)
                name = f"{name} & {partner_label}"
                if partner_id and partner_id != comp_id:
                    consumed.add(partner_id)
        competitors.append(
            {
                "name": name,
                "stand": assignments.get(str(comp_id)),
                "stand_label": _stand_label(stand_type, assignments.get(str(comp_id))),
            }
        )
    return {
        "heat_id": heat.id,
        "heat_number": heat.heat_number,
        "run_number": heat.run_number,
        "competitors": competitors,
    }


# ---------------------------------------------------------------------------
# #7 -- Heat sheet print page
# ---------------------------------------------------------------------------


@scheduling_bp.route("/<int:tournament_id>/heat-sheets")
def heat_sheets(tournament_id):
    """Print-ready heat sheets for all flights and events."""
    from datetime import datetime

    from services.flight_builder import _STAND_CONFLICT_GAP

    tournament = Tournament.query.get_or_404(tournament_id)

    # Build {(event_id, competitor_id): status} for SCR/DNF indicators on heat sheets
    result_status = {
        (r.event_id, r.competitor_id): r.status
        for r in EventResult.query.join(Event)
        .filter(Event.tournament_id == tournament_id)
        .all()
    }

    # Build ordered heat data: flights first, then ungrouped events
    flights = (
        Flight.query.filter_by(tournament_id=tournament_id)
        .order_by(Flight.flight_number)
        .all()
    )

    flight_data = []
    for flight in flights:
        heats_in_flight = flight.get_heats_ordered()
        heat_rows = []
        for heat in heats_in_flight:
            comp_ids = heat.get_competitors()
            assignments = heat.get_stand_assignments()
            event = Event.query.get(heat.event_id)
            if not event:
                continue
            if event.event_type == "college":
                comps = (
                    {
                        c.id: c
                        for c in CollegeCompetitor.query.filter(
                            CollegeCompetitor.id.in_(comp_ids)
                        ).all()
                    }
                    if comp_ids
                    else {}
                )
            else:
                comps = (
                    {
                        c.id: c
                        for c in ProCompetitor.query.filter(
                            ProCompetitor.id.in_(comp_ids)
                        ).all()
                    }
                    if comp_ids
                    else {}
                )
            is_partnered = bool(getattr(event, "is_partnered", False))
            consumed: set[int] = set()
            competitors_out = []
            for cid in comp_ids:
                if cid in consumed:
                    continue
                comp = comps.get(cid)
                name = comp.display_name if comp else f"ID:{cid}"
                if is_partnered and comp:
                    partner = _resolve_partner_name(comp, event)
                    if partner:
                        pid = _lookup_partner_cid(partner, comps, cid)
                        partner_label = (comps[pid].display_name
                                         if pid and pid in comps
                                         else partner)
                        name = f"{name} & {partner_label}"
                        if pid and pid != cid:
                            consumed.add(pid)
                competitors_out.append({
                    "name": name,
                    "stand": assignments.get(str(cid), "?"),
                    "status": result_status.get((event.id, cid), "pending"),
                })
            heat_rows.append(
                {
                    "heat": heat,
                    "event": event,
                    "competitors": competitors_out,
                }
            )
        if heat_rows:
            # Detect Cookie Stack / Standing Block conflicts within this flight
            conflicts = []
            indexed = [
                (i, row["heat"], row["event"].stand_type)
                for i, row in enumerate(heat_rows)
            ]
            conflict_pairs = [("cookie_stack", "standing_block")]
            for i, _h, st_i in indexed:
                if not st_i:
                    continue
                for pair_a, pair_b in conflict_pairs:
                    if st_i not in (pair_a, pair_b):
                        continue
                    conflict_type = pair_b if st_i == pair_a else pair_a
                    for j, _h2, st_j in indexed:
                        if (
                            st_j == conflict_type
                            and abs(i - j) < _STAND_CONFLICT_GAP
                            and i != j
                        ):
                            conflicts.append(
                                {"pos_a": i + 1, "pos_b": j + 1, "gap": abs(i - j)}
                            )
                            break
            flight_data.append(
                {"flight": flight, "heats": heat_rows, "stand_conflicts": conflicts}
            )

    # Also gather heats with no flight (college events, standalone)
    no_flight_heats = []
    birling_brackets = []
    for event in tournament.events.order_by(Event.event_type, Event.name).all():
        # Birling bracket events get special treatment -- show bracket, not heat cards.
        if event.scoring_type == "bracket":
            from services.birling_bracket import BirlingBracket

            bb = BirlingBracket(event)
            bdata = bb.bracket_data
            has_bracket = bool(bdata.get("bracket", {}).get("winners"))
            if has_bracket:
                comp_lookup = {
                    str(c["id"]): c["name"] for c in bdata.get("competitors", [])
                }
                birling_brackets.append(
                    {
                        "event": event,
                        "bracket": bdata.get("bracket", {}),
                        "comp_lookup": comp_lookup,
                        "placements": bdata.get("placements", {}),
                        "current_matches": bb.get_current_matches(),
                    }
                )
            continue

        event_heats = (
            event.heats.filter_by(flight_id=None)
            .order_by(Heat.heat_number, Heat.run_number)
            .all()
        )
        if not event_heats:
            continue
        heat_rows = []
        for heat in event_heats:
            comp_ids = heat.get_competitors()
            assignments = heat.get_stand_assignments()
            if event.event_type == "college":
                comps = (
                    {
                        c.id: c
                        for c in CollegeCompetitor.query.filter(
                            CollegeCompetitor.id.in_(comp_ids)
                        ).all()
                    }
                    if comp_ids
                    else {}
                )
            else:
                comps = (
                    {
                        c.id: c
                        for c in ProCompetitor.query.filter(
                            ProCompetitor.id.in_(comp_ids)
                        ).all()
                    }
                    if comp_ids
                    else {}
                )
            is_partnered = bool(getattr(event, "is_partnered", False))
            consumed: set[int] = set()
            competitors_out = []
            for cid in comp_ids:
                if cid in consumed:
                    continue
                comp = comps.get(cid)
                name = comp.display_name if comp else f"ID:{cid}"
                if is_partnered and comp:
                    partner = _resolve_partner_name(comp, event)
                    if partner:
                        pid = _lookup_partner_cid(partner, comps, cid)
                        partner_label = (comps[pid].display_name
                                         if pid and pid in comps
                                         else partner)
                        name = f"{name} & {partner_label}"
                        if pid and pid != cid:
                            consumed.add(pid)
                competitors_out.append({
                    "name": name,
                    "stand": assignments.get(str(cid), "?"),
                    "status": result_status.get((event.id, cid), "pending"),
                })
            heat_rows.append(
                {
                    "heat": heat,
                    "event": event,
                    "competitors": competitors_out,
                }
            )
        no_flight_heats.append({"event": event, "heats": heat_rows})

    return render_template(
        "scheduling/heat_sheets_print.html",
        tournament=tournament,
        flight_data=flight_data,
        no_flight_heats=no_flight_heats,
        birling_brackets=birling_brackets,
        now=datetime.utcnow(),
        stand_conflict_gap=_STAND_CONFLICT_GAP,
    )


@scheduling_bp.route("/<int:tournament_id>/day-schedule/print")
def day_schedule_print(tournament_id):
    """Printable day schedule with heat/stand assignments."""
    from services.schedule_builder import build_day_schedule

    tournament = Tournament.query.get_or_404(tournament_id)
    session_key = f"schedule_options_{tournament_id}"
    saved = session.get(session_key, {})
    friday_pro_event_ids = [int(eid) for eid in saved.get("friday_pro_event_ids", [])]
    saturday_college_event_ids = [
        int(eid) for eid in saved.get("saturday_college_event_ids", [])
    ]

    schedule = build_day_schedule(
        tournament,
        friday_pro_event_ids=friday_pro_event_ids,
        saturday_college_event_ids=saturday_college_event_ids,
    )
    detailed_schedule = _hydrate_schedule_for_display(tournament, schedule)

    return render_template(
        "scheduling/day_schedule_print.html",
        tournament=tournament,
        schedule=schedule,
        detailed_schedule=detailed_schedule,
    )
