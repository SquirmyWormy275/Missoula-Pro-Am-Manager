"""Add optimistic locking to normalized relay teams.

Revision ID: x3c4d5e6f7a8
Revises: w2b3c4d5e6f7
Create Date: 2026-08-11

Relay teams are edited by scorekeepers and payout administrators from
different screens.  A version counter makes a stale update fail instead of
silently overwriting the last saved team state.
"""

from alembic import op
import sqlalchemy as sa


revision = "x3c4d5e6f7a8"
down_revision = "w2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("relay_teams") as batch_op:
        batch_op.add_column(
            sa.Column("version_id", sa.Integer(), nullable=False, server_default="1")
        )


def downgrade():
    with op.batch_alter_table("relay_teams") as batch_op:
        batch_op.drop_column("version_id")
