"""Birling brackets graduate to real tables (D13-C, commit A1).

Revision ID: u0c4d5e6f7a8
Revises: t9b3c4d5e6f7
Create Date: 2026-08-03

Creates the five tables declared in ``models/birling.py`` and fills them from
``events.payouts``, which is where a birling bracket has lived since the app
was written: a Text column named for prize money, holding a JSON document of
about five and a half kilobytes per event.

This revision changes no behaviour. Nothing in the tree reads these tables
yet. The JSON is still the truth, ``BirlingBracket`` still reads and writes it,
and every route, template and print path behaves exactly as it did. Commit A2
makes the service write both stores, A3 moves the readers onto the rows, A4
drops the container. That is the ladder D12-C used for heat rosters, for the
same reason: a bracket is live judge state and there is no sitting in which it
can be unavailable.

What gets read out of the document
==================================
``seeding`` becomes ``birling_seeds``, one row per entrant at one seed
position. ``competitors`` becomes nothing: it is the same population with a
cached display name, and on both production brackets it is the seed order
element for element. ``pre_seedings`` becomes ``birling_pre_seeds``, which is a
separate table because it is an input to bracket generation and routinely
exists on an event that has no bracket at all. Every match dict in
``bracket.winners``, ``bracket.losers``, ``bracket.finals`` and
``bracket.true_finals`` becomes a ``birling_matches`` row, its ``falls`` list
becomes ``birling_falls`` rows, and ``placements`` becomes
``birling_placements``.

``current_round``, ``eliminated_position`` and ``round`` are dropped on the
floor. The model docstring carries the evidence for each: the first is written
once and read nowhere, the second is created as null and never assigned by
anything, the third is ``side`` and ``round_index`` spelled as one string.

Why an event with an unresolvable reference is skipped and not refused
=====================================================================
Every competitor reference in the document is a bare integer, and the pool it
indexes is implied by the event's ``event_type``. Resolving it against
``college_competitors`` or ``pro_competitors`` gives the ``competitors.uid``
the new columns hold. On the 2026 production data that resolution fails a lot:
both stored brackets carry pre-reseed college ids, 20 references across the
two events, left behind by the c38 reseed which preserved era-1 history
bit-for-bit rather than guessing at it. Those integers name nothing in the
college pool and name live pro competitors in the pro pool, which is the whole
reason this table wants a foreign key.

The first draft of this revision refused to run at all in that state, on the
precedent of ``t9b3c4d5e6f7``, and pointed the operator at
``scripts/repair_era1_references.py``. That is wrong here, and the regression
harness is what proves it. ``proam_regression/test_college_id_reseed.py`` pins
its clones to the archival ``proam_prod_mirror_2026pristine`` snapshot and
asserts that the era-1 ghost count does not move, because the reseed's contract
is that it neither creates nor destroys one. A migration that refuses to run on
ghosted data makes that snapshot un-upgradable, which retires the only test
that measures the defect. The defect has to stay measurable, so the migration
has to stay runnable on it.

So: an event whose document carries a reference this revision cannot resolve
gets no rows, and the count of such events is printed. Nothing is lost, because
the JSON is still the truth and stays untouched. Nothing wrong is written,
because a reference is either resolved or the whole event is left alone. The
repair runs on its own schedule, under a human, which is what
``scripts/repair_era1_references.py`` was built for and why it is a script and
not a migration.

The refusal moves to commit A3. That is the commit where an empty table becomes
a missing bracket, and by then the repair is a prerequisite rather than a
suggestion.

What counts as unresolvable
===========================
A reference that is not an integer, or that names nobody in the event's own
pool. Also, at the event level: an entrant list and a seed order that disagree
about who is in the bracket, a seed number repeated, a competitor seeded twice,
a match name repeated, two matches claiming one slot, a fall numbered outside
one to three, and a placement below one. Each of those would arrive as a bare
IntegrityError from the driver halfway through a table load, naming a
constraint instead of an event. Checking them here means the operator gets a
count of events and the reason, and the rest of the load still happens.

The scan reports counts and event ids and never a competitor id or a name. A
bracket is a list of integers and harmless on its own, but this log is shared
and the habit of not printing production rows into it is worth more than the
convenience.

Why the parsing is Python and not SQL
=====================================
``json_each`` and ``jsonb_array_elements`` are not the same function, a birling
document is four levels deep, and a loader that has to be right on both SQLite
and Postgres is cheaper to write once in Python than twice in dialect SQL. It
is one pass over ``events``, which is 44 rows on the production mirror and 88
on the multi-tournament oracle.

Downgrade
=========
Drops the five tables. Nothing else in the schema references them and the JSON
was never modified, so a downgrade loses exactly the rows this revision
created, which are a projection of a document that is still sitting there. A
downgrade followed by an upgrade is lossless.
"""
import json
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "u0c4d5e6f7a8"
down_revision = "t9b3c4d5e6f7"
branch_labels = None
depends_on = None


