"""
Preflight validation routes and async heat/flight generation jobs.
"""
import json

from flask import flash, jsonify, redirect, render_template, request, session, url_for

import config
from database import db
from models import Tournament
from services.audit import log_action
from services.background_jobs import submit as submit_job
from services.schedule_generation import (
    generate_tournament_schedule_artifacts,
    run_preflight_autofix,
)

from . import scheduling_bp


@scheduling_bp.route('/<int:tournament_id>/preflight', methods=['GET', 'POST'])
def preflight_check(tournament_id):
    """Run preflight checks and offer one-click auto-fix actions."""
    tournament = Tournament.query.get_or_404(tournament_id)
    from services.flight_builder import integrate_college_spillover_into_flights
    from services.partner_matching import auto_assign_pro_partners
    from services.preflight import build_preflight_report

    session_key = f'schedule_options_{tournament_id}'
    # DB-first read: Friday Showcase page persists to schedule_config; session
    # may be empty in a fresh browser session. Without the DB fallback the
    # autofix POST runs with saturday_ids=[] and orphans Chokerman Run 2 +
    # every selected spillover event with flight_id=NULL. Mirrors the JSON
    # endpoint a few lines below that already reads DB-first.
    saved = tournament.get_schedule_config() or session.get(session_key, {})
    saturday_ids = [int(eid) for eid in saved.get('saturday_college_event_ids', [])]

    if request.method == 'POST':
        action = request.form.get('action', 'autofix')
        if action == 'autofix':
            result = run_preflight_autofix(tournament, saturday_ids)
            db.session.commit()
            log_action('preflight_autofix_applied', 'tournament', tournament_id, {
                **result,
                'tournament_id': tournament_id,
            })
            gear_msg = (
                f" parsed {result['gear_parsed']['parsed']} gear detail(s),"
                if result['gear_parsed']['parsed'] else ''
            )
            pairs_msg = (
                f" completed {result['gear_pairs_completed']} one-sided gear pair(s),"
                if result['gear_pairs_completed'] else ''
            )
            flash(
                f"Auto-fix complete: synced {result['heats_fixed']} heats,{gear_msg}{pairs_msg} "
                f"assigned {result['partner_summary']['assigned_pairs']} pairs, "
                f"integrated {result['spillover']['integrated_heats']} spillover heats.",
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


@scheduling_bp.route('/<int:tournament_id>/events/generate-async', methods=['POST'])
def generate_async(tournament_id):
    """Submit heat + flight generation as a background job and return a job_id."""
    Tournament.query.get_or_404(tournament_id)

    job_id = submit_job(
        f'generate_all:{tournament_id}',
        generate_tournament_schedule_artifacts,
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
