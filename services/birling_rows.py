"""The birling bracket document and the five birling tables, both directions.

D13-C commits A2 and A3a. A2 built the forward half, which turns a document
into rows. A3a builds the inverse, which turns rows back into a document.

Forward half, D13-C commit A2. Commit A1 created ``birling_seeds``,
``birling_pre_seeds``, ``birling_matches``, ``birling_falls`` and
``birling_placements`` and filled them once, from the migration, out of the
JSON document living in ``events.payouts``. Nothing has written a row since.
This module is what keeps them current: every time the document changes, the
rows are rebuilt from it.

The JSON is still the truth in A2. Nothing reads these rows. A3 moves the
readers and is where an unprojectable bracket stops being harmless; A4 drops
the document. Until then this module exists to be proved correct against a
document that is still authoritative, which is the only window in which it can
be proved correct cheaply.

Why this is a module and not a method on ``BirlingBracket``
==========================================================
The register's phrasing for this commit is that ``BirlingBracket`` dual-writes,
and for four of the five tables that is exactly where the write happens. But
``birling_pre_seeds`` is a projection of ``payouts['pre_seedings']``, and
``pre_seedings`` is written by ``routes/scheduling/ability_rankings.py``, which
never constructs a ``BirlingBracket`` and has no reason to. A projector that
lived on the service would ship a table that live code never writes and that
only the A1 backfill had ever touched. So the projector is a free function over
an ``Event``, and both writers call it.

Those two are the whole set, enumerated by searching the tree for assignments
to ``Event.payouts``. ``scripts/reseed_college_ids.py`` also rewrites the
document and deliberately does NOT call this, because it does not need to: a
reseed changes the bare integer ids inside the document and leaves every
``competitors.uid`` alone, so the projection is reseed-invariant. That property
is not an accident, it is the entire argument for the identity spine, and it is
worth stating because it is the first place the spine has paid for itself
without anybody writing code to collect.

Why the planner is copied from the migration instead of shared with it
======================================================================
``migrations/versions/u0c4d5e6f7a8_birling_bracket_tables.py`` carries the same
walk over the same document. It could import this module and would then be one
refactor away from being unrunnable, because an Alembic revision has to run
against a tree whose application code has moved on by years. The revision is
frozen the moment it ships. So the logic is duplicated on purpose, and the
duplication is defended by ``tests/test_birling_rows.py``, which drives every
refusal case through BOTH implementations and asserts they file the same
reasons. A copy with a proof of equivalence is a maintained copy; a copy
without one is a bug waiting for a quiet afternoon.

Why a save replaces the whole event rather than diffing it
==========================================================
There is no per-match write path anywhere in the service. ``record_fall``,
``record_match_result`` and ``undo_match_result`` all mutate the in-memory
document and hand the entire thing back to ``_save_bracket_data``. A projector
that tried to diff would be inventing a granularity the caller does not have,
and would be the only thing in the system holding an opinion about which parts
of a bracket changed. Replacing the event's rows is the same shape as the write
it mirrors, and it is the only shape that cannot drift.

Why a document that will not project leaves NO rows rather than stale ones
==========================================================================
Two of the brackets on the production mirror carry references to college
competitors by their pre-reseed ids, which now resolve to different people on
the pro side. A1 skipped both. The reference gate forgives a bad id that was
already there, by design, so those documents can still be saved, which means
this projector will meet them. When a document cannot be projected the event's
rows are deleted and a warning is logged. Absent rows say "there is nothing
here"; stale rows say "this is the bracket", and one of those two statements
can be wrong in a way nobody notices. A3, which is where the readers move, can
tell the two apart without a marker column: a document that claims a bracket
and an event with no seed rows is exactly the case it has to refuse.

The forward half never raises on a bad document. A bracket is live judge state
on race day and there is no sitting in which it can be unavailable. It does not
catch ``BadReferenceWrite`` either, which is the reference gate refusing a
genuinely new bad reference; that one is the caller's to handle and rolling
back is the caller's job.

Inverse half, D13-C commit A3a
==============================
``load_document`` reads the five tables and returns the same document shape
``BirlingBracket`` has always worked in. Nothing calls it yet. A3b points
``BirlingBracket._load_bracket_data`` and the display readers at it, and that
is where an unprojected bracket stops being harmless; A4 drops the column.

Why an inverse and not a rewrite of ``BirlingBracket`` onto rows
----------------------------------------------------------------
``services/birling_bracket.py`` is a thousand lines of double-elimination
algebra: seed pairing, bye propagation across both sides, drop-down source
mapping, elimination placement counted backwards from the field size, and an
undo that walks it all in reverse. None of that is storage. Rewriting it to
operate on rows would put the riskiest change this program has attempted on the
one page that a judge cannot do without, in exchange for nothing a reader can
observe.

So the storage graduates and the algorithm keeps its in-memory shape. That is
the same trade D12-C made for heat rosters: ``heat_assignments`` became the
truth while the code went on building dicts. What changes here is where the
dict comes from, and therefore what a competitor reference inside it means. It
stops being a bare integer that happens to index a table and becomes the
resolution of a real foreign key.

What the inverse deliberately does not recover
----------------------------------------------
The forward half drops four things. Each is dropped because it carries no
information, and the inverse regenerates each from what does:

**``round``.** ``'winners_2'`` is ``side`` and ``round_index`` spelled
differently. Regenerated by ``round_name``. Nothing in ``routes``, ``services``
or ``templates`` reads it; the service parses the same fact back out of
``match_id`` in nine places.

**``current_round``.** Written once by the skeleton and read by nothing in the
tree. It has said ``'winners_1'`` on both production brackets since they were
generated, through four decided rounds. Regenerated as that constant.

**``eliminated_position``.** Present on every losers match dict, assigned by
nothing, read by nothing, and explicitly set back to None by
``services/birling_print.py``. Regenerated as None.

**The cached competitor name.** ``competitors[].name`` held a display name from
the moment the bracket was generated. The inverse takes the current one from a
join instead. A renamed competitor therefore reads correctly on a page that
used to show the old name, which is the point of the foreign key and is the one
visible behaviour change in the whole commit.

Two smaller normalisations, both supersets rather than losses. ``is_bye`` is
emitted on every winners and losers match, where the generator writes it only
on winners and ``_sweep_losers_byes`` adds it to a losers match after the fact;
a match shape that depends on the bracket's history is worse than one that does
not, and every reader in the tree uses ``.get``. And ``competitors`` comes back
in seed order, where the stored list was in whatever order the caller passed;
on both production brackets those are the same list element for element, and
the forward planner already refuses a document whose two lists name different
people.

Why the inverse raises where the forward half does not
------------------------------------------------------
Refusing to save loses judge work. Refusing to load loses nothing but a page,
and the alternative is worse: a document reconstructed around a reference that
cannot be resolved would name the wrong human, confidently, in the same layout
as the right one. That is the failure the era-1 reseed already inflicted once,
and it is the failure these tables exist to make impossible. So
``load_document`` raises ``UnloadableBracket`` rather than return a document it
knows is wrong.

It holds no policy beyond that. The question A3b has to answer, whether an
event whose document claims a bracket but which has no rows should be refused
or silently shown as empty, is a reader's question and is answered at the
reader, with ``is_projected`` to hand. A projector that decided it here would be
deciding it for readers it has never met.
"""
import json
import logging
from datetime import datetime, timezone

