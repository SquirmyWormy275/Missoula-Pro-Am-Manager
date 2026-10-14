"""Chromium, real service worker, persistent canonical queue and HTTP replay."""
from __future__ import annotations

import concurrent.futures
import hashlib
import shutil
import time
from pathlib import Path

from recovery_drills.lab import ROOT, Client
from recovery_drills.ownership import process_identity, stop_process, wait_until
from recovery_drills.scenarios import check, submit, verify_values

SYNC = """async (remove) => {
  const queue = window.ProAmOfflineQueue;
  const entry = queue.read()[0];
  try {
    const outcome = await queue.syncEntry(entry, window.ProAmOfflineContext);
    if (remove) await queue.removeVerified(outcome.entry, outcome.data.receipt);
    return {ok:true, replay:outcome.data.receipt_replayed};
  } catch (error) { return {ok:false, code:error.code || error.name}; }
}"""


def run_browser(lab, result):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise FileNotFoundError('Playwright is required for the browser drill.') from error
    executable = shutil.which('chromium', path=lab.env['PATH'])
    if not executable:
        raise FileNotFoundError('A local Chromium executable is required; runtime downloads are prohibited.')
    scratch = lab.owner.scratch()
    browser_env = dict(lab.env, TMPDIR=str(scratch),
                       XDG_CONFIG_HOME=str(scratch / 'config'),
                       XDG_CACHE_HOME=str(scratch / 'cache'),
                       XDG_RUNTIME_DIR=str(scratch / 'runtime'),
                       PULSE_SERVER='unix:' + str(scratch / 'disabled-pulse'),
                       DBUS_SESSION_BUS_ADDRESS='unix:path=' + str(scratch / 'disabled-dbus'))
    (scratch / 'runtime').mkdir(mode=0o700)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(scratch / 'profile'), executable_path=executable,
            headless=True, env=browser_env, viewport={'width': 1280, 'height': 900},
            service_workers='allow', args=['--disable-background-networking',
                                          '--disable-component-update', '--no-first-run'])
        try:
            lab.owner.reconcile_intents()
            context.set_default_timeout(10000)
            page = context.new_page()
            def wait(expression, timeout=10):
                return wait_until(lambda: page.evaluate('() => (' + expression + ')'),
                                  timeout, 'browser observation')
            def login(user):
                page.goto(lab.url + '/auth/login')
                page.locator('[name="username"]').fill(user)
                page.locator('[name="password"]').fill('synthetic-lab-password')
                page.locator('button[type="submit"]').click()
                page.wait_for_url(lambda url: '/auth/login' not in url)
            login('judge')
            page.goto(lab.url + '/scoring/1/offline-ops')
            wait('navigator.serviceWorker.controller !== null')
            page.locator('#prepareOfflineBtn').click()
            wait("document.querySelector('#preparedStatus').textContent === 'Ready'", timeout=25)
            check(result, 'real service worker verifies a nonempty prepared package',
                  'package items verified' in page.locator('#preparedDetail').inner_text())
            result['browser'] = {'version': context.browser.version if context.browser else 'persistent Chromium',
                                  'service_worker_sha256': hashlib.sha256((ROOT / 'static/sw.js').read_bytes()).hexdigest()}
            ids, heats = lab.fixture['competitors'], lab.fixture['heats']

            def queue_offline(heat, competitors, values):
                page.goto(lab.url + f'/scoring/1/heat/{heat}/enter')
                wait('window.ProAmOfflineQueue && window.ProAmOfflineContext')
                context.set_offline(True)
                page.reload()
                check(result, 'prepared scoring page reloads under actual offline emulation',
                      page.locator('#savePrimaryBtn').is_visible() and page.evaluate('() => navigator.onLine') is False)
                for competitor, value in zip(competitors, values):
                    page.locator(f'[name="t1_run1_{competitor}"]').fill(str(value - .1))
                    page.locator(f'[name="t2_run1_{competitor}"]').fill(str(value + .1))
                page.locator('#savePrimaryBtn').click()
                wait('window.ProAmOfflineQueue.read().length === 1')
                request_id = page.evaluate('() => window.ProAmOfflineQueue.read()[0].request_id')
                page.reload()
                wait('window.ProAmOfflineQueue')
                check(result, 'canonical browser queue survives cold page reload',
                      page.evaluate('() => window.ProAmOfflineQueue.read()[0].request_id'), request_id)
                return request_id

            request_id = queue_offline(heats[0], ids[:2], (11.1, 12.1))
            check(result, 'offline queue is not yet server acceptance', lab.query(
                'SELECT request_id FROM score_submission_receipts WHERE request_id=%s', (request_id,)), [])
            # Offline Ops is the supported manual reconciliation surface. Move
            # away before reconnecting so the heat page's automatic sync is
            # not raced by a second explicitly submitted request.
            page.goto('about:blank')
            context.set_offline(False)
            page.goto(lab.url + '/scoring/1/offline-ops')
            wait('window.ProAmOfflineQueue && window.ProAmOfflineContext')
            lab.arm('score-postcommit', request_id)
            def lose_ack():
                reached = lab.reached()
                check(result, 'browser replay accepted before acknowledgment loss', bool(lab.query(
                    'SELECT request_id FROM score_submission_receipts WHERE request_id=%s', (request_id,))))
                # Chromium may transparently retry a closed connection. Keep
                # the owned origin down until the browser observes the loss,
                # then explicitly restart it; do not race an automatic respawn.
                master = next(item['identity'] for item in reversed(lab.owner.record['processes'])
                              if item['kind'] == 'gunicorn')
                lab.owner.signal(master, __import__('signal').SIGKILL)
                lab.owner.signal(reached['process'], __import__('signal').SIGKILL)
                wait_until(lambda: process_identity(master['pid']) is None
                           and process_identity(reached['process']['pid']) is None,
                           5, 'owned origin termination')
                for name in ('fault.json', 'reached.json', 'release'):
                    (lab.private / name).unlink(missing_ok=True)
                return reached
            started = time.monotonic()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                fault = pool.submit(lose_ack)
                ambiguous = page.evaluate(SYNC, False)
                fault.result(timeout=25)
            check(result, 'lost browser acknowledgment cannot verify synchronization', ambiguous['ok'], False)
            check(result, 'ambiguous browser entry remains inspectable',
                  page.evaluate('() => window.ProAmOfflineQueue.read().length'), 1)
            lab.start_app()
            replay = page.evaluate(SYNC, False)
            check(result, 'browser exact retry recovers durable receipt', replay, {'ok': True, 'replay': True})
            # Invalid/expired transport authority is renewed by the original user.
            page.evaluate("""async () => {
                const entry = window.ProAmOfflineQueue.read()[0];
                entry.payload.csrf_token='expired-synthetic-csrf';
                entry.payload.replay_token='0.invalid';
                await window.ProAmOfflineQueue.upsert(entry);
            }""")
            renewed = page.evaluate(SYNC, True)
            check(result, 'expired CSRF and replay authority renew without a duplicate score', renewed,
                  {'ok': True, 'replay': True})
            check(result, 'queue clears only after verified receipt',
                  page.evaluate('() => window.ProAmOfflineQueue.read().length'), 0)
            verify_values(lab, result, [11.1, 12.1, None, None])
            # A second offline entry conflicts with a newer legitimate server save.
            queued = queue_offline(heats[1], ids[2:], (13.1, 14.1))
            client = Client(lab)
            client.login()
            submit(lab, result, client, heats[1], ids[2:], (20.1, 21.1))
            page.goto('about:blank')
            context.set_offline(False)
            page.goto(lab.url + '/scoring/1/offline-ops')
            wait('window.ProAmOfflineQueue && window.ProAmOfflineContext')
            conflict = page.evaluate(SYNC, True)
            check(result, 'newer server score rejects stale browser payload', conflict.get('code'), 'scoring_state_changed')
            check(result, 'conflicted entry remains in persistent browser queue',
                  page.evaluate('() => window.ProAmOfflineQueue.read()[0].request_id'), queued)
            context.clear_cookies()
            expired = page.evaluate(SYNC, True)
            check(result, 'expired authenticated session cannot drain queued scores', expired.get('code'), 'session_required')
            login('otherjudge')
            page.goto(lab.url + f'/scoring/1/heat/{heats[1]}/enter')
            wrong = page.evaluate(SYNC, True)
            check(result, 'wrong logged-in browser user cannot replay', wrong['ok'], False)
            check(result, 'wrong-user failure retains queue', page.evaluate('() => window.ProAmOfflineQueue.read().length'), 1)
            stale = page.evaluate("""async () => {
                try {
                    const entry=window.ProAmOfflineQueue.read()[0];
                    await window.ProAmOfflineQueue.syncEntry(entry, {...window.ProAmOfflineContext,
                        issuer_user_id:entry.issuer_user_id, schedule_fingerprint:'stale-synthetic-package'});
                    return 'accepted';
                } catch(error) { return error.code; }
            }""")
            check(result, 'stale schedule package cannot be automatically replayed', stale, 'stale_manifest')
            verify_values(lab, result, [11.1, 12.1, 20.1, 21.1])
            check(result, 'only two legitimate score requests were accepted',
                  lab.query('SELECT count(*) AS count FROM score_submission_receipts')[0]['count'], 2)
            page.screenshot(path=str(lab.owner.path / 'synthetic-queue-reconciliation.png'))
            result['fault'] = {'point': 'browser replay postcommit', 'mechanism': 'SIGKILL owned origin master and worker',
                                'reached': True, 'observed': True}
            result['timings_seconds']['ambiguous_replay_to_verified_reconciliation'] = time.monotonic() - started
            result['limitations'].append('Chromium emulation and page reload with the real service worker. '
                                          'No physical phone/radio, iOS background, browser-process restart '
                                          'or cross-browser qualification. Conflicted queue is inspected, then '
                                          'its synthetic profile is removed during cleanup.')
        finally:
            context.close()
