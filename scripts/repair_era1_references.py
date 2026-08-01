"""Repair the era-1 ghost references in the birling brackets and the relay.

What is broken
==============
The 2026 production data carries 55 competitor references that resolve to the
wrong human. They are pre-reseed ("era-1") college ids, integers below 29, left
behind in ``events.payouts`` and ``events.event_state`` when the c38 reseed
moved every live college id up by 100000. Pro ids run 1-49, so every one of
those integers lands on a live *pro*, in the same event, usually of the same
gender, because the reseed preserved ordering inside gendered blocks. Nothing
dangles. Everything reads back a plausible, wrong person.

``services/reference_audit.py`` is the detector and its docstring carries the
full account. ``scripts/reseed_college_ids.py`` is the code that caused this,
deliberately: it preserved pre-existing orphans bit-for-bit rather than
"fixing" history it could not verify, and named this repair as the follow-up
owner. This is that follow-up.

How it repairs
==============
By name, not by arithmetic. The mapping looks arithmetic on the 2026 data,
mostly ``n -> n + 100076``, and it is not: raw id 24 resolves to 100073, not
100100. Any offset-based repair silently rewrites four references to the wrong
person, which is the defect it was supposed to remove. So: read the name the
blob itself stores against the id, resolve that name against the live roster
for the pool the reference's context implies, and refuse anything that does not
resolve to exactly one person.

The traversal is not reimplemented here. ``rewrite_blob`` and the audit's
``walk_blob`` both drive ``services.reference_audit._traverse``, so the repair
cannot come to a different opinion than the detector about which JSON positions
hold a competitor id. Two independent enumerations of the same positions is
what produced these ghosts, and the detector's own container list had to be
corrected once for missing five of them.

Usage
=====
    DATABASE_URL=postgresql://... python scripts/repair_era1_references.py --check
    DATABASE_URL=postgresql://... python scripts/repair_era1_references.py --apply

``--check`` measures and prints the plan; it writes nothing. ``--apply`` runs
in ONE transaction and rolls the whole thing back if any post-check fails.

Read the ``--check`` report before running ``--apply``. This is a name-matched
rewrite of recorded competition results. It is not a schema change and it is
not idempotent housekeeping.

Why a script and not a migration
================================
It is a one-time repair of one database's data. As a migration it would be a
no-op on every fresh database and a surprise in every fixture, and it would run
unattended, which is the wrong mode for a fuzzy-matched rewrite of results a
human can still remember. ``scripts/reseed_college_ids.py`` set this precedent
for exactly these reasons.

Exit codes
==========
    0   nothing to repair, or the repair applied and every post-check passed
    1   findings this script refuses to touch, or a post-check failed

Scope
=====
Only the two JSON event stores, ``events.payouts`` and ``events.event_state``,
which is where all 55 live. A finding in ``heat_assignments``,
``event_results``, ``heats.competitors`` or ``heats.stand_assignments`` is
reported and refused rather than repaired: those are plain columns needing a
different UPDATE, there are none in any mirror, and an untested repair path is
worse than an honest refusal.
"""
import argparse
import copy
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.reference_audit import (  # noqa: E402
    COLLEGE,
    PRO,
    audit,
    rewrite_blob,
    walk_blob,
)

#: The stores this script knows how to rewrite. Everything else is refused.
REPAIRABLE_STORES = ('events.payouts', 'events.event_state')

_POOL_TABLES = {COLLEGE: 'college_competitors', PRO: 'pro_competitors'}

#: Ordinals used to blank out reference positions when proving nothing else in
#: a blob moved. Above any real id by a wide margin so a scrubbed position can
#: never be confused with a live one.
_SCRUB_BASE = 10 ** 9


