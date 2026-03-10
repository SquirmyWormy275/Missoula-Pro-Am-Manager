"""Add strathmark_id to pro_competitors and college_competitors

Revision ID: k8l9m0n1o2p3
Revises: j7k8l9m0n1o2
Create Date: 2026-03-09

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'k8l9m0n1o2p3'
down_revision = 'j7k8l9m0n1o2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('pro_competitors') as batch_op:
        batch_op.add_column(sa.Column('strathmark_id', sa.String(50), nullable=True))
        batch_op.create_index('ix_pro_competitors_strathmark_id', ['strathmark_id'])

    with op.batch_alter_table('college_competitors') as batch_op:
        batch_op.add_column(sa.Column('strathmark_id', sa.String(50), nullable=True))
        batch_op.create_index('ix_college_competitors_strathmark_id', ['strathmark_id'])


def downgrade():
    with op.batch_alter_table('college_competitors') as batch_op:
        batch_op.drop_index('ix_college_competitors_strathmark_id')
        batch_op.drop_column('strathmark_id')

    with op.batch_alter_table('pro_competitors') as batch_op:
        batch_op.drop_index('ix_pro_competitors_strathmark_id')
        batch_op.drop_column('strathmark_id')
