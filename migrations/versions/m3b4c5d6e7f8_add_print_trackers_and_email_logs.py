"""Add print_trackers + print_email_logs tables for the Print Hub feature.

- print_trackers: staleness tracker (one row per (tournament, doc_key, entity_id)).
- print_email_logs: audit trail for Print Hub email sends.

PG-safety: direct op.create_table + op.create_index only. No batch_alter_table.
Booleans: not present. server_default sa.text('false') unused here.

Revision ID: m3b4c5d6e7f8
Revises: l2a3b4c5d6e7
Create Date: 2026-04-21 20:45:00
"""

import sqlalchemy as sa
from alembic import op

revision = "m3b4c5d6e7f8"
down_revision = "l2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "print_trackers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tournament_id", sa.Integer(), nullable=False),
        sa.Column("doc_key", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("last_printed_at", sa.DateTime(), nullable=False),
        sa.Column("last_printed_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("last_printed_by_user_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["tournament_id"], ["tournaments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["last_printed_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tournament_id",
            "doc_key",
            "entity_id",
            name="uq_print_tracker_tournament_doc_entity",
        ),
    )
    op.create_index(
        "ix_print_trackers_tournament_id",
        "print_trackers",
        ["tournament_id"],
        unique=False,
    )

    op.create_table(
        "print_email_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tournament_id", sa.Integer(), nullable=False),
        sa.Column("doc_key", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("recipients_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("subject", sa.String(length=300), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=False),
        sa.Column("sent_by_user_id", sa.Integer(), nullable=True),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="queued"
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["tournament_id"], ["tournaments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["sent_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('queued', 'sent', 'failed')",
            name="ck_print_email_logs_status_valid",
        ),
    )
    op.create_index(
        "ix_print_email_logs_tournament_sent",
        "print_email_logs",
        ["tournament_id", "sent_at"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_print_email_logs_tournament_sent", table_name="print_email_logs")
    op.drop_table("print_email_logs")
    op.drop_index("ix_print_trackers_tournament_id", table_name="print_trackers")
    op.drop_table("print_trackers")
