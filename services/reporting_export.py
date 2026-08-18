"""Reporting export workflow helpers.

Routes should handle HTTP concerns; this module owns export file creation,
download naming, JSON payload assembly, and async job submission details.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import tempfile
import threading
import time
from pathlib import Path

from database import db
from models import Tournament
from services.background_jobs import submit as submit_job
from services.excel_io import export_results_to_excel
from services.handicap_export import build_chopping_rows, export_chopping_results_to_excel

_EXPORT_PREFIX = 'proam_export_'
_EXPORT_MAX_AGE_SECONDS = 60 * 60
_EXPORT_MAX_FILES = 100
_export_reservation_lock = threading.Lock()


def safe_download_name(tournament: Tournament, suffix: str) -> str:
    """Return a stable attachment filename for a tournament export."""
    return f'{tournament.name}_{tournament.year}_{suffix}'.replace(' ', '_')


def _reserve_export_path(tournament_id: int, *, suffix: str = '.xlsx', label: str = '') -> str:
    with _export_reservation_lock:
        prune_export_artifacts(reserve_slots=1)
        prefix = f'{_EXPORT_PREFIX}{tournament_id}_'
        if label:
            prefix = f'{prefix}{label}_'
        fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix)
        os.close(fd)
        return path


def prune_export_artifacts(
    *,
    directory: str | None = None,
    now: float | None = None,
    max_age_seconds: int = _EXPORT_MAX_AGE_SECONDS,
    max_files: int = _EXPORT_MAX_FILES,
    reserve_slots: int = 0,
) -> int:
    """Bound retryable async artifacts by age and count.

    Files remain available for repeat download for up to one hour. Each new
    export performs best-effort cleanup; open Windows files are left for the
    next pass instead of making a new export fail.
    """
    root = Path(directory or tempfile.gettempdir())
    current_time = time.time() if now is None else float(now)
    candidates = []
    for path in root.glob(f'{_EXPORT_PREFIX}*.xlsx'):
        try:
            if path.is_file():
                candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    candidates.sort(key=lambda item: (item[0], item[1].name))
    expired = {
        path for modified, path in candidates
        if current_time - modified > max(1, int(max_age_seconds))
    }
    survivors = [item for item in candidates if item[1] not in expired]
    overflow = max(
        0,
        len(survivors) + max(0, int(reserve_slots)) - max(0, int(max_files)),
    )
    doomed = expired | {path for _modified, path in survivors[:overflow]}
    removed = 0
    for path in doomed:
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


class ExportArtifactError(RuntimeError):
    """Raised when an export cannot be opened and verified for delivery."""


def open_verified_export(job: dict):
    """Open and hash the exact file descriptor that the response will stream."""
    artifact = job.get('result')
    if not isinstance(artifact, dict):
        raise ExportArtifactError(
            'Export artifact has no checksum metadata and cannot be verified.'
        )
    path = artifact.get('path')
    expected_sha256 = artifact.get('sha256')
    if not path or not expected_sha256:
        raise ExportArtifactError(
            'Export artifact has no checksum metadata and cannot be verified.'
        )
    try:
        handle = open(path, 'rb')
    except OSError as exc:
        raise ExportArtifactError(
            'Export artifact is no longer readable on this application instance.'
        ) from exc
    try:
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
        if not hmac.compare_digest(digest.hexdigest(), str(expected_sha256).lower()):
            raise ExportArtifactError(
                'Export artifact checksum verification failed; the file changed.'
            )
        handle.seek(0)
        return handle
    except BaseException:
        handle.close()
        raise


def build_results_export(tournament: Tournament) -> dict:
    """Create a full results Excel export and return file metadata."""
    path = _reserve_export_path(tournament.id, suffix='.xlsx')
    export_results_to_excel(tournament, path)
    return {
        'path': path,
        'download_name': safe_download_name(tournament, 'results.xlsx'),
        'format': 'xlsx',
        'kind': 'all_results',
        'sha256': _sha256_file(path),
    }


def build_chopping_export(tournament: Tournament) -> dict:
    """Create a chopping-only Excel export and return file metadata."""
    path = _reserve_export_path(tournament.id, suffix='.xlsx', label='chopping')
    export_chopping_results_to_excel(tournament, path)
    return {
        'path': path,
        'download_name': safe_download_name(tournament, 'chopping_results.xlsx'),
        'format': 'xlsx',
        'kind': 'chopping_results',
        'sha256': _sha256_file(path),
    }


def build_chopping_json_payload(tournament: Tournament) -> dict:
    """Return the JSON payload for chopping-only handicap tooling."""
    return {
        'tournament': {
            'id': tournament.id,
            'name': tournament.name,
            'year': tournament.year,
        },
        'rows': build_chopping_rows(tournament),
    }


def build_results_export_for_job(tournament_id: int) -> dict:
    """Background-job entry point for a full results export."""
    tournament = db.session.get(Tournament, tournament_id)
    if not tournament:
        raise RuntimeError(f'Tournament {tournament_id} not found.')
    return build_results_export(tournament)


def submit_results_export_job(tournament_id: int) -> str:
    """Submit a tournament-bound background results export."""
    return submit_job(
        f'export_results_{tournament_id}',
        build_results_export_for_job,
        tournament_id,
        metadata={'tournament_id': tournament_id, 'kind': 'export_results'},
    )


def build_video_judge_export(tournament: Tournament) -> dict:
    """Create a Video Judge Excel workbook for the tournament."""
    from services.video_judge_export import build_video_judge_rows, write_workbook

    path = _reserve_export_path(tournament.id, suffix='.xlsx', label='video_judge')
    sheets = build_video_judge_rows(tournament)
    write_workbook(sheets, path)
    return {
        'path': path,
        'download_name': safe_download_name(tournament, 'video_judge_sheets.xlsx'),
        'format': 'xlsx',
        'kind': 'video_judge_sheets',
        'sha256': _sha256_file(path),
    }


def build_video_judge_export_for_job(tournament_id: int) -> dict:
    """Background-job entry point for the Video Judge workbook."""
    tournament = db.session.get(Tournament, tournament_id)
    if not tournament:
        raise RuntimeError(f'Tournament {tournament_id} not found.')
    return build_video_judge_export(tournament)


def submit_video_judge_export_job(tournament_id: int) -> str:
    """Submit a tournament-bound background Video Judge workbook export."""
    return submit_job(
        f'export_video_judge_{tournament_id}',
        build_video_judge_export_for_job,
        tournament_id,
        metadata={'tournament_id': tournament_id, 'kind': 'video_judge_sheets'},
    )


def resolve_completed_export_path(tournament_id: int, job_id: str, job_getter) -> dict | None:
    """Return a validated export job snapshot or ``None`` for wrong tournament/missing jobs."""
    job = job_getter(job_id)
    job_meta = job.get('metadata') if job else {}
    if not job or int((job_meta or {}).get('tournament_id', -1)) != tournament_id:
        return None

    if job.get('status') != 'completed' or (job_meta or {}).get('kind') == 'build_pro_flights':
        return job

    def expired(message: str) -> dict:
        snapshot = dict(job)
        snapshot['status'] = 'expired'
        snapshot['error'] = message
        return snapshot

    artifact = job.get('result')
    if not isinstance(artifact, dict):
        return expired('Export artifact has no checksum metadata and cannot be verified.')

    path = artifact.get('path')
    expected_sha256 = artifact.get('sha256')
    if not path or not expected_sha256:
        return expired('Export artifact has no checksum metadata and cannot be verified.')
    if not os.path.isfile(path):
        return expired('Export artifact no longer exists on this application instance.')

    try:
        actual_sha256 = _sha256_file(path)
    except OSError:
        return expired('Export artifact is no longer readable on this application instance.')
    if not hmac.compare_digest(actual_sha256, str(expected_sha256).lower()):
        return expired('Export artifact checksum verification failed; the file changed.')
    return job
