"""Add scoring-inert STRATHMARK shadow operation state.

Revision ID: z5d6e7f8a9b0
Revises: y4c5d6e7f8a9
Create Date: 2026-08-14

Existing marks, predicted times, completed results, rankings, points, and
payouts are not rewritten.  The new tables are outside the scoring engine.
"""

import uuid

import sqlalchemy as sa
from alembic import op


revision = "z5d6e7f8a9b0"
down_revision = "y4c5d6e7f8a9"
branch_labels = None
depends_on = None

BIG_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
_APPEND_ONLY_TABLES = (
    "shadow_lifecycle_transitions",
    "shadow_receipt_revisions",
    "shadow_context_observations",
    "shadow_outcome_revisions",
)


def _install_append_only_guards():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION public.reject_shadow_evidence_mutation()
            RETURNS trigger
            LANGUAGE plpgsql
            SET search_path = ''
            AS $$
            BEGIN
                RAISE EXCEPTION 'shadow evidence is append-only';
            END;
            $$
            """
        )
        for table in _APPEND_ONLY_TABLES:
            op.execute(
                f"""
                CREATE TRIGGER trg_{table}_append_only
                BEFORE UPDATE OR DELETE ON public.{table}
                FOR EACH ROW EXECUTE FUNCTION public.reject_shadow_evidence_mutation()
                """
            )
    elif bind.dialect.name == "sqlite":
        for table in _APPEND_ONLY_TABLES:
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


def _remove_append_only_guards():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in _APPEND_ONLY_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON public.{table}")
        op.execute("DROP FUNCTION IF EXISTS public.reject_shadow_evidence_mutation()")
    elif bind.dialect.name == "sqlite":
        for table in _APPEND_ONLY_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_update")
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only_delete")


def upgrade():
    op.add_column(
        "tournaments",
        sa.Column("shadow_tournament_id", sa.String(length=224), nullable=True),
    )
    tournaments = sa.table(
        "tournaments",
        sa.column("id", sa.Integer()),
        sa.column("shadow_tournament_id", sa.String(length=224)),
    )
    bind = op.get_bind()
    tournament_ids = bind.execute(
        sa.select(tournaments.c.id).order_by(tournaments.c.id)
    ).scalars().all()
    for tournament_id in tournament_ids:
        bind.execute(
            tournaments.update()
            .where(tournaments.c.id == tournament_id)
            .values(shadow_tournament_id=f"missoula:tournament:{uuid.uuid4()}")
        )
    with op.batch_alter_table("tournaments", schema=None) as batch_op:
        batch_op.alter_column(
            "shadow_tournament_id",
            existing_type=sa.String(length=224),
            nullable=False,
        )
        batch_op.create_unique_constraint(
            "uq_tournaments_shadow_tournament_id",
            ["shadow_tournament_id"],
        )

    op.add_column(
        "events",
        sa.Column("shadow_event_occurrence_id", sa.String(length=224), nullable=True),
    )
    events = sa.table(
        "events",
        sa.column("id", sa.Integer()),
        sa.column("shadow_event_occurrence_id", sa.String(length=224)),
    )
    event_ids = bind.execute(sa.select(events.c.id).order_by(events.c.id)).scalars().all()
    for event_id in event_ids:
        bind.execute(
            events.update()
            .where(events.c.id == event_id)
            .values(shadow_event_occurrence_id=f"missoula:event-occurrence:{uuid.uuid4()}")
        )
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.alter_column(
            "shadow_event_occurrence_id",
            existing_type=sa.String(length=224),
            nullable=False,
        )
        batch_op.create_unique_constraint(
            "uq_events_shadow_event_occurrence_id",
            ["shadow_event_occurrence_id"],
        )

    op.add_column("users", sa.Column("shadow_actor_id", sa.String(length=224), nullable=True))
    users = sa.table(
        "users",
        sa.column("id", sa.Integer()),
        sa.column("shadow_actor_id", sa.String(length=224)),
    )
    bind = op.get_bind()
    user_ids = bind.execute(sa.select(users.c.id).order_by(users.c.id)).scalars().all()
    for user_id in user_ids:
        bind.execute(
            users.update()
            .where(users.c.id == user_id)
            .values(shadow_actor_id=f"missoula:operator:{uuid.uuid4()}")
        )
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.alter_column(
            "shadow_actor_id",
            existing_type=sa.String(length=224),
            nullable=False,
        )
        batch_op.create_unique_constraint("uq_users_shadow_actor_id", ["shadow_actor_id"])

    op.add_column(
        "events",
        sa.Column(
            "handicap_authority_mode",
            sa.String(length=16),
            server_default=sa.text("'official'"),
            nullable=False,
        ),
    )
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.create_check_constraint(
            "ck_events_handicap_authority_mode",
            "handicap_authority_mode IN ('official', 'shadow')",
        )

    op.create_table(
        "competitor_external_identities",
        sa.Column("id", BIG_ID, autoincrement=True, nullable=False),
        sa.Column("competitor_uid", BIG_ID, nullable=False),
        sa.Column("namespace", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=224), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reviewed_by_id", sa.Integer(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('reviewed', 'conflict', 'retired')",
            name="ck_competitor_external_identity_status",
        ),
        sa.ForeignKeyConstraint(["competitor_uid"], ["competitors.uid"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewed_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "competitor_uid",
            "namespace",
            name="uq_competitor_external_identity_owner_namespace",
        ),
        sa.UniqueConstraint(
            "namespace",
            "external_id",
            name="uq_competitor_external_identity_namespace_id",
        ),
    )
    op.create_index(
        "ix_competitor_external_identity_external",
        "competitor_external_identities",
        ["namespace", "external_id"],
        unique=False,
    )

    op.create_table(
        "shadow_handicap_runs",
        sa.Column("id", BIG_ID, autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=224), nullable=False),
        sa.Column("request_id", sa.String(length=224), nullable=False),
        sa.Column("consumer_id", sa.String(length=224), nullable=False),
        sa.Column("tournament_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("event_occurrence_id", sa.String(length=224), nullable=False),
        sa.Column("field_run_id", sa.String(length=224), nullable=False),
        sa.Column("run_revision", sa.String(length=224), nullable=False),
        sa.Column("supersedes_run_id", BIG_ID, nullable=True),
        sa.Column("authority", sa.String(length=16), server_default=sa.text("'shadow'"), nullable=False),
        sa.Column("lifecycle", sa.String(length=32), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("lifecycle_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("prediction_as_of", sa.Date(), nullable=False),
        sa.Column("roster_fingerprint", sa.CHAR(length=64), nullable=False),
        sa.Column("schedule_fingerprint", sa.CHAR(length=64), nullable=False),
        sa.Column("wood_fingerprint", sa.CHAR(length=64), nullable=False),
        sa.Column("active_input_fingerprint", sa.CHAR(length=64), nullable=False),
        sa.Column("observation_schema_version", sa.String(length=80), nullable=False),
        sa.Column("observation_fingerprint", sa.CHAR(length=64), nullable=False),
        sa.Column("input_snapshot_json", sa.Text(), nullable=False),
        sa.Column("input_snapshot_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("reviewed_by_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("issued_by_id", sa.Integer(), nullable=True),
        sa.Column("issued_at", sa.DateTime(), nullable=True),
        sa.Column("supersession_reason_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("authority = 'shadow'", name="ck_shadow_run_authority"),
        sa.CheckConstraint(
            "lifecycle IN ('draft', 'prepared', 'preflight-approved', 'calculated', "
            "'reviewed', 'shadow-issued', 'outcomes-complete', 'superseded', 'cancelled')",
            name="ck_shadow_run_lifecycle",
        ),
        sa.CheckConstraint("lifecycle_version >= 1", name="ck_shadow_run_lifecycle_version"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["issued_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["reviewed_by_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["supersedes_run_id"], ["shadow_handicap_runs.id"]),
        sa.ForeignKeyConstraint(["tournament_id"], ["tournaments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("consumer_id", "request_id", name="uq_shadow_run_consumer_request"),
        sa.UniqueConstraint("run_id", name="uq_shadow_run_id"),
    )
    op.create_index("ix_shadow_run_event_created", "shadow_handicap_runs", ["event_id", "created_at"], unique=False)
    op.create_index("ix_shadow_run_field_revision", "shadow_handicap_runs", ["field_run_id", "run_revision"], unique=False)

    op.create_table(
        "shadow_lifecycle_transitions",
        sa.Column("id", BIG_ID, autoincrement=True, nullable=False),
        sa.Column("run_id", BIG_ID, nullable=False),
        sa.Column("from_lifecycle", sa.String(length=32), nullable=False),
        sa.Column("to_lifecycle", sa.String(length=32), nullable=False),
        sa.Column("run_version", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("run_version >= 2", name="ck_shadow_transition_run_version"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["shadow_handicap_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "run_version", name="uq_shadow_transition_run_version"),
    )

    op.create_table(
        "shadow_receipt_revisions",
        sa.Column("id", BIG_ID, autoincrement=True, nullable=False),
        sa.Column("run_id", BIG_ID, nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=80), nullable=False),
        sa.Column("core_json", sa.Text(), nullable=False),
        sa.Column("core_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("prediction_count", sa.Integer(), nullable=False),
        sa.Column("ledger_request_id", sa.String(length=224), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_shadow_receipt_revision"),
        sa.CheckConstraint("prediction_count >= 0", name="ck_shadow_receipt_prediction_count"),
        sa.ForeignKeyConstraint(["run_id"], ["shadow_handicap_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ledger_request_id", name="uq_shadow_receipt_ledger_request"),
        sa.UniqueConstraint("run_id", "revision", name="uq_shadow_receipt_run_revision"),
    )

    op.create_table(
        "shadow_context_observations",
        sa.Column("id", BIG_ID, autoincrement=True, nullable=False),
        sa.Column("observation_id", sa.String(length=224), nullable=False),
        sa.Column("run_id", BIG_ID, nullable=False),
        sa.Column("schema_version", sa.String(length=80), nullable=False),
        sa.Column("subject_type", sa.String(length=40), nullable=False),
        sa.Column("subject_id", sa.String(length=224), nullable=False),
        sa.Column("factor", sa.String(length=64), nullable=False),
        sa.Column("value_state", sa.String(length=16), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("corrects_observation_id", BIG_ID, nullable=True),
        sa.Column("formula", sa.String(length=200), nullable=True),
        sa.Column("source_record_ids_json", sa.Text(), nullable=True),
        sa.CheckConstraint("value_state IN ('known', 'unknown')", name="ck_shadow_context_value_state"),
        sa.CheckConstraint(
            "(value_state = 'unknown' AND value_json IS NULL) OR "
            "(value_state = 'known' AND value_json IS NOT NULL)",
            name="ck_shadow_context_value_presence",
        ),
        sa.CheckConstraint(
            "source IN ('imported', 'operator_entered', 'system_recorded', "
            "'measured', 'scanned', 'derived')",
            name="ck_shadow_context_source",
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["corrects_observation_id"], ["shadow_context_observations.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["shadow_handicap_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("observation_id", name="uq_shadow_context_observation_id"),
    )
    op.create_index("ix_shadow_context_run_factor", "shadow_context_observations", ["run_id", "factor"], unique=False)

    op.create_table(
        "shadow_outcome_revisions",
        sa.Column("id", BIG_ID, autoincrement=True, nullable=False),
        sa.Column("outcome_revision_id", sa.String(length=224), nullable=False),
        sa.Column("run_id", BIG_ID, nullable=False),
        sa.Column("event_result_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("supersedes_outcome_revision_id", BIG_ID, nullable=True),
        sa.Column("classification", sa.String(length=32), nullable=False),
        sa.Column("raw_elapsed_seconds", sa.Float(), nullable=True),
        sa.Column("official_value", sa.Float(), nullable=True),
        sa.Column("penalty_applied", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_shadow_outcome_revision"),
        sa.CheckConstraint(
            "classification IN ('valid_finish', 'dns', 'scratch', 'dnf', 'dq', "
            "'penalty', 'rerun', 'no_contest', 'timing_failure')",
            name="ck_shadow_outcome_classification",
        ),
        sa.CheckConstraint("raw_elapsed_seconds IS NULL OR raw_elapsed_seconds > 0", name="ck_shadow_outcome_raw_positive"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["event_result_id"], ["event_results.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["shadow_handicap_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["supersedes_outcome_revision_id"], ["shadow_outcome_revisions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("outcome_revision_id", name="uq_shadow_outcome_revision_id"),
        sa.UniqueConstraint("run_id", "event_result_id", "revision", name="uq_shadow_outcome_result_revision"),
    )
    op.create_index("ix_shadow_outcome_run_result", "shadow_outcome_revisions", ["run_id", "event_result_id"], unique=False)

    op.create_table(
        "shadow_settlement_outbox",
        sa.Column("id", BIG_ID, autoincrement=True, nullable=False),
        sa.Column("outbox_id", sa.String(length=224), nullable=False),
        sa.Column("run_id", BIG_ID, nullable=False),
        sa.Column("outcome_revision_id", sa.String(length=224), nullable=False),
        sa.Column("schema_version", sa.String(length=80), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("delivery_status", sa.String(length=24), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("action IN ('settle', 'void')", name="ck_shadow_outbox_action"),
        sa.CheckConstraint(
            "delivery_status IN ('pending', 'recorded', 'retryable-failed')",
            name="ck_shadow_outbox_delivery_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_shadow_outbox_attempt_count"),
        sa.ForeignKeyConstraint(["outcome_revision_id"], ["shadow_outcome_revisions.outcome_revision_id"]),
        sa.ForeignKeyConstraint(["run_id"], ["shadow_handicap_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("outbox_id", name="uq_shadow_settlement_outbox_id"),
        sa.UniqueConstraint("outcome_revision_id", name="uq_shadow_settlement_outbox_outcome_revision"),
    )
    op.create_index("ix_shadow_outbox_status_next", "shadow_settlement_outbox", ["delivery_status", "next_attempt_at"], unique=False)

    _install_append_only_guards()


def downgrade():
    _remove_append_only_guards()
    op.drop_index("ix_shadow_outbox_status_next", table_name="shadow_settlement_outbox")
    op.drop_table("shadow_settlement_outbox")
    op.drop_index("ix_shadow_outcome_run_result", table_name="shadow_outcome_revisions")
    op.drop_table("shadow_outcome_revisions")
    op.drop_index("ix_shadow_context_run_factor", table_name="shadow_context_observations")
    op.drop_table("shadow_context_observations")
    op.drop_table("shadow_receipt_revisions")
    op.drop_table("shadow_lifecycle_transitions")
    op.drop_index("ix_shadow_run_field_revision", table_name="shadow_handicap_runs")
    op.drop_index("ix_shadow_run_event_created", table_name="shadow_handicap_runs")
    op.drop_table("shadow_handicap_runs")
    op.drop_index("ix_competitor_external_identity_external", table_name="competitor_external_identities")
    op.drop_table("competitor_external_identities")
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_constraint("ck_events_handicap_authority_mode", type_="check")
        batch_op.drop_column("handicap_authority_mode")
        batch_op.drop_constraint("uq_events_shadow_event_occurrence_id", type_="unique")
        batch_op.drop_column("shadow_event_occurrence_id")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_constraint("uq_users_shadow_actor_id", type_="unique")
        batch_op.drop_column("shadow_actor_id")
    with op.batch_alter_table("tournaments", schema=None) as batch_op:
        batch_op.drop_constraint("uq_tournaments_shadow_tournament_id", type_="unique")
        batch_op.drop_column("shadow_tournament_id")
