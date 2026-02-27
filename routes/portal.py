"""
Public/spectator and competitor portal routes.
"""
from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from models import Event, EventResult, Heat, Tournament
from models.competitor import CollegeCompetitor, ProCompetitor

portal_bp = Blueprint('portal', __name__)


@portal_bp.route('/')
def index():
    """Portal landing: send users to the latest active tournament view."""
    view_mode = _resolve_view_mode(prefer_mobile=True)
    active = Tournament.query.filter(
        Tournament.status.in_(['setup', 'college_active', 'pro_active'])
    ).order_by(Tournament.year.desc()).first()
    if active:
        return redirect(url_for('portal.spectator_dashboard', tournament_id=active.id, view=view_mode))
    tournaments = Tournament.query.order_by(Tournament.year.desc()).all()
    return render_template(
        'portal/landing.html',
        tournaments=tournaments,
        view_mode=view_mode,
        mobile_view=view_mode == 'mobile',
    )


@portal_bp.route('/competitor-access', methods=['GET', 'POST'])
def competitor_access():
    """No-login competitor access by full name."""
    view_mode = _resolve_view_mode(prefer_mobile=True)
    tournaments = Tournament.query.order_by(Tournament.year.desc()).all()
    active = Tournament.query.filter(
        Tournament.status.in_(['setup', 'college_active', 'pro_active'])
    ).order_by(Tournament.year.desc()).first()
    default_tournament_id = active.id if active else (tournaments[0].id if tournaments else None)

    if request.method == 'POST':
        tournament_id = request.form.get('tournament_id', type=int) or default_tournament_id
        full_name = (request.form.get('full_name') or '').strip()
        if not tournament_id:
            flash('No tournament is available yet.', 'error')
            return redirect(url_for('main.index'))
        if len(full_name) < 3:
            flash('Please enter the competitor full name.', 'error')
            return redirect(url_for('portal.competitor_access', view=view_mode))

        tournament = Tournament.query.get_or_404(tournament_id)
        matches = _find_competitor_matches(tournament, full_name)
        if not matches:
            flash('No competitor found with that name in this tournament.', 'error')
            return redirect(url_for('portal.competitor_access', view=view_mode))
        if len(matches) > 1:
            return render_template(
                'portal/competitor_access.html',
                tournaments=tournaments,
                selected_tournament_id=tournament_id,
                name_query=full_name,
                matches=matches,
                view_mode=view_mode,
                mobile_view=view_mode == 'mobile',
            )

        match = matches[0]
        return redirect(url_for(
            'portal.competitor_claim',
            tournament_id=tournament_id,
            competitor_type=match['competitor_type'],
            competitor_id=match['competitor'].id,
            view=view_mode,
        ))

    return render_template(
        'portal/competitor_access.html',
        tournaments=tournaments,
        selected_tournament_id=default_tournament_id,
        name_query='',
        matches=[],
        view_mode=view_mode,
        mobile_view=view_mode == 'mobile',
    )


@portal_bp.route('/spectator/<int:tournament_id>')
def spectator_dashboard(tournament_id):
    """Public spectator landing page with College vs Pro choice."""
    view_mode = _resolve_view_mode(prefer_mobile=True)
    tournament = Tournament.query.get_or_404(tournament_id)
    return render_template(
        'portal/spectator_dashboard.html',
        tournament=tournament,
        view_mode=view_mode,
        mobile_view=view_mode == 'mobile',
    )


@portal_bp.route('/spectator/<int:tournament_id>/college')
def spectator_college_standings(tournament_id):
    """College-focused spectator page."""
    view_mode = _resolve_view_mode(prefer_mobile=True)
    tournament = Tournament.query.get_or_404(tournament_id)

    bull = sorted(
        tournament.college_competitors.filter_by(status='active', gender='M').all(),
        key=lambda c: c.individual_points,
        reverse=True,
    )
    belle = sorted(
        tournament.college_competitors.filter_by(status='active', gender='F').all(),
        key=lambda c: c.individual_points,
        reverse=True,
    )
    team_standings = tournament.get_team_standings()

    completed_events = tournament.events.filter_by(
        status='completed',
        event_type='college'
    ).order_by(
        Event.event_type,
        Event.name,
        Event.gender
    ).all()
    event_summaries = []
    for event in completed_events:
        result_count = event.results.filter_by(status='completed').count()
        event_summaries.append({
            'event': event,
            'result_count': result_count,
        })

    return render_template(
        'portal/spectator_college.html',
        tournament=tournament,
        bull=bull,
        belle=belle,
        team_standings=team_standings,
        event_summaries=event_summaries,
        view_mode=view_mode,
        mobile_view=view_mode == 'mobile',
    )