# Must match models/_types.py exactly or tests/test_migration_integrity.py
# reports a type mismatch between the create_all schema and the upgrade schema.
BIG_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

_POOL_TABLE = {"college": "college_competitors", "pro": "pro_competitors"}

_FALL_INSERT = sa.text(
    "INSERT INTO birling_falls "
    "(match_row_id, fall_number, winner_uid, recorded_at) "
    "VALUES (:m, :n, :w, :t)"
).bindparams(sa.bindparam("t", type_=sa.DateTime()))


def _parse(raw):
    """Parse a ``payouts`` value into a dict, forgiving what it must.

    The column is ``NOT NULL`` with a default of ``'{}'`` and has held null on
    databases predating that, empty string from at least one legacy import
    path, and a JSON list on nothing at all so far. None of those is a bracket
    and none of them is an error, so all of them come back empty.
    """
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _timestamp(raw):
    """Parse a fall's ISO timestamp into a naive UTC datetime, or None.

    ``record_fall`` writes ``datetime.now(timezone.utc).isoformat()``, which is
    offset-aware, and the column is a plain ``DateTime`` like every other
    timestamp in this schema. An unparseable value comes back as None rather
    than failing the event: the column is nullable and a timestamp is not
    identity.
    """
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _pools(connection):
    """``{'college': {id: uid}, 'pro': {id: uid}}`` for the whole roster."""
    pools = {}
    for kind, table in _POOL_TABLE.items():
        pools[kind] = {
            row[0]: row[1]
            for row in connection.execute(
                sa.text(f"SELECT id, uid FROM {table}")
            ).fetchall()
        }
    return pools


class _Plan:
    """The rows one event's document turns into, and why it might turn into none.

    Every ``_ref`` call either resolves or files a reason. Nothing is written
    until the whole document has been walked, so an event contributes all of
    its rows or none of them, and the reason the operator sees names the class
    of defect rather than the constraint that would have tripped.
    """

    def __init__(self, event_id, pool):
        self.event_id = event_id
        self.pool = pool
        self.reasons = set()
        self.seeds = []
        self.pre_seeds = []
        self.matches = []
        self.placements = []

    def fail(self, reason):
        self.reasons.add(reason)

    def _ref(self, raw, required):
        """Resolve one bare competitor id to a uid.

        ``required=False`` means an empty slot is legitimate, which is the case
        for all four competitor columns on a match: a later round's slots are
        empty until the round feeding them is decided, a bye has no second
        competitor, and an undecided match has no winner.
        """
        if raw is None:
            if required:
                self.fail("a required competitor slot is empty")
            return None
        if isinstance(raw, bool) or not isinstance(raw, int):
            self.fail("a competitor reference is not an integer")
            return None
        uid = self.pool.get(raw)
        if uid is None:
            self.fail("a competitor reference names nobody in the event pool")
        return uid


def _plan_seeds(plan, doc):
    """``seeding`` and ``competitors`` become ``birling_seeds``."""
    seeding = doc.get("seeding") or []
    if not isinstance(seeding, list):
        plan.fail("the seed order is not a list")
        return

    entrants = doc.get("competitors") or []
    if isinstance(entrants, list):
        listed = [c.get("id") for c in entrants if isinstance(c, dict)]
        if len(listed) != len(entrants):
            plan.fail("the entrant list holds something that is not a competitor")
        elif seeding and set(listed) != set(seeding):
            plan.fail("the entrant list and the seed order name different people")
    else:
        plan.fail("the entrant list is not a list")

    seen = set()
    for position, raw in enumerate(seeding, start=1):
        uid = plan._ref(raw, required=True)
        if uid is None:
            continue
        if uid in seen:
            plan.fail("a competitor holds two seeds")
            continue
        seen.add(uid)
        plan.seeds.append({"seed_number": position, "uid": uid})


