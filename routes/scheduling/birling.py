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
from services.birling_print import build_birling_print_context
from services.print_response import weasyprint_or_html

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

    # Compute which decided matches can be undone (no downstream play)
    undoable_match_ids = bb.get_undoable_matches() if has_bracket else set()

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
        undoable_match_ids=undoable_match_ids,
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


@scheduling_bp.route('/<int:tournament_id>/event/<int:event_id>/birling/fall', methods=['POST'])
def birling_record_fall(tournament_id, event_id):
    """Record a single fall in a best-of-3 birling match."""
    tournament = Tournament.query.get_or_404(tournament_id)
    event = Event.query.get_or_404(event_id)
    if event.tournament_id != tournament_id or event.scoring_type != 'bracket':
        abort(404)

    match_id = request.form.get('match_id', '').strip()
    fall_winner_raw = request.form.get('fall_winner_id', '').strip()

    if not match_id or not fall_winner_raw:
        flash('Match ID and fall winner are required.', 'error')
        return redirect(url_for('scheduling.birling_manage',
                                tournament_id=tournament_id, event_id=event_id))

    try:
        fall_winner_id = int(fall_winner_raw)
    except (TypeError, ValueError):
        flash('Invalid fall winner ID.', 'error')
        return redirect(url_for('scheduling.birling_manage',
                                tournament_id=tournament_id, event_id=event_id))

    from services.birling_bracket import BirlingBracket
    bb = BirlingBracket(event)

    try:
        result = bb.record_fall(match_id, fall_winner_id)
    except ValueError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('scheduling.birling_manage',
                                tournament_id=tournament_id, event_id=event_id))

    comp_lookup = {c['id']: c['name'] for c in bb.bracket_data.get('competitors', [])}
    fall_winner_name = comp_lookup.get(fall_winner_id, f'#{fall_winner_id}')

    if result['match_decided']:
        winner_name = comp_lookup.get(result['winner'], f'#{result["winner"]}')
        flash(f'{winner_name} wins match {match_id} (2-{_fall_loser_count(result)}).', 'success')
    else:
        c1 = bb._find_match(match_id)['competitor1']
        c2 = bb._find_match(match_id)['competitor2']
        c1_name = comp_lookup.get(c1, f'#{c1}')
        c2_name = comp_lookup.get(c2, f'#{c2}')
        c1_falls = sum(1 for f in result['falls'] if f['winner'] == c1)
        c2_falls = sum(1 for f in result['falls'] if f['winner'] == c2)
        flash(
            f'Fall {len(result["falls"])} recorded. '
            f'Score: {c1_name} {c1_falls} - {c2_name} {c2_falls}',
            'info'
        )

    log_action('birling_fall_recorded', 'event', event_id, {
        'match_id': match_id,
        'fall_number': len(result['falls']),
        'fall_winner_id': fall_winner_id,
        'fall_winner_name': fall_winner_name,
        'match_decided': result['match_decided'],
    })
    return redirect(url_for('scheduling.birling_manage',
                            tournament_id=tournament_id, event_id=event_id))


def _fall_loser_count(result):
    """Count falls won by the losing competitor."""
    winner = result['winner']
    return sum(1 for f in result['falls'] if f['winner'] != winner)


@scheduling_bp.route('/<int:tournament_id>/event/<int:event_id>/birling/undo', methods=['POST'])
def birling_undo_match(tournament_id, event_id):
    """Undo a birling match result — return both competitors to the match."""
    tournament = Tournament.query.get_or_404(tournament_id)
    event = Event.query.get_or_404(event_id)
    if event.tournament_id != tournament_id or event.scoring_type != 'bracket':
        abort(404)

    match_id = request.form.get('match_id', '').strip()
    if not match_id:
        flash('Match ID is required.', 'error')
        return redirect(url_for('scheduling.birling_manage',
                                tournament_id=tournament_id, event_id=event_id))

    from services.birling_bracket import BirlingBracket
    bb = BirlingBracket(event)

    # Capture previous state for audit log
    match = bb._find_match(match_id)
    prev_winner = match['winner'] if match else None
    prev_loser = match['loser'] if match else None

    try:
        result = bb.undo_match_result(match_id)
    except ValueError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('scheduling.birling_manage',
                                tournament_id=tournament_id, event_id=event_id))

    comp_lookup = {c['id']: c['name'] for c in bb.bracket_data.get('competitors', [])}

    log_action('birling_match_undone', 'event', event_id, {
        'match_id': match_id,
        'previous_winner': prev_winner,
        'previous_winner_name': comp_lookup.get(prev_winner, '?') if prev_winner else None,
        'previous_loser': prev_loser,
        'previous_loser_name': comp_lookup.get(prev_loser, '?') if prev_loser else None,
    })
    flash(f'Match {match_id} result undone.', 'success')
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


