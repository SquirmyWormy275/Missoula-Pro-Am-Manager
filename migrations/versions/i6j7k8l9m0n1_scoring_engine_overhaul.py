"""scoring engine overhaul

Adds:
- events.is_finalized (Boolean, default False)
- events.requires_triple_runs (Boolean, default False)
- event_results.run3_value (Float, nullable)
- event_results.tiebreak_value (Float, nullable) — Hard-Hit time tiebreak
- event_results.throwoff_pending (Boolean, default False) — axe throw tie flag
- event_results.handicap_factor (Float, default 1.0) — future integration placeholder
- heats.locked_by_user_id (Integer FK users.id, nullable)
- heats.locked_at (DateTime, nullable)
- payout_templates table

Revision ID: i6j7k8l9m0n1
Revises: h5i6j7k8l9m0
Create Date: 2026-03-07
"""
from alembic import op
import sqlalchemy as sa

revision = 'i6j7k8l9m0n1'
down_revision = 'h5i6j7k8l9m0'
branch_labels = None
depends_on = None


def upgrade():
    # --- events table ---
    op.add_column('events', sa.Column('is_finalized', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('events', sa.Column('requires_triple_runs', sa.Boolean(), nullable=False, server_default='false'))

    # --- event_results table ---
    op.add_column('event_results', sa.Column('run3_value', sa.Float(), nullable=True))
    op.add_column('event_results', sa.Column('tiebreak_value', sa.Float(), nullable=True))
    op.add_column('event_results', sa.Column('throwoff_pending', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('event_results', sa.Column('handicap_factor', sa.Float(), nullable=False, server_default='1.0'))

    # --- heats table ---
    op.add_column('heats', sa.Column('locked_by_user_id', sa.Integer(), nullable=True))
    op.add_column('heats', sa.Column('locked_at', sa.DateTime(), nullable=True))
    op.create_foreign_key('fk_heats_locked_by_user_id', 'heats', 'users', ['locked_by_user_id'], ['id'])

    # --- payout_templates table ---
    op.create_table(
        'payout_templates',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('payouts', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )


def downgrade():
    op.drop_table('payout_templates')

    with op.batch_alter_table('heats', schema=None) as batch_op:
        batch_op.drop_constraint('fk_heats_locked_by_user_id', type_='foreignkey')
        batch_op.drop_column('locked_at')
        batch_op.drop_column('locked_by_user_id')

    with op.batch_alter_table('event_results', schema=None) as batch_op:
        batch_op.drop_column('handicap_factor')
        batch_op.drop_column('throwoff_pending')
        batch_op.drop_column('tiebreak_value')
        batch_op.drop_column('run3_value')

    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.drop_column('requires_triple_runs')
        batch_op.drop_column('is_finalized')
