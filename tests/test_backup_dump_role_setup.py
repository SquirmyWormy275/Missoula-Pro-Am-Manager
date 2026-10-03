import secrets
import uuid

import psycopg2
import pytest
from psycopg2 import sql
from sqlalchemy.engine import make_url

from scripts import configure_backup_dump_role as setup


def test_dump_connection_url_preserves_public_endpoint_and_escapes_credentials():
    url = setup.connection_url('postgres://admin:old@db.invalid:1234/app?sslmode=require',
                               'proam_backup_dump_test', 'abc:/@' * 10)
    parsed = make_url(url)
    assert parsed.username == 'proam_backup_dump_test'
    assert parsed.password == 'abc:/@' * 10
    assert parsed.host == 'db.invalid'
    assert parsed.port == 1234
    assert parsed.database == 'app'
    assert parsed.query['sslmode'] == 'require'


@pytest.mark.parametrize('role', ['postgres', 'proam_backup_dump_bad;DROP ROLE postgres', ''])
def test_setup_rejects_unscoped_or_injected_role_names(role):
    with pytest.raises(ValueError):
        setup.connection_url('postgresql://admin:pw@db.invalid/app', role, 'a' * 50)


def test_setup_suppresses_sensitive_database_errors(tmp_path, monkeypatch, capsys):
    import hashlib
    recipient = 'age1test'
    values = {'BACKUP_AGE_RECIPIENT': recipient,
              'BACKUP_AGE_RECIPIENT_SHA256': hashlib.sha256(recipient.encode()).hexdigest(),
              'BACKUP_DUMP_ROLE_NAME': 'proam_backup_dump_test',
              'BACKUP_ROLE_BOOTSTRAP_PASSWORD': 'DO_NOT_DISCLOSE' * 4,
              'RAILWAY_PG_PUBLIC_URL': 'postgresql://admin:DO_NOT_DISCLOSE@db.invalid/app'}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr('sys.argv', ['setup', '--output', str(tmp_path / 'credential.age')])
    monkeypatch.setattr(setup.subprocess, 'run', lambda *args, **kwargs: None)

    def fail(*args):
        raise psycopg2.OperationalError('DO_NOT_DISCLOSE private diagnostic')

    monkeypatch.setattr(setup, 'create_dump_role', fail)
    assert setup.main() == 1
    output = capsys.readouterr()
    assert 'role provisioning' in output.err
    assert 'DO_NOT_DISCLOSE' not in output.err
    assert 'private diagnostic' not in output.err


def test_postgres_dump_role_cannot_write_even_when_read_only_default_is_disabled(app):
    admin_url = app.config['SQLALCHEMY_DATABASE_URI']
    if not admin_url.startswith('postgresql://'):
        pytest.skip('Requires isolated unit-postgres service')
    parsed = make_url(admin_url)
    assert parsed.host in {'localhost', '127.0.0.1'}
    role = 'proam_backup_dump_test_' + uuid.uuid4().hex[:16]
    password = secrets.token_urlsafe(48)
    admin = psycopg2.connect(admin_url)
    admin.autocommit = True
    created = False
    try:
        assert setup.create_dump_role(admin_url, role, password)['read_only_privileges_verified']
        created = True
        readonly_url = setup.connection_url(admin_url, role, password)
        reader = psycopg2.connect(readonly_url)
        try:
            reader.autocommit = True
            with reader.cursor() as cursor:
                cursor.execute('SHOW default_transaction_read_only')
                assert cursor.fetchone() == ('on',)
                cursor.execute('SELECT count(*) FROM tournaments')
                assert cursor.fetchone()[0] >= 0
                with pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
                    cursor.execute("UPDATE tournaments SET name = 'must not change'")
                cursor.execute('SET default_transaction_read_only = off')
                with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                    cursor.execute("UPDATE tournaments SET name = 'must not change'")
                with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                    cursor.execute('CREATE TABLE public.must_not_create (id integer)')
        finally:
            reader.close()
        with pytest.raises(RuntimeError, match='Existing roles'):
            setup.create_dump_role(admin_url, role, password)
    finally:
        if created:
            with admin.cursor() as cursor:
                cursor.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
                cursor.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
        admin.close()
