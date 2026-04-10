"""
Birling bracket management routes — seeding, bracket generation, match recording.

College birling is gender-segregated (separate men's and women's brackets).
Judges rank/seed competitors before generating the double-elimination bracket,
then record match results to advance competitors through the bracket.
"""
from flask import abort, flash, jsonify, redirect, render_template, request, url_for

from database import db
from models import Event, EventResult, Tournament
from models.competitor import CollegeCompetitor, ProCompetitor
from services.audit import log_action

from . import _signed_up_competitors, scheduling_bp


@scheduling_bp.route('/<int:tournament_id>/event/<int:event_id>/birling', methods=['GET'])
def birling_manage(tournament_id, event_id):
    """Birling bracket management page — seeding, generation, and match recording."""
    tournament = Tournament.query.get_or_404(tournament_id)
    event = Event.query.get_or_404(event_id)
    if event.tournament_id != tournament_id or event.scoring_type != 'bracket':
        abort(404)

    from services.birling_bracket import BirlingBracket
    bb = BirlingBracket(event)
    bracket_data = bb.bracket_data
    has_bracket = bool(bracket_data.get('bracket', {}).get('winners'))

    # Get competitors signed up for this event
    competitors = _signed_up_competitors(event)

    # Load existing seedings from bracket data (if any)
    existing_seeding = bracket_data.get('seeding', [])
    seed_map = {}
    if existing_seeding:
        for idx, comp_id in enumerate(existing_seeding):
            seed_map[comp_id] = idx + 1

    # Build competitor list with seed info
    comp_list = []
    for comp in competitors:
        comp_list.append({
            'id': comp.id,
            'name': comp.display_name,
            'gender': getattr(comp, 'gender', None),
            'team': getattr(comp, 'team', None),
            'seed': seed_map.get(comp.id),
        })

    # Sort by seed (seeded first ascending, then unranked alphabetically)
    comp_list.sort(key=lambda c: (c['seed'] if c['seed'] is not None else float('inf'), c['name']))

    # Build lookup for bracket display
    comp_lookup = {str(c['id']): c['name'] for c in bracket_data.get('competitors', [])}

    # Get current playable matches
    current_matches = bb.get_current_matches() if has_bracket else []
    placements = bracket_data.get('placements', {})

    # Check if bracket is complete
    total_competitors = len(bracket_data.get('competitors', []))
    is_complete = len(placements) >= total_competitors and total_competitors > 0

    return render_template(
        'scheduling/birling_manage.html',
        tournament=tournament,
        event=event,
        competitors=comp_list,
        has_bracket=has_bracket,
        bracket=bracket_data.get('bracket', {}),
        comp_lookup=comp_lookup,
        current_matches=current_matches,
        placements=placements,
        is_complete=is_complete,
        total_competitors=total_competitors,
    )


@scheduling_bp.route('/<int:tournament_id>/event/<int:event_id>/birling/generate', methods=['POST'])
def birling_generate(tournament_id, event_id):
    """Generate a new birling bracket using seeded order from form."""
    tournament = Tournament.query.get_or_404(tournament_id)
    event = Event.query.get_or_404(event_id)
    if event.tournament_id != tournament_id or event.scoring_type != 'bracket':
        abort(404)

    competitors = _signed_up_competitors(event)
    if len(competitors) < 2:
        flash('Need at least 2 competitors to generate a bracket.', 'error')
        return redirect(url_for('scheduling.birling_manage',
                                tournament_id=tournament_id, event_id=event_id))

    # Parse seed order from form: seed_{comp_id} = rank number.
    # If no manual seeds given, fall back to pre_seedings from ability rankings page.
    import json
    seed_entries = []
    has_manual_seeds = any(
        request.form.get(f'seed_{comp.id}', '').strip()
        for comp in competitors
    )

    if has_manual_seeds:
        for comp in competitors:
            raw = request.form.get(f'seed_{comp.id}', '').strip()
            if raw:
                try:
                    seed_val = int(raw)
                    if seed_val < 1:
                        raise ValueError
                    seed_entries.append((comp, seed_val))
                except (TypeError, ValueError):
                    flash(f'Invalid seed value "{raw}" for {comp.name}.', 'error')
                    return redirect(url_for('scheduling.birling_manage',
                                            tournament_id=tournament_id, event_id=event_id))
            else:
                seed_entries.append((comp, None))
    else:
        # Use pre_seedings from ability rankings if available.
        try:
            bev_data = json.loads(event.payouts or '{}')
        except (json.JSONDecodeError, TypeError):
            bev_data = {}
        pre_seedings = bev_data.get('pre_seedings', {})
        for comp in competitors:
            seed_val = pre_seedings.get(str(comp.id))
            if seed_val is not None:
                try:
                    seed_entries.append((comp, int(seed_val)))
                except (TypeError, ValueError):
                    seed_entries.append((comp, None))
            else:
                seed_entries.append((comp, None))

    # Sort: seeded first by rank, then unseeded alphabetically
    seeded = [(c, s) for c, s in seed_entries if s is not None]
    unseeded = [(c, s) for c, s in seed_entries if s is None]
    seeded.sort(key=lambda x: x[1])
    unseeded.sort(key=lambda x: x[0].name.lower())

    ordered_comps = seeded + unseeded

    comp_dicts = [{'id': c.id, 'name': c.display_name} for c, _ in ordered_comps]
    seeding = [c.id for c, _ in ordered_comps]

    from services.birling_bracket import BirlingBracket
    bb = BirlingBracket(event)
    try:
        bb.generate_bracket(comp_dicts, seeding=seeding)
    except Exception as exc:
        flash(f'Bracket generation failed: {exc}', 'error')
        return redirect(url_for('scheduling.birling_manage',
                                tournament_id=tournament_id, event_id=event_id))

    log_action('birling_bracket_generated', 'event', event_id, {
        'competitors': len(comp_dicts),
        'event_name': event.display_name,
    })
    flash(f'Bracket generated with {len(comp_dicts)} competitors.', 'success')
    return redirect(url_for('scheduling.birling_manage',
                            tournament_id=tournament_id, event_id=event_id))


