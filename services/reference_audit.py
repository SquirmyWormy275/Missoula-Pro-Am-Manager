"""Find competitor references that point at the wrong person, or at nobody.

What this is for
================
``heat_assignments.competitor_id``, ``event_results.competitor_id``, and every
integer inside ``Heat.competitors``, ``Heat.stand_assignments``,
``Event.payouts`` and ``Event.event_state`` is a bare int with no foreign key.
A sibling ``competitor_type`` string, or the shape of the JSON path, is the
only thing that says which of the two competitor tables the integer addresses.

Measured on the 2026 production dump: 55 references do not resolve in the pool
their context implies, and **all 55 of them resolve to a live competitor in the
other discipline**. They are not dangling. They address the wrong human.

All 55 are also recoverable without leaving the blob they sit in, because every
stale id appears somewhere in the same blob with a name on it. Repair, not
regeneration. See :attr:`Finding.repairable_from_blob`.

The arithmetic behind that is not a coincidence and will not go away on its
own. The era-1 reseed moved college ids up to 29-92. Pro ids run 1-49. Every
stale reference is a pre-reseed college id, which is to say an integer below
29, and every integer below 29 is a live pro. The overlap is total by
construction, so a stale college reference *always* has a pro sitting on it.

Worse, the reseed preserved ordering inside gendered blocks, so the pro a
stale college id lands on is usually the same gender competing in the same
event. A wrong-discriminator read returns a plausible person. Nobody is going
to catch this by reading a heat sheet.

Why a naive gate does not work
==============================
The obvious write-time check is "does this competitor id exist". That check
**passes every one of the 55**, because each id does exist, in the other
table. A reference gate is only meaningful if it resolves against the pool the
reference's context implies, which is what this module does. The permanent
answer is ``competitors.uid`` (see ``models/competitor_identity.py``), which is
unique across both disciplines and cannot be read against the wrong pool. This
module is what holds the line until every call site carries a uid.

Scope
=====
Read-only and side effect free. It reports; it does not repair and it does not
block. Wiring :func:`check_blob` into the write paths is a separate change
that needs a decision on fail-closed versus log-and-continue, and that decision
has not been made.

Six stores are covered, enumerated in :func:`collect_sites`.

What is deliberately NOT covered
================================
This list was produced by enumerating every column in ``information_schema``
and classifying it, rather than by observing which columns happened to hold bad
data. That distinction matters: three of the four uncovered stores below are
invisible to any data-driven census, because their tables are empty in the 2026
dump.

``pro_event_ranks.competitor_id``
    A bare int, no foreign key, no ``competitor_type`` sibling, pro implied by
    the table name alone. Zero rows in 2026, but ``services/heat_generator.py``
    reads it to order the snake draft. Live code against an empty table, not
    dead code.

``users.competitor_id`` / ``users.competitor_type``
    The same column-discriminated shape as ``heat_assignments``, which *is*
    covered. Null throughout the 2026 dump. Portal login linkage.

``tournament_event.payload``
    The event log. Empty table with no production writer today; only tests
    construct ``TournamentEvent`` rows. It is the D12-C write target, so its
    payloads will carry competitor ids and this module will have to follow.

``audit_logs.entity_id`` where ``entity_type='pro_competitor'``
    Three rows in 2026. Uncovered on purpose: audit logs are history, and a
    reseed that rewrote them would be falsifying the record. A stale id in a
    log entry was correct when it was written.

Name-based references are a different problem
    ``college_competitors.partners`` stores ``{event name: partner first
    name}`` and ``pro_competitors.partners`` stores ``{event id: partner full
    name}``, both free text, both already inconsistent in 2026 ("MARIA" and
    "Maria" for the same person). Those references can dangle. They cannot be
    cross-*kind* wrong, which is what this module is for.

Two limits inside the stores that ARE covered
=============================================
:class:`_Pools` loads both competitor tables with **no tournament filter**, so
an id belonging to another tournament's competitor of the same kind is judged
``OK``. Zero such references exist in either mirror today, including the
two-tournament one, but the check cannot catch one by construction. This is the
cross-kind failure shape rotated one axis: a pool wider than the context
implies.

``heats.competitors`` and ``heats.stand_assignments`` get their kind from
:func:`kind_for_path` with an **empty path**, so ``event_type`` decides alone
and :data:`_PATH_RULES` cannot fire. For the one event type known to lie, the
Pro-Am Relay, this is unreachable rather than fixed: the relay's only Heat is a
rendering placeholder that ``flight_builder`` creates with
``set_competitors([])``, and heat moves are intra-event. Give the relay real
heat contents and this store starts reading college members against the pro
pool.
"""
import json
from dataclasses import dataclass
from typing import Optional