def normalize_name(name):
    """Fold a stored name to something comparable against the roster.

    Strips a trailing parenthesised team designator, because the blobs store
    ``'Davis Underwood (UM-B)'`` and the roster stores ``'Davis Underwood'``.
    Only a *trailing* one: a parenthesis in the middle of a name is part of the
    name as far as this script is concerned, and guessing otherwise is how a
    match gets manufactured.

    Case and whitespace are folded. Nothing else is. No initials, no nicknames,
    no edit distance. A near miss must fail loudly and land in front of a human
    rather than resolve to the closest warm body.
    """
    name = unicodedata.normalize('NFKD', name or '')
    name = re.sub(r'\s*\([^)]*\)\s*$', '', name)
    return re.sub(r'\s+', ' ', name).strip().lower()


def load_rosters(session):
    """``{kind: {id: name}}`` and ``{kind: {normalized name: [ids]}}``."""
    import sqlalchemy as sa

    by_id = {}
    by_name = {}
    for kind, table in _POOL_TABLES.items():
        rows = session.execute(sa.text(f'SELECT id, name FROM {table}')).all()
        by_id[kind] = {row[0]: row[1] for row in rows}
        index = defaultdict(list)
        for row in rows:
            index[normalize_name(row[1])].append(row[0])
        by_name[kind] = dict(index)
    return by_id, by_name


class Refusal(Exception):
    """A finding this script will not guess at. Carries the reason verbatim."""


def resolve_target(site, by_id, by_name):
    """The live id this reference should have been, or raise :class:`Refusal`.

    The name comes from the position itself when it has one, and otherwise from
    the same blob's ``competitors[]`` entry for the same id. ``name_in_row`` is
    already ``None`` when the blob names that id two different ways, so a
    contested id arrives here nameless and is refused rather than resolved to
    whichever array was walked last.
    """
    if site.kind not in by_name:
        raise Refusal(f'unusable discipline {site.kind!r}; nothing to resolve against')

    name = site.name_in_blob or site.name_in_row
    if not name:
        raise Refusal('no name anywhere in the blob for this id')

    hits = by_name[site.kind].get(normalize_name(name), [])
    if not hits:
        raise Refusal(f'{name!r} matches no live {site.kind} competitor')
    if len(hits) > 1:
        named = ', '.join(f'{i} ({by_id[site.kind][i]!r})' for i in sorted(hits))
        raise Refusal(f'{name!r} matches {len(hits)} live {site.kind} '
                      f'competitors: {named}')
    return hits[0]


def build_plan(findings, by_id, by_name):
    """Turn findings into ``{(store, row_id): {path: new_id}}`` plus refusals.

    Returns ``(plan, refusals, details)``. ``details`` is the ``(site, target)``
    pairs behind the plan, kept separately because the plan is keyed for the
    rewriter and the report needs the site. ``refusals`` is a list of
    ``(site, reason)`` and a non-empty one means ``--apply`` does nothing:
    a partial repair leaves a blob that is half era-1 and half current, which
    is harder to reason about than the blob this started with.
    """
    plan = defaultdict(dict)
    refusals = []
    details = []
    # raw_id -> new_id per blob, to catch a single stale id being sent two
    # different places, and the reverse.
    per_blob = defaultdict(dict)

    for finding in findings:
        site = finding.site
        if site.store not in REPAIRABLE_STORES:
            refusals.append((site, f'store {site.store} is not a JSON event '
                                   f'store; this script does not rewrite it'))
            continue
        try:
            target = resolve_target(site, by_id, by_name)
        except Refusal as exc:
            refusals.append((site, str(exc)))
            continue

        if target not in by_id[site.kind]:
            refusals.append((site, f'resolved to {target}, which is not a live '
                                   f'{site.kind} competitor'))
            continue

        key = (site.store, site.row_id)
        seen = per_blob[key].get(site.raw_id)
        if seen is not None and seen != target:
            refusals.append((site, f'id {site.raw_id} already resolved to '
                                   f'{seen} elsewhere in this blob, and here '
                                   f'to {target}'))
            continue
        per_blob[key][site.raw_id] = target
        plan[key][site.path] = target
        details.append((site, target))

    for key, mapping in per_blob.items():
        collapsed = defaultdict(list)
        for raw_id, target in mapping.items():
            collapsed[target].append(raw_id)
        for target, raw_ids in collapsed.items():
            if len(raw_ids) > 1:
                store, row_id = key
                refusals.append((None, f'{store} row {row_id}: ids '
                                       f'{sorted(raw_ids)} all resolve to '
                                       f'{target}; two competitors cannot '
                                       f'become one'))
    return dict(plan), refusals, details


