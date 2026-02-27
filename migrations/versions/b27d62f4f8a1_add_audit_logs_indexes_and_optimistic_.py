"""add audit logs indexes and optimistic locking

Revision ID: b27d62f4f8a1
Revises: 7f8f2f600aa1
Create Date: 2026-02-27 23:20:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b27d62f4f8a1'
down_revision = '7f8f2f600aa1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'audit_logs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('actor_user_id', sa.Integer(), nullable=True),
        sa.Column('action', sa.String(length=80), nullable=False),
        sa.Column('entity_type', sa.String(length=80), nullable=False),
        sa.Column('entity_id', sa.Integer(), nullable=True),
        sa.Column('ip_address', sa.String(length=64), nullable=True),
        sa.Column('user_agent', sa.String(length=255), nullable=True),
        sa.Column('details_json', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['actor_user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.create_index('ix_audit_logs_created_at', ['created_at'], unique=False)
        batch_op.create_index('ix_audit_logs_actor', ['actor_user_id'], unique=False)
        batch_op.create_index('ix_audit_logs_action', ['action'], unique=False)

    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.create_index('ix_events_tournament_type_status', ['tournament_id', 'event_type', 'status'], unique=False)

    with op.batch_alter_table('event_results', schema=None) as batch_op:
        batch_op.add_column(sa.Column('version_id', sa.Integer(), nullable=False, server_default='1'))
        batch_op.create_index('ix_event_results_event_status', ['event_id', 'status'], unique=False)
        batch_op.create_unique_constraint('uq_event_result_competitor', ['event_id', 'competitor_id', 'competitor_type'])
    with op.batch_alter_table('event_results', schema=None) as batch_op:
        batch_op.alter_column('version_id', server_default=None)

    with op.batch_alter_table('heats', schema=None) as batch_op:
        batch_op.add_column(sa.Column('version_id', sa.Integer(), nullable=False, server_default='1'))
        batch_op.create_index('ix_heats_event_status', ['event_id', 'status'], unique=False)
        batch_op.create_index('ix_heats_flight_id', ['flight_id'], unique=False)
        batch_op.create_unique_constraint('uq_event_heat_run', ['event_id', 'heat_number', 'run_number'])
    with op.batch_alter_table('heats', schema=None) as batch_op:
        batch_op.alter_column('version_id', server_default=None)


def downgrade():
    with op.batch_alter_table('heats', schema=None) as batch_op:
        batch_op.drop_constraint('uq_event_heat_run', type_='unique')
        batch_op.drop_index('ix_heats_flight_id')
        batch_op.drop_index('ix_heats_event_status')
        batch_op.drop_column('version_id')

    with op.batch_alter_table('event_results', schema=None) as batch_op:
        batch_op.drop_constraint('uq_event_result_competitor', type_='unique')
        batch_op.drop_index('ix_event_results_event_status')
        batch_op.drop_column('version_id')

    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.drop_index('ix_events_tournament_type_status')

    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.drop_index('ix_audit_logs_action')
        batch_op.drop_index('ix_audit_logs_actor')
        batch_op.drop_index('ix_audit_logs_created_at')
    op.drop_table('audit_logs')

