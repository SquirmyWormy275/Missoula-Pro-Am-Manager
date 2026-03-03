"""
Partner matching helpers for pro partnered events.
"""
from __future__ import annotations

import re
from database import db
from models import Event, Tournament
from models.competitor import ProCompetitor


def _normalize_name(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', str(value or '').strip().lower())


def _is_entered(event: Event, entered_events: list) -> bool:
    target_id = str(event.id)
    target_name = _normalize_name(event.name)
    target_display = _normalize_name(event.display_name)

    for raw in entered_events or []:
        value = str(raw or '').strip()
        if not value:
            continue
        if value == target_id:
            return True
        normalized = _normalize_name(value)
        if normalized in {target_name, target_display}:
            return True
    return False


def _read_partner_name(comp: ProCompetitor, event: Event) -> str:
    partners = comp.get_partners()
    if not isinstance(partners, dict):
        return ''
    for key in [str(event.id), event.name, event.display_name, event.name.lower(), event.display_name.lower()]:
        value = str(partners.get(key, '')).strip()
        if value:
            return value
    return ''


def _set_partner_bidirectional(a: ProCompetitor, b: ProCompetitor, event: Event) -> None:
    # Store by event id and names to stay compatible with existing readers/imports.
    for key in [str(event.id), event.name, event.display_name]:
        a.set_partner(key, b.name)
        b.set_partner(key, a.name)


def _event_pool(event: Event) -> list[ProCompetitor]:
    competitors = ProCompetitor.query.filter_by(
        tournament_id=event.tournament_id,
        status='active',
    ).order_by(ProCompetitor.name).all()

    if event.gender in {'M', 'F'}:
        competitors = [c for c in competitors if c.gender == event.gender]

    return [c for c in competitors if _is_entered(event, c.get_events_entered())]


def auto_assign_event_partners(event: Event) -> dict:
    """
    Auto assign partners for one partnered pro event.

    Returns summary dict.
    """
    if event.event_type != 'pro' or not event.is_partnered:
        return {'event_id': event.id, 'event': event.display_name, 'assigned_pairs': 0, 'unmatched': 0}

    pool = _event_pool(event)
    by_name = {_normalize_name(c.name): c for c in pool}
    unresolved = []
    assigned_pairs = 0
    used = set()

    for comp in pool:
        if comp.id in used:
            continue
        partner_name = _read_partner_name(comp, event)
        if not partner_name:
            unresolved.append(comp)
            continue

        partner = by_name.get(_normalize_name(partner_name))
        if not partner or partner.id == comp.id:
            unresolved.append(comp)
            continue
        if partner.id in used:
            continue

        reciprocal = _read_partner_name(partner, event)
        if _normalize_name(reciprocal) != _normalize_name(comp.name):
            unresolved.append(comp)
            continue

        used.add(comp.id)
        used.add(partner.id)

    unresolved = [c for c in unresolved if c.id not in used]

    # Mixed events prefer M/F pairs.
    mixed_required = event.partner_gender_requirement == 'mixed'
    men = [c for c in unresolved if c.gender == 'M']
    women = [c for c in unresolved if c.gender == 'F']
    remaining = [c for c in unresolved if c.gender not in {'M', 'F'}]

    if mixed_required:
        while men and women:
            a = men.pop(0)
            b = women.pop(0)
            _set_partner_bidirectional(a, b, event)
            used.add(a.id)
            used.add(b.id)
            assigned_pairs += 1
        unresolved = men + women + remaining
    else:
        unresolved = men + women + remaining
        while len(unresolved) >= 2:
            a = unresolved.pop(0)
            b = unresolved.pop(0)
            _set_partner_bidirectional(a, b, event)
            used.add(a.id)
            used.add(b.id)
            assigned_pairs += 1

    db.session.flush()
    return {
        'event_id': event.id,
        'event': event.display_name,
        'assigned_pairs': assigned_pairs,
        'unmatched': len(unresolved),
    }


def auto_assign_pro_partners(tournament: Tournament) -> dict:
    """Auto assign partners across all partnered pro events in a tournament."""
    events = tournament.events.filter_by(event_type='pro', is_partnered=True).order_by(Event.name, Event.gender).all()
    summaries = [auto_assign_event_partners(event) for event in events]
    return {
        'event_count': len(summaries),
        'assigned_pairs': sum(s['assigned_pairs'] for s in summaries),
        'unmatched': sum(s['unmatched'] for s in summaries),
        'events': summaries,
    }

