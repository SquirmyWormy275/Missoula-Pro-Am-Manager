"""Read-only scoring, gear and physical-stand census, with no participant names.

DATABASE_URL supplies the connection. No Flask factory, migrations, score
calculation or repair is invoked. Exit 1 means findings; exit 2 means the audit
could not finish. Reports contain numeric identifiers and aggregate counts only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from models import Event, EventResult, Heat, Tournament  # noqa: E402
from models.competitor import CollegeCompetitor, ProCompetitor  # noqa: E402
from services.gear_sharing import (  # noqa: E402
    competitors_share_gear_for_event,
    event_matches_gear_key,
    gear_partner_names,
    normalize_person_name,
)
from services.heat_generator import (  # noqa: E402
    LH_SPRINGBOARD_STAND,
    _effective_heat_capacity,
    _stand_numbers_for_event,
)
from services.scoring_workflow import _missing_heat_results  # noqa: E402


def audit_session(session: Session) -> dict:
    """Read one snapshot. The caller must enforce a read-only transaction."""
    tournaments = session.scalars(sa.select(Tournament)).all()
    events = session.scalars(sa.select(Event).order_by(Event.id)).all()
    heats = session.scalars(sa.select(Heat).order_by(Heat.id)).all()
    results = session.scalars(sa.select(EventResult)).all()
    competitors = [
        (kind, comp) for kind, model in (('pro', ProCompetitor), ('college', CollegeCompetitor))
        for comp in session.scalars(sa.select(model)).all()
    ]
    pools = {(comp.tournament_id, kind, comp.id): comp for kind, comp in competitors}
    name_pools = defaultdict(dict)
    for kind, comp in competitors:
        name_pools[(comp.tournament_id, kind)][normalize_person_name(comp.name)] = comp
    event_pools = defaultdict(list)
    for event in events:
        event_pools[(event.tournament_id, event.event_type)].append(event)
    result_pools = defaultdict(dict)
    for result in results:
        result_pools[(result.event_id, result.competitor_type)][result.competitor_id] = result
    event_by_id = {event.id: event for event in events}
    findings = []

    def record(code, event=None, heat=None, comp=None, kind=None, **extra):
        item = {'code': code, **extra}
        if event is not None:
            item.update(tournament_id=event.tournament_id, event_id=event.id)
        if heat is not None:
            item['heat_id'] = heat.id
        if comp is not None:
            item.update(tournament_id=comp.tournament_id, competitor_id=comp.id,
                        competitor_type=kind)
        findings.append(item)

    for kind, comp in competitors:
        if comp.status != 'active':
            continue
        peers = name_pools[(comp.tournament_id, kind)]
        family = event_pools[(comp.tournament_id, kind)]
        for key, value in comp.get_gear_sharing().items():
            if not any(event_matches_gear_key(event, key) for event in family):
                record('unmapped_gear_key', comp=comp, kind=kind)
            if str(value or '').strip().lower().startswith('group:'):
                continue
            names = gear_partner_names(value)
            if not names:
                record('blank_gear_partner', comp=comp, kind=kind)
            for name in names:
                partner = peers.get(normalize_person_name(name))
                if partner is None:
                    record('unknown_gear_partner', comp=comp, kind=kind)
                elif partner.id == comp.id:
                    record('self_gear_partner', comp=comp, kind=kind)
                elif partner.status == 'scratched':
                    record('scratched_gear_partner', comp=comp, kind=kind,
                           partner_id=partner.id)

    run_stands = defaultdict(dict)
    for heat in heats:
        event = event_by_id[heat.event_id]
        family = event_pools[(event.tournament_id, event.event_type)]
        rows = result_pools[(event.id, event.event_type)]
        if heat.status == 'completed' or event.is_finalized:
            for competitor_id in _missing_heat_results(heat, event, rows):
                record('finalized_event_missing_result' if event.is_finalized
                       else 'completed_heat_missing_result', event=event, heat=heat,
                       competitor_id=competitor_id, competitor_type=event.event_type)
        roster = []
        for assignment in heat.assignments:
            comp = pools.get((event.tournament_id, assignment.competitor_type,
                              assignment.competitor_id))
            if comp is None or assignment.competitor_type != event.event_type:
                record('invalid_heat_competitor', event=event, heat=heat,
                       competitor_id=assignment.competitor_id)
            else:
                roster.append(comp)
        for first, second in combinations(roster, 2):
            # A partnered saw team is one physical stand unit. Its members
            # intentionally share equipment; conflicts concern other units.
            first_stand = heat.get_stand_for_competitor(first.id)
            if (event.is_partnered and first_stand is not None
                    and first_stand == heat.get_stand_for_competitor(second.id)):
                continue
            if competitors_share_gear_for_event(
                first.name, first.get_gear_sharing(), second.name,
                second.get_gear_sharing(), event, all_events=family,
            ):
                record('heat_gear_conflict', event=event, heat=heat,
                       competitor_ids=[first.id, second.id])
        stand_config = config.STAND_CONFIGS.get(event.stand_type)
        if not stand_config:
            continue  # Bracket/mixed events may have no physical stand roster.
        configured = event.max_stands if event.max_stands is not None else stand_config['total']
        legal = _stand_numbers_for_event(event, configured, stand_config)
        capacity = _effective_heat_capacity(event, configured, legal)
        assignments = heat.get_stand_assignments()
        occupancy = len(set(assignments.values())) if event.is_partnered else len(roster)
        if occupancy > capacity:
            record('heat_over_capacity', event=event, heat=heat)
        # Partnered events intentionally put two people at one stand.
        if not event.is_partnered:
            used = Counter(assignments.values())
            if any(count > 1 for count in used.values()):
                record('duplicate_stand', event=event, heat=heat)
        left_handed = [comp for comp in roster if getattr(comp, 'is_left_handed_springboard', False)]
        if event.stand_type == 'springboard' and len(left_handed) > 1:
            record('multiple_left_handed_springboard', event=event, heat=heat)
        for comp in roster:
            stand = assignments.get(str(comp.id))
            if stand not in legal:
                record('invalid_or_missing_stand', event=event, heat=heat,
                       competitor_id=comp.id)
            if (event.stand_type == 'springboard' and getattr(comp, 'is_left_handed_springboard', False)
                    and stand != LH_SPRINGBOARD_STAND):
                record('left_handed_wrong_stand', event=event, heat=heat,
                       competitor_id=comp.id)
            if event.requires_dual_runs and stand is not None:
                previous = run_stands[(event.id, comp.id)]
                if any(run != heat.run_number and prior_stand == stand
                       for run, prior_stand in previous.items()):
                    record('dual_run_repeated_stand', event=event, heat=heat,
                           competitor_id=comp.id)
                previous[heat.run_number] = stand

    return {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'read_only': True,
        'counts': {'tournaments': len(tournaments), 'events': len(events),
                   'heats': len(heats), 'results': len(results),
                   'competitors': len(competitors),
                   'finalized_events': sum(bool(event.is_finalized) for event in events),
                   'completed_heats': sum(heat.status == 'completed' for heat in heats)},
        'finding_counts': dict(sorted(Counter(item['code'] for item in findings).items())),
        'findings': findings,
        'limitations': [
            'Only entrants present in authoritative heat rosters are checked for missing scores.',
            'Previously overwritten gear declarations require an original import or backup to recover.',
            'Historical findings are reported without changing scores, rosters or declarations.',
        ],
    }


def audit_database(url: str) -> dict:
    if url.startswith('postgres://'):
        url = 'postgresql://' + url[len('postgres://'):]
    postgres = sa.engine.make_url(url).get_backend_name() == 'postgresql'
    connect_args = ({'connect_timeout': 10, 'options': '-c default_transaction_read_only=on'}
                    if postgres else {})
    engine = sa.create_engine(url, connect_args=connect_args)
    try:
        with engine.connect() as connection:
            if postgres:
                connection = connection.execution_options(isolation_level='REPEATABLE READ')
            with connection.begin():
                if postgres:
                    connection.exec_driver_sql('SET TRANSACTION READ ONLY')
                    connection.exec_driver_sql("SET LOCAL statement_timeout = '30s'")
                    connection.exec_driver_sql("SET LOCAL lock_timeout = '2s'")
                else:
                    connection.exec_driver_sql('PRAGMA query_only = ON')
                with Session(bind=connection, autoflush=False) as session:
                    report = audit_session(session)
                connection.rollback()
                return report
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    url = os.environ.get('DATABASE_URL', '')
    if not url:
        print('Audit unavailable: DATABASE_URL is not configured.', file=sys.stderr)
        return 2
    try:
        report = audit_database(url)
        with args.output.open('x', encoding='utf-8') as stream:
            json.dump(report, stream, indent=2)
            stream.write('\n')
    except Exception as exc:
        # Connection/SQL exceptions can contain credentials, queries or names.
        print(f'Audit failed ({type(exc).__name__}; {_failure_category(exc)}); '
              'sensitive details suppressed.', file=sys.stderr)
        return 2
    print(json.dumps({'counts': report['counts'], 'finding_counts': report['finding_counts'],
                      'read_only': report['read_only']}))
    return 1 if report['findings'] else 0


def _failure_category(error) -> str:
    """Classify the failure without returning any database diagnostic text."""
    message = str(getattr(error, 'orig', error)).lower()
    if re.search(r'\bdatabase\s+"[^"\r\n]*"\s+does not exist', message):
        return 'configured database does not exist'
    if re.search(r'\brole\s+"[^"\r\n]*"\s+does not exist', message):
        return 'configured database role does not exist'
    if re.search(r'\b(?:relation|column)\s+"[^"\r\n]*"\s+does not exist', message):
        return 'schema mismatch'
    for needle, category in (
        ('password authentication failed', 'authentication rejected'),
        ('could not translate host name', 'hostname lookup failed'),
        ('connection refused', 'connection refused'),
        ('timeout expired', 'connection timed out'),
        ('timed out', 'connection timed out'),
        ('server closed the connection', 'connection closed by server'),
        ('read-only transaction', 'database rejected a write'),
        ('does not exist', 'referenced database object unavailable'),
        ('certificate', 'TLS certificate error'),
    ):
        if needle in message:
            return category
    return 'unclassified database failure'


if __name__ == '__main__':
    raise SystemExit(main())
