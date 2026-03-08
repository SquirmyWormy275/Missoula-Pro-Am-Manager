"""add providing_shirts to tournaments

Revision ID: c7d8e9f0a1b2
Revises: b5f1e9cd7c50
Create Date: 2026-03-06 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c7d8e9f0a1b2'
down_revision = 'b5f1e9cd7c50'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('tournaments', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'providing_shirts',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('0'),
        ))


def downgrade():
    with op.batch_alter_table('tournaments', schema=None) as batch_op:
        batch_op.drop_column('providing_shirts')
