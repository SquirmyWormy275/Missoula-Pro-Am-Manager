import io
import json
import os
import tempfile

from models.audit_log import AuditLog
from models.user import User
from routes import reporting as reporting_routes
from tests.conftest import make_tournament


def _login_as(client, user_id: int) -> None:
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)


def _make_user(db_session, username: str, role: str = 'admin') -> User:
    user = User(username=username, role=role)
    user.set_password('testpass123')
    db_session.add(user)
    db_session.flush()
    return user


def _make_logged_in_client(app, db_session, username: str, role: str = 'admin'):
    client = app.test_client()
    user = _make_user(db_session, username=username, role=role)
    _login_as(client, user.id)
    return client, user


def test_clone_tournament_writes_audit_log(app, db_session):
    client, _admin = _make_logged_in_client(app, db_session, 'clone_admin')
    tournament = make_tournament(db_session, name='Source Tournament', year=2026)

    response = client.post(f'/tournament/{tournament.id}/clone', follow_redirects=False)

    assert response.status_code == 302
    entry = (
        AuditLog.query
        .filter_by(action='tournament_cloned')
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert entry is not None
    details = json.loads(entry.details_json)
    assert details['source_id'] == tournament.id
    assert details['source_name'] == 'Source Tournament'


def test_tournament_settings_update_writes_audit_log(app, db_session):
    client, _admin = _make_logged_in_client(app, db_session, 'settings_admin')
    tournament = make_tournament(db_session, name='Before Name', year=2025)

    response = client.post(
        f'/tournament/{tournament.id}/setup/settings',
        data={
            'name': 'After Name',
            'year': '2026',
            'college_date': '2026-05-01',
            'pro_date': '2026-05-02',
            'providing_shirts': '1',
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    entry = (
        AuditLog.query
        .filter_by(action='tournament_settings_updated', entity_id=tournament.id)
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert entry is not None
    details = json.loads(entry.details_json)
    assert details['name'] == 'After Name'
    assert details['year'] == 2026
    assert details['college_date'] == '2026-05-01'
    assert details['pro_date'] == '2026-05-02'
    assert details['providing_shirts'] is True


def test_self_disable_attempt_is_audited(app, db_session):
    client, admin_user = _make_logged_in_client(app, db_session, 'self_disable_admin')
    tournament = make_tournament(db_session)

    response = client.post(
        f'/auth/users/{admin_user.id}/toggle-active',
        data={'tournament_id': tournament.id},
        follow_redirects=False,
    )

    assert response.status_code == 302
    entry = (
        AuditLog.query
        .filter_by(action='user_toggle_active_denied', entity_id=admin_user.id)
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert entry is not None
    details = json.loads(entry.details_json)
    assert details['reason'] == 'self_disable_attempt'


def test_restore_failure_writes_audit_log(app, db_session):
    client, _admin = _make_logged_in_client(app, db_session, 'restore_admin')
    tournament = make_tournament(db_session)
    db_session.commit()

    response = client.post(
        f'/reporting/{tournament.id}/restore',
        data={'backup_file': (io.BytesIO(b'SQLite format 3\x00broken payload'), 'broken.db')},
        content_type='multipart/form-data',
        follow_redirects=False,
    )

    assert response.status_code == 302
    entry = (
        AuditLog.query
        .filter_by(action='database_restore_failed', entity_id=tournament.id)
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert entry is not None
    details = json.loads(entry.details_json)
    assert details['filename'] == 'broken.db'
    assert details['error']


def test_ala_email_success_writes_audit_log(app, db_session, monkeypatch):
    client, _admin = _make_logged_in_client(app, db_session, 'ala_admin')
    tournament = make_tournament(db_session)
    db_session.commit()
    fd, pdf_path = tempfile.mkstemp(prefix='ala-report-', suffix='.pdf')
    os.close(fd)
    with open(pdf_path, 'wb') as handle:
        handle.write(b'%PDF-1.4\n%test\n')

    from services import ala_report as ala_report_module

    monkeypatch.setattr(
        ala_report_module,
        'build_ala_report',
        lambda _tournament: {
            'all_attendees': [],
            'non_members': [],
            'generated_at': '2026-04-20T12:00:00Z',
            'year': tournament.year,
        },
    )
    monkeypatch.setattr(ala_report_module, 'generate_ala_pdf', lambda _report: str(pdf_path))
    monkeypatch.setattr(reporting_routes, '_send_ala_email', lambda *_args, **_kwargs: None)

    try:
        response = client.post(
            f'/reporting/ala-membership-report/{tournament.id}/email',
            follow_redirects=False,
        )

        assert response.status_code == 302
        entry = (
            AuditLog.query
            .filter_by(action='ala_report_emailed', entity_id=tournament.id)
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert entry is not None
        details = json.loads(entry.details_json)
        assert details['recipient'] == reporting_routes.ALA_EMAIL
    finally:
        try:
            os.remove(pdf_path)
        except OSError:
            pass


def test_judge_cannot_restore_database(app, db_session, judge_user):
    tournament = make_tournament(db_session)
    client = app.test_client()
    _login_as(client, judge_user.id)

    response = client.post(
        f'/reporting/{tournament.id}/restore',
        data={'backup_file': (io.BytesIO(b'SQLite format 3\x00broken payload'), 'broken.db')},
        content_type='multipart/form-data',
        follow_redirects=False,
    )

    assert response.status_code == 403
