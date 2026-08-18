"""
Preflight checks for scheduling and registration consistency.

DOMAIN_CONTRACT (2026-04-27): preflight is a safety gate, not a substitute
for service-layer validation. The codes listed in ``BLOCKING_CODES`` are
hard-blockers — generation services already refuse to place affected
competitors, so the dashboard surfaces them as red banners with a
click-path fix rather than as advisory warnings.
"""
from __future__ import annotations

import json

from config import DAY_SPLIT_EVENT_NAMES
from models import Event, EventResult, Flight, Heat, Tournament
from models.competitor import CollegeCompetitor, ProCompetitor
from services.gear_sharing import (
    event_matches_gear_key,
    is_using_value,
    normalize_person_name,
    strip_using_prefix,
)
from services.mark_assignment import is_mark_assignment_eligible

# Input defects must be repaired before heat generation mutates the schedule.
PRE_GENERATION_BLOCKING_CODES = frozenset({
    'missing_partner_name',
    'unresolved_partner_name',
    'self_reference_partner',
    'non_reciprocal_partnership',
    'invalid_partner_gender',
    'gear_details_not_parsed',
    'gear_unmapped_event_keys',
    'gear_unknown_partner_names',
    'gear_self_reference',
    'gear_partner_mismatch',
    'partnered_axe_pair_state_invalid',
    'partnered_axe_prelims_incomplete',
    'partnered_axe_finals_not_advanced',
})

# These invariants can only be evaluated against the generated show layout.
POST_GENERATION_BLOCKING_CODES = frozenset({
    'invalid_flight_position',
    'duplicate_flight_position',
    'duplicate_flight_number',
    'chokerman_run2_missing_heats',
    'chokerman_run2_not_in_flights',
    'chokerman_run2_partially_in_flights',
    'chokerman_run2_invalid_closer',
})

# Public aggregate retained for the Preflight page and existing callers that
# need to answer the broad question "is any scheduling blocker present?".
BLOCKING_CODES = (
    PRE_GENERATION_BLOCKING_CODES | POST_GENERATION_BLOCKING_CODES
)


def get_blocking_issues(report: dict) -> list[dict]:
    """Return the subset of report['issues'] whose ``code`` is a hard blocker.

    Routes that trigger generation should call this and refuse to proceed
    while the list is non-empty, redirecting the operator to the preflight
    page with the blocking issues highlighted.
    """
    issues = report.get('issues') or []
    return [i for i in issues if i.get('code') in BLOCKING_CODES]


def get_pre_generation_blocking_issues(report: dict) -> list[dict]:
    """Return current input blockers that must be fixed before generation."""
    issues = report.get('issues') or []
    return [
        issue for issue in issues
        if issue.get('code') in PRE_GENERATION_BLOCKING_CODES
    ]


def get_post_generation_blocking_issues(report: dict) -> list[dict]:
    """Return blockers evaluated against generated schedule artifacts."""
    issues = report.get('issues') or []
    return [
        issue for issue in issues
        if issue.get('code') in POST_GENERATION_BLOCKING_CODES
    ]


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


def _signed_up_competitors_for_event(event: Event) -> list:
    """Return active competitors of the right type+gender enrolled in event.

    Used by partnered-event preflight checks (odd-pool, unresolved-partner,
    non-reciprocal). Mirrors the same enrollment-resolution rules
    services.heat_generator._get_event_competitors uses so what preflight
    sees matches what heat-gen sees.
    """
    target_id = str(event.id)
    target_names = {event.name.lower(), event.display_name.lower()}
    if event.event_type == 'college':
        rows = CollegeCompetitor.query.filter_by(
            tournament_id=event.tournament_id, status='active',
        ).all()
    else:
        rows = ProCompetitor.query.filter_by(
            tournament_id=event.tournament_id, status='active',
        ).all()
    if event.gender:
        rows = [c for c in rows if c.gender == event.gender]
    enrolled = []
    for comp in rows:
        entered = [str(v or '').strip() for v in (comp.get_events_entered() or [])]
        for value in entered:
            if not value:
                continue
            if value == target_id or value.lower() in target_names:
                enrolled.append(comp)
                break
    return enrolled


def _signed_up_count_for_event(event: Event) -> int:
    """Type-agnostic enrollment count (pro OR college). Replaces
    _signed_up_pro_count for the new partnered-event scan."""
    return len(_signed_up_competitors_for_event(event))


