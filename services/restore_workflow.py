from __future__ import annotations

import os
import sqlite3

from services.upload_security import malware_scan

REQUIRED_SQLITE_TABLES = {'tournaments', 'events', 'event_results', 'heats', 'users'}


def sqlite_schema_info(path: str) -> dict:
    conn = sqlite3.connect(path)
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        revision = None
        if 'alembic_version' in tables:
            row = conn.execute('SELECT version_num FROM alembic_version LIMIT 1').fetchone()
            revision = row[0] if row else None
        return {'tables': tables, 'revision': revision}
    finally:
        conn.close()


def sqlite_db_path_from_uri(uri: str, instance_path: str) -> str:
    if not uri.startswith('sqlite:///'):
        raise RuntimeError('Database restore is only available for SQLite in this environment.')
    db_path = uri.replace('sqlite:///', '', 1)
    if not os.path.isabs(db_path):
        db_path = os.path.join(instance_path, db_path)
    return db_path


def validate_sqlite_restore_file(upload_path: str, current_db_path: str) -> dict:
    current_info = sqlite_schema_info(current_db_path)
    uploaded_info = sqlite_schema_info(upload_path)
    missing_tables = sorted(REQUIRED_SQLITE_TABLES - set(uploaded_info['tables']))
    if missing_tables:
        raise RuntimeError(
            f'Restore file is missing required tables: {", ".join(missing_tables)}'
        )
    if not uploaded_info.get('revision'):
        raise RuntimeError('Restore file is missing Alembic migration metadata.')
    if current_info.get('revision') and uploaded_info['revision'] != current_info['revision']:
        raise RuntimeError(
            'Restore file schema revision does not match the current application schema. '
            f"Expected {current_info['revision']}, got {uploaded_info['revision']}."
        )
    return {
        'target_path': current_db_path,
        'current_revision': current_info.get('revision'),
        'uploaded_revision': uploaded_info.get('revision'),
        'tables': uploaded_info['tables'],
    }


def prepare_sqlite_restore(
    *,
    upload_path: str,
    db_uri: str,
    instance_path: str,
    malware_scan_enabled: bool = False,
    malware_scan_command: str = '',
) -> dict:
    target_path = sqlite_db_path_from_uri(db_uri, instance_path)
    malware_scan(
        upload_path,
        enabled=malware_scan_enabled,
        command_template=malware_scan_command,
    )
    return validate_sqlite_restore_file(upload_path, target_path)
