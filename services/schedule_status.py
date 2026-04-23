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
    competitors_placed: int
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

    friday = _day_status(
        tournament_id=tournament.id,
        events=college_events,
        heats_by_event=heats_by_event,
        detail_endpoint="scheduling.day_schedule",
    )
    saturday = _day_status(
        tournament_id=tournament.id,
        events=pro_events,
        heats_by_event=heats_by_event,
        detail_endpoint="scheduling.flight_list",
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
) -> DayStatus:
    heats_total = 0
    events_with_heats = 0
    competitor_ids: set[tuple[str, int]] = set()

    for ev in events:
        ev_heats = heats_by_event.get(ev.id, [])
        if ev_heats:
            events_with_heats += 1
            heats_total += len(ev_heats)
            for h in ev_heats:
                for cid in h.get_competitors():
                    competitor_ids.add((ev.event_type, int(cid)))

    return {
        "events_configured": len(events),
        "events_with_heats": events_with_heats,
        "heats_total": heats_total,
        "competitors_placed": len(competitor_ids),
        "detail_link": url_for(detail_endpoint, tournament_id=tournament_id),
        "detail_label": (
            "Day schedule" if detail_endpoint.endswith("day_schedule") else "Flights"
        ),
    }


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

    # --- 4. Cookie Stack + Standing Block simultaneous scheduling ---
    #
    # The flight builder already prevents this at build time, but surface
    # any pre-existing conflict in already-built flights so they get
    # cleaned up.
    cookie_block_conflicts = _count_cookie_standing_simultaneous(tournament.id)
    if cookie_block_conflicts:
        warnings.append(
            {
                "severity": "danger",
                "title": f"{cookie_block_conflicts} flight slot(s) schedule Cookie Stack and Standing Block simultaneously",
                "detail": "These events share physical stands and must not run at the same time. Rebuild flights to resolve.",
                "link": url_for("scheduling.event_list", tournament_id=tid),
                "link_label": "Rebuild flights",
                "submit_action": "rebuild_flights",
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


def _count_cookie_standing_simultaneous(tournament_id: int) -> int:
    """Count flight slots that schedule Cookie Stack and Standing Block at the same time."""
    flight_heats = (
        db.session.query(Heat.flight_id, Heat.flight_position, Event.stand_type)
        .join(Event, Event.id == Heat.event_id)
        .filter(
            Event.tournament_id == tournament_id,
            Heat.flight_id.isnot(None),
            Event.stand_type.in_(("cookie_stack", "standing_block")),
        )
        .all()
    )
    slot_stands: dict[tuple[int, int | None], set[str]] = defaultdict(set)
    for flight_id, pos, stand_type in flight_heats:
        slot_stands[(flight_id, pos)].add(stand_type)
    return sum(
        1
        for stands in slot_stands.values()
        if "cookie_stack" in stands and "standing_block" in stands
    )


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
