"""add users for auth and portals

Revision ID: 7f8f2f600aa1
Revises: 41b9a6cbcfd4
Create Date: 2026-02-27 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7f8f2f600aa1'
down_revision = '41b9a6cbcfd4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('username', sa.String(length=80), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False),
        sa.Column('tournament_id', sa.Integer(), nullable=True),
        sa.Column('competitor_type', sa.String(length=20), nullable=True),
        sa.Column('competitor_id', sa.Integer(), nullable=True),
        sa.Column('display_name', sa.String(length=200), nullable=True),
        sa.Column('is_active_user', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['tournament_id'], ['tournaments.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('username'),
    )

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.create_index('ix_users_role', ['role'], unique=False)
        batch_op.create_index('ix_users_tournament_id', ['tournament_id'], unique=False)


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index('ix_users_tournament_id')
        batch_op.drop_index('ix_users_role')

    op.drop_table('users')
