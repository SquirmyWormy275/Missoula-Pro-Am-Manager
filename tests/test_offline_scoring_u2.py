from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from database import db
from models import AuditLog, EventResult, ScoreSubmissionReceipt, User
from services import scoring_workflow
from services.scoring_workflow import (
    canonical_score_payload_sha256,
    heat_scoring_state_digest,
    heat_submission_identity,
    save_heat_results_submission,
)
from tests.conftest import (
    make_college_competitor,
    make_event,
    make_heat,
    make_team,
    make_tournament,
)

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope='module', autouse=True)
def _temporary_receipt_table(app):
    """The lead owns the combined migration; focused tests still need its table."""
    with app.app_context():
        ScoreSubmissionReceipt.__table__.create(bind=db.engine, checkfirst=True)
    yield
    with app.app_context():
        db.session.remove()
        ScoreSubmissionReceipt.__table__.drop(bind=db.engine, checkfirst=True)


def _seed_score(db_session, *, user_id: int):
    tournament = make_tournament(db_session, name=f'Offline Receipt {uuid4()}')
    team = make_team(db_session, tournament, code=f'T-{tournament.id}')
    competitor = make_college_competitor(
        db_session, tournament, team, f'Scorer {tournament.id}'
    )
    event = make_event(
        db_session,
        tournament,
        'Standing Block Hard Hit',
        event_type='college',
        scoring_type='hits',
        scoring_order='highest_wins',
    )
    heat = make_heat(
        db_session,
        event,
        competitors=[competitor.id],
        stand_assignments={str(competitor.id): 1},
    )
    request_id = str(uuid4())
    form_data = {
        'request_id': request_id,
        'tournament_id': str(tournament.id),
        'heat_id': str(heat.id),
        'issuer_user_id': str(user_id),
        'issuer_role': 'admin',
        'schedule_fingerprint': 'schedule-a',
        'queued_at': datetime.now(timezone.utc).isoformat(),
        'csrf_token': 'transport-only',
        'replay_token': 'transport-only',
        'heat_version': str(heat.version_id),
        'heat_identity': heat_submission_identity(heat),
        'scoring_state_digest': heat_scoring_state_digest(heat, event),
        f'result_{competitor.id}': '9',
        f'status_{competitor.id}': 'completed',
        f'reason_{competitor.id}': '',
    }
    return tournament, event, heat, competitor, request_id, form_data


def _make_user(db_session, *, role=User.ROLE_ADMIN):
    user = User(username=f'u2-{uuid4()}', role=role)
    user.set_password('test-password')
    db_session.add(user)
    db_session.flush()
    return user


def test_canonical_payload_ignores_transport_credentials_and_binds_route_ids():
    first = {
        'request_id': 'afca031f-194d-4e09-830c-fdbe25f483c0',
        'csrf_token': 'old',
        'replay_token': 'old-replay',
        'heat_version': '3',
        'heat_identity': 'heat-instance',
        'result_9': '12.40',
        'status_9': 'completed',
        'reason_9': '',
    }
    second = dict(reversed(list(first.items())))
    second['csrf_token'] = 'renewed'
    second['replay_token'] = 'renewed-replay'
    second['tournament_id'] = '999'
    second['heat_id'] = '999'

    assert canonical_score_payload_sha256(first, tournament_id=7, heat_id=11) == (
        canonical_score_payload_sha256(second, tournament_id=7, heat_id=11)
    )


