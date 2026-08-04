"""Append-only intent log.

One row per accepted command against a tournament.  Rows are never updated and
never deleted.  A correction is a new row whose ``parent_seq`` points at the row
being corrected; a reversal is the same shape with a reversing ``kind``.

Why a new table instead of extending audit_logs
===============================================
``audit_logs`` records what was *attempted*, including actions that were
rejected.  This log records only what was *accepted*, because a reducer that
replays it must produce exactly the state the system is in.  Feeding rejected
actions into a reducer would produce a state the system was never in.  The two
tables answer different questions and are kept separate.

Why ``seq`` is not the primary key
==================================
``id`` is a global surrogate.  ``seq`` is monotonic *per tournament* and is what
the reducer orders by, so two tournaments can be replayed independently and a
gap in one cannot be caused by writes to the other.  ``UNIQUE (tournament_id,
seq)`` is what makes the single-writer allocation safe: a second writer that
races on the same seq gets an IntegrityError instead of silently interleaving.

``occurred_at`` is injected at the edge and must never be read inside the
reducer.  A reducer that reads a clock is not a pure function of the log, and
replay would produce different state on different days.

Phase 0 scope
=============
This model is inert.  The migration creates the table; nothing writes to it and
nothing reads from it.  It exists so the reducer, projector, and dispatcher in
later phases have a substrate already in production and already migrated.
"""
import sqlalchemy as sa

from database import db
from services.time_utils import utc_now_naive

from ._types import BIG_ID, JSON_PAYLOAD


class TournamentEvent(db.Model):
    """One accepted intent. Append-only. Never updated, never deleted."""

    __tablename__ = 'tournament_event'
    __table_args__ = (
        db.UniqueConstraint('tournament_id', 'seq',
                            name='uq_tournament_event_tournament_seq'),
    )

    id = db.Column(BIG_ID, primary_key=True, autoincrement=True)

    tournament_id = db.Column(
        db.Integer,
        db.ForeignKey('tournaments.id', ondelete='CASCADE'),
        nullable=False,
    )

    # Monotonic per tournament, assigned under the single-writer lock.
    # Not a primary key: see module docstring.
    seq = db.Column(db.BigInteger, nullable=False)

    # Normalized verb. The 98 existing audit action strings collapse into this
    # vocabulary in a later phase; Phase 0 fixes only the column.
    kind = db.Column(db.String(64), nullable=False)

    actor_user_id = db.Column(
        db.Integer,
        db.ForeignKey('users.id'),
        nullable=True,
    )

    # Injected at the edge. NEVER read inside the reducer.
    occurred_at = db.Column(db.DateTime, nullable=False, default=utc_now_naive)

    # Fully self-describing intent. Must carry everything the reducer needs;
    # the reducer is not allowed to query other tables to interpret it.
    payload = db.Column(JSON_PAYLOAD, nullable=False)

    # Reversal and correction linkage. Points at another row's `seq` within the
    # same tournament. Deliberately not a foreign key: the target is identified
    # by (tournament_id, seq), and a composite self-referential FK would force
    # every insert to resolve the parent before the child could land.
    parent_seq = db.Column(db.BigInteger, nullable=True)

    # Hash of projected state after applying this event. NULL until the
    # projector runs (Phase 3), which is why it is nullable here.
    state_hash = db.Column(db.CHAR(64), nullable=True)

    schema_version = db.Column(
        db.SmallInteger,
        nullable=False,
        default=1,
        server_default=sa.text('1'),
    )

    def __repr__(self):
        return (f'<TournamentEvent t={self.tournament_id} '
                f'seq={self.seq} kind={self.kind}>')
