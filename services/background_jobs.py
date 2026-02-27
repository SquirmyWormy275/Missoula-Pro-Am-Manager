"""In-process background job execution for long-running tasks."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import threading
import uuid


_executor = ThreadPoolExecutor(max_workers=2)
_jobs = {}
_lock = threading.Lock()


def configure(max_workers: int) -> None:
    global _executor
    if max_workers < 1:
        max_workers = 1
    _executor = ThreadPoolExecutor(max_workers=max_workers)


def submit(label: str, fn, *args, **kwargs) -> str:
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
        }

    future = _executor.submit(fn, *args, **kwargs)

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
        }

