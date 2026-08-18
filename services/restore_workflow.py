"""Validated staging and offline-only SQLite restore workflow."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import weakref
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from services.backup import create_sqlite_snapshot, sha256_file
from services.upload_security import malware_scan

STAGING_DIRNAME = '.restore-staging'
PACKAGE_VERSION = 2
ACTIVE_JOB_STATUSES = ('queued', 'running')
SQLITE_SIDECAR_SUFFIXES = ('-wal', '-shm')
_PROCESS_FENCES: dict[str, dict[str, object]] = {}
_PROCESS_FENCES_LOCK = threading.Lock()


class RestoreValidationError(RuntimeError):
    """Raised when a restore artifact fails a recovery invariant."""


class MaintenanceFenceBusy(RuntimeError):
    """Raised when another process owns an incompatible database fence."""


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _read_only_connection(path: str) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise RestoreValidationError(f'SQLite database file not found: {resolved}')
    return sqlite3.connect(f'{resolved.as_uri()}?mode=ro', uri=True, timeout=30)


def _fsync_file(path: Path) -> None:
    with path.open('r+b') as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == 'nt':
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _chmod_if_supported(path: Path, mode: int) -> None:
    """Restrict restore material on filesystems that expose POSIX-like modes."""
    try:
        os.chmod(path, mode)
    except (NotImplementedError, OSError):
        if os.name != 'nt':
            raise


def _restrict_windows_acl(path: Path) -> None:
    """Set and verify a protected ACL containing only the process identity."""
    script_body = r'''
$ErrorActionPreference = 'Stop'
$target = [Environment]::GetEnvironmentVariable('PROAM_RESTORE_ACL_TARGET')
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$item = Get-Item -LiteralPath $target
if ($item.PSIsContainer) {
    $acl = [System.Security.AccessControl.DirectorySecurity]::new()
    $inheritance = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
} else {
    $acl = [System.Security.AccessControl.FileSecurity]::new()
    $inheritance = [System.Security.AccessControl.InheritanceFlags]::None
}
$acl.SetAccessRuleProtection($true, $false)
$rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
    $sid,
    [System.Security.AccessControl.FileSystemRights]::FullControl,
    $inheritance,
    [System.Security.AccessControl.PropagationFlags]::None,
    [System.Security.AccessControl.AccessControlType]::Allow
)
$acl.SetAccessRule($rule)
$item.SetAccessControl($acl)
$verified = $item.GetAccessControl(
    [System.Security.AccessControl.AccessControlSections]::Access
)
$rules = @($verified.GetAccessRules(
    $true,
    $false,
    [System.Security.Principal.SecurityIdentifier]
))
$full = [System.Security.AccessControl.FileSystemRights]::FullControl
if (-not $verified.AreAccessRulesProtected -or
    $rules.Count -ne 1 -or
    $rules[0].IdentityReference.Value -ne $sid.Value -or
    $rules[0].AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow -or
    (($rules[0].FileSystemRights -band $full) -ne $full)) {
    throw 'Restore ACL verification failed.'
}
'''.strip()
    script = f'& {{\n{script_body}\n}}'
    result = subprocess.run(
        [
            'powershell.exe',
            '-NoProfile',
            '-NonInteractive',
            '-ExecutionPolicy',
            'Bypass',
            '-Command',
            script,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={**os.environ, 'PROAM_RESTORE_ACL_TARGET': str(path)},
    )
    if result.returncode != 0:
        raise RestoreValidationError(
            'Could not establish and verify a private Windows ACL for restore material.'
        )


def _restrict_restore_path(path: Path, mode: int) -> None:
    _chmod_if_supported(path, mode)
    if os.name == 'nt':
        _restrict_windows_acl(path)


def _remove_incomplete_package(package_dir: Path, staging_root: Path) -> None:
    resolved_package = package_dir.resolve()
    resolved_root = staging_root.resolve()
    if resolved_package.parent != resolved_root:
        raise RuntimeError('Refusing to remove a restore package outside its staging root.')
    if resolved_package.exists():
        shutil.rmtree(resolved_package)
        _fsync_directory(resolved_root)


def _mkdir_private(path: Path, *, parents: bool = True, exist_ok: bool = True) -> None:
    path.mkdir(parents=parents, exist_ok=exist_ok)
    _chmod_if_supported(path, 0o700)


def _write_json_atomic(path: Path, payload: dict) -> None:
    _mkdir_private(path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
            json.dump(payload, handle, sort_keys=True, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        _chmod_if_supported(path, 0o600)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.remove(temp_name)
        except OSError:
            pass
        raise


def _append_journal(path: Path, event: str, **fields) -> None:
    entry = {'at': _utc_iso(), 'event': event, **fields}
    _mkdir_private(path.parent)
    with path.open('a', encoding='utf-8', newline='\n') as handle:
        _chmod_if_supported(path, 0o600)
        handle.write(json.dumps(entry, sort_keys=True, default=str))
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())


def _sqlite_health(path: str) -> dict:
    try:
        conn = _read_only_connection(path)
        try:
            integrity = [row[0] for row in conn.execute('PRAGMA integrity_check')]
            if integrity != ['ok']:
                raise RestoreValidationError('Restore file failed SQLite integrity check.')
            fk_violation = conn.execute('PRAGMA foreign_key_check').fetchone()
            if fk_violation is not None:
                raise RestoreValidationError('Restore file failed foreign key validation.')
        finally:
            conn.close()
    except RestoreValidationError:
        raise
    except sqlite3.Error as exc:
        raise RestoreValidationError(f'Restore file is not a readable SQLite database: {exc}') from exc
    return {'integrity_check': 'ok', 'foreign_key_violations': 0}


def _quote_sqlite_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _application_table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _application_table_structure(
    conn: sqlite3.Connection,
    table_names: set[str],
) -> dict[str, dict]:
    structure = {}
    for table_name in sorted(table_names):
        quoted_table = _quote_sqlite_identifier(table_name)
        table_definition_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        table_definition = (
            table_definition_row[0] if table_definition_row else None
        )
        try:
            table_columns = list(conn.execute(f'PRAGMA table_xinfo({quoted_table})'))
        except sqlite3.DatabaseError:
            table_columns = list(conn.execute(f'PRAGMA table_info({quoted_table})'))
        columns = [
            {
                'name': str(row[1]),
                'type': str(row[2] or '').strip().upper(),
                'not_null': bool(row[3]),
                'default': None if row[4] is None else str(row[4]),
                'primary_key_position': int(row[5]),
                'hidden': int(row[6]) if len(row) > 6 else 0,
            }
            for row in table_columns
        ]
        foreign_keys = sorted(
            (
                {
                    'id': int(row[0]),
                    'sequence': int(row[1]),
                    'table': str(row[2]),
                    'from': str(row[3]),
                    'to': None if row[4] is None else str(row[4]),
                    'on_update': str(row[5]).upper(),
                    'on_delete': str(row[6]).upper(),
                    'match': str(row[7]).upper(),
                }
                for row in conn.execute(f'PRAGMA foreign_key_list({quoted_table})')
            ),
            key=lambda item: (item['id'], item['sequence']),
        )
        indexes = []
        for row in conn.execute(f'PRAGMA index_list({quoted_table})'):
            index_name = str(row[1])
            quoted_index = _quote_sqlite_identifier(index_name)
            definition_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                (index_name,),
            ).fetchone()
            definition = definition_row[0] if definition_row else None
            index_columns = [
                {
                    'sequence': int(info[0]),
                    'column_id': int(info[1]),
                    'name': None if info[2] is None else str(info[2]),
                }
                for info in conn.execute(f'PRAGMA index_info({quoted_index})')
            ]
            indexes.append(
                {
                    'name': index_name,
                    'unique': bool(row[2]),
                    'origin': str(row[3]),
                    'partial': bool(row[4]),
                    'definition': (
                        None if definition is None else ' '.join(str(definition).split())
                    ),
                    'columns': index_columns,
                }
            )
        structure[table_name] = {
            'definition': (
                None
                if table_definition is None
                else ' '.join(str(table_definition).split())
            ),
            'columns': columns,
            'foreign_keys': foreign_keys,
            'indexes': sorted(indexes, key=lambda item: item['name']),
            'triggers': [
                {
                    'name': str(row[0]),
                    'definition': ' '.join(str(row[1]).split()),
                }
                for row in conn.execute(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type = 'trigger' AND tbl_name = ? ORDER BY name",
                    (table_name,),
                )
                if row[1] is not None
            ],
        }
    return structure


def _canonical_sha256(payload) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=True,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def sqlite_schema_info(path: str) -> dict:
    try:
        conn = _read_only_connection(path)
        try:
            tables = _application_table_names(conn)
            revision = None
            if 'alembic_version' in tables:
                row = conn.execute('SELECT version_num FROM alembic_version LIMIT 1').fetchone()
                revision = row[0] if row else None
            required_structure = _application_table_structure(conn, tables)
            return {
                'tables': tables,
                'revision': revision,
                'required_structure': required_structure,
                'required_structure_sha256': _canonical_sha256(required_structure),
            }
        finally:
            conn.close()
    except RestoreValidationError:
        raise
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise RestoreValidationError(f'Could not inspect SQLite schema: {exc}') from exc


def sqlite_db_path_from_uri(uri: str, instance_path: str) -> str:
    if not uri.startswith('sqlite:///'):
        raise RuntimeError('Database restore is only available for SQLite in this environment.')
    db_path = uri.replace('sqlite:///', '', 1)
    if not os.path.isabs(db_path):
        db_path = os.path.join(instance_path, db_path)
    return str(Path(db_path).resolve())


def _tournament_identity(path: str, tournament_id: int) -> tuple[int, str, int] | None:
    conn = _read_only_connection(path)
    try:
        row = conn.execute(
            'SELECT id, name, year FROM tournaments WHERE id = ?',
            (tournament_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return int(row[0]), str(row[1]), int(row[2])


def _database_identity(path: str) -> tuple[list[tuple[int, str, int]], str]:
    conn = _read_only_connection(path)
    try:
        identities = [
            (int(row[0]), str(row[1]), int(row[2]))
            for row in conn.execute(
                'SELECT id, name, year FROM tournaments ORDER BY id, name, year'
            )
        ]
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise RestoreValidationError(
            f'Could not establish restore database identity: {exc}'
        ) from exc
    finally:
        conn.close()
    return identities, _canonical_sha256(identities)


def sqlite_audit_chain_head(path: str) -> str | None:
    """Hash audit rows in order without exposing row data outside this process."""
    conn = _read_only_connection(path)
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if 'audit_logs' not in tables:
            return None
        columns = [row[1] for row in conn.execute('PRAGMA table_info(audit_logs)')]
        if not columns:
            return None
        quoted = ', '.join(f'"{column}"' for column in columns)
        ordering = 'id' if 'id' in columns else 'rowid'
        digest = hashlib.sha256()
        for row in conn.execute(f'SELECT {quoted} FROM audit_logs ORDER BY {ordering}'):
            digest.update(
                json.dumps(row, separators=(',', ':'), default=str).encode('utf-8')
            )
            digest.update(b'\n')
        return digest.hexdigest()
    finally:
        conn.close()


def validate_sqlite_restore_file(
    upload_path: str,
    current_db_path: str,
    *,
    tournament_id: int | None = None,
    expected_checksum: str | None = None,
    expected_schema_sha256: str | None = None,
    expected_database_identity_sha256: str | None = None,
) -> dict:
    current_info = sqlite_schema_info(current_db_path)
    uploaded_info = sqlite_schema_info(upload_path)
    health = _sqlite_health(upload_path)
    current_tables = set(current_info['tables'])
    uploaded_tables = set(uploaded_info['tables'])
    missing_tables = sorted(current_tables - uploaded_tables)
    unexpected_tables = sorted(uploaded_tables - current_tables)
    if missing_tables or unexpected_tables:
        details = []
        if missing_tables:
            details.append(f'missing: {", ".join(missing_tables)}')
        if unexpected_tables:
            details.append(f'unexpected: {", ".join(unexpected_tables)}')
        raise RestoreValidationError(
            'Restore file is missing required tables from the current application '
            f'tables or contains unexpected application tables ({"; ".join(details)}).'
        )
    if not uploaded_info.get('revision'):
        raise RestoreValidationError('Restore file is missing Alembic migration metadata.')
    if current_info.get('revision') and uploaded_info['revision'] != current_info['revision']:
        raise RestoreValidationError(
            'Restore file schema revision does not match the current application schema. '
            f"Expected {current_info['revision']}, got {uploaded_info['revision']}."
        )

    current_schema_sha = current_info['required_structure_sha256']
    uploaded_schema_sha = uploaded_info['required_structure_sha256']
    authoritative_schema_sha = expected_schema_sha256 or current_schema_sha
    if not hmac.compare_digest(uploaded_schema_sha, authoritative_schema_sha):
        raise RestoreValidationError(
            'Restore file required-table structure does not match the authoritative '
            'complete application-table structure.'
        )
    if expected_schema_sha256 and not hmac.compare_digest(
        current_schema_sha, expected_schema_sha256,
    ):
        raise RestoreValidationError(
            'Target database required-table structure no longer matches the staged '
            'complete application-table structure.'
        )

    source_checksum = sha256_file(upload_path)
    if expected_checksum and not hmac.compare_digest(source_checksum, expected_checksum):
        raise RestoreValidationError('Restore package checksum does not match its staged manifest.')

    identity_matches = tournament_id is None
    if tournament_id is not None:
        current_identity = _tournament_identity(current_db_path, tournament_id)
        uploaded_identity = _tournament_identity(upload_path, tournament_id)
        if current_identity is None:
            raise RestoreValidationError('Target database does not contain the selected tournament.')
        if uploaded_identity is None:
            raise RestoreValidationError('Restore file does not contain the selected tournament.')
        if uploaded_identity != current_identity:
            raise RestoreValidationError(
                'Restore file tournament identity does not match the selected tournament.'
            )
        identity_matches = True

    _, current_identity_sha = _database_identity(current_db_path)
    uploaded_identities, uploaded_identity_sha = _database_identity(upload_path)
    authoritative_identity_sha = (
        expected_database_identity_sha256 or current_identity_sha
    )
    if not hmac.compare_digest(uploaded_identity_sha, authoritative_identity_sha):
        raise RestoreValidationError(
            'Restore file database identity does not match the complete tournament '
            'set in the authoritative database.'
        )
    if expected_database_identity_sha256 and not hmac.compare_digest(
        current_identity_sha, expected_database_identity_sha256,
    ):
        raise RestoreValidationError(
            'Target database identity no longer matches the staged restore package.'
        )

    return {
        'target_path': str(Path(current_db_path).resolve()),
        'current_revision': current_info.get('revision'),
        'uploaded_revision': uploaded_info.get('revision'),
        'tables': uploaded_info['tables'],
        'integrity_check': health['integrity_check'],
        'foreign_key_violations': health['foreign_key_violations'],
        'tournament_identity_matches': identity_matches,
        'database_identity_matches': True,
        'database_identity_sha256': uploaded_identity_sha,
        'database_identity_tournament_count': len(uploaded_identities),
        'required_schema_matches': True,
        'required_schema_sha256': uploaded_schema_sha,
        'source_sha256': source_checksum,
        'audit_chain_head': sqlite_audit_chain_head(upload_path),
    }


def prepare_sqlite_restore(
    *,
    upload_path: str,
    db_uri: str,
    instance_path: str,
    tournament_id: int | None = None,
    malware_scan_enabled: bool = False,
    malware_scan_command: str = '',
) -> dict:
    """Scan and validate a restore upload without changing the live database."""
    target_path = sqlite_db_path_from_uri(db_uri, instance_path)
    malware_scan(
        upload_path,
        enabled=malware_scan_enabled,
        command_template=malware_scan_command,
    )
    return validate_sqlite_restore_file(
        upload_path,
        target_path,
        tournament_id=tournament_id,
    )


def stage_sqlite_restore(
    *,
    upload_path: str,
    db_uri: str,
    instance_path: str,
    tournament_id: int,
    actor: str,
    source_filename: str,
    malware_scan_enabled: bool = False,
    malware_scan_command: str = '',
) -> dict:
    """Validate an upload and create a same-volume offline restore package."""
    target_path = Path(sqlite_db_path_from_uri(db_uri, instance_path)).resolve()
    validation = prepare_sqlite_restore(
        upload_path=upload_path,
        db_uri=db_uri,
        instance_path=instance_path,
        tournament_id=tournament_id,
        malware_scan_enabled=malware_scan_enabled,
        malware_scan_command=malware_scan_command,
    )

    stage_id = uuid.uuid4().hex
    staging_root = target_path.parent / STAGING_DIRNAME
    _mkdir_private(staging_root)
    package_dir = staging_root / stage_id
    _mkdir_private(package_dir, parents=False, exist_ok=False)
    staged_db = package_dir / 'restore.db'
    journal = package_dir / 'restore-journal.jsonl'
    try:
        _restrict_restore_path(package_dir, 0o700)
        _append_journal(
            journal,
            'phase_started',
            phase='stage_copy',
            actor=actor,
            source_sha256=validation['source_sha256'],
            audit_chain_head=validation['audit_chain_head'],
        )
        shutil.copyfile(upload_path, staged_db)
        _restrict_restore_path(staged_db, 0o600)
        _fsync_file(staged_db)
        staged_validation = validate_sqlite_restore_file(
            str(staged_db),
            str(target_path),
            tournament_id=tournament_id,
            expected_checksum=validation['source_sha256'],
            expected_schema_sha256=validation['required_schema_sha256'],
            expected_database_identity_sha256=validation[
                'database_identity_sha256'
            ],
        )
        manifest = {
            'package_version': PACKAGE_VERSION,
            'stage_id': stage_id,
            'status': 'staged',
            'phase': 'staged',
            'staged_at': _utc_iso(),
            'actor': actor,
            'source_filename': os.path.basename(source_filename),
            'source_sha256': staged_validation['source_sha256'],
            'source_size_bytes': staged_db.stat().st_size,
            'source_audit_chain_head': staged_validation['audit_chain_head'],
            'target_path': str(target_path),
            'target_sha256_at_stage': sha256_file(str(target_path)),
            'tournament_id': tournament_id,
            'database_identity_sha256': staged_validation['database_identity_sha256'],
            'database_identity_tournament_count': staged_validation[
                'database_identity_tournament_count'
            ],
            'required_schema_sha256': staged_validation['required_schema_sha256'],
            'schema_revision': staged_validation['uploaded_revision'],
            'validation': {
                'integrity_check': staged_validation['integrity_check'],
                'foreign_key_violations': staged_validation['foreign_key_violations'],
                'tournament_identity_matches': True,
                'database_identity_matches': True,
                'required_schema_matches': True,
            },
        }
        _write_json_atomic(package_dir / 'manifest.json', manifest)
        _append_journal(
            journal,
            'phase_completed',
            phase='stage_copy',
            actor=actor,
            source_sha256=manifest['source_sha256'],
            audit_chain_head=manifest['source_audit_chain_head'],
            validation=manifest['validation'],
        )
        return {
            'ok': True,
            'stage_id': stage_id,
            'package_dir': str(package_dir),
            'manifest_path': str(package_dir / 'manifest.json'),
            'source_sha256': manifest['source_sha256'],
            'validation': manifest['validation'],
        }
    except BaseException as exc:
        try:
            _append_journal(
                journal,
                'phase_failed',
                phase='stage_copy',
                actor=actor,
                error=type(exc).__name__,
            )
        finally:
            _remove_incomplete_package(package_dir, staging_root)
        raise


class _FileFence:
    def __init__(self, db_path: str, *, exclusive: bool):
        self.path = Path(f'{Path(db_path).resolve()}.maintenance.lock')
        self.exclusive = exclusive
        self.handle = None
        self.acquired = False

    def acquire(self, timeout: float) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open('a+b')
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b'0')
            self.handle.flush()
            os.fsync(self.handle.fileno())
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                self.handle.seek(0)
                if os.name == 'nt':
                    import msvcrt

                    mode = msvcrt.LK_NBLCK if self.exclusive else msvcrt.LK_NBRLCK
                    msvcrt.locking(self.handle.fileno(), mode, 1)
                else:
                    import fcntl

                    mode = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
                    fcntl.flock(self.handle.fileno(), mode | fcntl.LOCK_NB)
                self.acquired = True
                return
            except OSError as exc:
                if time.monotonic() >= deadline:
                    self.close()
                    raise MaintenanceFenceBusy(str(self.path)) from exc
                time.sleep(0.05)

    def release(self) -> None:
        if not self.acquired or self.handle is None:
            return
        try:
            self.handle.seek(0)
            if os.name == 'nt':
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.acquired = False

    def close(self) -> None:
        try:
            self.release()
        finally:
            if self.handle is not None:
                self.handle.close()
                self.handle = None


@contextmanager
def database_activity_fence(db_path: str, *, exclusive: bool, timeout: float = 0):
    fence = _FileFence(db_path, exclusive=exclusive)
    fence.acquire(timeout)
    try:
        yield fence
    finally:
        fence.close()


def configure_sqlite_process_fence(app) -> None:
    """Hold a shared fence for the life of a SQLite web process."""
    uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    if not uri.startswith('sqlite:///'):
        return
    if app.extensions.get('sqlite_process_fence') is not None:
        return
    db_path = sqlite_db_path_from_uri(uri, app.instance_path)
    fence_key = str(Path(db_path).resolve())
    with _PROCESS_FENCES_LOCK:
        entry = _PROCESS_FENCES.get(fence_key)
        if entry is None:
            fence = _FileFence(db_path, exclusive=False)
            try:
                fence.acquire(0)
            except MaintenanceFenceBusy as exc:
                raise RuntimeError(
                    'SQLite offline maintenance is active; web startup is blocked.'
                ) from exc
            entry = {'fence': fence, 'references': 0}
            _PROCESS_FENCES[fence_key] = entry
        entry['references'] = int(entry['references']) + 1
        fence = entry['fence']
    app.extensions['sqlite_process_fence'] = fence
    app.extensions['sqlite_process_fence_finalizer'] = weakref.finalize(
        app,
        _release_sqlite_process_fence,
        fence_key,
    )


def _release_sqlite_process_fence(fence_key: str) -> None:
    with _PROCESS_FENCES_LOCK:
        entry = _PROCESS_FENCES.get(fence_key)
        if entry is None:
            return
        references = int(entry['references']) - 1
        if references > 0:
            entry['references'] = references
            return
        fence = entry['fence']
        del _PROCESS_FENCES[fence_key]
        fence.close()


def _load_manifest(package_dir: Path) -> dict:
    try:
        manifest = json.loads((package_dir / 'manifest.json').read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise RestoreValidationError('Restore package manifest is missing or invalid.') from exc
    if manifest.get('package_version') != PACKAGE_VERSION:
        raise RestoreValidationError('Restore package version is not supported.')
    return manifest


def _validate_package_paths(package_dir: Path, manifest: dict) -> tuple[Path, Path]:
    target_path = Path(manifest.get('target_path', '')).resolve()
    expected_root = (target_path.parent / STAGING_DIRNAME).resolve()
    if package_dir.parent.resolve() != expected_root:
        raise RestoreValidationError('Restore package is not on the target database volume.')
    staged_db = (package_dir / 'restore.db').resolve()
    if staged_db.parent != package_dir or not staged_db.is_file():
        raise RestoreValidationError('Restore package database is missing.')
    return target_path, staged_db


def _active_jobs(path: Path) -> int:
    conn = _read_only_connection(str(path))
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if 'background_jobs' not in tables:
            return 0
        placeholders = ','.join('?' for _ in ACTIVE_JOB_STATUSES)
        row = conn.execute(
            f'SELECT COUNT(*) FROM background_jobs WHERE status IN ({placeholders})',
            ACTIVE_JOB_STATUSES,
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _probe_exclusive_sqlite_access(path: Path) -> None:
    try:
        conn = sqlite3.connect(str(path), timeout=0, isolation_level=None)
        try:
            conn.execute('PRAGMA busy_timeout=0')
            conn.execute('BEGIN EXCLUSIVE')
            conn.execute('ROLLBACK')
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise RuntimeError('Could not obtain exclusive SQLite database access.') from exc


def _inject_failure(fail_at: str | None, checkpoint: str) -> None:
    if fail_at == checkpoint:
        raise RuntimeError(f'Injected restore failure at {checkpoint}')


def _phase(journal: Path, name: str, actor: str, fn, *, fail_at: str | None):
    _append_journal(journal, 'phase_started', phase=name, actor=actor)
    _inject_failure(fail_at, f'before_{name}')
    try:
        result = fn()
    except BaseException as exc:
        _append_journal(
            journal,
            'phase_failed',
            phase=name,
            actor=actor,
            error=type(exc).__name__,
        )
        raise
    _append_journal(journal, 'phase_completed', phase=name, actor=actor)
    _inject_failure(fail_at, f'after_{name}')
    return result


def _sidecar_quarantine_path(package_dir: Path, quarantine_name: str) -> Path:
    if not quarantine_name or Path(quarantine_name).name != quarantine_name:
        raise RestoreValidationError('SQLite sidecar quarantine name is unsafe.')
    quarantine_dir = (package_dir / 'sidecar-quarantine').resolve()
    quarantine_path = (quarantine_dir / quarantine_name).resolve()
    if quarantine_path.parent != quarantine_dir:
        raise RestoreValidationError('SQLite sidecar quarantine path is unsafe.')
    return quarantine_path


def _reconcile_pending_sidecar_quarantines(
    *,
    target_path: Path,
    package_dir: Path,
    manifest: dict,
    journal: Path,
    actor: str,
    fail_at: str | None,
) -> None:
    """Finish recorded sidecar move intents before creating another attempt."""
    quarantine_dir = package_dir / 'sidecar-quarantine'
    _mkdir_private(quarantine_dir)
    manifest_path = package_dir / 'manifest.json'

    for record in manifest.get('sidecar_quarantines', []):
        if record.get('status') == 'completed':
            continue
        attempt_id = str(record.get('attempt_id') or '')
        purpose = str(record.get('purpose') or 'unknown')
        for entry in record.get('entries', []):
            if entry.get('status') in {'absent', 'quarantined'}:
                continue
            suffix = str(entry.get('suffix') or '')
            if suffix not in SQLITE_SIDECAR_SUFFIXES:
                raise RestoreValidationError('SQLite sidecar suffix is invalid.')
            source_path = Path(f'{target_path}{suffix}')
            quarantine_path = _sidecar_quarantine_path(
                package_dir, str(entry.get('quarantine_name') or '')
            )
            if source_path.is_symlink() or (
                source_path.exists() and not source_path.is_file()
            ):
                raise RestoreValidationError(
                    f'SQLite sidecar has an unsafe file type: {source_path.name}'
                )
            if quarantine_path.is_symlink() or (
                quarantine_path.exists() and not quarantine_path.is_file()
            ):
                raise RestoreValidationError(
                    f'SQLite sidecar quarantine has an unsafe file type: '
                    f'{quarantine_path.name}'
                )
            if source_path.is_file() and quarantine_path.is_file():
                raise RestoreValidationError(
                    f'SQLite sidecar exists in both live and quarantine locations: '
                    f'{source_path.name}'
                )

            expected_sha = entry.get('source_sha256')
            reconciled_move = False
            if quarantine_path.is_file():
                if not expected_sha or not hmac.compare_digest(
                    sha256_file(str(quarantine_path)), str(expected_sha),
                ):
                    raise RestoreValidationError(
                        f'Quarantined SQLite sidecar checksum is invalid: '
                        f'{quarantine_path.name}'
                    )
                _chmod_if_supported(quarantine_path, 0o600)
                entry['status'] = 'quarantined'
                reconciled_move = True
            elif source_path.is_file():
                source_sha = sha256_file(str(source_path))
                if expected_sha and not hmac.compare_digest(
                    source_sha, str(expected_sha),
                ):
                    raise RestoreValidationError(
                        f'SQLite sidecar changed after quarantine intent: '
                        f'{source_path.name}'
                    )
                entry['source_sha256'] = source_sha
                entry['status'] = 'move_pending'
                _write_json_atomic(manifest_path, manifest)
                _append_journal(
                    journal,
                    'sidecar_move_intent',
                    actor=actor,
                    purpose=purpose,
                    attempt_id=attempt_id,
                    suffix=suffix,
                    source_sha256=source_sha,
                    quarantine_name=quarantine_path.name,
                )
                os.replace(source_path, quarantine_path)
                _chmod_if_supported(quarantine_path, 0o600)
                _fsync_file(quarantine_path)
                _fsync_directory(source_path.parent)
                _fsync_directory(quarantine_dir)
                _inject_failure(
                    fail_at,
                    f'after_sidecar_move_before_manifest_{suffix.removeprefix("-")}',
                )
                entry['status'] = 'quarantined'
            elif expected_sha:
                raise RestoreValidationError(
                    f'Recorded SQLite sidecar is missing from live and quarantine '
                    f'locations: {source_path.name}'
                )
            else:
                entry['status'] = 'absent'

            _write_json_atomic(manifest_path, manifest)
            event = (
                'sidecar_quarantine_reconciled'
                if reconciled_move
                else 'sidecar_quarantined'
            )
            _append_journal(
                journal,
                event,
                actor=actor,
                purpose=purpose,
                attempt_id=attempt_id,
                suffix=suffix,
                status=entry['status'],
                source_sha256=entry.get('source_sha256'),
            )
            _inject_failure(fail_at, f'after_quarantine_{suffix.removeprefix("-")}')

        record['status'] = 'completed'
        manifest['phase'] = f'{purpose}_sidecars_quarantined'
        _write_json_atomic(manifest_path, manifest)
        _append_journal(
            journal,
            'sidecar_quarantine_completed',
            actor=actor,
            purpose=purpose,
            attempt_id=attempt_id,
        )


def _quarantine_sqlite_sidecars(
    *,
    target_path: Path,
    package_dir: Path,
    manifest: dict,
    journal: Path,
    actor: str,
    purpose: str,
    fail_at: str | None,
) -> dict:
    """Move target WAL/SHM files away so they cannot attach to a new main file."""
    _reconcile_pending_sidecar_quarantines(
        target_path=target_path,
        package_dir=package_dir,
        manifest=manifest,
        journal=journal,
        actor=actor,
        fail_at=fail_at,
    )
    attempt_id = uuid.uuid4().hex
    quarantine_dir = package_dir / 'sidecar-quarantine'
    _mkdir_private(quarantine_dir)
    record = {
        'attempt_id': attempt_id,
        'purpose': purpose,
        'status': 'pending',
        'entries': [],
    }
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        record['entries'].append(
            {
                'suffix': suffix,
                'quarantine_name': f'{attempt_id}{suffix}',
                'status': 'pending',
            }
        )
    manifest.setdefault('sidecar_quarantines', []).append(record)
    manifest['phase'] = f'{purpose}_sidecar_quarantine_pending'
    _write_json_atomic(package_dir / 'manifest.json', manifest)
    _append_journal(
        journal,
        'sidecar_quarantine_started',
        actor=actor,
        purpose=purpose,
        attempt_id=attempt_id,
    )
    _reconcile_pending_sidecar_quarantines(
        target_path=target_path,
        package_dir=package_dir,
        manifest=manifest,
        journal=journal,
        actor=actor,
        fail_at=fail_at,
    )
    return record


def _restore_safety_snapshot(
    *,
    package_dir: Path,
    manifest: dict,
    journal: Path,
    actor: str,
    safety_path: Path,
    target_path: Path,
    tournament_id: int,
    expected_checksum: str,
) -> None:
    if not safety_path.is_file() or not hmac.compare_digest(
        sha256_file(str(safety_path)), expected_checksum,
    ):
        raise RuntimeError('Safety snapshot is missing or has changed; rollback is blocked.')
    rollback_copy = safety_path.with_name(f'.rollback-{uuid.uuid4().hex}.db')
    try:
        shutil.copyfile(safety_path, rollback_copy)
        _chmod_if_supported(rollback_copy, 0o600)
        _fsync_file(rollback_copy)
        _quarantine_sqlite_sidecars(
            target_path=target_path,
            package_dir=package_dir,
            manifest=manifest,
            journal=journal,
            actor=actor,
            purpose='rollback',
            fail_at=None,
        )
        os.replace(rollback_copy, target_path)
        _fsync_file(target_path)
        _fsync_directory(target_path.parent)
        validate_sqlite_restore_file(
            str(target_path),
            str(target_path),
            tournament_id=tournament_id,
            expected_checksum=expected_checksum,
            expected_schema_sha256=manifest['required_schema_sha256'],
            expected_database_identity_sha256=manifest['database_identity_sha256'],
        )
    finally:
        try:
            rollback_copy.unlink()
        except OSError:
            pass


def _recover_interrupted_attempt(
    *,
    package_dir: Path,
    manifest: dict,
    target_path: Path,
    journal: Path,
    actor: str,
) -> dict | None:
    if manifest.get('status') == 'completed':
        validation = validate_sqlite_restore_file(
            str(target_path),
            str(target_path),
            tournament_id=int(manifest['tournament_id']),
            expected_checksum=manifest['source_sha256'],
            expected_schema_sha256=manifest['required_schema_sha256'],
            expected_database_identity_sha256=manifest['database_identity_sha256'],
        )
        return {
            'ok': True,
            'status': 'completed',
            'stage_id': manifest['stage_id'],
            'target_path': str(target_path),
            'source_sha256': validation['source_sha256'],
            'recovered': False,
        }
    if manifest.get('status') != 'applying':
        return None

    _quarantine_sqlite_sidecars(
        target_path=target_path,
        package_dir=package_dir,
        manifest=manifest,
        journal=journal,
        actor=actor,
        purpose='recovery',
        fail_at=None,
    )

    source_sha = manifest['source_sha256']
    if target_path.is_file() and hmac.compare_digest(sha256_file(str(target_path)), source_sha):
        validation = validate_sqlite_restore_file(
            str(target_path),
            str(target_path),
            tournament_id=int(manifest['tournament_id']),
            expected_checksum=source_sha,
            expected_schema_sha256=manifest['required_schema_sha256'],
            expected_database_identity_sha256=manifest['database_identity_sha256'],
        )
        manifest.update(status='completed', phase='recovered_validation', completed_at=_utc_iso())
        _write_json_atomic(package_dir / 'manifest.json', manifest)
        _append_journal(
            journal,
            'restore_recovered',
            actor=actor,
            source_sha256=source_sha,
            audit_chain_head=validation['audit_chain_head'],
        )
        return {
            'ok': True,
            'status': 'completed',
            'stage_id': manifest['stage_id'],
            'target_path': str(target_path),
            'source_sha256': source_sha,
            'recovered': True,
        }

    safety_path = package_dir / 'safety.db'
    safety_sha = manifest.get('safety_sha256')
    if safety_sha:
        _append_journal(journal, 'rollback_started', actor=actor, reason='interrupted_apply')
        _restore_safety_snapshot(
            package_dir=package_dir,
            manifest=manifest,
            journal=journal,
            actor=actor,
            safety_path=safety_path,
            target_path=target_path,
            tournament_id=int(manifest['tournament_id']),
            expected_checksum=safety_sha,
        )
        _append_journal(
            journal,
            'rollback_completed',
            actor=actor,
            safety_sha256=safety_sha,
        )
        manifest.update(status='staged', phase='recovered_rollback')
        _write_json_atomic(package_dir / 'manifest.json', manifest)
        return None
    raise RuntimeError('Interrupted restore has no verifiable safety snapshot.')


def apply_staged_sqlite_restore(
    package_path: str,
    *,
    actor: str,
    fence_timeout: float = 0,
    fail_at: str | None = None,
) -> dict:
    """Apply a staged package while the web process is provably offline."""
    package_dir = Path(package_path).resolve()
    manifest = _load_manifest(package_dir)
    target_path, staged_db = _validate_package_paths(package_dir, manifest)
    journal = package_dir / 'restore-journal.jsonl'
    tournament_id = int(manifest['tournament_id'])

    try:
        with database_activity_fence(
            str(target_path), exclusive=True, timeout=fence_timeout,
        ):
            recovered = _recover_interrupted_attempt(
                package_dir=package_dir,
                manifest=manifest,
                target_path=target_path,
                journal=journal,
                actor=actor,
            )
            if recovered is not None:
                return recovered

            if _active_jobs(target_path):
                raise RuntimeError(
                    'Queued or running background jobs must be drained before restore.'
                )

            validation = _phase(
                journal,
                'pre_validation',
                actor,
                lambda: validate_sqlite_restore_file(
                    str(staged_db),
                    str(target_path),
                    tournament_id=tournament_id,
                    expected_checksum=manifest['source_sha256'],
                    expected_schema_sha256=manifest['required_schema_sha256'],
                    expected_database_identity_sha256=manifest[
                        'database_identity_sha256'
                    ],
                ),
                fail_at=fail_at,
            )
            _phase(
                journal,
                'exclusive_access',
                actor,
                lambda: _probe_exclusive_sqlite_access(target_path),
                fail_at=fail_at,
            )

            safety_path = package_dir / 'safety.db'
            if safety_path.exists():
                safety_path.unlink()
            try:
                safety = _phase(
                    journal,
                    'safety_snapshot',
                    actor,
                    lambda: create_sqlite_snapshot(str(target_path), str(safety_path)),
                    fail_at=fail_at,
                )
                _chmod_if_supported(safety_path, 0o600)
            except BaseException as exc:
                safety_sha = sha256_file(str(safety_path)) if safety_path.is_file() else None
                _append_journal(
                    journal,
                    'restore_failed',
                    actor=actor,
                    phase='safety_snapshot',
                    error=type(exc).__name__,
                )
                _append_journal(
                    journal,
                    'rollback_not_required',
                    actor=actor,
                    safety_sha256=safety_sha,
                )
                manifest.update(
                    status='failed',
                    phase='safety_snapshot',
                    failed_at=_utc_iso(),
                    safety_sha256=safety_sha,
                )
                _write_json_atomic(package_dir / 'manifest.json', manifest)
                raise
            manifest.update(
                status='applying',
                phase='safety_ready',
                maintenance_actor=actor,
                apply_started_at=_utc_iso(),
                safety_sha256=safety['sha256'],
                safety_audit_chain_head=sqlite_audit_chain_head(str(safety_path)),
            )
            _write_json_atomic(package_dir / 'manifest.json', manifest)

            replacement_copy = package_dir / f'.replacement-{uuid.uuid4().hex}.db'
            replaced = False
            target_mutated = False
            try:
                def _replace():
                    nonlocal replaced, target_mutated
                    shutil.copyfile(staged_db, replacement_copy)
                    _chmod_if_supported(replacement_copy, 0o600)
                    _fsync_file(replacement_copy)
                    manifest.update(phase='replacement_pending')
                    _write_json_atomic(package_dir / 'manifest.json', manifest)
                    target_mutated = True
                    _phase(
                        journal,
                        'sidecar_quarantine',
                        actor,
                        lambda: _quarantine_sqlite_sidecars(
                            target_path=target_path,
                            package_dir=package_dir,
                            manifest=manifest,
                            journal=journal,
                            actor=actor,
                            purpose='replace',
                            fail_at=fail_at,
                        ),
                        fail_at=fail_at,
                    )
                    os.replace(replacement_copy, target_path)
                    replaced = True
                    _fsync_file(target_path)
                    _fsync_directory(target_path.parent)
                    manifest.update(phase='replaced')
                    _write_json_atomic(package_dir / 'manifest.json', manifest)

                _phase(
                    journal,
                    'replace',
                    actor,
                    _replace,
                    fail_at=fail_at,
                )
                _phase(
                    journal,
                    'reopen',
                    actor,
                    lambda: sqlite_schema_info(str(target_path)),
                    fail_at=fail_at,
                )
                post_validation = _phase(
                    journal,
                    'post_validation',
                    actor,
                    lambda: validate_sqlite_restore_file(
                        str(target_path),
                        str(target_path),
                        tournament_id=tournament_id,
                        expected_checksum=manifest['source_sha256'],
                        expected_schema_sha256=manifest['required_schema_sha256'],
                        expected_database_identity_sha256=manifest[
                            'database_identity_sha256'
                        ],
                    ),
                    fail_at=fail_at,
                )
            except BaseException as exc:
                _append_journal(
                    journal,
                    'restore_failed',
                    actor=actor,
                    phase=manifest.get('phase'),
                    error=type(exc).__name__,
                )
                if replaced or target_mutated:
                    _append_journal(
                        journal,
                        'rollback_started',
                        actor=actor,
                        source_sha256=manifest['source_sha256'],
                        safety_sha256=safety['sha256'],
                    )
                    _restore_safety_snapshot(
                        package_dir=package_dir,
                        manifest=manifest,
                        journal=journal,
                        actor=actor,
                        safety_path=safety_path,
                        target_path=target_path,
                        tournament_id=tournament_id,
                        expected_checksum=safety['sha256'],
                    )
                    _append_journal(
                        journal,
                        'rollback_completed',
                        actor=actor,
                        safety_sha256=safety['sha256'],
                    )
                    manifest.update(status='rolled_back', phase='rollback_completed')
                else:
                    _append_journal(
                        journal,
                        'rollback_not_required',
                        actor=actor,
                        safety_sha256=safety['sha256'],
                    )
                    manifest.update(status='failed', phase='before_replacement')
                manifest['failed_at'] = _utc_iso()
                _write_json_atomic(package_dir / 'manifest.json', manifest)
                raise
            finally:
                try:
                    replacement_copy.unlink()
                except OSError:
                    pass

            manifest.update(status='completed', phase='completed', completed_at=_utc_iso())
            _write_json_atomic(package_dir / 'manifest.json', manifest)
            _append_journal(
                journal,
                'restore_completed',
                actor=actor,
                source_sha256=manifest['source_sha256'],
                safety_sha256=safety['sha256'],
                audit_chain_head=post_validation['audit_chain_head'],
                validation={
                    'integrity_check': post_validation['integrity_check'],
                    'foreign_key_violations': post_validation['foreign_key_violations'],
                    'tournament_identity_matches': True,
                },
            )
            return {
                'ok': True,
                'status': 'completed',
                'stage_id': manifest['stage_id'],
                'target_path': str(target_path),
                'source_sha256': manifest['source_sha256'],
                'safety_sha256': safety['sha256'],
                'validation': validation,
                'recovered': False,
            }
    except MaintenanceFenceBusy as exc:
        raise RuntimeError(
            'The application is still running. Stop it before applying a restore.'
        ) from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Missoula Pro-Am offline maintenance')
    subparsers = parser.add_subparsers(dest='command', required=True)
    apply_parser = subparsers.add_parser('apply', help='apply a staged SQLite restore')
    apply_parser.add_argument('package_path')
    apply_parser.add_argument('--actor', required=True)
    apply_parser.add_argument('--wait-seconds', type=float, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = apply_staged_sqlite_restore(
            args.package_path,
            actor=args.actor,
            fence_timeout=max(0.0, args.wait_seconds),
        )
    except Exception as exc:
        print(f'Restore failed: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