import sqlalchemy as sa

from services.entity_key import COLLEGE, PRO

# Dict keys whose integer value is a competitor reference.
#
# ``id`` is the general case: ``{"id": 9, "name": "Davis Underwood (UM-B)"}``.
# The other four are bracket match slots, which carry the integer alone with no
# name anywhere near it. That distinction decides whether a bad reference can
# be repaired from the blob or only regenerated, so it is recorded per site
# rather than inferred later.
_NAMED_KEY = 'id'
_BARE_SLOT_KEYS = ('competitor1', 'competitor2', 'winner', 'loser',
                   'eliminated')
_REFERENCE_KEYS = (_NAMED_KEY,) + _BARE_SLOT_KEYS

# Dict keys whose VALUE is a list of bare competitor ids, e.g.
# ``"seeding": [9, 44, 42]``. A plain integer list carries no key to match on,
# so these have to be named. An allowlist rather than "walk every int list"
# because ``entry_fees``, ``events_entered`` and ``team_number`` are also
# integers and are not competitor references.
_BARE_LIST_KEYS = ('seeding', 'falls')

# Dict keys whose VALUE is a dict KEYED BY competitor id, e.g.
# ``"placements": {"31": 1, "33": 2}``. The reference is the key, and JSON
# object keys are strings, so these are parsed rather than matched.
_ID_KEYED_DICT_KEYS = ('pre_seedings', 'placements')

# Where this list comes from
# ==========================
# Not from observing which positions happened to be broken. That is how the
# first version of this module was built and it missed five containers:
# ``eliminated``, ``seeding``, ``falls``, ``pre_seedings`` and ``placements``.
# The authoritative statement of "these JSON positions hold competitor
# references" is ``remap_bracket_payouts`` in ``scripts/reseed_college_ids.py``,
# because that is the function that has to rewrite all of them when college ids
# move. The two lists are held in sync by
# ``test_covers_every_container_the_reseed_remapper_rewrites``, which reads the
# remapper's own constants and fails if it grows a key this module does not
# audit.

# Ordered path-segment rules for deciding which pool a JSON reference belongs
# to. First match wins, checked against the full dotted path.
#
# This list exists because ``event_type`` is a liar for the mixed events.
# Pro-Am Relay is ``event_type='pro'``, but its ``event_state`` holds
# ``college_members[].id``. An earlier version of this audit trusted
# ``event_type`` and reported 64 dangling references instead of 45 (counts as
# measured then, before the bare-int containers below were covered): every
# college id under the relay was checked against the pro pool and came back
# unresolved. The path knows what the event type does not.
_PATH_RULES = (
    ('college_member', COLLEGE),
    ('eligible_college', COLLEGE),
    ('drawn_college', COLLEGE),
    ('college', COLLEGE),
    ('pro_member', PRO),
    ('eligible_pro', PRO),
    ('drawn_pro', PRO),
)

# Verdicts. Ordered by how much they should worry you.
OK = 'ok'
DANGLING = 'dangling'
CROSS_KIND = 'cross_kind'
UNKNOWN_KIND = 'unknown_kind'
"""The stored ``competitor_type`` is not a discipline this system has.

Distinct from, and worse than, a bad id: the id might be fine and there is no
way to find out, because the only thing that says which table to look in is
unusable. Nothing produces this today, which is exactly why it is a separate
verdict rather than being folded into :data:`DANGLING` and silently inflating
a count everyone reads as "stale ids".
"""

#: ``event_type`` values that mean "college competitors" when nothing in the
#: path says otherwise. Anything else falls back to the pro pool, which matches
#: how the rest of the codebase reads these columns.
_COLLEGE_EVENT_TYPES = frozenset({COLLEGE})


