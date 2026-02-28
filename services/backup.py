"""
Database backup service.

Supports two backends:
  1. S3 (boto3) — configure via BACKUP_S3_BUCKET, AWS_ACCESS_KEY_ID, etc.
  2. Local filesystem fallback — always available; stores .db copy in a directory.

Usage:
    from services.backup import backup_to_s3, backup_to_local, is_s3_configured
"""
from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _timestamp() -> str:
    return datetime.utcnow().strftime('%Y%m%d_%H%M%S')


def _db_path_from_uri(uri: str, instance_path: str) -> str | None:
    """Extract filesystem path from SQLite URI."""
    if not uri.startswith('sqlite:///'):
        return None
    path = uri.replace('sqlite:///', '', 1)
    if not os.path.isabs(path):
        path = os.path.join(instance_path, path)
    return path if os.path.exists(path) else None


# ---------------------------------------------------------------------------
# S3 backend
# ---------------------------------------------------------------------------

def is_s3_configured() -> bool:
    """Return True if all required S3 env vars are present and boto3 is installed."""
    try:
        import boto3  # noqa: F401  type: ignore
    except ImportError:
        return False
    return bool(
        os.environ.get('BACKUP_S3_BUCKET', '').strip()
        and os.environ.get('AWS_ACCESS_KEY_ID', '').strip()
        and os.environ.get('AWS_SECRET_ACCESS_KEY', '').strip()
    )


def backup_to_s3(db_path: str, tournament_id: int) -> dict:
    """
    Upload *db_path* to S3.

    Returns a dict with keys: ok, bucket, key, size_bytes, error.
    """
    if not is_s3_configured():
        return {'ok': False, 'error': 'S3 not configured (missing boto3 or env vars)'}

    try:
        import boto3  # type: ignore
    except ImportError:
        return {'ok': False, 'error': 'boto3 package not installed'}

    bucket = os.environ.get('BACKUP_S3_BUCKET', '').strip()
    prefix = os.environ.get('BACKUP_S3_PREFIX', 'proam-backups').strip().rstrip('/')
    region = os.environ.get('AWS_DEFAULT_REGION', '').strip() or None

    key = f'{prefix}/tournament_{tournament_id}/proam_{_timestamp()}.db'

    try:
        session = boto3.Session(
            aws_access_key_id=os.environ.get('AWS_ACCESS_KEY_ID'),
            aws_secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY'),
            region_name=region,
        )
        s3 = session.client('s3')
        file_size = os.path.getsize(db_path)
        s3.upload_file(db_path, bucket, key)
        logger.info('DB backup uploaded to s3://%s/%s (%d bytes)', bucket, key, file_size)
        return {'ok': True, 'bucket': bucket, 'key': key, 'size_bytes': file_size, 'error': None}
    except Exception as exc:
        logger.error('S3 backup failed: %s', exc)
        return {'ok': False, 'error': str(exc)}


# ---------------------------------------------------------------------------
# Local filesystem fallback
# ---------------------------------------------------------------------------

def backup_to_local(db_path: str, dest_dir: str, tournament_id: int) -> dict:
    """
    Copy *db_path* to *dest_dir*.

    Returns a dict with keys: ok, dest, size_bytes, error.
    """
    try:
        os.makedirs(dest_dir, exist_ok=True)
        filename = f'proam_t{tournament_id}_{_timestamp()}.db'
        dest = os.path.join(dest_dir, filename)
        shutil.copy2(db_path, dest)
        size = os.path.getsize(dest)
        logger.info('Local DB backup saved to %s (%d bytes)', dest, size)
        return {'ok': True, 'dest': dest, 'size_bytes': size, 'error': None}
    except Exception as exc:
        logger.error('Local backup failed: %s', exc)
        return {'ok': False, 'error': str(exc)}
