"""Add headshot_filename and phone_opted_in to competitors

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-02-27 00:01:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'e2f3a4b5c6d7'
down_revision = 'd1e2f3a4b5c6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('pro_competitors', sa.Column('headshot_filename', sa.Text(), nullable=True))
    op.add_column('college_competitors', sa.Column('headshot_filename', sa.Text(), nullable=True))
    op.add_column('pro_competitors', sa.Column('phone_opted_in', sa.Boolean(), nullable=True, server_default='0'))
    op.add_column('college_competitors', sa.Column('phone_opted_in', sa.Boolean(), nullable=True, server_default='0'))


def downgrade():
    op.drop_column('pro_competitors', 'headshot_filename')
    op.drop_column('college_competitors', 'headshot_filename')
    op.drop_column('pro_competitors', 'phone_opted_in')
    op.drop_column('college_competitors', 'phone_opted_in')
