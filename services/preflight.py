"""
Preflight checks for scheduling and registration consistency.
"""
from __future__ import annotations

from models import Event, Flight, Heat, HeatAssignment, Tournament
from models.competitor import ProCompetitor


def _signed_up_pro_count(event: Event) -> int:
    target_id = str(event.id)
    target_names = {event.name.lower(), event.display_name.lower()}
    count = 0
    for comp in ProCompetitor.query.filter_by(tournament_id=event.tournament_id, status='active').all():
        entered = [str(v or '').strip() for v in comp.get_events_entered()]
        for value in entered:
            if not value:
                continue
            if value == target_id or value.lower() in target_names:
                count += 1
                break
    return count


def build_preflight_report(tournament: Tournament, saturday_college_event_ids: list[int] | None = None) -> dict:
    issues: list[dict] = []
    saturday_ids = set(int(v) for v in (saturday_college_event_ids or []))

    # 1) Heat JSON vs HeatAssignment divergence
    for event in tournament.events.order_by(Event.event_type, Event.name, Event.gender).all():
        mismatched = 0
        for heat in event.heats.all():
            json_ids = set(heat.get_competitors())
            table_ids = set(a.competitor_id for a in HeatAssignment.query.filter_by(heat_id=heat.id).all())
            if json_ids != table_ids:
                mismatched += 1
        if mismatched:
            issues.append({
                'severity': 'high',
                'code': 'heat_sync_mismatch',
                'title': 'Heat assignment mismatch',
                'detail': f'{event.display_name}: {mismatched} heat(s) have JSON/table mismatch.',
                'autofix': True,
            })

    # 2) Partner completeness for pro partnered events
    partnered_events = tournament.events.filter_by(event_type='pro', is_partnered=True).all()
    for event in partnered_events:
        entered = _signed_up_pro_count(event)
        if entered <= 1:
            continue
        if entered % 2 != 0:
            issues.append({
                'severity': 'medium',
                'code': 'odd_partner_pool',
                'title': 'Odd partner pool',
                'detail': f'{event.display_name}: {entered} entrants, one competitor will remain unmatched.',
                'autofix': True,
            })

    # 3) Saturday spillover integration
    if saturday_ids:
        flights = Flight.query.filter_by(tournament_id=tournament.id).all()
        if not flights:
            issues.append({
                'severity': 'high',
                'code': 'spillover_no_flights',
                'title': 'No flights for spillover integration',
                'detail': 'Saturday spillover events selected but pro flights are not built yet.',
                'autofix': False,
            })
        else:
            for event in tournament.events.filter(Event.id.in_(saturday_ids)).all():
                spillover_heats = event.heats.filter_by(run_number=2).all() if event.name == "Chokerman's Race" else event.heats.all()
                if not spillover_heats:
                    issues.append({
                        'severity': 'medium',
                        'code': 'spillover_missing_heats',
                        'title': 'Spillover has no heats',
                        'detail': f'{event.display_name}: no heats to integrate.',
                        'autofix': False,
                    })
                    continue
                unassigned = [h for h in spillover_heats if h.flight_id is None]
                if unassigned:
                    issues.append({
                        'severity': 'high',
                        'code': 'spillover_not_in_flights',
                        'title': 'Spillover not integrated into flights',
                        'detail': f'{event.display_name}: {len(unassigned)} heat(s) are not assigned to a Saturday flight.',
                        'autofix': True,
                    })

    # Always enforce mandatory Chokerman run 2 awareness.
    chokerman = tournament.events.filter_by(event_type='college', name="Chokerman's Race").first()
    if chokerman and chokerman.id not in saturday_ids:
        issues.append({
            'severity': 'medium',
            'code': 'mandatory_chokerman_not_selected',
            'title': 'Mandatory Saturday Chokerman Run 2 not selected',
            'detail': "Chokerman's Race exists but is not selected for Saturday spillover integration.",
            'autofix': True,
        })

    by_severity = {'high': 0, 'medium': 0, 'low': 0}
    for item in issues:
        by_severity[item.get('severity', 'low')] = by_severity.get(item.get('severity', 'low'), 0) + 1

    return {
        'issue_count': len(issues),
        'issues': issues,
        'severity': by_severity,
        'has_autofixable': any(i.get('autofix') for i in issues),
    }

