"""convert points columns Integer to Numeric for fractional split-ties

Phase 1B of the scoring system fix (V2.8.0).

Converts three Integer columns to Numeric so that fractional placement points
from split-tie scoring (Phase 3) can be represented accurately:

  - event_results.points_awarded:    Integer → Numeric(6, 2)
  - college_competitors.individual_points: Integer → Numeric(8, 2)
  - teams.total_points:              Integer → Numeric(8, 2)

Why this is needed
==================
The AWFC tie-split rule says that competitors tied at the same average time
share the combined points of the positions they collectively occupy.  Examples:

  - Two competitors tied for 5th place each receive (2 + 1) / 2 = 1.5 points
  - Three competitors tied for 1st each receive (10 + 7 + 5) / 3 = 7.33 points

The previous Integer columns silently truncated fractional values to whole
numbers on insert, causing the system to either award the wrong points or
duplicate points across all tied rows.  Phase 3 of the scoring fix replaces
the duplicate-points tie handling with a proper split, but it cannot be
deployed until these columns can store fractional values.

This migration is also a deliberate drift fix
=============================================
Per the Migration Protocol in CLAUDE.md Section 6:

  > "Never fix drift from a prior session inside a new feature migration.
  >  Create a separate fix_xyz_drift migration."

This migration is the dedicated drift-fix.  Three entries have been removed
from KNOWN_NULLABLE_DRIFT in tests/test_migration_integrity.py as part of the
same PR — those entries pointed at exactly the columns this migration tightens.

Why batch_alter_table here
==========================
SQLite does NOT support `ALTER TABLE ... ALTER COLUMN` natively.  Any column
type change requires Alembic's batch mode, which on SQLite uses the table-
rebuild pattern (CREATE new → INSERT FROM old → DROP old → RENAME).  On
PostgreSQL, batch_alter_table emits direct `ALTER TABLE ... ALTER COLUMN`
DDL — the table-rebuild dance only happens on SQLite.

The PG safety scanner in tests/test_pg_migration_safety.py explicitly permits
batch_alter_table when it wraps `alter_column` (see _BATCH_REQUIRED_OPS at
test_pg_migration_safety.py:105-108).  The ban applies only when batch wraps
operations that have direct op.* equivalents (add_column, drop_column).

postgresql_using=
=================
Each alter_column call passes `postgresql_using='col::numeric(N,M)'` so that
PostgreSQL can convert the column in place.  Without this, PG would raise
"column cannot be cast automatically to type numeric" and require a manual
USING clause.  SQLite ignores this argument because the table-rebuild path
copies values via INSERT INTO ... SELECT and the type coercion happens at
INSERT time.

server_default=
===============
Each column gets `server_default=sa.text("'0.00'")` to match what the
corresponding model now declares.  This closes the long-standing nullable
drift on these three columns (see KNOWN_NULLABLE_DRIFT removal in this PR).

Revision ID: f0a1b2c3d4e6
Revises: f0a1b2c3d4e5
Create Date: 2026-04-08
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f0a1b2c3d4e6'
down_revision = 'f0a1b2c3d4e5'
branch_labels = None
depends_on = None


def upgrade():
    # event_results.points_awarded: Integer → Numeric(6, 2)
    with op.batch_alter_table('event_results', schema=None) as batch_op:
        batch_op.alter_column(
            'points_awarded',
            existing_type=sa.Integer(),
            type_=sa.Numeric(6, 2),
            existing_nullable=False,
            nullable=False,
            existing_server_default=None,
            server_default=sa.text("'0.00'"),
            postgresql_using='points_awarded::numeric(6,2)',
        )

    # college_competitors.individual_points: Integer → Numeric(8, 2)
    with op.batch_alter_table('college_competitors', schema=None) as batch_op:
        batch_op.alter_column(
            'individual_points',
            existing_type=sa.Integer(),
            type_=sa.Numeric(8, 2),
            existing_nullable=False,
            nullable=False,
            existing_server_default=None,
            server_default=sa.text("'0.00'"),
            postgresql_using='individual_points::numeric(8,2)',
        )

    # teams.total_points: Integer → Numeric(8, 2)
    with op.batch_alter_table('teams', schema=None) as batch_op:
        batch_op.alter_column(
            'total_points',
            existing_type=sa.Integer(),
            type_=sa.Numeric(8, 2),
            existing_nullable=False,
            nullable=False,
            existing_server_default=None,
            server_default=sa.text("'0.00'"),
            postgresql_using='total_points::numeric(8,2)',
        )


def downgrade():
    # Downgrade is lossy: any fractional points awarded after this migration
    # was applied will be ROUNDED on the way back to Integer.  In practice
    # this only matters if you're rolling back after a tournament has been
    # scored using fractional ties.  The ROUND() in postgresql_using handles
    # PG; SQLite's INSERT INTO ... SELECT will silently truncate Decimal to
    # Integer during the table rebuild.
    with op.batch_alter_table('teams', schema=None) as batch_op:
        batch_op.alter_column(
            'total_points',
            existing_type=sa.Numeric(8, 2),
            type_=sa.Integer(),
            existing_nullable=False,
            nullable=False,
            existing_server_default=sa.text("'0.00'"),
            server_default=sa.text("'0'"),
            postgresql_using='ROUND(total_points)::integer',
        )

    with op.batch_alter_table('college_competitors', schema=None) as batch_op:
        batch_op.alter_column(
            'individual_points',
            existing_type=sa.Numeric(8, 2),
            type_=sa.Integer(),
            existing_nullable=False,
            nullable=False,
            existing_server_default=sa.text("'0.00'"),
            server_default=sa.text("'0'"),
            postgresql_using='ROUND(individual_points)::integer',
        )

    with op.batch_alter_table('event_results', schema=None) as batch_op:
        batch_op.alter_column(
            'points_awarded',
            existing_type=sa.Numeric(6, 2),
            type_=sa.Integer(),
            existing_nullable=False,
            nullable=False,
            existing_server_default=sa.text("'0.00'"),
            server_default=sa.text("'0'"),
            postgresql_using='ROUND(points_awarded)::integer',
        )
