"""Add is_override column to teams (admin validation override persistence).

Allows a judge to mark a team valid despite failing roster constraints (small
schools with e.g. 5 men + 0 women that still want to compete). Re-validation
and Excel re-imports preserve this flag via Team.set_validation_errors so the
override survives operational churn.

Revision ID: l2a3b4c5d6e7
Revises: f5a6b7c8d9e0
Create Date: 2026-04-21 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "l2a3b4c5d6e7"
down_revision = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade():
    # Direct op.add_column call for PG safety per CLAUDE.md migration protocol.
    # Boolean server_default uses sa.text('false') — NEVER '0' or sa.text('0')
    # because PostgreSQL rejects integer literals on boolean columns.
    op.add_column(
        "teams",
        sa.Column(
            "is_override",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade():
    op.drop_column("teams", "is_override")
