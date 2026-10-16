"""Hold the selected component's export fence through one host transaction."""

import hashlib
import json
import queue
import sqlite3
import subprocess
import threading
from pathlib import Path

from sqlalchemy import event as sa_event


class SourceLockError(ValueError):
    pass


class HeldSourceExport:
    def __init__(self, executable, database, environment, principal, event_ids, *, failure_type=SourceLockError):
        self.closed = False
        self.source_connection = None
        self.failure_type = failure_type
        try:
            self.process = subprocess.Popen(
                [str(executable), '-I', str(Path(__file__).with_name('managed_export_worker.py')),
                 '--database', str(database)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, env=environment)
        except OSError as exc:
            raise SourceLockError('Source fence interpreter is unavailable.') from exc
        try:
            self.process.stdin.write(json.dumps({'principal': principal, 'event_ids': event_ids}) + '\n')
            self.process.stdin.flush()
            output = queue.Queue(maxsize=1)
            threading.Thread(target=lambda: output.put(self.process.stdout.readline(16 * 1024 * 1024 + 1)),
                             daemon=True).start()
            line = output.get(timeout=45)
            if len(line) > 16 * 1024 * 1024:
                raise SourceLockError('Source fence response exceeded capacity.')
            packet = json.loads(line)
            if not packet.get('ok') or packet.get('schema_version') != 'strathmark.held-export.v2':
                raise SourceLockError('Source export fence refused: ' + str(packet.get('error', 'invalid response')))
            self.exports, self.states = packet['exports'], packet['states']
            if set(self.exports) != set(event_ids) or set(self.states) != set(event_ids) or set(packet['state_digests']) != set(event_ids):
                raise SourceLockError('Source export fence identity mismatch.')
            self.process.stdin.write('handoff\n')
            self.process.stdin.flush()
            self.process.wait(timeout=5)
            # The host process owns this SQLite mutex through its DBAPI commit;
            # a helper crash cannot release it. Only compare verified bytes here:
            # export interpretation stays in the exact selected component.
            self.source_connection = sqlite3.connect(Path(database).resolve().as_uri() + '?mode=rw',
                                                     uri=True, timeout=45, isolation_level=None)
            self.source_connection.execute('BEGIN IMMEDIATE')
            for event_id in event_ids:
                row = self.source_connection.execute('SELECT state FROM events WHERE event_id=?', (event_id,)).fetchone()
                if row is None or hashlib.sha256(row[0].encode()).hexdigest() != packet['state_digests'][event_id]:
                    raise SourceLockError('Source changed during verified lock handoff; refresh and retry.')
            self.check_alive()
        except (OSError, ValueError, KeyError, TypeError, queue.Empty, sqlite3.Error, subprocess.TimeoutExpired) as exc:
            self.close(False)
            raise SourceLockError('Source fence unavailable; no host acceptance was committed.') from exc

    def check_alive(self, *_args):
        if self.closed:
            return
        try:
            if self.source_connection is None or not self.source_connection.in_transaction:
                raise sqlite3.OperationalError('source_fence_not_active')
            self.source_connection.execute('SELECT 1')
        except sqlite3.Error as exc:
            raise self.failure_type('Source fence was lost; rollback and recover before accepting results.') from exc

    def close(self, committed):
        if self.closed:
            return
        self.closed = True
        if self.source_connection is not None:
            try:
                self.source_connection.rollback()
            except sqlite3.Error:
                pass
            self.source_connection.close()
        try:
            self.process.stdin.write('abort\n')
            self.process.stdin.flush()
        except (OSError, ValueError):
            pass
        finally:
            try:
                self.process.stdin.close()
            except (OSError, ValueError):
                pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process.stdout.close()
        self.process.stderr.close()

    def bind_transaction(self, session):
        def finish(committed):
            self.close(committed)
            if session.info.get('managed_source_fence') is self:
                session.info.pop('managed_source_fence', None)

        sa_event.listen(session, 'before_commit', self.check_alive)
        sa_event.listen(session, 'after_commit',
                        lambda _session: finish(True) if not _session.in_nested_transaction() else None)
        sa_event.listen(session, 'after_rollback',
                        lambda _session: finish(False) if not _session.in_nested_transaction() else None)
        sa_event.listen(session, 'after_transaction_end',
                        lambda _session, transaction: finish(False) if transaction.parent is None else None)
