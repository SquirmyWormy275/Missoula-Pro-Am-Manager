"""
Reporting routes for standings, results, and exports.
"""
import json
import os
import tempfile

from flask import (
    Blueprint,
    Response,
    abort,
    after_this_request,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

try:
    from flask_login import current_user
except ModuleNotFoundError:
    class _AnonymousCurrentUser:
        is_authenticated = False
        is_admin = False

    current_user = _AnonymousCurrentUser()
from database import db
from models import Event, Tournament
from services.audit import log_action
from services.background_jobs import get as get_job
from services.background_jobs import submit as submit_job
from services.excel_io import export_results_to_excel
from services.handicap_export import build_chopping_rows, export_chopping_results_to_excel
from services.report_cache import get as cache_get
from services.report_cache import set as cache_set
from services.restore_workflow import prepare_sqlite_restore
from services.restore_workflow import sqlite_schema_info as _restore_schema_info

reporting_bp = Blueprint('reporting', __name__)


def _sqlite_schema_info(path: str) -> dict:
    """Read lightweight schema metadata from a SQLite database file."""
    return _restore_schema_info(path)


def _cached_payload(key: str, builder):
    ttl = int(current_app.config.get('REPORT_CACHE_TTL_SECONDS', 60))
    cached = cache_get(key)
    if cached is not None:
        return cached
    payload = builder()
    cache_set(key, payload, ttl)
    return payload


@reporting_bp.route('/<int:tournament_id>/college/standings')
def college_standings(tournament_id):
    """View college standings (Bull/Belle of Woods and Team Standings).

    Phase 5 (V2.8.0): Bull/Belle now use the multi-key tiebreak query and
    surface placement counts + tied_with_next flags so the template can
    render a "TIE — manual resolution required" indicator when the chain
    fails to break the tie.  The plain ``bull`` / ``belle`` lists stay in
    the payload for backwards compat with any caller that expects them.
    """
    tournament = Tournament.query.get_or_404(tournament_id)

    payload = _cached_payload(
        f'reports:{tournament_id}:college_standings',
        lambda: {
            'bull': tournament.get_bull_of_woods(10),
            'belle': tournament.get_belle_of_woods(10),
            'bull_tiebreak': tournament.get_bull_belle_with_tiebreak_data('M', 10),
            'belle_tiebreak': tournament.get_bull_belle_with_tiebreak_data('F', 10),
            'team_standings': tournament.get_team_standings(),
        }
    )

    # Events that are not finalized but have at least one completed result —
    # these mean standings may be incomplete / provisional.
    from sqlalchemy import exists

    from models.event import EventResult
    unfinalized_events = (
        Event.query
        .filter(
            Event.tournament_id == tournament_id,
            Event.is_finalized == False,  # noqa: E712
        )
        .filter(
            exists().where(
                (EventResult.event_id == Event.id) &
                (EventResult.status == 'completed')
            )
        )
        .order_by(Event.name)
        .all()
    )

    return render_template('reports/college_standings.html',
                           tournament=tournament,
                           bull=payload['bull'],
                           belle=payload['belle'],
                           bull_tiebreak=payload['bull_tiebreak'],
                           belle_tiebreak=payload['belle_tiebreak'],
                           team_standings=payload['team_standings'],
                           unfinalized_events=unfinalized_events)


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
    if event.tournament_id != tournament.id:
        abort(404)

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
    if event.tournament_id != tournament.id:
        abort(404)

    results = event.get_results_sorted()

    return render_template('reports/event_results_print.html',
                           tournament=tournament,
                           event=event,
                           results=results)


@reporting_bp.route('/<int:tournament_id>/pro/payouts', methods=['GET', 'POST'])
def pro_payout_summary(tournament_id):
    """View pro competitor payout summary with settlement tracking."""
    tournament = Tournament.query.get_or_404(tournament_id)
    from models.competitor import ProCompetitor

    if request.method == 'POST':
        try:
            comp_id = int(request.form.get('competitor_id', ''))
        except (TypeError, ValueError):
            flash('Invalid request.', 'error')
            return redirect(url_for('reporting.pro_payout_summary', tournament_id=tournament_id))

        competitor = ProCompetitor.query.filter_by(id=comp_id, tournament_id=tournament_id).first_or_404()
        competitor.payout_settled = not competitor.payout_settled
        db.session.commit()
        log_action('payout_settlement_toggled', 'pro_competitor', comp_id, {
            'settled': competitor.payout_settled,
            'name': competitor.name,
        })
        return redirect(url_for('reporting.pro_payout_summary', tournament_id=tournament_id))

    competitors = tournament.pro_competitors.filter_by(status='active').all()
    competitors = sorted(competitors, key=lambda c: c.total_earnings, reverse=True)
    total_competitors = len(competitors)

    earners = [c for c in competitors if c.total_earnings and c.total_earnings > 0]
    total_owed = sum(c.total_earnings for c in earners)
    total_settled = sum(c.total_earnings for c in earners if c.payout_settled)
    total_outstanding = total_owed - total_settled

    # Build a mapping of competitor_id → first EventResult id with payout_amount > 0
    # so the template can wire per-result AJAX toggle buttons.
    from models.event import EventResult
    comp_ids = [c.id for c in competitors]
    if comp_ids:
        results_with_payout = (
            EventResult.query
            .join(Event, EventResult.event_id == Event.id)
            .filter(
                Event.tournament_id == tournament_id,
                EventResult.competitor_type == 'pro',
                EventResult.competitor_id.in_(comp_ids),
                EventResult.payout_amount > 0,
            )
            .order_by(EventResult.payout_amount.desc())
            .all()
        )
        # Keep only the first (highest-payout) result per competitor.
        result_id_map = {}
        for r in results_with_payout:
            if r.competitor_id not in result_id_map:
                result_id_map[r.competitor_id] = r.id
    else:
        result_id_map = {}

    return render_template('reports/payout_summary.html',
                           tournament=tournament,
                           competitors=competitors,
                           total_owed=total_owed,
                           total_settled=total_settled,
                           total_outstanding=total_outstanding,
                           earners_count=len(earners),
                           total_competitors=total_competitors,
                           result_id_map=result_id_map)


@reporting_bp.route('/<int:tournament_id>/pro/payouts/print')
def pro_payout_summary_print(tournament_id):
    """Printable version of payout summary."""
    tournament = Tournament.query.get_or_404(tournament_id)

    competitors = tournament.pro_competitors.filter_by(status='active').all()
    competitors = sorted(competitors, key=lambda c: c.total_earnings, reverse=True)
    competitors = [c for c in competitors if c.total_earnings and c.total_earnings > 0]
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


@reporting_bp.route('/<int:tournament_id>/export-results')
def export_results(tournament_id):
    """Export standings and event results to Excel."""
    tournament = Tournament.query.get_or_404(tournament_id)
    fd, path = tempfile.mkstemp(prefix=f'proam_{tournament_id}_', suffix='.xlsx')
    os.close(fd)
    export_results_to_excel(tournament, path)
    log_action('report_export_downloaded', 'tournament', tournament.id, {
        'tournament_id': tournament.id,
        'format': 'xlsx',
        'kind': 'all_results',
    })
    db.session.commit()

    @after_this_request
    def cleanup_file(response):
        try:
            os.remove(path)
        except OSError:
            pass
        return response

    download_name = f'{tournament.name}_{tournament.year}_results.xlsx'.replace(' ', '_')
    return send_file(path, as_attachment=True, download_name=download_name)


@reporting_bp.route('/<int:tournament_id>/export-chopping')
def export_chopping_results(tournament_id):
    """Export only chopping event scores/results for external handicap tools."""
    tournament = Tournament.query.get_or_404(tournament_id)
    fmt = (request.args.get('format') or 'xlsx').strip().lower()

    if fmt == 'json':
        payload = {
            'tournament': {'id': tournament.id, 'name': tournament.name, 'year': tournament.year},
            'rows': build_chopping_rows(tournament),
        }
        log_action('report_export_downloaded', 'tournament', tournament.id, {
            'tournament_id': tournament.id,
            'format': 'json',
            'kind': 'chopping_results',
        })
        db.session.commit()
        return Response(json.dumps(payload), mimetype='application/json')

    fd, path = tempfile.mkstemp(prefix=f'proam_{tournament_id}_chopping_', suffix='.xlsx')
    os.close(fd)
    export_chopping_results_to_excel(tournament, path)
    log_action('report_export_downloaded', 'tournament', tournament.id, {
        'tournament_id': tournament.id,
        'format': 'xlsx',
        'kind': 'chopping_results',
    })
    db.session.commit()

    @after_this_request
    def cleanup_file(response):
        try:
            os.remove(path)
        except OSError:
            pass
        return response

    download_name = f'{tournament.name}_{tournament.year}_chopping_results.xlsx'.replace(' ', '_')
    return send_file(path, as_attachment=True, download_name=download_name)


@reporting_bp.route('/<int:tournament_id>/export-results/async', methods=['POST'])
def export_results_async(tournament_id):
    """Start export generation as a background job for larger tournaments."""
    tournament = Tournament.query.get_or_404(tournament_id)

    def _build_export(target_tournament_id: int) -> str:
        export_tournament = Tournament.query.get(target_tournament_id)
        if not export_tournament:
            raise RuntimeError(f'Tournament {target_tournament_id} not found.')
        fd, path = tempfile.mkstemp(prefix=f'proam_{tournament_id}_', suffix='.xlsx')
        os.close(fd)
        export_results_to_excel(export_tournament, path)
        return path

    job_id = submit_job(
        f'export_results_{tournament_id}',
        _build_export,
        tournament_id,
        metadata={'tournament_id': tournament_id, 'kind': 'export_results'},
    )
    log_action('report_export_job_started', 'tournament', tournament.id, {'job_id': job_id})
    db.session.commit()
    return redirect(url_for('reporting.export_results_job_status', tournament_id=tournament_id, job_id=job_id))


@reporting_bp.route('/<int:tournament_id>/jobs/<job_id>')
def export_results_job_status(tournament_id, job_id):
    tournament = Tournament.query.get_or_404(tournament_id)
    job = get_job(job_id)
    job_meta = job.get('metadata') if job else {}
    if not job or int((job_meta or {}).get('tournament_id', -1)) != tournament_id:
        abort(404)
    job_kind = (job_meta or {}).get('kind') or ''

    if job['status'] != 'completed':
        if job['status'] == 'failed':
            flash(f"Export job failed: {job.get('error', 'Unknown error')}", 'error')
            if job_kind == 'build_pro_flights':
                return redirect(url_for('scheduling.flight_list', tournament_id=tournament_id))
            return redirect(url_for('main.tournament_detail', tournament_id=tournament_id))
        flash('Export is still running. Refresh in a moment.', 'warning')
        return render_template('reports/export_status.html', tournament=tournament, job=job)

    if job_kind == 'build_pro_flights':
        flights_built = int(job.get('result') or 0)
        flash(f'Built {flights_built} flight(s).', 'success')
        return redirect(url_for('scheduling.flight_list', tournament_id=tournament_id))

    path = job.get('result')
    if not path or not os.path.exists(path):
        flash('Export file is no longer available.', 'error')
        return redirect(url_for('main.tournament_detail', tournament_id=tournament_id))

    @after_this_request
    def cleanup_file(response):
        try:
            os.remove(path)
        except OSError:
            pass
        return response

    download_name = f'{tournament.name}_{tournament.year}_results.xlsx'.replace(' ', '_')
    return send_file(path, as_attachment=True, download_name=download_name)


@reporting_bp.route('/<int:tournament_id>/backup')
def backup_database(tournament_id):
    """Download a raw sqlite backup file for disaster recovery."""
    Tournament.query.get_or_404(tournament_id)
    if not current_user.is_authenticated or not current_user.is_admin:
        abort(403)

    uri = current_app.config.get('SQLALCHEMY_DATABASE_URI', '')
    if not uri.startswith('sqlite:///'):
        flash('Database backup download is only available for SQLite in this environment.', 'warning')
        return redirect(url_for('main.tournament_detail', tournament_id=tournament_id))

    db_path = uri.replace('sqlite:///', '', 1)
    if not os.path.isabs(db_path):
        db_path = os.path.join(current_app.instance_path, db_path)

    if not os.path.exists(db_path):
        flash('Database file not found.', 'error')
        return redirect(url_for('main.tournament_detail', tournament_id=tournament_id))

    log_action('database_backup_downloaded', 'tournament', tournament_id, {'path': db_path})
    db.session.commit()
    return send_file(db_path, as_attachment=True, download_name=f'proam_backup_{tournament_id}.db')


@reporting_bp.route('/<int:tournament_id>/restore', methods=['POST'])
def restore_database(tournament_id):
    """Restore SQLite database from an uploaded backup file."""
    Tournament.query.get_or_404(tournament_id)
    if not current_user.is_authenticated or not current_user.is_admin:
        abort(403)

    uri = current_app.config.get('SQLALCHEMY_DATABASE_URI', '')
    if not uri.startswith('sqlite:///'):
        flash('Database restore is only available for SQLite in this environment.', 'warning')
        return redirect(url_for('main.tournament_detail', tournament_id=tournament_id))

    f = request.files.get('backup_file')
    if not f or not f.filename:
        flash('Select a .db backup file to restore.', 'error')
        return redirect(url_for('main.tournament_detail', tournament_id=tournament_id))

    if not f.filename.lower().endswith('.db'):
        flash('Backup restore only accepts .db files.', 'error')
        return redirect(url_for('main.tournament_detail', tournament_id=tournament_id))

    header = f.stream.read(16)
    f.stream.seek(0)
    if not header.startswith(b'SQLite format 3'):
        flash('Uploaded file is not a valid SQLite database.', 'error')
        return redirect(url_for('main.tournament_detail', tournament_id=tournament_id))

    fd, temp_path = tempfile.mkstemp(prefix='proam_restore_', suffix='.db')
    os.close(fd)
    f.save(temp_path)

    try:
        restore_plan = prepare_sqlite_restore(
            upload_path=temp_path,
            db_uri=uri,
            instance_path=current_app.instance_path,
            malware_scan_enabled=bool(current_app.config.get('ENABLE_UPLOAD_MALWARE_SCAN', False)),
            malware_scan_command=current_app.config.get('MALWARE_SCAN_COMMAND', ''),
        )
        db_path = restore_plan['target_path']
        db.session.remove()
        db.engine.dispose()
        os.replace(temp_path, db_path)
        log_action('database_restored', 'tournament', tournament_id, {'target_path': db_path})
        db.session.commit()
        flash('Database restore complete.', 'success')
    except Exception as exc:
        db.session.rollback()
        log_action('database_restore_failed', 'tournament', tournament_id, {
            'tournament_id': tournament_id,
            'filename': f.filename,
            'error': str(exc),
        })
        db.session.commit()
        flash(f'Database restore failed: {exc}', 'error')
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass

    return redirect(url_for('main.tournament_detail', tournament_id=tournament_id))


# ---------------------------------------------------------------------------
# #21 — Pro payout settlement (merged into pro_payout_summary)
# ---------------------------------------------------------------------------

@reporting_bp.route('/<int:tournament_id>/pro/payout-settlement', methods=['GET', 'POST'])
def payout_settlement(tournament_id):
    """Redirect to merged pro payouts page."""
    return redirect(url_for('reporting.pro_payout_summary', tournament_id=tournament_id), code=301)


# ---------------------------------------------------------------------------
# Fee tracker — entry fee collection checklist
# ---------------------------------------------------------------------------

@reporting_bp.route('/<int:tournament_id>/pro/fee-tracker', methods=['GET', 'POST'])
def fee_tracker(tournament_id):
    """Consolidated view for tracking entry fee collection from pro competitors."""
    tournament = Tournament.query.get_or_404(tournament_id)
    from models.competitor import ProCompetitor

    if request.method == 'POST':
        try:
            comp_id = int(request.form.get('competitor_id', ''))
        except (TypeError, ValueError):
            flash('Invalid request.', 'error')
            return redirect(url_for('reporting.fee_tracker', tournament_id=tournament_id))

        action = request.form.get('action', 'mark_all_paid')
        competitor = ProCompetitor.query.filter_by(id=comp_id, tournament_id=tournament_id).first_or_404()

        if action == 'unmark_all':
            competitor.fees_paid = '{}'
        else:
            # Mark every enrolled event as paid
            entered = [str(eid) for eid in competitor.get_events_entered()]
            competitor.fees_paid = json.dumps({eid: True for eid in entered})

        db.session.commit()
        log_action('fee_payment_updated', 'pro_competitor', comp_id, {
            'action': action,
            'name': competitor.name,
        })
        return redirect(url_for('reporting.fee_tracker', tournament_id=tournament_id))

    # Build event id → display name lookup
    pro_events = Event.query.filter_by(tournament_id=tournament_id, event_type='pro').all()
    event_map = {str(e.id): e.display_name for e in pro_events}

    competitors = tournament.pro_competitors.filter_by(status='active').all()

    competitor_data = []
    for c in competitors:
        fees = c.get_entry_fees()
        paid = c.get_fees_paid()
        entered = [str(eid) for eid in c.get_events_entered()]

        event_rows = []
        for eid in entered:
            event_rows.append({
                'event_id': eid,
                'name': event_map.get(eid, f'Event {eid}'),
                'fee': fees.get(eid, 0),
                'paid': paid.get(eid, False),
            })

        competitor_data.append({
            'competitor': c,
            'events': event_rows,
        })

    # Sort: outstanding balance descending (those who owe most shown first)
    competitor_data.sort(key=lambda x: x['competitor'].fees_balance, reverse=True)

    total_owed = sum(d['competitor'].total_fees_owed for d in competitor_data)
    total_paid = sum(d['competitor'].total_fees_paid for d in competitor_data)
    total_outstanding = total_owed - total_paid

    return render_template(
        'reporting/fee_tracker.html',
        tournament=tournament,
        competitor_data=competitor_data,
        total_owed=total_owed,
        total_paid=total_paid,
        total_outstanding=total_outstanding,
    )


# ---------------------------------------------------------------------------
# Pro event fee configuration — bulk-set entry fees per event
# ---------------------------------------------------------------------------

@reporting_bp.route('/<int:tournament_id>/pro/event-fees', methods=['GET', 'POST'])
def pro_event_fees(tournament_id):
    """Set default entry fees per event and bulk-apply to enrolled competitors.

    POST fields:
      fee_<event_id>   — fee amount for that event (float, blank to skip)
      overwrite        — if present, overwrite existing non-zero fees too
    """
    tournament = Tournament.query.get_or_404(tournament_id)
    from models.competitor import ProCompetitor

    pro_events = Event.query.filter_by(
        tournament_id=tournament_id,
        event_type='pro',
    ).order_by(Event.name, Event.gender).all()

    if request.method == 'POST':
        overwrite = bool(request.form.get('overwrite'))
        updated_count = 0

        for event in pro_events:
            raw = request.form.get(f'fee_{event.id}', '').strip()
            if not raw:
                continue
            try:
                fee_amount = float(raw)
            except (TypeError, ValueError):
                flash(f'Invalid fee amount for {event.display_name}: {raw!r}', 'error')
                continue

            # Apply to every active competitor enrolled in this event.
            competitors = ProCompetitor.query.filter_by(
                tournament_id=tournament_id,
                status='active',
            ).all()
            for comp in competitors:
                entered = [str(eid) for eid in comp.get_events_entered()]
                if str(event.id) not in entered:
                    continue
                existing_fees = comp.get_entry_fees()
                existing = existing_fees.get(str(event.id), 0)
                if existing and not overwrite:
                    continue
                comp.set_entry_fee(event.id, fee_amount)
                updated_count += 1

        db.session.commit()
        log_action('pro_event_fees_configured', 'tournament', tournament_id, {
            'event_count': len(pro_events),
            'overwrite': overwrite,
            'competitor_fees_updated': updated_count,
        })
        flash(f'Entry fees applied to {updated_count} competitor-event record(s).', 'success')
        return redirect(url_for('reporting.pro_event_fees', tournament_id=tournament_id))

    # Build display data: per event, count enrolled and sum of fees already set.
    competitors_all = ProCompetitor.query.filter_by(
        tournament_id=tournament_id, status='active',
    ).all()

    event_rows = []
    for event in pro_events:
        enrolled = [
            c for c in competitors_all
            if str(event.id) in [str(eid) for eid in c.get_events_entered()]
        ]
        fee_values = [
            c.get_entry_fees().get(str(event.id), 0)
            for c in enrolled
        ]
        set_count = sum(1 for v in fee_values if v)
        # Suggest the most common non-zero fee, or 0 if none set yet.
        from collections import Counter
        nonzero = [v for v in fee_values if v]
        suggested = Counter(nonzero).most_common(1)[0][0] if nonzero else 0
        event_rows.append({
            'event': event,
            'enrolled_count': len(enrolled),
            'fee_set_count': set_count,
            'suggested_fee': suggested,
        })

    return render_template(
        'reporting/event_fee_config.html',
        tournament=tournament,
        event_rows=event_rows,
    )


# ---------------------------------------------------------------------------
# #25 — Cloud / local backup
# ---------------------------------------------------------------------------

@reporting_bp.route('/<int:tournament_id>/backup/cloud', methods=['POST'])
def cloud_backup(tournament_id):
    """Trigger an S3 cloud backup (or local fallback) and return JSON status."""
    from flask import jsonify
    Tournament.query.get_or_404(tournament_id)
    if not current_user.is_authenticated or not current_user.is_admin:
        abort(403)

    uri = current_app.config.get('SQLALCHEMY_DATABASE_URI', '')
    instance_path = current_app.instance_path
    from services.backup import backup_database

    def _run_backup():
        return backup_database(uri, tournament_id, instance_path)

    job_id = submit_job(
        f'backup:t{tournament_id}',
        _run_backup,
        metadata={'tournament_id': tournament_id, 'kind': 'backup'},
    )

    log_action('cloud_backup_triggered', 'tournament', tournament_id, {'job_id': job_id})
    db.session.commit()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'ok': True, 'job_id': job_id,
                        'message': 'Backup started in background.'}), 202

    flash('Database backup started in background.', 'success')
    return redirect(url_for('main.tournament_detail', tournament_id=tournament_id))