@dataclass(frozen=True)
class ReferenceSite:
    """One place in the database where an integer addresses a competitor."""

    store: str
    """Where it lives, e.g. ``'events.payouts'`` or ``'heat_assignments'``."""

    row_id: int
    """Primary key of the owning row."""

    path: str
    """Dotted path to the value, e.g. ``'e28.payouts.bracket.winners[0][0].competitor1'``."""

    raw_id: int
    kind: str
    """``COLLEGE`` or ``PRO``: the pool this reference is supposed to resolve in."""

    kind_source: str
    """How ``kind`` was decided. See :data:`KIND_SOURCES`.

    Carried per site because the three sources are not equally trustworthy and
    a caller deciding whether to act on a finding needs to know which one it
    got. ``'column'`` is a stored discriminator and is as good as the data.
    ``'json_path'`` is a structural rule from :data:`_PATH_RULES`.
    ``'event_type'`` is the fallback, and it is the one that produced a wrong
    answer once already.
    """

    name_in_blob: Optional[str] = None
    """The name stored alongside, when there is one. ``None`` for bare slots."""

    name_in_row: Optional[str] = None
    """The name this id carries *anywhere else in the same blob*.

    Bracket match slots, seeding lists and placement keys are bare integers, but
    the blob that holds them almost always also holds a ``competitors[]`` entry
    for the same id with the name on it. Repairing a bare slot therefore does
    not need anything outside the blob: read the name off the sibling entry and
    re-resolve it.

    Kept separate from :attr:`name_in_blob` because they answer different
    questions. ``name_in_blob`` is "does this position carry a name", which is a
    fact about the schema. ``name_in_row`` is "can this reference be recovered",
    which is a fact about the data and is the one that decides repair versus
    regeneration.

    ``None`` when the blob is self-contradictory about that id. See
    :func:`name_index`.
    """


KIND_SOURCES = ('column', 'json_path', 'event_type')


@dataclass(frozen=True)
class Finding:
    site: ReferenceSite
    verdict: str
    collides_with: Optional[str] = None
    """Name of the live competitor in the *other* pool that this integer hits.

    Set only for :data:`CROSS_KIND`. This is the person whose name a
    wrong-discriminator read would print.
    """

    @property
    def repairable_from_blob(self):
        """Whether a name inside the same blob is enough to re-resolve this.

        An earlier version asked only whether *this position* carried a name,
        which reported the 24 bracket match slots in the 2026 dump as
        unrecoverable and turned "repair the brackets" into "regenerate the
        recorded results". That was wrong: every one of those ids also appears
        in the same blob's ``competitors[]`` array with the name attached.
        Measured on the production mirror, all 55 findings are recoverable
        without leaving the blob.

        Recoverable is not the same as repaired. The name still has to resolve
        to exactly one live competitor, and that is a separate check the caller
        makes against the roster, not something this property can answer.
        """
        return bool(self.site.name_in_row or self.site.name_in_blob)


def kind_for_path(path, event_type):
    """Decide which competitor pool a JSON reference addresses.

    Returns ``(kind, kind_source)``. Path rules beat ``event_type`` because
    mixed events lie about their type; see :data:`_PATH_RULES`.
    """
    lowered = path.lower()
    for segment, kind in _PATH_RULES:
        if segment in lowered:
            return kind, 'json_path'
    if (event_type or '').lower() in _COLLEGE_EVENT_TYPES:
        return COLLEGE, 'event_type'
    return PRO, 'event_type'


def _is_int(value):
    """A real integer. ``bool`` is a subclass of ``int`` and is never an id."""
    return isinstance(value, int) and not isinstance(value, bool)


def name_index(blob):
    """Map ``competitor id -> name`` for every named reference in one blob.

    This is what makes a bare bracket slot repairable. Built from the
    :data:`_NAMED_KEY` form only, since that is the one position where the name
    provably belongs to the competitor rather than to the surrounding match.

    An id that carries two different names inside one blob is dropped rather
    than resolved to whichever was walked last. This is not hypothetical and it
    is not rare: a Pro-Am Relay blob holds ``eligible_college`` and
    ``eligible_pro`` side by side, both id-keyed from 1, so id 8 is Alpine
    Griffin in one array and Erin LaVoie in the other. Four such collisions sit
    in event 44 of the 2026 dump. Guessing between them would hand back a real,
    wrong human, which is the exact failure this whole module exists to report.
    An unrepairable finding is a correct answer. A confidently wrong name is
    not.
    """
    index = {}
    contested = set()

    def _walk(node):
        if isinstance(node, dict):
            value = node.get(_NAMED_KEY)
            name = node.get('name')
            if _is_int(value) and isinstance(name, str) and name.strip():
                name = name.strip()
                if index.setdefault(value, name) != name:
                    contested.add(value)
            for child in node.values():
                _walk(child)
        elif isinstance(node, list):
            for child in node:
                _walk(child)

    _walk(blob)
    for value in contested:
        del index[value]
    return index


