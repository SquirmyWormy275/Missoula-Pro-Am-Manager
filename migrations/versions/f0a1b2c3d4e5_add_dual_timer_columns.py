"""add dual judge timer readings to event_results

Phase 1A of the scoring system fix (V2.8.0).

Adds four new columns to event_results to store the two raw judge stopwatch
readings for each run.  Every timed event in this codebase (college and pro,
single-run AND dual-run) has TWO judges with stopwatches on each physical run.
The two readings are averaged in routes/scoring.py at heat-save time and the
result flows into the existing run1_value / run2_value / result_value fields.

Schema model:

  Single-run timed events (e.g., Pro Underhand, College Single Buck):
      t1_run1, t2_run1 → averaged into result_value (and run1_value)
      t1_run2, t2_run2 stay NULL

  Dual-run timed events (Speed Climb, Chokerman's Race, Caber Toss):
      t1_run1, t2_run1 → averaged into run1_value
      t1_run2, t2_run2 → averaged into run2_value
      best_run = min(run1_value, run2_value) for lowest_wins, max for highest_wins

This migration also backfills the new columns for any existing EventResult rows
so historical data round-trips through the new code path with no value drift:
for each existing row with a non-null result_value (or run1_value / run2_value
for dual-run events), set the corresponding raw timer columns equal to that
value.  The average of two identical numbers is the original number, so no
ranking changes are produced.

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
Create Date: 2026-04-08
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f0a1b2c3d4e5'
down_revision = 'e9f0a1b2c3d4'
branch_labels = None
depends_on = None


def upgrade():
    # ------------------------------------------------------------------
    # 1. Add the four new columns to event_results.
    #    nullable=True because they are NULL until the heat is scored.
    #    No server_default because NULL is the correct default state.
    # ------------------------------------------------------------------
    op.add_column(
        'event_results',
        sa.Column('t1_run1', sa.Numeric(8, 2), nullable=True),
    )
    op.add_column(
        'event_results',
        sa.Column('t2_run1', sa.Numeric(8, 2), nullable=True),
    )
    op.add_column(
        'event_results',
        sa.Column('t1_run2', sa.Numeric(8, 2), nullable=True),
    )
    op.add_column(
        'event_results',
        sa.Column('t2_run2', sa.Numeric(8, 2), nullable=True),
    )

    # ------------------------------------------------------------------
    # 2. Data backfill — populate the new columns from existing values.
    #    Uses dialect-portable SQLAlchemy expressions (no raw SQL with
    #    boolean comparisons that differ between SQLite and PostgreSQL).
    #
    #    Strategy:
    #      - For events with scoring_type IN ('time','distance'):
    #          * If requires_dual_runs is False: copy result_value into
    #            t1_run1 + t2_run1 for any row with non-null result_value.
    #          * If requires_dual_runs is True: copy run1_value into
    #            t1_run1 + t2_run1 (where non-null), and run2_value into
    #            t1_run2 + t2_run2 (where non-null).
    #      - Score-based events (axe throw — score), bracket events
    #        (birling), and hits-based events (Hard Hit primary metric)
    #        are NOT backfilled — they don't use the dual-timer rule.
    #        Hard Hit's tiebreak_value is also not backfilled in v1; the
    #        tiebreak time stays a single judge entry per the design
    #        decision in PLAN_REVIEW.md A1.
    # ------------------------------------------------------------------
    events_meta = sa.table(
        'events',
        sa.column('id', sa.Integer),
        sa.column('scoring_type', sa.String),
        sa.column('requires_dual_runs', sa.Boolean),
    )
    results_meta = sa.table(
        'event_results',
        sa.column('event_id', sa.Integer),
        sa.column('result_value', sa.Float),
        sa.column('run1_value', sa.Float),
        sa.column('run2_value', sa.Float),
        sa.column('t1_run1', sa.Numeric(8, 2)),
        sa.column('t2_run1', sa.Numeric(8, 2)),
        sa.column('t1_run2', sa.Numeric(8, 2)),
        sa.column('t2_run2', sa.Numeric(8, 2)),
    )

    conn = op.get_bind()

    # Single-run timed/distance events
    single_run_event_ids = [
        row[0]
        for row in conn.execute(
            sa.select(events_meta.c.id).where(
                sa.and_(
                    events_meta.c.scoring_type.in_(['time', 'distance']),
                    events_meta.c.requires_dual_runs.is_(False),
                )
            )
        )
    ]

    # Dual-run timed/distance events
    dual_run_event_ids = [
        row[0]
        for row in conn.execute(
            sa.select(events_meta.c.id).where(
                sa.and_(
                    events_meta.c.scoring_type.in_(['time', 'distance']),
                    events_meta.c.requires_dual_runs.is_(True),
                )
            )
        )
    ]

    if single_run_event_ids:
        conn.execute(
            sa.update(results_meta)
            .where(
                sa.and_(
                    results_meta.c.event_id.in_(single_run_event_ids),
                    results_meta.c.result_value.isnot(None),
                )
            )
            .values(
                t1_run1=results_meta.c.result_value,
                t2_run1=results_meta.c.result_value,
            )
        )

    if dual_run_event_ids:
        # Backfill run 1 timer columns from existing run1_value
        conn.execute(
            sa.update(results_meta)
            .where(
                sa.and_(
                    results_meta.c.event_id.in_(dual_run_event_ids),
                    results_meta.c.run1_value.isnot(None),
                )
            )
            .values(
                t1_run1=results_meta.c.run1_value,
                t2_run1=results_meta.c.run1_value,
            )
        )
        # Backfill run 2 timer columns from existing run2_value
        conn.execute(
            sa.update(results_meta)
            .where(
                sa.and_(
                    results_meta.c.event_id.in_(dual_run_event_ids),
                    results_meta.c.run2_value.isnot(None),
                )
            )
            .values(
                t1_run2=results_meta.c.run2_value,
                t2_run2=results_meta.c.run2_value,
            )
        )


def downgrade():
    # Drop the four new columns.  op.drop_column() works directly on
    # PostgreSQL and on SQLite >= 3.35 (released 2021).  This codebase
    # ships SQLAlchemy 2.0+ which requires SQLite 3.21+ at minimum and
    # almost all distros now ship 3.35+, so direct drop_column is safe.
    op.drop_column('event_results', 't2_run2')
    op.drop_column('event_results', 't1_run2')
    op.drop_column('event_results', 't2_run1')
    op.drop_column('event_results', 't1_run1')