from database import db
from models import (
    BirlingFall,
    BirlingMatch,
    BirlingPlacement,
    BirlingPreSeed,
    BirlingSeed,
    CollegeCompetitor,
    ProCompetitor,
)

logger = logging.getLogger(__name__)

#: Which roster an event's bare integer ids are drawn from. Same mapping the
#: migration makes against the table names.
POOL_MODEL = {'college': CollegeCompetitor, 'pro': ProCompetitor}

#: A document carrying any of these is a birling document. Keyed on content
#: rather than on ``scoring_type`` for the same reason the backfill was: an
#: event whose type was changed after a bracket was generated still has the
#: bracket, and the rows should follow the document.
BRACKET_KEYS = ('bracket', 'seeding', 'pre_seedings')

#: Filed when the event's ``event_type`` names neither roster.
NO_POOL = 'the event names no competitor pool'


def parse_document(raw):
    """A ``payouts`` value as a dict, forgiving what it must.

    Mirrors the migration's ``_parse``. Null, empty string and a JSON list are
    all "not a bracket" rather than errors.
    """
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def parse_timestamp(raw):
    """A fall's ISO timestamp as a naive UTC datetime, or None.

    ``record_fall`` writes an offset-aware ISO string and the column is a plain
    ``DateTime`` like every other timestamp in this schema. An unparseable
    value comes back as None rather than failing the event, because the column
    is nullable and a timestamp is not identity.
    """
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def pool_for(event_type):
    """``{bare id: uid}`` for the roster an event's ids are drawn from.

    The whole table, not the tournament, which is what the A1 backfill used and
    is therefore what this has to use to be comparable to it. Open question 7
    asks whether reference resolution should be tournament-scoped everywhere;
    when it is answered, both sides move together or neither does.
    """
    model = POOL_MODEL.get(event_type)
    if model is None:
        return None
    return dict(db.session.query(model.id, model.uid).all())


