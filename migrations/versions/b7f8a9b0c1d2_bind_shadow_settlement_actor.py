"""Bind each shadow settlement delivery to its authenticated operator.

Revision ID: b7f8a9b0c1d2
Revises: a6e7f8a9b0c1
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op


revision = "b7f8a9b0c1d2"
down_revision = "a6e7f8a9b0c1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("shadow_settlement_outbox", schema=None) as batch_op:
        batch_op.add_column(sa.Column("actor_id", sa.Integer(), nullable=True))

    op.execute(
        """
        UPDATE shadow_settlement_outbox
        SET actor_id = (
            SELECT COALESCE(shadow_handicap_runs.issued_by_id, shadow_handicap_runs.created_by_id)
            FROM shadow_handicap_runs
            WHERE shadow_handicap_runs.id = shadow_settlement_outbox.run_id
        )
        WHERE actor_id IS NULL
        """
    )
    with op.batch_alter_table("shadow_settlement_outbox", schema=None) as batch_op:
        batch_op.alter_column("actor_id", existing_type=sa.Integer(), nullable=False)
        batch_op.create_foreign_key(
            "fk_shadow_settlement_outbox_actor",
            "users",
            ["actor_id"],
            ["id"],
        )


def downgrade():
    with op.batch_alter_table("shadow_settlement_outbox", schema=None) as batch_op:
        batch_op.drop_constraint("fk_shadow_settlement_outbox_actor", type_="foreignkey")
        batch_op.drop_column("actor_id")
