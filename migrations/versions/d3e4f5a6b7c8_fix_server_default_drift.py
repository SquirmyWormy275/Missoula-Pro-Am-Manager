"""Fix server-default drift for handicap_factor.

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-04-19
"""

from alembic import op
import sqlalchemy as sa


revision = 'd3e4f5a6b7c8'
down_revision = 'c2d3e4f5a6b7'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'sqlite':
        with op.batch_alter_table('event_results') as batch_op:
            batch_op.alter_column(
                'handicap_factor',
                existing_type=sa.Float(),
                server_default='0.0',
            )
        return

    op.alter_column(
        'event_results',
        'handicap_factor',
        existing_type=sa.Float(),
        server_default='0.0',
    )


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'sqlite':
        with op.batch_alter_table('event_results') as batch_op:
            batch_op.alter_column(
                'handicap_factor',
                existing_type=sa.Float(),
                server_default='1.0',
            )
        return

    op.alter_column(
        'event_results',
        'handicap_factor',
        existing_type=sa.Float(),
        server_default='1.0',
    )
