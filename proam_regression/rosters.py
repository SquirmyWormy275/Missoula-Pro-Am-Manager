"""Read heat rosters back out of the database, off ``heat_assignments``.

D12-C commit F1. Every test module in this package used to ask the same
question the same way: ``SELECT competitors FROM heats``, then ``json.loads``
with an ``isinstance(str)`` guard because psycopg hands a Text column back as
a string and SQLite sometimes does not. That idiom appeared a dozen times and
each copy had to be right about the same three things.

As of D12-C commit E the roster is ``heat_assignments`` rows and the JSON
column was a rendering of them; commit F3 dropped the column. So the idiom had
to move, and moving it once into shared helpers is cheaper and safer than
moving twelve copies of it and hoping they agree.

Two things these helpers guarantee that the JSON reads could not:

**Running order.** ``ORDER BY heat_assignments.id`` is the order the rows were
written, which ``Heat.set_roster`` writes in roster order, which is the order
the judge sheet prints. This is the same order the JSON column carried, which
is why the pinned rosters in ``test_college_id_reseed`` and
``test_multitournament`` still compare equal against the c35 pins.

**Identity.** A row carries ``competitor_type``, so ``competitor_id`` plus type
names exactly one person. The JSON column held bare integers, and before the
c39 reseed the pro and college id sequences overlapped, so an id alone was
ambiguous. Callers that know the kind should pass ``kind=``; the default of
None reproduces the old, looser behaviour exactly.
"""


def heat_roster(sql, heat_id, kind=None):
    """One heat's roster: a list of competitor ids in running order."""
    clause = " AND a.competitor_type = :k" if kind else ""
    params = {"h": heat_id}
    if kind:
        params["k"] = kind
    return [r[0] for r in sql(
        "SELECT a.competitor_id FROM heat_assignments a "
        "WHERE a.heat_id = :h" + clause + " ORDER BY a.id", **params)]


def heat_stands(sql, heat_id):
    """One heat's stand map, ``{str(competitor_id): stand_number}``.

    Keyed by string and omitting a competitor with no stand, which is the
    shape ``Heat.get_stand_assignments`` returns and the shape the
    ``stand_assignments`` column had for as long as it existed.
    """
    return {str(cid): stand for cid, stand in sql(
        "SELECT a.competitor_id, a.stand_number FROM heat_assignments a "
        "WHERE a.heat_id = :h ORDER BY a.id", h=heat_id)
        if stand is not None}


def event_rosters(sql, event_id):
    """Every heat in an event: a list of rosters, one per heat.

    Heats come back in ``heat_number, run_number`` order and each roster is in
    running order. An empty heat contributes an empty list rather than
    disappearing, because a generator that produced an empty heat is a defect
    several tests in this package exist to catch, and a helper that silently
    dropped it would hide the thing being measured.
    """
    rows = sql(
        "SELECT h.id, a.competitor_id "
        "  FROM heats h LEFT JOIN heat_assignments a ON a.heat_id = h.id "
        " WHERE h.event_id = :e "
        " ORDER BY h.heat_number, h.run_number, a.id", e=event_id)
    out = {}
    for heat_id, comp_id in rows:
        roster = out.setdefault(heat_id, [])
        if comp_id is not None:
            roster.append(comp_id)
    return list(out.values())
