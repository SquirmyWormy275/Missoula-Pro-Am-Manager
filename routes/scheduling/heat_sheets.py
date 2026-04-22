"""
Heat sheet and day schedule print routes, plus schedule hydration helpers.
"""

from flask import redirect, render_template, session, url_for

import config
from config import DAY_SPLIT_EVENT_NAMES
from database import db
from models import Event, EventResult, Flight, Heat, Tournament
from models.competitor import CollegeCompetitor, ProCompetitor
from services.partner_resolver import pair_competitors_for_heat
from services.print_catalog import record_print

from . import _load_competitor_lookup, scheduling_bp


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

    competitors = [
        {
            "name": row["name"],
            "stand": assignments.get(str(row["primary_comp_id"])),
            "stand_label": _stand_label(
                stand_type, assignments.get(str(row["primary_comp_id"]))
            ),
        }
        for row in pair_competitors_for_heat(event, comp_ids, comp_lookup)
    ]
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
@record_print("heat_sheets")
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
            competitors_out = []
            for row in pair_competitors_for_heat(event, comp_ids, comps):
                cid = row["primary_comp_id"]
                name = row["name"] if row["competitor"] else f"ID:{cid}"
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
            competitors_out = []
            for row in pair_competitors_for_heat(event, comp_ids, comps):
                cid = row["primary_comp_id"]
                name = row["name"] if row["competitor"] else f"ID:{cid}"
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


@scheduling_bp.route("/<int:tournament_id>/relay-teams-sheet")
@record_print("relay_teams_sheet")
def relay_teams_sheet(tournament_id):
    """Printable Pro-Am Relay drawn-teams sheet.

    Phase 4 addition: the relay is placed in the final flight as a pseudo-heat
    (see services.flight_builder.integrate_proam_relay_into_final_flight).
    The teams themselves live in Event.event_state / payouts JSON. This route
    renders the drawn teams in a landscape, large-font, one-team-per-row
    layout suitable for taping to a wall on show day.

    Uses services.print_response.weasyprint_or_html so Railway deploys without
    cairo/pango fall back to HTML (Content-Type: text/html) while still showing
    a PDF filename hint.
    """
    from datetime import datetime

    from services.print_response import weasyprint_or_html
    from services.proam_relay import ProAmRelay

    tournament = Tournament.query.get_or_404(tournament_id)
    relay = ProAmRelay(tournament)
    state = relay.relay_data or {}
    teams = state.get("teams") or []
    # Print-ready once the lottery has run — state machine runs
    # not_drawn → drawn → in_progress → completed. Any post-lottery state
    # should render the roster (crews still need to know who's on which team
    # during scoring).
    drawn = state.get("status") in {"drawn", "in_progress", "completed"} and bool(teams)

    html = render_template(
        "scheduling/relay_teams_sheet_print.html",
        tournament=tournament,
        teams=teams,
        drawn=drawn,
        now=datetime.utcnow(),
    )
    safe_name = (
        f"{tournament.name}_{tournament.year}_relay_teams"
        .replace(" ", "_").replace("/", "-")
    )
    return weasyprint_or_html(html, safe_name)


@scheduling_bp.route("/<int:tournament_id>/day-schedule/print")
@record_print("day_schedule")
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
