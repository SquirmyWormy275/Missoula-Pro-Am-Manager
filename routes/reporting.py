"""
Reporting routes for standings, results, and exports.
"""
from flask import Blueprint, render_template, Response
from models import Tournament, Event

reporting_bp = Blueprint('reporting', __name__)


@reporting_bp.route('/<int:tournament_id>/college/standings')
def college_standings(tournament_id):
    """View college standings (Bull/Belle of Woods and Team Standings)."""
    tournament = Tournament.query.get_or_404(tournament_id)

    bull = tournament.get_bull_of_woods(10)
    belle = tournament.get_belle_of_woods(10)
    team_standings = tournament.get_team_standings()

    return render_template('reports/college_standings.html',
                           tournament=tournament,
                           bull=bull,
                           belle=belle,
                           team_standings=team_standings)


@reporting_bp.route('/<int:tournament_id>/college/standings/print')
def college_standings_print(tournament_id):
    """Printable version of college standings."""
    tournament = Tournament.query.get_or_404(tournament_id)

    bull = tournament.get_bull_of_woods(5)
    belle = tournament.get_belle_of_woods(5)
    team_standings = tournament.get_team_standings()[:5]

    return render_template('reports/college_standings_print.html',
                           tournament=tournament,
                           bull=bull,
                           belle=belle,
                           team_standings=team_standings)


@reporting_bp.route('/<int:tournament_id>/event/<int:event_id>/results')
def event_results_report(tournament_id, event_id):
    """View detailed event results."""
    tournament = Tournament.query.get_or_404(tournament_id)
    event = Event.query.get_or_404(event_id)

    results = event.get_results_sorted()

    return render_template('reports/event_results.html',
                           tournament=tournament,
                           event=event,
                           results=results)


@reporting_bp.route('/<int:tournament_id>/event/<int:event_id>/results/print')
def event_results_print(tournament_id, event_id):
    """Printable version of event results."""
    tournament = Tournament.query.get_or_404(tournament_id)
    event = Event.query.get_or_404(event_id)

    results = event.get_results_sorted()

    return render_template('reports/event_results_print.html',
                           tournament=tournament,
                           event=event,
                           results=results)


@reporting_bp.route('/<int:tournament_id>/pro/payouts')
def pro_payout_summary(tournament_id):
    """View pro competitor payout summary."""
    tournament = Tournament.query.get_or_404(tournament_id)

    competitors = tournament.pro_competitors.filter_by(status='active').all()

    # Sort by total earnings
    competitors = sorted(competitors, key=lambda c: c.total_earnings, reverse=True)

    total_paid = sum(c.total_earnings for c in competitors)

    return render_template('reports/payout_summary.html',
                           tournament=tournament,
                           competitors=competitors,
                           total_paid=total_paid)


@reporting_bp.route('/<int:tournament_id>/pro/payouts/print')
def pro_payout_summary_print(tournament_id):
    """Printable version of payout summary."""
    tournament = Tournament.query.get_or_404(tournament_id)

    competitors = tournament.pro_competitors.filter_by(status='active').all()
    competitors = sorted(competitors, key=lambda c: c.total_earnings, reverse=True)

    # Filter to only those with earnings
    competitors = [c for c in competitors if c.total_earnings > 0]

    total_paid = sum(c.total_earnings for c in competitors)

    return render_template('reports/payout_summary_print.html',
                           tournament=tournament,
                           competitors=competitors,
                           total_paid=total_paid)


@reporting_bp.route('/<int:tournament_id>/all-results')
def all_results(tournament_id):
    """View all event results for the tournament."""
    tournament = Tournament.query.get_or_404(tournament_id)

    college_events = tournament.events.filter_by(event_type='college', status='completed').all()
    pro_events = tournament.events.filter_by(event_type='pro', status='completed').all()

    return render_template('reports/all_results.html',
                           tournament=tournament,
                           college_events=college_events,
                           pro_events=pro_events)


@reporting_bp.route('/<int:tournament_id>/all-results/print')
def all_results_print(tournament_id):
    """Printable version of all results."""
    tournament = Tournament.query.get_or_404(tournament_id)

    college_events = tournament.events.filter_by(event_type='college', status='completed').all()
    pro_events = tournament.events.filter_by(event_type='pro', status='completed').all()

    return render_template('reports/all_results_print.html',
                           tournament=tournament,
                           college_events=college_events,
                           pro_events=pro_events)
