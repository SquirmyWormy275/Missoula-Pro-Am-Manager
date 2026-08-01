"""Typed competitor reference for the identity transition.

The problem this exists for
===========================
``EventResult.competitor_id`` and every integer inside the ``Heat.competitors``
JSON blob is a bare int with no foreign key.  A sibling ``competitor_type``
string is the only thing that says which of the two competitor tables the
integer points at.  In the production mirror, college ids 29 through 49 all
also exist as pro ids: 21 live collisions.  A code path that reads the integer
and forgets the discriminator addresses the wrong person, and nothing in the
type system or the database stops it.

``EntityKey`` is the transitional wrapper.  Anywhere a bare int currently
flows, an ``EntityKey`` forces the caller to state the kind, and the pair
becomes a single value that can be compared, hashed, put in a set, and used as
a dict key without the discriminator getting separated from the integer.

The permanent fix is ``competitors.uid``: one integer that is unique across both
disciplines, with real foreign keys.  ``EntityKey`` is what carries the legacy
pair safely until every call site has been converted to a uid.  ``resolve_uid``
is the bridge between the two.

Who imports this
================
``models/heat.py``, as of D12-C commit A.  ``Heat.sync_assignments`` builds one
``EntityKey`` per id in a heat's competitors JSON and calls ``resolve_uids`` to
turn them into the ``heat_assignments.uid`` values the table now requires.

It landed a cycle earlier than that with no importer at all, deliberately, so
its semantics could be reviewed on their own rather than in the middle of a
route conversion.  That is the only reason this section exists: the first
version of this docstring said "imported by nothing", and a line like that goes
stale the moment it stops being true.

Deliberately not here
=====================
No ORM object loading.  ``resolve_uid`` returns an integer or ``None`` and does
not touch the identity table beyond a single scalar lookup.  Anything that wants
the competitor row can query for it with the kind it already has.
"""
from dataclasses import dataclass

COLLEGE = 'college'
PRO = 'pro'

VALID_KINDS = frozenset({COLLEGE, PRO})

# Legacy `competitor_type` values map straight through; both tables already use
# these exact strings.  The mapping is written out rather than assumed so that a
# third discipline added later fails loudly here instead of silently resolving
# to one of the existing two.
_TABLE_FOR_KIND = {
    COLLEGE: 'college_competitors',
    PRO: 'pro_competitors',
}


@dataclass(frozen=True)
class EntityKey:
    """A competitor reference that cannot lose its discriminator.

    ``frozen=True`` gives hashability and equality for free, which is the whole
    point: a bare int and its ``competitor_type`` can be separated by a refactor,
    a JSON round trip, or a function signature.  An ``EntityKey`` cannot.

    ``kind`` is validated on construction.  An invalid kind is a programming
    error, not a data condition, so it raises rather than returning a sentinel.
    """

    kind: str
    id: int

    def __post_init__(self):
        if self.kind not in VALID_KINDS:
            raise ValueError(
                f'EntityKey kind must be one of {sorted(VALID_KINDS)}, '
                f'got {self.kind!r}'
            )
        if not isinstance(self.id, int) or isinstance(self.id, bool):
            raise TypeError(
                f'EntityKey id must be an int, got {type(self.id).__name__}'
            )

    @property
    def is_college(self):
        return self.kind == COLLEGE

    @property
    def is_pro(self):
        return self.kind == PRO

    @property
    def table_name(self):
        """The legacy table this key addresses."""
        return _TABLE_FOR_KIND[self.kind]

    @classmethod
    def from_legacy(cls, competitor_id, competitor_type):
        """Build a key from the bare-int pair the codebase currently passes around.

        Returns ``None`` when either half is missing, because that pair is
        genuinely absent in existing rows (an EventResult can carry a NULL
        competitor_id) and callers converting a legacy path should get a value
        they can test rather than an exception they have to guard.
        """
        if competitor_id is None or competitor_type is None:
            return None
        return cls(kind=str(competitor_type), id=int(competitor_id))

    def as_legacy(self):
        """Back to the ``(competitor_id, competitor_type)`` pair."""
        return self.id, self.kind

    def __str__(self):
        return f'{self.kind}:{self.id}'


def resolve_uid(session, key):
    """Return the ``competitors.uid`` for an ``EntityKey``, or ``None``.

    ``None`` means the row exists but has no identity, or the row does not
    exist.  Those are different conditions and a caller that needs to tell them
    apart should query the source table directly; this function answers only
    "what is the durable id for this legacy reference".

    Uses a raw scalar select against the source table rather than the ORM so it
    can be called with any session, including one already mid-flush, and so it
    costs one round trip rather than a full object load.
    """
    if key is None:
        return None

    import sqlalchemy as sa

    table = _TABLE_FOR_KIND[key.kind]
    row = session.execute(
        sa.text(f'SELECT uid FROM {table} WHERE id = :id'),
        {'id': key.id},
    ).first()
    return row[0] if row else None


def resolve_uids(session, keys):
    """Batch form of :func:`resolve_uid`.

    Returns a dict of ``EntityKey -> uid``.  Keys that do not resolve are absent
    from the result rather than mapped to ``None``, so ``len(result)`` is a
    direct count of how many resolved and the caller cannot accidentally treat
    an unresolved key as identified.

    Groups by kind so this is at most two queries regardless of how many keys
    are passed, which matters because the natural call site is a heat or an
    event result set.
    """
    import sqlalchemy as sa

    out = {}
    by_kind = {}
    for key in keys:
        if key is not None:
            by_kind.setdefault(key.kind, []).append(key.id)

    for kind, ids in by_kind.items():
        table = _TABLE_FOR_KIND[kind]
        rows = session.execute(
            sa.text(f'SELECT id, uid FROM {table} WHERE id IN :ids').bindparams(
                sa.bindparam('ids', expanding=True)
            ),
            {'ids': ids},
        ).all()
        for row_id, uid in rows:
            if uid is not None:
                out[EntityKey(kind=kind, id=row_id)] = uid

    return out
