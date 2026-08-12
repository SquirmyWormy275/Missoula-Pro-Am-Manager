"""Retire Birling bracket state from events.payouts.

Revision ID: w2b3c4d5e6f7
Revises: v1a2b3c4d5e6
Create Date: 2026-08-11

Birling bracket state was first projected from ``events.payouts`` and then
read from the normalized Birling tables. This revision completes that cutover.
It fails closed if a non-empty legacy document is missing the rows required to
represent it, then removes only the bracket-state keys. Numeric payout keys,
if a legacy event has any, are retained as normal payout configuration.
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op


revision = "w2b3c4d5e6f7"
down_revision = "v1a2b3c4d5e6"
branch_labels = None
depends_on = None

BRACKET_KEYS = frozenset((
    "bracket", "competitors", "current_round", "placements", "pre_seedings", "seeding",
))


def _document(raw):
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _has_matches(document):
    bracket = document.get("bracket")
    if not isinstance(bracket, dict):
        return False
    for value in bracket.values():
        if isinstance(value, dict) and value.get("match_id"):
            return True
        if isinstance(value, list):
            for round_matches in value:
                if isinstance(round_matches, list) and any(
                        isinstance(match, dict) and match.get("match_id")
                        for match in round_matches):
                    return True
    return False


def _count(connection, table, event_id):
    return connection.execute(sa.text(
        f"SELECT count(*) FROM {table} WHERE event_id = :event_id"),
        {"event_id": event_id}).scalar()


def _assert_projected(connection, event_id, document):
    missing = []
    if document.get("seeding") and not _count(connection, "birling_seeds", event_id):
        missing.append("seed rows")
    if document.get("pre_seedings") and not _count(
            connection, "birling_pre_seeds", event_id):
        missing.append("pre-seed rows")
    if _has_matches(document) and not _count(connection, "birling_matches", event_id):
        missing.append("match rows")
    if document.get("placements") and not _count(
            connection, "birling_placements", event_id):
        missing.append("placement rows")
    if missing:
        raise RuntimeError(
            "Birling event %s still has legacy state without %s. "
            "Run scripts/reproject_birling.py and repair it before upgrading."
            % (event_id, ", ".join(missing)))


def _payout_configuration(document):
    """Keep only normal position-to-amount payout keys after state removal."""
    return {key: value for key, value in document.items()
            if key not in BRACKET_KEYS and str(key).isdigit()}


def upgrade():
    connection = op.get_bind()
    rows = connection.execute(sa.text(
        "SELECT id, payouts FROM events WHERE payouts IS NOT NULL")).fetchall()
    cleared = 0
    for row in rows:
        document = _document(row.payouts)
        if not document or not set(document).intersection(BRACKET_KEYS):
            continue
        _assert_projected(connection, row.id, document)
        connection.execute(sa.text(
            "UPDATE events SET payouts = :payouts WHERE id = :event_id"),
            {"event_id": row.id,
             "payouts": json.dumps(_payout_configuration(document))})
        cleared += 1
    if cleared:
        print(f"w2b3c4d5e6f7: removed legacy Birling JSON state from {cleared} event(s).")


def downgrade():
    # The rows remain authoritative across rollback. Reconstructing a legacy
    # document here would create a second writer and could fabricate history.
    pass
