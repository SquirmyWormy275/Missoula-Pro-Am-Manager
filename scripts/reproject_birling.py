"""Rebuild the projected birling rows for named events from their documents.

Why this exists
===============
D13-C commit A3b made the five birling tables the truth the manage page and
the index page read. Everything that writes a bracket through the app writes
the rows alongside the JSON, because ``services.birling_bracket`` calls
``services.birling_rows.project`` on every save. Nothing that writes a bracket
around the app does.

``scripts/repair_era1_references.py`` is the one that matters. It rewrites
``events.payouts`` through raw SQL on a bare session with no Flask app
context, so it cannot project, and its whole purpose is turning documents that
were unprojectable into documents that are projectable. Left alone it would
leave rows behind on exactly the events whose rows were most wrong: the reader
would fall back to the repaired JSON and the judge would see the right bracket,
but the tables would still hold the ghost references the repair just removed,
and D13-C A4 takes that fallback away.

So the repair names the events it touched and this script rebuilds their rows.
Any other out-of-band edit to a bracket document has the same obligation.

Why it is a second transaction and not part of the repair
=========================================================
``create_app`` builds its own engine and its own connection. A projection run
under an app context cannot see the repair's uncommitted UPDATEs, so folding
this into the repair would mean either committing the repair first, which
throws away its all-or-nothing rollback, or rewriting the repair onto an app
context, which is a much larger change to a script already proven against the
production mirror.

Two transactions are safe here because this one is idempotent. ``project``
clears an event's rows and rewrites them from the document every time, so a
run that dies halfway is fixed by running it again, and a run over events that
were already correct changes nothing observable.

Usage
=====
    python -m scripts.reproject_birling 12 14 15
    python -m scripts.reproject_birling --database-url postgresql://... 12 14
    python -m scripts.reproject_birling --dry-run 12 14 15

``--database-url`` is pushed into ``DATABASE_URL`` before the app is imported,
which is how ``scripts/audit_competitor_references.py`` and
``scripts/ci_build_smoke_db.py`` point the app at a dump restore. Give it the
same value you gave the repair, or you will project one database's rows from
another database's documents.

An event id that is not a bracket at all is not an error. ``project`` clears
its rows and writes none, which is the correct state for an event that has no
bracket to hold.

Exit codes
==========
    0   every named event projected, or the dry run found nothing refused
    1   an event refused projection, an id named nothing, or the run aborted
    2   usage

Note for D13-C A4
=================
This reads refusals off ``Plan.reasons``. A3c makes ``project`` raise on a
refusal instead, at which point the ``refused`` branch below stops firing and
the abort path starts catching what it used to report. Revisit both together.
"""

from __future__ import annotations

import argparse
import os
import sys


def reproject(event_ids, dry_run: bool = False) -> dict:
    """Rebuild rows for each event id. Requires an app context.

    Returns a summary dict. The whole run is one transaction: an id that
    resolves to nothing or a projection that raises aborts it, because a
    projection that raises is not a refusal, it is a defect, and continuing
    over a session that has already failed a flush reports noise.
    """
    from database import db
    from models import Event
    from services import birling_rows

    summary = {
        'dry_run': dry_run,
        'projected': [],
        'refused': [],
        'aborted': None,
    }

    for event_id in event_ids:
        event = db.session.get(Event, event_id)
        if event is None:
            db.session.rollback()
            summary['aborted'] = 'no event %s' % event_id
            return summary
        try:
            plan = birling_rows.project(event)
        except Exception as exc:  # noqa: BLE001  reported, not swallowed
            db.session.rollback()
            summary['aborted'] = 'event %s raised %s: %s' % (
                event_id, type(exc).__name__, exc)
            return summary

        if plan.reasons:
            summary['refused'].append({
                'event_id': event_id,
                'reasons': sorted(plan.reasons),
            })
        else:
            summary['projected'].append({
                'event_id': event_id,
                'seeds': len(plan.seeds),
                'pre_seeds': len(plan.pre_seeds),
                'matches': len(plan.matches),
                'placements': len(plan.placements),
            })

    if dry_run:
        db.session.rollback()
    else:
        db.session.commit()
    return summary


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('event_ids', nargs='+', type=int,
                        help='the event ids whose rows to rebuild')
    parser.add_argument('--database-url',
                        help='override DATABASE_URL before the app is built')
    parser.add_argument('--dry-run', action='store_true',
                        help='report what would be written and roll back')
    args = parser.parse_args(argv)

    if args.database_url:
        os.environ['DATABASE_URL'] = args.database_url

    from app import create_app

    app = create_app()
    with app.app_context():
        summary = reproject(args.event_ids, dry_run=args.dry_run)

    if summary['aborted']:
        print('ABORTED, nothing written: %s' % summary['aborted'])
        return 1

    verb = 'would project' if summary['dry_run'] else 'projected'
    print('%s %d event(s), %d refused'
          % (verb, len(summary['projected']), len(summary['refused'])))
    for row in summary['projected']:
        print('  event %s  seeds=%d pre_seeds=%d matches=%d placements=%d'
              % (row['event_id'], row['seeds'], row['pre_seeds'],
                 row['matches'], row['placements']))
    if summary['refused']:
        print('--- refused, rows cleared and no rows written ---')
        for row in summary['refused']:
            print('  event %s' % row['event_id'])
            for reason in row['reasons']:
                print('    %s' % reason)
        print('A refusal here after a repair means the repair did not finish '
              'the job. Run scripts/repair_era1_references.py --check.')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(_main())
