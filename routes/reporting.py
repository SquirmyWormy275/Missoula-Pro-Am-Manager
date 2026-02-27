"""
Reporting routes for standings, results, and exports.
"""
import json
import os
import tempfile
from flask import Blueprint, render_template, Response, abort, send_file, after_this_request, redirect, url_for, flash, current_app, request
try:
    from flask_login import current_user
except ModuleNotFoundError:
    class _AnonymousCurrentUser:
        is_authenticated = False
        is_admin = False

    current_user = _AnonymousCurrentUser()
from database import db
from models import Tournament, Event
from services.excel_io import export_results_to_excel
from services.audit import log_action
from services.background_jobs import get as get_job, submit as submit_job
from services.report_cache import get as cache_get, set as cache_set
from services.upload_security import malware_scan

reporting_bp = Blueprint('reporting', __name__)


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
    """View college standings (Bull/Belle of Woods and Team Standings)."""
    tournament = Tournament.query.get_or_404(tournament_id)

    payload = _cached_payload(
        f'reports:{tournament_id}:college_standings',
        lambda: {
            'bull': tournament.get_bull_of_woods(10),
            'belle': tournament.get_belle_of_woods(10),
            'team_standings': tournament.get_team_standings(),
        }
    )

    return render_template('reports/college_standings.html',
                           tournament=tournament,
                           bull=payload['bull'],
                           belle=payload['belle'],
                           team_standings=payload['team_standings'])


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


@reporting_bp.route('/<int:tournament_id>/pro/payouts')
def pro_payout_summary(tournament_id):
    """View pro competitor payout summary."""
    tournament = Tournament.query.get_or_404(tournament_id)

    payload = _cached_payload(
        f'reports:{tournament_id}:pro_payouts',
        lambda: _build_payout_payload(tournament)
    )

    return render_template('reports/payout_summary.html',
                           tournament=tournament,
                           competitors=payload['competitors'],
                           total_paid=payload['total_paid'])


@reporting_bp.route('/<int:tournament_id>/pro/payouts/print')
def pro_payout_summary_print(tournament_id):
    """Printable version of payout summary."""
    tournament = Tournament.query.get_or_404(tournament_id)

    payload = _cached_payload(
        f'reports:{tournament_id}:pro_payouts',
        lambda: _build_payout_payload(tournament)
    )
    competitors = [c for c in payload['competitors'] if c.total_earnings > 0]
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

    @after_this_request
    def cleanup_file(response):
        try:
            os.remove(path)
        except OSError:
            pass
        return response

    download_name = f'{tournament.name}_{tournament.year}_results.xlsx'.replace(' ', '_')
    return send_file(path, as_attachment=True, download_name=download_name)


@reporting_bp.route('/<int:tournament_id>/export-results/async', methods=['POST'])
def export_results_async(tournament_id):
    """Start export generation as a background job for larger tournaments."""
    tournament = Tournament.query.get_or_404(tournament_id)

    def _build_export() -> str:
        fd, path = tempfile.mkstemp(prefix=f'proam_{tournament_id}_', suffix='.xlsx')
        os.close(fd)
        export_results_to_excel(tournament, path)
        return path

    job_id = submit_job(f'export_results_{tournament_id}', _build_export)
    log_action('report_export_job_started', 'tournament', tournament.id, {'job_id': job_id})
    db.session.commit()
    return redirect(url_for('reporting.export_results_job_status', tournament_id=tournament_id, job_id=job_id))


@reporting_bp.route('/<int:tournament_id>/jobs/<job_id>')
def export_results_job_status(tournament_id, job_id):
    tournament = Tournament.query.get_or_404(tournament_id)
    job = get_job(job_id)
    if not job:
        abort(404)

    if job['status'] != 'completed':
        if job['status'] == 'failed':
            flash(f"Export job failed: {job.get('error', 'Unknown error')}", 'error')
            return redirect(url_for('main.tournament_detail', tournament_id=tournament_id))
        flash('Export is still running. Refresh in a moment.', 'warning')
        return render_template('reports/export_status.html', tournament=tournament, job=job)

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
        malware_scan(
            temp_path,
            enabled=bool(current_app.config.get('ENABLE_UPLOAD_MALWARE_SCAN', False)),
            command_template=current_app.config.get('MALWARE_SCAN_COMMAND', '')
        )
        db_path = uri.replace('sqlite:///', '', 1)
        if not os.path.isabs(db_path):
            db_path = os.path.join(current_app.instance_path, db_path)
        db.session.remove()
        db.engine.dispose()
        os.replace(temp_path, db_path)
        log_action('database_restored', 'tournament', tournament_id, {'target_path': db_path})
        db.session.commit()
        flash('Database restore complete.', 'success')
    except Exception as exc:
        flash(f'Database restore failed: {exc}', 'error')
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass

    return redirect(url_for('main.tournament_detail', tournament_id=tournament_id))


def _build_payout_payload(tournament: Tournament) -> dict:
    competitors = tournament.pro_competitors.filter_by(status='active').all()
    competitors = sorted(competitors, key=lambda c: c.total_earnings, reverse=True)
    total_paid = sum(c.total_earnings for c in competitors)
    return {'competitors': competitors, 'total_paid': total_paid}