def scrub(blob, root_path, event_type):
    """A copy with every reference position replaced by its traversal ordinal.

    Two blobs that scrub identically differ only at reference positions, and
    hold the same references in the same order. That is the check that says the
    repair rewrote ids and nothing else: not a placing, not a heat time, not a
    name, not the order of a bracket.
    """
    counter = iter(range(_SCRUB_BASE, _SCRUB_BASE + 10 ** 6))
    working = copy.deepcopy(blob)
    rewrite_blob(working, root_path, event_type, 'scrub', 0,
                 lambda _site: next(counter))
    return working


def repair_blob(blob, root_path, event_type, store, row_id, paths):
    """Apply one blob's slice of the plan. Returns ``(new_blob, changes)``."""
    working = copy.deepcopy(blob)
    changes = rewrite_blob(working, root_path, event_type, store, row_id,
                           lambda site: paths.get(site.path))
    return working, changes


def _event_rows(session, row_ids):
    import sqlalchemy as sa

    if not row_ids:
        return {}
    statement = sa.text(
        'SELECT id, event_type, name, payouts, event_state '
        'FROM events WHERE id IN :ids').bindparams(
            sa.bindparam('ids', expanding=True))
    rows = session.execute(statement, {'ids': sorted(row_ids)}).all()
    return {row[0]: row for row in rows}


def _column_for(store):
    return store.split('.', 1)[1]


def print_plan(plan, refusals, details, events, by_id):
    """The report a human reads before authorising ``--apply``.

    Every line carries the stored name and the resolved name, because the only
    way to audit a name-matched repair by eye is to see both halves of the
    match. An id and an arrow prove nothing.
    """
    by_site = {(s.store, s.row_id, s.path): (s, t) for s, t in details}
    print('--- plan ---')
    if not plan and not refusals:
        print('  nothing to repair')
    for (store, row_id), paths in sorted(plan.items()):
        row = events.get(row_id)
        label = f'{row[2]!r} ({row[1]})' if row else '(row not found)'
        print(f'  {store} row {row_id} {label}: {len(paths)} references')
        for path in sorted(paths):
            site, target = by_site[(store, row_id, path)]
            stored = site.name_in_blob or site.name_in_row
            print(f'    {path}')
            print(f'      {site.raw_id} -> {target}  {stored!r} '
                  f'-> {by_id[site.kind][target]!r} '
                  f'({site.kind}, from {site.kind_source})')
    if refusals:
        print('--- refused ---')
        for site, reason in refusals:
            where = site.path if site is not None else '(whole blob)'
            print(f'  {where}')
            print(f'    {reason}')


def apply_plan(session, plan, events):
    """Rewrite every planned blob. Returns ``{(store, row_id): changes}``."""
    import sqlalchemy as sa

    applied = {}
    for (store, row_id), paths in sorted(plan.items()):
        row = events[row_id]
        column = _column_for(store)
        raw = row[3] if column == 'payouts' else row[4]
        event_type = row[1]
        root_path = f'e{row_id}.{column}'
        before = json.loads(raw)

        after, changes = repair_blob(before, root_path, event_type, store,
                                     row_id, paths)

        if len(changes) != len(paths):
            raise SystemExit(
                f'{store} row {row_id}: planned {len(paths)} rewrites but the '
                f'traversal made {len(changes)}; the plan and the blob disagree')
        if scrub(before, root_path, event_type) != \
                scrub(after, root_path, event_type):
            raise SystemExit(
                f'{store} row {row_id}: the repair changed something other '
                f'than a competitor id')

        session.execute(
            sa.text(f'UPDATE events SET {column} = :value WHERE id = :id'),
            {'value': json.dumps(after), 'id': row_id})
        applied[(store, row_id)] = (after, changes)
    return applied