class Plan:
    """The rows one document turns into, and why it might turn into none.

    Every ``ref`` call either resolves or files a reason. Nothing is written
    until the whole document has been walked, so an event contributes all of
    its rows or none of them, and the reason names the class of defect rather
    than the constraint that would have tripped.
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

    def ref(self, raw, required):
        """Resolve one bare competitor id to a uid.

        ``required=False`` means an empty slot is legitimate, which is the case
        for all four competitor columns on a match: a later round's slots are
        empty until the round feeding them is decided, a bye has no second
        competitor, and an undecided match has no winner.
        """
        if raw is None:
            if required:
                self.fail('a required competitor slot is empty')
            return None
        if isinstance(raw, bool) or not isinstance(raw, int):
            self.fail('a competitor reference is not an integer')
            return None
        uid = self.pool.get(raw)
        if uid is None:
            self.fail('a competitor reference names nobody in the event pool')
        return uid


def plan_seeds(plan, doc):
    """``seeding`` and ``competitors`` become ``birling_seeds``."""
    seeding = doc.get('seeding') or []
    if not isinstance(seeding, list):
        plan.fail('the seed order is not a list')
        return

    entrants = doc.get('competitors') or []
    if isinstance(entrants, list):
        listed = [c.get('id') for c in entrants if isinstance(c, dict)]
        if len(listed) != len(entrants):
            plan.fail('the entrant list holds something that is not a competitor')
        elif seeding and set(listed) != set(seeding):
            plan.fail('the entrant list and the seed order name different people')
    else:
        plan.fail('the entrant list is not a list')

    seen = set()
    for position, raw in enumerate(seeding, start=1):
        uid = plan.ref(raw, required=True)
        if uid is None:
            continue
        if uid in seen:
            plan.fail('a competitor holds two seeds')
            continue
        seen.add(uid)
        plan.seeds.append({'seed_number': position, 'uid': uid})


def plan_pre_seeds(plan, doc):
    """``pre_seedings`` becomes ``birling_pre_seeds``."""
    pre = doc.get('pre_seedings') or {}
    if not isinstance(pre, dict):
        plan.fail('the pre-seeding map is not a map')
        return

    seen_uid = set()
    seen_number = set()
    for key, number in pre.items():
        try:
            raw = int(key)
        except (TypeError, ValueError):
            plan.fail('a pre-seeding is keyed by something that is not an id')
            continue
        uid = plan.ref(raw, required=True)
        if uid is None:
            continue
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            plan.fail('a pre-seeding holds something that is not a seed number')
            continue
        if uid in seen_uid or number in seen_number:
            plan.fail('a pre-seeding repeats a competitor or a seed number')
            continue
        seen_uid.add(uid)
        seen_number.add(number)
        plan.pre_seeds.append({'uid': uid, 'seed_number': number})


def plan_match(plan, side, round_index, position, match, seen_name, seen_slot):
    """One match dict becomes a ``birling_matches`` row and its falls."""
    if not isinstance(match, dict):
        plan.fail('a match slot holds something that is not a match')
        return

    name = match.get('match_id')
    if not isinstance(name, str) or not name or len(name) > 20:
        plan.fail('a match has no usable name')
        return
    if name in seen_name:
        plan.fail('two matches share a name')
        return
    seen_name.add(name)

    slot = (side, round_index, position)
    if slot in seen_slot:
        plan.fail('two matches claim one slot')
        return
    seen_slot.add(slot)

    row = {
        'match_id': name,
        'side': side,
        'round_index': round_index,
        'position': position,
        'competitor1_uid': plan.ref(match.get('competitor1'), required=False),
        'competitor2_uid': plan.ref(match.get('competitor2'), required=False),
        'winner_uid': plan.ref(match.get('winner'), required=False),
        'loser_uid': plan.ref(match.get('loser'), required=False),
        'is_bye': bool(match.get('is_bye', False)),
        # Null everywhere but the true finals, because false would claim the
        # question of a second grand final had been asked and answered.
        'needed': (bool(match.get('needed', False))
                   if side == 'true_finals' else None),
        'falls': [],
    }

    falls = match.get('falls') or []
    if not isinstance(falls, list):
        plan.fail('a match holds a fall list that is not a list')
        return

    seen_fall = set()
    for index, fall in enumerate(falls, start=1):
        if not isinstance(fall, dict):
            plan.fail('a fall is not a fall')
            continue
        number = fall.get('fall_number', index)
        if isinstance(number, bool) or not isinstance(number, int):
            plan.fail('a fall has no usable number')
            continue
        if number < 1 or number > 3:
            # Birling is best of three. A fourth fall is not a fall.
            plan.fail('a fall is numbered outside one to three')
            continue
        if number in seen_fall:
            plan.fail('a match records one fall number twice')
            continue
        seen_fall.add(number)
        winner = plan.ref(fall.get('winner'), required=True)
        if winner is None:
            continue
        row['falls'].append({
            'fall_number': number,
            'winner_uid': winner,
            'recorded_at': parse_timestamp(fall.get('recorded_at')),
        })

    plan.matches.append(row)


def plan_matches(plan, doc):
    """The whole ``bracket`` subtree becomes ``birling_matches``."""
    bracket = doc.get('bracket') or {}
    if not isinstance(bracket, dict):
        plan.fail('the bracket is not a bracket')
        return

    seen_name = set()
    seen_slot = set()

    for side in ('winners', 'losers'):
        rounds = bracket.get(side) or []
        if not isinstance(rounds, list):
            plan.fail('a bracket side is not a list of rounds')
            continue
        for round_index, matches in enumerate(rounds):
            if not isinstance(matches, list):
                plan.fail('a bracket round is not a list of matches')
                continue
            for position, match in enumerate(matches, start=1):
                plan_match(plan, side, round_index, position, match,
                           seen_name, seen_slot)

    # The two finals are singletons rather than rounds, so they sit at round
    # zero, position one, distinguished from each other by ``side``.
    for side in ('finals', 'true_finals'):
        match = bracket.get(side)
        if match is None:
            continue
        plan_match(plan, side, 0, 1, match, seen_name, seen_slot)


def plan_placements(plan, doc):
    """``placements`` becomes ``birling_placements``.

    Position is not unique inside an event and is not checked for uniqueness
    here. The grand finals write one and two while the losers bracket has
    already written the same numbers downward from the field size, and
    reconciling that is the service's business.
    """
    placements = doc.get('placements') or {}
    if not isinstance(placements, dict):
        plan.fail('the placement map is not a map')
        return

    seen = set()
    for key, position in placements.items():
        try:
            raw = int(key)
        except (TypeError, ValueError):
            plan.fail('a placement is keyed by something that is not an id')
            continue
        uid = plan.ref(raw, required=True)
        if uid is None:
            continue
        if isinstance(position, bool) or not isinstance(position, int) or position < 1:
            plan.fail('a placement holds something that is not a position')
            continue
        if uid in seen:
            plan.fail('a competitor is placed twice')
            continue
        seen.add(uid)
        plan.placements.append({'uid': uid, 'position': position})


def plan_document(event_id, pool, doc):
    """Walk one document and return the ``Plan`` it makes.

    Pure. No session, no event, no writing. This is the half that has to agree
    with the migration, and keeping it callable without a database is what lets
    the equivalence test drive both implementations over the same inputs.
    """
    plan = Plan(event_id, pool)
    plan_seeds(plan, doc)
    plan_pre_seeds(plan, doc)
    plan_matches(plan, doc)
    plan_placements(plan, doc)
    return plan


def clear_event(event_id):
    """Delete every projected row for one event.

    Falls go with their match through the ``delete-orphan`` cascade rather than
    being deleted directly, which is why the matches are loaded as objects
    instead of swept with a bulk delete.
    """
    for match in BirlingMatch.query.filter_by(event_id=event_id).all():
        db.session.delete(match)
    for model in (BirlingSeed, BirlingPreSeed, BirlingPlacement):
        for row in model.query.filter_by(event_id=event_id).all():
            db.session.delete(row)


def write_plan(plan):
    """Add one plan's rows to the session."""
    for seed in plan.seeds:
        db.session.add(BirlingSeed(event_id=plan.event_id,
                                   seed_number=seed['seed_number'],
                                   uid=seed['uid']))

    for pre in plan.pre_seeds:
        db.session.add(BirlingPreSeed(event_id=plan.event_id,
                                      uid=pre['uid'],
                                      seed_number=pre['seed_number']))

    for match in plan.matches:
        row = BirlingMatch(
            event_id=plan.event_id,
            match_id=match['match_id'],
            side=match['side'],
            round_index=match['round_index'],
            position=match['position'],
            competitor1_uid=match['competitor1_uid'],
            competitor2_uid=match['competitor2_uid'],
            winner_uid=match['winner_uid'],
            loser_uid=match['loser_uid'],
            is_bye=match['is_bye'],
            needed=match['needed'],
        )
        # Through the relationship rather than by match_row_id, so no flush is
        # needed between the match and its falls to learn the parent's id.
        row.falls = [BirlingFall(fall_number=fall['fall_number'],
                                 winner_uid=fall['winner_uid'],
                                 recorded_at=fall['recorded_at'])
                     for fall in match['falls']]
        db.session.add(row)

    for placement in plan.placements:
        db.session.add(BirlingPlacement(event_id=plan.event_id,
                                        uid=placement['uid'],
                                        position=placement['position']))