@scheduling_bp.route('/<int:tournament_id>/event/<int:event_id>/birling/record', methods=['POST'])
def birling_record_match(tournament_id, event_id):
    """Record the result of a birling match."""
    tournament = Tournament.query.get_or_404(tournament_id)
    event = Event.query.get_or_404(event_id)
    if event.tournament_id != tournament_id or event.scoring_type != 'bracket':
        abort(404)

    match_id = request.form.get('match_id', '').strip()
    winner_id_raw = request.form.get('winner_id', '').strip()

    if not match_id or not winner_id_raw:
        flash('Match ID and winner are required.', 'error')
        return redirect(url_for('scheduling.birling_manage',
                                tournament_id=tournament_id, event_id=event_id))

    try:
        winner_id = int(winner_id_raw)
    except (TypeError, ValueError):
        flash('Invalid winner ID.', 'error')
        return redirect(url_for('scheduling.birling_manage',
                                tournament_id=tournament_id, event_id=event_id))

    from services.birling_bracket import BirlingBracket
    bb = BirlingBracket(event)

    try:
        bb.record_match_result(match_id, winner_id)
    except ValueError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('scheduling.birling_manage',
                                tournament_id=tournament_id, event_id=event_id))

    # Get competitor name for the flash message
    comp_lookup = {c['id']: c['name'] for c in bb.bracket_data.get('competitors', [])}
    winner_name = comp_lookup.get(winner_id, f'#{winner_id}')

    log_action('birling_match_recorded', 'event', event_id, {
        'match_id': match_id,
        'winner_id': winner_id,
        'winner_name': winner_name,
    })
    flash(f'{winner_name} wins match {match_id}.', 'success')
    return redirect(url_for('scheduling.birling_manage',
                            tournament_id=tournament_id, event_id=event_id))


@scheduling_bp.route('/<int:tournament_id>/event/<int:event_id>/birling/reset', methods=['POST'])
def birling_reset(tournament_id, event_id):
    """Reset the birling bracket (clear all data)."""
    tournament = Tournament.query.get_or_404(tournament_id)
    event = Event.query.get_or_404(event_id)
    if event.tournament_id != tournament_id or event.scoring_type != 'bracket':
        abort(404)

    event.payouts = '{}'
    db.session.commit()

    log_action('birling_bracket_reset', 'event', event_id, {
        'event_name': event.display_name,
    })
    flash('Bracket has been reset.', 'success')
    return redirect(url_for('scheduling.birling_manage',
                            tournament_id=tournament_id, event_id=event_id))


@scheduling_bp.route('/<int:tournament_id>/event/<int:event_id>/birling/finalize', methods=['POST'])
def birling_finalize(tournament_id, event_id):
    """Finalize birling bracket — write placements to EventResult records."""
    tournament = Tournament.query.get_or_404(tournament_id)
    event = Event.query.get_or_404(event_id)
    if event.tournament_id != tournament_id or event.scoring_type != 'bracket':
        abort(404)

    from services.birling_bracket import BirlingBracket
    bb = BirlingBracket(event)

    placements = bb.get_placements()
    if not placements:
        flash('No placements to finalize. Complete the bracket first.', 'error')
        return redirect(url_for('scheduling.birling_manage',
                                tournament_id=tournament_id, event_id=event_id))

    try:
        bb.finalize_to_event_results()
    except Exception as exc:
        flash(f'Finalization failed: {exc}', 'error')
        return redirect(url_for('scheduling.birling_manage',
                                tournament_id=tournament_id, event_id=event_id))

    log_action('birling_bracket_finalized', 'event', event_id, {
        'placements': len(placements),
        'event_name': event.display_name,
    })
    flash(f'Bracket finalized with {len(placements)} placements.', 'success')
    return redirect(url_for('scheduling.birling_manage',
                            tournament_id=tournament_id, event_id=event_id))
