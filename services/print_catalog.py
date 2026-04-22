"""Print Catalog — registry of every printable document in the app.

The catalog drives the Print Hub page and instruments the existing print
routes with a staleness tracker via the ``@record_print`` decorator.

Two kinds of entries:
  - FIXED: one row in the Hub (e.g. Heat Sheets, ALA Report).
  - DYNAMIC: one row PER entity (currently: Event Results, one per Event).

Each entry declares:
  - ``key``: stable identifier persisted in PrintTracker.doc_key.
  - ``label``: human-readable name for the Hub row.
  - ``section``: which section header this row appears under
    (Setup / Run Show / Results / Compliance).
  - ``route_endpoint``: Flask endpoint name used by url_for().
  - ``status_fn(tournament, entity=None) -> PrintDocStatus``:
    cheap row-by-row configuration check.
  - ``fingerprint_fn(tournament, entity=None) -> str``:
    short sha1 of the underlying data — compared to the last-printed
    fingerprint to decide FRESH vs STALE.
  - DYNAMIC entries additionally have ``enumerate_fn(tournament) ->
    list[entity]`` to yield per-row entities.

Staleness rule: a fingerprint mismatch marks a row STALE. False-positive
stale (judge reprints unnecessarily) is strictly better than false-negative
fresh (judge trusts a stale printout) on race day.
"""

from __future__ import annotations

import functools
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable, Optional

from flask import g

from database import db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrintDocStatus:
    """Output of a status_fn — is this document ready to print right now?"""

    configured: bool
    reason: Optional[str] = None


@dataclass(frozen=True)
class PrintDoc:
    """A fixed Print Hub row (one-to-one with an existing print route)."""

    key: str
    label: str
    section: str
    route_endpoint: str
    status_fn: Callable
    fingerprint_fn: Callable
    description: str = ""
    # Optional extra url_for kwargs (e.g., {'event_id': None} for dynamic docs)
    route_kwargs: tuple = field(default_factory=tuple)
    dynamic: bool = False
    enumerate_fn: Optional[Callable] = None


# ---------------------------------------------------------------------------
# Status + fingerprint helpers
# ---------------------------------------------------------------------------


