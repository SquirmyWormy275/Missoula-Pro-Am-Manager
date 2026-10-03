"""Provision a dedicated read-only dump role and encrypt its connection URL.

This owner-invoked setup creates a new role only. It never alters an existing
role, application tables, shared PUBLIC privileges, or production rows.
The recovery private identity must remain outside GitHub and Railway.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import psycopg2
from psycopg2 import extensions, sql
from sqlalchemy.engine import make_url

ROLE_SAFETY_SQL = """
SELECT NOT role.rolsuper AND NOT role.rolcreaterole AND NOT role.rolcreatedb
  AND NOT role.rolreplication AND NOT role.rolbypassrls
  AND NOT has_database_privilege(role.rolname, current_database(), 'CREATE')
  AND NOT has_schema_privilege(role.rolname, 'public', 'CREATE')
  AND NOT EXISTS (
    SELECT 1 FROM pg_class object
    JOIN pg_namespace namespace ON namespace.oid = object.relnamespace
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
      AND ((object.relkind IN ('r', 'p', 'v', 'm', 'f')
        AND has_table_privilege(role.rolname, object.oid,
          'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'))
        OR (object.relkind = 'S'
          AND has_sequence_privilege(role.rolname, object.oid, 'UPDATE')))
  )
FROM pg_roles role WHERE role.rolname = %s
"""


def connection_url(admin_url: str, role: str, password: str) -> str:
    if not re.fullmatch(r'proam_backup_dump_[a-z0-9_]{1,40}', role):
        raise ValueError('Invalid dedicated dump role name')
    if len(password) < 40:
        raise ValueError('Dump role password must have at least 40 characters')
    url = make_url(admin_url.replace('postgres://', 'postgresql://', 1))
    if url.get_backend_name() != 'postgresql':
        raise ValueError('PostgreSQL is required')
    return url.set(drivername='postgresql', username=role, password=password).render_as_string(
        hide_password=False,
    )


def create_dump_role(admin_url: str, role: str, password: str) -> dict:
    connection_url(admin_url, role, password)
    admin_url = admin_url.replace('postgres://', 'postgresql://', 1)
    connection = psycopg2.connect(admin_url, connect_timeout=10)
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '30s'")
                cursor.execute("SET LOCAL lock_timeout = '5s'")
                cursor.execute("SELECT current_setting('server_version_num')::int")
                if cursor.fetchone()[0] < 140000:
                    raise RuntimeError('PostgreSQL 14 or newer is required')
                cursor.execute('SELECT 1 FROM pg_roles WHERE rolname = %s', (role,))
                if cursor.fetchone():
                    raise RuntimeError('Existing roles are never modified by setup')
                verifier = extensions.encrypt_password(
                    password, role, connection, algorithm='scram-sha-256',
                )
                cursor.execute(sql.SQL(
                    'CREATE ROLE {} LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE '
                    'NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 3 PASSWORD %s'
                ).format(sql.Identifier(role)), (verifier,))
                cursor.execute(sql.SQL(
                    'ALTER ROLE {} SET default_transaction_read_only = on'
                ).format(sql.Identifier(role)))
                cursor.execute(sql.SQL('GRANT pg_read_all_data TO {}').format(sql.Identifier(role)))
                cursor.execute('SELECT current_database()')
                database = cursor.fetchone()[0]
                cursor.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(
                    sql.Identifier(database), sql.Identifier(role),
                ))
                cursor.execute(ROLE_SAFETY_SQL, (role,))
                if cursor.fetchone() != (True,):
                    raise RuntimeError('Read-only privilege guard rejected setup; transaction rolled back')
        return {'role_created': role, 'read_only_privileges_verified': True,
                'existing_roles_modified': False, 'production_rows_modified': False}
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    stage = 'configuration validation'
    try:
        recipient = os.environ['BACKUP_AGE_RECIPIENT'].strip()
        fingerprint = os.environ['BACKUP_AGE_RECIPIENT_SHA256'].strip().lower()
        if not re.fullmatch(r'age1[0-9a-z]+', recipient):
            raise ValueError('Invalid public recipient')
        if hashlib.sha256(recipient.encode()).hexdigest() != fingerprint:
            raise ValueError('Recipient fingerprint mismatch')
        role = os.environ['BACKUP_DUMP_ROLE_NAME']
        password = os.environ['BACKUP_ROLE_BOOTSTRAP_PASSWORD']
        admin_url = os.environ['RAILWAY_PG_PUBLIC_URL']
        dump_url = connection_url(admin_url, role, password)
        if args.output.exists():
            raise FileExistsError('Encrypted credential output already exists')
        stage = 'credential encryption'
        subprocess.run(['age', '--recipient', recipient, '--output', str(args.output)],
                       input=dump_url.encode(), capture_output=True, check=True)
        stage = 'role provisioning'
        result = create_dump_role(admin_url, role, password)
        print(json.dumps(result))
        return 0
    except Exception as error:
        # Database exceptions may contain connection URLs or password verifiers.
        print(f'Backup setup failed during {stage} ({type(error).__name__}); '
              'sensitive diagnostics suppressed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
