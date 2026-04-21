"""Friday Night Feature scheduling route."""

from flask import flash, redirect, render_template, request, session, url_for

import config
from database import db
from models import Event, Tournament
from services.audit import log_action

from . import scheduling_bp


def _fnf_config_path(tournament_id: int) -> str:
    """Return path to the per-tournament Friday Night Feature JSON config."""
    import os
    instance_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'instance')
    os.makedirs(instance_dir, exist_ok=True)
    return os.path.join(instance_dir, f'friday_feature_{tournament_id}.json')


def _load_legacy_fnf_config(tournament_id: int) -> dict:
    """Load old file-based Friday Feature selections for compatibility."""
    import json
    import os

    path = _fnf_config_path(tournament_id)
    if not os.path.exists(path):
        return {'event_ids': [], 'notes': ''}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'event_ids': [], 'notes': ''}


def _load_fnf_config(tournament: Tournament) -> dict:
    """Load Friday Feature selections from DB-backed schedule_config."""
    schedule_config = tournament.get_schedule_config()
    event_ids = [
        int(event_id)
        for event_id in schedule_config.get('friday_pro_event_ids', [])
        if str(event_id).strip()
    ]
    notes = str(schedule_config.get('friday_feature_notes', '') or '')
    if event_ids or notes:
        return {'event_ids': event_ids, 'notes': notes}

    legacy = _load_legacy_fnf_config(tournament.id)
    if legacy.get('event_ids') or legacy.get('notes'):
        schedule_config['friday_pro_event_ids'] = [
            int(event_id)
            for event_id in legacy.get('event_ids', [])
            if str(event_id).strip()
        ]
        schedule_config['friday_feature_notes'] = str(legacy.get('notes', '') or '')
        tournament.set_schedule_config(schedule_config)
        db.session.commit()
        return {
            'event_ids': schedule_config['friday_pro_event_ids'],
            'notes': schedule_config['friday_feature_notes'],
        }
    return {'event_ids': [], 'notes': ''}


def _save_fnf_config(tournament: Tournament, data: dict) -> None:
    """Persist Friday Night Feature selections in schedule_config."""
    schedule_config = tournament.get_schedule_config()
    schedule_config['friday_pro_event_ids'] = [
        int(event_id)
        for event_id in data.get('event_ids', [])
        if str(event_id).strip()
    ]
    schedule_config['friday_feature_notes'] = str(data.get('notes', '') or '')
    tournament.set_schedule_config(schedule_config)


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

    fnf_config = _load_fnf_config(tournament)
    session_key = f'schedule_options_{tournament_id}'
    saved_opts = tournament.get_schedule_config() or session.get(session_key, {})

    if request.method == 'POST':
        action = request.form.get('action', 'save')
        selected_ids = [int(x) for x in request.form.getlist('event_ids') if x.isdigit()]
        notes = (request.form.get('notes') or '').strip()

        _save_fnf_config(tournament, {'event_ids': selected_ids, 'notes': notes})

        # Save Saturday spillover selections into the shared schedule session
        try:
            saturday_college_event_ids = [
                int(eid) for eid in request.form.getlist('saturday_college_event_ids') if str(eid).strip()
            ]
        except (TypeError, ValueError):
            saturday_college_event_ids = []

        # Merge into DB config (preserves friday_event_order / saturday_event_order)
        db_cfg = tournament.get_schedule_config()
        db_cfg['saturday_college_event_ids'] = saturday_college_event_ids
        saved_opts = dict(saved_opts)
        saved_opts['saturday_college_event_ids'] = saturday_college_event_ids
        session[session_key] = saved_opts
        session.modified = True
        tournament.set_schedule_config(db_cfg)
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
    fnf_schedule = _build_fnf_schedule(tournament, eligible_events, fnf_config)

    return render_template(
        'scheduling/friday_feature.html',
        tournament=tournament,
        eligible_events=eligible_events,
        selected_ids=set(fnf_config.get('event_ids', [])),
        notes=fnf_config.get('notes', ''),
        sat_eligible=sat_eligible,
        selected_saturday_ids=selected_saturday_ids,
        fnf_schedule=fnf_schedule,
    )