def _plan_pre_seeds(plan, doc):
    """``pre_seedings`` becomes ``birling_pre_seeds``."""
    pre = doc.get("pre_seedings") or {}
    if not isinstance(pre, dict):
        plan.fail("the pre-seeding map is not a map")
        return

    seen_uid = set()
    seen_number = set()
    for key, number in pre.items():
        try:
            raw = int(key)
        except (TypeError, ValueError):
            plan.fail("a pre-seeding is keyed by something that is not an id")
            continue
        uid = plan._ref(raw, required=True)
        if uid is None:
            continue
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            plan.fail("a pre-seeding holds something that is not a seed number")
            continue
        if uid in seen_uid or number in seen_number:
            plan.fail("a pre-seeding repeats a competitor or a seed number")
            continue
        seen_uid.add(uid)
        seen_number.add(number)
        plan.pre_seeds.append({"uid": uid, "seed_number": number})


def _plan_match(plan, side, round_index, position, match, seen_name, seen_slot):
    """One match dict becomes a ``birling_matches`` row and its falls."""
    if not isinstance(match, dict):
        plan.fail("a match slot holds something that is not a match")
        return

    name = match.get("match_id")
    if not isinstance(name, str) or not name or len(name) > 20:
        plan.fail("a match has no usable name")
        return
    if name in seen_name:
        plan.fail("two matches share a name")
        return
    seen_name.add(name)

    slot = (side, round_index, position)
    if slot in seen_slot:
        plan.fail("two matches claim one slot")
        return
    seen_slot.add(slot)

    row = {
        "match_id": name,
        "side": side,
        "round_index": round_index,
        "position": position,
        "competitor1_uid": plan._ref(match.get("competitor1"), required=False),
        "competitor2_uid": plan._ref(match.get("competitor2"), required=False),
        "winner_uid": plan._ref(match.get("winner"), required=False),
        "loser_uid": plan._ref(match.get("loser"), required=False),
        "is_bye": bool(match.get("is_bye", False)),
        # Null everywhere but the true finals, because false would claim the
        # question of a second grand final had been asked and answered.
        "needed": (bool(match.get("needed", False))
                   if side == "true_finals" else None),
        "falls": [],
    }

    falls = match.get("falls") or []
    if not isinstance(falls, list):
        plan.fail("a match holds a fall list that is not a list")
        return

    seen_fall = set()
    for index, fall in enumerate(falls, start=1):
        if not isinstance(fall, dict):
            plan.fail("a fall is not a fall")
            continue
        number = fall.get("fall_number", index)
        if isinstance(number, bool) or not isinstance(number, int):
            plan.fail("a fall has no usable number")
            continue
        if number < 1 or number > 3:
            # Birling is best of three. A fourth fall is not a fall.
            plan.fail("a fall is numbered outside one to three")
            continue
        if number in seen_fall:
            plan.fail("a match records one fall number twice")
            continue
        seen_fall.add(number)
        winner = plan._ref(fall.get("winner"), required=True)
        if winner is None:
            continue
        row["falls"].append({
            "fall_number": number,
            "winner_uid": winner,
            "recorded_at": _timestamp(fall.get("recorded_at")),
        })

    plan.matches.append(row)


def _plan_matches(plan, doc):
    """The whole ``bracket`` subtree becomes ``birling_matches``."""
    bracket = doc.get("bracket") or {}
    if not isinstance(bracket, dict):
        plan.fail("the bracket is not a bracket")
        return

    seen_name = set()
    seen_slot = set()

    for side in ("winners", "losers"):
        rounds = bracket.get(side) or []
        if not isinstance(rounds, list):
            plan.fail("a bracket side is not a list of rounds")
            continue
        for round_index, matches in enumerate(rounds):
            if not isinstance(matches, list):
                plan.fail("a bracket round is not a list of matches")
                continue
            for position, match in enumerate(matches, start=1):
                _plan_match(plan, side, round_index, position, match,
                            seen_name, seen_slot)

    # The two finals are singletons rather than rounds, so they sit at round
    # zero, position one, distinguished from each other by ``side``.
    for side in ("finals", "true_finals"):
        match = bracket.get(side)
        if match is None:
            continue
        _plan_match(plan, side, 0, 1, match, seen_name, seen_slot)


