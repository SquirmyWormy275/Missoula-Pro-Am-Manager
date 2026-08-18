"""Repair shipped shadow schema drift and freeze delivery authority.

Revision ID: c8a9b0c1d2e3
Revises: b7f8a9b0c1d2
Create Date: 2026-08-17

The a5/a6 revisions were already published before model-side timestamp
defaults and ``context_version`` were introduced.  This forward revision is
therefore the only safe place to make fresh databases and databases already
stamped at b7 converge on the same schema.
"""

import uuid

import sqlalchemy as sa
from alembic import op


revision = "c8a9b0c1d2e3"
down_revision = "b7f8a9b0c1d2"
branch_labels = None
depends_on = None

_LEDGER_NAMESPACE = uuid.UUID("2f08f564-cae9-54bf-b488-7d5a19831f80")

_TIMESTAMP_DEFAULT_COLUMNS = {
    "competitor_external_identities": ("reviewed_at", "created_at"),
    "shadow_handicap_runs": ("created_at", "updated_at"),
    "shadow_lifecycle_transitions": ("created_at",),
    "shadow_receipt_revisions": ("received_at",),
    "shadow_context_observations": ("captured_at",),
    "shadow_outcome_revisions": ("created_at",),
    "shadow_settlement_outbox": ("created_at",),
    "shadow_field_reviews": ("created_at",),
    "shadow_issue_artifacts": ("created_at",),
}

_APPEND_ONLY_TABLES = (
    "shadow_lifecycle_transitions",
    "shadow_receipt_revisions",
    "shadow_context_observations",
    "shadow_outcome_revisions",
    "shadow_field_reviews",
    "shadow_issue_artifacts",
)


def _alter_timestamp_defaults(server_default):
    bind = op.get_bind()
    effective_default = server_default
    if server_default is not None and bind.dialect.name == "postgresql":
        # These legacy DateTime columns store naive UTC.  Do not let a
        # connection-specific PostgreSQL timezone change their meaning.
        effective_default = sa.text("timezone('utc', CURRENT_TIMESTAMP)")
    for table_name, column_names in _TIMESTAMP_DEFAULT_COLUMNS.items():
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table(table_name, schema=None) as batch_op:
                for column_name in column_names:
                    batch_op.alter_column(
                        column_name,
                        existing_type=sa.DateTime(),
                        existing_nullable=False,
                        server_default=effective_default,
                    )
        else:
            for column_name in column_names:
                op.alter_column(
                    table_name,
                    column_name,
                    existing_type=sa.DateTime(),
                    existing_nullable=False,
                    server_default=effective_default,
                )


def _drop_sqlite_append_only_guard(table_name):
    op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_append_only_update")
    op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_append_only_delete")


def _restore_sqlite_append_only_guard(table_name):
    _drop_sqlite_append_only_guard(table_name)
    op.execute(
        f"""
            CREATE TRIGGER trg_{table_name}_append_only_update
            BEFORE UPDATE ON {table_name}
            BEGIN
                SELECT RAISE(ABORT, 'shadow evidence is append-only');
            END
            """
    )
    op.execute(
        f"""
            CREATE TRIGGER trg_{table_name}_append_only_delete
            BEFORE DELETE ON {table_name}
            BEGIN
                SELECT RAISE(ABORT, 'shadow evidence is append-only');
            END
            """
    )


def _restore_sqlite_append_only_guards():
    if op.get_bind().dialect.name != "sqlite":
        return
    for table_name in _APPEND_ONLY_TABLES:
        _restore_sqlite_append_only_guard(table_name)