def project(event):
    """Rebuild one event's projected rows from its current document.

    Call this after assigning to ``event.payouts`` and before committing, so
    the rows and the document that produced them land in one transaction.

    Returns the ``Plan``, whose ``reasons`` is empty on a projection that was
    written. A document that is not a bracket at all also returns an empty
    plan with no reasons, having cleared any rows the event used to have,
    because an event that stopped being a bracket has no bracket.
    """
    doc = parse_document(event.payouts)
    is_bracket = any(key in doc for key in BRACKET_KEYS)

    pool = pool_for(event.event_type) if is_bracket else {}
    if pool is None:
        plan = Plan(event.id, {})
        plan.fail(NO_POOL)
    elif is_bracket:
        plan = plan_document(event.id, pool, doc)
    else:
        plan = Plan(event.id, {})

    clear_event(event.id)
    # The deletes must reach the database before the inserts do. Within one
    # flush SQLAlchemy emits a mapper's inserts before its deletes, so a
    # re-save that reuses a seed number would collide with the row it is
    # replacing on ``uq_birling_seeds_event_seed``.
    db.session.flush()

    if plan.reasons:
        logger.warning(
            'birling rows: event %s was not projected and now has no rows. '
            'Its JSON is untouched and is still what the app reads. '
            'Reasons: %s. Run scripts/repair_era1_references.py --check.',
            event.id, '; '.join(sorted(plan.reasons)))
        return plan

    write_plan(plan)
    return plan


