"""Add score submission receipts and background job boot ownership.

Revision ID: a6b7c8d9e0f1
Revises: c8a9b0c1d2e3
Create Date: 2026-08-18

Receipts make a committed heat score idempotent across lost responses and
offline replay. Boot ownership lets startup distinguish abandoned process-local
jobs from work owned by the current process.
"""

from alembic import op
import sqlalchemy as sa


revision = "a6b7c8d9e0f1"
down_revision = "c8a9b0c1d2e3"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "background_jobs",
        sa.Column("owner_boot_id", sa.String(length=32), nullable=True),
    )
    op.create_index(
        "ix_background_jobs_owner_boot_id",
        "background_jobs",
        ["owner_boot_id"],
        unique=False,
    )
    op.add_column(
        "background_jobs",
        sa.Column("owner_heartbeat_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_background_jobs_owner_heartbeat_at",
        "background_jobs",
        ["owner_heartbeat_at"],
        unique=False,
    )

    op.create_table(
        "score_submission_receipts",
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("tournament_id", sa.Integer(), nullable=False),
        sa.Column("heat_id", sa.Integer(), nullable=False),
        sa.Column("issuing_user_id", sa.Integer(), nullable=True),
        sa.Column("canonical_payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("accepted_outcome_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["issuing_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["tournament_id"], ["tournaments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.create_index(
        "ix_score_submission_receipts_binding",
        "score_submission_receipts",
        ["tournament_id", "heat_id", "issuing_user_id"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        "ix_score_submission_receipts_binding",
        table_name="score_submission_receipts",
    )
    op.drop_table("score_submission_receipts")

    op.drop_index(
        "ix_background_jobs_owner_boot_id",
        table_name="background_jobs",
    )
    op.drop_index(
        "ix_background_jobs_owner_heartbeat_at",
        table_name="background_jobs",
    )
    op.drop_column("background_jobs", "owner_heartbeat_at")
    op.drop_column("background_jobs", "owner_boot_id")
