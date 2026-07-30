"""
Aggregated schedule-state summary for the Events & Schedule page.

Rendered as a "Current Schedule" card at the top of events.html so judges
see the actual state of heats, flights, and warnings inline after every
build/generate action — no round-trip to day_schedule / flights /
show_day / per-event heats pages just to verify "did that work?".

Design constraints:
- Read-only. No mutations.
- Fast enough to render on every GET of event_list. Avoid N+1.
- Cross-links back to the detail pages when the judge needs more than
  a count.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import TypedDict

from flask import url_for

from config import LIST_ONLY_EVENT_NAMES
from database import db
from models.competitor import CollegeCompetitor, ProCompetitor
from models.event import Event
from models.heat import Flight, Heat
from models.tournament import Tournament

# Pro events that never produce regular Heat rows because their progression
# is managed by a state machine stored in Event.payouts JSON. Surfacing
# "no heats yet" for these on the Run Show panel is a false alarm — both
# events are working as designed when their heat count is zero.
_STATE_MACHINE_PRO_NAMES = {"partneredaxethrow", "proamrelay"}


class Warning_(TypedDict, total=False):
    severity: str  # 'danger' | 'warning' | 'info'
    title: str
    detail: str
    link: str | None
    link_label: str | None
    # When set, the events.html warning panel renders the call-to-action
    # as a POST <form> submitting this value as the ``action`` field to
    # ``scheduling.event_list`` instead of a hyperlink. Lets a single
    # click on the warning actually run the operation it advertises
    # (e.g. "Generate pro heats" actually generates), instead of bouncing
    # the user back to the page they are already on.
    submit_action: str | None


class DayStatus(TypedDict):
    events_configured: int
    events_with_heats: int
    heats_total: int
    competitors_total: int
    competitors_placed: int
    competitors_non_heat_only: int  # only signed up for list-only / bracket / state-machine events
    competitors_no_events: int  # entered no events at all
    competitors_missing_from_heats: int  # entered a heat event but not in any heat — BUG surface
    competitors_missing_sample: list[str]  # up to 10 names for drill-down
    detail_link: str
    detail_label: str


class ScheduleStatus(TypedDict):
    friday: DayStatus
    saturday: DayStatus
    saturday_flights: int
    saturday_heats_per_flight_avg: float
    warnings: list[Warning_]
    overall_label: str
    overall_severity: str  # 'success' | 'warning' | 'danger' | 'info'


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def build_schedule_status(tournament: Tournament) -> ScheduleStatus:
    """Aggregated day-by-day status for the Events page status panel."""
    events = list(tournament.events.all())
    college_events = [e for e in events if e.event_type == "college"]
    pro_events = [e for e in events if e.event_type == "pro"]

    heats_by_event = _heats_by_event(tournament.id)

    college_competitors = CollegeCompetitor.query.filter_by(
        tournament_id=tournament.id, status="active",
    ).all()
    pro_competitors = ProCompetitor.query.filter_by(
        tournament_id=tournament.id, status="active",
    ).all()

    friday = _day_status(
        tournament_id=tournament.id,
        events=college_events,
        heats_by_event=heats_by_event,
        detail_endpoint="scheduling.day_schedule",
        competitors=college_competitors,
    )
    saturday = _day_status(
        tournament_id=tournament.id,
        events=pro_events,
        heats_by_event=heats_by_event,
        detail_endpoint="scheduling.flight_list",
        competitors=pro_competitors,
    )

    flight_count, avg_heats_per_flight = _flight_stats(tournament.id)

    warnings = _build_warnings(
        tournament=tournament,
        college_events=college_events,
        pro_events=pro_events,
        heats_by_event=heats_by_event,
        flight_count=flight_count,
    )

    overall_label, overall_severity = _overall(
        friday=friday,
        saturday=saturday,
        flight_count=flight_count,
        warnings=warnings,
    )

    return {
        "friday": friday,
        "saturday": saturday,
        "saturday_flights": flight_count,
        "saturday_heats_per_flight_avg": avg_heats_per_flight,
        "warnings": warnings,
        "overall_label": overall_label,
        "overall_severity": overall_severity,
    }


# ---------------------------------------------------------------------------
# Heat + flight aggregation
# ---------------------------------------------------------------------------


def _heats_by_event(tournament_id: int) -> dict[int, list[Heat]]:
    """One query, group heats by event_id. Avoids N+1 over events."""
    heats = Heat.query.join(Event).filter(Event.tournament_id == tournament_id).all()
    grouped: dict[int, list[Heat]] = defaultdict(list)
    for h in heats:
        grouped[h.event_id].append(h)
    return grouped


def _day_status(
    tournament_id: int,
    events: list[Event],
    heats_by_event: dict[int, list[Heat]],
    detail_endpoint: str,
    competitors: list,
) -> DayStatus:
    # Local import to avoid circular dependency at module load.
    from routes.scheduling import _competitor_entered_event, _is_list_only_event

    heats_total = 0
    events_with_heats = 0
    placed_ids: set[int] = set()

    # Classify every event as heat / non-heat (list-only, bracket, pro state machine).
    heat_event_ids: set[int] = set()
    non_heat_event_ids: set[int] = set()
    for ev in events:
        name_norm = _norm_event_name(ev.name)
        is_list_only = _is_list_only_event(ev)
        is_bracket = ev.scoring_type == "bracket"
        is_state_machine = (
            ev.event_type == "pro" and name_norm in _STATE_MACHINE_PRO_NAMES
        )
        if is_list_only or is_bracket or is_state_machine:
            non_heat_event_ids.add(ev.id)
        else:
            heat_event_ids.add(ev.id)

    # Bound placement counting to the ACTIVE competitor population.
    # Without this, a competitor scratched mid-event still appears in the
    # heats they were originally assigned to, inflating the numerator above
    # the active denominator (e.g. "38 / 37"). The four buckets must always
    # sum to competitors_total exactly — and competitors_total is "active".
    active_ids = {c.id for c in competitors}
    for ev in events:
        ev_heats = heats_by_event.get(ev.id, [])
        if ev_heats:
            events_with_heats += 1
            heats_total += len(ev_heats)
            for h in ev_heats:
                for cid in h.get_competitors():
                    cid_int = int(cid)
                    if cid_int in active_ids:
                        placed_ids.add(cid_int)

    # Classify each competitor against THIS day's events.
    non_heat_only = 0
    no_events = 0
    missing_from_heats = 0
    missing_sample: list[str] = []

    for comp in competitors:
        if comp.id in placed_ids:
            continue

        try:
            entered = comp.get_events_entered() if hasattr(comp, "get_events_entered") else []
        except Exception:
            entered = []
        if not isinstance(entered, list) or not entered:
            no_events += 1
            continue

        entered_heat_event = False
        entered_non_heat_event = False
        for ev in events:
            # Apply the same gender filter that _signed_up_competitors uses
            # when building the actual heat list. Without this, a Men's
            # competitor entered in "Underhand" matches BOTH the Men's and
            # Women's "Underhand" event records by name, and a heatless
            # opposite-gender event silently flags them as MISSING FROM HEATS.
            ev_gender = getattr(ev, "gender", None)
            comp_gender = getattr(comp, "gender", None)
            if ev_gender and comp_gender and ev_gender != comp_gender:
                continue
            if not _competitor_entered_event(ev, entered):
                continue
            if ev.id in heat_event_ids:
                entered_heat_event = True
            elif ev.id in non_heat_event_ids:
                entered_non_heat_event = True

        if entered_heat_event:
            missing_from_heats += 1
            if len(missing_sample) < 10:
                missing_sample.append(comp.name)
        elif entered_non_heat_event:
            non_heat_only += 1
        else:
            # Entered events that don't belong to THIS day
            # (e.g. pro competitor whose events_entered only matches Saturday pro
            # when we're tallying Friday). Treat as "no events for this day".
            no_events += 1

    return {
        "events_configured": len(events),
        "events_with_heats": events_with_heats,
        "heats_total": heats_total,
        "competitors_total": len(competitors),
        "competitors_placed": len(placed_ids),
        "competitors_non_heat_only": non_heat_only,
        "competitors_no_events": no_events,
        "competitors_missing_from_heats": missing_from_heats,
        "competitors_missing_sample": missing_sample,
        "detail_link": url_for(detail_endpoint, tournament_id=tournament_id),
        "detail_label": (
            "Day schedule" if detail_endpoint.endswith("day_schedule") else "Flights"
        ),
    }


def _norm_event_name(value: str) -> str:
    return "".join(ch.lower() for ch in (value or "") if ch.isalnum())


def _flight_stats(tournament_id: int) -> tuple[int, float]:
    """Return (flight_count, avg_heats_per_flight) for Saturday pro flights."""
    flight_ids = [
        row[0]
        for row in db.session.query(Flight.id)
        .filter(Flight.tournament_id == tournament_id)
        .all()
    ]
    if not flight_ids:
        return 0, 0.0
    heat_count = (
        db.session.query(db.func.count(Heat.id))
        .filter(Heat.flight_id.in_(flight_ids))
        .scalar()
    ) or 0
    return len(flight_ids), round(heat_count / len(flight_ids), 1)


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------


def _build_warnings(
    tournament: Tournament,
    college_events: list[Event],
    pro_events: list[Event],
    heats_by_event: dict[int, list[Heat]],
    flight_count: int,
) -> list[Warning_]:
    warnings: list[Warning_] = []
    tid = tournament.id

    # --- 1. Events configured but no heats yet ---
    # List-only college events (Axe Throw, Caber Toss, Peavey, Pulp Toss) are
    # signup-only by name regardless of the is_open flag — they never produce
    # heats. State-machine pro events (Partnered Axe Throw, Pro-Am Relay) also
    # never produce regular Heat rows. Excluding both classes here removes the
    # phantom "X events have no heats" banner that fired on every Generate.
    college_missing = [
        e
        for e in college_events
        if not heats_by_event.get(e.id) and not _is_signup_only_college(e)
    ]
    if college_missing:
        warnings.append(
            {
                "severity": "warning",
                "title": f"{len(college_missing)} college event(s) have no heats yet",
                "detail": ", ".join(_display_event_name(e) for e in college_missing[:5])
                + ("…" if len(college_missing) > 5 else ""),
                "link": url_for("scheduling.event_list", tournament_id=tid),
                "link_label": "Generate college heats",
                "submit_action": "generate_all",
            }
        )

    pro_missing = [
        e
        for e in pro_events
        if not heats_by_event.get(e.id) and not _is_state_machine_pro(e)
    ]
    if pro_missing:
        warnings.append(
            {
                "severity": "warning",
                "title": f"{len(pro_missing)} pro event(s) have no heats yet",
                "detail": ", ".join(_display_event_name(e) for e in pro_missing[:5])
                + ("…" if len(pro_missing) > 5 else ""),
                "link": url_for("scheduling.event_list", tournament_id=tid),
                "link_label": "Generate pro heats",
                "submit_action": "generate_all",
            }
        )

    # --- 2. Pro heats generated but flights not built ---
    pro_heats_exist = any(heats_by_event.get(e.id) for e in pro_events)
    if pro_heats_exist and flight_count == 0:
        warnings.append(
            {
                "severity": "warning",
                "title": "Pro heats exist but flights are not built",
                "detail": 'Click "Build Flights" or "One-click Saturday Show Build" to group heats into flights.',
                "link": url_for("scheduling.event_list", tournament_id=tid),
                "link_label": "Build flights",
                "submit_action": "rebuild_flights",
            }
        )

    # --- 3. Gear-sharing conflicts in existing heats ---
    try:
        from services.gear_sharing import build_gear_report

        gear = build_gear_report(tournament)
        pro_conflicts = gear.get("pro_conflicts", []) or []
        if pro_conflicts:
            warnings.append(
                {
                    "severity": "danger",
                    "title": f"{len(pro_conflicts)} gear-sharing conflict(s) in pro heats",
                    "detail": "Competitors who share equipment are in the same heat. Use auto-fix or the manager.",
                    "link": url_for("registration.pro_gear_sharing", tournament_id=tid),
                    "link_label": "Gear Sharing Manager",
                }
            )
    except Exception:
        # Gear report is best-effort; never block the status panel on it
        pass

    # --- 4. Events that share physical stands running too close together ---
    #
    # The builder tries to keep a gap between events on shared stands, but the
    # block is not absolute: when every remaining candidate is blocked it
    # re-scores with the check disabled, and college spillover is integrated
    # afterwards by a path that never consults the rule. So violations do reach
    # the built schedule and the crew needs to know which changeovers are tight.
    #
    # This is a briefing item, not a blocker: no button on the page clears it
    # (a full rebuild was measured to take the 2026 count from 11 to 10), so it
    # is severity 'warning' with a link to the flight editor where the heats can
    # actually be reordered by hand.
    stand_conflicts = _find_stand_conflicts(tournament.id)
    if stand_conflicts:
        pair_counts: dict[tuple[str, str], int] = defaultdict(int)
        for pair in stand_conflicts:
            pair_counts[pair] += 1
        pairs_text = ", ".join(
            f"{earlier} then {later}" + (f" (x{n})" if n > 1 else "")
            for (earlier, later), n in sorted(
                pair_counts.items(), key=lambda kv: (-kv[1], kv[0])
            )
        )
        warnings.append(
            {
                "severity": "warning",
                "title": f"{len(stand_conflicts)} back-to-back heat(s) on shared stands",
                "detail": (
                    f"These events share physical stands and run too close together "
                    f"for a changeover: {pairs_text}. Rebuilding flights does not "
                    f"clear this. Reorder the heats in the flight editor, or brief "
                    f"the crew to reset the stands between them."
                ),
                "link": url_for("scheduling.flight_list", tournament_id=tid),
                "link_label": "Flight editor",
            }
        )

    return warnings


def _is_open_list_only(event: Event) -> bool:
    """College OPEN events with no heats (sign-up-only) are not a warning.

    Retained for callers/tests that import this helper directly. Prefer
    ``_is_signup_only_college`` for the broader name-driven check used by
    the warning aggregator.
    """
    return event.event_type == "college" and bool(getattr(event, "is_open", False))


def _is_signup_only_college(event: Event) -> bool:
    """College events that never produce heats — Axe Throw, Caber Toss,
    Peavey Log Roll, Pulp Toss. These run come-and-go signup-list format
    no matter how the operator toggled OPEN/CLOSED on the setup page.
    """
    if event.event_type != "college":
        return False
    if bool(getattr(event, "is_open", False)):
        return True
    normalized = re.sub(r"[^a-z0-9]+", "", str(event.name or "").lower())
    return normalized in LIST_ONLY_EVENT_NAMES


def _is_state_machine_pro(event: Event) -> bool:
    """Pro events whose progression is stored in Event.payouts JSON, not
    Heat rows. Partnered Axe Throw runs prelims → finals via state machine
    and only inserts heats during finals. Pro-Am Relay synthesises a single
    pseudo-Heat at flight-build time. Either having zero Heat rows is the
    expected steady state, not a configuration gap.
    """
    if event.event_type != "pro":
        return False
    normalized = re.sub(r"[^a-z0-9]+", "", str(event.name or "").lower())
    return normalized in _STATE_MACHINE_PRO_NAMES


def _display_event_name(e: Event) -> str:
    base = e.name
    gender = (getattr(e, "gender", None) or "").strip()
    if gender:
        return f"{base} ({gender})"
    return base


def _stand_label(stand_type: str) -> str:
    """Human name for a stand_type, e.g. 'obstacle_pole' -> 'Obstacle Pole'."""
    return (stand_type or "").replace("_", " ").title()


def _find_stand_conflicts(tournament_id: int) -> list[tuple[str, str]]:
    """Placements in the built show order that break the builder's stand rule.

    A flight is a SEQUENCE of heats, not a simultaneous group: Heat.flight_position
    is the 1-based order of a heat WITHIN its flight. Two events that share
    physical stands therefore never collide "at the same time"; they collide as
    a changeover with no break. What matters is the GAP between them, measured
    over the whole show order, exactly as the builder measures it while placing
    heats (flight_builder._calculate_heat_score, current_position = len(ordered)).

    The rule table and the gap are IMPORTED from services.flight_builder rather
    than restated here, so there is one spelling of the rule and it cannot
    drift. The local import keeps schedule_status free of a module-level
    dependency on the builder.

    Returns the clashing (earlier_stand, later_stand) label pairs, one entry per
    violation, so the caller can name them.
    """
    from services.flight_builder import _CONFLICTING_STANDS, _STAND_CONFLICT_GAP

    rows = (
        db.session.query(Heat.id, Event.stand_type)
        .join(Flight, Flight.id == Heat.flight_id)
        .join(Event, Event.id == Heat.event_id)
        .filter(Flight.tournament_id == tournament_id)
        .order_by(Flight.flight_number, Heat.flight_position, Heat.id)
        .all()
    )

    last_seen: dict[str, int] = {}
    conflicts: list[tuple[str, str]] = []
    for position, (_heat_id, stand_type) in enumerate(rows):
        for other in _CONFLICTING_STANDS.get(stand_type, ()):
            previous = last_seen.get(other)
            if previous is not None and (position - previous) < _STAND_CONFLICT_GAP:
                conflicts.append((_stand_label(other), _stand_label(stand_type)))
        if stand_type:
            last_seen[stand_type] = position
    return conflicts


# ---------------------------------------------------------------------------
# Overall summary
# ---------------------------------------------------------------------------


def _overall(
    friday: DayStatus,
    saturday: DayStatus,
    flight_count: int,
    warnings: list[Warning_],
) -> tuple[str, str]:
    """One-liner for the card header + its Bootstrap severity class."""
    if any(w.get("severity") == "danger" for w in warnings):
        return "Schedule has conflicts — fix before race day", "danger"

    any_college_configured = friday["events_configured"] > 0
    any_pro_configured = saturday["events_configured"] > 0

    if not any_college_configured and not any_pro_configured:
        return "No events configured yet", "info"

    friday_ready = (
        friday["events_configured"] == 0
        or friday["events_with_heats"] == friday["events_configured"]
    )
    saturday_ready = saturday["events_configured"] == 0 or (
        saturday["events_with_heats"] == saturday["events_configured"]
        and flight_count > 0
    )

    if friday_ready and saturday_ready:
        return "Schedule ready", "success"
    if warnings:
        return "Schedule in progress — action needed", "warning"
    return "Schedule in progress", "info"
