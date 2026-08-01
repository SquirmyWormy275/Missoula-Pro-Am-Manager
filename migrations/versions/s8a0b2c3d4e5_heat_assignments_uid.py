"""Put a real competitor reference on heat_assignments (D12-C, commit A).

Revision ID: s8a0b2c3d4e5
Revises: r7f8a0b2c3d4
Create Date: 2026-08-01

PG-safety: no batch_alter_table except around `alter_column` /
`create_unique_constraint` / `create_foreign_key`, which is the exemption
`tests/test_pg_migration_safety.py::_BATCH_REQUIRED_OPS` grants. No Boolean
server_default. No SQLite-only introspection statements.

What this migration does
========================
1. Adds `uid` to `heat_assignments`, nullable.
2. Backfills it from `(competitor_id, competitor_type)` through
   `college_competitors.uid` / `pro_competitors.uid`.
3. Guards: refuses to continue if any row is still NULL, or if any heat would
   end up with the same competitor twice.
4. Promotes `uid` to NOT NULL, adds the foreign key to `competitors.uid`, adds
   a unique constraint on `(heat_id, uid)`, and indexes `uid`.

It drops nothing. `competitor_id` and `competitor_type` still hold their values
when this migration finishes and are still what `sync_assignments` writes.
D12-C phase 2 reverses the direction of authority; this revision only makes the
table capable of holding it.

Why the pair, and never the id alone
====================================
`heat_assignments.competitor_id` has never had a foreign key. It is a bare
integer, and `competitor_type` is an unconstrained VARCHAR(20). The pro and
college id sequences overlap, so the integer on its own does not identify a
human: on the pre-reseed pristine mirror, 188 of 379 assignment rows carry a
`competitor_id` that exists in BOTH pools. Half the table is pointed at the
right person only by a string column nothing was checking.

That is the same failure mode as the era-1 ghost references G1-C just repaired,
one table over. The backfill below is therefore keyed on the pair every time,
and the guard is what proves the pair was sufficient rather than assuming it.

Census taken before writing this, across all four parity mirrors (p0, p0rev,
mt, 2026pristine): 379 rows each, zero college orphans, zero pro orphans, zero
`competitor_type` values outside {'college','pro'}, zero NULL `competitor_id`,
zero duplicate `(heat_id, competitor_id, competitor_type)` triples, and zero
rows whose `competitor_type` disagrees with the parent event's `event_type`.
The `heats.competitors` JSON carries no duplicate ids and no non-integer ids on
any mirror. The backfill has no ambiguity to resolve on real data. The guard
exists for the databases this migration has not seen.

Why a unique constraint on (heat_id, uid) and not on the legacy pair
====================================================================
The pair is exactly as unique as the uid is, since a uid belongs to one row of
one pool. Constraining the uid says the thing that is actually true: a
competitor appears in a heat once. Constraining the pair would restate it in
the vocabulary D12-C is retiring.

This is a real behaviour change and it is worth naming. Today, JSON holding the
same competitor twice produces two rows and `sync_assignments` reports the heat
as drifted forever, which is the condition its sorted-list comparison was
written to keep visible. After this revision that state cannot reach the
database. `Heat.sync_assignments` refuses it in Python first, with a named
error, so the failure says which heat and which competitor instead of arriving
as a bare IntegrityError from the driver.

Why the index on uid is separate
================================
`(heat_id, uid)` serves lookups that start from a heat. The question D12-C
phase 2 asks constantly is the other one, "every heat this competitor is in",
which a composite index led by `heat_id` cannot answer without a scan.

Downgrade
=========
Drops the index, the two constraints and the column. `competitor_id` and
`competitor_type` were never touched, so the table returns to exactly the shape
it had, and a downgrade followed by an upgrade is lossless.
"""
import sqlalchemy as sa
from alembic import op

revision = "s8a0b2c3d4e5"
down_revision = "r7f8a0b2c3d4"
branch_labels = None
depends_on = None


# Must match models/_types.py exactly or tests/test_migration_integrity.py
# reports a type mismatch between the create_all schema and the upgrade schema.
BIG_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


_UNRESOLVED_SQL = """
    SELECT
        SUM(CASE WHEN competitor_type = 'college' THEN 1 ELSE 0 END)
            AS college_unresolved,
        SUM(CASE WHEN competitor_type = 'pro' THEN 1 ELSE 0 END)
            AS pro_unresolved,
        SUM(CASE WHEN competitor_type NOT IN ('college', 'pro') THEN 1 ELSE 0 END)
            AS bad_type
    FROM heat_assignments
    WHERE uid IS NULL
"""