@portal_bp.route('/spectator/<int:tournament_id>/pro')
def spectator_pro_standings(tournament_id):
    """Pro-focused spectator page."""
    view_mode = _resolve_view_mode(prefer_mobile=True)
    tournament = Tournament.query.get_or_404(tournament_id)

    completed_events = tournament.events.filter_by(
        status='completed',
        event_type='pro'
    ).order_by(
        Event.name,
        Event.gender
    ).all()
    event_summaries = []
    for event in completed_events:
        result_count = event.results.filter_by(status='completed').count()
        event_summaries.append({
            'event': event,
            'result_count': result_count,
        })

    return render_template(
        'portal/spectator_pro.html',
        tournament=tournament,
        event_summaries=event_summaries,
        view_mode=view_mode,
        mobile_view=view_mode == 'mobile',
    )


@portal_bp.route('/spectator/<int:tournament_id>/relay')
def spectator_relay_results(tournament_id):
    """Public Pro-Am Relay results page."""
    view_mode = _resolve_view_mode(prefer_mobile=True)
    tournament = Tournament.query.get_or_404(tournament_id)
    from services.proam_relay import get_proam_relay

    relay = get_proam_relay(tournament)
    return render_template(
        'portal/relay_results.html',
        tournament=tournament,
        status=relay.get_status(),
        teams=relay.get_teams(),
        results=relay.get_results(),
        view_mode=view_mode,
        mobile_view=view_mode == 'mobile',
    )


@portal_bp.route('/spectator/<int:tournament_id>/event/<int:event_id>')
def spectator_event_results(tournament_id, event_id):
    """Public event results page with heat/ranking sorting options."""
    view_mode = _resolve_view_mode(prefer_mobile=True)
    tournament = Tournament.query.get_or_404(tournament_id)
    event = Event.query.get_or_404(event_id)
    if event.tournament_id != tournament_id:
        abort(404)
    if event.status != 'completed':
        flash('Event results are not completed yet.', 'warning')
        return redirect(url_for('portal.spectator_dashboard', tournament_id=tournament_id, view=view_mode))

    sort_by = (request.args.get('sort') or 'ranking').strip().lower()
    if sort_by not in {'ranking', 'heat'}:
        sort_by = 'ranking'

    ranking_rows = _build_event_ranking_rows(event)
    heat_rows = _build_event_heat_rows(event)

    return render_template(
        'portal/event_results.html',
        tournament=tournament,
        event=event,
        back_url=url_for(
            'portal.spectator_college_standings' if event.event_type == 'college' else 'portal.spectator_pro_standings',
            tournament_id=tournament.id,
            view=view_mode,
        ),
        sort_by=sort_by,
        ranking_rows=ranking_rows,
        heat_rows=heat_rows,
        view_mode=view_mode,
        mobile_view=view_mode == 'mobile',
    )


@portal_bp.route('/competitor/public')
def competitor_public():
    """Public competitor dashboard by explicit tournament/type/id."""
    view_mode = _resolve_view_mode(prefer_mobile=True)
    tournament_id = request.args.get('tournament_id', type=int)
    competitor_type = request.args.get('competitor_type', type=str)
    competitor_id = request.args.get('competitor_id', type=int)

    if not tournament_id or competitor_type not in {'college', 'pro'} or not competitor_id:
        flash('Invalid competitor portal link.', 'error')
        return redirect(url_for('portal.competitor_access', view=view_mode))

    tournament = Tournament.query.get_or_404(tournament_id)
    competitor = _load_competitor(tournament_id, competitor_type, competitor_id)
    if not competitor:
        flash('Competitor not found for this tournament.', 'error')
        return redirect(url_for('portal.competitor_access', view=view_mode))
    if not _can_access_competitor_page(tournament_id, competitor_type, competitor_id):
        return redirect(url_for(
            'portal.competitor_claim',
            tournament_id=tournament_id,
            competitor_type=competitor_type,
            competitor_id=competitor_id,
            view=view_mode,
        ))

    schedule_rows = _build_competitor_schedule(tournament, competitor_type, competitor_id)
    results_rows, total_payout = _build_competitor_results(tournament, competitor_type, competitor_id)

    return render_template(
        'portal/competitor_dashboard.html',
        tournament=tournament,
        competitor=competitor,
        competitor_type=competitor_type,
        schedule_rows=schedule_rows,
        results_rows=results_rows,
        total_payout=total_payout,
        view_mode=view_mode,
        mobile_view=view_mode == 'mobile',
    )


