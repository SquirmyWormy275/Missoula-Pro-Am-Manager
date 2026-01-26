"""
Routes for validation and data integrity checks.
"""
from flask import Blueprint, render_template, request, jsonify
from models import Tournament
from services.validation import (
    TournamentValidator,
    TeamValidator,
    CollegeCompetitorValidator,
    ProCompetitorValidator,
    validate_tournament
)

bp = Blueprint('validation', __name__, url_prefix='/tournament/<int:tournament_id>/validation')


@bp.route('/')
def validation_dashboard(tournament_id):
    """Validation dashboard showing all checks."""
    tournament = Tournament.query.get_or_404(tournament_id)

    # Run all validations
    results = TournamentValidator.validate_full(tournament_id)

    return render_template('validation/dashboard.html',
                         tournament=tournament,
                         college_result=results['college'],
                         pro_result=results['pro'])


@bp.route('/college')
def college_validation(tournament_id):
    """Detailed college validation results."""
    tournament = Tournament.query.get_or_404(tournament_id)
    result = TournamentValidator.validate_college(tournament_id)

    return render_template('validation/college.html',
                         tournament=tournament,
                         result=result)


@bp.route('/pro')
def pro_validation(tournament_id):
    """Detailed pro validation results."""
    tournament = Tournament.query.get_or_404(tournament_id)
    result = TournamentValidator.validate_pro(tournament_id)

    return render_template('validation/pro.html',
                         tournament=tournament,
                         result=result)


# API endpoints
@bp.route('/api/full')
def api_full_validation(tournament_id):
    """Get full validation results as JSON."""
    return jsonify(validate_tournament(tournament_id))


@bp.route('/api/college')
def api_college_validation(tournament_id):
    """Get college validation results as JSON."""
    result = TournamentValidator.validate_college(tournament_id)
    return jsonify(result.to_dict())


@bp.route('/api/pro')
def api_pro_validation(tournament_id):
    """Get pro validation results as JSON."""
    result = TournamentValidator.validate_pro(tournament_id)
    return jsonify(result.to_dict())


@bp.route('/api/status')
def api_validation_status(tournament_id):
    """Quick validation status check."""
    results = TournamentValidator.validate_full(tournament_id)

    return jsonify({
        'college_valid': results['college'].is_valid,
        'college_errors': len(results['college'].errors),
        'college_warnings': len(results['college'].warnings),
        'pro_valid': results['pro'].is_valid,
        'pro_errors': len(results['pro'].errors),
        'pro_warnings': len(results['pro'].warnings),
        'overall_valid': results['college'].is_valid and results['pro'].is_valid
    })