def test_exact_duplicate_returns_receipt_without_second_mutation_or_audit(
    db_session, monkeypatch,
):
    admin_user = _make_user(db_session)
    tournament, event, heat, competitor, request_id, form_data = _seed_score(
        db_session, user_id=admin_user.id
    )

    first = save_heat_results_submission(
        tournament_id=tournament.id,
        heat=heat,
        event=event,
        form_data=form_data,
        judge_user_id=admin_user.id,
    )
    monkeypatch.setattr(
        scoring_workflow,
        'lock_tournament_schedule',
        lambda _tournament_id: pytest.fail(
            'committed receipt retry reached the mutable schedule gate'
        ),
    )
    duplicate = save_heat_results_submission(
        tournament_id=tournament.id,
        heat=heat,
        event=event,
        form_data=form_data,
        judge_user_id=admin_user.id,
    )

    assert first['ok'] is True
    assert first['receipt']['request_id'] == request_id
    assert duplicate['ok'] is True
    assert duplicate['receipt_replayed'] is True
    assert duplicate['receipt'] == first['receipt']
    assert EventResult.query.filter_by(event_id=event.id).count() == 1
    assert ScoreSubmissionReceipt.query.filter_by(request_id=request_id).count() == 1
    assert AuditLog.query.filter_by(
        action='heat_results_saved', entity_type='heat', entity_id=heat.id
    ).count() == 1
    assert EventResult.query.filter_by(event_id=event.id).one().result_value == 9


def test_new_submission_still_refuses_foreign_heat_lock(db_session):
    admin_user = _make_user(db_session)
    other_user = _make_user(db_session, role=User.ROLE_JUDGE)
    tournament, event, heat, competitor, _request_id, form_data = _seed_score(
        db_session, user_id=admin_user.id
    )
    heat.locked_by_user_id = other_user.id
    heat.locked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db_session.commit()

    outcome = save_heat_results_submission(
        tournament_id=tournament.id,
        heat=heat,
        event=event,
        form_data=form_data,
        judge_user_id=admin_user.id,
    )

    assert outcome['ok'] is False
    assert outcome['status_code'] == 423
    assert outcome['error_code'] == 'heat_locked'
    assert EventResult.query.filter_by(
        event_id=event.id, competitor_id=competitor.id
    ).count() == 0


def test_same_judge_lock_refresh_does_not_stale_offline_score(db_session):
    admin_user = _make_user(db_session)
    tournament, event, heat, competitor, _request_id, form_data = _seed_score(
        db_session, user_id=admin_user.id
    )
    prepared_version = heat.version_id

    assert heat.acquire_lock(admin_user.id) is True
    db_session.commit()
    assert heat.version_id > prepared_version

    outcome = save_heat_results_submission(
        tournament_id=tournament.id,
        heat=heat,
        event=event,
        form_data=form_data,
        judge_user_id=admin_user.id,
    )

    assert outcome['ok'] is True
    assert EventResult.query.filter_by(
        event_id=event.id, competitor_id=competitor.id
    ).one().result_value == 9


def test_scoring_state_digest_rejects_score_changed_after_offline_prepare(db_session):
    admin_user = _make_user(db_session)
    tournament, event, heat, competitor, _request_id, stale_form = _seed_score(
        db_session, user_id=admin_user.id
    )
    first_form = dict(stale_form)
    first_form['request_id'] = str(uuid4())

    accepted = save_heat_results_submission(
        tournament_id=tournament.id,
        heat=heat,
        event=event,
        form_data=first_form,
        judge_user_id=admin_user.id,
    )
    assert accepted['ok'] is True

    stale_form['request_id'] = str(uuid4())
    stale_form['heat_version'] = str(heat.version_id)
    rejected = save_heat_results_submission(
        tournament_id=tournament.id,
        heat=heat,
        event=event,
        form_data=stale_form,
        judge_user_id=admin_user.id,
    )

    assert rejected['ok'] is False
    assert rejected['status_code'] == 409
    assert rejected['error_code'] == 'scoring_state_changed'
    assert EventResult.query.filter_by(
        event_id=event.id, competitor_id=competitor.id
    ).one().result_value == 9


def test_request_id_payload_drift_is_rejected(db_session):
    admin_user = _make_user(db_session)
    tournament, event, heat, competitor, request_id, form_data = _seed_score(
        db_session, user_id=admin_user.id
    )
    accepted = save_heat_results_submission(
        tournament_id=tournament.id,
        heat=heat,
        event=event,
        form_data=form_data,
        judge_user_id=admin_user.id,
    )
    changed = dict(form_data)
    changed[f'result_{competitor.id}'] = '12'

    rejected = save_heat_results_submission(
        tournament_id=tournament.id,
        heat=heat,
        event=event,
        form_data=changed,
        judge_user_id=admin_user.id,
    )

    assert accepted['ok'] is True
    assert rejected['ok'] is False
    assert rejected['status_code'] == 409
    assert rejected['error_code'] == 'request_id_payload_mismatch'
    assert EventResult.query.filter_by(event_id=event.id).one().result_value == 9


