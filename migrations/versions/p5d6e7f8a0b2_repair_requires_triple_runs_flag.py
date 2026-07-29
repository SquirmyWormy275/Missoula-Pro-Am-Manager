"""Repair requires_triple_runs on existing axe throw events.

Revision ID: p5d6e7f8a0b2
Revises: n4c5d6e7f8a9
Create Date: 2026-07-29

PG-safety: data only. No DDL, no batch_alter_table, no Boolean server_default,
no SQLite-only introspection statements. A single guarded UPDATE.

Why this exists
===============
`config.py` has declared `'requires_triple_runs': True` on Axe Throw
(COLLEGE_OPEN_EVENTS) and Partnered Axe Throw (PRO_EVENTS) since the scoring
overhaul. `routes/scheduling/events.py::_upsert_event` copied
`requires_dual_runs` out of that config and had no `requires_triple_runs` twin,
so the application never wrote the column. Every event in the 2026 database
carries `false`, including events 1, 2 and 40.

The companion code change fixes the writer, but the writer only runs when an
operator saves the event setup form. Nothing prompts them to, and
`routes/main.py` copies `requires_triple_runs` verbatim when a tournament is
cloned, so a 2027 clone of the 2026 tournament would inherit `false` from a
source nobody thought to re-save. Fixing the writer without repairing the rows
leaves the deployed database wrong and the bug one clone away from surviving
into next season.

Why the guard
=============
The flag changes how results are displayed, not how stored values are ranked.
`calculate_cumulative_score` runs at entry and import time; it is not re-derived
on read. So flipping the flag on an event that already holds finalized results
whose totals were typed into the `result` column by hand, with the per-throw
columns left empty, would repaint that event's results page with blank T1/T2/T3
cells and a blank Total. Correcting a configuration flag must not rewrite the
appearance of a competition that has already been run and published.

The UPDATE therefore touches only events that are not finalized and hold no
recorded result values. On the production database as it stands that is all
three axe throw events: events 1, 2 and 40 have 14, 4 and 32 result rows
respectively and not one of them carries a `result_value`, a `run1_value` or a
`run3_value`, and none of the three is finalized. An event outside that guard
is left alone deliberately, and the operator can still fix it by saving the
event setup form now that the writer works.

Matching on name
================
`config.AXE_THROW_CUMULATIVE_EVENTS` already keys tie and throw-off detection
off the event name rather than this column, which is the only reason axe throw
scoring survived the missing flag at all. The same two names are used here so
the repair and the existing tie logic cannot disagree about what a cumulative
axe throw event is.

Downgrade
=========
Deliberately a no-op. `upgrade` corrects data that was wrong because of an
application defect. Reversing it would re-introduce the defect's output into a
database that no longer has the defect, and there is no record of which rows
were originally wrong versus intentionally configured. Dropping back past this
revision leaves the corrected flags in place, which is the safe direction.
"""

import sqlalchemy as sa
from alembic import op

revision = 'p5d6e7f8a0b2'
down_revision = 'n4c5d6e7f8a9'
branch_labels = None
depends_on = None


# Kept in sync with config.AXE_THROW_CUMULATIVE_EVENTS. Spelled out here rather
# than imported because a migration must describe the database at the moment it
# ran, not follow application config that may move underneath it later.
CUMULATIVE_EVENT_NAMES = ('Axe Throw', 'Partnered Axe Throw')


def upgrade():
    bind = op.get_bind()
    result = bind.execute(
        sa.text("""
            UPDATE events
               SET requires_triple_runs = true
             WHERE name IN :names
               AND requires_triple_runs = false
               AND is_finalized = false
               AND NOT EXISTS (
                     SELECT 1 FROM event_results r
                      WHERE r.event_id = events.id
                        AND (r.result_value IS NOT NULL
                          OR r.run1_value IS NOT NULL
                          OR r.run2_value IS NOT NULL
                          OR r.run3_value IS NOT NULL)
                   )
        """).bindparams(sa.bindparam('names', value=CUMULATIVE_EVENT_NAMES,
                                     expanding=True))
    )
    print(f'repaired requires_triple_runs on {result.rowcount} event(s)')


def downgrade():
    # No-op on purpose. See the module docstring: reversing a data correction
    # would restore the defect's output into a database that no longer has the
    # defect.
    pass