def build_preflight_report(tournament: Tournament, saturday_college_event_ids: list[int] | None = None) -> dict:
    issues: list[dict] = []
    saturday_ids = set(int(v) for v in (saturday_college_event_ids or []))

    # 1) Heat JSON vs HeatAssignment divergence used to be checked here, and
    # emitted `heat_sync_mismatch`. D12-C commit F2 deleted it. The check
    # existed because two stores held the same roster and could disagree; as
    # of commit E there is one store, `heat_assignments`, and the JSON column
    # is a projection of it that nothing reads. Two things that cannot
    # disagree do not need a check that they agree, and keeping one would
    # have meant keeping the column alive to be the thing compared against.

    # 1a) Handicap mark review.  Zero is a legitimate scratch mark, so a
    # value alone cannot distinguish a deliberate decision from a late entry
    # created after the event's marks were set.  The explicit timestamp makes
    # that distinction visible before race-day scoring starts.
    unreviewed_marks: list[dict] = []
    handicap_events = [
        event for event in tournament.events.all()
        if is_mark_assignment_eligible(event)
    ]
    for event in handicap_events:
        results = EventResult.query.filter_by(event_id=event.id).filter(
            EventResult.status.in_(['pending', 'completed'])
        ).all()
        for result in results:
            if result.mark_assigned_at is None:
                unreviewed_marks.append({
                    'event_id': event.id,
                    'event_name': event.display_name,
                    'result_id': result.id,
                    'competitor_name': result.competitor_name,
                })
    if unreviewed_marks:
        names = ', '.join(
            f"{row['competitor_name']} ({row['event_name']})"
            for row in unreviewed_marks[:5]
        )
        suffix = (
            f' (+{len(unreviewed_marks) - 5} more)'
            if len(unreviewed_marks) > 5 else ''
        )
        issues.append({
            'severity': 'high',
            'code': 'handicap_marks_unreviewed',
            'title': 'Handicap marks need review',
            'detail': (
                f'{len(unreviewed_marks)} active handicap entrant(s) have no '
                f'explicit mark review. This includes late entrants and legacy '
                f'rows, where 0.0 may mean either scratch or unassigned. Review '
                f'the marks page before scoring: {names}{suffix}.'
            ),
            'autofix': False,
            'unreviewed_marks': unreviewed_marks,
        })

    # 2) Partner completeness for ordinary partnered events (college + pro).
    # Previously pro-only — Jack & Jill / Double Buck / Pulp Toss / Peavey on
    # the college side had no odd-pool check, so a missing partner silently
    # placed the lone entrant solo on a stand at race time. Now both sides
    # are scanned.
    all_partnered_events = tournament.events.filter_by(is_partnered=True).all()
    partnered_events = [event for event in all_partnered_events if not event.has_prelims]
    partnered_axe_events = [
        event for event in all_partnered_events
        if event.has_prelims
        and not event.is_finalized
        and event.status != 'completed'
    ]
    for event in partnered_events:
        entered = _signed_up_count_for_event(event)
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

    # Partnered Axe owns its pair declarations in Event.event_state. Requiring
    # duplicate competitor.partner fields creates false blockers; ignoring the
    # state document would let missing pairs or unfinished prelims reach the
    # show builder. Validate the authoritative document instead.
    for event in partnered_axe_events:
        pool = _signed_up_competitors_for_event(event)
        if not pool:
            continue
        pool_ids = {comp.id for comp in pool}
        raw_state = event.event_state or event.payouts
        try:
            state = json.loads(raw_state or '{}')
        except (json.JSONDecodeError, TypeError):
            state = None

        pairs = state.get('pairs') if isinstance(state, dict) else None
        stage = state.get('stage') if isinstance(state, dict) else None
        represented_ids: list[int] = []
        structurally_valid = isinstance(pairs, list) and stage in {
            'prelims', 'finals', 'completed',
        }
        if structurally_valid:
            for pair in pairs:
                if not isinstance(pair, dict):
                    structurally_valid = False
                    break
                member_ids = []
                for key in ('competitor1', 'competitor2'):
                    member = pair.get(key)
                    member_id = member.get('id') if isinstance(member, dict) else None
                    if not isinstance(member_id, int) or isinstance(member_id, bool):
                        structurally_valid = False
                        break
                    member_ids.append(member_id)
                if not structurally_valid or len(set(member_ids)) != 2:
                    structurally_valid = False
                    break
                represented_ids.extend(member_ids)

        pair_state_valid = (
            structurally_valid
            and len(represented_ids) == len(set(represented_ids))
            and set(represented_ids) == pool_ids
        )
        if not pair_state_valid:
            missing_names = [
                comp.display_name for comp in pool if comp.id not in represented_ids
            ]
            names = ', '.join(missing_names[:5]) or 'review the registered pairs'
            if len(missing_names) > 5:
                names += f' (+{len(missing_names) - 5} more)'
            issues.append({
                'severity': 'high',
                'code': 'partnered_axe_pair_state_invalid',
                'title': 'Partnered Axe pair registration is incomplete',
                'detail': (
                    f'{event.display_name} pair registration does not represent '
                    'every entered competitor exactly once. Complete pair '
                    f'registration before building the show: {names}.'
                ),
                'autofix': False,
                'event_ids': [event.id],
            })
            continue

        unscored_pairs = [
            pair for pair in pairs if pair.get('prelim_score') is None
        ]
        if unscored_pairs:
            issues.append({
                'severity': 'high',
                'code': 'partnered_axe_prelims_incomplete',
                'title': 'Partnered Axe prelims are incomplete',
                'detail': (
                    f'{event.display_name} has {len(unscored_pairs)} registered '
                    'pair(s) without a preliminary score. Record every prelim '
                    'before building the finals card.'
                ),
                'autofix': False,
                'event_ids': [event.id],
            })
        elif stage == 'prelims':
            issues.append({
                'severity': 'high',
                'code': 'partnered_axe_finals_not_advanced',
                'title': 'Partnered Axe finalists are not confirmed',
                'detail': (
                    f'{event.display_name} prelims are scored, but the top four '
                    'have not been advanced to finals. Confirm the finalists '
                    'before building the show.'
                ),
                'autofix': False,
                'event_ids': [event.id],
            })

    # 2a) Unresolved partner names + non-reciprocal partnerships.
    # For every partnered-event entrant, attempt the same three-tier match the
    # heat generator runs (exact → first-name → Levenshtein ≤ 2). Flag any
    # entrant whose partner can't be resolved against the event pool, AND any
    # pair where reciprocity breaks (Jordan says McKinley but McKinley says
    # someone else, or doesn't list Jordan at all).
    from services.name_match import find_partner_match, normalize_alphanum
    unresolved_pairs: list[dict] = []
    non_reciprocal: list[dict] = []
    self_ref_partner: list[dict] = []
    invalid_partner_gender: list[dict] = []
    missing_partner: list[dict] = []
    for event in partnered_events:
        pool = _signed_up_competitors_for_event(event)
        # Build lookup by competitor id and by normalized name for fast checks.
        for comp in pool:
            partners = comp.get_partners() if hasattr(comp, 'get_partners') else {}
            partner_name = ''
            if isinstance(partners, dict):
                # Same key-priority as services.heat_generator._get_partner_name_for_event.
                for key in (str(event.id), event.name, event.display_name,
                            event.name.lower(), event.display_name.lower()):
                    raw = partners.get(key)
                    if raw and str(raw).strip():
                        partner_name = str(raw).strip()
                        break
            if not partner_name:
                missing_partner.append({
                    'competitor_id': comp.id,
                    'competitor_name': comp.display_name,
                    'event_id': event.id,
                    'event_name': event.display_name,
                })
                continue
            # Self-reference check.
            if normalize_alphanum(partner_name) == normalize_alphanum(comp.name):
                self_ref_partner.append({
                    'competitor_id': comp.id,
                    'competitor_name': comp.display_name,
                    'event_id': event.id,
                    'event_name': event.display_name,
                    'partner_name': partner_name,
                })
                continue
            matched = find_partner_match(
                partner_name, pool,
                name_getter=lambda c: c.name,
                exclude_key=comp.id,
            )
            if matched is None:
                unresolved_pairs.append({
                    'competitor_id': comp.id,
                    'competitor_name': comp.display_name,
                    'event_id': event.id,
                    'event_name': event.display_name,
                    'partner_name': partner_name,
                })
                continue
            gender_requirement = getattr(event, 'partner_gender_requirement', None)
            gender_valid = (
                gender_requirement not in {'mixed', 'same'}
                or (gender_requirement == 'mixed' and comp.gender != matched.gender)
                or (gender_requirement == 'same' and comp.gender == matched.gender)
            )
            if not gender_valid:
                invalid_partner_gender.append({
                    'competitor_id': comp.id,
                    'competitor_name': comp.display_name,
                    'competitor_gender': comp.gender,
                    'event_id': event.id,
                    'event_name': event.display_name,
                    'partner_id': matched.id,
                    'partner_name': matched.display_name,
                    'partner_gender': matched.gender,
                    'requirement': gender_requirement,
                })
            # Reciprocity: matched partner must list comp back.
            their_partners = (
                matched.get_partners() if hasattr(matched, 'get_partners') else {}
            )
            their_partner_name = ''
            if isinstance(their_partners, dict):
                for key in (str(event.id), event.name, event.display_name,
                            event.name.lower(), event.display_name.lower()):
                    raw = their_partners.get(key)
                    if raw and str(raw).strip():
                        their_partner_name = str(raw).strip()
                        break
            if not their_partner_name:
                non_reciprocal.append({
                    'competitor_id': comp.id,
                    'competitor_name': comp.display_name,
                    'event_id': event.id,
                    'event_name': event.display_name,
                    'partner_name': partner_name,
                    'partner_id': matched.id,
                    'matched_partner_name': matched.display_name,
                    'partner_says': '',
                })
                continue
            their_match = find_partner_match(
                their_partner_name, pool,
                name_getter=lambda c: c.name,
                exclude_key=matched.id,
            )
            if their_match is None or their_match.id != comp.id:
                non_reciprocal.append({
                    'competitor_id': comp.id,
                    'competitor_name': comp.display_name,
                    'event_id': event.id,
                    'event_name': event.display_name,
                    'partner_name': partner_name,
                    'partner_id': matched.id,
                    'matched_partner_name': matched.display_name,
                    'partner_says': their_partner_name,
                })
    if missing_partner:
        names = ', '.join(
            f"{row['competitor_name']} ({row['event_name']})"
            for row in missing_partner[:5]
        )
        suffix = (
            f' (+{len(missing_partner) - 5} more)'
            if len(missing_partner) > 5 else ''
        )
        issues.append({
            'severity': 'high',
            'code': 'missing_partner_name',
            'title': 'Partner declaration is blank',
            'detail': (
                f'{len(missing_partner)} partnered-event entrant(s) have no '
                f'partner declaration. Add reciprocal partners in registration '
                f'before generating heats: {names}{suffix}.'
            ),
            'autofix': False,
            'event_ids': sorted({row['event_id'] for row in missing_partner}),
            'missing': missing_partner,
        })
    if unresolved_pairs:
        names = ', '.join(
            f"{p['competitor_name']} → \"{p['partner_name']}\" ({p['event_name']})"
            for p in unresolved_pairs[:5]
        )
        suffix = f' (+{len(unresolved_pairs) - 5} more)' if len(unresolved_pairs) > 5 else ''
        issues.append({
            'severity': 'high',
            'code': 'unresolved_partner_name',
            'title': 'Partner name does not match any entered competitor',
            'detail': (
                f'{len(unresolved_pairs)} partnered-event entrant(s) listed a '
                f'partner that does not match anyone entered in the same event '
                f'(checked exact, first-name, and Levenshtein ≤ 2 fuzzy). '
                f'These competitors will be HELD BACK from heat generation '
                f'until resolved. {names}{suffix}.'
            ),
            'autofix': False,
            'unresolved': unresolved_pairs,
        })
    if self_ref_partner:
        names = ', '.join(
            f"{p['competitor_name']} ({p['event_name']})" for p in self_ref_partner[:5]
        )
        suffix = f' (+{len(self_ref_partner) - 5} more)' if len(self_ref_partner) > 5 else ''
        issues.append({
            'severity': 'high',
            'code': 'self_reference_partner',
            'title': 'Competitor listed themselves as partner',
            'detail': (
                f'{len(self_ref_partner)} entrant(s) listed their own name as '
                f'their partner. Held back from heats. {names}{suffix}.'
            ),
            'autofix': False,
            'self_references': self_ref_partner,
        })
    if non_reciprocal:
        names = ', '.join(
            f"{p['competitor_name']} → {p['matched_partner_name']} but "
            f"{p['matched_partner_name']} → {p['partner_says'] or '(none)'} "
            f"({p['event_name']})"
            for p in non_reciprocal[:5]
        )
        suffix = f' (+{len(non_reciprocal) - 5} more)' if len(non_reciprocal) > 5 else ''
        issues.append({
            'severity': 'high',
            'code': 'non_reciprocal_partnership',
            'title': 'Partnership is not reciprocal',
            'detail': (
                f'{len(non_reciprocal)} partnership(s) are non-reciprocal — '
                f'A says B is their partner, but B says someone else (or no one). '
                f'{names}{suffix}.'
            ),
            'autofix': False,
            'non_reciprocal': non_reciprocal,
        })
    if invalid_partner_gender:
        names = ', '.join(
            f"{pair['competitor_name']} ({pair['competitor_gender']}) and "
            f"{pair['partner_name']} ({pair['partner_gender']}) "
            f"({pair['event_name']})"
            for pair in invalid_partner_gender[:5]
        )
        suffix = (
            f" (+{len(invalid_partner_gender) - 5} more)"
            if len(invalid_partner_gender) > 5 else ''
        )
        issues.append({
            'severity': 'high',
            'code': 'invalid_partner_gender',
            'title': 'Partner genders violate event rule',
            'detail': (
                f"{len(invalid_partner_gender)} partnership declaration(s) "
                f"violate their event's mixed- or same-gender rule. These "
                f"competitors are held back from heat generation. {names}{suffix}."
            ),
            'autofix': False,
            'invalid_pairs': invalid_partner_gender,
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

    # 2d) Pro earnings cache must match the event-level payout ledger.
    # total_earnings is a fast standings cache; EventResult.payout_amount is
    # the payout record. Alert operators before either report is trusted when
    # an unusual correction path leaves the two out of sync.
    payout_totals: dict[int, float] = {}
    payout_rows = (
        EventResult.query.join(Event)
        .filter(
            Event.tournament_id == tournament.id,
            Event.event_type == 'pro',
            EventResult.competitor_type == 'pro',
            EventResult.payout_amount > 0,
        )
        .all()
    )
    for result in payout_rows:
        payout_totals[result.competitor_id] = (
            payout_totals.get(result.competitor_id, 0.0)
            + float(result.payout_amount or 0.0)
        )

    payout_mismatches = []
    for competitor in ProCompetitor.query.filter_by(
            tournament_id=tournament.id, status='active').all():
        ledger_total = round(payout_totals.get(competitor.id, 0.0), 2)
        cached_total = round(float(competitor.total_earnings or 0.0), 2)
        if ledger_total != cached_total:
            payout_mismatches.append({
                'competitor_name': competitor.name,
                'ledger_total': ledger_total,
                'cached_total': cached_total,
            })
    if payout_mismatches:
        names = _name_list([item['competitor_name'] for item in payout_mismatches])
        issues.append({
            'severity': 'high',
            'code': 'pro_earnings_cache_mismatch',
            'title': 'Pro earnings cache does not match payout ledger',
            'detail': (
                f'{len(payout_mismatches)} active pro competitor(s) have earnings totals '
                f'that differ from their event payout rows: {names}. Review the '
                'event results and recalculate affected events before settling payouts.'
            ),
            'autofix': False,
            'mismatches': payout_mismatches,
        })

    flights = (
        Flight.query
        .filter_by(tournament_id=tournament.id)
        .order_by(Flight.flight_number, Flight.id)
        .all()
    )

    flight_ids = [flight.id for flight in flights]
    flight_number_by_id = {
        flight.id: flight.flight_number for flight in flights
    }
    flight_order_by_id = {
        flight.id: order for order, flight in enumerate(flights)
    }
    ordered_heats = (
        Heat.query
        .filter(Heat.flight_id.in_(flight_ids))
        .all()
        if flight_ids else []
    )
    ordered_heats.sort(key=lambda heat: (
        flight_order_by_id[heat.flight_id],
        heat.flight_position is None,
        heat.flight_position if heat.flight_position is not None else 0,
        heat.id,
    ))

    from services.flight_builder import (
        _CONFLICTING_STANDS,
        _STAND_CONFLICT_GAP,
        FlightRebuildSafetyError,
        find_stand_conflicts,
        validate_chokerman_closer_invariant,
    )

    # 3) Saturday spillover integration
    # Match integrate_college_spillover_into_flights(): explicit selections are
    # restricted to this tournament's college events, then every day-split event
    # is auto-added. Chokerman participation is checked separately because the
    # shared closer validator intentionally treats an absent closer as valid.
    effective_spillover_events = sorted(
        (
            event for event in college_events
            if event.id in saturday_ids or event.name in DAY_SPLIT_EVENT_NAMES
        ),
        key=lambda event: (event.name, event.gender or '', event.id),
    )
    chokerman_events = [
        event for event in effective_spillover_events
        if event.name == "Chokerman's Race"
    ]
    current_flight_ids = set(flight_ids)
    chokerman_participation_complete = bool(chokerman_events)
    chokerman_run2_heats: list[Heat] = []

    for event in chokerman_events:
        run2_heats = (
            event.heats
            .filter_by(run_number=2)
            .order_by(Heat.heat_number, Heat.id)
            .all()
        )
        chokerman_run2_heats.extend(run2_heats)
        if not run2_heats:
            chokerman_participation_complete = False
            issues.append({
                'severity': 'high',
                'code': 'chokerman_run2_missing_heats',
                'title': "Mandatory Chokerman's Race Run 2 heats are missing",
                'detail': (
                    f'{event.display_name} has no Run 2 heats. Open Events and '
                    f'run "Generate All Heats" before building or publishing '
                    f'the Saturday flights.'
                ),
                'autofix': False,
                'event_id': event.id,
                'expected_run_number': 2,
                'heat_ids': [],
            })
            continue

        assigned = [
            heat for heat in run2_heats
            if heat.flight_id in current_flight_ids
        ]
        unassigned = [
            heat for heat in run2_heats
            if heat.flight_id not in current_flight_ids
        ]
        if not unassigned:
            continue

        chokerman_participation_complete = False
        heat_ids = [heat.id for heat in run2_heats]
        assigned_heat_ids = [heat.id for heat in assigned]
        unassigned_heat_ids = [heat.id for heat in unassigned]
        if not assigned:
            code = 'chokerman_run2_not_in_flights'
            title = "Mandatory Chokerman's Race Run 2 is not in Saturday flights"
            assignment_summary = 'None of the generated Run 2 heats are assigned'
        else:
            code = 'chokerman_run2_partially_in_flights'
            title = "Mandatory Chokerman's Race Run 2 is only partly assigned"
            assignment_summary = (
                f'{len(assigned)} of {len(run2_heats)} generated Run 2 heats '
                f'are assigned'
            )
        issues.append({
            'severity': 'high',
            'code': code,
            'title': title,
            'detail': (
                f'{event.display_name}: {assignment_summary} to this '
                f'tournament\'s Saturday flights. Build or Rebuild Flights if '
                f'needed, then run "Integrate College Spillover" so every '
                f'Chokerman Run 2 heat closes the show.'
            ),
            'autofix': bool(flights),
            'event_id': event.id,
            'heat_ids': heat_ids,
            'assigned_heat_ids': assigned_heat_ids,
            'unassigned_heat_ids': unassigned_heat_ids,
        })

    if chokerman_participation_complete:
        try:
            validate_chokerman_closer_invariant(
                tournament,
                flights=flights,
                projected_order=ordered_heats,
            )
        except FlightRebuildSafetyError as exc:
            if exc.reason == 'chokerman_closer_order':
                issues.append({
                    'severity': 'high',
                    'code': 'chokerman_run2_invalid_closer',
                    'title': "Chokerman's Race Run 2 heats are out of order",
                    'detail': (
                        'All mandatory Chokerman Run 2 heats form the final '
                        'flight suffix, but their heat-number order is reversed '
                        'or otherwise invalid. Reorder that closing block by '
                        'heat number, or run "Rebuild Flights". Current closing '
                        'heat ID order: '
                        + ', '.join(str(heat.id) for heat in chokerman_run2_heats)
                        + '.'
                    ),
                    'autofix': False,
                    'event_ids': [event.id for event in chokerman_events],
                    'heat_ids': [heat.id for heat in chokerman_run2_heats],
                    'last_flight_id': flights[-1].id if flights else None,
                    'wrong_flight_heat_ids': [],
                    'trailing_heat_ids': [],
                })
            else:
                first_closer_index = next(
                    index for index, heat in enumerate(ordered_heats)
                    if heat in chokerman_run2_heats
                )
                trailing_heat_ids = [
                    heat.id for heat in ordered_heats[first_closer_index + 1:]
                    if heat not in chokerman_run2_heats
                ]
                last_flight_id = flights[-1].id if flights else None
                wrong_flight_heat_ids = [
                    heat.id for heat in chokerman_run2_heats
                    if heat.flight_id != last_flight_id
                ]
                offending_ids = wrong_flight_heat_ids + trailing_heat_ids
                offending_summary = ', '.join(
                    str(heat_id) for heat_id in offending_ids
                )
                issues.append({
                    'severity': 'high',
                    'code': 'chokerman_run2_invalid_closer',
                    'title': "Chokerman's Race Run 2 does not close the show",
                    'detail': (
                        f'All mandatory Chokerman Run 2 heats are assigned, but '
                        f'they do not form the final suffix of the last flight. '
                        f'Run "Rebuild Flights" or reorder the affected heats so '
                        f'nothing follows Chokerman Run 2. Affected schedule heat '
                        f'ID(s): {offending_summary or "unknown"}.'
                    ),
                    'autofix': False,
                    'event_ids': [event.id for event in chokerman_events],
                    'heat_ids': [heat.id for heat in chokerman_run2_heats],
                    'last_flight_id': last_flight_id,
                    'wrong_flight_heat_ids': wrong_flight_heat_ids,
                    'trailing_heat_ids': trailing_heat_ids,
                })

    if flights:
        for event in effective_spillover_events:
            if event.name == "Chokerman's Race":
                continue
            if event.name in DAY_SPLIT_EVENT_NAMES:
                spillover_heats = (
                    event.heats
                    .filter_by(run_number=2)
                    .order_by(Heat.heat_number, Heat.id)
                    .all()
                )
            else:
                spillover_heats = (
                    event.heats
                    .order_by(Heat.run_number, Heat.heat_number, Heat.id)
                    .all()
                )
            if not spillover_heats:
                issues.append({
                    'severity': 'medium',
                    'code': 'spillover_missing_heats',
                    'title': 'Spillover has no heats',
                    'detail': f'{event.display_name}: no heats to integrate.',
                    'autofix': False,
                    'event_id': event.id,
                    'expected_run_number': (
                        2 if event.name in DAY_SPLIT_EVENT_NAMES else None
                    ),
                })
                continue
            unassigned = [
                heat for heat in spillover_heats
                if heat.flight_id not in current_flight_ids
            ]
            if unassigned:
                issues.append({
                    'severity': 'high',
                    'code': 'spillover_not_in_flights',
                    'title': 'Spillover not integrated into flights',
                    'detail': (
                        f'{event.display_name}: {len(unassigned)} heat(s) are '
                        f'not assigned to a Saturday flight.'
                    ),
                    'autofix': True,
                    'event_id': event.id,
                    'unassigned_heat_ids': [heat.id for heat in unassigned],
                })

    configured_stand_pairs = sorted({
        tuple(sorted((stand_type, conflict_type)))
        for stand_type, conflict_types in _CONFLICTING_STANDS.items()
        for conflict_type in conflict_types
    })

    # 4) Before flights exist, show every configured physical stand pair that
    # already has heats on both sides. Reciprocal configuration entries collapse
    # into one pair so the operator receives one actionable diagnostic.
    if not flights and configured_stand_pairs:
        event_ids_with_heats = {
            event_id
            for event_id, in (
                Heat.query
                .with_entities(Heat.event_id)
                .filter(Heat.event_id.in_([event.id for event in all_events]))
                .distinct()
                .all()
            )
        }
        events_by_stand_type: dict[str, list[Event]] = {}
        for event in all_events:
            if event.id not in event_ids_with_heats or not event.stand_type:
                continue
            events_by_stand_type.setdefault(event.stand_type, []).append(event)

        unbuilt_conflicts = []
        pair_summaries = []
        for first_type, second_type in configured_stand_pairs:
            first_names = tuple(sorted(
                event.display_name
                for event in events_by_stand_type.get(first_type, ())
            ))
            second_names = tuple(sorted(
                event.display_name
                for event in events_by_stand_type.get(second_type, ())
            ))
            if not first_names or not second_names:
                continue
            unbuilt_conflicts.append({
                'stand_types': (first_type, second_type),
                'event_names': (first_names, second_names),
                'required_gap': _STAND_CONFLICT_GAP,
            })
            pair_summaries.append(
                f'{", ".join(first_names)} ({first_type}) / '
                f'{", ".join(second_names)} ({second_type})'
            )

        if unbuilt_conflicts:
            issues.append({
                'severity': 'medium',
                'code': 'stand_conflict_no_flights',
                'title': 'Shared physical stands need flight spacing',
                'detail': (
                    f'Generated heats share physical stands across these event '
                    f'pairs: {"; ".join(pair_summaries)}. Run "Rebuild Flights" '
                    f'after heat generation to enforce the required '
                    f'{_STAND_CONFLICT_GAP}-heat gap for each pair.'
                ),
                'autofix': False,
                'pairs': unbuilt_conflicts,
            })

    # 4a) Inspect the actual built Saturday show order for every physical
    # shared-stand pair configured by the flight builder. Keep this advisory:
    # the builder may retain an unavoidable fallback, but the judge still
    # needs the exact heats and spacing required to resolve it manually.
    if flights:
        # Spillover refuses to mutate an ambiguous persisted show order. Detect
        # the same malformed structures from this already-batched snapshot so
        # preflight gives the operator exact records to repair before calling it.
        heats_by_flight_id = {flight.id: [] for flight in flights}
        for heat in ordered_heats:
            heats_by_flight_id[heat.flight_id].append(heat)

        invalid_position_heats = sorted(
            (
                heat for heat in ordered_heats
                if heat.flight_position is None or heat.flight_position <= 0
            ),
            key=lambda heat: (flight_order_by_id[heat.flight_id], heat.id),
        )
        if invalid_position_heats:
            invalid_details = []
            invalid_summaries = []
            for heat in invalid_position_heats:
                problem = (
                    'missing' if heat.flight_position is None
                    else 'non_positive'
                )
                invalid_details.append({
                    'flight_id': heat.flight_id,
                    'flight_number': flight_number_by_id[heat.flight_id],
                    'heat_id': heat.id,
                    'event_id': heat.event_id,
                    'event_name': heat.event.display_name,
                    'heat_number': heat.heat_number,
                    'run_number': heat.run_number,
                    'flight_position': heat.flight_position,
                    'problem': problem,
                })
                position_summary = (
                    'missing flight_position'
                    if heat.flight_position is None
                    else f'non-positive flight_position {heat.flight_position}'
                )
                invalid_summaries.append(
                    f'Flight {flight_number_by_id[heat.flight_id]} heat {heat.id} '
                    f'({heat.event.display_name} Heat {heat.heat_number}, '
                    f'Run {heat.run_number}): {position_summary}'
                )
            issues.append({
                'severity': 'high',
                'code': 'invalid_flight_position',
                'title': 'Built flight heats have invalid positions',
                'detail': (
                    f'{"; ".join(invalid_summaries)}. Open Flights and assign '
                    f'each heat a positive unique position, or run "Rebuild '
                    f'Flights" before integrating spillover.'
                ),
                'autofix': False,
                'heats': invalid_details,
            })

        duplicate_position_details = []
        duplicate_position_summaries = []
        for flight in flights:
            heats_by_position: dict[int, list[Heat]] = {}
            for heat in heats_by_flight_id[flight.id]:
                if heat.flight_position is None:
                    continue
                heats_by_position.setdefault(heat.flight_position, []).append(heat)
            for position, position_heats in sorted(heats_by_position.items()):
                if len(position_heats) < 2:
                    continue
                position_heats.sort(key=lambda heat: heat.id)
                heat_details = tuple({
                    'heat_id': heat.id,
                    'event_id': heat.event_id,
                    'event_name': heat.event.display_name,
                    'heat_number': heat.heat_number,
                    'run_number': heat.run_number,
                } for heat in position_heats)
                duplicate_position_details.append({
                    'flight_id': flight.id,
                    'flight_number': flight.flight_number,
                    'flight_position': position,
                    'heats': heat_details,
                })
                heat_ids = [str(heat.id) for heat in position_heats]
                if len(heat_ids) == 2:
                    heat_id_summary = f'{heat_ids[0]} and {heat_ids[1]}'
                else:
                    heat_id_summary = f'{", ".join(heat_ids[:-1])}, and {heat_ids[-1]}'
                duplicate_position_summaries.append(
                    f'Flight {flight.flight_number} position {position}: '
                    f'heats {heat_id_summary}'
                )
        if duplicate_position_details:
            issues.append({
                'severity': 'high',
                'code': 'duplicate_flight_position',
                'title': 'Built flight heats share positions',
                'detail': (
                    f'{"; ".join(duplicate_position_summaries)}. Open Flights '
                    f'and assign each heat a positive unique position, or run '
                    f'"Rebuild Flights" before integrating spillover.'
                ),
                'autofix': False,
                'duplicates': duplicate_position_details,
            })

        flights_by_number: dict[int, list[Flight]] = {}
        for flight in flights:
            flights_by_number.setdefault(flight.flight_number, []).append(flight)
        duplicate_number_details = []
        duplicate_number_summaries = []
        for flight_number, number_flights in sorted(flights_by_number.items()):
            if len(number_flights) < 2:
                continue
            number_flights.sort(key=lambda flight: flight.id)
            flight_details = []
            for flight in number_flights:
                heat_details = tuple({
                    'heat_id': heat.id,
                    'event_id': heat.event_id,
                    'event_name': heat.event.display_name,
                    'heat_number': heat.heat_number,
                    'run_number': heat.run_number,
                    'flight_position': heat.flight_position,
                } for heat in heats_by_flight_id[flight.id])
                flight_details.append({
                    'flight_id': flight.id,
                    'heats': heat_details,
                })
            duplicate_number_details.append({
                'flight_number': flight_number,
                'flights': tuple(flight_details),
            })
            flight_ids_for_number = [
                str(flight.id) for flight in number_flights
            ]
            if len(flight_ids_for_number) == 2:
                flight_id_summary = (
                    f'{flight_ids_for_number[0]} and {flight_ids_for_number[1]}'
                )
            else:
                flight_id_summary = (
                    f'{", ".join(flight_ids_for_number[:-1])}, and '
                    f'{flight_ids_for_number[-1]}'
                )
            heat_id_summary = ', '.join(
                f'flight {flight.id}: '
                f'{", ".join(f"heat {heat.id}" for heat in heats_by_flight_id[flight.id]) or "no heats"}'
                for flight in number_flights
            )
            duplicate_number_summaries.append(
                f'Flight number {flight_number} is used by flights '
                f'{flight_id_summary} ({heat_id_summary})'
            )
        if duplicate_number_details:
            issues.append({
                'severity': 'high',
                'code': 'duplicate_flight_number',
                'title': 'Built flights share flight numbers',
                'detail': (
                    f'{"; ".join(duplicate_number_summaries)}. Open Flights and '
                    f'assign every flight a unique number, or run "Rebuild '
                    f'Flights" before integrating spillover.'
                ),
                'autofix': False,
                'duplicates': duplicate_number_details,
            })

        detected_conflicts = find_stand_conflicts(ordered_heats)
        if detected_conflicts:
            heat_by_id = {heat.id: heat for heat in ordered_heats}
            conflict_details = []
            summaries = []
            for conflict in detected_conflicts:
                first_id, second_id = conflict['heat_ids']
                first_heat = heat_by_id[first_id]
                second_heat = heat_by_id[second_id]
                first_flight_number = flight_number_by_id[first_heat.flight_id]
                second_flight_number = flight_number_by_id[second_heat.flight_id]
                conflict_details.append({
                    'heat_ids': (first_id, second_id),
                    'stand_types': conflict['stand_types'],
                    'events': (
                        first_heat.event.display_name,
                        second_heat.event.display_name,
                    ),
                    'heat_numbers': (
                        first_heat.heat_number,
                        second_heat.heat_number,
                    ),
                    'run_numbers': (
                        first_heat.run_number,
                        second_heat.run_number,
                    ),
                    'flight_numbers': (
                        first_flight_number,
                        second_flight_number,
                    ),
                    'flight_positions': (
                        first_heat.flight_position,
                        second_heat.flight_position,
                    ),
                    'gap': conflict['gap'],
                    'required_gap': _STAND_CONFLICT_GAP,
                })
                summaries.append(
                    f'{first_heat.event.display_name} Heat {first_heat.heat_number} '
                    f'(Run {first_heat.run_number}, Flight {first_flight_number}, '
                    f'position {first_heat.flight_position}) and '
                    f'{second_heat.event.display_name} Heat {second_heat.heat_number} '
                    f'(Run {second_heat.run_number}, Flight {second_flight_number}, '
                    f'position {second_heat.flight_position}): current gap '
                    f"{conflict['gap']}, required gap {_STAND_CONFLICT_GAP}"
                )

            shown = '; '.join(summaries[:5])
            suffix = (
                f' (+{len(summaries) - 5} more)'
                if len(summaries) > 5 else ''
            )
            issues.append({
                'severity': 'medium',
                'code': 'stand_conflict_built_flights',
                'title': 'Built flights contain physical shared-stand conflicts',
                'detail': (
                    f'{len(conflict_details)} shared-stand conflict(s) are too '
                    f'close in the Saturday show order. Rebuild flights or '
                    f'reorder these heats to meet the required gap: '
                    f'{shown}{suffix}.'
                ),
                'autofix': False,
                'conflicts': conflict_details,
            })

    by_severity = {'high': 0, 'medium': 0, 'low': 0}
    for item in issues:
        by_severity[item.get('severity', 'low')] = by_severity.get(item.get('severity', 'low'), 0) + 1

    pre_generation_blocking = [
        issue for issue in issues
        if issue.get('code') in PRE_GENERATION_BLOCKING_CODES
    ]
    post_generation_blocking = [
        issue for issue in issues
        if issue.get('code') in POST_GENERATION_BLOCKING_CODES
    ]
    blocking = pre_generation_blocking + post_generation_blocking
    return {
        'issue_count': len(issues),
        'issues': issues,
        'severity': by_severity,
        'has_autofixable': any(i.get('autofix') for i in issues),
        'blocking': blocking,
        'has_blockers': bool(blocking),
        'pre_generation_blocking': pre_generation_blocking,
        'post_generation_blocking': post_generation_blocking,
    }