# ---------------------------------------------------------------------------
# Blank bracket print (show-prep)
# ---------------------------------------------------------------------------


def _safe_filename_part(name: str) -> str:
    """Strip characters that break Content-Disposition filenames."""
    return ''.join(c if c.isalnum() or c in ('-', '_') else '_' for c in name)


@scheduling_bp.route('/<int:tournament_id>/event/<int:event_id>/birling/print-blank',
                      methods=['GET'])
def birling_print_blank(tournament_id, event_id):
    """Printable blank bracket for one birling event.

    Round-1 matchups are shown (so the judge knows who faces whom first);
    everything beyond is blank so the judge can hand-fill advancement as
    matches play out.  If the bracket has not been generated yet, flash
    a redirect back to the seeding page.
    """
    tournament = Tournament.query.get_or_404(tournament_id)
    event = Event.query.get_or_404(event_id)
    if event.tournament_id != tournament_id or event.scoring_type != 'bracket':
        abort(404)

    ctx = build_birling_print_context(event)
    if ctx is None:
        flash('Seed the bracket first, then come back to print it.', 'warning')
        return redirect(url_for('scheduling.birling_manage',
                                tournament_id=tournament_id, event_id=event_id))

    html = render_template(
        'scoring/birling_bracket_print.html',
        brackets=[{'event': event, 'ctx': ctx}],
        year=tournament.year,
    )
    filename = f'birling_blank_{_safe_filename_part(event.display_name)}'
    log_action('birling_blank_bracket_printed', 'event', event_id, {
        'event_name': event.display_name,
    })
    db.session.commit()
    return weasyprint_or_html(html, filename)


@scheduling_bp.route('/<int:tournament_id>/birling/print-all', methods=['GET'])
def birling_print_all(tournament_id):
    """Combined blank-bracket print for every birling event in the tournament.

    Skips any bracket event that has not been generated yet and flashes
    the list of skipped event names so the admin can seed them.  Matches
    the ``judge_sheets_all`` idiom: one click, one document, show-prep
    ready.
    """
    tournament = Tournament.query.get_or_404(tournament_id)
    events = (
        Event.query
        .filter_by(tournament_id=tournament_id)
        .filter(Event.scoring_type == 'bracket')
        .order_by(Event.event_type, Event.name, Event.gender)
        .all()
    )
    if not events:
        flash('No birling events configured for this tournament.', 'warning')
        return redirect(url_for('main.tournament_detail',
                                tournament_id=tournament_id))

    rendered: list = []
    skipped_names: list = []
    for event in events:
        ctx = build_birling_print_context(event)
        if ctx is None:
            skipped_names.append(event.display_name)
            continue
        rendered.append({'event': event, 'ctx': ctx})

    if not rendered:
        flash(
            'No birling brackets have been seeded yet: {}.  Seed at least one to print.'
            .format(', '.join(skipped_names)),
            'warning',
        )
        return redirect(url_for('main.tournament_detail',
                                tournament_id=tournament_id))

    if skipped_names:
        flash(
            'Skipped {} birling event(s) without a generated bracket: {}.'
            .format(len(skipped_names), ', '.join(skipped_names)),
            'info',
        )

    html = render_template(
        'scoring/birling_bracket_print.html',
        brackets=rendered,
        year=tournament.year,
    )
    filename = f'birling_blank_all_tournament_{tournament.id}'
    log_action('birling_blank_bracket_printed_all', 'tournament', tournament_id, {
        'rendered_count': len(rendered),
        'skipped': skipped_names,
    })
    db.session.commit()
    return weasyprint_or_html(html, filename)