# ---------------------------------------------------------------------------
# The inverse half, D13-C commit A3a. Rows back into a document.
# ---------------------------------------------------------------------------


class UnloadableBracket(Exception):
    """The rows cannot produce a document this module is willing to vouch for.

    Raised rather than returned because every caller of ``load_document`` is a
    page, and a page that renders a document built around a reference it could
    not resolve names the wrong human in the right layout. Refusing costs the
    page. Guessing costs the result.
    """


#: A projected row points at a competitor the roster no longer holds. The
#: reference gate makes this impossible to create through the app, so it means
#: the roster changed underneath committed rows.
UNRESOLVABLE = 'a projected row names a competitor the roster no longer holds'

#: A bracket side's round indices are not 0, 1, 2 ... n. Reindexing silently
#: would move every match in the side up a round.
ROUND_GAP = 'a bracket side is missing a round'

#: A round's positions are not 1, 2 ... n. Reindexing silently would lose the
#: pairing a position carries.
POSITION_GAP = 'a bracket round is missing a match'

#: Seed numbers are not 1, 2 ... n. The forward half writes them from
#: ``enumerate``, so a gap means something else wrote these rows.
SEED_GAP = 'the seed order is missing a seed'

#: A grand final is a singleton and lives at round zero, position one. More
#: than one row on that side, or a row somewhere else, is a shape this module
#: has no document for.
FINALS_SHAPE = 'a grand final is not one match in its own slot'

