"""Missoula outbox process crashes against a persistent controlled HTTP peer.

The reviewed contract-example fixtures are reused from the existing adapter
tests. This peer does NOT independently qualify the real STRATHMARK service.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from recovery_drills.ownership import digest, process_identity, publish, stop_process, wait_until
from recovery_drills.scenarios import check, restore, same_state, stop_app


def seed_shadow(app, settings):
    from database import db
    from models import ShadowSettlementOutbox, User
    from services.shadow_settlement import capture_shadow_outcome_revisions
    from tests.test_shadow_settlement_workflow import _seed_issued_shadow_run
    from tests.test_strathmark_shadow_adapter import _numeric_outcome_response
    with app.app_context():
        actor = db.session.get(User, 1)
        tournament, event, rows, run, actor = _seed_issued_shadow_run(db.session, actor)
        tournament.name = '[SYNTHETIC RECOVERY] Shadow'
        for row, value in zip(rows, (25., 35.)):
            row.result_value = value
            row.status = 'completed'
        captured = capture_shadow_outcome_revisions(event, actor_id=actor.id)
        db.session.commit()
        outbox = db.session.get(ShadowSettlementOutbox, captured.outbox_id)
        payload = json.loads(outbox.payload_json)
        response = _numeric_outcome_response(run, actor, payload)
        assert not app.testing and app.config['WTF_CSRF_ENABLED']
        publish(Path(settings['private']) / 'shadow-fixture.json', json.dumps({
            'outbox_id': outbox.id, 'payload': payload, 'response': response,
            'actor': actor.shadow_actor_id, 'run_revision': run.run_revision}))


def deliver_shadow(app, settings):
    from recovery_drills.worker import barrier
    from services.shadow_settlement import deliver_shadow_settlement_outbox
    from services.strathmark_shadow import ShadowClientConfig, StrathmarkShadowClient
    client = StrathmarkShadowClient(ShadowClientConfig(
        base_url=settings['peer_url'], consumer_id='missoula:service:shadow',
        service_token=settings['peer_token'], attestation_key=settings['peer_key']))
    original = client.apply_outcome
    def apply(*args, **kwargs):
        barrier(settings, 'shadow-predelivery')
        return original(*args, **kwargs)
    client.apply_outcome = apply
    with app.app_context():
        result = deliver_shadow_settlement_outbox(client=client, limit=25, commit=True)
    publish(Path(settings['private']) / 'shadow-delivery.json', json.dumps(result.__dict__))


class Peer:
    def __init__(self, lab, fixture):
        self.lab, self.fixture = lab, fixture
        self.mode = 'lose'
        self.release = threading.Event()
        self.accepted = {}
        self.attempts = []
        self.lock = threading.Lock()
        peer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    from services.strathmark_shadow import (
                        _validate_contract_component,
                        canonical_shadow_request_digest,
                    )
                    _validate_contract_component(payload, 'NumericOutcomeRequest', 'controlled peer rejects schema')
                    assert self.path == '/v1/shadow/outcomes/apply'
                    assert self.headers['Authorization'] == 'Bearer ' + lab.settings['peer_token']
                    encoded, signed = self.headers['X-STRATHMARK-Actor-Attestation'].split('.')
                    def decode(value):
                        return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
                    expected = hmac.new(lab.settings['peer_key'].encode(), encoded.encode(), hashlib.sha256).digest()
                    assert hmac.compare_digest(decode(signed), expected)
                    claims = json.loads(decode(encoded))
                    assert claims['actor_id'] == fixture['actor']
                    assert claims['action'] == 'shadow.outcome.apply'
                    assert claims['subject_revision'] == fixture['run_revision']
                    assert claims['request_digest'] == canonical_shadow_request_digest(payload)
                    assert claims['issued_at'] <= time.time() <= claims['expires_at']
                    assert payload == fixture['payload']
                    key = payload['outcome_revision_id']
                    canonical = json.dumps(payload, sort_keys=True)
                    with peer.lock:
                        duplicate = key in peer.accepted
                        if duplicate:
                            assert peer.accepted[key] == canonical
                        else:
                            peer.accepted[key] = canonical
                        peer.attempts.append({'outcome_revision_id': key,
                                              'sha256': hashlib.sha256(canonical.encode()).hexdigest(),
                                              'duplicate': duplicate})
                        publish(lab.owner.path / 'controlled-peer.json', json.dumps({
                            'accepted_effects': len(peer.accepted), 'attempts': peer.attempts}), replace=True)
                    if peer.mode == 'lose':
                        peer.release.wait(timeout=35)
                        return  # the killed sender receives no acknowledgment
                    response = copy.deepcopy(fixture['response'])
                    response['outcome']['status'] = 'duplicate' if duplicate else 'recorded'
                    body = json.dumps(response).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception:
                    self.send_response(403)
                    self.end_headers()
        self.server = ThreadingHTTPServer(('127.0.0.1', lab.settings['peer_port']), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError('Controlled peer did not stop.')


def run_shadow(lab, result):
    import secrets

    from recovery_drills.lab import free_port
    lab.worker('shadow-seed')
    lab.expected_tournaments = 3
    fixture = json.loads((lab.private / 'shadow-fixture.json').read_text())
    port = free_port()
    lab.settings.update(peer_port=port, peer_url=f'http://127.0.0.1:{port}',
                         peer_token=secrets.token_hex(24), peer_key=secrets.token_hex(24))
    lab.settings['ports'].append(port)
    lab.write_settings()
    def outbox():
        return lab.query('SELECT delivery_status,attempt_count FROM shadow_settlement_outbox WHERE id=%s',
                         (fixture['outbox_id'],))[0]
    def official():
        return lab.query('SELECT id,result_value,final_position,points_awarded,payout_amount,handicap_factor,'
                         'predicted_time,mark_assigned_at,payout_settled FROM event_results ORDER BY id')
    protected = official()
    lab.worker('shadow-deliver')
    check(result, 'receiver unavailable preserves retryable intent', outbox()['delivery_status'], 'retryable-failed')
    wait_until(lambda: lab.query('SELECT id FROM shadow_settlement_outbox WHERE id=%s '
                                 'AND next_attempt_at <= timezone(\'utc\',now())',
                                 (fixture['outbox_id'],)), 8, 'existing delivery backoff')
    checkpoint = lab.snapshot()
    lab.worker('backup')
    backup = json.loads((lab.private / 'backup.json').read_text())
    lab.arm('shadow-predelivery')
    sender = lab.launch([sys.executable, '-m', 'recovery_drills.worker',
                         lab.private / 'settings.json', 'shadow-deliver'], 'shadow-delivery')
    barrier = lab.reached()
    check(result, 'predelivery crash targets owned sender', barrier['process']['pid'], sender.pid)
    lab.owner.signal(barrier['process'], signal.SIGKILL)
    sender.wait(timeout=8)
    for name in ('fault.json', 'reached.json', 'release'):
        (lab.private / name).unlink(missing_ok=True)
    same_state(result, 'sender crash before delivery preserves committed local checkpoint', checkpoint, lab.snapshot())
    peer = Peer(lab, fixture)
    try:
        sender = lab.launch([sys.executable, '-m', 'recovery_drills.worker',
                             lab.private / 'settings.json', 'shadow-deliver'], 'shadow-delivery')
        wait_until(lambda: len(peer.accepted) == 1, 12, 'controlled peer durable acceptance')
        check(result, 'peer acceptance is separate from uncommitted local acknowledgment',
              outbox()['delivery_status'], 'retryable-failed')
        (lab.private / 'shadow-delivery.json').unlink(missing_ok=True)
        lab.worker('shadow-deliver')
        competing = json.loads((lab.private / 'shadow-delivery.json').read_text())
        check(result, 'competing delivery skips locked row', competing['attempted'], 0)
        check(result, 'competing worker did not send another request', len(peer.attempts), 1)
        identity = next(item['identity'] for item in lab.owner.record['processes']
                        if item['identity']['pid'] == sender.pid)
        lab.owner.signal(identity, signal.SIGKILL)
        sender.wait(timeout=8)
        peer.release.set()
        peer.mode = 'ack'
        stop_app(lab)
        restore(lab, backup['dest'])
        lab.database = 'restored'
        same_state(result, 'client backup restores while receiver remembers later acceptance', checkpoint, lab.snapshot())
        lab.settings['database_url'] = lab.uri('restored')
        lab.write_settings()
        (lab.private / 'shadow-delivery.json').unlink()
        lab.worker('shadow-deliver')
        delivered = json.loads((lab.private / 'shadow-delivery.json').read_text())
        check(result, 'restart retries and validates exact peer duplicate', delivered['recorded'], 1)
        check(result, 'one accepted numeric effect after restart and local restore', len(peer.accepted), 1)
        check(result, 'repeated delivery preserves stable payload binding',
              len({item['sha256'] for item in peer.attempts}), 1)
        check(result, 'delivered intent is durable', outbox()['delivery_status'], 'recorded')
        check(result, 'shadow outage crash restore retry never changes official scoring', official(), protected)
        lab.start_app()
        result['fault'] = {'point': 'before send and after controlled peer acceptance',
                            'mechanism': 'SIGKILL owned delivery process', 'reached': True, 'observed': True}
        result['limitations'].append('Controlled receiver uses installed reviewed contract examples and validates '
                                      'schema, bearer, actor signature, request digest and immutable identity. '
                                      'Real remote STRATHMARK delivery NOT_QUALIFIED by this double; the installed '
                                      'producer contract verifier remains a separate gate.')
    finally:
        peer.close()
