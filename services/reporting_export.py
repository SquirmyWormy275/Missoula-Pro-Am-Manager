"""Reporting export workflow helpers.

Routes should handle HTTP concerns; this module owns export file creation,
download naming, JSON payload assembly, and async job submission details.
"""
from __future__ import annotations

import os
import tempfile

from database import db
from models import Tournament
from services.background_jobs import submit as submit_job
from services.excel_io import export_results_to_excel
from services.handicap_export import build_chopping_rows, export_chopping_results_to_excel


def safe_download_name(tournament: Tournament, suffix: str) -> str:
    """Return a stable attachment filename for a tournament export."""
    return f'{tournament.name}_{tournament.year}_{suffix}'.replace(' ', '_')


def _reserve_export_path(tournament_id: int, *, suffix: str = '.xlsx', label: str = '') -> str:
    prefix = f'proam_{tournament_id}_'
    if label:
        prefix = f'{prefix}{label}_'
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    os.close(fd)
    return path


def build_results_export(tournament: Tournament) -> dict:
    """Create a full results Excel export and return file metadata."""
    path = _reserve_export_path(tournament.id, suffix='.xlsx')
    export_results_to_excel(tournament, path)
    return {
        'path': path,
        'download_name': safe_download_name(tournament, 'results.xlsx'),
        'format': 'xlsx',
        'kind': 'all_results',
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


def build_results_export_for_job(tournament_id: int) -> str:
    """Background-job entry point for a full results export."""
    tournament = db.session.get(Tournament, tournament_id)
    if not tournament:
        raise RuntimeError(f'Tournament {tournament_id} not found.')
    return build_results_export(tournament)['path']


def submit_results_export_job(tournament_id: int) -> str:
    """Submit a tournament-bound background results export."""
    return submit_job(
        f'export_results_{tournament_id}',
        build_results_export_for_job,
        tournament_id,
        metadata={'tournament_id': tournament_id, 'kind': 'export_results'},
    )


def resolve_completed_export_path(tournament_id: int, job_id: str, job_getter) -> dict | None:
    """Return a validated export job snapshot or ``None`` for wrong tournament/missing jobs."""
    job = job_getter(job_id)
    job_meta = job.get('metadata') if job else {}
    if not job or int((job_meta or {}).get('tournament_id', -1)) != tournament_id:
        return None
    return job