#: The two sides that are lists of rounds, in the order the document holds them.
LIST_SIDES = ('winners', 'losers')

#: The two sides that are single matches.
SINGLE_SIDES = ('finals', 'true_finals')


def empty_document():
    """The skeleton ``BirlingBracket`` builds when an event has no bracket.

    Byte for byte the same shape as ``_load_bracket_data``'s fallback, because
    A3b makes this its replacement and a reader must not be able to tell which
    of the two it got.
    """
    return {
        'bracket': {
            'winners': [],
            'losers': [],
            'finals': None,
            'true_finals': None,
        },
        'competitors': [],
        'seeding': [],
        'current_round': 'winners_1',
        'placements': {},
    }


def round_name(side, round_index):
    """The document's ``round`` string for a stored (side, round_index).

    The generator writes ``'winners_1'`` for the first winners round, so the
    zero-based stored index is one-based here. The two finals are their own
    names and ignore the index.
    """
    if side in LIST_SIDES:
        return '%s_%d' % (side, round_index + 1)
    return side


def format_timestamp(value):
    """A stored naive UTC datetime back to the ISO string a fall carried.

    ``parse_timestamp`` strips the offset on the way in because the column is a
    plain ``DateTime``. This puts it back, so a fall that went in as UTC comes
    out as UTC and not as a bare local-looking string.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def reverse_pool_for(event_type, uids):
    """``{uid: (bare id, display name)}`` for exactly the uids asked about.

    Scoped to the bracket's own references rather than the whole roster for two
    reasons. It is the smaller query on a tournament with a few hundred
    competitors, and ``display_name`` is a property rather than a column, so
    this has to load entities and for a college competitor that means a lazy
    load of ``team`` apiece. Loading twenty is fine. Loading the roster on every
    page render would not be.

    Returns None for an event type that names no roster, the same way
    ``pool_for`` does.
    """
    model = POOL_MODEL.get(event_type)
    if model is None:
        return None
    wanted = sorted({uid for uid in uids if uid is not None})
    if not wanted:
        return {}
    return {competitor.uid: (competitor.id, competitor.display_name)
            for competitor in model.query.filter(model.uid.in_(wanted)).all()}


def is_projected(event_id):
    """True when this event has any projected birling row at all.

    Falls are not consulted because a fall cannot exist without its match. The
    question this answers is A3b's: an event whose document claims a bracket
    but which has no rows is either a save that was refused or a backfill that
    skipped it, and either way the reader, not the projector, decides what to
    show.
    """
    for model in (BirlingSeed, BirlingPreSeed, BirlingMatch, BirlingPlacement):
        row = db.session.query(model.id).filter_by(event_id=event_id).first()
        if row is not None:
            return True
    return False


def _bare(uid, reverse):
    """One stored uid back to the bare integer id a document holds."""
    if uid is None:
        return None
    return reverse[uid][0]


def _match_document(row, side, reverse):
    """One ``birling_matches`` row back to a match dict.

    ``round`` is regenerated from the slot, ``is_bye`` is emitted on both list
    sides where the generator emitted it only on winners, ``eliminated_position``
    is the None that the only writer of it wrote, and ``needed`` appears only on
    the true finals where it means anything.
    """
    match = {
        'match_id': row.match_id,
        'round': round_name(side, row.round_index),
        'competitor1': _bare(row.competitor1_uid, reverse),
        'competitor2': _bare(row.competitor2_uid, reverse),
        'winner': _bare(row.winner_uid, reverse),
        'loser': _bare(row.loser_uid, reverse),
        'falls': [{'fall_number': fall.fall_number,
                   'winner': _bare(fall.winner_uid, reverse),
                   'recorded_at': format_timestamp(fall.recorded_at)}
                  for fall in sorted(row.falls, key=lambda f: f.fall_number)],
    }
    if side in LIST_SIDES:
        match['is_bye'] = bool(row.is_bye)
    if side == 'losers':
        match['eliminated_position'] = None
    if side == 'true_finals':
        match['needed'] = bool(row.needed)
    return match


def _round_document(rows, side, reverse):
    """One round's rows back to a list of match dicts, in position order."""
    ordered = sorted(rows, key=lambda row: row.position)
    positions = [row.position for row in ordered]
    if positions != list(range(1, len(positions) + 1)):
        raise UnloadableBracket(POSITION_GAP)
    return [_match_document(row, side, reverse) for row in ordered]


