"""
Routes for Partnered Axe Throw prelims/finals management.
"""
from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from database import db
from models import Tournament
from models.competitor import ProCompetitor
from services.cache_invalidation import invalidate_tournament_caches
from services.partnered_axe import (
    find_partnered_axe_throw,
    get_or_create_partnered_axe_throw,
)

bp = Blueprint('partnered_axe', __name__, url_prefix='/tournament/<int:tournament_id>/partnered-axe')


def _eligible_pros(tournament_id: int, event_id: int) -> list:
    """Active pros in this tournament who are actually entered in the
    Partnered Axe Throw event. Filtering here (instead of passing the
    whole active roster to the template) prevents judges from
    accidentally pairing someone who never signed up."""
    pros = ProCompetitor.query.filter_by(
        tournament_id=tournament_id,
        status='active',
    ).all()
    eligible = []
    for comp in pros:
        entered = set()
        for raw in comp.get_events_entered():
            try:
                entered.add(int(raw))
            except (TypeError, ValueError):
                continue
        if event_id in entered:
            eligible.append(comp)
    return eligible


@bp.route('/')
def dashboard(tournament_id):
    """Partnered Axe Throw dashboard.

    GET is read-only: if the tournament never configured Partnered Axe
    Throw, render a prompt to enable it rather than silently creating
    an Event row (GET-with-side-effect is the same class of bug as the
    Woodboss ghost rows — one of the reasons this branch exists).
    """
    tournament = Tournament.query.get_or_404(tournament_id)
    pat = find_partnered_axe_throw(tournament_id)

    if pat is None:
        return render_template('partnered_axe/dashboard.html',
                               tournament=tournament,
                               pat=None,
                               stage=None,
                               pairs=[],
                               available_pros=[],
                               event_missing=True)

    available_pros = _eligible_pros(tournament_id, pat.event.id)

    return render_template('partnered_axe/dashboard.html',
                         tournament=tournament,
                         pat=pat,
                         stage=pat.get_stage(),
                         pairs=pat.get_pairs(),
                         available_pros=available_pros,
                         event_missing=False)


@bp.route('/enable', methods=['POST'])
def enable(tournament_id):
    """Explicit POST to create the Partnered Axe Throw event row."""
    Tournament.query.get_or_404(tournament_id)
    get_or_create_partnered_axe_throw(tournament_id)
    invalidate_tournament_caches(tournament_id)
    flash('Partnered Axe Throw enabled for this tournament.', 'success')
    return redirect(url_for('partnered_axe.dashboard', tournament_id=tournament_id))


@bp.route('/register-pair', methods=['POST'])
def register_pair(tournament_id):
    """Register a new pair."""
    tournament = Tournament.query.get_or_404(tournament_id)
    pat = get_or_create_partnered_axe_throw(tournament_id)

    try:
        competitor1_id = int(request.form.get('competitor1_id'))
        competitor2_id = int(request.form.get('competitor2_id'))
    except (TypeError, ValueError):
        flash('Invalid competitor selection.', 'error')
        return redirect(url_for('partnered_axe.dashboard', tournament_id=tournament_id))

    if competitor1_id == competitor2_id:
        flash('Cannot pair a competitor with themselves', 'danger')
        return redirect(url_for('partnered_axe.dashboard', tournament_id=tournament_id))

    try:
        pair = pat.register_pair(competitor1_id, competitor2_id)
        invalidate_tournament_caches(tournament_id)
        flash(f'Pair registered: {pair["competitor1"]["name"]} & {pair["competitor2"]["name"]}', 'success')
    except ValueError as e:
        flash(str(e), 'danger')

    return redirect(url_for('partnered_axe.dashboard', tournament_id=tournament_id))


@bp.route('/prelims')
def prelims(tournament_id):
    """Prelims scoring page."""
    tournament = Tournament.query.get_or_404(tournament_id)
    pat = find_partnered_axe_throw(tournament_id)
    if pat is None:
        flash('Partnered Axe Throw is not enabled for this tournament.', 'warning')
        return redirect(url_for('partnered_axe.dashboard', tournament_id=tournament_id))

    return render_template('partnered_axe/prelims.html',
                         tournament=tournament,
                         pat=pat,
                         stage=pat.get_stage(),
                         pairs=pat.get_pairs(),
                         standings=pat.get_prelim_standings(),
                         can_advance=pat.can_advance_to_finals())


