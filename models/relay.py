"""Normalized Pro-Am Relay state.

The relay is scored by team. Team members carry a competitor identity so the
roster cannot silently point to the wrong college or pro record, but a relay
leg deliberately has no competitor reference: teams arrange their own legs.
"""

import sqlalchemy as sa

from database import db

from ._types import BIG_ID

RELAY_EVENT_KEYS = (
    "partnered_sawing",
    "standing_butcher_block",
    "underhand_butcher_block",
    "team_axe_throw",
)


class RelayState(db.Model):
    """One relay draw and result set for one Pro-Am Relay event."""

    __tablename__ = "relay_states"
    __table_args__ = (
        db.UniqueConstraint("event_id", name="uq_relay_states_event"),
        db.CheckConstraint(
            "status IN ('not_drawn', 'drawn', 'in_progress', 'completed')",
            name="ck_relay_states_status_valid",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("events.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="not_drawn")

    teams = db.relationship(
        "RelayTeam",
        back_populates="relay_state",
        cascade="all, delete-orphan",
        order_by="RelayTeam.team_number",
    )


class RelayTeam(db.Model):
    """One eight-person relay team, its aggregate time, and payout state."""

    __tablename__ = "relay_teams"
    __table_args__ = (
        db.UniqueConstraint("relay_state_id", "team_number", name="uq_relay_teams_state_number"),
        db.UniqueConstraint("relay_state_id", "id", name="uq_relay_teams_state_id"),
        db.CheckConstraint("team_number >= 1", name="ck_relay_teams_number_positive"),
        db.CheckConstraint(
            "total_time IS NULL OR total_time >= 0", name="ck_relay_teams_time_nonnegative"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    relay_state_id = db.Column(db.Integer, db.ForeignKey("relay_states.id"), nullable=False)
    team_number = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String(200), nullable=False)
    total_time = db.Column(db.Float, nullable=True)
    payout_settled = db.Column(
        db.Boolean, nullable=False, default=False, server_default=sa.text("false")
    )

    relay_state = db.relationship("RelayState", back_populates="teams")
    members = db.relationship(
        "RelayTeamMember",
        back_populates="relay_team",
        cascade="all, delete-orphan",
        order_by="RelayTeamMember.id",
    )
    events = db.relationship(
        "RelayTeamEvent",
        back_populates="relay_team",
        cascade="all, delete-orphan",
        order_by="RelayTeamEvent.event_key",
    )


class RelayTeamMember(db.Model):
    """A roster assignment, unique across a relay draw."""

    __tablename__ = "relay_team_members"
    __table_args__ = (
        db.ForeignKeyConstraint(
            ["relay_state_id", "relay_team_id"],
            ["relay_teams.relay_state_id", "relay_teams.id"],
        ),
        db.ForeignKeyConstraint(["uid"], ["competitors.uid"]),
        db.UniqueConstraint("relay_state_id", "uid", name="uq_relay_members_state_uid"),
    )

    id = db.Column(db.Integer, primary_key=True)
    relay_state_id = db.Column(db.Integer, nullable=False)
    relay_team_id = db.Column(db.Integer, nullable=False)
    uid = db.Column(BIG_ID, nullable=False)

    relay_team = db.relationship("RelayTeam", back_populates="members")


class RelayTeamEvent(db.Model):
    """A team-level relay leg result, intentionally without a competitor uid."""

    __tablename__ = "relay_team_events"
    __table_args__ = (
        db.UniqueConstraint("relay_team_id", "event_key", name="uq_relay_team_events_key"),
        db.CheckConstraint(
            "event_key IN ('partnered_sawing', 'standing_butcher_block', "
            "'underhand_butcher_block', 'team_axe_throw')",
            name="ck_relay_team_events_key_valid",
        ),
        db.CheckConstraint(
            "status IN ('pending', 'completed')", name="ck_relay_team_events_status_valid"
        ),
        db.CheckConstraint(
            "result IS NULL OR result >= 0", name="ck_relay_team_events_result_nonnegative"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    relay_team_id = db.Column(db.Integer, db.ForeignKey("relay_teams.id"), nullable=False)
    event_key = db.Column(db.String(40), nullable=False)
    result = db.Column(db.Float, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="pending")

    relay_team = db.relationship("RelayTeam", back_populates="events")
