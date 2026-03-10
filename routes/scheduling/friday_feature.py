"""
Friday Night Feature scheduling route.
"""
import json
import os
from flask import render_template, redirect, url_for, flash, request, session
from database import db
from models import Tournament, Event
import config
from services.audit import log_action
from . import scheduling_bp


def _fnf_config_path(tournament_id: int) -> str:
    """Return path to the per-tournament Friday Night Feature JSON config."""
    instance_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'instance')
    os.makedirs(instance_dir, exist_ok=True)
    return os.path.join(instance_dir, f'friday_feature_{tournament_id}.json')


def _load_fnf_config(tournament_id: int) -> dict:
    """Load persisted Friday Night Feature selections for a tournament."""
    path = _fnf_config_path(tournament_id)
    if not os.path.exists(path):
        return {'event_ids': [], 'notes': ''}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'event_ids': [], 'notes': ''}


def _save_fnf_config(tournament_id: int, data: dict) -> None:
    """Persist Friday Night Feature selections."""
    path = _fnf_config_path(tournament_id)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f)


@scheduling_bp.route('/<int:tournament_id>/friday-night', methods=['GET', 'POST'])
def friday_feature(tournament_id):
    """Configure Friday Night Feature events and Saturday college spillover."""
    from services.schedule_builder import COLLEGE_SATURDAY_PRIORITY

    tournament = Tournament.query.get_or_404(tournament_id)

    # FNF: pro events eligible for Friday Night
    eligible_names = set(config.FRIDAY_NIGHT_EVENTS)
    pro_events = tournament.events.filter_by(event_type='pro').order_by(Event.name, Event.gender).all()
    eligible_events = [e for e in pro_events if e.name in eligible_names]

    # Saturday spillover: college events eligible to run Saturday morning
    priority_index = {p: i for i, p in enumerate(COLLEGE_SATURDAY_PRIORITY)}
    all_college = tournament.events.filter_by(event_type='college').all()
    sat_eligible = sorted(
        [e for e in all_college if (e.name, e.gender) in priority_index],
        key=lambda e: priority_index[(e.name, e.gender)]
    )

    fnf_config = _load_fnf_config(tournament_id)
    session_key = f'schedule_options_{tournament_id}'
    saved_opts = session.get(session_key, {})

    if request.method == 'POST':
        action = request.form.get('action', 'save')
        selected_ids = [int(x) for x in request.form.getlist('event_ids') if x.isdigit()]
        notes = (request.form.get('notes') or '').strip()

        _save_fnf_config(tournament_id, {'event_ids': selected_ids, 'notes': notes})

        # Save Saturday spillover selections into the shared schedule session
        try:
            saturday_college_event_ids = [
                int(eid) for eid in request.form.getlist('saturday_college_event_ids') if str(eid).strip()
            ]
        except (TypeError, ValueError):
            saturday_college_event_ids = []

        saved_opts = dict(saved_opts)
        saved_opts['saturday_college_event_ids'] = saturday_college_event_ids
        session[session_key] = saved_opts
        session.modified = True
        # Also persist to DB
        tournament.set_schedule_config(saved_opts)
        db.session.commit()

        if action == 'generate_heats' and selected_ids:
            # Generate heats for each Friday Night Feature event using the
            # standard heat generator.  Existing heats for these events are
            # cleared first; only events already created in the DB are processed.
            from services.heat_generator import generate_event_heats
            generated = 0
            errors = []
            for event_id in selected_ids:
                event = Event.query.filter_by(id=event_id, tournament_id=tournament_id).first()
                if not event:
                    continue
                try:
                    heat_count = generate_event_heats(event)
                    generated += heat_count
                except Exception as exc:
                    errors.append(f'{event.display_name}: {exc}')
            db.session.commit()
            if errors:
                for err in errors:
                    flash(f'Heat generation error — {err}', 'error')
            if generated > 0:
                flash(f'Generated {generated} Friday Night Feature heat(s).', 'success')
            elif not errors:
                flash('No heats generated (check that competitors are enrolled in the selected events).', 'warning')
            log_action('fnf_heats_generated', 'tournament', tournament_id, {
                'event_ids': selected_ids,
                'heats_generated': generated,
            })
        else:
            log_action('friday_feature_configured', 'tournament', tournament_id, {
                'fnf_event_count': len(selected_ids),
                'sat_spillover_count': len(saturday_college_event_ids),
            })
            db.session.commit()
            flash('Friday Showcase & Saturday spillover saved.', 'success')
        return redirect(url_for('scheduling.friday_feature', tournament_id=tournament_id))

    selected_saturday_ids = set(int(i) for i in saved_opts.get('saturday_college_event_ids', []))

    return render_template(
        'scheduling/friday_feature.html',
        tournament=tournament,
        eligible_events=eligible_events,
        selected_ids=set(fnf_config.get('event_ids', [])),
        notes=fnf_config.get('notes', ''),
        sat_eligible=sat_eligible,
        selected_saturday_ids=selected_saturday_ids,
    )
