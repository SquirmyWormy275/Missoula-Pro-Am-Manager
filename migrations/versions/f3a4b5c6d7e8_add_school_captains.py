"""Add school_captains table for per-school captain portal accounts

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-02-27 00:10:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'f3a4b5c6d7e8'
down_revision = 'e2f3a4b5c6d7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'school_captains',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tournament_id', sa.Integer(), nullable=False),
        sa.Column('school_name', sa.String(length=200), nullable=False),
        sa.Column('pin_hash', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=True),
        sa.ForeignKeyConstraint(['tournament_id'], ['tournaments.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tournament_id', 'school_name', name='unique_school_captain_per_tournament'),
    )


def downgrade():
    op.drop_table('school_captains')