@portal_bp.route('/competitor/claim', methods=['GET', 'POST'])
def competitor_claim():
    """Set or verify a competitor PIN before granting access."""
    view_mode = _resolve_view_mode(prefer_mobile=True)
    tournament_id = request.values.get('tournament_id', type=int)
    competitor_type = request.values.get('competitor_type', type=str)
    competitor_id = request.values.get('competitor_id', type=int)

    if not tournament_id or competitor_type not in {'college', 'pro'} or not competitor_id:
        flash('Invalid competitor verification request.', 'error')
        return redirect(url_for('portal.competitor_access', view=view_mode))

    tournament = Tournament.query.get_or_404(tournament_id)
    competitor = _load_competitor(tournament_id, competitor_type, competitor_id)
    if not competitor:
        flash('Competitor not found for this tournament.', 'error')
        return redirect(url_for('portal.competitor_access', view=view_mode))

    if _can_access_competitor_page(tournament_id, competitor_type, competitor_id):
        return redirect(url_for(
            'portal.competitor_public',
            tournament_id=tournament_id,
            competitor_type=competitor_type,
            competitor_id=competitor_id,
            view=view_mode,
        ))

    requires_pin = bool(getattr(competitor, 'has_portal_pin', False))
    if request.method == 'POST':
        if requires_pin:
            pin = (request.form.get('pin') or '').strip()
            if not _is_valid_pin(pin):
                flash('PIN must be 4-8 digits.', 'error')
                return redirect(url_for(
                    'portal.competitor_claim',
                    tournament_id=tournament_id,
                    competitor_type=competitor_type,
                    competitor_id=competitor_id,
                    view=view_mode,
                ))
            if not competitor.check_portal_pin(pin):
                flash('Incorrect PIN.', 'error')
                return redirect(url_for(
                    'portal.competitor_claim',
                    tournament_id=tournament_id,
                    competitor_type=competitor_type,
                    competitor_id=competitor_id,
                    view=view_mode,
                ))
            _mark_competitor_session_authorized(tournament_id, competitor_type, competitor_id)
            return redirect(url_for(
                'portal.competitor_public',
                tournament_id=tournament_id,
                competitor_type=competitor_type,
                competitor_id=competitor_id,
                view=view_mode,
            ))

        pin = (request.form.get('pin') or '').strip()
        confirm_pin = (request.form.get('confirm_pin') or '').strip()
        if not _is_valid_pin(pin):
            flash('PIN must be 4-8 digits.', 'error')
            return redirect(url_for(
                'portal.competitor_claim',
                tournament_id=tournament_id,
                competitor_type=competitor_type,
                competitor_id=competitor_id,
                view=view_mode,
            ))
        if pin != confirm_pin:
            flash('PIN confirmation does not match.', 'error')
            return redirect(url_for(
                'portal.competitor_claim',
                tournament_id=tournament_id,
                competitor_type=competitor_type,
                competitor_id=competitor_id,
                view=view_mode,
            ))
        competitor.set_portal_pin(pin)
        _mark_competitor_session_authorized(tournament_id, competitor_type, competitor_id)
        from database import db
        db.session.commit()
        flash('PIN set. Your competitor portal is now protected.', 'success')
        return redirect(url_for(
            'portal.competitor_public',
            tournament_id=tournament_id,
            competitor_type=competitor_type,
            competitor_id=competitor_id,
            view=view_mode,
        ))

    return render_template(
        'portal/competitor_claim.html',
        tournament=tournament,
        competitor=competitor,
        competitor_type=competitor_type,
        requires_pin=requires_pin,
        view_mode=view_mode,
        mobile_view=view_mode == 'mobile',
    )


