from __future__ import annotations

import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest

from services.restore_workflow import (
    prepare_sqlite_restore,
    sqlite_db_path_from_uri,
    validate_sqlite_restore_file,
)


@pytest.fixture
def workspace_tmpdir() -> Path:
    path = Path.cwd() / 'instance' / f'restore-workflow-{uuid.uuid4().hex}'
    path.mkdir(parents=True, exist_ok=False)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _make_sqlite_db(path, *, revision='rev1', tables=None):
    tables = tables or {'tournaments', 'events', 'event_results', 'heats', 'users'}
    conn = sqlite3.connect(path)
    try:
        conn.execute('CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)')
        conn.execute('INSERT INTO alembic_version (version_num) VALUES (?)', (revision,))
        for table in tables:
            conn.execute(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)')
        conn.commit()
    finally:
        conn.close()


def test_validate_sqlite_restore_file_accepts_matching_schema(workspace_tmpdir):
    current = workspace_tmpdir / 'current.db'
    upload = workspace_tmpdir / 'upload.db'
    _make_sqlite_db(current, revision='head')
    _make_sqlite_db(upload, revision='head')

    result = validate_sqlite_restore_file(str(upload), str(current))

    assert result['target_path'] == str(current)
    assert result['current_revision'] == 'head'
    assert result['uploaded_revision'] == 'head'


def test_validate_sqlite_restore_file_rejects_missing_tables(workspace_tmpdir):
    current = workspace_tmpdir / 'current.db'
    upload = workspace_tmpdir / 'upload.db'
    _make_sqlite_db(current, revision='head')
    _make_sqlite_db(upload, revision='head', tables={'tournaments'})

    with pytest.raises(RuntimeError, match='missing required tables'):
        validate_sqlite_restore_file(str(upload), str(current))


def test_validate_sqlite_restore_file_rejects_revision_mismatch(workspace_tmpdir):
    current = workspace_tmpdir / 'current.db'
    upload = workspace_tmpdir / 'upload.db'
    _make_sqlite_db(current, revision='head')
    _make_sqlite_db(upload, revision='old')

    with pytest.raises(RuntimeError, match='schema revision does not match'):
        validate_sqlite_restore_file(str(upload), str(current))


def test_sqlite_db_path_from_uri_rejects_non_sqlite(workspace_tmpdir):
    with pytest.raises(RuntimeError, match='only available for SQLite'):
        sqlite_db_path_from_uri('postgresql://example/db', str(workspace_tmpdir))


def test_prepare_sqlite_restore_runs_scan_and_returns_plan(workspace_tmpdir, monkeypatch):
    current = workspace_tmpdir / 'current.db'
    upload = workspace_tmpdir / 'upload.db'
    _make_sqlite_db(current, revision='head')
    _make_sqlite_db(upload, revision='head')
    calls = []

    def _scan(path, *, enabled, command_template):
        calls.append((path, enabled, command_template))

    monkeypatch.setattr('services.restore_workflow.malware_scan', _scan)

    result = prepare_sqlite_restore(
        upload_path=str(upload),
        db_uri='sqlite:///current.db',
        instance_path=str(workspace_tmpdir),
        malware_scan_enabled=True,
        malware_scan_command='scan {path}',
    )

    assert result['target_path'] == str(current)
    assert calls == [(str(upload), True, 'scan {path}')]
