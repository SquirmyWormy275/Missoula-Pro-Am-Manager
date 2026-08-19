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

import hashlib
import logging
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from services.time_utils import utc_timestamp_for_filename

logger = logging.getLogger(__name__)


class SQLiteBackupValidationError(RuntimeError):
    """Raised when a completed SQLite snapshot is not recoverable."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _timestamp() -> str:
    return utc_timestamp_for_filename()


def _artifact_stamp() -> str:
    """Return a sortable, collision-resistant backup artifact stamp."""
    return f'{_timestamp()}_{uuid4().hex[:12]}'


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


def sha256_file(path: str) -> str:
    """Return the SHA-256 digest of *path* without loading it into memory."""
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _read_only_sqlite_connection(path: str) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise SQLiteBackupValidationError(f'SQLite database file not found: {resolved}')
    return sqlite3.connect(f'{resolved.as_uri()}?mode=ro', uri=True, timeout=30)


def _fsync_file(path: str) -> None:
    with open(path, 'r+b') as handle:
        os.fsync(handle.fileno())


def _replace_file_durably(source: str, destination: str) -> None:
    """Publish a file and durably commit the containing-directory entry."""
    if os.name == 'nt':
        import ctypes

        move_file_ex = ctypes.windll.kernel32.MoveFileExW
        move_file_ex.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        move_file_ex.restype = ctypes.c_int
        movefile_replace_existing = 0x1
        movefile_write_through = 0x8
        if not move_file_ex(
            str(source),
            str(destination),
            movefile_replace_existing | movefile_write_through,
        ):
            raise ctypes.WinError()
        return

    os.replace(source, destination)
    directory = os.path.dirname(os.path.abspath(destination)) or '.'
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
    directory_fd = os.open(directory, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def validate_sqlite_snapshot(path: str) -> dict:
    """Prove a SQLite snapshot is readable, integral, and FK-consistent."""
    try:
        conn = _read_only_sqlite_connection(path)
        try:
            integrity_rows = [row[0] for row in conn.execute('PRAGMA integrity_check')]
            if integrity_rows != ['ok']:
                raise SQLiteBackupValidationError(
                    'SQLite integrity check failed for completed backup.'
                )
            fk_row = conn.execute('PRAGMA foreign_key_check').fetchone()
            if fk_row is not None:
                raise SQLiteBackupValidationError(
                    'SQLite foreign key check failed for completed backup.'
                )
        finally:
            conn.close()
    except SQLiteBackupValidationError:
        raise
    except sqlite3.Error as exc:
        raise SQLiteBackupValidationError(
            f'Completed SQLite backup could not be opened: {exc}'
        ) from exc

    return {
        'integrity_check': 'ok',
        'foreign_key_violations': 0,
        'size_bytes': os.path.getsize(path),
        'sha256': sha256_file(path),
    }


def create_sqlite_snapshot(db_path: str, destination_path: str | None = None) -> dict:
    """Create and validate a consistent snapshot with SQLite's online API.

    The source remains online while ``Connection.backup`` takes a transactionally
    consistent image. The destination is fsynced and independently reopened for
    integrity and foreign-key checks before the caller can publish it.
    """
    source_path = str(Path(db_path).resolve())
    owns_destination = destination_path is None
    if destination_path is None:
        fd, destination_path = tempfile.mkstemp(
            prefix='proam_sqlite_snapshot_', suffix='.db'
        )
        os.close(fd)
    destination_path = str(Path(destination_path).resolve())

    source = None
    destination = None
    try:
        source = _read_only_sqlite_connection(source_path)
        destination = sqlite3.connect(destination_path, timeout=30)
        source.backup(destination, pages=256, sleep=0.05)
        destination.commit()
        destination.close()
        destination = None
        _fsync_file(destination_path)
        validation = validate_sqlite_snapshot(destination_path)
        return {'path': destination_path, **validation}
    except Exception:
        if owns_destination or destination_path != source_path:
            try:
                os.remove(destination_path)
            except OSError:
                pass
        raise
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()


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
    s3_key = f'{prefix}/tournament_{tournament_id}/proam_{_artifact_stamp()}.dump'

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
    """Run pg_dump into a private temp file, then publish it atomically."""
    temp_dest = None
    try:
        os.makedirs(dest_dir, exist_ok=True)
        filename = f'proam_t{tournament_id}_{_artifact_stamp()}.dump'
        dest = os.path.join(dest_dir, filename)
        fd, temp_dest = tempfile.mkstemp(
            prefix='.proam_pg_backup_', suffix='.tmp', dir=dest_dir,
        )
        os.close(fd)

        connection_args, env_overlay = _pg_dump_args_and_env(db_uri)
        env = os.environ.copy()
        env.update(env_overlay)
        result = subprocess.run(
            ['pg_dump', '--format=custom', f'--file={temp_dest}', *connection_args],
            capture_output=True, text=True, timeout=300, env=env,
        )
        if result.returncode != 0:
            logger.error('pg_dump failed: %s', result.stderr)
            return {'ok': False, 'error': f'pg_dump failed: {result.stderr[:500]}'}

        size = os.path.getsize(temp_dest)
        if size < 1:
            return {'ok': False, 'error': 'pg_dump produced an empty archive'}
        digest = sha256_file(temp_dest)
        _replace_file_durably(temp_dest, dest)
        temp_dest = None
        _fsync_file(dest)
        logger.info('PG backup saved to %s (%d bytes)', dest, size)
        return {
            'ok': True,
            'dest': dest,
            'size_bytes': size,
            'sha256': digest,
            'error': None,
        }

    except FileNotFoundError:
        return {'ok': False, 'error': 'pg_dump not found — is PostgreSQL client installed?'}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'pg_dump timed out after 5 minutes'}
    except Exception as exc:
        logger.error('PG local backup failed: %s', exc)
        return {'ok': False, 'error': str(exc)}
    finally:
        if temp_dest:
            try:
                os.remove(temp_dest)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Unified backup entry point
# ---------------------------------------------------------------------------

def backup_database(db_uri: str, tournament_id: int, instance_path: str = '') -> dict:
    """Detect DB type and run the appropriate backup.

    Tries S3 first, falls back to local. Works for both SQLite and PostgreSQL.
    """
    dest_dir = os.environ.get('LOCAL_BACKUP_DIR', 'instance/backups')
    if is_postgres(db_uri):
        if is_s3_configured():
            primary = backup_pg_to_s3(db_uri, tournament_id)
            if primary.get('ok'):
                return primary
            fallback = backup_pg_to_local(db_uri, dest_dir, tournament_id)
            fallback['fallback_from'] = 's3'
            fallback['primary_error'] = primary.get('error')
            return fallback
        return backup_pg_to_local(db_uri, dest_dir, tournament_id)

    db_path = _db_path_from_uri(db_uri, instance_path)
    if not db_path:
        return {'ok': False, 'error': 'SQLite database file not found'}
    if is_s3_configured():
        primary = backup_to_s3(db_path, tournament_id)
        if primary.get('ok'):
            return primary
        fallback = backup_to_local(db_path, dest_dir, tournament_id)
        fallback['fallback_from'] = 's3'
        fallback['primary_error'] = primary.get('error')
        return fallback
    return backup_to_local(db_path, dest_dir, tournament_id)


# ---------------------------------------------------------------------------
# Legacy SQLite-only functions (kept for backward compatibility)
# ---------------------------------------------------------------------------

def backup_to_s3(db_path: str, tournament_id: int) -> dict:
    """Create a validated SQLite snapshot and upload that snapshot to S3."""
    if not is_s3_configured():
        return {'ok': False, 'error': 'S3 not configured (missing boto3 or env vars)'}

    prefix = os.environ.get('BACKUP_S3_PREFIX', 'proam-backups').strip().rstrip('/')
    key = f'{prefix}/tournament_{tournament_id}/proam_{_artifact_stamp()}.db'
    snapshot_path = None
    try:
        snapshot = create_sqlite_snapshot(db_path)
        snapshot_path = snapshot['path']
        result = _upload_to_s3(snapshot_path, key)
        if result.get('ok'):
            result.update({
                'sha256': snapshot['sha256'],
                'integrity_check': snapshot['integrity_check'],
                'foreign_key_violations': snapshot['foreign_key_violations'],
            })
        return result
    except Exception as exc:
        logger.error('SQLite S3 backup failed: %s', exc)
        return {'ok': False, 'error': str(exc)}
    finally:
        if snapshot_path:
            try:
                os.remove(snapshot_path)
            except OSError:
                pass


def backup_to_local(db_path: str, dest_dir: str, tournament_id: int) -> dict:
    """Create a validated SQLite snapshot in *dest_dir*."""
    temp_dest = None
    try:
        os.makedirs(dest_dir, exist_ok=True)
        filename = f'proam_t{tournament_id}_{_artifact_stamp()}.db'
        dest = os.path.join(dest_dir, filename)
        fd, temp_dest = tempfile.mkstemp(
            prefix='.proam_backup_', suffix='.tmp', dir=dest_dir,
        )
        os.close(fd)
        snapshot = create_sqlite_snapshot(db_path, temp_dest)
        _replace_file_durably(temp_dest, dest)
        temp_dest = None
        _fsync_file(dest)
        logger.info('Local DB backup saved to %s (%d bytes)', dest, snapshot['size_bytes'])
        return {
            'ok': True,
            'dest': dest,
            'size_bytes': snapshot['size_bytes'],
            'sha256': snapshot['sha256'],
            'integrity_check': snapshot['integrity_check'],
            'foreign_key_violations': snapshot['foreign_key_violations'],
            'error': None,
        }
    except Exception as exc:
        logger.error('Local backup failed: %s', exc)
        return {'ok': False, 'error': str(exc)}
    finally:
        if temp_dest:
            try:
                os.remove(temp_dest)
            except OSError:
                pass
