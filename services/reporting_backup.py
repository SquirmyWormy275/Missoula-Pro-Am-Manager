"""Reporting backup workflow helpers."""
from __future__ import annotations

import os

from services.background_jobs import submit as submit_job
from services.backup import backup_database as run_backup_database


def sqlite_backup_download_plan(db_uri: str, instance_path: str) -> dict:
    """Return a validated SQLite backup download plan."""
    if not db_uri.startswith('sqlite:///'):
        return {
            'ok': False,
            'reason': 'unsupported',
            'message': 'Database backup download is only available for SQLite in this environment.',
        }

    db_path = db_uri.replace('sqlite:///', '', 1)
    if not os.path.isabs(db_path):
        db_path = os.path.join(instance_path, db_path)

    if not os.path.exists(db_path):
        return {
            'ok': False,
            'reason': 'missing',
            'message': 'Database file not found.',
        }

    return {'ok': True, 'path': db_path}


def run_database_backup(db_uri: str, tournament_id: int, instance_path: str) -> dict:
    """Background-job entry point for unified database backup."""
    return run_backup_database(db_uri, tournament_id, instance_path)


def submit_database_backup_job(db_uri: str, tournament_id: int, instance_path: str) -> str:
    """Submit a tournament-bound database backup job."""
    return submit_job(
        f'backup:t{tournament_id}',
        run_database_backup,
        db_uri,
        tournament_id,
        instance_path,
        metadata={'tournament_id': tournament_id, 'kind': 'backup'},
    )
