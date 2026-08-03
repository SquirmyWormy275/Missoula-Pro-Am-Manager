"""Drop the two heats JSON roster columns (D12-C phase 2, commit F3).

Revision ID: t9b3c4d5e6f7
Revises: s8a0b2c3d4e5
Create Date: 2026-08-03

The end of D12-C. ``s8a0b2c3d4e5`` gave ``heat_assignments`` a real reference
onto the identity spine and made the row the thing a roster can be expressed
as; commit E made ``Heat.set_roster`` the only writer and the rows the only
truth; commit F2 removed the last code in this tree that read
``heats.competitors`` or ``heats.stand_assignments``. This drops them.

Columns dropped
===============
``heats.competitors``, a Text column holding a JSON list of competitor ids in
running order. ``heats.stand_assignments``, a Text column holding a JSON dict
of competitor id to stand number. Both were ``NOT NULL`` with defaults of
``'[]'`` and ``'{}'``. Neither carries an index or is referenced by any
constraint, verified against the production mirror, so there is nothing to
drop alongside them.

Why ``op.drop_column`` and not ``batch_alter_table``
====================================================
Same reason as ``r7f8a0b2c3d4``:
``tests/test_pg_migration_safety.py::TestNoBatchAlterTableInUpgrades`` fails a
``batch_alter_table`` block containing no constraint operation, and a bare
column drop is not one. Production is Postgres as of D14-B and SQLite has
supported ``ALTER TABLE DROP COLUMN`` natively since 3.35 (this tree runs
3.45). Neither column is indexed or referenced, so the SQLite restriction on
that does not apply.

What the guard checks, and what it deliberately does not
========================================================
It checks for *loss*: a heat whose ``competitors`` column names a roster while
the heat has no ``heat_assignments`` rows at all. That is the one state in
which dropping these columns destroys information, because the roster exists
in exactly one place and this migration is about to delete that place.

It does not fail on *difference*. Since commit E every roster write has gone
through ``Heat.set_roster``, which wrote the rows first and rendered the
columns from them, so on any database that has served traffic since then the
columns are a stale mirror by design and the rows are the answer. Failing on a
divergence would block the deployment for doing exactly what D12-C built it to
do. Divergences are counted and logged so an operator can see them, and then
the rows win, which is what every reader in the app has already been doing
since F2.

It reports counts and heat ids, never rosters. A heat roster is a list of
integers and harmless on its own, but this migration log is shared and the
habit of not printing production rows into it is worth more than the
convenience.

The comparison runs in Python rather than SQL because ``json_each`` and
``jsonb_array_elements`` are not the same function, and a guard that has to be
right on both SQLite and Postgres is cheaper to write once in Python than
twice in dialect SQL. It is one pass over ``heats``, which is 173 rows on the
production mirror.

Downgrade
=========
Re-adds both columns with their original defaults and refills them from
``heat_assignments``, which is the truth by this point, in ``id`` order, which
is the running order ``set_roster`` writes and the judge sheet prints. A
downgrade followed by an upgrade is therefore lossless.

An upgrade followed by a downgrade is not, and this is the commit where that
stops being true for D12-C. Every commit from C through F2 could be reverted
and leave a working tree that still read a correct column. After this one the
columns come back empty of anything the rows do not hold. Nothing needs what
the rows do not hold, which is the entire argument of phase 2, but the
one-way-ness is real and is why this is its own commit.
"""
import json

import sqlalchemy as sa
from alembic import op

revision = "t9b3c4d5e6f7"
down_revision = "s8a0b2c3d4e5"
branch_labels = None
depends_on = None


def _parse_roster(raw):
    """Parse a ``heats.competitors`` value into a list, forgiving what it must.

    The column has held ``NULL`` on databases predating its ``NOT NULL``
    default, ``''`` from at least one legacy import path, and valid JSON
    everywhere else. None of those three is a roster and none of them is a
    reason to refuse a drop, so all three come back empty. Anything that
    parses to a non-list is treated the same way: it was never readable as a
    roster, so dropping it loses nothing.
    """
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _guard(connection):
    """Refuse to drop a roster that lives nowhere else.

    Postgres has transactional DDL, so raising here leaves the schema exactly
    as it was. On SQLite the drops have not run either: the guard is the first
    thing ``upgrade`` does.
    """
    rows = connection.execute(sa.text(
        "SELECT id, competitors FROM heats ORDER BY id"
    )).fetchall()

    seated = {
        heat_id: count
        for heat_id, count in connection.execute(sa.text(
            "SELECT heat_id, COUNT(*) FROM heat_assignments GROUP BY heat_id"
        )).fetchall()
    }

    unseated = []
    diverged = 0
    for heat_id, raw in rows:
        roster = _parse_roster(raw)
        if not roster:
            continue
        have = seated.get(heat_id, 0)
        if have == 0:
            unseated.append(heat_id)
        elif have != len(roster):
            diverged += 1

    if diverged:
        print(
            f"t9b3c4d5e6f7: {diverged} heats have a JSON roster of a "
            "different length than their heat_assignments rows. The rows are "
            "the roster as of D12-C commit E and every reader in the app has "
            "used them since commit F2, so this is stale JSON and is being "
            "dropped on purpose."
        )

    if unseated:
        shown = ", ".join(str(h) for h in unseated[:20])
        more = f" (and {len(unseated) - 20} more)" if len(unseated) > 20 else ""
        raise RuntimeError(
            f"t9b3c4d5e6f7 refused to drop heats.competitors and "
            f"heats.stand_assignments: {len(unseated)} heats name a roster in "
            f"the JSON column and have no heat_assignments rows at all, so "
            f"the column is the only copy.\n"
            f"  heat ids: {shown}{more}\n\n"
            "Seat them through Heat.set_roster, or delete the heats if they "
            "are junk, before upgrading. Nothing has been dropped."
        )


def upgrade():
    connection = op.get_bind()
    _guard(connection)

    op.drop_column("heats", "stand_assignments")
    op.drop_column("heats", "competitors")


def downgrade():
    op.add_column(
        "heats",
        sa.Column("competitors", sa.Text(), nullable=False,
                  server_default=sa.text("'[]'")),
    )
    op.add_column(
        "heats",
        sa.Column("stand_assignments", sa.Text(), nullable=False,
                  server_default=sa.text("'{}'")),
    )

    connection = op.get_bind()
    rosters = {}
    stands = {}
    for heat_id, competitor_id, stand_number in connection.execute(sa.text(
        "SELECT heat_id, competitor_id, stand_number "
        "  FROM heat_assignments ORDER BY heat_id, id"
    )).fetchall():
        rosters.setdefault(heat_id, []).append(competitor_id)
        if stand_number is not None:
            stands.setdefault(heat_id, {})[str(competitor_id)] = stand_number

    for heat_id, roster in rosters.items():
        connection.execute(
            sa.text("UPDATE heats SET competitors = :c, "
                    "stand_assignments = :s WHERE id = :h"),
            {
                "h": heat_id,
                "c": json.dumps(roster),
                "s": json.dumps(stands.get(heat_id, {})),
            },
        )
