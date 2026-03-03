"""add validation_errors to teams

Revision ID: a4b5c6d7e8f9
Revises: f3a4b5c6d7e8
Create Date: 2026-03-02

Adds a TEXT column to the teams table to store structured validation error
data (JSON list of dicts) for teams that failed constraint checks on upload.
Also introduces 'invalid' as a valid team status alongside 'active' and 'scratched'.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'a4b5c6d7e8f9'
down_revision = 'f3a4b5c6d7e8'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('teams', sa.Column('validation_errors', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('teams', 'validation_errors')
