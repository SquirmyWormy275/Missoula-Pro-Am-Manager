"""Audit completeness, privacy and database-enforced write protection."""
import json

import pytest
import sqlalchemy as sa

from scripts import audit_tournament_workflows as auditor
from tests.conftest import make_event, make_heat, make_pro_competitor, make_tournament


def test_audit_reports_missing_finalized_scores_and_gear_without_names(db_session):
    tournament = make_tournament(db_session)
    event = make_event(db_session, tournament, 'Underhand', max_stands=4)
    event.is_finalized = True
    first = make_pro_competitor(db_session, tournament, 'Private Owner',
                              gear_sharing={str(event.id): 'Private Partner'})
    second = make_pro_competitor(db_session, tournament, 'Private Partner')
    heat = make_heat(db_session, event, competitors=[first.id, second.id], status='completed',
                     stand_assignments={str(first.id): 1, str(second.id): 2})

    report = auditor.audit_session(db_session)

    assert report['finding_counts'] == {'finalized_event_missing_result': 2, 'heat_gear_conflict': 1}
    assert {finding['heat_id'] for finding in report['findings']} == {heat.id}
    assert 'Private' not in json.dumps(report)


def test_audit_counts_partnered_stands_rather_than_people(db_session):
    tournament = make_tournament(db_session)
    event = make_event(db_session, tournament, 'Partnered heat', max_stands=2, is_partnered=True)
    roster = [make_pro_competitor(db_session, tournament, f'Pair member {i}') for i in range(4)]
    roster[0].gear_sharing = json.dumps({str(event.id): roster[1].name})
    make_heat(db_session, event, competitors=[comp.id for comp in roster],
              stand_assignments={str(comp.id): 1 + index // 2 for index, comp in enumerate(roster)})

    report = auditor.audit_session(db_session)

    assert report['findings'] == []


def test_audit_detects_sharing_between_different_partnered_stand_units(db_session):
    tournament = make_tournament(db_session)
    event = make_event(db_session, tournament, 'Partnered heat', max_stands=2, is_partnered=True)
    roster = [make_pro_competitor(db_session, tournament, f'Other pair member {i}') for i in range(4)]
    roster[0].gear_sharing = json.dumps({str(event.id): roster[2].name})
    make_heat(db_session, event, competitors=[comp.id for comp in roster],
              stand_assignments={str(comp.id): 1 + index // 2 for index, comp in enumerate(roster)})

    assert auditor.audit_session(db_session)['finding_counts'] == {'heat_gear_conflict': 1}


def test_audit_checks_scratched_names_inside_multi_partner_values(db_session):
    tournament = make_tournament(db_session)
    event = make_event(db_session, tournament, 'Underhand')
    owner = make_pro_competitor(db_session, tournament, 'Owner',
                              gear_sharing={str(event.id): 'partners:Scratched|Active'})
    scratched = make_pro_competitor(db_session, tournament, 'Scratched', status='scratched')
    make_pro_competitor(db_session, tournament, 'Active')

    report = auditor.audit_session(db_session)

    assert report['finding_counts'] == {'scratched_gear_partner': 1}
    assert report['findings'][0]['competitor_id'] == owner.id
    assert report['findings'][0]['partner_id'] == scratched.id


def test_database_audit_cannot_write_and_preserves_bytes(tmp_path, monkeypatch):
    path = tmp_path / 'read-only.db'
    url = f'sqlite:///{path}'
    engine = sa.create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE sentinel (value TEXT)')
        connection.exec_driver_sql("INSERT INTO sentinel VALUES ('keep')")
    engine.dispose()
    before = path.read_bytes()

    def attempted_write(session):
        session.execute(sa.text("UPDATE sentinel SET value = 'changed'"))

    monkeypatch.setattr(auditor, 'audit_session', attempted_write)
    with pytest.raises(sa.exc.OperationalError, match='readonly'):
        auditor.audit_database(url)
    assert path.read_bytes() == before


def test_postgres_audit_connection_rejects_writes(app, monkeypatch):
    url = app.config['SQLALCHEMY_DATABASE_URI']
    if not url.startswith('postgresql://'):
        pytest.skip('PostgreSQL read-only enforcement requires the unit-postgres job')

    def attempted_write(session):
        session.execute(sa.text("UPDATE tournaments SET name = 'must not write'"))

    monkeypatch.setattr(auditor, 'audit_session', attempted_write)
    with pytest.raises(sa.exc.DBAPIError, match='read-only'):
        auditor.audit_database(url)


def test_failed_audit_reports_a_category_without_sensitive_details(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('DATABASE_URL', 'postgresql://user:DO_NOT_DISCLOSE@example.invalid/db')
    monkeypatch.setattr('sys.argv', ['audit', '--output', str(tmp_path / 'unused.json')])

    def connection_failure(url):
        raise sa.exc.OperationalError('private query', {},
                                      RuntimeError('password authentication failed DO_NOT_DISCLOSE'))

    monkeypatch.setattr(auditor, 'audit_database', connection_failure)
    assert auditor.main() == 2
    output = capsys.readouterr()
    assert 'authentication rejected' in output.err
    assert 'DO_NOT_DISCLOSE' not in output.err
    assert 'private query' not in output.err
    assert not (tmp_path / 'unused.json').exists()


@pytest.mark.parametrize(('diagnostic', 'category'), [
    ('FATAL: database "DO_NOT_DISCLOSE" does not exist', 'configured database does not exist'),
    ('FATAL: role "DO_NOT_DISCLOSE" does not exist', 'configured database role does not exist'),
    ('relation "DO_NOT_DISCLOSE" does not exist', 'schema mismatch'),
    ('column "DO_NOT_DISCLOSE" does not exist', 'schema mismatch'),
    ('DO_NOT_DISCLOSE does not exist', 'referenced database object unavailable'),
])
def test_missing_database_and_role_are_not_classified_as_schema_mismatches(diagnostic, category):
    error = sa.exc.OperationalError('DO_NOT_DISCLOSE query', {}, RuntimeError(diagnostic))
    assert auditor._failure_category(error) == category