def test_request_id_user_rebinding_is_rejected(db_session):
    admin_user = _make_user(db_session)
    other = _make_user(db_session, role=User.ROLE_JUDGE)
    tournament, event, heat, _competitor, _request_id, form_data = _seed_score(
        db_session, user_id=admin_user.id
    )
    accepted = save_heat_results_submission(
        tournament_id=tournament.id,
        heat=heat,
        event=event,
        form_data=form_data,
        judge_user_id=admin_user.id,
    )

    rejected = save_heat_results_submission(
        tournament_id=tournament.id,
        heat=heat,
        event=event,
        form_data=form_data,
        judge_user_id=other.id,
    )

    assert accepted['ok'] is True
    assert rejected['ok'] is False
    assert rejected['status_code'] == 409
    assert rejected['error_code'] == 'request_id_binding_mismatch'


def test_removed_receipt_issuer_retains_tombstone_and_disables_replay(db_session):
    original_user = _make_user(db_session)
    replacement_user = _make_user(db_session)
    tournament, event, heat, _competitor, request_id, form_data = _seed_score(
        db_session, user_id=original_user.id
    )
    accepted = save_heat_results_submission(
        tournament_id=tournament.id,
        heat=heat,
        event=event,
        form_data=form_data,
        judge_user_id=original_user.id,
    )
    assert accepted['ok'] is True
    db_session.commit()

    receipt = db.session.get(ScoreSubmissionReceipt, request_id)
    receipt.issuing_user_id = None
    db_session.commit()
    db_session.expire_all()
    receipt = db.session.get(ScoreSubmissionReceipt, request_id)
    assert receipt is not None
    assert receipt.issuing_user_id is None

    replay = save_heat_results_submission(
        tournament_id=tournament.id,
        heat=heat,
        event=event,
        form_data=form_data,
        judge_user_id=replacement_user.id,
    )

    assert replay['ok'] is False
    assert replay['status_code'] == 409
    assert replay['error_code'] == 'request_id_issuer_deleted'


def test_receipt_failure_rolls_back_score_heat_and_saved_audit(
    db_session, monkeypatch
):
    admin_user = _make_user(db_session)
    tournament, event, heat, _competitor, request_id, form_data = _seed_score(
        db_session, user_id=admin_user.id
    )
    db_session.commit()

    def _fail_receipt(*_args, **_kwargs):
        raise IntegrityError('forced receipt failure', {}, RuntimeError('forced'))

    monkeypatch.setattr(
        scoring_workflow, '_create_submission_receipt', _fail_receipt, raising=False
    )
    outcome = save_heat_results_submission(
        tournament_id=tournament.id,
        heat=heat,
        event=event,
        form_data=form_data,
        judge_user_id=admin_user.id,
    )

    assert outcome['ok'] is False
    assert ScoreSubmissionReceipt.query.filter_by(request_id=request_id).count() == 0
    assert EventResult.query.filter_by(event_id=event.id).count() == 0
    assert db.session.get(type(heat), heat.id).status == 'pending'
    assert AuditLog.query.filter_by(
        action='heat_results_saved', entity_type='heat', entity_id=heat.id
    ).count() == 0


def test_replay_rejects_entries_older_than_thirty_days_without_mutation(
    client, db_session,
):
    admin_user = _make_user(db_session)
    tournament, event, heat, competitor, _request_id, form_data = _seed_score(
        db_session, user_id=admin_user.id
    )
    form_data['queued_at'] = (
        datetime.now(timezone.utc) - timedelta(days=31)
    ).isoformat()
    db_session.commit()
    with client.session_transaction() as session_data:
        session_data['_user_id'] = str(admin_user.id)
    token_response = client.get('/scoring/api/replay-token')
    form_data['replay_token'] = token_response.get_json()['replay_token']

    response = client.post('/scoring/api/replay', data=form_data)

    assert response.status_code == 409
    assert response.is_json
    assert response.get_json()['error']['code'] == 'manual_reconciliation_required'
    assert EventResult.query.filter_by(
        event_id=event.id, competitor_id=competitor.id
    ).count() == 0