def _side_document(grouped, side, reverse):
    """One list side's rows back to a list of rounds."""
    rounds = grouped.get(side) or {}
    if not rounds:
        return []
    indices = sorted(rounds)
    if indices != list(range(len(indices))):
        raise UnloadableBracket(ROUND_GAP)
    return [_round_document(rounds[index], side, reverse) for index in indices]


def _single_document(grouped, side, reverse):
    """One singleton side's row back to a match dict, or None if absent."""
    rounds = grouped.get(side) or {}
    if not rounds:
        return None
    if sorted(rounds) != [0]:
        raise UnloadableBracket(FINALS_SHAPE)
    rows = rounds[0]
    if len(rows) != 1 or rows[0].position != 1:
        raise UnloadableBracket(FINALS_SHAPE)
    return _match_document(rows[0], side, reverse)


def load_rows(event_id):
    """Every projected row for one event, each collection already ordered.

    Separate from ``load_document`` so the round-trip proof can look at what
    came out of the database before it becomes a document, and so a caller that
    wants a count does not build a document to get one.
    """
    seeds = (BirlingSeed.query
             .filter_by(event_id=event_id)
             .order_by(BirlingSeed.seed_number).all())
    pre_seeds = (BirlingPreSeed.query
                 .filter_by(event_id=event_id)
                 .order_by(BirlingPreSeed.seed_number).all())
    matches = (BirlingMatch.query
               .filter_by(event_id=event_id)
               .order_by(BirlingMatch.side,
                         BirlingMatch.round_index,
                         BirlingMatch.position).all())
    placements = (BirlingPlacement.query
                  .filter_by(event_id=event_id)
                  .order_by(BirlingPlacement.position,
                            BirlingPlacement.uid).all())
    return seeds, pre_seeds, matches, placements


