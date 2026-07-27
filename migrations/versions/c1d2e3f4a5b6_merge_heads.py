"""Merge migration heads

Revision ID: c1d2e3f4a5b6
Revises: 8b2fd0d307bb, b27d62f4f8a1
Create Date: 2026-02-27 23:59:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c1d2e3f4a5b6'
down_revision = ('8b2fd0d307bb', 'b27d62f4f8a1')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
