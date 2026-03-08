"""add schedule_config to tournaments

Revision ID: h5i6j7k8l9m0
Revises: b5f1e9cd7c50
Create Date: 2026-03-07 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'h5i6j7k8l9m0'
down_revision = 'e8f9a0b1c2d3'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('tournaments', sa.Column('schedule_config', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('tournaments', 'schedule_config')
