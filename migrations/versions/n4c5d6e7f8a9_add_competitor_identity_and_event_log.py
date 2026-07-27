"""Add the competitor identity table and the append-only tournament event log.

Revision ID: n4c5d6e7f8a9
Revises: m3b4c5d6e7f8
Create Date: 2026-07-27

PG-safety: no batch_alter_table except around `alter_column` /
`create_unique_constraint` / `create_foreign_key`, which is the exemption
`tests/test_pg_migration_safety.py::_BATCH_REQUIRED_OPS` grants. No Boolean
server_default. No SQLite-only introspection statements. Both new tables are
created with direct `op.create_table`.

(That sentence is deliberately worded around the SQLite introspection keyword.
`tests/test_pg_migration_safety.py::TestNoSqlitePragma` greps the full file
source and only skips lines beginning with `#`, so naming the keyword inside
this docstring fails the scan. Prose is not a valid reason to add an allowlist
entry.)

What this migration does
========================
1. Creates `competitors`: one row per competitor, college or pro, carrying the
   single cross-discipline `uid`.
2. Adds `uid` to `college_competitors` and `pro_competitors`, nullable.
3. Backfills one identity row per existing competitor and writes the `uid` back.
4. Promotes `uid` to NOT NULL + UNIQUE + FOREIGN KEY.
5. Creates `tournament_event`, the append-only intent log.

Nothing in the application reads or writes either new table in this phase. The
one behavioural change is the mapper-level allocator in
`models/competitor_identity.py`, which fills `uid` on every future insert.

Why batch_alter_table in step 4
===============================
SQLite cannot `ALTER TABLE ... ALTER COLUMN`, cannot add a UNIQUE constraint to
an existing table, and cannot add a FOREIGN KEY to an existing table. All three
require Alembic batch mode, which on SQLite does the table-rebuild dance and on
PostgreSQL emits direct `ALTER TABLE` DDL. Same precedent and same reasoning as
`f0a1b2c3d4e6_points_columns_to_numeric.py`.

CHECK constraints across the SQLite batch rebuild
=================================================
`college_competitors` carries 3 CHECK constraints and `pro_competitors` carries
4, all added by `f5a6b7c8d9e0`. No parity test in this repo compares CHECK
constraints, so a rebuild that silently dropped them would leave CI green while
the database stopped enforcing gender, status, and the non-negative money
invariants.

It does not drop them. Under SQLAlchemy 2.0.23 / Alembic 1.18.5 the SQLite
dialect reflects named CHECK constraints and batch mode carries them into the
rebuilt table. This was measured directly against `sqlite_master`, not assumed.
Passing them again via `table_args` was tried first and is WRONG: it emits every
CHECK twice, because reflection has already supplied them.

`tests/test_identity_migration.py` asserts the exact counts so a future
SQLAlchemy upgrade that changes this reflection behaviour fails loudly instead
of silently disarming the constraints.

Why the backfill inserts row by row
===================================
An `INSERT ... SELECT` into `competitors` cannot report which generated uid
belongs to which source row, and there is no portable `INSERT ... RETURNING`
that both dialects consume the same way through Alembic. Executing one insert
per competitor lets `inserted_primary_key` do the mapping on PostgreSQL
(RETURNING) and on SQLite (lastrowid) alike. The production mirror holds 64
college and 49 pro rows, so this is 113 inserts plus 113 updates.

Re-running after a partial failure cannot double-allocate: every statement is
scoped by `WHERE uid IS NULL`, so a row that already has an identity is skipped.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "n4c5d6e7f8a9"
down_revision = "m3b4c5d6e7f8"
branch_labels = None
depends_on = None


# Must match models/_types.py exactly or tests/test_migration_integrity.py
# reports a type mismatch between the create_all schema and the upgrade schema.
BIG_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
JSON_PAYLOAD = postgresql.JSONB().with_variant(sa.Text(), "sqlite")

def _identity_table():
    """Core handle on `competitors` for the backfill.

    This is a full `sa.Table` with `primary_key=True` on `uid`, not the
    lightweight `sa.table()` / `sa.column()` form. That distinction is load
    bearing and was found by measurement, not by reading: the lightweight form
    carries no primary key, so SQLAlchemy does not append a RETURNING clause on
    PostgreSQL and `result.inserted_primary_key` comes back as an empty tuple.
    The first version of this migration used `sa.table()`, passed on SQLite
    (where the value is recovered from `cursor.lastrowid` regardless), and died
    with `IndexError: tuple index out of range` on the first row of the
    PostgreSQL backfill. Declaring the primary key makes SQLAlchemy emit
    RETURNING on PG and keep using lastrowid on SQLite, so one code path serves
    both dialects.
    """
    return sa.Table(
        "competitors",
        sa.MetaData(),
        sa.Column("uid", BIG_ID, primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(16)),
        sa.Column("tournament_id", sa.Integer),
        sa.Column("created_at", sa.DateTime),
    )


def _backfill(connection, source_table, kind):
    """Allocate one identity per un-identified row in `source_table`."""
    identity = _identity_table()
    now = sa.func.now()

    rows = connection.execute(
        sa.text(
            f"SELECT id, tournament_id FROM {source_table} "
            f"WHERE uid IS NULL ORDER BY id"
        )
    ).fetchall()

    for row_id, tournament_id in rows:
        result = connection.execute(
            identity.insert().values(
                kind=kind,
                tournament_id=tournament_id,
                created_at=now,
            )
        )
        new_uid = result.inserted_primary_key[0]
        connection.execute(
            sa.text(
                f"UPDATE {source_table} SET uid = :uid "
                f"WHERE id = :row_id AND uid IS NULL"
            ),
            {"uid": new_uid, "row_id": row_id},
        )


def upgrade():
    # ------------------------------------------------------------------
    # 1. The identity table.
    # ------------------------------------------------------------------
    op.create_table(
        "competitors",
        sa.Column("uid", BIG_ID, autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("tournament_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("kind IN ('college', 'pro')", name="ck_competitors_kind_valid"),
        sa.ForeignKeyConstraint(["tournament_id"], ["tournaments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("uid"),
    )
    op.create_index("ix_competitors_tournament_id", "competitors", ["tournament_id"], unique=False)

    # ------------------------------------------------------------------
    # 2. Nullable uid on both competitor tables.
    # ------------------------------------------------------------------
    op.add_column("college_competitors", sa.Column("uid", BIG_ID, nullable=True))
    op.add_column("pro_competitors", sa.Column("uid", BIG_ID, nullable=True))

    # ------------------------------------------------------------------
    # 3. Backfill.
    # ------------------------------------------------------------------
    connection = op.get_bind()
    _backfill(connection, "college_competitors", "college")
    _backfill(connection, "pro_competitors", "pro")

    # ------------------------------------------------------------------
    # 4. Promote to NOT NULL + UNIQUE + FOREIGN KEY.
    # ------------------------------------------------------------------
    with op.batch_alter_table("college_competitors", schema=None) as batch_op:
        batch_op.alter_column("uid", existing_type=BIG_ID, nullable=False)
        batch_op.create_unique_constraint("uq_college_competitors_uid", ["uid"])
        batch_op.create_foreign_key(
            "fk_college_competitors_uid", "competitors", ["uid"], ["uid"]
        )

    with op.batch_alter_table("pro_competitors", schema=None) as batch_op:
        batch_op.alter_column("uid", existing_type=BIG_ID, nullable=False)
        batch_op.create_unique_constraint("uq_pro_competitors_uid", ["uid"])
        batch_op.create_foreign_key(
            "fk_pro_competitors_uid", "competitors", ["uid"], ["uid"]
        )

    # ------------------------------------------------------------------
    # 5. The append-only intent log.
    # ------------------------------------------------------------------
    op.create_table(
        "tournament_event",
        sa.Column("id", BIG_ID, autoincrement=True, nullable=False),
        sa.Column("tournament_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("payload", JSON_PAYLOAD, nullable=False),
        sa.Column("parent_seq", sa.BigInteger(), nullable=True),
        sa.Column("state_hash", sa.CHAR(length=64), nullable=True),
        sa.Column("schema_version", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["tournament_id"], ["tournaments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tournament_id", "seq", name="uq_tournament_event_tournament_seq"),
    )


def downgrade():
    op.drop_table("tournament_event")

    with op.batch_alter_table("pro_competitors", schema=None) as batch_op:
        batch_op.drop_constraint("fk_pro_competitors_uid", type_="foreignkey")
        batch_op.drop_constraint("uq_pro_competitors_uid", type_="unique")

    with op.batch_alter_table("college_competitors", schema=None) as batch_op:
        batch_op.drop_constraint("fk_college_competitors_uid", type_="foreignkey")
        batch_op.drop_constraint("uq_college_competitors_uid", type_="unique")

    op.drop_column("pro_competitors", "uid")
    op.drop_column("college_competitors", "uid")

    op.drop_index("ix_competitors_tournament_id", table_name="competitors")
    op.drop_table("competitors")