@portal_bp.route('/competitor')
@login_required
def competitor_dashboard():
    """Competitor-focused dashboard for personal schedule/results/payouts."""
    user = current_user
    if not getattr(user, 'is_competitor', False) and not getattr(user, 'is_judge', False):
        abort(403)

    if getattr(user, 'is_judge', False):
        tournament_id = request.args.get('tournament_id', type=int)
        competitor_type = request.args.get('competitor_type', default='pro', type=str)
        competitor_id = request.args.get('competitor_id', type=int)
    else:
        tournament_id = user.tournament_id
        competitor_type = user.competitor_type
        competitor_id = user.competitor_id

    view_mode = _resolve_view_mode(prefer_mobile=not getattr(user, 'is_judge', False))

    if not tournament_id or competitor_type not in {'college', 'pro'} or not competitor_id:
        flash('Competitor account is missing tournament/competitor link data.', 'error')
        return redirect(url_for('main.index'))

    tournament = Tournament.query.get_or_404(tournament_id)
    competitor = _load_competitor(tournament_id, competitor_type, competitor_id)
    if not competitor:
        flash('Linked competitor record was not found.', 'error')
        return redirect(url_for('main.index'))

    schedule_rows = _build_competitor_schedule(tournament, competitor_type, competitor_id)
    results_rows, total_payout = _build_competitor_results(tournament, competitor_type, competitor_id)

    return render_template(
        'portal/competitor_dashboard.html',
        tournament=tournament,
        competitor=competitor,
        competitor_type=competitor_type,
        schedule_rows=schedule_rows,
        results_rows=results_rows,
        total_payout=total_payout,
        view_mode=view_mode,
        mobile_view=view_mode == 'mobile',
    )


def _resolve_view_mode(*, prefer_mobile: bool = False) -> str:
    explicit = (request.args.get('view') or request.values.get('view') or '').strip().lower()
    if explicit in {'mobile', 'desktop'}:
        return explicit
    if prefer_mobile and _is_mobile_client():
        return 'mobile'
    return 'desktop'


def _is_mobile_client() -> bool:
    user_agent = (request.user_agent.string or '').lower()
    mobile_markers = ('iphone', 'android', 'mobile', 'ipad', 'ipod')
    return any(marker in user_agent for marker in mobile_markers)


def _load_competitor(tournament_id: int, competitor_type: str, competitor_id: int):
    if competitor_type == 'college':
        return CollegeCompetitor.query.filter_by(
            id=competitor_id,
            tournament_id=tournament_id
        ).first()
    return ProCompetitor.query.filter_by(
        id=competitor_id,
        tournament_id=tournament_id
    ).first()


def _build_competitor_schedule(tournament: Tournament, competitor_type: str, competitor_id: int):
    rows = []
    events = tournament.events.order_by(Event.event_type, Event.name, Event.gender).all()
    for event in events:
        if event.event_type != competitor_type:
            continue
        heats = event.heats.order_by(Heat.heat_number, Heat.run_number).all()
        for heat in heats:
            competitor_ids = heat.get_competitors()
            if competitor_id not in competitor_ids:
                continue
            rows.append({
                'event_name': event.display_name,
                'event_type': event.event_type,
                'heat_number': heat.heat_number,
                'run_number': heat.run_number,
                'stand_number': heat.get_stand_for_competitor(competitor_id),
                'flight_number': heat.flight.flight_number if heat.flight else None,
                'status': heat.status,
            })
    return rows


def _build_competitor_results(tournament: Tournament, competitor_type: str, competitor_id: int):
    events = {
        event.id: event
        for event in tournament.events.all()
    }
    results = EventResult.query.filter_by(
        competitor_type=competitor_type,
        competitor_id=competitor_id
    ).order_by(EventResult.final_position).all()

    rows = []
    total_payout = 0.0
    for result in results:
        event = events.get(result.event_id)
        if not event:
            continue
        payout = float(result.payout_amount or 0.0)
        total_payout += payout
        rows.append({
            'event_name': event.display_name,
            'status': result.status,
            'result_value': result.result_value,
            'result_unit': result.result_unit,
            'position': result.final_position,
            'points_awarded': result.points_awarded,
            'payout_amount': payout,
        })

    return rows, total_payout