def _plan_placements(plan, doc):
    """``placements`` becomes ``birling_placements``.

    Position is not unique inside an event and is not checked for uniqueness
    here. The grand finals write one and two while the losers bracket has
    already written the same numbers downward from the field size, and
    reconciling that is the service's business.
    """
    placements = doc.get("placements") or {}
    if not isinstance(placements, dict):
        plan.fail("the placement map is not a map")
        return

    seen = set()
    for key, position in placements.items():
        try:
            raw = int(key)
        except (TypeError, ValueError):
            plan.fail("a placement is keyed by something that is not an id")
            continue
        uid = plan._ref(raw, required=True)
        if uid is None:
            continue
        if isinstance(position, bool) or not isinstance(position, int) or position < 1:
            plan.fail("a placement holds something that is not a position")
            continue
        if uid in seen:
            plan.fail("a competitor is placed twice")
            continue
        seen.add(uid)
        plan.placements.append({"uid": uid, "position": position})


def _write(connection, plan):
    """Write one planned event's rows."""
    event_id = plan.event_id

    for seed in plan.seeds:
        connection.execute(
            sa.text("INSERT INTO birling_seeds (event_id, seed_number, uid) "
                    "VALUES (:e, :n, :u)"),
            {"e": event_id, "n": seed["seed_number"], "u": seed["uid"]})

    for pre in plan.pre_seeds:
        connection.execute(
            sa.text("INSERT INTO birling_pre_seeds (event_id, uid, seed_number) "
                    "VALUES (:e, :u, :n)"),
            {"e": event_id, "u": pre["uid"], "n": pre["seed_number"]})

    for match in plan.matches:
        connection.execute(
            sa.text(
                "INSERT INTO birling_matches "
                "(event_id, match_id, side, round_index, position, "
                " competitor1_uid, competitor2_uid, winner_uid, loser_uid, "
                " is_bye, needed) "
                "VALUES (:e, :m, :s, :r, :p, :c1, :c2, :w, :l, :bye, :needed)"),
            {"e": event_id, "m": match["match_id"], "s": match["side"],
             "r": match["round_index"], "p": match["position"],
             "c1": match["competitor1_uid"], "c2": match["competitor2_uid"],
             "w": match["winner_uid"], "l": match["loser_uid"],
             "bye": match["is_bye"], "needed": match["needed"]})

        if not match["falls"]:
            continue

        # Read the id back rather than using RETURNING: SQLite only learned it
        # in 3.35 and this loader has to be right on both engines. The pair is
        # unique, so this is one row.
        row_id = connection.execute(
            sa.text("SELECT id FROM birling_matches "
                    "WHERE event_id = :e AND match_id = :m"),
            {"e": event_id, "m": match["match_id"]}).scalar()

        for fall in match["falls"]:
            connection.execute(
                _FALL_INSERT,
                {"m": row_id, "n": fall["fall_number"], "w": fall["winner_uid"],
                 "t": fall["recorded_at"]})

    for placement in plan.placements:
        connection.execute(
            sa.text("INSERT INTO birling_placements (event_id, uid, position) "
                    "VALUES (:e, :u, :p)"),
            {"e": event_id, "u": placement["uid"], "p": placement["position"]})


def _backfill(connection):
    """Fill the five tables from every event that carries a bracket document."""
    pools = _pools(connection)

    events = connection.execute(sa.text(
        "SELECT id, event_type, payouts FROM events ORDER BY id"
    )).fetchall()

    loaded = 0
    skipped = []
    reasons = set()

    for event_id, event_type, payouts in events:
        doc = _parse(payouts)
        if not any(key in doc for key in ("bracket", "seeding", "pre_seedings")):
            continue

        pool = pools.get(event_type)
        if pool is None:
            skipped.append(event_id)
            reasons.add("the event names no competitor pool")
            continue

        plan = _Plan(event_id, pool)
        _plan_seeds(plan, doc)
        _plan_pre_seeds(plan, doc)
        _plan_matches(plan, doc)
        _plan_placements(plan, doc)

        if plan.reasons:
            skipped.append(event_id)
            reasons |= plan.reasons
            continue

        _write(connection, plan)
        loaded += 1

    if skipped:
        shown = ", ".join(str(e) for e in skipped[:20])
        more = f" (and {len(skipped) - 20} more)" if len(skipped) > 20 else ""
        print(
            f"u0c4d5e6f7a8: loaded {loaded} birling brackets into rows and "
            f"left {len(skipped)} alone.\n"
            f"  event ids not loaded: {shown}{more}\n"
            f"  reasons: {'; '.join(sorted(reasons))}\n"
            "  Their JSON is untouched and is still what the app reads, so "
            "nothing is lost and nothing is broken. Run "
            "scripts/repair_era1_references.py --check to see what the "
            "unresolvable references are, and --apply to repair them under a "
            "human. Commit A3, which moves the readers onto these rows, is "
            "where an unloaded bracket stops being harmless."
        )
    elif loaded:
        print(f"u0c4d5e6f7a8: loaded {loaded} birling brackets into rows.")