@scheduling_bp.route('/<int:tournament_id>/friday-night/print')
def friday_feature_print(tournament_id):
    """Printable Friday Night Feature schedule — heat-by-heat order per event."""
    tournament = Tournament.query.get_or_404(tournament_id)
    eligible_names = set(config.FRIDAY_NIGHT_EVENTS)
    pro_events = tournament.events.filter_by(event_type='pro').order_by(Event.name, Event.gender).all()
    eligible_events = [e for e in pro_events if e.name in eligible_names]

    fnf_config = _load_fnf_config(tournament)
    fnf_schedule = _build_fnf_schedule(tournament, eligible_events, fnf_config)

    from datetime import datetime
    return render_template(
        'scheduling/friday_feature_print.html',
        tournament=tournament,
        fnf_schedule=fnf_schedule,
        notes=fnf_config.get('notes', ''),
        now=datetime.utcnow(),
    )


@scheduling_bp.route('/<int:tournament_id>/friday-night/pdf')
def friday_feature_pdf(tournament_id):
    """FNF schedule as a PDF download (WeasyPrint if installed, HTML fallback otherwise).

    Reuses the same template as /friday-night/print so both surfaces stay in sync.
    On Railway the fallback serves HTML with Content-Type text/html so the user
    can still print via Ctrl-P without the deploy needing cairo/pango.
    """
    from datetime import datetime

    from services.print_response import weasyprint_or_html

    tournament = Tournament.query.get_or_404(tournament_id)
    eligible_names = set(config.FRIDAY_NIGHT_EVENTS)
    pro_events = tournament.events.filter_by(event_type='pro').order_by(Event.name, Event.gender).all()
    eligible_events = [e for e in pro_events if e.name in eligible_names]

    fnf_config = _load_fnf_config(tournament)
    fnf_schedule = _build_fnf_schedule(tournament, eligible_events, fnf_config)

    html = render_template(
        'scheduling/friday_feature_print.html',
        tournament=tournament,
        fnf_schedule=fnf_schedule,
        notes=fnf_config.get('notes', ''),
        now=datetime.utcnow(),
    )
    safe_name = f"{tournament.name}_{tournament.year}_friday_night_feature".replace(' ', '_').replace('/', '-')
    return weasyprint_or_html(html, safe_name)


def _build_fnf_schedule(tournament, eligible_events, fnf_config):
    """Build the Friday Night Feature schedule: selected FNF events in run order,
    each with its heats (run_number, heat_number ordered) and competitor/stand data.

    FNF runs as a straight heat-by-heat schedule similar to college day — no flight
    grouping. Used by both the interactive page and the printable view.
    """
    from models import Heat
    from models.competitor import ProCompetitor

    selected_event_ids = list(fnf_config.get('event_ids', []))
    if not selected_event_ids:
        return []

    ordered_events = sorted(
        [e for e in eligible_events if e.id in selected_event_ids],
        key=lambda e: (_fnf_event_order(e.name), e.gender or ''),
    )

    schedule = []
    slot = 1
    for event in ordered_events:
        event_heats = (
            Heat.query
            .filter_by(event_id=event.id)
            .order_by(Heat.run_number, Heat.heat_number)
            .all()
        )
        heat_rows = []
        for heat in event_heats:
            comp_ids = heat.get_competitors()
            stand_assignments = heat.get_stand_assignments()
            if comp_ids:
                pros = {
                    c.id: c for c in ProCompetitor.query.filter(
                        ProCompetitor.id.in_(comp_ids)
                    ).all()
                }
            else:
                pros = {}
            heat_rows.append({
                'heat_id': heat.id,
                'heat_number': heat.heat_number,
                'run_number': heat.run_number,
                'competitors': [
                    {
                        'name': pros[cid].display_name if cid in pros else f'ID:{cid}',
                        'stand': stand_assignments.get(str(cid), '?'),
                    }
                    for cid in comp_ids
                ],
            })
        schedule.append({
            'slot': slot,
            'event': event,
            'heats': heat_rows,
        })
        slot += 1

    return schedule


# Friday Night Feature event ordering — Springboard sequencing rules for the showcase.
# Matches services/schedule_builder._apply_friday_springboard_ordering at a high level:
# Springboard → Pro 1-Board → 3-Board Jigger, with anything else trailing.
_FNF_ORDER = ['Springboard', 'Pro 1-Board', '3-Board Jigger']


def _fnf_event_order(name: str) -> int:
    try:
        return _FNF_ORDER.index(name)
    except ValueError:
        return len(_FNF_ORDER) + 1
