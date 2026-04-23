"""
Preflight checks for scheduling and registration consistency.
"""
from __future__ import annotations

from models import Event, Flight, Heat, HeatAssignment, Tournament
from models.competitor import CollegeCompetitor, ProCompetitor
from services.gear_sharing import (
    event_matches_gear_key,
    is_using_value,
    normalize_person_name,
    strip_using_prefix,
)


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

    # 1) Heat JSON vs HeatAssignment divergence.
    # Previously: events × heats per-event lazy `event.heats.all()` plus
    # one HeatAssignment query per heat — N+M queries on the longest
    # user-visible request on race day. Replaced with two batch queries
    # (Heat + HeatAssignment scoped to tournament) and an in-memory join.
    all_events_ordered = tournament.events.order_by(
        Event.event_type, Event.name, Event.gender,
    ).all()
    event_by_id = {e.id: e for e in all_events_ordered}
    event_ids = list(event_by_id.keys())
    if event_ids:
        from collections import defaultdict
        all_heats = (
            Heat.query.filter(Heat.event_id.in_(event_ids)).all()
        )
        heat_ids = [h.id for h in all_heats]
        assignments_by_heat: dict[int, set[int]] = defaultdict(set)
        if heat_ids:
            for a in HeatAssignment.query.filter(HeatAssignment.heat_id.in_(heat_ids)).all():
                assignments_by_heat[a.heat_id].add(a.competitor_id)
        mismatched_by_event: dict[int, int] = defaultdict(int)
        for heat in all_heats:
            json_ids = set(heat.get_competitors())
            table_ids = assignments_by_heat.get(heat.id, set())
            if json_ids != table_ids:
                mismatched_by_event[heat.event_id] += 1
        # Emit issues in the same order the original loop produced.
        for event in all_events_ordered:
            count = mismatched_by_event.get(event.id, 0)
            if count:
                issues.append({
                    'severity': 'high',
                    'code': 'heat_sync_mismatch',
                    'title': 'Heat assignment mismatch',
                    'detail': f'{event.display_name}: {count} heat(s) have JSON/table mismatch.',
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
    non_enrolled_gear_rows = 0
    # Collect names for detailed issue messages.
    unresolved_details_names: list[str] = []
    unresolved_event_key_names: list[str] = []
    unknown_partner_names: list[str] = []
    self_reference_names: list[str] = []
    non_enrolled_gear_names: list[str] = []

    def _scan_rows(rows, relevant_events, known_names):
        nonlocal unknown_partner_rows, unresolved_event_key_rows, unresolved_details_rows, self_reference_rows, non_enrolled_gear_rows
        for competitor in rows:
            gear = competitor.get_gear_sharing() if hasattr(competitor, 'get_gear_sharing') else {}
            if not isinstance(gear, dict):
                continue

            details = str(getattr(competitor, 'gear_sharing_details', '') or '').strip()
            if details and not gear:
                unresolved_details_rows += 1
                if competitor.name not in unresolved_details_names:
                    unresolved_details_names.append(competitor.name)

            # Build a set of event IDs/names the competitor is actually enrolled in.
            entered_vals = {str(v or '').strip() for v in competitor.get_events_entered() if str(v or '').strip()}

            self_name = normalize_person_name(competitor.name)
            for key, partner in gear.items():
                if not any(event_matches_gear_key(event, key) for event in relevant_events):
                    unresolved_event_key_rows += 1
                    if competitor.name not in unresolved_event_key_names:
                        unresolved_event_key_names.append(competitor.name)
                    continue

                # Check the gear key actually matches an event the competitor is enrolled in.
                key_events = [e for e in relevant_events if event_matches_gear_key(e, key)]
                if key_events and entered_vals:
                    enrolled_in_key_event = any(
                        str(e.id) in entered_vals or e.name in entered_vals or e.display_name in entered_vals
                        for e in key_events
                    )
                    if not enrolled_in_key_event:
                        non_enrolled_gear_rows += 1
                        if competitor.name not in non_enrolled_gear_names:
                            non_enrolled_gear_names.append(competitor.name)

                partner_text = str(partner or '').strip()
                # USING entries carry a "using:" prefix to flag partnered-event
                # confirmation (see services/gear_sharing._USING_VALUE_PREFIX).
                # The underlying name must still resolve to a real competitor,
                # but the prefix itself is not part of the person's name.
                partner_name_only = strip_using_prefix(partner_text)
                partner_norm = normalize_person_name(partner_name_only)
                if not partner_name_only:
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
    if non_enrolled_gear_rows:
        issues.append({
            'severity': 'medium',
            'code': 'gear_non_enrolled_event',
            'title': 'Gear entries for events competitor is not enrolled in',
            'detail': (
                f'{non_enrolled_gear_rows} gear-sharing key(s) reference events the competitor is not enrolled in'
                f': {_name_list(non_enrolled_gear_names)}. These entries have no effect on heat placement.'
            ),
            'autofix': False,
        })

    # 2c) Gear vs. partner field mismatch (pro only)
    # Only USING entries claim to confirm the event partner — a mismatch there
    # is a genuine data bug (stale confirmation vs. new partner assignment).
    # SHARING entries (no "using:" prefix) are defined as cross-competitor gear
    # dependency OUTSIDE the event partnership, so gear_partner != event_partner
    # is the expected, correct shape — flagging it produced noise on every
    # Double Buck / Jack & Jill pair with a saw-sharer.
    partner_mismatch_rows = 0
    partner_mismatch_names: list[str] = []
    for comp in ProCompetitor.query.filter_by(tournament_id=tournament.id, status='active').all():
        gear = comp.get_gear_sharing() if hasattr(comp, 'get_gear_sharing') else {}
        partners = comp.get_partners() if hasattr(comp, 'get_partners') else {}
        if not isinstance(gear, dict) or not isinstance(partners, dict):
            continue
        for key, gear_partner in gear.items():
            gear_text = str(gear_partner or '').strip()
            if not is_using_value(gear_text):
                continue
            gp = normalize_person_name(strip_using_prefix(gear_text))
            pp = normalize_person_name(str(partners.get(key, '') or '').strip())
            if gp and pp and gp != pp:
                partner_mismatch_rows += 1
                if comp.name not in partner_mismatch_names:
                    partner_mismatch_names.append(comp.name)
    if partner_mismatch_rows:
        issues.append({
            'severity': 'medium',
            'code': 'gear_partner_mismatch',
            'title': 'Gear-sharing and partner fields disagree',
            'detail': (
                f'{partner_mismatch_rows} entry(s) have different names in gear_sharing vs. partners for the same event'
                f': {_name_list(partner_mismatch_names)}. Use Auto-Populate Partners in the Gear Sharing Manager to sync.'
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

    # 4) Cookie Stack / Standing Block stand conflict — both have heats but no flights yet
    # These events share 5 physical stands. The flight builder enforces the required
    # 8-heat gap automatically, but only when flights are rebuilt after heat generation.
    # Warn the judge if both events have heats but no flights exist yet.
    cookie_events = [e for e in all_events if getattr(e, 'stand_type', '') == 'cookie_stack']
    sb_events = [e for e in all_events if getattr(e, 'stand_type', '') == 'standing_block']
    cs_has_heats = any(e.heats.count() > 0 for e in cookie_events)
    sb_has_heats = any(e.heats.count() > 0 for e in sb_events)
    if cs_has_heats and sb_has_heats:
        flights_exist = Flight.query.filter_by(tournament_id=tournament.id).count() > 0
        if not flights_exist:
            issues.append({
                'severity': 'medium',
                'code': 'stand_conflict_no_flights',
                'title': 'Cookie Stack / Standing Block — rebuild flights to enforce stand gap',
                'detail': (
                    'Both Cookie Stack and Standing Block have heats generated. '
                    'These events share the same 5 physical stands. '
                    'Run "Rebuild Flights" after heat generation so the flight builder enforces '
                    'the required 8-heat gap between these event types.'
                ),
                'autofix': False,
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
