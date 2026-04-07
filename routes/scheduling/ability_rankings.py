"""
Pro ability rankings route — judge-assigned per-event ranks for heat snake-draft sort.
"""
from flask import flash, redirect, render_template, request, url_for

from config import event_rank_category as _event_rank_category
from database import db
from models import Event, Tournament
from models.competitor import ProCompetitor
from services.audit import log_action

from . import scheduling_bp

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
        # Parse rank_{category}_{competitor_id} fields and upsert ProEventRank rows.
        saved_count = 0
        deleted_count = 0
        for key, raw_val in request.form.items():
            if not key.startswith('rank_'):
                continue
            parts = key.split('_', 2)
            if len(parts) != 3:
                continue
            _, category, comp_id_str = parts
            if category not in RANKED_CATEGORIES:
                continue
            try:
                competitor_id = int(comp_id_str)
            except (TypeError, ValueError):
                continue

            raw_val = raw_val.strip()
            if not raw_val:
                # Blank field — delete existing rank if present.
                existing = ProEventRank.query.filter_by(
                    tournament_id=tournament_id,
                    competitor_id=competitor_id,
                    event_category=category,
                ).first()
                if existing:
                    db.session.delete(existing)
                    deleted_count += 1
                continue

            try:
                rank = int(raw_val)
                if rank < 1:
                    raise ValueError
            except (TypeError, ValueError):
                flash(f'Invalid rank value "{raw_val}" for category {category} — must be a positive integer.', 'error')
                continue

            existing = ProEventRank.query.filter_by(
                tournament_id=tournament_id,
                competitor_id=competitor_id,
                event_category=category,
            ).first()
            if existing:
                existing.rank = rank
            else:
                db.session.add(ProEventRank(
                    tournament_id=tournament_id,
                    competitor_id=competitor_id,
                    event_category=category,
                    rank=rank,
                ))
            saved_count += 1

        db.session.commit()
        log_action('ability_rankings_saved', 'tournament', tournament_id, {
            'saved': saved_count,
            'cleared': deleted_count,
        })
        flash(f'Ability rankings saved ({saved_count} set, {deleted_count} cleared).', 'success')
        return redirect(url_for('scheduling.ability_rankings', tournament_id=tournament_id))

    # GET — build display data.
    # Find which ranked categories have at least one pro event for this tournament.
    pro_events = tournament.events.filter_by(event_type='pro').all()
    category_event_map: dict = {}
    for event in pro_events:
        cat = _event_rank_category(event)
        if cat:
            category_event_map.setdefault(cat, []).append(event)

    # Load existing ranks for this tournament.
    existing_ranks = ProEventRank.query.filter_by(tournament_id=tournament_id).all()
    rank_map: dict = {
        (r.competitor_id, r.event_category): r.rank for r in existing_ranks
    }

    # Build category_groups: {category: {'M': [...], 'F': [...], 'open': [...]}}
    # Each entry is {competitor, rank (or None)}.
    category_groups: dict = {}
    for category, cat_events in category_event_map.items():
        genders_seen: dict = {}  # gender -> set of competitor ids already added
        group: dict = {}
        for event in cat_events:
            gender_key = event.gender if event.gender else 'open'
            seen = genders_seen.setdefault(gender_key, set())
            comps = ProCompetitor.query.filter_by(
                tournament_id=tournament_id,
                status='active',
            )
            if event.gender:
                comps = comps.filter_by(gender=event.gender)
            comps = comps.order_by(ProCompetitor.name).all()
            for comp in comps:
                if comp.id in seen:
                    continue
                seen.add(comp.id)
                group.setdefault(gender_key, []).append({
                    'competitor': comp,
                    'rank': rank_map.get((comp.id, category)),
                })
        # Sort each gender group by current rank (ranked first, then unranked alphabetically).
        for gk in group:
            group[gk].sort(key=lambda e: (
                e['rank'] if e['rank'] is not None else float('inf'),
                e['competitor'].name,
            ))
        if group:
            category_groups[category] = group

    return render_template(
        'scheduling/ability_rankings.html',
        tournament=tournament,
        category_groups=category_groups,
        category_display_names=CATEGORY_DISPLAY_NAMES,
        category_descriptions=CATEGORY_DESCRIPTIONS,
    )
