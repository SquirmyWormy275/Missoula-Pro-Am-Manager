"""
Scoring routes for entering and managing results.
"""
from flask import Blueprint, render_template, redirect, url_for, flash, request, abort, jsonify
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from database import db
from models import Tournament, Event, EventResult, Heat
from models.competitor import CollegeCompetitor, ProCompetitor
import config
import strings as text
from services.audit import log_action
from services.cache_invalidation import invalidate_tournament_caches

scoring_bp = Blueprint('scoring', __name__)


def _event_for_tournament_or_404(tournament_id: int, event_id: int) -> Event:
    """Load event and ensure it belongs to the URL tournament."""
    event = Event.query.get_or_404(event_id)
    if event.tournament_id != tournament_id:
        abort(404)
    return event


def _heat_for_tournament_or_404(tournament_id: int, heat_id: int) -> Heat:
    """Load heat and ensure its event belongs to the URL tournament."""
    heat = Heat.query.get_or_404(heat_id)
    if not heat.event or heat.event.tournament_id != tournament_id:
        abort(404)
    return heat


def _is_async_save_request() -> bool:
    """Detect frontend save calls that expect JSON instead of redirect/flash."""
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def _save_heat_results_submission(tournament_id: int, heat: Heat, event: Event):
    """Apply posted heat results and return response metadata for UI handling."""
    competitor_ids = [int(cid) for cid in heat.get_competitors()]
    posted_heat_version = request.form.get('heat_version', type=int)
    if posted_heat_version is None or posted_heat_version != heat.version_id:
        return {
            'ok': False,
            'category': 'error',
            'message': 'This heat changed in another session. Reload and re-enter results.',
            'redirect_url': url_for('scoring.enter_heat_results', tournament_id=tournament_id, heat_id=heat.id),
            'status_code': 409,
        }

    existing_results = EventResult.query.filter(
        EventResult.event_id == event.id,
        EventResult.competitor_id.in_(competitor_ids),
        EventResult.competitor_type == event.event_type
    ).all()
    result_by_competitor = {row.competitor_id: row for row in existing_results}

    if event.event_type == 'college':
        competitors = CollegeCompetitor.query.filter(CollegeCompetitor.id.in_(competitor_ids)).all()
    else:
        competitors = ProCompetitor.query.filter(ProCompetitor.id.in_(competitor_ids)).all()
    competitor_by_id = {c.id: c for c in competitors}

    changes_written = 0
    invalid_entries = []
    try:
        for comp_id in competitor_ids:
            result_value = request.form.get(f'result_{comp_id}')
            status = request.form.get(f'status_{comp_id}', 'completed')
            if not result_value:
                continue

            try:
                parsed_value = float(result_value)
            except (TypeError, ValueError):
                invalid_entries.append((comp_id, result_value))
                continue

            result = result_by_competitor.get(comp_id)
            if not result:
                competitor = competitor_by_id.get(comp_id)
                result = EventResult(
                    event_id=event.id,
                    competitor_id=comp_id,
                    competitor_type=event.event_type,
                    competitor_name=competitor.name if competitor else f'Unknown ({comp_id})'
                )
                db.session.add(result)
                result_by_competitor[comp_id] = result

            if event.requires_dual_runs:
                if heat.run_number == 1:
                    result.run1_value = parsed_value
                else:
                    result.run2_value = parsed_value
                result.calculate_best_run()
            else:
                result.result_value = parsed_value

            result.status = status
            changes_written += 1

        if changes_written == 0:
            return {
                'ok': False,
                'category': 'warning',
                'message': 'No result values were entered; heat remains pending.',
                'redirect_url': url_for('scoring.enter_heat_results', tournament_id=tournament_id, heat_id=heat.id),
                'status_code': 400,
            }

        heat.status = 'completed'

        all_heats_complete = all(h.status == 'completed' for h in event.heats.all())
        if all_heats_complete and not event.requires_dual_runs:
            _calculate_positions(event)

        log_action(
            action='heat_results_saved',
            entity_type='heat',
            entity_id=heat.id,
            details={'event_id': event.id, 'result_updates': changes_written}
        )
        db.session.commit()
    except (StaleDataError, IntegrityError):
        db.session.rollback()
        return {
            'ok': False,
            'category': 'error',
            'message': 'Concurrent edit detected while saving results. Reload and try again.',
            'redirect_url': url_for('scoring.enter_heat_results', tournament_id=tournament_id, heat_id=heat.id),
            'status_code': 409,
        }

    invalidate_tournament_caches(tournament_id)
    if invalid_entries:
        return {
            'ok': True,
            'category': 'warning',
            'message': f"Heat saved with {len(invalid_entries)} invalid value(s) skipped.",
            'redirect_url': url_for('scoring.event_results', tournament_id=tournament_id, event_id=event.id),
            'status_code': 200,
        }
    return {
        'ok': True,
        'category': 'success',
        'message': text.FLASH['heat_saved'],
        'redirect_url': url_for('scoring.event_results', tournament_id=tournament_id, event_id=event.id),
        'status_code': 200,
    }


