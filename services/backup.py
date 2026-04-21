"""
Database backup service.

Supports three backends:
  1. PostgreSQL via pg_dump → S3 (production on Railway)
  2. SQLite file copy → S3
  3. Local filesystem fallback — always available; stores .db/.dump copy locally.

Usage:
    from services.backup import (
        backup_database, is_s3_configured, is_postgres,
        backup_to_s3, backup_to_local,  # legacy SQLite-only
    )
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from urllib.parse import urlparse

from services.time_utils import utc_timestamp_for_filename

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _timestamp() -> str:
    return utc_timestamp_for_filename()


def is_postgres(uri: str) -> bool:
    """Return True if the database URI points to PostgreSQL."""
    return uri.startswith('postgresql://') or uri.startswith('postgres://')


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


def _upload_to_s3(local_path: str, s3_key: str) -> dict:
    """Upload a local file to S3. Returns result dict."""
    try:
        import boto3  # type: ignore
    except ImportError:
        return {'ok': False, 'error': 'boto3 package not installed'}

    bucket = os.environ.get('BACKUP_S3_BUCKET', '').strip()
    region = os.environ.get('AWS_DEFAULT_REGION', '').strip() or None

    try:
        session = boto3.Session(
            aws_access_key_id=os.environ.get('AWS_ACCESS_KEY_ID'),
            aws_secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY'),
            region_name=region,
        )
        s3 = session.client('s3')
        file_size = os.path.getsize(local_path)
        s3.upload_file(local_path, bucket, s3_key)
        logger.info('Backup uploaded to s3://%s/%s (%d bytes)', bucket, s3_key, file_size)
        return {'ok': True, 'bucket': bucket, 'key': s3_key, 'size_bytes': file_size, 'error': None}
    except Exception as exc:
        logger.error('S3 upload failed: %s', exc)
        return {'ok': False, 'error': str(exc)}


# ---------------------------------------------------------------------------
# PostgreSQL backup via pg_dump
# ---------------------------------------------------------------------------

def _pg_dump_args_and_env(db_uri):
    """
    Split a postgres URL into pg_dump-safe args + env.

    Returns (args_prefix, env_overlay) where args_prefix is the list of
    connection flags (--host/--port/--username/--dbname) and env_overlay
    is a dict containing PGPASSWORD if a password was present. Callers
    should append --format=custom and --file=... to args_prefix and merge
    env_overlay into os.environ.copy() before invoking pg_dump.
    """
    from urllib.parse import unquote, urlparse
    parsed = urlparse(db_uri)
    host = parsed.hostname or 'localhost'
    port = str(parsed.port or 5432)
    user = unquote(parsed.username or '')
    password = unquote(parsed.password or '')
    dbname = (parsed.path or '').lstrip('/') or ''
    args = ['--host', host, '--port', port]
    if user:
        args.extend(['--username', user])
    if dbname:
        args.extend(['--dbname', dbname])
    env_overlay = {}
    if password:
        env_overlay['PGPASSWORD'] = password
    return args, env_overlay


def backup_pg_to_s3(db_uri: str, tournament_id: int) -> dict:
    """Run pg_dump and upload the custom-format dump to S3.

    Returns a dict with keys: ok, bucket, key, size_bytes, error.
    """
    if not is_s3_configured():
        return {'ok': False, 'error': 'S3 not configured (missing boto3 or env vars)'}

    prefix = os.environ.get('BACKUP_S3_PREFIX', 'proam-backups').strip().rstrip('/')
    s3_key = f'{prefix}/tournament_{tournament_id}/proam_{_timestamp()}.dump'

    dump_file = None
    try:
        # pg_dump uses DATABASE_URL directly via --dbname
        fd, dump_file = tempfile.mkstemp(suffix='.dump', prefix='proam_backup_')
        os.close(fd)

        connection_args, env_overlay = _pg_dump_args_and_env(db_uri)
        env = os.environ.copy()
        env.update(env_overlay)
        result = subprocess.run(
            ['pg_dump', '--format=custom', f'--file={dump_file}', *connection_args],
            capture_output=True, text=True, timeout=300, env=env,
        )
        if result.returncode != 0:
            logger.error('pg_dump failed: %s', result.stderr)
            return {'ok': False, 'error': f'pg_dump failed: {result.stderr[:500]}'}

        upload_result = _upload_to_s3(dump_file, s3_key)
        return upload_result

    except FileNotFoundError:
        return {'ok': False, 'error': 'pg_dump not found — is PostgreSQL client installed?'}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'pg_dump timed out after 5 minutes'}
    except Exception as exc:
        logger.error('PG backup failed: %s', exc)
        return {'ok': False, 'error': str(exc)}
    finally:
        if dump_file and os.path.exists(dump_file):
            os.unlink(dump_file)


def backup_pg_to_local(db_uri: str, dest_dir: str, tournament_id: int) -> dict:
    """Run pg_dump and save the dump locally."""
    try:
        os.makedirs(dest_dir, exist_ok=True)
        filename = f'proam_t{tournament_id}_{_timestamp()}.dump'
        dest = os.path.join(dest_dir, filename)

        connection_args, env_overlay = _pg_dump_args_and_env(db_uri)
        env = os.environ.copy()
        env.update(env_overlay)
        result = subprocess.run(
            ['pg_dump', '--format=custom', f'--file={dest}', *connection_args],
            capture_output=True, text=True, timeout=300, env=env,
        )
        if result.returncode != 0:
            logger.error('pg_dump failed: %s', result.stderr)
            return {'ok': False, 'error': f'pg_dump failed: {result.stderr[:500]}'}

        size = os.path.getsize(dest)
        logger.info('PG backup saved to %s (%d bytes)', dest, size)
        return {'ok': True, 'dest': dest, 'size_bytes': size, 'error': None}

    except FileNotFoundError:
        return {'ok': False, 'error': 'pg_dump not found — is PostgreSQL client installed?'}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'pg_dump timed out after 5 minutes'}
    except Exception as exc:
        logger.error('PG local backup failed: %s', exc)
        return {'ok': False, 'error': str(exc)}


# ---------------------------------------------------------------------------
# Unified backup entry point
# ---------------------------------------------------------------------------

def backup_database(db_uri: str, tournament_id: int, instance_path: str = '') -> dict:
    """Detect DB type and run the appropriate backup.

    Tries S3 first, falls back to local. Works for both SQLite and PostgreSQL.
    """
    if is_postgres(db_uri):
        if is_s3_configured():
            return backup_pg_to_s3(db_uri, tournament_id)
        else:
            dest_dir = os.environ.get('LOCAL_BACKUP_DIR', 'instance/backups')
            return backup_pg_to_local(db_uri, dest_dir, tournament_id)
    else:
        db_path = _db_path_from_uri(db_uri, instance_path)
        if not db_path:
            return {'ok': False, 'error': 'SQLite database file not found'}
        if is_s3_configured():
            return backup_to_s3(db_path, tournament_id)
        else:
            dest_dir = os.environ.get('LOCAL_BACKUP_DIR', 'instance/backups')
            return backup_to_local(db_path, dest_dir, tournament_id)


# ---------------------------------------------------------------------------
# Legacy SQLite-only functions (kept for backward compatibility)
# ---------------------------------------------------------------------------

def backup_to_s3(db_path: str, tournament_id: int) -> dict:
    """Upload SQLite *db_path* to S3."""
    if not is_s3_configured():
        return {'ok': False, 'error': 'S3 not configured (missing boto3 or env vars)'}

    prefix = os.environ.get('BACKUP_S3_PREFIX', 'proam-backups').strip().rstrip('/')
    key = f'{prefix}/tournament_{tournament_id}/proam_{_timestamp()}.db'
    return _upload_to_s3(db_path, key)


def backup_to_local(db_path: str, dest_dir: str, tournament_id: int) -> dict:
    """Copy SQLite *db_path* to *dest_dir*."""
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
