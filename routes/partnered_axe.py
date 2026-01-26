"""
Routes for Partnered Axe Throw prelims/finals management.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from database import db
from models import Tournament
from models.competitor import ProCompetitor
from services.partnered_axe import get_partnered_axe_throw

bp = Blueprint('partnered_axe', __name__, url_prefix='/tournament/<int:tournament_id>/partnered-axe')


@bp.route('/')
def dashboard(tournament_id):
    """Partnered Axe Throw dashboard."""
    tournament = Tournament.query.get_or_404(tournament_id)
    pat = get_partnered_axe_throw(tournament_id)

    # Get available pro competitors for pair registration
    available_pros = ProCompetitor.query.filter_by(
        tournament_id=tournament_id,
        status='active'
    ).all()

    return render_template('partnered_axe/dashboard.html',
                         tournament=tournament,
                         pat=pat,
                         stage=pat.get_stage(),
                         pairs=pat.get_pairs(),
                         available_pros=available_pros)


@bp.route('/register-pair', methods=['POST'])
def register_pair(tournament_id):
    """Register a new pair."""
    tournament = Tournament.query.get_or_404(tournament_id)
    pat = get_partnered_axe_throw(tournament_id)

    competitor1_id = int(request.form.get('competitor1_id'))
    competitor2_id = int(request.form.get('competitor2_id'))

    if competitor1_id == competitor2_id:
        flash('Cannot pair a competitor with themselves', 'danger')
        return redirect(url_for('partnered_axe.dashboard', tournament_id=tournament_id))

    try:
        pair = pat.register_pair(competitor1_id, competitor2_id)
        flash(f'Pair registered: {pair["competitor1"]["name"]} & {pair["competitor2"]["name"]}', 'success')
    except ValueError as e:
        flash(str(e), 'danger')

    return redirect(url_for('partnered_axe.dashboard', tournament_id=tournament_id))


@bp.route('/prelims')
def prelims(tournament_id):
    """Prelims scoring page."""
    tournament = Tournament.query.get_or_404(tournament_id)
    pat = get_partnered_axe_throw(tournament_id)

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
    pat = get_partnered_axe_throw(tournament_id)

    pair_id = int(request.form.get('pair_id'))
    hits = int(request.form.get('hits'))

    pat.record_prelim_result(pair_id, hits)
    flash(f'Prelim result recorded for Pair {pair_id}', 'success')

    return redirect(url_for('partnered_axe.prelims', tournament_id=tournament_id))


@bp.route('/advance-to-finals', methods=['POST'])
def advance_to_finals(tournament_id):
    """Advance top 4 to finals."""
    tournament = Tournament.query.get_or_404(tournament_id)
    pat = get_partnered_axe_throw(tournament_id)

    try:
        finalists = pat.advance_to_finals()
        flash(f'Top 4 pairs advanced to finals!', 'success')
    except ValueError as e:
        flash(str(e), 'danger')

    return redirect(url_for('partnered_axe.finals', tournament_id=tournament_id))


@bp.route('/finals')
def finals(tournament_id):
    """Finals scoring page."""
    tournament = Tournament.query.get_or_404(tournament_id)
    pat = get_partnered_axe_throw(tournament_id)

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
    pat = get_partnered_axe_throw(tournament_id)

    pair_id = int(request.form.get('pair_id'))
    hits = int(request.form.get('hits'))

    pat.record_final_result(pair_id, hits)

    if pat.get_stage() == 'completed':
        flash('Finals complete! Final standings are now available.', 'success')
        return redirect(url_for('partnered_axe.results', tournament_id=tournament_id))

    flash(f'Final result recorded for Pair {pair_id}', 'success')
    return redirect(url_for('partnered_axe.finals', tournament_id=tournament_id))


@bp.route('/results')
def results(tournament_id):
    """Final results page."""
    tournament = Tournament.query.get_or_404(tournament_id)
    pat = get_partnered_axe_throw(tournament_id)

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
    pat = get_partnered_axe_throw(tournament_id)

    pat.reset()
    flash('Partnered Axe Throw has been reset', 'warning')

    return redirect(url_for('partnered_axe.dashboard', tournament_id=tournament_id))


# API endpoints
@bp.route('/api/status')
def api_status(tournament_id):
    """Get event status as JSON."""
    tournament = Tournament.query.get_or_404(tournament_id)
    pat = get_partnered_axe_throw(tournament_id)

    return jsonify({
        'stage': pat.get_stage(),
        'pairs': pat.get_pairs(),
        'prelim_standings': pat.get_prelim_standings(),
        'finalists': pat.get_finalists(),
        'can_advance': pat.can_advance_to_finals()
    })
