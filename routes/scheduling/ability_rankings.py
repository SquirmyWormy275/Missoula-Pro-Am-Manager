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
from services import birling_rows
from services.audit import log_action
from services.cache_invalidation import invalidate_tournament_caches
from services.flight_builder import (
    lock_tournament_schedule,
    serialize_sqlite_schedule_writer,
)

from . import _competitor_entered_event, _signed_up_competitors, scheduling_bp

# ---------------------------------------------------------------------------
# Ability Rankings — per-event judge-assigned ranks for heat snake-draft sort
# ---------------------------------------------------------------------------

@scheduling_bp.route('/<int:tournament_id>/pro/ability-rankings', methods=['GET', 'POST'])
@serialize_sqlite_schedule_writer
def ability_rankings(tournament_id):
    """View and set per-event ability rankings for pro competitors."""
    from models.pro_event_rank import (
        CATEGORY_DESCRIPTIONS,
        CATEGORY_DISPLAY_NAMES,
        RANKED_CATEGORIES,
        ProEventRank,
    )

    tournament = db.get_or_404(Tournament, tournament_id)

    if request.method == 'POST':
        tournament = lock_tournament_schedule(tournament)
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
        for (category, _gender), comp_ids in ordered_lists.items():
            for position, comp_id in enumerate(comp_ids, start=1):
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

        # Delete ranks for competitors dragged out of a Ranked zone.
        #
        # BUG HISTORY, two layers deep.
        #
        # First layer, fixed in V2.14.15: this used `if ranked_ids else True`
        # as the filter clause. Passing Python True makes SQLAlchemy emit
        # WHERE TRUE, which SILENTLY WIPED every rank in that category whenever
        # the form submitted an empty Ranked zone. The user's symptom was "I set
        # my rankings, save, and they go back to whatever your preset was."
        # Root cause: the form always submits every rendered Ranked zone's
        # hidden input, and empty ones were treated as "clear everything."
        #
        # Second layer, and the reason that fix was not enough: it built ONE
        # ranked set per category by unioning every gender's list, and skipped
        # the cleanup only when that whole union was empty. But a rank is stored
        # on (tournament_id, competitor_id, event_category) with NO gender,
        # while the form submits one order_<category>_<gender> list PER GENDER.
        # So a non-empty men's list made the union non-empty, the cleanup ran
        # across the entire category, and every woman's rank in it was deleted.
        # Measured on the real 2026 data: seeding four men and four women in
        # `underhand` and saving the men's ladder with an empty women's zone
        # deleted Kate Page, Brianna Kvinge, Chrissy Marcellus and Emma Macon.
        # Five of the seven ranked categories on that dataset carry both
        # genders, so this was the common case, not a corner.
        #
        # Fix: scope each cleanup pass to the gender zone its list came from. A
        # submitted list may only delete ranks belonging to competitors the GET
        # handler would have rendered in THAT zone, resolved the same way it
        # resolves gender_key. An empty Ranked zone still deletes nothing, so
        # the first-layer fix is preserved; mass-clearing a ladder still means
        # unranking each competitor explicitly.
        pro_gender: dict = dict(
            db.session.query(ProCompetitor.id, ProCompetitor.gender)
            .filter(ProCompetitor.tournament_id == tournament_id)
            .all()
        )

        def _zone_of(category, comp_id):
            """Which order_<category>_<gender> zone a competitor is rendered in.

            Mirrors the gender_key branch in the GET handler below: Jack & Jill
            is mixed so everyone shares one 'open' ladder, and a competitor with
            no recorded gender also falls to 'open'.
            """
            if category == 'jack_jill':
                return 'open'
            g = pro_gender.get(comp_id)
            return g if g in ('M', 'F') else 'open'

        for (category, gender_key), ranked_ids in ordered_lists.items():
            if not ranked_ids:
                continue
            stale = ProEventRank.query.filter(
                ProEventRank.tournament_id == tournament_id,
                ProEventRank.event_category == category,
                ~ProEventRank.competitor_id.in_(ranked_ids),
            ).all()
            for r in stale:
                if _zone_of(category, r.competitor_id) != gender_key:
                    continue
                db.session.delete(r)
                deleted_count += 1

        # ── College birling seedings ────────────────────────────────────
        birling_saved = 0
        birling_refused = []
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
            try:
                birling_rows.replace_pre_seedings(bev, pre_seedings)
            except birling_rows.ProjectionRefused as exc:
                birling_refused.append((bev, exc))
            else:
                birling_saved += len(pre_seedings)

        db.session.commit()
        invalidate_tournament_caches(tournament_id)
        log_action('ability_rankings_saved', 'tournament', tournament_id, {
            'saved': saved_count,
            'cleared': deleted_count,
            'birling_saved': birling_saved,
        })

        total_saved = saved_count + birling_saved
        flash(f'Rankings saved ({total_saved} set, {deleted_count} cleared).', 'success')
        if birling_refused:
            flash(
                'Some Birling seedings could not be saved: '
                + '; '.join(
                    '%s (%s)' % (bev.display_name, ', '.join(exc.reasons))
                    for bev, exc in birling_refused
                )
                + '. Correct the listed issue and save again.',
                'warning',
            )
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

        # Pre-seeds are table-native Birling state; this page is their writer.
        pre_seedings = birling_rows.load_pre_seedings(bev)
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