# ---------------------------------------------------------------------------
# ALA Membership Status Report
# ---------------------------------------------------------------------------

@reporting_bp.route('/ala-membership-report/<int:tournament_id>')
def ala_membership_report(tournament_id):
    """Admin-only ALA membership status report for all active pro competitors."""
    if not current_user.is_authenticated or not current_user.is_admin:
        abort(403)

    tournament = Tournament.query.get_or_404(tournament_id)
    from services.ala_report import build_ala_report

    report = build_ala_report(tournament)

    return render_template(
        'reporting/ala_membership_report.html',
        tournament=tournament,
        all_attendees=report['all_attendees'],
        non_members=report['non_members'],
        generated_at=report['generated_at'],
        year=report['year'],
    )


@reporting_bp.route('/ala-membership-report/<int:tournament_id>/pdf')
def ala_membership_report_pdf(tournament_id):
    """Download ALA membership report as PDF."""
    if not current_user.is_authenticated or not current_user.is_admin:
        abort(403)

    tournament = Tournament.query.get_or_404(tournament_id)
    from services.ala_report import build_ala_report, generate_ala_pdf

    report = build_ala_report(tournament)

    try:
        path = generate_ala_pdf(report)
    except Exception as exc:
        flash(f'PDF generation failed: {exc}', 'error')
        return redirect(url_for('reporting.ala_membership_report', tournament_id=tournament_id))

    @after_this_request
    def cleanup_file(response):
        try:
            os.remove(path)
        except OSError:
            pass
        return response

    from datetime import datetime
    log_action('ala_report_downloaded', 'tournament', tournament.id, {
        'tournament_id': tournament.id,
        'format': 'pdf',
    })
    db.session.commit()
    download_name = f'ala_report_{datetime.now().strftime("%Y%m%d")}.pdf'
    return send_file(path, as_attachment=True, download_name=download_name)


