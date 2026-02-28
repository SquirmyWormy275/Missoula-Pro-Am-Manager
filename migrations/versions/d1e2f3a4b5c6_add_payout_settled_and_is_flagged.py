"""Add payout_settled to pro_competitors and is_flagged to event_results

Revision ID: d1e2f3a4b5c6
Revises: c1d2e3f4a5b6
Create Date: 2026-02-27 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'd1e2f3a4b5c6'
down_revision = 'c1d2e3f4a5b6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('pro_competitors', sa.Column('payout_settled', sa.Boolean(), nullable=True, server_default='0'))
    op.add_column('event_results', sa.Column('is_flagged', sa.Boolean(), nullable=True, server_default='0'))


def downgrade():
    op.drop_column('pro_competitors', 'payout_settled')
    op.drop_column('event_results', 'is_flagged')
