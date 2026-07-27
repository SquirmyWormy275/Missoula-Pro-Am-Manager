"""Shared column type helpers for the event-log substrate.

BIG_ID
    Renders BIGINT (BIGSERIAL when used as an autoincrement primary key) on
    PostgreSQL, and plain INTEGER on SQLite.  The variant is required, not
    cosmetic: SQLite only treats a column declared *exactly* ``INTEGER PRIMARY
    KEY`` as a rowid alias.  ``BIGINT PRIMARY KEY`` on SQLite does not
    autoincrement, so every insert in the test suite would have to supply its
    own id.  Production runs PostgreSQL, where BIGINT is what the log actually
    needs.

JSON_PAYLOAD
    Renders JSONB on PostgreSQL (indexable, queryable) and TEXT on SQLite.
    ``sa.JSON`` was not used because it maps to plain JSON on PostgreSQL, which
    gives up every advantage JSONB has.

Both constants must be used identically in the models and in the Alembic
migration that creates the columns, or tests/test_migration_integrity.py will
report a type mismatch between the ``db.create_all()`` schema and the
``flask db upgrade`` schema.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

BIG_ID = sa.BigInteger().with_variant(sa.Integer(), 'sqlite')

JSON_PAYLOAD = postgresql.JSONB().with_variant(sa.Text(), 'sqlite')
