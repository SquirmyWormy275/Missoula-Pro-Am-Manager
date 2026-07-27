"""Schema parity fix — add columns that models declare but prior migrations missed.

Covers columns on users, college_competitors, and cleans up stale is_active on users.
These columns were present in the SQLAlchemy models but never had corresponding ALTER
TABLE statements in the migration chain, causing 500 errors on fresh databases.

Revision ID: e9f0a1b2c3d4
Revises: d8d4aa7bdb45
Create Date: 2026-03-23
"""
from alembic import op
import sqlalchemy as sa

revision = 'e9f0a1b2c3d4'
down_revision = 'd8d4aa7bdb45'
branch_labels = None
depends_on = None


def _get_existing_columns(table):
    """Get existing column names for a table (works on both SQLite and PostgreSQL)."""
    conn = op.get_bind()
    dialect = conn.dialect.name
    if dialect == 'sqlite':
        result = conn.execute(sa.text(f'PRAGMA table_info("{table}")'))
        return {row[1] for row in result}
    else:
        # PostgreSQL / other dialects: use information_schema
        result = conn.execute(sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :table"
        ), {'table': table})
        return {row[0] for row in result}


def _add_column_if_missing(table, column_name, column_type):
    """Add a column only if it doesn't already exist (idempotent for patched DBs)."""
    existing = _get_existing_columns(table)
    if column_name not in existing:
        op.add_column(table, sa.Column(column_name, column_type))
        return True
    return False


def upgrade():
    # --- users table ---
    _add_column_if_missing('users', 'tournament_id',
                           sa.Integer())
    _add_column_if_missing('users', 'competitor_type',
                           sa.String(20))
    _add_column_if_missing('users', 'competitor_id',
                           sa.Integer())
    _add_column_if_missing('users', 'display_name',
                           sa.String(200))
    _add_column_if_missing('users', 'is_active_user',
                           sa.Boolean())

    # Backfill is_active_user from stale is_active if both exist
    cols = _get_existing_columns('users')
    if 'is_active' in cols and 'is_active_user' in cols:
        conn = op.get_bind()
        conn.execute(sa.text(
            'UPDATE users SET is_active_user = is_active WHERE is_active_user IS NULL'
        ))

    # --- tournaments table ---
    _add_column_if_missing('tournaments', 'providing_shirts',
                           sa.Boolean())
    _add_column_if_missing('tournaments', 'schedule_config',
                           sa.Text())

    # --- college_competitors table ---
    _add_column_if_missing('college_competitors', 'headshot_filename',
                           sa.String(200))
    _add_column_if_missing('college_competitors', 'phone_opted_in',
                           sa.Boolean())


def downgrade():
    # Best-effort reverse — SQLite doesn't support DROP COLUMN before 3.35
    pass