def _rewrite_legacy_ledger_request_ids():
    """Converge shipped receipt rows on STRATHMARK's raw UUID identity."""
    bind = op.get_bind()
    dialect = bind.dialect.name
    table_name = "shadow_receipt_revisions"
    if dialect == "postgresql":
        op.execute(
            "ALTER TABLE public.shadow_receipt_revisions "
            "DISABLE TRIGGER trg_shadow_receipt_revisions_append_only"
        )
    elif dialect == "sqlite":
        _drop_sqlite_append_only_guard(table_name)

    receipts = sa.table(
        table_name,
        sa.column("id", sa.BigInteger()),
        sa.column("run_id", sa.BigInteger()),
        sa.column("ledger_request_id", sa.String(length=224)),
    )
    runs = sa.table(
        "shadow_handicap_runs",
        sa.column("id", sa.BigInteger()),
        sa.column("consumer_id", sa.String(length=224)),
        sa.column("request_id", sa.String(length=224)),
    )
    try:
        rows = bind.execute(
            sa.select(
                receipts.c.id,
                runs.c.consumer_id,
                runs.c.request_id,
            ).select_from(receipts.join(runs, receipts.c.run_id == runs.c.id))
        ).all()
        rewrites = []
        for receipt_id, consumer_id, request_id in rows:
            ledger_request_id = str(
                uuid.uuid5(
                    _LEDGER_NAMESPACE,
                    f"request:{consumer_id}:{request_id}",
                )
            )
            rewrites.append((receipt_id, consumer_id, request_id, ledger_request_id))

        final_ids = [rewrite[3] for rewrite in rewrites]
        if len(final_ids) != len(set(final_ids)):
            raise RuntimeError(
                "shadow receipt history contains multiple rows for one ledger request"
            )

        # Move every row through a receipt-specific UUID before assigning the
        # final UUIDs.  That avoids a transient unique-key collision if a
        # legacy row happens to hold another row's correct final value.
        for receipt_id, consumer_id, request_id, _ledger_request_id in rewrites:
            staging_id = str(
                uuid.uuid5(
                    _LEDGER_NAMESPACE,
                    f"c8-staging:{receipt_id}:{consumer_id}:{request_id}",
                )
            )
            bind.execute(
                receipts.update()
                .where(receipts.c.id == receipt_id)
                .values(ledger_request_id=staging_id)
            )
        for receipt_id, _consumer_id, _request_id, ledger_request_id in rewrites:
            bind.execute(
                receipts.update()
                .where(receipts.c.id == receipt_id)
                .values(ledger_request_id=ledger_request_id)
            )
    finally:
        if dialect == "postgresql":
            op.execute(
                "ALTER TABLE public.shadow_receipt_revisions "
                "ENABLE TRIGGER trg_shadow_receipt_revisions_append_only"
            )
        elif dialect == "sqlite":
            _restore_sqlite_append_only_guard(table_name)


def _create_context_version_constraint():
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("shadow_handicap_runs", schema=None) as batch_op:
            batch_op.create_check_constraint(
                "ck_shadow_run_context_version",
                "context_version >= 0",
            )
    else:
        op.create_check_constraint(
            "ck_shadow_run_context_version",
            "shadow_handicap_runs",
            "context_version >= 0",
        )


def _drop_context_version_column():
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("shadow_handicap_runs", schema=None) as batch_op:
            batch_op.drop_constraint("ck_shadow_run_context_version", type_="check")
            batch_op.drop_column("context_version")
    else:
        op.drop_constraint(
            "ck_shadow_run_context_version",
            "shadow_handicap_runs",
            type_="check",
        )
        op.drop_column("shadow_handicap_runs", "context_version")


def _create_delivery_actor_foreign_key():
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("shadow_settlement_outbox", schema=None) as batch_op:
            batch_op.create_foreign_key(
                "fk_shadow_settlement_outbox_delivery_actor",
                "users",
                ["delivery_actor_id"],
                ["id"],
            )
    else:
        op.create_foreign_key(
            "fk_shadow_settlement_outbox_delivery_actor",
            "shadow_settlement_outbox",
            "users",
            ["delivery_actor_id"],
            ["id"],
        )


def _drop_delivery_actor_column():
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("shadow_settlement_outbox", schema=None) as batch_op:
            batch_op.drop_constraint(
                "fk_shadow_settlement_outbox_delivery_actor",
                type_="foreignkey",
            )
            batch_op.drop_column("delivery_actor_id")
    else:
        op.drop_constraint(
            "fk_shadow_settlement_outbox_delivery_actor",
            "shadow_settlement_outbox",
            type_="foreignkey",
        )
        op.drop_column("shadow_settlement_outbox", "delivery_actor_id")


def upgrade():
    op.add_column(
        "shadow_handicap_runs",
        sa.Column(
            "context_version",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    _create_context_version_constraint()

    _alter_timestamp_defaults(sa.func.now())

    op.add_column(
        "shadow_settlement_outbox",
        # Expand phase: the prior b7 writer does not supply this column.  The
        # runtime always freezes it and delivery rejects NULL, but the schema
        # stays nullable until a later contract migration retires b7 code.
        sa.Column("delivery_actor_id", sa.Integer(), nullable=True),
    )
    op.execute(
        """
        UPDATE shadow_settlement_outbox
        SET delivery_actor_id = actor_id
        WHERE delivery_actor_id IS NULL
        """
    )
    _create_delivery_actor_foreign_key()

    _rewrite_legacy_ledger_request_ids()

    # SQLite batch table recreation drops the append-only triggers installed
    # by a5/a6.  Restore them after every affected table has converged.
    _restore_sqlite_append_only_guards()


def downgrade():
    _drop_delivery_actor_column()

    _alter_timestamp_defaults(None)

    _drop_context_version_column()

    _restore_sqlite_append_only_guards()
