"""Scoped host controls; normal CSRF protects every mutation."""

import json
import uuid

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user

from database import db
from models import Event, Heat, Tournament
from models.managed_event import ManagedCareerDelivery, ManagedCommand
from services.cache_invalidation import invalidate_tournament_caches
from services.managed_events import (
    ManagedEventError,
    accept_export,
    apply_host_awards,
    authorize,
    bind_event,
    deliver_career_results,
    recover_command,
    submit,
    workspace_url,
)

managed_bp = Blueprint('managed', __name__, url_prefix='/managed')


@managed_bp.before_request
def enabled_and_scoped():
    if not current_app.config.get('MANAGED_EVENTS_ENABLED'):
        abort(403, description='Managed event rehearsal is disabled for this installation.')
    tournament_id = (request.view_args or {}).get('tournament_id')
    if current_user.tournament_id is not None and current_user.tournament_id != tournament_id:
        abort(403)


@managed_bp.before_app_request
def protect_delegated_writes():
    if request.method != 'POST' or not request.endpoint or not request.endpoint.startswith(('scoring.', 'scheduling.')) or request.endpoint == 'scoring.configure_payouts':
        return
    args = request.view_args or {}
    event_id = args.get('event_id', args.get('eid'))
    event = db.session.get(Event, event_id) if event_id else None
    heat_id = args.get('heat_id', args.get('source_heat_id'))
    if event is None and heat_id:
        heat = db.session.get(Heat, heat_id)
        event = heat.event if heat else None
    if event is not None and event.operation_mode == 'managed':
        abort(409, description='Use STRATHMARK managed event operations for marks, outcomes and progression.')


@managed_bp.get('/<int:tournament_id>')
def event_list(tournament_id):
    tournament = db.get_or_404(Tournament, tournament_id)
    events = Event.query.filter_by(tournament_id=tournament_id, is_handicap=True).all()
    return render_template('managed/list.html', tournament=tournament, events=events)


@managed_bp.route('/<int:tournament_id>/event/<int:event_id>', methods=['GET', 'POST'])
def event_workspace(tournament_id, event_id):
    event = Event.query.filter_by(tournament_id=tournament_id, id=event_id).first_or_404()
    try:
        authorize(current_user, event)
    except ManagedEventError:
        abort(403)
    error = None
    if request.method == 'POST':
        try:
            action = request.form.get('action')
            if action == 'bind':
                bind_event(event, current_user, json.loads(request.form['payload']))
            elif action == 'refresh':
                submit(event, current_user, {'operation': 'read'})
            elif action == 'handover_gui':
                submit(event, current_user, {'operation': 'handover', 'request_id': request.form['request_id'],
                    'expected_revision': int(request.form['expected_revision']), 'editor': 'missoula', 'payload': {'editor': 'gui'}})
            elif action == 'recover':
                recover_command(int(request.form['command_id']), current_user)
            elif action == 'accept_export':
                accept_export(event, current_user, submit(event, current_user, {'operation': 'export'}))
                db.session.commit()
            elif action == 'awards':
                apply_host_awards(event, current_user)
                db.session.commit()
            elif action == 'deliver':
                delivery = deliver_career_results(event, current_user, limit=5)
                flash(f"Career delivery: {delivery['accepted']} acknowledged, {delivery['remaining_pending']} pending, {delivery['conflicting']} conflicting.", 'info')
            else:
                submit(event, current_user, json.loads(request.form['payload']))
            invalidate_tournament_caches(tournament_id)
            return redirect(url_for('managed.event_workspace', tournament_id=tournament_id, event_id=event_id), code=303)
        except (ManagedEventError, ValueError, KeyError, TypeError) as exc:
            db.session.rollback()
            error = str(exc)
    link = event.managed_operation
    state = json.loads(link.state_json) if link and link.state_json else None
    commands = ManagedCommand.query.filter_by(event_id=event.id, status='pending').all() if link else []
    delivery_counts = {status: ManagedCareerDelivery.query.filter_by(event_id=event.id, status=status).count() for status in ('pending', 'accepted', 'conflicting')}
    shared_url = None
    try:
        shared_url = workspace_url(event)
    except ManagedEventError as exc:
        error = str(exc)
    return render_template('managed/event.html', tournament=event.tournament, event=event, state=state, commands=commands, error=error, shared_url=shared_url, delivery_counts=delivery_counts, request_id=str(uuid.uuid4())), (409 if error else 200)
