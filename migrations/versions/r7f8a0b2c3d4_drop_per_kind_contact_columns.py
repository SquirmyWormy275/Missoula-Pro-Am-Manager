"""Drop the per-kind contact columns (G2-C, commit B).

Revision ID: r7f8a0b2c3d4
Revises: q6e7f8a0b2c3
Create Date: 2026-08-01

The other half of ``q6e7f8a0b2c3``.  That revision added
``address``/``phone``/``email``/``phone_opted_in`` to ``competitors`` and
backfilled them, and its docstring promised the columns it copied from would be
dropped here, "and only after a guard proves the spine holds everything they
did".  This is that guard, and that drop.

Columns dropped
===============
``pro_competitors``: ``address``, ``phone``, ``email``, ``phone_opted_in``.
``college_competitors``: ``phone_opted_in``.  College never had the three text
columns; the pro table was the only place contact ever lived, which is the
asymmetry G2-C existed to remove.

None of the five carry an index or a constraint, verified against the
production mirror, so there is nothing to drop alongside them.

Why ``op.drop_column`` and not ``batch_alter_table``
====================================================
``tests/test_pg_migration_safety.py::TestNoBatchAlterTableInUpgrades`` fails any
``batch_alter_table`` block that does not contain a constraint operation, and a
bare column drop is not one.  Direct ``op.drop_column`` is therefore the only
shape that passes, and it is also the correct one: production is Postgres as of
D14-B, and SQLite has supported ``ALTER TABLE DROP COLUMN`` natively since
3.35 (this tree runs 3.45).  A column that is indexed or referenced would break
that on SQLite; none of these five are.

What the guard checks, and what it deliberately does not
========================================================
It checks for *loss*: a per-kind column that holds a value where the spine holds
nothing.  That is the only failure ``q6e7f8a0b2c3``'s backfill could have
produced, and it is the only condition under which dropping these columns
destroys information.

It does not check for *difference*.  Since commit A the association proxies on
``ProCompetitor``/``CollegeCompetitor`` write to the spine and nothing writes to
these columns any more, so on any database that has been serving traffic between
the two migrations the per-kind columns are a stale mirror by design.  A phone
number edited after commit A reads new on the spine and old here, and failing on
that would block the deployment for doing exactly what it was built to do.

The guard reports counts only, never values.  These columns hold real competitor
contact details and a migration log is not a place to put them.

Downgrade
=========
Re-adds the five columns and refills them from the spine, which is the truth by
this point.  A downgrade followed by an upgrade is therefore lossless.
"""
import sqlalchemy as sa
from alembic import op

revision = "r7f8a0b2c3d4"
down_revision = "q6e7f8a0b2c3"
branch_labels = None
depends_on = None


_PRO_LOSS_SQL = """
SELECT
    SUM(CASE WHEN c.uid IS NULL THEN 1 ELSE 0 END)                 AS no_spine,
    SUM(CASE WHEN p.address IS NOT NULL AND p.address <> ''
              AND (c.address IS NULL OR c.address = '')
             THEN 1 ELSE 0 END)                                    AS lost_address,
    SUM(CASE WHEN p.phone IS NOT NULL AND p.phone <> ''
              AND (c.phone IS NULL OR c.phone = '')
             THEN 1 ELSE 0 END)                                    AS lost_phone,
    SUM(CASE WHEN p.email IS NOT NULL AND p.email <> ''
              AND (c.email IS NULL OR c.email = '')
             THEN 1 ELSE 0 END)                                    AS lost_email,
    SUM(CASE WHEN p.phone_opted_in AND NOT c.phone_opted_in
             THEN 1 ELSE 0 END)                                    AS lost_optin
FROM pro_competitors p
LEFT JOIN competitors c ON c.uid = p.uid
"""

_COLLEGE_LOSS_SQL = """
SELECT
    SUM(CASE WHEN c.uid IS NULL THEN 1 ELSE 0 END)                 AS no_spine,
    SUM(CASE WHEN cc.phone_opted_in AND NOT c.phone_opted_in
             THEN 1 ELSE 0 END)                                    AS lost_optin
FROM college_competitors cc
LEFT JOIN competitors c ON c.uid = cc.uid
"""


def _guard(connection):
    """Refuse to drop anything the spine does not already hold.

    Postgres has transactional DDL, so raising here leaves the schema exactly as
    it was.  On SQLite the drops have not run yet either: the guard is the first
    thing ``upgrade`` does.
    """
    problems = []

    row = connection.execute(sa.text(_PRO_LOSS_SQL)).mappings().first()
    if row is not None:
        for label, key in (
            ("pro_competitors rows with no identity row", "no_spine"),
            ("pro addresses on the table but not the spine", "lost_address"),
            ("pro phones on the table but not the spine", "lost_phone"),
            ("pro emails on the table but not the spine", "lost_email"),
            ("pro opt-ins true on the table but false on the spine", "lost_optin"),
        ):
            count = row[key] or 0
            if count:
                problems.append(f"  {count} {label}")

    row = connection.execute(sa.text(_COLLEGE_LOSS_SQL)).mappings().first()
    if row is not None:
        for label, key in (
            ("college_competitors rows with no identity row", "no_spine"),
            ("college opt-ins true on the table but false on the spine",
             "lost_optin"),
        ):
            count = row[key] or 0
            if count:
                problems.append(f"  {count} {label}")

    if problems:
        raise RuntimeError(
            "r7f8a0b2c3d4 refused to drop the per-kind contact columns: the "
            "identity spine does not hold everything they do.\n"
            + "\n".join(problems)
            + "\n\nRe-run the q6e7f8a0b2c3 backfill, or reconcile by hand, "
            "before upgrading. Nothing has been dropped."
        )


def upgrade():
    connection = op.get_bind()
    _guard(connection)

    op.drop_column("pro_competitors", "phone_opted_in")
    op.drop_column("pro_competitors", "email")
    op.drop_column("pro_competitors", "phone")
    op.drop_column("pro_competitors", "address")

    op.drop_column("college_competitors", "phone_opted_in")


def downgrade():
    op.add_column(
        "college_competitors",
        sa.Column("phone_opted_in", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
    )
    op.add_column("pro_competitors", sa.Column("address", sa.Text(), nullable=True))
    op.add_column("pro_competitors", sa.Column("phone", sa.String(length=50),
                                               nullable=True))
    op.add_column("pro_competitors", sa.Column("email", sa.String(length=200),
                                               nullable=True))
    op.add_column(
        "pro_competitors",
        sa.Column("phone_opted_in", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
    )

    connection = op.get_bind()
    connection.execute(sa.text("""
        UPDATE pro_competitors SET
            address = (SELECT c.address FROM competitors c
                        WHERE c.uid = pro_competitors.uid),
            phone = (SELECT c.phone FROM competitors c
                      WHERE c.uid = pro_competitors.uid),
            email = (SELECT c.email FROM competitors c
                      WHERE c.uid = pro_competitors.uid),
            phone_opted_in = COALESCE(
                (SELECT c.phone_opted_in FROM competitors c
                  WHERE c.uid = pro_competitors.uid), false)
        WHERE EXISTS (SELECT 1 FROM competitors c
                       WHERE c.uid = pro_competitors.uid)
    """))
    connection.execute(sa.text("""
        UPDATE college_competitors SET
            phone_opted_in = COALESCE(
                (SELECT c.phone_opted_in FROM competitors c
                  WHERE c.uid = college_competitors.uid), false)
        WHERE EXISTS (SELECT 1 FROM competitors c
                       WHERE c.uid = college_competitors.uid)
    """))