def walk_blob(blob, root_path, event_type, store, row_id):
    """Yield every :class:`ReferenceSite` inside one decoded JSON blob.

    ``blob`` is already-decoded JSON, not a string, so the caller owns the
    decision about what a parse failure means. Nothing here swallows one.
    """
    sites = []
    names = name_index(blob)

    def _add(path, raw_id, own_name=None):
        kind, source = kind_for_path(path, event_type)
        sites.append(ReferenceSite(
            store=store, row_id=row_id, path=path, raw_id=raw_id,
            kind=kind, kind_source=source,
            name_in_blob=own_name,
            name_in_row=names.get(raw_id),
        ))

    def _walk(node, path):
        if isinstance(node, dict):
            name = node.get('name')
            name = name.strip() if isinstance(name, str) and name.strip() else None
            for key, value in node.items():
                child = f'{path}.{key}'
                if key in _REFERENCE_KEYS and _is_int(value):
                    # A bracket slot's name, if the dict even has one,
                    # describes the match and not the competitor. Only the
                    # `id` form gets to claim a name of its own.
                    _add(child, value, name if key == _NAMED_KEY else None)
                elif key in _BARE_LIST_KEYS and isinstance(value, list):
                    for index, item in enumerate(value):
                        if _is_int(item):
                            _add(f'{child}[{index}]', item)
                elif key in _ID_KEYED_DICT_KEYS and isinstance(value, dict):
                    for raw_key in value:
                        try:
                            _add(f'{child}[{raw_key!r}]', int(raw_key))
                        except (TypeError, ValueError):
                            # A non-numeric key here is not a competitor
                            # reference. Skipped rather than guessed at.
                            continue
                else:
                    _walk(value, child)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                _walk(value, f'{path}[{index}]')

    _walk(blob, root_path)
    return sites


class _Pools:
    """The two id-to-name maps, loaded once."""

    def __init__(self, session):
        self.by_kind = {}
        for kind, table in ((COLLEGE, 'college_competitors'),
                            (PRO, 'pro_competitors')):
            rows = session.execute(
                sa.text(f'SELECT id, name FROM {table}')).all()
            self.by_kind[kind] = {row[0]: row[1] for row in rows}

    def other(self, kind):
        return PRO if kind == COLLEGE else COLLEGE

    def judge(self, site):
        """Classify one site against the pools."""
        if site.kind not in self.by_kind:
            return Finding(site=site, verdict=UNKNOWN_KIND)
        own = self.by_kind[site.kind]
        if site.raw_id in own:
            return Finding(site=site, verdict=OK)
        other_kind = self.other(site.kind)
        other = self.by_kind[other_kind]
        if site.raw_id in other:
            return Finding(site=site, verdict=CROSS_KIND,
                           collides_with=other[site.raw_id])
        return Finding(site=site, verdict=DANGLING)


