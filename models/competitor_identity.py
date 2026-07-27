"""Competitor identity table.

Every competitor in the system, college or pro, gets exactly one row here and
exactly one ``uid``.  ``college_competitors.uid`` and ``pro_competitors.uid``
are foreign keys onto this table, which is what makes a college id and a pro
id structurally incapable of colliding.

Why this exists
===============
``EventResult.competitor_id`` and ``Heat.competitors`` carry bare integers with
no foreign key.  A sibling ``competitor_type`` string is the only thing that
says which table the integer points at.  In the production mirror, college ids
29 through 49 all also exist as pro ids: 21 live collisions.  Any code path
that reads the integer and forgets the discriminator addresses the wrong
person.  Commit 9bd4599 fixed the three known paths.  This table removes the
class of bug instead of the instances.

The rejected alternative was a shared sequence: keep two tables, allocate both
their ids from one counter, and add a test asserting the id sets never overlap.
That is uniqueness by discipline.  It holds only as long as every future insert
site remembers to use the shared allocator, which is the same failure shape as
the 188 ``db.session.commit()`` sites that let scratch state drift in the first
place.

Phase 0 scope
=============
This model is inert.  Nothing reads it, nothing writes it outside the backfill
migration.  It exists so that later phases have an identity to point at.
"""
from datetime import datetime

import sqlalchemy as sa

from database import db

from ._types import BIG_ID


class Competitor(db.Model):
    """One row per competitor, college or pro. The identity spine."""

    __tablename__ = 'competitors'
    __table_args__ = (
        db.CheckConstraint("kind IN ('college', 'pro')",
                           name='ck_competitors_kind_valid'),
        db.Index('ix_competitors_tournament_id', 'tournament_id'),
    )

    uid = db.Column(BIG_ID, primary_key=True, autoincrement=True)

    # Which discipline table owns the detail row for this competitor.
    # This is a label, not a discriminator anything is allowed to branch on for
    # identity purposes: the uid is already unique across both.
    kind = db.Column(db.String(16), nullable=False)

    tournament_id = db.Column(
        db.Integer,
        db.ForeignKey('tournaments.id', ondelete='CASCADE'),
        nullable=False,
    )

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f'<Competitor uid={self.uid} kind={self.kind}>'


def allocate_uid(connection, tournament_id, kind):
    """Insert one identity row and return its uid.

    Uses Core against the live connection rather than the ORM session so it can
    be called from inside a ``before_insert`` mapper event, where the session is
    mid-flush and adding new ORM objects is not allowed.
    """
    table = Competitor.__table__
    result = connection.execute(
        table.insert().values(
            kind=kind,
            tournament_id=tournament_id,
            created_at=datetime.utcnow(),
        )
    )
    return result.inserted_primary_key[0]


def attach_identity_allocator(model, kind):
    """Auto-allocate ``uid`` on insert for a competitor model.

    Why this exists
    ===============
    ``uid`` is NOT NULL.  There are 188 non-test ``db.session.commit()`` sites in
    this codebase and competitor rows are created from registration routes, the
    Excel importer, the pro entry importer, demo seeding, and every test fixture.
    Requiring each of those to allocate an identity by hand is the exact failure
    shape this phase exists to eliminate: it would hold only as long as every
    author remembers.

    So allocation happens at the mapper level.  Any code that inserts a
    competitor gets a uid whether it knows about identities or not, and the
    NOT NULL + FOREIGN KEY pair means a row that somehow escaped the allocator
    fails at the database instead of landing without an identity.

    Explicitly supplying ``uid`` (as the backfill migration does, and as a future
    merge-two-records path would) is honoured and skips allocation.
    """

    @sa.event.listens_for(model, 'before_insert')
    def _allocate(mapper, connection, target):  # noqa: ARG001
        if target.uid is not None:
            return
        if target.tournament_id is None:
            raise ValueError(
                f'{model.__name__} inserted without tournament_id; cannot '
                f'allocate a competitor identity'
            )
        target.uid = allocate_uid(connection, target.tournament_id, kind)