# A duplicate uid inside one heat is the only thing that can make the unique
# constraint fail, and it can only arise from a duplicated
# (competitor_id, competitor_type) pair, since a uid belongs to one competitor
# row.  Counting heats, not rows: the operator needs to know how many places to
# look at.
_DUPLICATE_SQL = """
    SELECT COUNT(*) AS dup_heats FROM (
        SELECT heat_id, uid
        FROM heat_assignments
        WHERE uid IS NOT NULL
        GROUP BY heat_id, uid
        HAVING COUNT(*) > 1
    ) AS d
"""


def _guard(connection):
    """Refuse to promote the column if the backfill did not fully land.

    Reports counts only.  These rows point at real competitors and a migration
    log is not a place to put competitor identifiers.
    """
    problems = []

    row = connection.execute(sa.text(_UNRESOLVED_SQL)).mappings().first()
    if row is not None:
        for label, key in (
            ("college assignment rows whose competitor_id is in no college "
             "competitor row", "college_unresolved"),
            ("pro assignment rows whose competitor_id is in no pro competitor "
             "row", "pro_unresolved"),
            ("assignment rows whose competitor_type is neither 'college' nor "
             "'pro'", "bad_type"),
        ):
            count = row[key] or 0
            if count:
                problems.append(f"  {count} {label}")

    row = connection.execute(sa.text(_DUPLICATE_SQL)).mappings().first()
    if row is not None:
        count = row["dup_heats"] or 0
        if count:
            problems.append(
                f"  {count} heats holding the same competitor more than once"
            )

    if problems:
        raise RuntimeError(
            "s8a0b2c3d4e5 refused to put a foreign key on heat_assignments: "
            "the rows below do not name a competitor that exists, or name one "
            "twice in the same heat.\n"
            + "\n".join(problems)
            + "\n\nRun `python scripts/audit_competitor_references.py` for the "
            "detail, repair the rows, and upgrade again. Both engines this "
            "chain runs on have transactional DDL, so the whole revision rolls "
            "back and the column this raise interrupted is not there either."
        )


def upgrade():
    op.add_column("heat_assignments", sa.Column("uid", BIG_ID, nullable=True))

    connection = op.get_bind()

    # Correlated subqueries rather than UPDATE ... FROM: the latter is
    # PostgreSQL syntax that SQLite only learned in 3.33, and this chain has to
    # replay on whatever SQLite a developer happens to have.  Speed is
    # irrelevant, the production table holds 379 rows.
    connection.execute(sa.text("""
        UPDATE heat_assignments SET
            uid = (SELECT c.uid FROM college_competitors c
                    WHERE c.id = heat_assignments.competitor_id)
        WHERE competitor_type = 'college'
          AND EXISTS (SELECT 1 FROM college_competitors c
                       WHERE c.id = heat_assignments.competitor_id)
    """))

    connection.execute(sa.text("""
        UPDATE heat_assignments SET
            uid = (SELECT p.uid FROM pro_competitors p
                    WHERE p.id = heat_assignments.competitor_id)
        WHERE competitor_type = 'pro'
          AND EXISTS (SELECT 1 FROM pro_competitors p
                       WHERE p.id = heat_assignments.competitor_id)
    """))

    _guard(connection)

    with op.batch_alter_table("heat_assignments", schema=None) as batch_op:
        batch_op.alter_column("uid", existing_type=BIG_ID, nullable=False)
        batch_op.create_unique_constraint(
            "uq_heat_assignments_heat_uid", ["heat_id", "uid"]
        )
        batch_op.create_foreign_key(
            "fk_heat_assignments_uid", "competitors", ["uid"], ["uid"]
        )

    op.create_index("ix_heat_assignments_uid", "heat_assignments", ["uid"])


def downgrade():
    op.drop_index("ix_heat_assignments_uid", table_name="heat_assignments")

    with op.batch_alter_table("heat_assignments", schema=None) as batch_op:
        batch_op.drop_constraint("fk_heat_assignments_uid", type_="foreignkey")
        batch_op.drop_constraint("uq_heat_assignments_heat_uid", type_="unique")

    op.drop_column("heat_assignments", "uid")
