"""add pro_event_ranks table

Revision ID: d7e8f9a0b1c2
Revises: b5f1e9cd7c50
Create Date: 2026-03-07 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd7e8f9a0b1c2'
down_revision = 'b5f1e9cd7c50'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'pro_event_ranks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tournament_id', sa.Integer(), nullable=False),
        sa.Column('competitor_id', sa.Integer(), nullable=False),
        sa.Column('event_category', sa.String(length=32), nullable=False),
        sa.Column('rank', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['tournament_id'], ['tournaments.id']),
        sa.ForeignKeyConstraint(['competitor_id'], ['pro_competitors.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'tournament_id', 'competitor_id', 'event_category',
            name='uq_pro_event_rank_tournament_comp_cat'
        ),
    )
    op.create_index(
        'ix_pro_event_ranks_tournament_cat',
        'pro_event_ranks',
        ['tournament_id', 'event_category'],
    )


def downgrade():
    op.drop_index('ix_pro_event_ranks_tournament_cat', table_name='pro_event_ranks')
    op.drop_table('pro_event_ranks')
