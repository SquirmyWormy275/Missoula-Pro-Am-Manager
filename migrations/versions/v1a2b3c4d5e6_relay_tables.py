"""Project Pro-Am Relay state into normalized tables (D13-C, commit B1).

Revision ID: v1a2b3c4d5e6
Revises: u0c4d5e6f7a8
Create Date: 2026-08-11

Relay state has lived in ``events.event_state`` as a single JSON document. It
holds the draw status, teams, roster snapshots, four team-level leg results,
and aggregate times. This revision creates a validated row projection while
leaving that document untouched and still authoritative. Later commits can
dual-write and then switch readers only after their parity checks are proven.

Team memberships reference ``competitors.uid``. Relay legs deliberately do
not: a team decides who performs each leg, and relay results must not become
individual EventResult rows or college-score inputs.
"""

from __future__ import annotations

import json
import math

import sqlalchemy as sa
from alembic import op

from models._types import BIG_ID


revision = "v1a2b3c4d5e6"
down_revision = "u0c4d5e6f7a8"
branch_labels = None
depends_on = None

RELAY_EVENT_KEYS = (
    "partnered_sawing",
    "standing_butcher_block",
    "underhand_butcher_block",
    "team_axe_throw",
)
TABLES = ("relay_states", "relay_teams", "relay_team_members", "relay_team_events")
_STATE_STATUSES = frozenset(("not_drawn", "drawn", "in_progress", "completed"))
_TEAM_EVENT_STATUSES = frozenset(("pending", "completed"))
_MEMBER_FIELDS = (("pro_members", "pro"), ("college_members", "college"))


class _Plan:
    """Validated rows for one legacy relay document, or refusal reasons."""

    def __init__(self, event_id, tournament_id):
        self.event_id = event_id
        self.tournament_id = tournament_id
        self.status = "not_drawn"
        self.teams = []
        self.reasons = set()

    @property
    def valid(self):
        return not self.reasons

    def reject(self, reason):
        self.reasons.add(reason)


def _integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def _competitor_pool(connection, tournament_id):
    """Return legacy ``(kind, id)`` references mapped to identity uids."""
    pool = {}
    for kind, table in (("pro", "pro_competitors"), ("college", "college_competitors")):
        rows = connection.execute(sa.text(
            f"SELECT id, uid FROM {table} WHERE tournament_id = :tournament_id"),
            {"tournament_id": tournament_id}).fetchall()
        pool.update({(kind, row.id): row.uid for row in rows})
    return pool


def _plan(connection, event_id, tournament_id, raw):
    plan = _Plan(event_id, tournament_id)
    try:
        document = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        plan.reject("relay state is not valid JSON")
        return plan

    if not isinstance(document, dict):
        plan.reject("relay state is not an object")
        return plan

    status = document.get("status", "not_drawn")
    if status not in _STATE_STATUSES:
        plan.reject("relay state has an invalid status")
    else:
        plan.status = status

    teams = document.get("teams", [])
    if not isinstance(teams, list):
        plan.reject("relay state has a non-list team collection")
        return plan

    pool = _competitor_pool(connection, tournament_id)
    team_numbers = set()
    used_uids = set()
    for raw_team in teams:
        if not isinstance(raw_team, dict):
            plan.reject("relay state contains a non-object team")
            continue

        team_number = _integer(raw_team.get("team_number"))
        if team_number is None or team_number < 1 or team_number in team_numbers:
            plan.reject("relay state has a missing or duplicate team number")
            continue
        team_numbers.add(team_number)

        name = raw_team.get("name")
        if not isinstance(name, str) or not name:
            plan.reject("relay state has a team without a name")
            continue

        total_raw = raw_team.get("total_time")
        total_time = None if total_raw is None else _number(total_raw)
        if total_raw is not None and total_time is None:
            plan.reject("relay state has an invalid total time")
            continue

        members = []
        for field, kind in _MEMBER_FIELDS:
            raw_members = raw_team.get(field, [])
            if not isinstance(raw_members, list):
                plan.reject("relay state has a non-list roster")
                continue
            for raw_member in raw_members:
                member_id = _integer(raw_member.get("id")) if isinstance(raw_member, dict) else None
                uid = pool.get((kind, member_id))
                if uid is None:
                    plan.reject("a relay member reference names nobody in its tournament pool")
                    continue
                if uid in used_uids:
                    plan.reject("a relay member is assigned to more than one team")
                    continue
                used_uids.add(uid)
                members.append(uid)

        raw_events = raw_team.get("events")
        if not isinstance(raw_events, dict) or set(raw_events) != set(RELAY_EVENT_KEYS):
            plan.reject("relay state does not contain exactly the four relay events")
            continue

        events = []
        for event_key in RELAY_EVENT_KEYS:
            raw_event = raw_events[event_key]
            if not isinstance(raw_event, dict):
                plan.reject("relay state contains a non-object team event")
                continue
            event_status = raw_event.get("status", "pending")
            result_raw = raw_event.get("result")
            result = None if result_raw is None else _number(result_raw)
            if event_status not in _TEAM_EVENT_STATUSES:
                plan.reject("relay state has an invalid team event status")
            elif result_raw is not None and result is None:
                plan.reject("relay state has an invalid team event result")
            elif event_status == "completed" and result is None:
                plan.reject("relay state marks a team event complete without a result")
            else:
                events.append({"event_key": event_key, "result": result, "status": event_status})

        plan.teams.append({
            "team_number": team_number,
            "name": name,
            "total_time": total_time,
            "members": members,
            "events": events,
        })
    return plan