@scoring_bp.route('/<int:tournament_id>/event/<int:event_id>/next-heat')
def next_unscored_heat(tournament_id, event_id):
    """Redirect to the first unscored heat for an event."""
    event = _event_for_tournament_or_404(tournament_id, event_id)
    heat = Heat.query.filter_by(event_id=event.id, status='pending') \
                     .order_by(Heat.heat_number, Heat.run_number).first()
    if heat:
        return redirect(url_for('scoring.enter_heat_results',
                                tournament_id=tournament_id, heat_id=heat.id))
    return redirect(url_for('scoring.event_results',
                            tournament_id=tournament_id, event_id=event_id))


@scoring_bp.route('/<int:tournament_id>/event/<int:event_id>/results')
def event_results(tournament_id, event_id):
    """View and enter results for an event."""
    tournament = Tournament.query.get_or_404(tournament_id)
    event = _event_for_tournament_or_404(tournament_id, event_id)

    heats = event.heats.order_by(Heat.heat_number, Heat.run_number).all()
    results = event.get_results_sorted()

    return render_template('scoring/event_results.html',
                           tournament=tournament,
                           event=event,
                           heats=heats,
                           results=results)


@scoring_bp.route('/<int:tournament_id>/offline-ops')
def offline_ops(tournament_id):
    """Judge-facing local offline queue operations panel."""
    tournament = Tournament.query.get_or_404(tournament_id)
    events = tournament.events.order_by(Event.name).all()
    event_directory = {
        event.id: {
            'name': event.display_name,
            'type': event.event_type,
        }
        for event in events
    }
    return render_template(
        'scoring/offline_ops.html',
        tournament=tournament,
        event_directory=event_directory,
    )


@scoring_bp.route('/<int:tournament_id>/heat/<int:heat_id>/enter', methods=['GET', 'POST'])
def enter_heat_results(tournament_id, heat_id):
    """Enter results for a specific heat."""
    tournament = Tournament.query.get_or_404(tournament_id)
    heat = _heat_for_tournament_or_404(tournament_id, heat_id)
    event = heat.event

    if request.method == 'POST':
        outcome = _save_heat_results_submission(tournament_id=tournament_id, heat=heat, event=event)
        if _is_async_save_request():
            payload = {
                'ok': outcome['ok'],
                'message': outcome['message'],
                'redirect_url': outcome['redirect_url'],
                'category': outcome['category'],
            }
            return jsonify(payload), outcome['status_code']

        flash(outcome['message'], outcome['category'])
        return redirect(outcome['redirect_url'])

    # Get competitor details for display
    competitor_ids = heat.get_competitors()
    competitors = []

    if event.event_type == 'college':
        comps = CollegeCompetitor.query.filter(CollegeCompetitor.id.in_(competitor_ids)).all()
    else:
        comps = ProCompetitor.query.filter(ProCompetitor.id.in_(competitor_ids)).all()
    comp_lookup = {c.id: c for c in comps}

    for comp_id in competitor_ids:
        comp = comp_lookup.get(comp_id)
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
                           competitors=competitors,
                           heat_version=heat.version_id)


