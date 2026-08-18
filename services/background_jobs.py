"""In-process background job execution for long-running tasks."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from sqlalchemy import inspect, or_
from sqlalchemy.exc import SQLAlchemyError

from database import db
from models.background_job import BackgroundJob
from services.time_utils import utc_now_naive

_executor = ThreadPoolExecutor(max_workers=2)
_jobs = {}
_lock = threading.Lock()
_app = None
_BOOT_ID = uuid.uuid4().hex
_heartbeat_thread = None
_heartbeat_stop = threading.Event()
PROCESS_MODEL = 'one-gunicorn-worker-with-threads'
logger = logging.getLogger(__name__)
_PERSIST_RETRY_ATTEMPTS = 3
_PERSIST_RETRY_DELAY_SECONDS = 0.05


def boot_id() -> str:
    """Return the immutable owner ID for this worker process boot."""
    return _BOOT_ID


def _assert_single_process_job_model() -> None:
    configured = os.environ.get('WEB_CONCURRENCY', '').strip()
    if configured and configured != '1':
        raise RuntimeError(
            'In-process background jobs require WEB_CONCURRENCY=1. '
            f'Configured value: {configured}.'
        )


def configure(max_workers: int, app=None) -> None:
    global _app, _executor
    _assert_single_process_job_model()
    if max_workers < 1:
        max_workers = 1
    try:
        _executor.shutdown(wait=False, cancel_futures=True)
    except TypeError:
        _executor.shutdown(wait=False)
    _executor = ThreadPoolExecutor(max_workers=max_workers)
    if app is not None:
        _app = app


def _run_with_app_context(fn, *args, **kwargs):
    if _app is None:
        return fn(*args, **kwargs)
    with _app.app_context():
        return fn(*args, **kwargs)


def _serialize_json(value):
    if value is None:
        return None
    try:
        return json.dumps(value)
    except TypeError:
        return json.dumps(str(value))


def _deserialize_json(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def _persist_job(
    job_id: str,
    *,
    label: str | None = None,
    status: str | None = None,
    submitted_at: datetime | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    result=None,
    error: str | None = None,
    metadata: dict | None = None,
    owner_boot_id: str | None = None,
    owner_heartbeat_at: datetime | None = None,
    require_active_owner: bool = False,
):
    if _app is None:
        return

    with _app.app_context():
        updates = {}
        if label is not None:
            updates['label'] = label
        if status is not None:
            updates['status'] = status
        if submitted_at is not None:
            updates['submitted_at'] = submitted_at
        if started_at is not None:
            updates['started_at'] = started_at
        if finished_at is not None:
            updates['finished_at'] = finished_at
        if error is not None:
            updates['error_text'] = error
        if metadata is not None:
            updates['metadata_json'] = _serialize_json(metadata)
            updates['tournament_id'] = (metadata or {}).get('tournament_id')
        if owner_boot_id is not None:
            updates['owner_boot_id'] = owner_boot_id
        if owner_heartbeat_at is not None:
            updates['owner_heartbeat_at'] = owner_heartbeat_at
        if result is not None:
            updates['result_json'] = _serialize_json(result)

        if require_active_owner:
            # Keep owner/status verification and the completion write in one
            # SQL statement. A concurrent reconciliation that wins the row
            # lock changes the predicate and this update affects zero rows;
            # an old process can never overwrite ``interrupted`` afterward.
            updated = (
                BackgroundJob.query
                .filter_by(id=job_id, owner_boot_id=_BOOT_ID)
                .filter(BackgroundJob.status.in_(('queued', 'running')))
                .update(updates, synchronize_session=False)
            )
            db.session.commit()
            return updated == 1

        row = db.session.get(BackgroundJob, job_id)
        if row is None:
            row = BackgroundJob(id=job_id)
            db.session.add(row)
        for attribute, value in updates.items():
            setattr(row, attribute, value)
        db.session.commit()
        return True


def _persist_job_with_retry(job_id: str, **updates):
    """Persist a lifecycle transition, tolerating brief database outages."""
    for attempt in range(1, _PERSIST_RETRY_ATTEMPTS + 1):
        try:
            return _persist_job(job_id, **updates)
        except SQLAlchemyError:
            if _app is not None:
                with _app.app_context():
                    db.session.rollback()
            if attempt == _PERSIST_RETRY_ATTEMPTS:
                logger.error(
                    'Background job %s lifecycle write failed after %d attempts.',
                    job_id,
                    attempt,
                    exc_info=True,
                )
                return None
            time.sleep(_PERSIST_RETRY_DELAY_SECONDS * attempt)


def _persist_terminal_snapshot(job_id: str, snapshot: dict) -> bool:
    persisted = _persist_job_with_retry(
        job_id,
        status=snapshot['status'],
        finished_at=snapshot['finished_at'],
        result=snapshot['result'],
        error=snapshot['error'],
        owner_heartbeat_at=snapshot['finished_at'],
        require_active_owner=True,
    )
    resolved = persisted is not None
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            job['terminal_persisted'] = resolved
    return resolved


def _snapshot(job: dict) -> dict:
    return {
        'id': job['id'],
        'label': job['label'],
        'status': job['status'],
        'submitted_at': job['submitted_at'],
        'finished_at': job['finished_at'],
        'result': job['result'],
        'error': job['error'],
        'metadata': dict(job.get('metadata') or {}),
        'owner_boot_id': job.get('owner_boot_id'),
        'owner_heartbeat_at': job.get('owner_heartbeat_at'),
    }


def _row_to_dict(row: BackgroundJob) -> dict:
    return {
        'id': row.id,
        'label': row.label,
        'status': row.status,
        'submitted_at': row.submitted_at,
        'finished_at': row.finished_at,
        'result': _deserialize_json(row.result_json),
        'error': row.error_text,
        'metadata': _deserialize_json(row.metadata_json) or {},
        'owner_boot_id': row.owner_boot_id,
        'owner_heartbeat_at': row.owner_heartbeat_at,
    }


def submit(label: str, fn, *args, metadata: dict | None = None, **kwargs) -> str:
    job_id = uuid.uuid4().hex
    submitted_at = utc_now_naive()
    with _lock:
        _jobs[job_id] = {
            'id': job_id,
            'label': label,
            'status': 'queued',
            'submitted_at': submitted_at,
            'finished_at': None,
            'result': None,
            'error': None,
            'metadata': dict(metadata or {}),
            'owner_boot_id': _BOOT_ID,
            'owner_heartbeat_at': submitted_at,
            'terminal_persisted': False,
        }
    _persist_job(
        job_id,
        label=label,
        status='queued',
        submitted_at=submitted_at,
        metadata=dict(metadata or {}),
        owner_boot_id=_BOOT_ID,
        owner_heartbeat_at=submitted_at,
    )

    def _run_job():
        started_at = utc_now_naive()
        with _lock:
            job = _jobs.get(job_id)
            if job is not None:
                job['status'] = 'running'
                job['started_at'] = started_at
                job['owner_heartbeat_at'] = started_at
        owner_active = _persist_job_with_retry(
            job_id,
            status='running',
            started_at=started_at,
            owner_heartbeat_at=started_at,
            require_active_owner=True,
        )
        if owner_active is False:
            raise RuntimeError('Background job ownership expired before execution.')
        return _run_with_app_context(fn, *args, **kwargs)

    future = _executor.submit(_run_job)

    def _done_callback(done_future):
        with _lock:
            job = _jobs.get(job_id)
            if not job:
                return
            try:
                job['result'] = done_future.result()
                if (
                    isinstance(job['result'], Mapping)
                    and job['result'].get('ok') is False
                ):
                    job['status'] = 'failed'
                    job['error'] = str(
                        job['result'].get('error')
                        or job['result'].get('message')
                        or 'Background job returned ok=false.'
                    )
                else:
                    job['status'] = 'completed'
            except Exception as exc:
                job['status'] = 'failed'
                job['error'] = str(exc)
            job['finished_at'] = utc_now_naive()
            job['owner_heartbeat_at'] = job['finished_at']
            job['terminal_persisted'] = False
            snapshot = _snapshot(job)
        _persist_terminal_snapshot(job_id, snapshot)

    with _lock:
        _jobs[job_id]['future'] = future

    future.add_done_callback(_done_callback)
    return job_id


def get(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        if job:
            return _snapshot(job)
    if _app is None:
        return None
    with _app.app_context():
        row = db.session.get(BackgroundJob, job_id)
        if row is None:
            return None
        return _row_to_dict(row)


def list_recent(limit: int = 20, tournament_id: int | None = None) -> list[dict]:
    """Return the most recent jobs first for operator diagnostics."""
    if limit < 1:
        return []
    if _app is not None:
        with _app.app_context():
            query = BackgroundJob.query
            if tournament_id is not None:
                query = query.filter(BackgroundJob.tournament_id == tournament_id)
            rows = query.order_by(BackgroundJob.submitted_at.desc()).limit(limit).all()
            return [_row_to_dict(row) for row in rows]
    with _lock:
        rows = [_snapshot(job) for job in _jobs.values()]
    if tournament_id is not None:
        rows = [
            row for row in rows
            if (row.get('metadata') or {}).get('tournament_id') == tournament_id
        ]
    rows.sort(
        key=lambda job: job.get('submitted_at') or datetime.min,
        reverse=True,
    )
    return rows[:limit]


def reconcile_interrupted_jobs(
    app=None,
    *,
    now: datetime | None = None,
    lease_timeout_seconds: int | None = None,
) -> int:
    """Mark queued/running rows from prior process boots as interrupted.

    The deployment contract is one gunicorn worker with threads. A different
    boot can still be alive during a graceful traffic handoff, so ownership is
    considered abandoned only after its heartbeat lease expires.
    """
    active_app = app or _app
    if active_app is None:
        return 0

    with active_app.app_context():
        try:
            inspector = inspect(db.engine)
            if not inspector.has_table(BackgroundJob.__tablename__):
                return 0
            columns = {
                column['name']
                for column in inspector.get_columns(BackgroundJob.__tablename__)
            }
            if not {'owner_boot_id', 'owner_heartbeat_at'} <= columns:
                return 0
            current_time = now or utc_now_naive()
            timeout = lease_timeout_seconds
            if timeout is None:
                timeout = int(active_app.config.get('JOB_LEASE_TIMEOUT_SECONDS', 30))
            cutoff = current_time - timedelta(seconds=max(1, timeout))
            rows = (
                BackgroundJob.query
                .filter(BackgroundJob.status.in_(('queued', 'running')))
                .filter(or_(
                    BackgroundJob.owner_boot_id.is_(None),
                    BackgroundJob.owner_boot_id != _BOOT_ID,
                ))
                .filter(or_(
                    BackgroundJob.owner_heartbeat_at < cutoff,
                    BackgroundJob.owner_heartbeat_at.is_(None),
                ))
                .all()
            )
            finished_at = current_time
            for row in rows:
                row.status = 'interrupted'
                row.finished_at = finished_at
                row.error_text = (
                    'Worker process restarted before this in-process job finished; '
                    'the job cannot resume.'
                )
            db.session.commit()
            return len(rows)
        except SQLAlchemyError:
            db.session.rollback()
            logger.warning(
                'Background job reconciliation deferred after a database error.',
                exc_info=True,
            )
            return 0


def _heartbeat_once(app) -> None:
    with _lock:
        terminal_job_ids = {
            job_id
            for job_id, job in _jobs.items()
            if job.get('status') in {'completed', 'failed'}
        }
    with app.app_context():
        now = utc_now_naive()
        query = BackgroundJob.query.filter(
            BackgroundJob.owner_boot_id == _BOOT_ID,
            BackgroundJob.status.in_(('queued', 'running')),
        )
        if terminal_job_ids:
            query = query.filter(BackgroundJob.id.notin_(terminal_job_ids))
        query.update(
            {BackgroundJob.owner_heartbeat_at: now},
            synchronize_session=False,
        )
        db.session.commit()


def _retry_unpersisted_terminal_jobs() -> None:
    with _lock:
        pending = [
            (job_id, _snapshot(job))
            for job_id, job in _jobs.items()
            if job.get('status') in {'completed', 'failed'}
            and not job.get('terminal_persisted')
        ]
    for job_id, snapshot in pending:
        _persist_terminal_snapshot(job_id, snapshot)


def _heartbeat_loop(app, interval_seconds: int) -> None:
    while not _heartbeat_stop.wait(max(1, interval_seconds)):
        try:
            _retry_unpersisted_terminal_jobs()
            _heartbeat_once(app)
            reconcile_interrupted_jobs(app)
        except SQLAlchemyError:
            with app.app_context():
                db.session.rollback()
            logger.warning('Background job heartbeat failed.', exc_info=True)


def start_heartbeat(app) -> None:
    """Start the production lease heartbeat after database initialization."""
    global _heartbeat_thread
    uri = str(app.config.get('SQLALCHEMY_DATABASE_URI', ''))
    testing = (
        bool(app.config.get('TESTING'))
        or os.environ.get('FLASK_ENV', '').strip().lower() == 'testing'
        or bool(os.environ.get('TESTING', '').strip())
        or os.environ.get('PROAM_UNIT_PG', '').strip() == '1'
    )
    if testing or not uri.startswith('postgresql'):
        return
    if _heartbeat_thread is not None and _heartbeat_thread.is_alive():
        return
    with app.app_context():
        inspector = inspect(db.engine)
        if not inspector.has_table(BackgroundJob.__tablename__):
            return
        columns = {
            column['name']
            for column in inspector.get_columns(BackgroundJob.__tablename__)
        }
        if not {'owner_boot_id', 'owner_heartbeat_at'} <= columns:
            return
    _heartbeat_stop.clear()
    interval = int(app.config.get('JOB_HEARTBEAT_INTERVAL_SECONDS', 5))
    _heartbeat_once(app)
    _heartbeat_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(app, interval),
        name='proam-job-heartbeat',
        daemon=True,
    )
    _heartbeat_thread.start()