ALA_EMAIL = 'americanlumberjacks@gmail.com'


@reporting_bp.route('/ala-membership-report/<int:tournament_id>/email', methods=['POST'])
def ala_email_report(tournament_id):
    """Generate ALA PDF and email it to the ALA."""
    if not current_user.is_authenticated or not current_user.is_admin:
        abort(403)

    tournament = Tournament.query.get_or_404(tournament_id)
    from services.ala_report import build_ala_report, generate_ala_pdf

    report = build_ala_report(tournament)

    try:
        path = generate_ala_pdf(report)
    except Exception as exc:
        flash(f'PDF generation failed: {exc}', 'error')
        return redirect(url_for('reporting.ala_membership_report', tournament_id=tournament_id))

    try:
        _send_ala_email(path, tournament, report)
        log_action('ala_report_emailed', 'tournament', tournament.id, {
            'tournament_id': tournament.id,
            'recipient': ALA_EMAIL,
        })
        db.session.commit()
        flash(f'ALA report emailed to {ALA_EMAIL}.', 'success')
    except Exception as exc:
        db.session.rollback()
        log_action('ala_report_email_failed', 'tournament', tournament.id, {
            'tournament_id': tournament.id,
            'recipient': ALA_EMAIL,
            'error': str(exc),
        })
        db.session.commit()
        flash(f'Email failed: {exc}', 'error')
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    return redirect(url_for('reporting.ala_membership_report', tournament_id=tournament_id))