def _calculate_positions(event):
    """Calculate final positions and award points/payouts."""
    all_results = event.results.all()

    # Remove previously awarded totals so finalization is idempotent.
    if event.event_type == 'college':
        for result in all_results:
            awarded = int(result.points_awarded or 0)
            if not awarded:
                continue
            competitor = CollegeCompetitor.query.get(result.competitor_id)
            if competitor:
                competitor.individual_points = max(0, competitor.individual_points - awarded)
            result.points_awarded = 0
            result.final_position = None
    else:
        for result in all_results:
            awarded = float(result.payout_amount or 0)
            if not awarded:
                continue
            competitor = ProCompetitor.query.get(result.competitor_id)
            if competitor:
                competitor.total_earnings = max(0.0, competitor.total_earnings - awarded)
            result.payout_amount = 0.0
            result.final_position = None

    results = event.results.filter_by(status='completed').all()
    if not results:
        event.status = 'in_progress'
        return

    # Sort based on scoring type
    def _metric(row):
        if event.requires_dual_runs:
            return row.best_run
        return row.result_value

    if event.scoring_order == 'lowest_wins':
        results.sort(key=lambda r: _metric(r) if _metric(r) is not None else float('inf'))
    else:
        results.sort(key=lambda r: _metric(r) if _metric(r) is not None else float('-inf'), reverse=True)

    # Assign positions and points/payouts
    competitor_ids = [r.competitor_id for r in results]
    if event.event_type == 'college':
        competitor_rows = CollegeCompetitor.query.filter(CollegeCompetitor.id.in_(competitor_ids)).all()
    else:
        competitor_rows = ProCompetitor.query.filter(ProCompetitor.id.in_(competitor_ids)).all()
    competitor_lookup = {c.id: c for c in competitor_rows}

    for position, result in enumerate(results, start=1):
        result.final_position = position

        if event.event_type == 'college':
            # Award points based on placement
            points = config.PLACEMENT_POINTS.get(position, 0)
            result.points_awarded = points

            # Update competitor's individual points
            competitor = competitor_lookup.get(result.competitor_id)
            if competitor:
                competitor.individual_points += points

        else:
            # Award payout based on placement
            payout = event.get_payout_for_position(position)
            result.payout_amount = payout

            # Update competitor's earnings
            competitor = competitor_lookup.get(result.competitor_id)
            if competitor:
                competitor.total_earnings += payout

    if event.event_type == 'college':
        touched_team_ids = {c.team_id for c in competitor_rows if c.team_id}
        from models import Team
        for team_id in touched_team_ids:
            team = Team.query.get(team_id)
            if team:
                team.recalculate_points()

    # #8 — Score discrepancy flagging: flag outliers > 2 std dev from mean
    _flag_score_outliers(results, event)

    event.status = 'completed'


def _flag_score_outliers(results, event):
    """Flag EventResult rows whose score is >2 standard deviations from the event mean."""
    import statistics
    values = []
    for r in results:
        v = r.best_run if event.requires_dual_runs else r.result_value
        if v is not None:
            try:
                values.append(float(v))
            except (TypeError, ValueError):
                pass
    if len(values) < 3:
        # Not enough data to compute meaningful stats
        for r in results:
            r.is_flagged = False
        return
    mean = statistics.mean(values)
    stdev = statistics.stdev(values)
    for r in results:
        v = r.best_run if event.requires_dual_runs else r.result_value
        try:
            fv = float(v)
            r.is_flagged = (stdev > 0 and abs(fv - mean) > 2 * stdev)
        except (TypeError, ValueError):
            r.is_flagged = False


