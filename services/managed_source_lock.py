"""Hold the selected component's export fence through one host transaction."""

import json
import queue
import subprocess
import threading
from pathlib import Path

from sqlalchemy import event as sa_event


class SourceLockError(ValueError):
    pass


class HeldSourceExport:
    def __init__(self, executable, database, environment, principal, event_ids, *, failure_type=SourceLockError):
        self.closed = False
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
            if not packet.get('ok') or packet.get('schema_version') != 'strathmark.held-export.v1':
                raise SourceLockError('Source export fence refused: ' + str(packet.get('error', 'invalid response')))
            self.exports, self.states = packet['exports'], packet['states']
            if set(self.exports) != set(event_ids) or set(self.states) != set(event_ids):
                raise SourceLockError('Source export fence identity mismatch.')
            self.check_alive()
        except (OSError, ValueError, queue.Empty) as exc:
            self.close(False)
            raise SourceLockError('Source fence unavailable; no host acceptance was committed.') from exc

    def check_alive(self, *_args):
        if not self.closed and self.process.poll() is not None:
            raise self.failure_type('Source fence was lost; rollback and recover before accepting results.')

    def close(self, committed):
        if self.closed:
            return
        self.closed = True
        try:
            self.process.stdin.write('commit\n' if committed else 'abort\n')
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
