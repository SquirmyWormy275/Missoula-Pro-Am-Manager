"""Add durable background job records.

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-04-20
"""

from alembic import op
import sqlalchemy as sa


revision = 'e4f5a6b7c8d9'
down_revision = 'd3e4f5a6b7c8'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'background_jobs',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('label', sa.String(length=120), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('tournament_id', sa.Integer(), nullable=True),
        sa.Column('submitted_at', sa.DateTime(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('result_json', sa.Text(), nullable=True),
        sa.Column('error_text', sa.Text(), nullable=True),
        sa.Column('metadata_json', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_background_jobs_status', 'background_jobs', ['status'])
    op.create_index('ix_background_jobs_submitted_at', 'background_jobs', ['submitted_at'])
    op.create_index('ix_background_jobs_tournament_id', 'background_jobs', ['tournament_id'])


def downgrade():
    op.drop_index('ix_background_jobs_tournament_id', table_name='background_jobs')
    op.drop_index('ix_background_jobs_submitted_at', table_name='background_jobs')
    op.drop_index('ix_background_jobs_status', table_name='background_jobs')
    op.drop_table('background_jobs')