@scoring_bp.route('/<int:tournament_id>/event/<int:event_id>/finalize', methods=['POST'])
def finalize_event(tournament_id, event_id):
    """Finalize an event and calculate positions."""
    event = _event_for_tournament_or_404(tournament_id, event_id)

    try:
        _calculate_positions(event)
        log_action(
            action='event_finalized',
            entity_type='event',
            entity_id=event.id,
            details={'tournament_id': tournament_id}
        )
        db.session.commit()
    except (StaleDataError, IntegrityError):
        db.session.rollback()
        flash('Concurrent update detected while finalizing event.', 'error')
        return redirect(url_for('scoring.event_results', tournament_id=tournament_id, event_id=event_id))

    invalidate_tournament_caches(tournament_id)
    flash(text.FLASH['event_finalized'].format(event_name=event.display_name), 'success')
    return redirect(url_for('scoring.event_results',
                            tournament_id=tournament_id,
                            event_id=event_id))


@scoring_bp.route('/<int:tournament_id>/event/<int:event_id>/payouts', methods=['GET', 'POST'])
def configure_payouts(tournament_id, event_id):
    """Configure payouts for a pro event."""
    tournament = Tournament.query.get_or_404(tournament_id)
    event = _event_for_tournament_or_404(tournament_id, event_id)

    if event.event_type != 'pro':
        flash(text.FLASH['pro_only_payouts'], 'error')
        return redirect(url_for('scheduling.event_list', tournament_id=tournament_id))

    if request.method == 'POST':
        payouts = {}
        for i in range(1, 11):  # Up to 10th place
            amount = request.form.get(f'payout_{i}')
            if amount:
                try:
                    payouts[str(i)] = float(amount)
                except (TypeError, ValueError):
                    flash(f'Invalid payout amount for position {i}: {amount!r}', 'error')
                    return redirect(url_for('scoring.configure_payouts',
                                            tournament_id=tournament_id,
                                            event_id=event_id))

        try:
            event.set_payouts(payouts)
            log_action(
                action='payouts_configured',
                entity_type='event',
                entity_id=event.id,
                details={'positions': sorted(payouts.keys())}
            )
            db.session.commit()
        except (StaleDataError, IntegrityError):
            db.session.rollback()
            flash('Another user changed this event while saving payouts. Please retry.', 'error')
            return redirect(url_for('scoring.configure_payouts',
                                    tournament_id=tournament_id,
                                    event_id=event_id))

        invalidate_tournament_caches(tournament_id)
        flash(text.FLASH['payouts_saved'], 'success')
        return redirect(url_for('scoring.event_results',
                                tournament_id=tournament_id,
                                event_id=event_id))

    return render_template('scoring/configure_payouts.html',
                           tournament=tournament,
                           event=event,
                           current_payouts=event.get_payouts())


# ---------------------------------------------------------------------------
# #13 — Birling bracket visual display
# ---------------------------------------------------------------------------

@scoring_bp.route('/<int:tournament_id>/event/<int:event_id>/birling-bracket')
def birling_bracket(tournament_id, event_id):
    """Display the double-elimination Birling bracket for an event."""
    tournament = Tournament.query.get_or_404(tournament_id)
    event = _event_for_tournament_or_404(tournament_id, event_id)

    if event.scoring_type != 'bracket':
        flash('This event does not use a bracket.', 'warning')
        return redirect(url_for('scoring.event_results', tournament_id=tournament_id, event_id=event_id))

    from services.birling_bracket import BirlingBracket
    bb = BirlingBracket(event)
    bracket_data = bb.bracket_data

    comp_lookup = {
        str(c['id']): c['name']
        for c in bracket_data.get('competitors', [])
    }

    return render_template(
        'scoring/birling_bracket.html',
        tournament=tournament,
        event=event,
        bracket=bracket_data['bracket'],
        placements=bracket_data.get('placements', {}),
        comp_lookup=comp_lookup,
        current_round=bracket_data.get('current_round', ''),
    )
