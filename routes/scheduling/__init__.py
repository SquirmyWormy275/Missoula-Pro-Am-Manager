"""
Scheduling routes package — event setup, heat generation, flight management.

The scheduling blueprint is defined here and registered against sub-modules that
each contain a logical slice of the original monolithic scheduling.py file.

Sub-module layout:
  events.py          — event_list, setup_events, day_schedule, apply_saturday_priority
  heats.py           — event_heats, generate_heats, generate_college_heats, move_competitor_between_heats,
                       heat_sync_check, heat_sync_fix
  flights.py         — flight_list, build_flights, start_flight, complete_flight, reorder_flight_heats
  heat_sheets.py     — heat_sheets, day_schedule_print
  friday_feature.py  — friday_feature
  show_day.py        — show_day
  ability_rankings.py — ability_rankings
  preflight.py       — preflight_check, preflight_json, generate_async, generation_job_status
  assign_marks.py    — assign_marks (handicap start-mark assignment via STRATHMARK)
  birling.py         — birling_manage, birling_generate, birling_record_match, birling_reset, birling_finalize
"""
import json
import re

from flask import Blueprint

import config
from config import LIST_ONLY_EVENT_NAMES
from database import db
from models import Event, Flight, Heat, HeatAssignment, Tournament
from models.competitor import CollegeCompetitor, ProCompetitor

scheduling_bp = Blueprint('scheduling', __name__)


# ---------------------------------------------------------------------------
# Shared utility helpers — used by two or more sub-modules.
# All private (underscore prefix) helpers shared across sub-modules live here
# so sub-modules can import them without circular-import concerns.
# ---------------------------------------------------------------------------

