"""add wood_configs table

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-03-02

Adds the wood_configs table for the Virtual Woodboss feature.
Stores per-tournament wood species and size configuration for chopping
blocks and saw logs (one row per tournament + config_key combination).
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'b5c6d7e8f9a0'
down_revision = 'a4b5c6d7e8f9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'wood_configs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tournament_id', sa.Integer(), nullable=False),
        sa.Column('config_key', sa.String(length=100), nullable=False),
        sa.Column('species', sa.Text(), nullable=True),
        sa.Column('size_value', sa.Float(), nullable=True),
        sa.Column('size_unit', sa.String(length=4), nullable=False, server_default='in'),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['tournament_id'], ['tournaments.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tournament_id', 'config_key',
                            name='uq_wood_config_tournament_key'),
    )


def downgrade():
    op.drop_table('wood_configs')
