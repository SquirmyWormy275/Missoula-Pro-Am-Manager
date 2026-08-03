"""Refuse to save an event blob that introduces a bad competitor reference.

Why this exists
===============
``scripts/repair_era1_references.py`` cleaned up 55 references that pointed at
the wrong human. Nothing stops the next one. The c38 reseed created those by
renumbering the college pool while three JSON blobs kept the old numbers, and
the damage sat undetected for months because a stale id lands on a live *pro*
and reads back as a plausible name rather than an error.

This module is the write-time half of G1-C. ``services/reference_audit.py``
reports; this refuses.

What it refuses
===============
A save is refused when the blob being written contains a competitor reference
that (a) does not resolve in the pool its position implies, and (b) was not
already in the blob's previous value.

Both halves matter.

The first half is :func:`services.reference_audit.check_blob`, which classifies
a reference as ``DANGLING`` (the integer is in neither pool) or ``CROSS_KIND``
(it is a live competitor of the *other* discipline). ``UNKNOWN_KIND`` is logged
and allowed: it means the discipline could not be determined at all, and
refusing a save on the strength of a value the audit itself declines to
classify would be guessing in the direction of an outage.

The second half is what makes this safe to install on a database that still has
legacy damage. Every production mirror carries the 55 era-1 ghosts until the
repair script has run against it, and a gate that judged the whole blob would
refuse every save touching events 28, 29 and 44 including the saves that would
have fixed them. So the comparison is against the previous value of the same
column: an id that was already bad in this blob stays allowed no matter where
in the blob it moves, and only an id that this save *brings in* is refused.

The forgiveness is keyed on the id, not on the JSON path. A bracket save
rewrites paths wholesale as matches advance, so a path-keyed rule would read a
legacy ghost sliding from ``winners[0][2]`` to ``winners[1][1]`` as a brand new
defect and refuse a save that changed nothing about it.

Where it sits
=============
One ``before_flush`` listener on the session, not a call at each of the eight
places that assign to ``Event.payouts`` or ``Event.event_state``. Eight call
sites is eight chances to add a ninth and forget, and two independent
enumerations of the same thing is the exact failure that produced the era-1
ghosts in the first place. The listener cannot be bypassed by a write path that
does not know it exists.

It prefers SQLAlchemy's attribute history for the old value, and falls back to
a direct read of the row when history has nothing. The fallback is not
optional. A column attribute records no old value unless it was loaded before
being assigned, and ``commit()`` expires every attribute on every object in the
session, so the second save in a request that already committed once reads as
"there was nothing here before" and the forgiveness silently evaporates. That
is precisely the shape of the relay and partnered-axe write paths, which hold
one ``Event`` across repeated ``_save_state(commit=True)`` calls. The fallback
query runs only when the new value already looks bad, so a clean save still
costs nothing.

What it does not do
===================
It does not repair. A refusal raises :class:`BadReferenceWrite` and the flush
fails; the caller decides what to tell the user. Silently correcting a save
would put this module in the business of guessing which competitor was meant,
and that decision belongs to a human reading a ``--check`` report.

It does not clean up after a refusal either. The raise happens before any SQL
is emitted, so the transaction is intact, but the offending assignment is still
pending on the object and the next flush, including the autoflush in front of
the next query, raises the same error somewhere that has nothing to do with it.
A caller that catches :class:`BadReferenceWrite` must roll back.

It does not cover the other reference stores. The two ``heats`` JSON columns
were D12-C's territory and have stopped being writable truth: as of commit E
the roster is ``heat_assignments`` and the columns are a rendering of it, as of
commit F2 nothing reads them, and F3 drops them.  ``heat_assignments`` gained a
real foreign key onto ``competitors.uid`` in D12-C commit A, and
``Heat.set_roster`` refuses a bad reference in Python before the constraint
can, so that store is now defended by the database rather than by anything
here. ``event_results.competitor_id`` is still a bare integer
with a CHECK on its discipline column and no foreign key of its own; it is
covered by ``services/reference_audit.py`` and by nothing at write time. This
gate is scoped to the two stores where the damage happened.

An earlier version of this paragraph said ``heat_assignments`` and
``event_results`` both carried a foreign key. Neither did. The migration named
above makes it true of the first one; the second is still owed, and saying so
is more use than the sentence that claimed it was already done.
"""
import json
import logging

import sqlalchemy as sa
from sqlalchemy import event as sa_event
from sqlalchemy import inspect as sa_inspect

from services.reference_audit import UNKNOWN_KIND, check_blob