def _sha1(parts: Iterable) -> str:
    """Deterministic short sha1 fingerprint across app restarts / redeploys.

    hash(frozenset(...)) is NOT stable across Python runs (PYTHONHASHSEED).
    sha1 is. 16 hex chars (~1e19 combinations) is more than sufficient for
    a 16-doc catalog per tournament.
    """
    payload = "|".join(str(p) for p in parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _ok() -> PrintDocStatus:
    return PrintDocStatus(configured=True, reason=None)


def _not_configured(reason: str) -> PrintDocStatus:
    return PrintDocStatus(configured=False, reason=reason)


def _safe_call(fn, *args, **kwargs):
    """Wrap a status/fingerprint fn so a query error → safe default, not 500."""
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.exception("print_catalog fn raised — defaulting to not-configured")
        return None


# ---------------------------------------------------------------------------
# Per-doc status + fingerprint implementations
# ---------------------------------------------------------------------------
# Pattern: status_fn returns PrintDocStatus(configured, reason); fingerprint_fn
# returns a short deterministic sha1 derived from the same data the print
# route renders. fingerprint_fn must reflect EVERY column that changes the
# rendered output — miss one and staleness silently under-reports.

# --- Heat Sheets ----------------------------------------------------------


def _status_heat_sheets(tournament, entity=None):
    from models import Event, Heat

    count = (
        Heat.query.join(Event, Heat.event_id == Event.id)
        .filter(Event.tournament_id == tournament.id)
        .count()
    )
    if count == 0:
        return _not_configured("No heats generated yet.")
    return _ok()


def _fp_heat_sheets(tournament, entity=None):
    from models import Event, Heat
    from models.competitor import ProCompetitor

    heats = (
        Heat.query.join(Event, Heat.event_id == Event.id)
        .filter(Event.tournament_id == tournament.id)
        .order_by(Heat.id)
        .all()
    )
    scratched = ProCompetitor.query.filter_by(
        tournament_id=tournament.id, status="scratched"
    ).count()
    parts = [
        tournament.updated_at.isoformat() if tournament.updated_at else "",
        f"scratched={scratched}",
    ]
    for h in heats:
        parts.append(
            f'{h.id}:{h.status}:{h.flight_id}:{h.competitors or ""}:{h.stand_assignments or ""}'
        )
    return _sha1(parts)


# --- Day Schedule ---------------------------------------------------------


def _status_day_schedule(tournament, entity=None):
    event_count = tournament.events.count()
    if event_count == 0:
        return _not_configured("No events configured yet.")
    from models import Event, Heat

    heat_count = (
        Heat.query.join(Event).filter(Event.tournament_id == tournament.id).count()
    )
    if heat_count == 0:
        return _not_configured("No heats generated yet.")
    return _ok()


def _fp_day_schedule(tournament, entity=None):
    from models import Event, Heat

    events = tournament.events.order_by(Event.id).all()
    heats = (
        Heat.query.join(Event)
        .filter(Event.tournament_id == tournament.id)
        .order_by(Heat.id)
        .all()
    )
    parts = [f"{e.id}:{e.name}:{e.status}:{e.gender}" for e in events]
    parts += [f"{h.id}:{h.heat_number}:{h.run_number}:{h.status}" for h in heats]
    return _sha1(parts)


# --- Friday Night Feature (print + PDF share fingerprint) ----------------


def _status_fnf(tournament, entity=None):
    cfg = tournament.get_schedule_config()
    fnf_ids = cfg.get("friday_pro_event_ids") or []
    if not fnf_ids:
        return _not_configured("No Friday Night events selected.")
    return _ok()


def _fp_fnf(tournament, entity=None):
    cfg = tournament.get_schedule_config()
    fnf_ids = sorted(cfg.get("friday_pro_event_ids") or [])
    from models import Event, Heat

    heats = (
        Heat.query.join(Event)
        .filter(
            Event.tournament_id == tournament.id,
            Event.id.in_(fnf_ids) if fnf_ids else False,
        )
        .order_by(Heat.id)
        .all()
    )
    parts = [",".join(str(i) for i in fnf_ids)]
    parts += [
        f'{h.id}:{h.heat_number}:{h.competitors or ""}:{h.stand_assignments or ""}'
        for h in heats
    ]
    return _sha1(parts)


# --- Birling (blank bracket + seeded all) --------------------------------


def _status_birling_seeded(tournament, entity=None):
    birling_events = [
        e for e in tournament.events if "birling" in (e.name or "").lower()
    ]
    if not birling_events:
        return _not_configured("No birling event configured.")
    return _ok()


def _fp_birling_seeded(tournament, entity=None):
    birling_events = [
        e for e in tournament.events if "birling" in (e.name or "").lower()
    ]
    parts = []
    for e in sorted(birling_events, key=lambda x: x.id):
        parts.append(f'{e.id}:{e.gender}:{e.status}:{e.payouts or ""}')
    return _sha1(parts)


# --- Judge Sheets (per event + all) ---------------------------------------


def _status_judge_sheets(tournament, entity=None):
    from models import Event, Heat

    count = Heat.query.join(Event).filter(Event.tournament_id == tournament.id).count()
    if count == 0:
        return _not_configured("No heats generated yet.")
    return _ok()


def _fp_judge_sheets(tournament, entity=None):
    # Same shape as heat-sheets — judge sheets are derived from the same heats.
    return _fp_heat_sheets(tournament)


# --- College standings ----------------------------------------------------


def _status_college_standings(tournament, entity=None):
    from models import Event, EventResult

    completed = (
        EventResult.query.join(Event)
        .filter(
            Event.tournament_id == tournament.id,
            Event.event_type == "college",
            EventResult.status == "completed",
        )
        .count()
    )
    if completed == 0:
        return _not_configured("No college results yet.")
    return _ok()


def _fp_college_standings(tournament, entity=None):
    from models import Event, EventResult

    results = (
        EventResult.query.join(Event)
        .filter(
            Event.tournament_id == tournament.id,
            Event.event_type == "college",
        )
        .order_by(EventResult.id)
        .all()
    )
    parts = [
        f"{r.id}:{r.competitor_id}:{r.final_position}:{r.points_awarded}:{r.status}"
        for r in results
    ]
    return _sha1(parts)


# --- All Results ----------------------------------------------------------


def _status_all_results(tournament, entity=None):
    from models import Event, EventResult

    count = (
        EventResult.query.join(Event)
        .filter(
            Event.tournament_id == tournament.id,
            EventResult.status == "completed",
        )
        .count()
    )
    if count == 0:
        return _not_configured("No finalized results yet.")
    return _ok()


def _fp_all_results(tournament, entity=None):
    from models import Event, EventResult

    results = (
        EventResult.query.join(Event)
        .filter(
            Event.tournament_id == tournament.id,
        )
        .order_by(EventResult.id)
        .all()
    )
    parts = [
        f"{r.id}:{r.competitor_id}:{r.final_position}:{r.result_value}:{r.status}"
        for r in results
    ]
    return _sha1(parts)


# --- Pro Payout Summary ---------------------------------------------------


def _status_pro_payouts(tournament, entity=None):
    earners = [c for c in tournament.pro_competitors if (c.total_earnings or 0) > 0]
    if not earners:
        return _not_configured("No pro earnings recorded yet.")
    return _ok()


def _fp_pro_payouts(tournament, entity=None):
    parts = []
    for c in sorted(tournament.pro_competitors, key=lambda x: x.id):
        parts.append(f"{c.id}:{c.total_earnings or 0}:{int(c.payout_settled or False)}")
    return _sha1(parts)


# --- ALA Report -----------------------------------------------------------


def _status_ala(tournament, entity=None):
    count = tournament.pro_competitors.filter_by(status="active").count()
    if count == 0:
        return _not_configured("No pro competitors registered.")
    return _ok()


def _fp_ala(tournament, entity=None):
    pros = sorted(tournament.pro_competitors.all(), key=lambda p: p.id)
    parts = [f"{p.id}:{p.name}:{int(bool(p.is_ala_member))}" for p in pros]
    return _sha1(parts)


# --- Gear Sharing Report --------------------------------------------------


def _status_gear_sharing(tournament, entity=None):
    pros = tournament.pro_competitors.filter_by(status="active").all()
    with_gear = [p for p in pros if (p.gear_sharing or "").strip() not in ("", "{}")]
    if not with_gear:
        return _not_configured("No gear-sharing entries recorded yet.")
    return _ok()


def _fp_gear_sharing(tournament, entity=None):
    pros = sorted(tournament.pro_competitors.all(), key=lambda p: p.id)
    parts = [f'{p.id}:{p.gear_sharing or ""}' for p in pros]
    return _sha1(parts)


# --- Woodboss Report ------------------------------------------------------


def _status_woodboss(tournament, entity=None):
    count = tournament.wood_configs.count()
    if count == 0:
        return _not_configured("No wood species / sizes configured.")
    return _ok()


def _fp_woodboss(tournament, entity=None):
    rows = sorted(tournament.wood_configs.all(), key=lambda w: w.id)
    parts = [
        f"{w.id}:{w.config_key}:{w.species}:{w.size_value}:{w.size_unit}:{w.count_override}"
        for w in rows
    ]
    return _sha1(parts)


# --- Pro Checkout Roster (NEW) -------------------------------------------


def _status_pro_checkout(tournament, entity=None):
    count = tournament.pro_competitors.filter_by(status="active").count()
    if count == 0:
        return _not_configured("No pro competitors registered.")
    return _ok()


def _fp_pro_checkout(tournament, entity=None):
    pros = sorted(
        tournament.pro_competitors.filter_by(status="active").all(),
        key=lambda p: p.id,
    )
    parts = [f'{p.id}:{p.name}:{p.events_entered or ""}' for p in pros]
    return _sha1(parts)


# --- Event Results (DYNAMIC — one row per event) -------------------------


def _status_event_results(tournament, entity=None):
    # entity is an Event instance (dynamic doc).
    if entity is None:
        return _not_configured("Missing event reference.")
    if not entity.is_finalized:
        return _not_configured("Event not finalized yet.")
    return _ok()


def _fp_event_results(tournament, entity=None):
    if entity is None:
        return _sha1(["missing-entity"])
    from models import EventResult

    results = (
        EventResult.query.filter_by(event_id=entity.id).order_by(EventResult.id).all()
    )
    parts = [f"ev:{entity.id}:{int(entity.is_finalized)}:{entity.status}"]
    parts += [
        f"{r.id}:{r.competitor_id}:{r.final_position}:{r.result_value}:{r.points_awarded}"
        for r in results
    ]
    return _sha1(parts)


def _enum_event_results(tournament):
    """Order: Friday (college) first, then Saturday (pro); alphabetical within day."""
    from models import Event

    return tournament.events.order_by(Event.event_type, Event.name, Event.gender).all()


# ---------------------------------------------------------------------------
# Catalog registry
# ---------------------------------------------------------------------------

# Section labels — match the sidebar taxonomy so judges can map Hub rows to
# the place in the app where the underlying data lives.
SECTION_SETUP = "Setup"
SECTION_RUN_SHOW = "Run Show"
SECTION_RESULTS = "Results"
SECTION_COMPLIANCE = "Compliance"

# Ordered — this is the order rows appear in the Hub.
PRINT_DOCUMENTS: list[PrintDoc] = [
    # --- Setup -----------------------------------------------------------
    PrintDoc(
        key="woodboss_report",
        label="Woodboss Report",
        section=SECTION_SETUP,
        route_endpoint="woodboss.report_print",
        status_fn=_status_woodboss,
        fingerprint_fn=_fp_woodboss,
        description="Wood block and saw-log inventory print.",
        route_kwargs=("tid",),
    ),
    PrintDoc(
        key="day_schedule",
        label="Day Schedule",
        section=SECTION_SETUP,
        route_endpoint="scheduling.day_schedule_print",
        status_fn=_status_day_schedule,
        fingerprint_fn=_fp_day_schedule,
        description="College day heat-by-heat schedule.",
    ),
    PrintDoc(
        key="fnf_print",
        label="Friday Night Feature",
        section=SECTION_SETUP,
        route_endpoint="scheduling.friday_feature_print",
        status_fn=_status_fnf,
        fingerprint_fn=_fp_fnf,
        description="FNF heat-by-heat schedule (HTML print view).",
    ),
    PrintDoc(
        key="fnf_pdf",
        label="Friday Night Feature (PDF)",
        section=SECTION_SETUP,
        route_endpoint="scheduling.friday_feature_pdf",
        status_fn=_status_fnf,
        fingerprint_fn=_fp_fnf,
        description="FNF schedule as PDF download.",
    ),
    # --- Run Show --------------------------------------------------------
    PrintDoc(
        key="heat_sheets",
        label="Heat Sheets",
        section=SECTION_RUN_SHOW,
        route_endpoint="scheduling.heat_sheets",
        status_fn=_status_heat_sheets,
        fingerprint_fn=_fp_heat_sheets,
        description="Master heat sheet print page (per-flight tabs).",
    ),
    PrintDoc(
        key="judge_sheet_all",
        label="Judge Sheets (All Events)",
        section=SECTION_RUN_SHOW,
        route_endpoint="scoring.judge_sheets_all",
        status_fn=_status_judge_sheets,
        fingerprint_fn=_fp_judge_sheets,
        description="Judge sheets for every event, bundled.",
    ),
    PrintDoc(
        key="birling_blank",
        label="Birling Bracket (Blank)",
        section=SECTION_RUN_SHOW,
        route_endpoint="scheduling.birling_print_all",
        status_fn=_status_birling_seeded,
        fingerprint_fn=_fp_birling_seeded,
        description="Blank birling bracket worksheet.",
    ),
    PrintDoc(
        key="gear_sharing_print",
        label="Gear Sharing Roster",
        section=SECTION_RUN_SHOW,
        route_endpoint="registration.pro_gear_print",
        status_fn=_status_gear_sharing,
        fingerprint_fn=_fp_gear_sharing,
        description="Pro gear-sharing audit printout.",
    ),
    PrintDoc(
        key="pro_checkout",
        label="Pro Saturday Checkout Roster",
        section=SECTION_RUN_SHOW,
        route_endpoint="scheduling.pro_checkout_roster_print",
        status_fn=_status_pro_checkout,
        fingerprint_fn=_fp_pro_checkout,
        description="Printed check-in sheet for Saturday morning.",
    ),
    # --- Results ---------------------------------------------------------
    PrintDoc(
        key="college_standings",
        label="College Standings",
        section=SECTION_RESULTS,
        route_endpoint="reporting.college_standings_print",
        status_fn=_status_college_standings,
        fingerprint_fn=_fp_college_standings,
        description="Team + Bull/Belle standings.",
    ),
    PrintDoc(
        key="all_results",
        label="All Results",
        section=SECTION_RESULTS,
        route_endpoint="reporting.all_results_print",
        status_fn=_status_all_results,
        fingerprint_fn=_fp_all_results,
        description="Combined results across every event.",
    ),
    PrintDoc(
        key="pro_payouts",
        label="Pro Payout Summary",
        section=SECTION_RESULTS,
        route_endpoint="reporting.pro_payout_summary_print",
        status_fn=_status_pro_payouts,
        fingerprint_fn=_fp_pro_payouts,
        description="Pro competitor earnings + settlement tracking.",
    ),
    PrintDoc(
        key="event_results",
        label="Event Results",
        section=SECTION_RESULTS,
        route_endpoint="reporting.event_results_print",
        status_fn=_status_event_results,
        fingerprint_fn=_fp_event_results,
        description="Per-event ranked list (one row per event).",
        dynamic=True,
        enumerate_fn=_enum_event_results,
        route_kwargs=("event_id",),
    ),
    # --- Compliance ------------------------------------------------------
    PrintDoc(
        key="ala_report",
        label="ALA Report",
        section=SECTION_COMPLIANCE,
        route_endpoint="reporting.ala_membership_report_pdf",
        status_fn=_status_ala,
        fingerprint_fn=_fp_ala,
        description="American Lumberjack Association membership report.",
    ),
]


# Build a key → PrintDoc index for O(1) lookup.
_DOCS_BY_KEY: dict[str, PrintDoc] = {d.key: d for d in PRINT_DOCUMENTS}


def get_doc(key: str) -> Optional[PrintDoc]:
    """Return the PrintDoc with the given key, or None if unknown."""
    return _DOCS_BY_KEY.get(key)


# ---------------------------------------------------------------------------
# Section ordering for the Hub
# ---------------------------------------------------------------------------

SECTIONS_ORDER = [SECTION_SETUP, SECTION_RUN_SHOW, SECTION_RESULTS, SECTION_COMPLIANCE]


# ---------------------------------------------------------------------------
# Tracker helpers
# ---------------------------------------------------------------------------


def upsert_tracker(
    tournament_id: int,
    doc_key: str,
    entity_id: Optional[int],
    fingerprint: str,
    user_id: Optional[int],
) -> None:
    """Insert or update the PrintTracker row for this (tournament, doc, entity).

    SQLite + PostgreSQL both treat NULL as distinct in UNIQUE constraints, so
    we query-then-insert/update rather than rely on ON CONFLICT.
    """
    from models import PrintTracker

    q = PrintTracker.query.filter_by(
        tournament_id=tournament_id,
        doc_key=doc_key,
    )
    if entity_id is None:
        q = q.filter(PrintTracker.entity_id.is_(None))
    else:
        q = q.filter(PrintTracker.entity_id == entity_id)
    row = q.first()

    now = datetime.utcnow()
    if row is None:
        row = PrintTracker(
            tournament_id=tournament_id,
            doc_key=doc_key,
            entity_id=entity_id,
            last_printed_at=now,
            last_printed_fingerprint=fingerprint,
            last_printed_by_user_id=user_id,
        )
        db.session.add(row)
    else:
        row.last_printed_at = now
        row.last_printed_fingerprint = fingerprint
        row.last_printed_by_user_id = user_id
    db.session.commit()


def load_trackers_for_tournament(tournament_id: int) -> dict:
    """Return {(doc_key, entity_id_or_None): PrintTracker} for one tournament."""
    from models import PrintTracker

    rows = PrintTracker.query.filter_by(tournament_id=tournament_id).all()
    return {(r.doc_key, r.entity_id): r for r in rows}


# ---------------------------------------------------------------------------
# @record_print decorator
# ---------------------------------------------------------------------------


def record_print(doc_key: str, entity_id_kwarg: Optional[str] = None):
    """Wrap an existing print route so each successful hit updates PrintTracker.

    Rules:
      1. Tracker update runs AFTER the view returns. If the view raises,
         no tracker row is written.
      2. Tracker failures are swallowed and logged — the print itself MUST
         NOT be blocked by an audit bookkeeping error.
      3. Tournament id is read from kwargs['tournament_id'] or kwargs['tid'].
      4. If entity_id_kwarg is set (e.g. 'event_id'), that kwarg identifies
         the per-row entity for dynamic docs.
    """

    def wrap(view):
        @functools.wraps(view)
        def inner(*args, **kwargs):
            response = view(*args, **kwargs)
            try:
                _write_tracker_from_request(doc_key, entity_id_kwarg, kwargs)
            except Exception:
                logger.exception(
                    "PrintTracker upsert failed (non-fatal) doc=%s kwargs=%s",
                    doc_key,
                    kwargs,
                )
            return response

        return inner

    return wrap


def _write_tracker_from_request(
    doc_key: str,
    entity_id_kwarg: Optional[str],
    view_kwargs: dict,
) -> None:
    doc = _DOCS_BY_KEY.get(doc_key)
    if doc is None:
        logger.warning("record_print: unknown doc_key %s", doc_key)
        return

    tid = view_kwargs.get("tournament_id") or view_kwargs.get("tid")
    if tid is None:
        logger.warning("record_print: no tournament id in kwargs for %s", doc_key)
        return

    from models import Tournament

    tournament = db.session.get(Tournament, tid)
    if tournament is None:
        return

    entity_id = None
    entity_obj = None
    if entity_id_kwarg and entity_id_kwarg in view_kwargs:
        entity_id = view_kwargs[entity_id_kwarg]
        if doc.dynamic and entity_id is not None:
            from models import Event

            if doc.key == "event_results":
                entity_obj = db.session.get(Event, entity_id)

    fingerprint = _safe_call(doc.fingerprint_fn, tournament, entity_obj) or _sha1(
        ["empty"]
    )

    user_id = _current_user_id()
    upsert_tracker(
        tournament_id=int(tid),
        doc_key=doc_key,
        entity_id=int(entity_id) if entity_id is not None else None,
        fingerprint=fingerprint,
        user_id=user_id,
    )


def _current_user_id() -> Optional[int]:
    """Return the current authenticated user id, or None for anonymous / no Flask-Login."""
    try:
        from flask_login import current_user

        if current_user and getattr(current_user, "is_authenticated", False):
            return int(current_user.id)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Hub row builder
# ---------------------------------------------------------------------------


@dataclass
class HubRow:
    """One rendered row for the Print Hub page."""

    doc: PrintDoc
    entity: Optional[object]  # Event (dynamic) or None (fixed)
    status: PrintDocStatus
    current_fingerprint: Optional[str]
    last_printed_at: Optional[datetime]
    last_printed_by: Optional[str]  # display name of the user, or None
    stale: bool
    never_printed: bool

    @property
    def url_kwargs(self) -> dict:
        out = {}
        if self.entity is not None:
            out["event_id"] = self.entity.id
        return out

    @property
    def label(self) -> str:
        if self.entity is not None:
            # Dynamic row — append the event display name.
            label = getattr(self.entity, "display_name", None) or self.entity.name
            return f"{self.doc.label} — {label}"
        return self.doc.label

    @property
    def row_key(self) -> str:
        """Stable DOM id / form key for this row."""
        if self.entity is not None:
            return f"{self.doc.key}:{self.entity.id}"
        return self.doc.key


def build_hub_rows(tournament) -> list[HubRow]:
    """Return all Hub rows for the tournament, fixed + dynamic, in display order.

    Computed per-request. Per-request cache via flask.g prevents recomputation
    if the hub page renders twice (e.g., during tests).
    """
    cache_key = f"_hub_rows_{tournament.id}"
    cached = getattr(g, cache_key, None) if g else None
    if cached is not None:
        return cached

    trackers = load_trackers_for_tournament(tournament.id)
    user_names = _load_user_names(trackers)

    rows: list[HubRow] = []
    for doc in PRINT_DOCUMENTS:
        if doc.dynamic:
            entities = _safe_call(doc.enumerate_fn, tournament) or []
            for entity in entities:
                rows.append(_build_row(doc, entity, tournament, trackers, user_names))
        else:
            rows.append(_build_row(doc, None, tournament, trackers, user_names))

    try:
        setattr(g, cache_key, rows)
    except Exception:
        pass
    return rows


def _build_row(doc, entity, tournament, trackers, user_names) -> HubRow:
    status = _safe_call(doc.status_fn, tournament, entity) or PrintDocStatus(
        configured=False, reason="Status check failed."
    )

    tracker = trackers.get((doc.key, entity.id if entity is not None else None))
    current_fp = None
    stale = False
    never_printed = tracker is None

    if status.configured:
        current_fp = _safe_call(doc.fingerprint_fn, tournament, entity)
        if tracker is not None and current_fp is not None:
            stale = tracker.last_printed_fingerprint != current_fp

    last_user = None
    if tracker and tracker.last_printed_by_user_id:
        last_user = user_names.get(tracker.last_printed_by_user_id)

    return HubRow(
        doc=doc,
        entity=entity,
        status=status,
        current_fingerprint=current_fp,
        last_printed_at=tracker.last_printed_at if tracker else None,
        last_printed_by=last_user,
        stale=stale,
        never_printed=never_printed,
    )


def _load_user_names(trackers: dict) -> dict:
    """Batch-load usernames for display in the 'last printed by' column."""
    ids = {
        t.last_printed_by_user_id
        for t in trackers.values()
        if t.last_printed_by_user_id
    }
    if not ids:
        return {}
    try:
        from models import User

        users = User.query.filter(User.id.in_(ids)).all()
        return {u.id: (getattr(u, "full_name", None) or u.username) for u in users}
    except Exception:
        return {}
