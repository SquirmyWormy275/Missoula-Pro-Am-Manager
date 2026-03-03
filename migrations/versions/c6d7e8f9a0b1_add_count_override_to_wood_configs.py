"""add count_override to wood_configs

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-03-02

Adds a count_override column to wood_configs so the head judge can manually
specify block counts for events not driven by enrollment data (e.g. Pro-Am
Relay teams whose count is determined by lottery, not registration).
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'c6d7e8f9a0b1'
down_revision = 'b5c6d7e8f9a0'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('wood_configs', sa.Column('count_override', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column('wood_configs', 'count_override')
