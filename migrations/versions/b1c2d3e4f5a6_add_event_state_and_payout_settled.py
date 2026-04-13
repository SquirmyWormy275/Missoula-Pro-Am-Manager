"""Add event_state to events and payout_settled to event_results.

Separates the overloaded Event.payouts column: events that store state-machine
data (Pro-Am Relay, Partnered Axe Throw prelims, Birling bracket) get their
existing payouts JSON copied to the new event_state column and their payouts
column reset to '{}'.  All other events are unaffected.

Requirements: R8 (event_state column), R18 (payout_settled column)

Revision ID: b1c2d3e4f5a6
Revises: a1b2c3d4e5f8
Create Date: 2026-04-12
"""

from __future__ import annotations

import json
import logging

from alembic import op
import sqlalchemy as sa

logger = logging.getLogger(__name__)

# revision identifiers, used by Alembic.
revision = "b1c2d3e4f5a6"
down_revision = "a1b2c3d4e5f8"
branch_labels = None
depends_on = None

# Events whose payouts column stores state-machine JSON rather than payout
# amounts.  Matches Event.uses_payouts_for_state logic: has_prelims=True,
# scoring_type='bracket', or name='Pro-Am Relay'.
_STATE_EVENT_NAMES = frozenset(["Pro-Am Relay"])
_STATE_SCORING_TYPES = frozenset(["bracket"])


def upgrade():
    # --- DDL: add new columns ---

    # event_state: nullable TEXT on events — existing rows get NULL.
    # PG-safe: direct op.add_column, no server_default.
    op.add_column(
        "events",
        sa.Column("event_state", sa.Text(), nullable=True),
    )

    # payout_settled: non-nullable Boolean with server_default 'false'.
    # server_default ensures existing rows get False without a table rewrite on PG.
    op.add_column(
        "event_results",
        sa.Column(
            "payout_settled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # --- Data migration: copy payouts → event_state for state-machine events ---

    conn = op.get_bind()

    # Fetch ALL events and filter in Python — avoids IN-clause binding issues
    # across SQLite and PostgreSQL dialects (SQLite's sqlite3 driver does not
    # accept tuple parameters for IN via sa.text()).
    all_rows = conn.execute(
        sa.text(
            "SELECT id, name, payouts, has_prelims, scoring_type FROM events"
        )
    ).fetchall()

    # Apply uses_payouts_for_state criteria: name in STATE_NAMES, scoring_type
    # in STATE_SCORING_TYPES, or has_prelims is truthy.
    rows = [
        r
        for r in all_rows
        if r[1] in _STATE_EVENT_NAMES
        or r[4] in _STATE_SCORING_TYPES
        or r[3]
    ]

    for row in rows:
        event_id = row[0]
        event_name = row[1]
        payouts_raw = row[2]

        # Validate JSON before migrating — skip rows with malformed data.
        try:
            state_json = json.loads(payouts_raw or "{}")
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning(
                "event_state migration: skipping event id=%s name=%r — "
                "payouts column contains invalid JSON: %s",
                event_id,
                event_name,
                exc,
            )
            continue

        # Copy existing payouts value to event_state.
        # Only migrate if there's meaningful state (non-empty dict / non-null).
        new_state = json.dumps(state_json) if state_json else None

        conn.execute(
            sa.text(
                "UPDATE events "
                "SET event_state = :state, payouts = :empty "
                "WHERE id = :eid"
            ),
            {"state": new_state, "empty": "{}", "eid": event_id},
        )

    logger.info("event_state migration: processed %d candidate event(s).", len(rows))


def downgrade():
    conn = op.get_bind()

    # Reverse the data migration: copy event_state back to payouts for migrated
    # events (those where event_state IS NOT NULL and payouts is still '{}').
    rows = conn.execute(
        sa.text("SELECT id, event_state FROM events " "WHERE event_state IS NOT NULL")
    ).fetchall()

    for row in rows:
        event_id = row[0]
        state_raw = row[1]
        conn.execute(
            sa.text("UPDATE events SET payouts = :state WHERE id = :eid"),
            {"state": state_raw, "eid": event_id},
        )

    logger.info("event_state downgrade: restored payouts for %d event(s).", len(rows))

    # --- DDL: drop columns ---
    op.drop_column("event_results", "payout_settled")
    op.drop_column("events", "event_state")