logger = logging.getLogger(__name__)

#: The two columns this gate watches, and the store name each reports as.
GATED_COLUMNS = ('payouts', 'event_state')


class BadReferenceWrite(Exception):
    """A save that would have introduced a reference to the wrong competitor.

    Carries the findings so a route can render them. ``str()`` is one line per
    finding, because the operator seeing this needs to know which position and
    which id, not that "validation failed".
    """

    def __init__(self, event_id, column, findings):
        self.event_id = event_id
        self.column = column
        self.findings = findings
        detail = '; '.join(
            f'{f.site.path} -> {f.site.raw_id} ({f.verdict}'
            + (f', which is {f.collides_with!r} in the other pool'
               if f.collides_with else '')
            + ')'
            for f in findings)
        super().__init__(
            f'refusing to save events.{column} for event {event_id}: '
            f'{len(findings)} new bad competitor reference(s): {detail}')


def _decode(raw):
    """A blob column's value as JSON, or ``None`` if there is nothing usable.

    A value that will not parse is not this module's problem. Reporting it as a
    reference defect would be a lie about what is wrong, and corrupt-blob
    detection is D13-C's job.
    """
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _bad_ids(session, blob, root_path, event_type):
    """The raw ids that already fail in ``blob``. Empty for a blob that is
    ``None``, which is how a brand new row reads."""
    if blob is None:
        return set()
    return {f.site.raw_id for f in check_blob(session, blob, root_path,
                                              event_type)
            if f.verdict != UNKNOWN_KIND}


def _previous_value(session, event, column, history):
    """The column's value before this save, as a raw string, or ``None``.

    ``history.deleted`` is authoritative when it has anything, because it is
    the value this session actually loaded. It is empty in two very different
    situations and they must not be conflated: the row is new and there is
    genuinely no previous value, or the attribute was expired (which
    ``commit()`` does to everything) and SQLAlchemy never bothered to fetch the
    old value before overwriting it. Only the second one needs a query, and
    only that one is a trap.
    """
    if history.deleted:
        return history.deleted[0]
    state = sa_inspect(event)
    if state.pending or state.transient or event.id is None:
        return None
    from models import Event
    table = Event.__table__
    stmt = sa.select(table.c[column]).where(table.c.id == sa.bindparam('id'))
    return session.execute(stmt, {'id': event.id}).scalar()


def check_pending(session, event, column):
    """The findings that make this pending write unacceptable. Empty is a pass.

    ``event`` is a dirty or new ``Event``. Returns ``[]`` when the column was
    not touched by this flush, which is the usual case.
    """
    history = sa_inspect(event).attrs[column].history
    if not history.has_changes():
        return []

    new_blob = _decode(history.added[0] if history.added else None)
    if new_blob is None:
        return []

    root_path = f'e{event.id}.{column}'
    findings = [f for f in check_blob(session, new_blob, root_path,
                                      event.event_type)
                if f.verdict != UNKNOWN_KIND]
    if not findings:
        return []

    old_blob = _decode(_previous_value(session, event, column, history))
    already_bad = _bad_ids(session, old_blob, root_path, event.event_type)
    return [f for f in findings if f.site.raw_id not in already_bad]


def _gate(session, _flush_context, _instances):
    from models import Event

    for obj in list(session.new) + list(session.dirty):
        if not isinstance(obj, Event):
            continue
        for column in GATED_COLUMNS:
            findings = check_pending(session, obj, column)
            if findings:
                raise BadReferenceWrite(obj.id, column, findings)


def is_installed(session_factory):
    """Whether the gate is currently armed on this session factory.

    Exists so a test fixture can disarm the gate and put it back the way it
    found it rather than the way it assumes it was. A module with no app
    fixture has no gate, and re-arming one there would leave a listener on the
    global scoped session for every module that ran after it.
    """
    return sa_event.contains(session_factory, 'before_flush', _gate)


def install(session_factory):
    """Attach the gate to a session factory. Idempotent.

    Called from the app factory. Idempotent because the test suite builds many
    apps against the one scoped session and a listener registered twice would
    raise twice and report the same defect twice.
    """
    if not sa_event.contains(session_factory, 'before_flush', _gate):
        sa_event.listen(session_factory, 'before_flush', _gate)


def uninstall(session_factory):
    """Detach the gate. For tests that need to write a broken blob on purpose,
    which is the only legitimate reason to want this off."""
    if sa_event.contains(session_factory, 'before_flush', _gate):
        sa_event.remove(session_factory, 'before_flush', _gate)