def _tables(connection):
    metadata = sa.MetaData()
    return {
        name: sa.Table(name, metadata, autoload_with=connection)
        for name in TABLES
    }


def _write(connection, plan):
    tables = _tables(connection)
    state_id = connection.execute(tables["relay_states"].insert().values(
        event_id=plan.event_id,
        status=plan.status,
    )).inserted_primary_key[0]

    for team in plan.teams:
        team_id = connection.execute(tables["relay_teams"].insert().values(
            relay_state_id=state_id,
            team_number=team["team_number"],
            name=team["name"],
            total_time=team["total_time"],
            payout_settled=False,
        )).inserted_primary_key[0]
        for uid in team["members"]:
            connection.execute(tables["relay_team_members"].insert().values(
                relay_state_id=state_id,
                relay_team_id=team_id,
                uid=uid,
            ))
        for event in team["events"]:
            connection.execute(tables["relay_team_events"].insert().values(
                relay_team_id=team_id,
                event_key=event["event_key"],
                result=event["result"],
                status=event["status"],
            ))


def _backfill(connection):
    """Project resolvable relay state; refuse a whole event rather than part of it."""
    rows = connection.execute(sa.text(
        "SELECT id, tournament_id, event_state FROM events "
        "WHERE name = 'Pro-Am Relay' AND event_state IS NOT NULL")).fetchall()
    loaded = 0
    skipped = 0
    reasons = set()
    for row in rows:
        already_loaded = connection.execute(sa.text(
            "SELECT 1 FROM relay_states WHERE event_id = :event_id"),
            {"event_id": row.id}).scalar()
        if already_loaded:
            continue
        plan = _plan(connection, row.id, row.tournament_id, row.event_state)
        if not plan.valid:
            skipped += 1
            reasons |= plan.reasons
            continue
        _write(connection, plan)
        loaded += 1

    if skipped:
        print(
            f"v1a2b3c4d5e6: loaded {loaded} relay state projection(s); "
            f"left {skipped} legacy document(s) untouched. reasons: "
            f"{'; '.join(sorted(reasons))}"
        )
    elif loaded:
        print(f"v1a2b3c4d5e6: loaded {loaded} relay state projection(s).")


def upgrade():
    op.create_table(
        "relay_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_relay_states_event"),
        sa.CheckConstraint(
            "status IN ('not_drawn', 'drawn', 'in_progress', 'completed')",
            name="ck_relay_states_status_valid",
        ),
    )
    op.create_table(
        "relay_teams",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("relay_state_id", sa.Integer(), nullable=False),
        sa.Column("team_number", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("total_time", sa.Float(), nullable=True),
        sa.Column("payout_settled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.ForeignKeyConstraint(["relay_state_id"], ["relay_states.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("relay_state_id", "team_number", name="uq_relay_teams_state_number"),
        sa.UniqueConstraint("relay_state_id", "id", name="uq_relay_teams_state_id"),
        sa.CheckConstraint("team_number >= 1", name="ck_relay_teams_number_positive"),
        sa.CheckConstraint(
            "total_time IS NULL OR total_time >= 0", name="ck_relay_teams_time_nonnegative"
        ),
    )
    op.create_table(
        "relay_team_members",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("relay_state_id", sa.Integer(), nullable=False),
        sa.Column("relay_team_id", sa.Integer(), nullable=False),
        sa.Column("uid", BIG_ID, nullable=False),
        sa.ForeignKeyConstraint(
            ["relay_state_id", "relay_team_id"],
            ["relay_teams.relay_state_id", "relay_teams.id"],
        ),
        sa.ForeignKeyConstraint(["uid"], ["competitors.uid"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("relay_state_id", "uid", name="uq_relay_members_state_uid"),
    )
    op.create_table(
        "relay_team_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("relay_team_id", sa.Integer(), nullable=False),
        sa.Column("event_key", sa.String(length=40), nullable=False),
        sa.Column("result", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(["relay_team_id"], ["relay_teams.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("relay_team_id", "event_key", name="uq_relay_team_events_key"),
        sa.CheckConstraint(
            "event_key IN ('partnered_sawing', 'standing_butcher_block', "
            "'underhand_butcher_block', 'team_axe_throw')",
            name="ck_relay_team_events_key_valid",
        ),
        sa.CheckConstraint("status IN ('pending', 'completed')", name="ck_relay_team_events_status_valid"),
        sa.CheckConstraint(
            "result IS NULL OR result >= 0", name="ck_relay_team_events_result_nonnegative"
        ),
    )
    _backfill(op.get_bind())


def downgrade():
    op.drop_table("relay_team_events")
    op.drop_table("relay_team_members")
    op.drop_table("relay_teams")
    op.drop_table("relay_states")