def post_check(session, plan, applied, before_findings):
    """Every invariant that has to hold before this transaction is allowed to
    commit. Returns a list of defect strings; empty means clean."""
    defects = []

    touched = {key for key in plan}
    after_findings = audit(session)
    for finding in after_findings:
        key = (finding.site.store, finding.site.row_id)
        if key in touched:
            defects.append(f'{finding.site.path} is still {finding.verdict} '
                           f'after the repair')

    before_elsewhere = sorted(
        f.site.path for f in before_findings
        if (f.site.store, f.site.row_id) not in touched)
    after_elsewhere = sorted(
        f.site.path for f in after_findings
        if (f.site.store, f.site.row_id) not in touched)
    if before_elsewhere != after_elsewhere:
        defects.append(f'findings outside the repaired rows changed: '
                       f'{len(before_elsewhere)} -> {len(after_elsewhere)}')

    # The blob that came back out of the database has to be the blob that went
    # in. A wrong-row UPDATE and a JSON round trip that drops a key both look
    # like success up to this point.
    events = _event_rows(session, {row_id for _store, row_id in plan})
    for (store, row_id), (expected, _changes) in sorted(applied.items()):
        column = _column_for(store)
        row = events[row_id]
        stored = json.loads(row[3] if column == 'payouts' else row[4])
        root_path = f'e{row_id}.{column}'
        if walk_blob(stored, root_path, row[1], store, row_id) != \
                walk_blob(expected, root_path, row[1], store, row_id):
            defects.append(f'{store} row {row_id} did not round trip through '
                           f'the database intact')
    return defects


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true',
                      help='measure and print the plan; write nothing')
    mode.add_argument('--apply', action='store_true',
                      help='repair, post-check, and roll back on any defect')
    parser.add_argument('--database-url',
                        help='override DATABASE_URL')
    args = parser.parse_args(argv)

    url = args.database_url or os.environ.get('DATABASE_URL')
    if not url:
        parser.error('no DATABASE_URL set and --database-url not given')

    import sqlalchemy as sa
    from sqlalchemy.orm import Session

    engine = sa.create_engine(url)
    print(f'database: {url.rsplit("/", 1)[-1]}')

    with Session(engine) as session:
        findings = audit(session)
        by_id, by_name = load_rosters(session)
        plan, refusals, details = build_plan(findings, by_id, by_name)
        events = _event_rows(session, {row_id for _store, row_id in plan})

        planned = sum(len(paths) for paths in plan.values())
        print(f'  findings   {len(findings)}')
        print(f'  planned    {planned}')
        print(f'  refused    {len(refusals)}')
        print_plan(plan, refusals, details, events, by_id)

        if args.check:
            return 1 if refusals else 0

        if refusals:
            print('REFUSING TO APPLY: the plan is incomplete. A half-repaired '
                  'blob is worse than the one this started with.')
            return 1
        if not plan:
            print('nothing to repair')
            return 0

        applied = apply_plan(session, plan, events)
        defects = post_check(session, plan, applied, findings)
        if defects:
            session.rollback()
            print('POST-CHECK FAILED, rolled back:')
            for defect in defects:
                print(f'  - {defect}')
            return 1

        session.commit()
        repaired = sum(len(changes) for _blob, changes in applied.values())
        print(f'repaired {repaired} references across {len(applied)} blobs; '
              f'all post-checks passed')
        return 0


if __name__ == '__main__':
    sys.exit(main())