def collect_sites(session):
    """Every competitor reference in the database, from all six stores.

    ``heat_assignments``, ``event_results``, ``heats.competitors``,
    ``heats.stand_assignments``, ``events.payouts``, ``events.event_state``.
    Four tables, six stores, because ``heats`` and ``events`` each carry two
    independent reference columns. The module docstring lists the stores that
    exist and are NOT walked here, and why.

    The two column-discriminated stores come first because their ``kind`` is
    stored rather than inferred, which makes them the trustworthy half of any
    count drawn from this function.
    """
    sites = []

    for row_id, competitor_id, competitor_type in session.execute(sa.text(
            'SELECT id, competitor_id, competitor_type FROM heat_assignments')):
        sites.append(ReferenceSite(
            store='heat_assignments', row_id=row_id,
            path=f'heat_assignments[{row_id}].competitor_id',
            raw_id=competitor_id, kind=competitor_type, kind_source='column'))

    for row_id, competitor_id, competitor_type, name in session.execute(sa.text(
            'SELECT id, competitor_id, competitor_type, competitor_name '
            'FROM event_results')):
        sites.append(ReferenceSite(
            store='event_results', row_id=row_id,
            path=f'event_results[{row_id}].competitor_id',
            raw_id=competitor_id, kind=competitor_type, kind_source='column',
            name_in_blob=name))

    heats = session.execute(sa.text(
        'SELECT h.id, h.competitors, h.stand_assignments, e.event_type '
        'FROM heats h JOIN events e ON e.id = h.event_id')).all()
    for row_id, competitors, stands, event_type in heats:
        kind, source = kind_for_path('', event_type)
        for index, value in enumerate(_loads(competitors, [])):
            if _is_int(value):
                sites.append(ReferenceSite(
                    store='heats.competitors', row_id=row_id,
                    path=f'heats[{row_id}].competitors[{index}]',
                    raw_id=value, kind=kind, kind_source=source))
        # stand_assignments is {competitor_id: stand_number}, so the reference
        # is the KEY and JSON keys are strings. A non-numeric key is not a
        # competitor reference and is skipped rather than guessed at.
        for key in _loads(stands, {}):
            try:
                value = int(key)
            except (TypeError, ValueError):
                continue
            sites.append(ReferenceSite(
                store='heats.stand_assignments', row_id=row_id,
                path=f'heats[{row_id}].stand_assignments[{key!r}]',
                raw_id=value, kind=kind, kind_source=source))

    events = session.execute(sa.text(
        'SELECT id, event_type, payouts, event_state FROM events')).all()
    for row_id, event_type, payouts, state in events:
        for raw, column in ((payouts, 'payouts'), (state, 'event_state')):
            if not raw:
                continue
            sites.extend(walk_blob(
                _loads(raw, {}), f'e{row_id}.{column}', event_type,
                f'events.{column}', row_id))

    return sites


def audit(session, include_ok=False):
    """Classify every reference in the database.

    Returns a list of :class:`Finding`. ``include_ok=False`` (the default)
    drops the resolving ones, because on the production dump that is roughly
    two thousand rows of nothing wrong.
    """
    pools = _Pools(session)
    findings = [pools.judge(site) for site in collect_sites(session)]
    if include_ok:
        return findings
    return [f for f in findings if f.verdict != OK]


def check_blob(session, blob, root_path, event_type):
    """Gate form: the bad references in one blob, before it is written.

    Separate from :func:`audit` so a write path can validate the value it is
    about to store without reading the rest of the database. Returns an empty
    list when the blob is clean, which makes the call site read as
    ``if check_blob(...):``.
    """
    pools = _Pools(session)
    findings = [pools.judge(site) for site in
                walk_blob(blob, root_path, event_type, 'pending', 0)]
    return [f for f in findings if f.verdict != OK]


def summarize(findings):
    """Counts by verdict, store, and repairability, for a report line."""
    summary = {
        'total': len(findings),
        CROSS_KIND: 0,
        DANGLING: 0,
        UNKNOWN_KIND: 0,
        'repairable_from_blob': 0,
        'not_repairable': 0,
        'by_store': {},
    }
    for finding in findings:
        summary[finding.verdict] = summary.get(finding.verdict, 0) + 1
        key = 'repairable_from_blob' if finding.repairable_from_blob \
            else 'not_repairable'
        summary[key] += 1
        store = finding.site.store
        summary['by_store'][store] = summary['by_store'].get(store, 0) + 1
    return summary


def _loads(raw, default):
    """Decode a TEXT JSON column, treating unusable content as empty.

    The columns are ``nullable=False`` with a JSON default, so ``None`` and
    ``''`` both mean "never written". A genuinely corrupt value returning the
    default here is a deliberate narrowing: this module's job is to report bad
    *references*, and a blob that will not parse has no references to report.
    Corrupt-blob detection belongs to the schema validation work (D13-C), not
    here, and hiding it behind a reference count would be worse than leaving it
    to the module that will own it.
    """
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default
