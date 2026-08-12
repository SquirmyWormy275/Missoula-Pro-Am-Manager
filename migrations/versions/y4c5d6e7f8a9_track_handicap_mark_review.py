"""Track explicit review of handicap marks.

Revision ID: y4c5d6e7f8a9
Revises: x3c4d5e6f7a8
Create Date: 2026-08-12

A zero-second mark is a valid deliberate scratch.  Persisting a review time
lets preflight distinguish it from a late entrant whose default mark has not
been considered by a judge.
"""

from alembic import op
import sqlalchemy as sa


revision = "y4c5d6e7f8a9"
down_revision = "x3c4d5e6f7a8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "event_results",
        sa.Column("mark_assigned_at", sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_column("event_results", "mark_assigned_at")
