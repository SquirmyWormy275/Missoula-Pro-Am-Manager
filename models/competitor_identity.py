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

What lives here
===============
Identity, and the facts that belong to the competitor rather than to the
discipline.  Contact (``address``, ``phone``, ``email``, ``phone_opted_in``)
moved onto this table in migration ``q6e7f8a0b2c3``.

A row here is one competitor registration, not one human.  Somebody who enters
both the college and the pro side has a ``kind='college'`` row and a
``kind='pro'`` row with different uids, and this table does not claim they are
the same person.  Contact is here because every competitor has contact and only
pros had a column for it, not because the spine deduplicates anybody.

``CollegeCompetitor`` and ``ProCompetitor`` reach these fields through
``association_proxy``, so ``competitor.phone`` reads, writes and filters exactly
as it did when it was a column, at every call site, with no flush dance.  That
is the whole point: 18 places in this repo pass contact as a constructor keyword
(``ProCompetitor(name=..., phone=...)``), and a design that required them all to
add ``db.session.flush()`` first would be a footgun re-armed by every new
registration path somebody writes.  ``attach_identity_allocator`` therefore
creates the identity object at construction time, so there is always something
for the proxy to write through.
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

    # Contact.  Moved off pro_competitors / college_competitors by q6e7f8a0b2c3.
    #
    # The three text fields are nullable because they are genuinely absent for
    # most rows: no college competitor in the production data has any of them,
    # and a pro can register without an email.
    #
    # phone_opted_in is NOT NULL because it feeds a boolean predicate.  A
    # nullable opt-in would force every read site to invent a meaning for NULL,
    # which is a third state nobody asked for.  It defaults false: a competitor
    # who has never seen the toggle has not consented to anything.
    address = db.Column(db.Text, nullable=True)
    phone = db.Column(db.String(50), nullable=True)
    email = db.Column(db.String(200), nullable=True)
    phone_opted_in = db.Column(
        db.Boolean, nullable=False, default=False, server_default=sa.text("false")
    )

    def __repr__(self):
        return f'<Competitor uid={self.uid} kind={self.kind}>'


@sa.event.listens_for(Competitor, 'before_insert')
def _require_tournament(mapper, connection, target):  # noqa: ARG001
    """Fail in Python, not in the database.

    Identities are now created at competitor construction time, before
    ``tournament_id`` is necessarily known, so the NOT NULL constraint is
    reachable by ordinary application code.  A named error beats
    ``null value in column "tournament_id" violates not-null constraint`` on a
    table the caller never mentioned.
    """
    if target.tournament_id is None:
        raise ValueError(
            'Competitor identity has no tournament_id. The competitor row it '
            'belongs to was built without one and never got one.'
        )


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

    Two paths, and why
    ==================
    ``_make_identity`` handles anything built through ``__init__``, which is
    every ORM caller in this repo.  It runs at construction so that contact
    fields, which are association proxies onto the identity, are assignable in
    the constructor call itself.  The identity is a real ORM object from that
    moment, and the unit of work inserts it ahead of the competitor row and syncs
    the uid across.

    ``_allocate`` is the floor under that.  ``__init__`` does not fire for
    instances SQLAlchemy builds internally (``merge``, deserialization), and
    nothing stops a future caller from bypassing it.  Any competitor row that
    reaches an INSERT without a uid still gets one, through Core, against the
    live connection.  It is not dead code, it is the case that is not supposed to
    happen and must not corrupt anything when it does.
    """

    @sa.event.listens_for(model, 'init')
    def _make_identity(target, args, kwargs):  # noqa: ARG001
        if 'uid' in kwargs or 'identity' in kwargs:
            # The caller is wiring identity by hand.
            # proam_regression/stage_multitournament.py does exactly this.
            return
        identity = Competitor(kind=kind,
                              tournament_id=kwargs.get('tournament_id'))
        # Order matters and it is not obvious. The declarative constructor
        # applies kwargs in dict order, so if 'identity' were simply appended it
        # would be set AFTER phone/email/address and those writes would have had
        # nothing to write through. Rebuild the dict with identity first.
        reordered = {'identity': identity}
        reordered.update(kwargs)
        kwargs.clear()
        kwargs.update(reordered)

    @sa.event.listens_for(model.tournament_id, 'set')
    def _sync_identity_tournament(target, value, oldvalue, initiator):  # noqa: ARG001
        # Covers the build-empty-then-fill-in pattern: ProCompetitor() followed
        # by comp.tournament_id = 4. Read the identity out of __dict__ rather
        # than through the attribute so a persistent row does not emit a lazy
        # load in the middle of an attribute set.
        identity = target.__dict__.get('identity')
        if identity is not None and identity.tournament_id is None:
            identity.tournament_id = value

    @sa.event.listens_for(model.uid, 'set')
    def _adopt_identity(target, value, oldvalue, initiator):  # noqa: ARG001
        # `p = ProCompetitor(...)` followed by `p.uid = existing.uid`.  Without
        # this, the identity auto-created at construction is still the one the
        # relationship points at, and the relationship wins the flush: the uid
        # the caller asked for gets silently overwritten with the auto one.
        # Nothing in the app does this today, but the contract predates the
        # contact move (tests/test_identity_migration.py) and a silent
        # overwrite is the worst possible way to break it.
        if value is None:
            return
        identity = target.__dict__.get('identity')
        if identity is None or identity.uid == value:
            # None is the `_allocate` path below, which sets uid during flush on
            # an instance that never had an identity object to begin with.
            return
        session = sa.orm.object_session(target)
        real = (session.get(Competitor, value) if session is not None
                else db.session.get(Competitor, value))
        if real is None:
            raise ValueError(
                f'{model.__name__}.uid was set to {value}, but no competitor '
                f'identity with that uid exists yet. Flush the identity first, '
                f'or pass uid= to the constructor.'
            )
        target.identity = real

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
