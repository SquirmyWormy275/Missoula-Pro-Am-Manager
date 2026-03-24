"""Add heat flight_position and pro springboard_slow_heat flag

Revision ID: a9b8c7d6e5f4
Revises: f3a4b5c6d7e8
Create Date: 2026-03-05 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'a9b8c7d6e5f4'
down_revision = 'f3a4b5c6d7e8'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('heats', sa.Column('flight_position', sa.Integer(), nullable=True))
    op.add_column(
        'pro_competitors',
        sa.Column('springboard_slow_heat', sa.Boolean(), nullable=True, server_default=sa.false()),
    )
    op.execute(
        "UPDATE pro_competitors SET springboard_slow_heat = 0 "
        "WHERE springboard_slow_heat IS NULL"
    )


def downgrade():
    op.drop_column('pro_competitors', 'springboard_slow_heat')
    op.drop_column('heats', 'flight_position')
