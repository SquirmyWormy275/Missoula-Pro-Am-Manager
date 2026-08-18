"""Add immutable whole-field shadow review and issue evidence.

Revision ID: a6e7f8a9b0c1
Revises: a5e6f7a8b9c0
Create Date: 2026-08-14

These tables are scoring-inert.  They retain operator decisions and a
checksummed recommendation export without writing official EventResult marks.
"""

import sqlalchemy as sa
from alembic import op


revision = "a6e7f8a9b0c1"
down_revision = "a5e6f7a8b9c0"
branch_labels = None
depends_on = None

BIG_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
_TABLES = ("shadow_field_reviews", "shadow_issue_artifacts")


def _install_append_only_guards():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in _TABLES:
            op.execute(
                f"""
                CREATE TRIGGER trg_{table}_append_only
                BEFORE UPDATE OR DELETE ON public.{table}
                FOR EACH ROW EXECUTE FUNCTION public.reject_shadow_evidence_mutation()
                """
            )
    elif bind.dialect.name == "sqlite":
        for table in _TABLES:
            op.execute(
                f"""
                CREATE TRIGGER trg_{table}_append_only_update
                BEFORE UPDATE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, 'shadow evidence is append-only');
                END
                """
            )
            op.execute(
                f"""
                CREATE TRIGGER trg_{table}_append_only_delete
                BEFORE DELETE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, 'shadow evidence is append-only');
                END
                """
            )


def upgrade():
    op.create_table(
        "shadow_field_reviews",
        sa.Column("id", BIG_ID, primary_key=True, autoincrement=True),
        sa.Column("review_id", sa.String(length=224), nullable=False),
        sa.Column(
            "run_id",
            BIG_ID,
            sa.ForeignKey("shadow_handicap_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.String(length=80), nullable=False),
        sa.Column("receipt_core_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("decision_json", sa.Text(), nullable=False),
        sa.Column("decision_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("prediction_count", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("review_id", name="uq_shadow_field_review_id"),
        sa.UniqueConstraint("run_id", name="uq_shadow_field_review_run"),
        sa.CheckConstraint("prediction_count > 0", name="ck_shadow_field_review_count"),
    )
    op.create_table(
        "shadow_issue_artifacts",
        sa.Column("id", BIG_ID, primary_key=True, autoincrement=True),
        sa.Column("issue_id", sa.String(length=224), nullable=False),
        sa.Column(
            "run_id",
            BIG_ID,
            sa.ForeignKey("shadow_handicap_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.String(length=80), nullable=False),
        sa.Column("receipt_core_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("review_decision_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("export_json", sa.Text(), nullable=False),
        sa.Column("export_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("prediction_count", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("issue_id", name="uq_shadow_issue_artifact_id"),
        sa.UniqueConstraint("run_id", name="uq_shadow_issue_artifact_run"),
        sa.CheckConstraint("prediction_count > 0", name="ck_shadow_issue_artifact_count"),
    )
    _install_append_only_guards()


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in reversed(_TABLES):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON public.{table}")
    elif bind.dialect.name == "sqlite":
        for table in reversed(_TABLES):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_update")
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_delete")
    op.drop_table("shadow_issue_artifacts")
    op.drop_table("shadow_field_reviews")