def _normalize_name(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', str(value or '').lower())


def _normalize_person_name(value: str) -> str:
    return str(value or '').strip().lower()


def _is_list_only_event(event: Event) -> bool:
    return event.event_type == 'college' and _normalize_name(event.name) in LIST_ONLY_EVENT_NAMES


def _max_per_heat(event: Event) -> int:
    """Authoritative max competitors per heat for an event.

    Resolution order: event.max_stands → config.STAND_CONFIGS → hard default 4.
    Matches the logic in services/heat_generator.py line 106.
    """
    if event.max_stands is not None:
        return event.max_stands
    stand_config = config.STAND_CONFIGS.get(event.stand_type or '', {})
    return stand_config.get('total', 4)


def _load_competitor_lookup(event: Event, competitor_ids: list) -> dict:
    ids = sorted(set(int(cid) for cid in competitor_ids if cid is not None))
    if not ids:
        return {}
    if event.event_type == 'college':
        competitors = CollegeCompetitor.query.filter(CollegeCompetitor.id.in_(ids)).all()
    else:
        competitors = ProCompetitor.query.filter(ProCompetitor.id.in_(ids)).all()
    return {c.id: c for c in competitors}


def _signed_up_competitors(event: Event) -> list:
    if event.event_type == 'college':
        all_comps = CollegeCompetitor.query.filter_by(
            tournament_id=event.tournament_id,
            status='active'
        ).all()
    else:
        all_comps = ProCompetitor.query.filter_by(
            tournament_id=event.tournament_id,
            status='active'
        ).all()

    signed = []
    for comp in all_comps:
        entered = comp.get_events_entered() if hasattr(comp, 'get_events_entered') else []
        if _competitor_entered_event(event, entered):
            if event.gender and getattr(comp, 'gender', None) != event.gender:
                continue
            signed.append(comp)

    return sorted(signed, key=lambda c: c.name.lower())


def _competitor_entered_event(event: Event, entered_events: list) -> bool:
    entered = entered_events if isinstance(entered_events, list) else []
    target_id = str(event.id)
    target_name = _normalize_name(event.name)
    target_display_name = _normalize_name(event.display_name)
    aliases = {target_name, target_display_name}

    # Backward-compatible aliases for historic imports and form-label variants.
    if event.event_type == 'pro':
        if target_name == 'springboard':
            aliases.update({'springboardl', 'springboardr'})
        elif target_name in {'pro1board', '1boardspringboard'}:
            aliases.update({'intermediate1boardspringboard', 'pro1board', '1boardspringboard'})
        elif target_name == 'jackjillsawing':
            aliases.update({'jackjill', 'jackandjill'})
        elif target_name in {'poleclimb', 'speedclimb'}:
            aliases.update({'poleclimb', 'speedclimb'})
        elif target_name == 'partneredaxethrow':
            aliases.update({'partneredaxethrow', 'axethrow'})

    for raw in entered:
        value = str(raw).strip()
        if not value:
            continue
        if value == target_id:
            return True
        normalized = _normalize_name(value)
        if normalized in aliases:
            return True
    return False


def _resolve_partner_name(competitor, event: Event) -> str:
    partners = competitor.get_partners() if hasattr(competitor, 'get_partners') else {}
    if not isinstance(partners, dict):
        return ''
    candidates = [
        str(event.id),
        event.name,
        event.display_name,
        event.name.lower(),
        event.display_name.lower(),
    ]
    for key in candidates:
        value = partners.get(key)
        if str(value or '').strip():
            return str(value).strip()
    return ''


def _build_signup_rows(event: Event) -> list:
    competitors = _signed_up_competitors(event)
    if not event.is_partnered:
        return [c.name for c in competitors]

    rows = []
    used = set()
    by_name = {_normalize_person_name(c.name): c for c in competitors}
    for comp in competitors:
        if comp.id in used:
            continue
        partner_name = _resolve_partner_name(comp, event)
        partner = by_name.get(_normalize_person_name(partner_name)) if partner_name else None
        if partner and partner.id not in used:
            rows.append(f'{comp.name} + {partner.name}')
            used.add(comp.id)
            used.add(partner.id)
        else:
            rows.append(comp.name)
            used.add(comp.id)

    return rows


def _build_assignment_details(tournament: Tournament, events: list) -> dict:
    details = {}
    for event in events:
        if _is_list_only_event(event):
            signup_rows = _build_signup_rows(event)
            details[event.id] = {
                'mode': 'signup',
                'rows': signup_rows,
                'count': len(signup_rows),
            }
            continue

        heats = event.heats.order_by(Heat.heat_number, Heat.run_number).all()
        all_comp_ids = []
        for heat in heats:
            all_comp_ids.extend(heat.get_competitors())
        comp_lookup = _load_competitor_lookup(event, all_comp_ids)

        heat_rows = []
        for heat in heats:
            assignments = heat.get_stand_assignments()
            competitors = []
            for comp_id in heat.get_competitors():
                comp = comp_lookup.get(comp_id)
                competitors.append({
                    'name': comp.display_name if comp else f'Unknown ({comp_id})',
                    'stand': assignments.get(str(comp_id)),
                })
            heat_rows.append({
                'heat_number': heat.heat_number,
                'run_number': heat.run_number,
                'competitors': competitors,
            })

        details[event.id] = {
            'mode': 'heats',
            'rows': heat_rows,
            'count': len(heat_rows),
        }

    return details


def _generate_all_heats(tournament: Tournament, generate_event_heats_fn):
    """Generate heats for all configured events.

    Each event is wrapped in its own savepoint (begin_nested) so that a failure
    in one event is rolled back in isolation without corrupting the session state
    for subsequent events or for the flight-building step that follows.

    Per-event outcomes are surfaced to the operator via flash:
      - generated: count + nothing more (success, common case)
      - skipped (no entrants): each event named individually so the operator can
        click straight through to registration and add the missing competitors,
        rather than seeing a single "Skipped 13 without entrants" line that
        gives no path to resolution.
      - errors (anything else): each event named with the underlying error,
        plus a per-event ``error`` flash so they cannot be missed.
    """
    from flask import flash
    from markupsafe import Markup, escape

    from services.heat_generator import get_last_unpaired_partnered

    events = tournament.events.order_by(Event.event_type, Event.name, Event.gender).all()
    generated = 0
    skipped_events: list[tuple[Event, str]] = []
    failed_events: list[tuple[Event, str]] = []
    unpaired_by_event: list[tuple[Event, list[dict]]] = []

    for event in events:
        try:
            with db.session.begin_nested():   # savepoint per event — rollback on failure
                generate_event_heats_fn(event)
            generated += 1
            unpaired = get_last_unpaired_partnered(event.id)
            if unpaired:
                unpaired_by_event.append((event, unpaired))
        except Exception as exc:
            # begin_nested().__exit__ rolls back to the savepoint on exception.
            msg = str(exc)
            if 'No competitors entered' in msg:
                skipped_events.append((event, msg))
            else:
                failed_events.append((event, msg))
                flash(
                    f'Heat generation error for {event.display_name}: {msg}',
                    'error',
                )

    flash(f'Heats generated for {generated} event(s).', 'success')

    # Surface partnered-event entrants that were held back across the bulk run.
    # One aggregated flash with a Preflight link beats N per-event flashes that
    # bury the actionable list at the bottom of the screen.
    if unpaired_by_event:
        from flask import url_for
        total = sum(len(rows) for _, rows in unpaired_by_event)
        per_event_blurbs = []
        for ev, rows in unpaired_by_event:
            names = ', '.join(
                f"{r['comp_name']}{(' → \"' + r['partner_name'] + '\"') if r['partner_name'] else ''}"
                for r in rows[:3]
            )
            extra = f' (+{len(rows) - 3} more)' if len(rows) > 3 else ''
            per_event_blurbs.append(
                f'<strong>{escape(_event_label(ev))}</strong>: {escape(names)}{extra}'
            )
        link = url_for('scheduling.preflight_check', tournament_id=tournament.id)
        flash(
            Markup(
                f'HELD BACK: {total} partnered-event entrant(s) across '
                f'{len(unpaired_by_event)} event(s) have unresolved partners and '
                f'were NOT placed in heats — '
                + '; '.join(per_event_blurbs)
                + f'. <a href="{escape(link)}" class="alert-link">Open Preflight Check</a> '
                'to resolve, then click Generate again.'
            ),
            'warning',
        )

    if skipped_events:
        # Build clickable per-division summary so operators can jump straight
        # to the right registration page and fix the missing entries.
        from flask import url_for
        college_skipped = [e for e, _ in skipped_events if e.event_type == 'college']
        pro_skipped = [e for e, _ in skipped_events if e.event_type == 'pro']

        if pro_skipped:
            names = ', '.join(_event_label(e) for e in pro_skipped)
            link = url_for('registration.pro_registration', tournament_id=tournament.id)
            flash(
                Markup(
                    f'{len(pro_skipped)} pro event(s) skipped — no competitors entered: '
                    f'<strong>{escape(names)}</strong>. '
                    f'<a href="{escape(link)}" class="alert-link">Open pro registration</a> '
                    f'to add entries, then click Generate again.'
                ),
                'warning',
            )
        if college_skipped:
            names = ', '.join(_event_label(e) for e in college_skipped)
            link = url_for('registration.college_registration', tournament_id=tournament.id)
            flash(
                Markup(
                    f'{len(college_skipped)} college event(s) skipped — no competitors entered: '
                    f'<strong>{escape(names)}</strong>. '
                    f'<a href="{escape(link)}" class="alert-link">Open college registration</a> '
                    f'to add entries, then click Generate again.'
                ),
                'warning',
            )

    if failed_events:
        flash(
            f'Failed to generate heats for {len(failed_events)} event(s) — see errors above.',
            'error',
        )


def _event_label(event: Event) -> str:
    """Compact label for flash messages: ``"Underhand (M)"``."""
    base = event.display_name if hasattr(event, 'display_name') else event.name
    gender = (getattr(event, 'gender', None) or '').strip()
    if gender and f'({gender})' not in base:
        return f'{base} ({gender})'
    return base


def _build_pro_flights_if_possible(tournament: Tournament, build_pro_flights_fn, num_flights=None):
    """Build pro flights if there are any pro heats.

    ``num_flights`` is forwarded to ``build_pro_flights_fn`` so callers on the
    Run Show page can honour the operator's flight-count choice without going
    through /flights/build first. ``None`` defers sizing to the builder's
    persisted-config + auto-derive logic.
    """
    from flask import flash
    pro_heats = Heat.query.join(Event).filter(
        Event.tournament_id == tournament.id,
        Event.event_type == 'pro',
        Heat.run_number == 1
    ).count()
    if pro_heats == 0:
        flash('No pro heats available yet, so no flights were built.', 'warning')
        return None
    if num_flights is None:
        return build_pro_flights_fn(tournament)
    return build_pro_flights_fn(tournament, num_flights=num_flights)


# ---------------------------------------------------------------------------
# Import sub-modules so their @scheduling_bp.route decorators execute.
# IMPORTANT: these imports must come AFTER scheduling_bp and all shared helpers
# are defined above, so sub-modules can import them without NameError.
# ---------------------------------------------------------------------------
from . import (
    ability_rankings,  # noqa: F401, E402
    assign_marks,  # noqa: F401, E402
    birling,  # noqa: F401, E402
    events,  # noqa: F401, E402
    flights,  # noqa: F401, E402
    friday_feature,  # noqa: F401, E402
    heat_sheets,  # noqa: F401, E402
    heats,  # noqa: F401, E402
    partners,  # noqa: F401, E402
    preflight,  # noqa: F401, E402
    print_hub,  # noqa: F401, E402
    pro_checkout_roster,  # noqa: F401, E402
    show_day,  # noqa: F401, E402
)

# Re-export helpers used by routes/main.py (tournament_setup)
from .events import _get_existing_event_config, _with_field_key  # noqa: F401, E402

__all__ = ['scheduling_bp']
