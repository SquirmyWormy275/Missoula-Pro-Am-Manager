"""
Preflight validation routes and async heat/flight generation jobs.
"""
import json

from flask import flash, jsonify, redirect, render_template, request, session, url_for

import config
from database import db
from models import Event, Heat, HeatAssignment, Tournament
from services.audit import log_action
from services.background_jobs import submit as submit_job

from . import _build_pro_flights_if_possible, _generate_all_heats, scheduling_bp


@scheduling_bp.route('/<int:tournament_id>/preflight', methods=['GET', 'POST'])
def preflight_check(tournament_id):
    """Run preflight checks and offer one-click auto-fix actions."""
    tournament = Tournament.query.get_or_404(tournament_id)
    from services.flight_builder import integrate_college_spillover_into_flights
    from services.partner_matching import auto_assign_pro_partners
    from services.preflight import build_preflight_report

    session_key = f'schedule_options_{tournament_id}'
    saved = session.get(session_key, {})
    saturday_ids = [int(eid) for eid in saved.get('saturday_college_event_ids', [])]

    if request.method == 'POST':
        action = request.form.get('action', 'autofix')
        if action == 'autofix':
            from services.gear_sharing import complete_one_sided_pairs, parse_all_gear_details

            # 1) Heat assignment sync for all events
            heats_fixed = 0
            for event in tournament.events.all():
                for heat in event.heats.all():
                    json_ids = heat.get_competitors()
                    HeatAssignment.query.filter_by(heat_id=heat.id).delete()
                    assignments = heat.get_stand_assignments()
                    for comp_id in json_ids:
                        db.session.add(HeatAssignment(
                            heat_id=heat.id,
                            competitor_id=comp_id,
                            competitor_type=event.event_type,
                            stand_number=assignments.get(str(comp_id)),
                        ))
                    heats_fixed += 1

            # 2) Parse unstructured gear-sharing details into structured maps
            gear_parse_result = parse_all_gear_details(tournament)

            # 3) Write reciprocals for all one-sided gear pairs
            pairs_result = complete_one_sided_pairs(tournament)

            # 4) Auto-partner assignments
            partner_summary = auto_assign_pro_partners(tournament)

            # 5) Saturday spillover integration
            integration = integrate_college_spillover_into_flights(tournament, saturday_ids)

            db.session.commit()
            log_action('preflight_autofix_applied', 'tournament', tournament_id, {
                'heats_fixed': heats_fixed,
                'gear_parsed': gear_parse_result,
                'gear_pairs_completed': pairs_result['completed'],
                'partner_summary': partner_summary,
                'spillover': integration,
            })
            gear_msg = (
                f" parsed {gear_parse_result['parsed']} gear detail(s),"
                if gear_parse_result['parsed'] else ''
            )
            pairs_msg = (
                f" completed {pairs_result['completed']} one-sided gear pair(s),"
                if pairs_result['completed'] else ''
            )
            flash(
                f"Auto-fix complete: synced {heats_fixed} heats,{gear_msg}{pairs_msg} "
                f"assigned {partner_summary['assigned_pairs']} pairs, "
                f"integrated {integration['integrated_heats']} spillover heats.",
                'success'
            )
            return redirect(url_for('scheduling.preflight_check', tournament_id=tournament_id))

    report = build_preflight_report(tournament, saturday_ids)
    return render_template(
        'scheduling/preflight.html',
        tournament=tournament,
        report=report,
        saturday_college_event_ids=saturday_ids,
    )


# ---------------------------------------------------------------------------
# Preflight JSON — inline checklist for events page
# ---------------------------------------------------------------------------

@scheduling_bp.route('/<int:tournament_id>/preflight-json')
def preflight_json(tournament_id):
    """JSON endpoint: inline preflight status for the events page."""
    from services.preflight import build_preflight_report
    tournament = Tournament.query.get_or_404(tournament_id)
    session_key = f'schedule_options_{tournament_id}'
    saved = tournament.get_schedule_config() or session.get(session_key, {})
    saturday_ids = [int(eid) for eid in saved.get('saturday_college_event_ids', [])]
    report = build_preflight_report(tournament, saturday_ids)
    return jsonify({
        'issue_count': report['issue_count'],
        'severity': report['severity'],
        'issues': [
            {
                'severity': i['severity'],
                'title': i['title'],
                'detail': i.get('detail', ''),
                'autofix': i.get('autofix', False),
            }
            for i in report['issues']
        ],
    })


# ---------------------------------------------------------------------------
# #4 — Async heat / flight generation  (#4)
# ---------------------------------------------------------------------------

def _async_generate_all(tournament_id: int) -> dict:
    """Background task: generate all heats + pro flights for a tournament.

    Runs inside a new application context so the ThreadPoolExecutor worker
    has access to the DB session and Flask app.
    """
    from flask import current_app
    app = current_app._get_current_object()
    with app.app_context():
        from models import Event as _Event
        from models import Tournament as _Tournament
        from services.flight_builder import build_pro_flights
        from services.heat_generator import generate_event_heats

        tournament = _Tournament.query.get(tournament_id)
        if not tournament:
            return {'ok': False, 'error': f'Tournament {tournament_id} not found.'}

        generated, skipped, errors = 0, 0, []
        for event in tournament.events.order_by(_Event.event_type, _Event.name, _Event.gender).all():
            try:
                generate_event_heats(event)
                generated += 1
            except Exception as exc:
                if 'No competitors entered' in str(exc):
                    skipped += 1
                else:
                    errors.append(str(exc))

        db.session.commit()  # Persist heats before building flights

        pro_flights = _build_pro_flights_if_possible(tournament, build_pro_flights)
        return {
            'ok': True,
            'generated': generated,
            'skipped': skipped,
            'errors': errors,
            'flights': pro_flights,
        }


@scheduling_bp.route('/<int:tournament_id>/events/generate-async', methods=['POST'])
def generate_async(tournament_id):
    """Submit heat + flight generation as a background job and return a job_id."""
    Tournament.query.get_or_404(tournament_id)

    job_id = submit_job(
        f'generate_all:{tournament_id}',
        _async_generate_all,
        tournament_id,
        metadata={'tournament_id': tournament_id, 'kind': 'generate_all'},
    )
    return json.dumps({'job_id': job_id}), 202, {'Content-Type': 'application/json'}


@scheduling_bp.route('/<int:tournament_id>/events/job-status/<job_id>')
def generation_job_status(tournament_id, job_id):
    """Poll background job status. Returns JSON with status/result."""
    from services.background_jobs import get as get_job
    job = get_job(job_id)
    if not job or int((job.get('metadata') or {}).get('tournament_id', -1)) != tournament_id:
        return json.dumps({'error': 'Job not found.'}), 404, {'Content-Type': 'application/json'}
    return json.dumps({
        'job_id': job['id'],
        'status': job['status'],
        'result': job['result'],
        'error': job['error'],
    }), 200, {'Content-Type': 'application/json'}
