"""In-process background job execution for long-running tasks."""
from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from database import db
from models.background_job import BackgroundJob
from services.time_utils import utc_now_naive

_executor = ThreadPoolExecutor(max_workers=2)
_jobs = {}
_lock = threading.Lock()
_app = None


def configure(max_workers: int, app=None) -> None:
    global _app, _executor
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
):
    if _app is None:
        return

    with _app.app_context():
        row = db.session.get(BackgroundJob, job_id)
        if row is None:
            row = BackgroundJob(id=job_id)
            db.session.add(row)
        if label is not None:
            row.label = label
        if status is not None:
            row.status = status
        if submitted_at is not None:
            row.submitted_at = submitted_at
        if started_at is not None:
            row.started_at = started_at
        if finished_at is not None:
            row.finished_at = finished_at
        if error is not None:
            row.error_text = error
        if metadata is not None:
            row.metadata_json = _serialize_json(metadata)
            row.tournament_id = (metadata or {}).get('tournament_id')
        if result is not None:
            row.result_json = _serialize_json(result)
        db.session.commit()


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
        }
    _persist_job(
        job_id,
        label=label,
        status='queued',
        submitted_at=submitted_at,
        metadata=dict(metadata or {}),
    )

    future = _executor.submit(_run_with_app_context, fn, *args, **kwargs)

    def _done_callback(done_future):
        with _lock:
            job = _jobs.get(job_id)
            if not job:
                return
            try:
                job['result'] = done_future.result()
                job['status'] = 'completed'
            except Exception as exc:
                job['status'] = 'failed'
                job['error'] = str(exc)
            job['finished_at'] = utc_now_naive()
            snapshot = _snapshot(job)
        _persist_job(
            job_id,
            status=snapshot['status'],
            finished_at=snapshot['finished_at'],
            result=snapshot['result'],
            error=snapshot['error'],
        )

    with _lock:
        _jobs[job_id]['status'] = 'running'
        _jobs[job_id]['future'] = future
        started_at = utc_now_naive()
        _jobs[job_id]['started_at'] = started_at
    _persist_job(job_id, status='running', started_at=started_at)

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


def list_recent(limit: int = 20) -> list[dict]:
    """Return the most recent jobs first for operator diagnostics."""
    if limit < 1:
        return []
    if _app is not None:
        with _app.app_context():
            rows = (
                BackgroundJob.query
                .order_by(BackgroundJob.submitted_at.desc())
                .limit(limit)
                .all()
            )
            return [_row_to_dict(row) for row in rows]
    with _lock:
        rows = [_snapshot(job) for job in _jobs.values()]
    rows.sort(
        key=lambda job: job.get('submitted_at') or datetime.min,
        reverse=True,
    )
    return rows[:limit]

