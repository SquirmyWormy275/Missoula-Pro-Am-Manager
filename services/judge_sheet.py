"""
Judge Sheet data builder.

This module produces a structured, printable view of an event's heats for judges
to record scores on paper or clipboard.  It is OUTPUT-ONLY: it reads the event,
its heats, and the competitors inside each heat — it writes nothing.

The shape returned by `get_event_heats_for_judging()` is deliberately flat and
serializable so that a template can walk it without reaching back into the ORM
for each cell.

Run count rules (derived from Event flags, not a separate column):
    requires_triple_runs → 3     (axe throw, partnered axe throw)
    requires_dual_runs   → 2     (speed climb, chokerman, caber toss)
    else                 → 1     (everything else — single run)

Scoring type mapping (also flattened from the raw Event.scoring_type):
    'time', 'distance' → 'timed'   (two judge stopwatches / measuring tapes)
    everything else    → 'scored'  (two judges assigning points / hits)
"""

from __future__ import annotations

from typing import TypedDict

from models.competitor import CollegeCompetitor, ProCompetitor
from models.event import Event
from models.heat import Heat


class JudgeSheetCompetitor(TypedDict):
    name: str
    team_code: str | None


class JudgeSheetHeat(TypedDict):
    heat_number: int
    competitors: list[JudgeSheetCompetitor]


class JudgeSheetData(TypedDict):
    event_name: str
    event_type: str
    num_runs: int
    scoring_type: str
    heats: list[JudgeSheetHeat]


def _num_runs_for_event(event: Event) -> int:
    if event.requires_triple_runs:
        return 3
    if event.requires_dual_runs:
        return 2
    return 1


def _sheet_scoring_type(event: Event) -> str:
    # 'time' and 'distance' both use two timers/tapes; everything else
    # (hits, score, bracket) is two-judge scored.  This flattening gives the
    # template a single binary decision for column headers.
    return "timed" if event.scoring_type in ("time", "distance") else "scored"


def get_event_heats_for_judging(event_id: int) -> JudgeSheetData | None:
    """Return a structured dict describing heats + competitors for judging.

    Returns None if the event does not exist.  Returns an empty heats list when
    the event exists but has no heats (caller handles the "no heats" case, e.g.
    the bulk-PDF route skips events without heats instead of erroring).
    """
    event = Event.query.get(event_id)
    if event is None:
        return None

    data: JudgeSheetData = {
        "event_name": event.display_name,
        "event_type": (event.stand_type or event.name),
        "num_runs": _num_runs_for_event(event),
        "scoring_type": _sheet_scoring_type(event),
        "heats": [],
    }

    # For dual-run events we only print run 1 — the judge sheet is a recording
    # layout with columns for each run, not two separate sheets.  Skipping
    # run 2 avoids duplicating the same competitors on a second page.
    heats = (
        Heat.query.filter_by(event_id=event.id)
        .filter(Heat.run_number != 2)
        .order_by(Heat.heat_number.asc())
        .all()
    )
    if not heats:
        return data

    # Collect all competitor IDs across heats so we can do a single batched
    # query per competitor type — avoids N+1 when an event has many heats.
    all_ids: list[int] = []
    for heat in heats:
        all_ids.extend(int(cid) for cid in heat.get_competitors())

    if event.event_type == "college":
        rows = (
            CollegeCompetitor.query.filter(CollegeCompetitor.id.in_(all_ids)).all()
            if all_ids
            else []
        )
        comp_lookup = {c.id: c for c in rows}
    else:
        rows = (
            ProCompetitor.query.filter(ProCompetitor.id.in_(all_ids)).all()
            if all_ids
            else []
        )
        comp_lookup = {c.id: c for c in rows}

    for heat in heats:
        heat_row: JudgeSheetHeat = {
            "heat_number": heat.heat_number,
            "competitors": [],
        }
        for cid in heat.get_competitors():
            comp = comp_lookup.get(int(cid))
            if not comp:
                continue  # competitor was deleted after heat was built
            team_code: str | None = None
            if event.event_type == "college" and getattr(comp, "team", None):
                team_code = comp.team.team_code
            heat_row["competitors"].append(
                {
                    "name": comp.name,
                    "team_code": team_code,
                }
            )
        data["heats"].append(heat_row)

    return data