def upgrade():
    op.create_table(
        "birling_seeds",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("seed_number", sa.Integer(), nullable=False),
        sa.Column("uid", BIG_ID, nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"]),
        sa.ForeignKeyConstraint(["uid"], ["competitors.uid"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "seed_number",
                            name="uq_birling_seeds_event_seed"),
        sa.UniqueConstraint("event_id", "uid",
                            name="uq_birling_seeds_event_uid"),
        sa.CheckConstraint("seed_number >= 1",
                           name="ck_birling_seeds_seed_positive"),
    )

    op.create_table(
        "birling_pre_seeds",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("uid", BIG_ID, nullable=False),
        sa.Column("seed_number", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"]),
        sa.ForeignKeyConstraint(["uid"], ["competitors.uid"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "uid",
                            name="uq_birling_pre_seeds_event_uid"),
        sa.UniqueConstraint("event_id", "seed_number",
                            name="uq_birling_pre_seeds_event_seed"),
        sa.CheckConstraint("seed_number >= 1",
                           name="ck_birling_pre_seeds_seed_positive"),
    )

    op.create_table(
        "birling_matches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("match_id", sa.String(length=20), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("round_index", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("competitor1_uid", BIG_ID, nullable=True),
        sa.Column("competitor2_uid", BIG_ID, nullable=True),
        sa.Column("winner_uid", BIG_ID, nullable=True),
        sa.Column("loser_uid", BIG_ID, nullable=True),
        sa.Column("is_bye", sa.Boolean(), nullable=False),
        sa.Column("needed", sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"]),
        sa.ForeignKeyConstraint(["competitor1_uid"], ["competitors.uid"]),
        sa.ForeignKeyConstraint(["competitor2_uid"], ["competitors.uid"]),
        sa.ForeignKeyConstraint(["winner_uid"], ["competitors.uid"]),
        sa.ForeignKeyConstraint(["loser_uid"], ["competitors.uid"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "match_id",
                            name="uq_birling_matches_event_match"),
        sa.UniqueConstraint("event_id", "side", "round_index", "position",
                            name="uq_birling_matches_event_slot"),
        sa.CheckConstraint(
            "side IN ('winners', 'losers', 'finals', 'true_finals')",
            name="ck_birling_matches_side_valid"),
        sa.CheckConstraint("round_index >= 0",
                           name="ck_birling_matches_round_nonneg"),
        sa.CheckConstraint("position >= 1",
                           name="ck_birling_matches_position_positive"),
    )

    op.create_table(
        "birling_falls",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("match_row_id", sa.Integer(), nullable=False),
        sa.Column("fall_number", sa.Integer(), nullable=False),
        sa.Column("winner_uid", BIG_ID, nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["match_row_id"], ["birling_matches.id"]),
        sa.ForeignKeyConstraint(["winner_uid"], ["competitors.uid"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("match_row_id", "fall_number",
                            name="uq_birling_falls_match_number"),
        sa.CheckConstraint("fall_number >= 1 AND fall_number <= 3",
                           name="ck_birling_falls_number_range"),
    )

    op.create_table(
        "birling_placements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("uid", BIG_ID, nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"]),
        sa.ForeignKeyConstraint(["uid"], ["competitors.uid"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "uid",
                            name="uq_birling_placements_event_uid"),
        sa.CheckConstraint("position >= 1",
                           name="ck_birling_placements_position_positive"),
    )

    _backfill(op.get_bind())


def downgrade():
    op.drop_table("birling_placements")
    op.drop_table("birling_falls")
    op.drop_table("birling_matches")
    op.drop_table("birling_pre_seeds")
    op.drop_table("birling_seeds")
