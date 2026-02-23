"""
Scoring routes for entering and managing results.
"""
from flask import Blueprint, render_template, redirect, url_for, flash, request
from database import db
from models import Tournament, Event, EventResult, Heat
from models.competitor import CollegeCompetitor, ProCompetitor
import config
import strings as text

scoring_bp = Blueprint('scoring', __name__)


@scoring_bp.route('/<int:tournament_id>/event/<int:event_id>/results')
def event_results(tournament_id, event_id):
    """View and enter results for an event."""
    tournament = Tournament.query.get_or_404(tournament_id)
    event = Event.query.get_or_404(event_id)

    heats = event.heats.order_by(Heat.heat_number, Heat.run_number).all()
    results = event.get_results_sorted()

    return render_template('scoring/event_results.html',
                           tournament=tournament,
                           event=event,
                           heats=heats,
                           results=results)


@scoring_bp.route('/<int:tournament_id>/heat/<int:heat_id>/enter', methods=['GET', 'POST'])
def enter_heat_results(tournament_id, heat_id):
    """Enter results for a specific heat."""
    tournament = Tournament.query.get_or_404(tournament_id)
    heat = Heat.query.get_or_404(heat_id)
    event = heat.event

    if request.method == 'POST':
        competitor_ids = heat.get_competitors()

        for comp_id in competitor_ids:
            result_value = request.form.get(f'result_{comp_id}')
            status = request.form.get(f'status_{comp_id}', 'completed')

            if result_value:
                # Find or create result
                result = EventResult.query.filter_by(
                    event_id=event.id,
                    competitor_id=comp_id
                ).first()

                if not result:
                    # Get competitor info
                    if event.event_type == 'college':
                        competitor = CollegeCompetitor.query.get(comp_id)
                    else:
                        competitor = ProCompetitor.query.get(comp_id)

                    result = EventResult(
                        event_id=event.id,
                        competitor_id=comp_id,
                        competitor_type=event.event_type,
                        competitor_name=competitor.name if competitor else f"Unknown ({comp_id})"
                    )
                    db.session.add(result)

                # Update result based on run number
                if event.requires_dual_runs:
                    if heat.run_number == 1:
                        result.run1_value = float(result_value)
                    else:
                        result.run2_value = float(result_value)
                    result.calculate_best_run()
                else:
                    result.result_value = float(result_value)

                result.status = status

        heat.status = 'completed'
        db.session.commit()

        # Check if all heats are complete to calculate final positions
        all_heats_complete = all(h.status == 'completed' for h in event.heats.all())
        if all_heats_complete and not event.requires_dual_runs:
            _calculate_positions(event)

        flash(text.FLASH['heat_saved'], 'success')
        return redirect(url_for('scoring.event_results',
                                tournament_id=tournament_id,
                                event_id=event.id))

    # Get competitor details for display
    competitor_ids = heat.get_competitors()
    competitors = []

    for comp_id in competitor_ids:
        if event.event_type == 'college':
            comp = CollegeCompetitor.query.get(comp_id)
        else:
            comp = ProCompetitor.query.get(comp_id)

        if comp:
            competitors.append({
                'id': comp_id,
                'name': comp.name,
                'stand': heat.get_stand_for_competitor(comp_id)
            })

    return render_template('scoring/enter_heat.html',
                           tournament=tournament,
                           heat=heat,
                           event=event,
                           competitors=competitors)


def _calculate_positions(event):
    """Calculate final positions and award points/payouts."""
    results = event.results.filter_by(status='completed').all()

    if not results:
        return

    # Sort based on scoring type
    if event.scoring_order == 'lowest_wins':
        results.sort(key=lambda r: r.result_value if r.result_value else float('inf'))
    else:
        results.sort(key=lambda r: r.result_value if r.result_value else 0, reverse=True)

    # Assign positions and points/payouts
    for position, result in enumerate(results, start=1):
        result.final_position = position

        if event.event_type == 'college':
            # Award points based on placement
            points = config.PLACEMENT_POINTS.get(position, 0)
            result.points_awarded = points

            # Update competitor's individual points
            competitor = CollegeCompetitor.query.get(result.competitor_id)
            if competitor:
                competitor.add_points(points)

        else:
            # Award payout based on placement
            payout = event.get_payout_for_position(position)
            result.payout_amount = payout

            # Update competitor's earnings
            competitor = ProCompetitor.query.get(result.competitor_id)
            if competitor:
                competitor.add_earnings(payout)

    event.status = 'completed'
    db.session.commit()


@scoring_bp.route('/<int:tournament_id>/event/<int:event_id>/finalize', methods=['POST'])
def finalize_event(tournament_id, event_id):
    """Finalize an event and calculate positions."""
    event = Event.query.get_or_404(event_id)

    _calculate_positions(event)

    flash(text.FLASH['event_finalized'].format(event_name=event.display_name), 'success')
    return redirect(url_for('scoring.event_results',
                            tournament_id=tournament_id,
                            event_id=event_id))


@scoring_bp.route('/<int:tournament_id>/event/<int:event_id>/payouts', methods=['GET', 'POST'])
def configure_payouts(tournament_id, event_id):
    """Configure payouts for a pro event."""
    tournament = Tournament.query.get_or_404(tournament_id)
    event = Event.query.get_or_404(event_id)

    if event.event_type != 'pro':
        flash(text.FLASH['pro_only_payouts'], 'error')
        return redirect(url_for('scheduling.event_list', tournament_id=tournament_id))

    if request.method == 'POST':
        payouts = {}
        for i in range(1, 11):  # Up to 10th place
            amount = request.form.get(f'payout_{i}')
            if amount:
                payouts[str(i)] = float(amount)

        event.set_payouts(payouts)
        db.session.commit()

        flash(text.FLASH['payouts_saved'], 'success')
        return redirect(url_for('scoring.event_results',
                                tournament_id=tournament_id,
                                event_id=event_id))

    return render_template('scoring/configure_payouts.html',
                           tournament=tournament,
                           event=event,
                           current_payouts=event.get_payouts())
