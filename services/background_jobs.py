"""In-process background job execution for long-running tasks."""
from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

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
    _app = app


def _run_with_app_context(fn, *args, **kwargs):
    if _app is None:
        return fn(*args, **kwargs)
    with _app.app_context():
        return fn(*args, **kwargs)


def submit(label: str, fn, *args, metadata: dict | None = None, **kwargs) -> str:
    job_id = uuid.uuid4().hex
    with _lock:
        _jobs[job_id] = {
            'id': job_id,
            'label': label,
            'status': 'queued',
            'submitted_at': datetime.utcnow(),
            'finished_at': None,
            'result': None,
            'error': None,
            'metadata': dict(metadata or {}),
        }

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
            job['finished_at'] = datetime.utcnow()

    with _lock:
        _jobs[job_id]['status'] = 'running'
        _jobs[job_id]['future'] = future

    future.add_done_callback(_done_callback)
    return job_id


def get(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return None
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


def list_recent(limit: int = 20) -> list[dict]:
    """Return the most recent jobs first for operator diagnostics."""
    if limit < 1:
        return []
    with _lock:
        rows = [
            {
                'id': job['id'],
                'label': job['label'],
                'status': job['status'],
                'submitted_at': job['submitted_at'],
                'finished_at': job['finished_at'],
                'result': job['result'],
                'error': job['error'],
                'metadata': dict(job.get('metadata') or {}),
            }
            for job in _jobs.values()
        ]
    rows.sort(
        key=lambda job: job.get('submitted_at') or datetime.min,
        reverse=True,
    )
    return rows[:limit]