def test_committed_retry_returns_receipt_before_foreign_heat_lock(
    client, db_session,
):
    admin_user = _make_user(db_session)
    other_user = _make_user(db_session, role=User.ROLE_JUDGE)
    tournament, event, heat, competitor, request_id, form_data = _seed_score(
        db_session, user_id=admin_user.id
    )
    accepted = save_heat_results_submission(
        tournament_id=tournament.id,
        heat=heat,
        event=event,
        form_data=form_data,
        judge_user_id=admin_user.id,
    )
    assert accepted['ok'] is True

    persisted_heat = db.session.get(type(heat), heat.id)
    persisted_heat.locked_by_user_id = other_user.id
    persisted_heat.locked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db_session.commit()
    with client.session_transaction() as session_data:
        session_data['_user_id'] = str(admin_user.id)

    response = client.post(
        f'/scoring/{tournament.id}/heat/{heat.id}/enter',
        data=form_data,
        headers={
            'Accept': 'application/json',
            'X-Requested-With': 'XMLHttpRequest',
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['ok'] is True
    assert payload['receipt_replayed'] is True
    assert payload['receipt']['request_id'] == request_id
    assert EventResult.query.filter_by(event_id=event.id).one().result_value == 9


def test_committed_retry_payload_drift_is_rejected_before_foreign_heat_lock(
    client, db_session,
):
    admin_user = _make_user(db_session)
    other_user = _make_user(db_session, role=User.ROLE_JUDGE)
    tournament, event, heat, competitor, _request_id, form_data = _seed_score(
        db_session, user_id=admin_user.id
    )
    accepted = save_heat_results_submission(
        tournament_id=tournament.id,
        heat=heat,
        event=event,
        form_data=form_data,
        judge_user_id=admin_user.id,
    )
    assert accepted['ok'] is True

    persisted_heat = db.session.get(type(heat), heat.id)
    persisted_heat.locked_by_user_id = other_user.id
    persisted_heat.locked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db_session.commit()
    changed = dict(form_data)
    changed[f'result_{competitor.id}'] = '17'
    with client.session_transaction() as session_data:
        session_data['_user_id'] = str(admin_user.id)

    response = client.post(
        f'/scoring/{tournament.id}/heat/{heat.id}/enter',
        data=changed,
        headers={
            'Accept': 'application/json',
            'X-Requested-With': 'XMLHttpRequest',
        },
    )

    assert response.status_code == 409
    assert response.get_json()['error']['code'] == 'request_id_payload_mismatch'
    assert EventResult.query.filter_by(event_id=event.id).one().result_value == 9


def test_heat_undo_revokes_accepted_receipt_before_delayed_retry(
    client, db_session,
):
    admin_user = _make_user(db_session)
    tournament, event, heat, competitor, request_id, form_data = _seed_score(
        db_session, user_id=admin_user.id
    )
    accepted = save_heat_results_submission(
        tournament_id=tournament.id,
        heat=heat,
        event=event,
        form_data=form_data,
        judge_user_id=admin_user.id,
    )
    assert accepted['ok'] is True
    db_session.commit()

    with client.session_transaction() as session_data:
        session_data['_user_id'] = str(admin_user.id)
        session_data[f'undo_heat_{heat.id}'] = accepted['undo_token']

    undo_response = client.post(
        f'/scoring/{tournament.id}/heat/{heat.id}/undo',
        headers={
            'Accept': 'application/json',
            'X-Requested-With': 'XMLHttpRequest',
        },
    )
    assert undo_response.status_code == 200
    assert EventResult.query.filter_by(
        event_id=event.id, competitor_id=competitor.id
    ).count() == 0

    delayed_retry = client.post(
        f'/scoring/{tournament.id}/heat/{heat.id}/enter',
        data=form_data,
        headers={
            'Accept': 'application/json',
            'X-Requested-With': 'XMLHttpRequest',
        },
    )

    assert delayed_retry.status_code == 409
    assert delayed_retry.get_json()['error']['code'] == 'request_id_revoked'
    assert EventResult.query.filter_by(
        event_id=event.id, competitor_id=competitor.id
    ).count() == 0
    receipt = db.session.get(ScoreSubmissionReceipt, request_id)
    assert receipt.accepted_outcome_json['receipt_revoked'] is True
    assert receipt.accepted_outcome_json['reason'] == 'heat_undo'
    revocation = AuditLog.query.filter_by(
        action='score_submission_receipts_revoked',
        entity_type='heat',
        entity_id=heat.id,
    ).one()
    assert request_id in revocation.details_json


def test_heat_undo_receipt_revocation_rolls_back_with_failed_undo(
    client, db_session, monkeypatch,
):
    from routes import scoring as scoring_routes

    admin_user = _make_user(db_session)
    tournament, event, heat, competitor, request_id, form_data = _seed_score(
        db_session, user_id=admin_user.id
    )
    accepted = save_heat_results_submission(
        tournament_id=tournament.id,
        heat=heat,
        event=event,
        form_data=form_data,
        judge_user_id=admin_user.id,
    )
    db_session.commit()
    real_revoke = scoring_routes.revoke_heat_submission_receipts_for_undo

    def revoke_then_fail(**kwargs):
        real_revoke(**kwargs)
        raise RuntimeError('forced failure after receipt revocation')

    monkeypatch.setattr(
        scoring_routes,
        'revoke_heat_submission_receipts_for_undo',
        revoke_then_fail,
    )
    with client.session_transaction() as session_data:
        session_data['_user_id'] = str(admin_user.id)
        session_data[f'undo_heat_{heat.id}'] = accepted['undo_token']

    response = client.post(
        f'/scoring/{tournament.id}/heat/{heat.id}/undo',
        headers={
            'Accept': 'application/json',
            'X-Requested-With': 'XMLHttpRequest',
        },
    )

    assert response.status_code == 500
    db.session.expire_all()
    assert EventResult.query.filter_by(
        event_id=event.id, competitor_id=competitor.id
    ).count() == 1
    assert db.session.get(type(heat), heat.id).status == 'completed'
    receipt = db.session.get(ScoreSubmissionReceipt, request_id)
    assert receipt.accepted_outcome_json.get('receipt_revoked') is not True
    assert AuditLog.query.filter_by(
        action='score_submission_receipts_revoked',
        entity_type='heat',
        entity_id=heat.id,
    ).count() == 0


def test_replay_requires_bound_issuer_identity(client, db_session):
    admin_user = _make_user(db_session)
    tournament, event, heat, competitor, _request_id, form_data = _seed_score(
        db_session, user_id=admin_user.id
    )
    form_data.pop('issuer_user_id')
    form_data.pop('issuer_role')
    db_session.commit()
    with client.session_transaction() as session_data:
        session_data['_user_id'] = str(admin_user.id)
    token_response = client.get('/scoring/api/replay-token')
    form_data['replay_token'] = token_response.get_json()['replay_token']

    response = client.post('/scoring/api/replay', data=form_data)

    assert response.status_code == 409
    assert response.get_json()['error']['code'] == 'issuer_identity_required'
    assert EventResult.query.filter_by(
        event_id=event.id, competitor_id=competitor.id
    ).count() == 0


def test_offline_queue_javascript_race_suite():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required for executable offline queue tests')

    result = subprocess.run(
        [node, str(ROOT / 'tests' / 'js' / 'offline_queue_shared.test.js')],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_service_worker_content_and_legacy_renewal_suite():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required for executable service worker tests')

    result = subprocess.run(
        [node, str(ROOT / 'tests' / 'js' / 'service_worker_offline.test.js')],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_prepared_package_manifest_and_page_bind_cached_content(
    client,
    db_session,
):
    user = _make_user(db_session)
    tournament, _event, heat, _competitor, _request_id, _form_data = _seed_score(
        db_session, user_id=user.id
    )
    db_session.commit()
    with client.session_transaction() as session_data:
        session_data['_user_id'] = str(user.id)

    ops_response = client.get(f'/scoring/{tournament.id}/offline-ops')

    assert ops_response.status_code == 200
    html = ops_response.get_data(as_text=True)
    context_match = re.search(
        r'window\.ProAmOfflineContext\s*=\s*(\{.*?\});', html
    )
    manifest_match = re.search(
        r'const OFFLINE_MANIFEST\s*=\s*(\{.*?\});', html
    )
    assert context_match is not None
    assert manifest_match is not None
    context = json.loads(context_match.group(1))
    manifest = json.loads(manifest_match.group(1))
    assert manifest['schema_version'] == 2
    assert manifest['assets']
    assert all(row['kind'] == 'asset' for row in manifest['assets'])
    assert all(len(row['content_sha256']) == 64 for row in manifest['assets'])
    assert all(row['kind'] == 'page' for row in manifest['pages'])
    assert 'url_digests' not in manifest

    page_response = client.get(
        f'/scoring/{tournament.id}/heat/{heat.id}/enter',
        query_string={
            'offline_prepare': '1',
            'prepared_schedule': context['schedule_fingerprint'],
        },
        headers={'X-Offline-Prepare': '1'},
    )

    assert page_response.status_code == 200
    assert page_response.headers['X-ProAm-Offline-Build'] == context[
        'application_build'
    ]
    assert page_response.headers['X-ProAm-Offline-Schedule'] == context[
        'schedule_fingerprint'
    ]
    assert page_response.headers['X-ProAm-Offline-Tournament'] == str(tournament.id)
    assert page_response.headers['X-ProAm-Offline-Issuer'] == str(user.id)
    assert page_response.headers['X-ProAm-Offline-Role'] == user.role
    assert page_response.headers['X-ProAm-Offline-Content-SHA256'] == hashlib.sha256(
        page_response.data
    ).hexdigest()


def test_offline_client_contract_is_single_queue_receipt_verified_and_prepared():
    sw = (ROOT / 'static' / 'sw.js').read_text(encoding='utf-8')
    shared = (ROOT / 'static' / 'js' / 'offline_queue_shared.js').read_text(
        encoding='utf-8'
    )
    entry = (ROOT / 'templates' / 'scoring' / 'enter_heat.html').read_text(
        encoding='utf-8'
    )
    ops = (ROOT / 'templates' / 'scoring' / 'offline_ops.html').read_text(
        encoding='utf-8'
    )

    assert 'handleScorePost' not in sw
    assert "objectStore('queue').add" not in sw
    assert 'prepare-offline-package' in sw
    assert 'clear-prepared-package' in sw
    assert 'legacy-queue-status' in sw
    assert "event.tag === 'score-sync'" not in sw
    assert 'legacy_unbound_issuer' in sw
    assert 'receipt.issuing_user_id' in sw
    assert 'renewLegacyReplayToken' in sw
    assert 'verifyFetchedRow' in sw
    assert 'cached_content_digests' in sw
    assert 'sha256(row.url)' not in sw
    assert sw.index('if (!prepared.replayable)') < sw.index('postLegacy(entry.url')
    assert "controllerMessage({type: 'manual-sync'})" not in (
        ROOT / 'static' / 'offline_queue.js'
    ).read_text(encoding='utf-8')
    assert 'verifyReceipt' in shared
    assert 'navigator.locks.request' in shared
    assert 'mutateQueue' in shared
    assert 'manual_reconciliation_required' in shared
    assert '30 * 24 * 60 * 60 * 1000' in shared
    assert 'request_id' in entry
    assert 'value="{{ score_request_id }}"' in entry
    assert 'issuer_user_id' in entry
    assert 'schedule_fingerprint' in entry
    assert 'prepareOfflineBtn' in ops
    assert 'legacyQueueCount' in ops
