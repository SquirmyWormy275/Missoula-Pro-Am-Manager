"""
Ability rankings route — judge-assigned per-event ranks for heat snake-draft sort (pro)
and per-school birling seedings (college).
"""
import json

from flask import flash, redirect, render_template, request, url_for

from config import event_rank_category as _event_rank_category
from database import db
from models import Event, Tournament
from models.competitor import CollegeCompetitor, ProCompetitor
from services.audit import log_action

from . import _competitor_entered_event, _signed_up_competitors, scheduling_bp

# ---------------------------------------------------------------------------
# Ability Rankings — per-event judge-assigned ranks for heat snake-draft sort
# ---------------------------------------------------------------------------

@scheduling_bp.route('/<int:tournament_id>/pro/ability-rankings', methods=['GET', 'POST'])
def ability_rankings(tournament_id):
    """View and set per-event ability rankings for pro competitors."""
    from models.pro_event_rank import (
        CATEGORY_DESCRIPTIONS,
        CATEGORY_DISPLAY_NAMES,
        RANKED_CATEGORIES,
        ProEventRank,
    )

    tournament = Tournament.query.get_or_404(tournament_id)

    if request.method == 'POST':
        # Parse order_{category}_{gender} fields — each is a comma-separated list
        # of competitor IDs in drag-and-drop rank order (position = rank).
        # Competitors not in the list are unranked (their existing rank is deleted).
        saved_count = 0
        deleted_count = 0

        # Collect all ordered lists from the form.
        ordered_lists: dict = {}  # (category, gender) → [comp_id, ...]
        for key, raw_val in request.form.items():
            if not key.startswith('order_'):
                continue
            # key format: order_{category}_{gender} — category may contain underscores
            # so split from the right: last segment is gender, middle is category.
            rest = key[len('order_'):]
            last_underscore = rest.rfind('_')
            if last_underscore < 0:
                continue
            category = rest[:last_underscore]
            if category not in RANKED_CATEGORIES:
                continue
            raw_val = raw_val.strip()
            if not raw_val:
                ordered_lists[(category, rest[last_underscore + 1:])] = []
                continue
            try:
                comp_ids = [int(x) for x in raw_val.split(',') if x.strip()]
            except (TypeError, ValueError):
                continue
            ordered_lists[(category, rest[last_underscore + 1:])] = comp_ids

        # Process each ordered list: ranked competitors get rank = position (1-based).
        all_comp_ids_by_cat: dict = {}  # category → set of comp_ids that have been ranked
        for (category, _gender), comp_ids in ordered_lists.items():
            ranked_set = all_comp_ids_by_cat.setdefault(category, set())
            for position, comp_id in enumerate(comp_ids, start=1):
                ranked_set.add(comp_id)
                existing = ProEventRank.query.filter_by(
                    tournament_id=tournament_id,
                    competitor_id=comp_id,
                    event_category=category,
                ).first()
                if existing:
                    existing.rank = position
                else:
                    db.session.add(ProEventRank(
                        tournament_id=tournament_id,
                        competitor_id=comp_id,
                        event_category=category,
                        rank=position,
                    ))
                saved_count += 1

        # Delete ranks for competitors that were NOT in any ordered list for their category.
        for category, ranked_ids in all_comp_ids_by_cat.items():
            stale = ProEventRank.query.filter(
                ProEventRank.tournament_id == tournament_id,
                ProEventRank.event_category == category,
                ~ProEventRank.competitor_id.in_(ranked_ids) if ranked_ids else True,
            ).all()
            for r in stale:
                db.session.delete(r)
                deleted_count += 1

        db.session.commit()
        log_action('ability_rankings_saved', 'tournament', tournament_id, {
            'saved': saved_count,
            'cleared': deleted_count,
        })

        # ── College birling seedings ────────────────────────────────────
        birling_saved = 0
        birling_events = tournament.events.filter_by(
            event_type='college', scoring_type='bracket'
        ).all()
        for bev in birling_events:
            key = f'birling_schools_{bev.id}'
            raw_schools = request.form.get(key, '').strip()
            if not raw_schools:
                continue
            # raw_schools is JSON: {"school_name": [comp_id, comp_id], ...}
            try:
                school_orders = json.loads(raw_schools)
            except (json.JSONDecodeError, TypeError):
                continue
            # Compute global seed numbers:
            # All #1 picks first (one per school), then all #2 picks.
            pre_seedings = {}
            max_depth = max((len(ids) for ids in school_orders.values()), default=0)
            seed = 1
            for depth in range(max_depth):
                for _school, ids in sorted(school_orders.items()):
                    if depth < len(ids):
                        try:
                            pre_seedings[int(ids[depth])] = seed
                            seed += 1
                        except (TypeError, ValueError):
                            continue
            # Store in the Event.payouts JSON under 'pre_seedings'.
            try:
                existing_data = json.loads(bev.payouts or '{}')
            except (json.JSONDecodeError, TypeError):
                existing_data = {}
            existing_data['pre_seedings'] = pre_seedings
            bev.payouts = json.dumps(existing_data)
            birling_saved += len(pre_seedings)

        if birling_saved:
            db.session.commit()

        total_saved = saved_count + birling_saved
        flash(f'Rankings saved ({total_saved} set, {deleted_count} cleared).', 'success')
        return redirect(url_for('scheduling.ability_rankings', tournament_id=tournament_id))

    # GET — build display data.
    # ── Pro ability rankings ────────────────────────────────────────────
    # Only show competitors who actually signed up for an event in each
    # category, segregated by the event's gender. Jack & Jill is mixed —
    # both genders appear in one 'open' list.
    pro_events = tournament.events.filter_by(event_type='pro').all()

    # Group events by ability-ranking category.
    category_events: dict = {}
    for event in pro_events:
        cat = _event_rank_category(event)
        if cat:
            category_events.setdefault(cat, []).append(event)

    # Load existing ranks for this tournament.
    existing_ranks = ProEventRank.query.filter_by(tournament_id=tournament_id).all()
    rank_map: dict = {
        (r.competitor_id, r.event_category): r.rank for r in existing_ranks
    }

    # Pre-fetch active pros once so each category lookup is in-memory.
    all_active_comps = ProCompetitor.query.filter_by(
        tournament_id=tournament_id,
        status='active',
    ).order_by(ProCompetitor.name).all()

    # Build category_groups: {category: {'M': [...], 'F': [...], 'open': [...]}}.
    # A pro appears in a category only if they signed up for at least one event
    # of that category matching their gender (or mixed, for jack_jill).
    category_groups: dict = {}
    for category, events_in_cat in category_events.items():
        group: dict = {}
        # Track competitor IDs already placed per bucket so the same pro
        # doesn't appear twice when multiple events map to one category
        # (e.g. Standing Block Speed + Standing Block Hard Hit).
        seen: dict = {'M': set(), 'F': set(), 'open': set()}
        for comp in all_active_comps:
            entered = comp.get_events_entered()
            # Check each event in this category. The event-level gender
            # filter (Men's Underhand excludes women, etc.) is enforced
            # here the same way _signed_up_competitors does it.
            matched_any = False
            for event in events_in_cat:
                if event.gender and comp.gender != event.gender:
                    continue
                if _competitor_entered_event(event, entered):
                    matched_any = True
                    break
            if not matched_any:
                continue

            if category == 'jack_jill':
                gender_key = 'open'
            elif comp.gender in ('M', 'F'):
                gender_key = comp.gender
            else:
                gender_key = 'open'

            if comp.id in seen[gender_key]:
                continue
            seen[gender_key].add(comp.id)
            group.setdefault(gender_key, []).append({
                'competitor': comp,
                'rank': rank_map.get((comp.id, category)),
            })

        # Sort each gender group by current rank (ranked first, then
        # unranked alphabetically).
        for gk in group:
            group[gk].sort(key=lambda e: (
                e['rank'] if e['rank'] is not None else float('inf'),
                e['competitor'].name,
            ))
        if group:
            category_groups[category] = group

    # ── College birling seedings ────────────────────────────────────────
    birling_events_data = []
    college_birling_events = tournament.events.filter_by(
        event_type='college', scoring_type='bracket'
    ).all()
    for bev in college_birling_events:
        signed_up = _signed_up_competitors(bev)
        if not signed_up:
            continue

        # Load existing pre-seedings.
        try:
            bev_data = json.loads(bev.payouts or '{}')
        except (json.JSONDecodeError, TypeError):
            bev_data = {}
        pre_seedings = bev_data.get('pre_seedings', {})
        # pre_seedings is {comp_id_str: seed_number}
        seed_map = {int(k): v for k, v in pre_seedings.items()}

        # Group competitors by school.
        schools: dict = {}  # school_name → [comp, ...]
        for comp in signed_up:
            team = getattr(comp, 'team', None)
            school = team.school_name if team else 'Unaffiliated'
            schools.setdefault(school, []).append(comp)

        # Within each school, sort by existing seed (seeded first, then alphabetical).
        school_groups = []
        for school_name in sorted(schools.keys()):
            comps = schools[school_name]
            comps.sort(key=lambda c: (seed_map.get(c.id, 9999), c.name))
            school_groups.append({
                'school': school_name,
                'competitors': [
                    {'id': c.id, 'name': c.display_name, 'seed': seed_map.get(c.id)}
                    for c in comps
                ],
            })

        birling_events_data.append({
            'event': bev,
            'school_groups': school_groups,
            'total_competitors': len(signed_up),
        })

    return render_template(
        'scheduling/ability_rankings.html',
        tournament=tournament,
        category_groups=category_groups,
        category_display_names=CATEGORY_DISPLAY_NAMES,
        category_descriptions=CATEGORY_DESCRIPTIONS,
        birling_events_data=birling_events_data,
    )
