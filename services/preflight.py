"""
Preflight checks for scheduling and registration consistency.
"""
from __future__ import annotations

from models import Event, Flight, HeatAssignment, Tournament
from models.competitor import CollegeCompetitor, ProCompetitor
from services.gear_sharing import event_matches_gear_key, normalize_person_name


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

    # 2b) Gear-sharing integrity (college + pro)
    all_events = tournament.events.all()
    pro_events = [e for e in all_events if e.event_type == 'pro']
    college_events = [e for e in all_events if e.event_type == 'college']
    pro_names = {
        normalize_person_name(c.name)
        for c in ProCompetitor.query.filter_by(tournament_id=tournament.id, status='active').all()
    }
    college_names = {
        normalize_person_name(c.name)
        for c in CollegeCompetitor.query.filter_by(tournament_id=tournament.id, status='active').all()
    }

    unknown_partner_rows = 0
    unresolved_event_key_rows = 0
    unresolved_details_rows = 0
    self_reference_rows = 0
    # Collect names for detailed issue messages.
    unresolved_details_names: list[str] = []
    unresolved_event_key_names: list[str] = []
    unknown_partner_names: list[str] = []
    self_reference_names: list[str] = []

    def _scan_rows(rows, relevant_events, known_names):
        nonlocal unknown_partner_rows, unresolved_event_key_rows, unresolved_details_rows, self_reference_rows
        for competitor in rows:
            gear = competitor.get_gear_sharing() if hasattr(competitor, 'get_gear_sharing') else {}
            if not isinstance(gear, dict):
                continue

            details = str(getattr(competitor, 'gear_sharing_details', '') or '').strip()
            if details and not gear:
                unresolved_details_rows += 1
                if competitor.name not in unresolved_details_names:
                    unresolved_details_names.append(competitor.name)

            self_name = normalize_person_name(competitor.name)
            for key, partner in gear.items():
                if not any(event_matches_gear_key(event, key) for event in relevant_events):
                    unresolved_event_key_rows += 1
                    if competitor.name not in unresolved_event_key_names:
                        unresolved_event_key_names.append(competitor.name)
                partner_text = str(partner or '').strip()
                partner_norm = normalize_person_name(partner_text)
                if not partner_text:
                    unknown_partner_rows += 1
                    if competitor.name not in unknown_partner_names:
                        unknown_partner_names.append(competitor.name)
                    continue
                if partner_text.startswith('group:'):
                    continue
                if partner_norm == self_name:
                    self_reference_rows += 1
                    if competitor.name not in self_reference_names:
                        self_reference_names.append(competitor.name)
                if partner_norm and partner_norm not in known_names:
                    unknown_partner_rows += 1
                    if competitor.name not in unknown_partner_names:
                        unknown_partner_names.append(competitor.name)

    _scan_rows(ProCompetitor.query.filter_by(tournament_id=tournament.id, status='active').all(), pro_events, pro_names)
    _scan_rows(CollegeCompetitor.query.filter_by(tournament_id=tournament.id, status='active').all(), college_events, college_names)

    def _name_list(names: list[str], limit: int = 5) -> str:
        shown = names[:limit]
        suffix = f' (+{len(names) - limit} more)' if len(names) > limit else ''
        return ', '.join(shown) + suffix

    if unresolved_details_rows:
        issues.append({
            'severity': 'medium',
            'code': 'gear_details_not_parsed',
            'title': 'Gear-sharing details not structured',
            'detail': (
                f'{unresolved_details_rows} competitor(s) have free-text gear details but no structured gear-sharing map'
                f': {_name_list(unresolved_details_names)}.'
            ),
            'autofix': True,
        })
    if unresolved_event_key_rows:
        issues.append({
            'severity': 'high',
            'code': 'gear_unmapped_event_keys',
            'title': 'Gear-sharing event keys not mapped',
            'detail': (
                f'{unresolved_event_key_rows} gear-sharing key(s) do not map to configured events/categories'
                f': {_name_list(unresolved_event_key_names)}.'
            ),
            'autofix': False,
        })
    if unknown_partner_rows:
        issues.append({
            'severity': 'high',
            'code': 'gear_unknown_partner_names',
            'title': 'Gear-sharing partner names unresolved',
            'detail': (
                f'{unknown_partner_rows} gear-sharing entry(s) reference blank or unknown partner names'
                f': {_name_list(unknown_partner_names)}.'
            ),
            'autofix': False,
        })
    if self_reference_rows:
        issues.append({
            'severity': 'high',
            'code': 'gear_self_reference',
            'title': 'Self-referenced gear-sharing entries',
            'detail': (
                f'{self_reference_rows} gear-sharing entry(s) reference the same competitor as partner'
                f': {_name_list(self_reference_names)}.'
            ),
            'autofix': False,
        })

    # 3) Saturday spillover integration
    if saturday_ids:
        flights = Flight.query.filter_by(tournament_id=tournament.id).all()
        if flights:
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

    by_severity = {'high': 0, 'medium': 0, 'low': 0}
    for item in issues:
        by_severity[item.get('severity', 'low')] = by_severity.get(item.get('severity', 'low'), 0) + 1

    return {
        'issue_count': len(issues),
        'issues': issues,
        'severity': by_severity,
        'has_autofixable': any(i.get('autofix') for i in issues),
    }
