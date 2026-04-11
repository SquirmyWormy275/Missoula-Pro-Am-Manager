"""add status_reason to event_result (judge sheet feature)

Adds a nullable TEXT column `status_reason` to `event_results` so judges can
record the reason for a non-completion status (dnf, dq, scratched).  No other
schema changes — single concern per the migration protocol.

Revision ID: a1b2c3d4e5f8
Revises: f0a1b2c3d4e6
Create Date: 2026-04-10

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f8"
down_revision = "f0a1b2c3d4e6"
branch_labels = None
depends_on = None


def upgrade():
    # Nullable TEXT — no server_default needed, existing rows get NULL.
    # PG-safe: direct op.add_column call.
    op.add_column(
        "event_results",
        sa.Column("status_reason", sa.Text(), nullable=True),
    )


def downgrade():
    op.drop_column("event_results", "status_reason")
