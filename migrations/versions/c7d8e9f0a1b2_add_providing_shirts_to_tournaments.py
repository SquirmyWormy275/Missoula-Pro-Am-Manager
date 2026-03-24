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
    op.add_column('tournaments', sa.Column(
        'providing_shirts',
        sa.Boolean(),
        nullable=True,
        server_default=sa.text('false'),
    ))


def downgrade():
    op.drop_column('tournaments', 'providing_shirts')
