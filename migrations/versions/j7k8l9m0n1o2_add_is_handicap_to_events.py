"""Add is_handicap to events

Adds:
- events.is_handicap (Boolean, default False) — Championship vs. Handicap format flag
  Applies to underhand, standing block, and springboard events only.

Revision ID: j7k8l9m0n1o2
Revises: i6j7k8l9m0n1
Create Date: 2026-03-09
"""
from alembic import op
import sqlalchemy as sa

revision = 'j7k8l9m0n1o2'
down_revision = 'i6j7k8l9m0n1'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_handicap', sa.Boolean(), nullable=False, server_default='0'))


def downgrade():
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.drop_column('is_handicap')