@bp.route('/prelims/record', methods=['POST'])
def record_prelim(tournament_id):
    """Record a prelim result."""
    tournament = Tournament.query.get_or_404(tournament_id)
    pat = get_or_create_partnered_axe_throw(tournament_id)

    try:
        pair_id = int(request.form.get('pair_id'))
        hits = int(request.form.get('hits'))
    except (TypeError, ValueError):
        flash('Invalid pair ID or hit count.', 'error')
        return redirect(url_for('partnered_axe.prelims', tournament_id=tournament_id))

    pat.record_prelim_result(pair_id, hits)
    invalidate_tournament_caches(tournament_id)
    flash(f'Prelim result recorded for Pair {pair_id}', 'success')

    # If the score was entered from the event_results page, redirect back there
    if request.form.get('return_to') == 'event_results':
        return redirect(url_for('scoring.event_results',
                                tournament_id=tournament_id,
                                event_id=pat.event.id))

    return redirect(url_for('partnered_axe.prelims', tournament_id=tournament_id))


@bp.route('/advance-to-finals', methods=['POST'])
def advance_to_finals(tournament_id):
    """Advance top 4 to finals."""
    tournament = Tournament.query.get_or_404(tournament_id)
    pat = get_or_create_partnered_axe_throw(tournament_id)

    try:
        finalists = pat.advance_to_finals()
        invalidate_tournament_caches(tournament_id)
        flash('Top 4 pairs advanced to finals!', 'success')
    except ValueError as e:
        flash(str(e), 'danger')

    return redirect(url_for('partnered_axe.finals', tournament_id=tournament_id))


@bp.route('/finals')
def finals(tournament_id):
    """Finals scoring page."""
    tournament = Tournament.query.get_or_404(tournament_id)
    pat = find_partnered_axe_throw(tournament_id)
    if pat is None:
        flash('Partnered Axe Throw is not enabled for this tournament.', 'warning')
        return redirect(url_for('partnered_axe.dashboard', tournament_id=tournament_id))

    return render_template('partnered_axe/finals.html',
                         tournament=tournament,
                         pat=pat,
                         stage=pat.get_stage(),
                         finalists=pat.get_finalists(),
                         prelim_standings=pat.get_prelim_standings())


@bp.route('/finals/record', methods=['POST'])
def record_final(tournament_id):
    """Record a final result."""
    tournament = Tournament.query.get_or_404(tournament_id)
    pat = get_or_create_partnered_axe_throw(tournament_id)

    try:
        pair_id = int(request.form.get('pair_id'))
        hits = int(request.form.get('hits'))
    except (TypeError, ValueError):
        flash('Invalid pair ID or hit count.', 'error')
        return redirect(url_for('partnered_axe.finals', tournament_id=tournament_id))

    pat.record_final_result(pair_id, hits)
    invalidate_tournament_caches(tournament_id)

    if pat.get_stage() == 'completed':
        flash('Finals complete! Final standings are now available.', 'success')
        return redirect(url_for('partnered_axe.results', tournament_id=tournament_id))

    flash(f'Final result recorded for Pair {pair_id}', 'success')
    return redirect(url_for('partnered_axe.finals', tournament_id=tournament_id))


@bp.route('/results')
def results(tournament_id):
    """Final results page."""
    tournament = Tournament.query.get_or_404(tournament_id)
    pat = find_partnered_axe_throw(tournament_id)
    if pat is None:
        flash('Partnered Axe Throw is not enabled for this tournament.', 'warning')
        return redirect(url_for('partnered_axe.dashboard', tournament_id=tournament_id))

    return render_template('partnered_axe/results.html',
                         tournament=tournament,
                         pat=pat,
                         stage=pat.get_stage(),
                         standings=pat.get_full_standings(),
                         finalists=pat.get_finalists())


@bp.route('/reset', methods=['POST'])
def reset(tournament_id):
    """Reset the event."""
    tournament = Tournament.query.get_or_404(tournament_id)
    pat = find_partnered_axe_throw(tournament_id)
    if pat is None:
        flash('Partnered Axe Throw is not enabled for this tournament.', 'warning')
        return redirect(url_for('partnered_axe.dashboard', tournament_id=tournament_id))

    pat.reset()
    invalidate_tournament_caches(tournament_id)
    flash('Partnered Axe Throw has been reset', 'warning')

    return redirect(url_for('partnered_axe.dashboard', tournament_id=tournament_id))


# API endpoints
@bp.route('/api/status')
def api_status(tournament_id):
    """Get event status as JSON."""
    tournament = Tournament.query.get_or_404(tournament_id)
    pat = find_partnered_axe_throw(tournament_id)
    if pat is None:
        return jsonify({
            'enabled': False,
            'stage': None,
            'pairs': [],
            'prelim_standings': [],
            'finalists': [],
            'can_advance': False,
        })

    return jsonify({
        'enabled': True,
        'stage': pat.get_stage(),
        'pairs': pat.get_pairs(),
        'prelim_standings': pat.get_prelim_standings(),
        'finalists': pat.get_finalists(),
        'can_advance': pat.can_advance_to_finals()
    })