def referenced_uids(seeds, pre_seeds, matches, placements):
    """Every competitor uid the rows name, falls included."""
    uids = set()
    for row in seeds:
        uids.add(row.uid)
    for row in pre_seeds:
        uids.add(row.uid)
    for row in placements:
        uids.add(row.uid)
    for row in matches:
        for uid in (row.competitor1_uid, row.competitor2_uid,
                    row.winner_uid, row.loser_uid):
            if uid is not None:
                uids.add(uid)
        for fall in row.falls:
            uids.add(fall.winner_uid)
    return uids


def load_document(event):
    """The five tables back into the document shape the service works in.

    The inverse of ``project``, modulo the four fields the forward half drops
    because they carry no information and the name that is now a live join
    rather than a snapshot. All five differences are argued in the module
    docstring above.

    An event with no rows gets ``empty_document()``, which is what an event
    with no bracket has always got. Distinguishing "no bracket" from "a bracket
    that failed to project" is ``is_projected``'s job and the reader's
    decision.

    Raises ``UnloadableBracket`` rather than returning a document built around
    a reference it could not resolve or a slot it had to renumber.
    """
    seeds, pre_seeds, matches, placements = load_rows(event.id)
    uids = referenced_uids(seeds, pre_seeds, matches, placements)

    reverse = reverse_pool_for(event.event_type, uids)
    if reverse is None:
        if uids:
            raise UnloadableBracket(NO_POOL)
        reverse = {}
    if uids - set(reverse):
        raise UnloadableBracket(UNRESOLVABLE)

    document = empty_document()

    numbers = [row.seed_number for row in seeds]
    if numbers != list(range(1, len(numbers) + 1)):
        raise UnloadableBracket(SEED_GAP)
    document['seeding'] = [reverse[row.uid][0] for row in seeds]
    document['competitors'] = [{'id': reverse[row.uid][0],
                                'name': reverse[row.uid][1]}
                               for row in seeds]

    grouped = {}
    for row in matches:
        grouped.setdefault(row.side, {}).setdefault(row.round_index, []).append(row)

    bracket = document['bracket']
    for side in LIST_SIDES:
        bracket[side] = _side_document(grouped, side, reverse)
    for side in SINGLE_SIDES:
        bracket[side] = _single_document(grouped, side, reverse)

    unknown = set(grouped) - set(LIST_SIDES) - set(SINGLE_SIDES)
    if unknown:
        # Unreachable through the check constraint on ``side``, and checked
        # anyway, because dropping a side silently is how a bracket loses half
        # of itself without anybody noticing.
        raise UnloadableBracket(FINALS_SHAPE)

    document['placements'] = {str(reverse[row.uid][0]): row.position
                              for row in placements}

    # Absent rather than empty when there are no rows, because the document has
    # only ever carried this key when the ability rankings page wrote it, and
    # an empty map is a claim that the page ran and found nothing.
    if pre_seeds:
        document['pre_seedings'] = {str(reverse[row.uid][0]): row.seed_number
                                    for row in pre_seeds}

    return document
