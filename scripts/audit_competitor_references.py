"""Report competitor references that point at the wrong person, or at nobody.

Read-only. Repairs nothing, writes nothing, and is safe to run against a live
race-day database or a production dump.

Usage
=====
    python scripts/audit_competitor_references.py
    python scripts/audit_competitor_references.py --database-url postgresql://...
    python scripts/audit_competitor_references.py --all

Without ``--all`` it prints only ``cross_kind`` findings, which are the ones
that put a real, wrong human's name on a heat sheet. Everything else is
suppressed because on the 2026 dump there is nothing else: all 55 findings are
cross_kind, none are merely dangling.

Exit codes
==========
    0   nothing found
    1   findings present

Non-zero on findings so this can be dropped into a preflight or a CI step
later without rewriting it. It is not wired into either today.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.reference_audit import (  # noqa: E402
    CROSS_KIND,
    UNKNOWN_KIND,
    audit,
    summarize,
)


def _severity(finding):
    """Sort key. Wrong-person first, then unresolvable discriminators, then
    the merely absent, then by store and path so runs are diffable."""
    rank = {CROSS_KIND: 0, UNKNOWN_KIND: 1}.get(finding.verdict, 2)
    return (rank, finding.site.store, finding.site.path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument(
        '--database-url',
        help='Override DATABASE_URL. Useful for pointing at a dump restore.')
    parser.add_argument(
        '--all', action='store_true',
        help='Show every finding, not just the wrong-person ones.')
    args = parser.parse_args(argv)

    if args.database_url:
        os.environ['DATABASE_URL'] = args.database_url

    import sqlalchemy as sa
    from sqlalchemy.orm import Session

    url = os.environ.get('DATABASE_URL')
    if not url:
        parser.error('no DATABASE_URL set and --database-url not given')

    engine = sa.create_engine(url)
    with Session(engine) as session:
        findings = audit(session)

    summary = summarize(findings)
    print(f'database: {url.rsplit("/", 1)[-1]}')
    print(f'  findings                    {summary["total"]}')
    print(f'    wrong person (cross_kind) {summary[CROSS_KIND]}')
    print(f'    resolves to nobody        {summary["dangling"]}')
    print(f'    unusable discriminator    {summary[UNKNOWN_KIND]}')
    print(f'    repairable from the blob  {summary["repairable_from_blob"]}')
    print(f'    regenerate only           {summary["not_repairable"]}')
    for store, count in sorted(summary['by_store'].items()):
        print(f'      {store:28s} {count}')

    shown = findings if args.all else [
        f for f in findings if f.verdict == CROSS_KIND]
    if shown:
        print()
        for finding in sorted(shown, key=_severity):
            site = finding.site
            target = finding.collides_with or '(nobody)'
            stored = site.name_in_blob or '(no name stored)'
            print(f'  {site.path}')
            print(f'    id={site.raw_id} read as {site.kind} '
                  f'(from {site.kind_source}); stored name {stored!r}; '
                  f'this id is {target!r} in the other discipline')

    return 1 if findings else 0


if __name__ == '__main__':
    sys.exit(main())
