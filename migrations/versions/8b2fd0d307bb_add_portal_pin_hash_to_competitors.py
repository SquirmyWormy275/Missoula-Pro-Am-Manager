"""add portal pin hash to competitors

Revision ID: 8b2fd0d307bb
Revises: 7f8f2f600aa1
Create Date: 2026-02-27 00:00:01.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '8b2fd0d307bb'
down_revision = '7f8f2f600aa1'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('college_competitors', schema=None) as batch_op:
        batch_op.add_column(sa.Column('portal_pin_hash', sa.String(length=255), nullable=True))

    with op.batch_alter_table('pro_competitors', schema=None) as batch_op:
        batch_op.add_column(sa.Column('portal_pin_hash', sa.String(length=255), nullable=True))


def downgrade():
    with op.batch_alter_table('pro_competitors', schema=None) as batch_op:
        batch_op.drop_column('portal_pin_hash')

    with op.batch_alter_table('college_competitors', schema=None) as batch_op:
        batch_op.drop_column('portal_pin_hash')