def _find_competitor_matches(tournament: Tournament, full_name: str):
    normalized = full_name.strip().lower()
    if not normalized:
        return []

    matches = []
    for comp in tournament.college_competitors.filter_by(status='active').all():
        if comp.name.strip().lower() == normalized:
            matches.append({'competitor_type': 'college', 'competitor': comp})
    for comp in tournament.pro_competitors.filter_by(status='active').all():
        if comp.name.strip().lower() == normalized:
            matches.append({'competitor_type': 'pro', 'competitor': comp})

    if matches:
        return matches

    # Fallback to partial name search if exact match is not found.
    for comp in tournament.college_competitors.filter_by(status='active').all():
        if normalized in comp.name.strip().lower():
            matches.append({'competitor_type': 'college', 'competitor': comp})
    for comp in tournament.pro_competitors.filter_by(status='active').all():
        if normalized in comp.name.strip().lower():
            matches.append({'competitor_type': 'pro', 'competitor': comp})

    return matches


def _portal_session_key(tournament_id: int, competitor_type: str, competitor_id: int) -> str:
    return f'{tournament_id}:{competitor_type}:{competitor_id}'


def _mark_competitor_session_authorized(tournament_id: int, competitor_type: str, competitor_id: int):
    authorized = session.get('competitor_portal_auth', {})
    if not isinstance(authorized, dict):
        authorized = {}
    authorized[_portal_session_key(tournament_id, competitor_type, competitor_id)] = True
    session['competitor_portal_auth'] = authorized
    session.modified = True


def _is_competitor_session_authorized(tournament_id: int, competitor_type: str, competitor_id: int) -> bool:
    authorized = session.get('competitor_portal_auth', {})
    if not isinstance(authorized, dict):
        return False
    return bool(authorized.get(_portal_session_key(tournament_id, competitor_type, competitor_id)))


def _can_access_competitor_page(tournament_id: int, competitor_type: str, competitor_id: int) -> bool:
    if current_user.is_authenticated and getattr(current_user, 'is_admin', False):
        return True
    return _is_competitor_session_authorized(tournament_id, competitor_type, competitor_id)


def _is_valid_pin(pin: str) -> bool:
    return pin.isdigit() and 4 <= len(pin) <= 8


def _build_event_ranking_rows(event: Event):
    results = event.results.filter_by(status='completed').all()
    rows = []
    for result in results:
        rows.append({
            'competitor_name': result.competitor_name,
            'result_value': result.result_value,
            'result_unit': result.result_unit,
            'position': result.final_position,
            'status': result.status,
            'run1': result.run1_value,
            'run2': result.run2_value,
            'best_run': result.best_run,
        })
    rows.sort(key=lambda r: (r['position'] is None, r['position'] or 999999, r['competitor_name'].lower()))
    return rows


def _build_event_heat_rows(event: Event):
    heats = event.heats.order_by(Heat.heat_number, Heat.run_number).all()
    result_lookup = {
        int(r.competitor_id): r
        for r in event.results.filter_by(status='completed').all()
        if r.competitor_id is not None
    }
    heat_rows = []
    for heat in heats:
        comp_ids = [int(cid) for cid in heat.get_competitors() if cid is not None]
        comp_lookup = _event_competitor_lookup(event, comp_ids)
        assignments = heat.get_stand_assignments()
        competitors = []
        for comp_id in comp_ids:
            comp = comp_lookup.get(comp_id)
            result = result_lookup.get(comp_id)
            competitors.append({
                'competitor_name': comp.name if comp else f'Unknown ({comp_id})',
                'stand_number': assignments.get(str(comp_id)),
                'result_value': result.result_value if result else None,
                'result_unit': result.result_unit if result else None,
                'position': result.final_position if result else None,
                'status': result.status if result else 'pending',
                'run1': result.run1_value if result else None,
                'run2': result.run2_value if result else None,
                'best_run': result.best_run if result else None,
            })
        heat_rows.append({
            'heat_number': heat.heat_number,
            'run_number': heat.run_number,
            'competitors': competitors,
        })
    return heat_rows


def _event_competitor_lookup(event: Event, competitor_ids: list[int]):
    ids = sorted(set(int(cid) for cid in competitor_ids if cid is not None))
    if not ids:
        return {}
    if event.event_type == 'college':
        competitors = CollegeCompetitor.query.filter(CollegeCompetitor.id.in_(ids)).all()
    else:
        competitors = ProCompetitor.query.filter(ProCompetitor.id.in_(ids)).all()
    return {c.id: c for c in competitors}