def _send_ala_email(pdf_path, tournament, report):
    """Send ALA report PDF via SMTP (uses same config as sms_notify)."""
    import smtplib
    from datetime import datetime
    from email import encoders
    from email.mime.base import MIMEBase
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    smtp_host = os.environ.get('SMTP_HOST', '')
    smtp_port = int(os.environ.get('SMTP_PORT', 587))
    smtp_user = os.environ.get('SMTP_USER', '')
    smtp_password = os.environ.get('SMTP_PASSWORD', '')
    from_addr = os.environ.get('SMTP_FROM', smtp_user)

    if not smtp_host or not smtp_user:
        raise RuntimeError('SMTP not configured. Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD env vars.')

    total = len(report['all_attendees'])
    members = total - len(report['non_members'])
    non_members = len(report['non_members'])
    year = report.get('year', datetime.now().year)

    msg = MIMEMultipart()
    msg['From'] = from_addr
    msg['To'] = ALA_EMAIL
    msg['Subject'] = f'Missoula Pro-Am {year} — ALA Membership Report'

    body = (
        f'Attached is the ALA membership report for the Missoula Pro-Am {year}.\n\n'
        f'Total pro competitors: {total}\n'
        f'ALA members: {members}\n'
        f'Non-members: {non_members}\n\n'
        f'Generated: {report["generated_at"]}\n'
    )
    msg.attach(MIMEText(body, 'plain'))

    with open(pdf_path, 'rb') as f:
        part = MIMEBase('application', 'pdf')
        part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', f'attachment; filename="ala_report_{year}.pdf"')
        msg.attach(part)

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)
