"""Linux local-process ownership, independent of Flask and database imports."""
from __future__ import annotations

try:
    import fcntl
except ImportError:  # ordinary Windows/SQLite application tests can still collect
    fcntl = None
import hashlib
import json
import os
import shutil
import signal
import stat
import tempfile
import time
import uuid
from pathlib import Path


class Refused(RuntimeError):
    """A requested operation cannot be proved to belong to this run."""


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def no_alias(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise Refused('An absolute path without traversal is required.')
    for part in (path, *path.parents):
        if part.is_symlink():
            raise Refused('Symlink paths are not owned output paths.')
        if part.exists() and part.is_file() and part.stat().st_nlink != 1:
            raise Refused('Hard-linked output is not permitted.')
    return path


def publish(path, value, *, replace=False):
    path = no_alias(path)
    if path.exists() and not replace:
        raise Refused('Refusing to overwrite retained evidence.')
    fd, name = tempfile.mkstemp(prefix='.publish-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb' if isinstance(value, bytes) else 'w') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(name, path)
        else:
            os.link(name, path)  # exclusive publication, never replaces another run
            os.unlink(name)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def process_identity(pid):
    try:
        base = Path('/proc') / str(int(pid))
        fields = (base / 'stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return None
        return {'pid': int(pid), 'start': fields[19],
                'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'exe': os.readlink(base / 'exe'),
                'command': (base / 'cmdline').read_bytes().decode().split('\0')[:-1],
                'uid': base.stat().st_uid}
    except (FileNotFoundError, ProcessLookupError):
        return None


def stop_process(identity, sig=signal.SIGTERM):
    current = process_identity(identity['pid'])
    if current is None:
        return
    if current != identity:
        raise Refused('Process identity changed; no signal sent.')
    # pidfd closes the PID-reuse race between validation and signalling.
    fd = os.pidfd_open(identity['pid'])
    try:
        if process_identity(identity['pid']) != identity:
            raise Refused('Process changed while opening pidfd.')
        signal.pidfd_send_signal(fd, sig)
    finally:
        os.close(fd)


def wait_until(predicate, timeout=20, description='condition'):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise TimeoutError(f'Watchdog expired waiting for {description}.')


class OwnedRun:
    def __init__(self, path, *, create=False):
        if fcntl is None:
            raise Refused('The process lab requires Linux flock and pidfd support.')
        self.path = no_alias(path)
        if create:
            if {'instance', 'uploads', '.ssh', '.aws', '.config'} & set(self.path.parts):
                raise Refused('Operational storage is not a lab output directory.')
            self.path.mkdir(mode=0o700)  # must be entirely new
            self.record = {'schema': 1, 'run_id': uuid.uuid4().hex,
                           'path': str(self.path), 'device': self.path.stat().st_dev,
                           'inode': self.path.stat().st_ino, 'uid': os.getuid(),
                           'processes': [], 'intents': [], 'cleanup': 'NOT_RUN',
                           'network_namespace': os.readlink('/proc/self/ns/net')}
            publish(self.path / 'owner.json', json.dumps(self.record, indent=2))
        else:
            owner = no_alias(self.path / 'owner.json')
            self.record = json.loads(owner.read_text())
        self.validate()
        self.lock = (self.path / 'run.lock').open('a+')
        try:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock.close()
            raise Refused('This exact run is still active.') from exc

    def validate(self):
        no_alias(self.path)
        info = self.path.stat()
        if (str(self.path) != self.record['path'] or info.st_dev != self.record['device']
                or info.st_ino != self.record['inode'] or info.st_uid != self.record['uid']
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise Refused('Run directory identity or permissions changed.')
        if len(self.record['run_id']) != 32:
            raise Refused('Invalid ownership record.')

    def save(self):
        self.validate()
        publish(self.path / 'owner.json', json.dumps(self.record, indent=2), replace=True)

    def intent(self, kind, command):
        item = {'kind': kind, 'command': command, 'pid': None}
        self.record['intents'].append(item)
        self.save()
        return item

    def remember(self, identity, kind):
        if identity is None:
            raise Refused('Allocated process exited before identity recording.')
        self.record['processes'].append({'kind': kind, 'identity': identity})
        self.save()

    def signal(self, identity, sig=signal.SIGTERM):
        self.validate()
        if identity not in [item['identity'] for item in self.record['processes']]:
            raise Refused('Process was not allocated by this run.')
        if process_identity(identity['pid']) is None:
            return
        try:
            environment = (Path('/proc') / str(identity['pid']) / 'environ').read_bytes().split(b'\0')
        except OSError as error:
            raise Refused('Process run identity cannot be revalidated.') from error
        if ('PROAM_LAB_RUN_ID=' + self.record['run_id']).encode() not in environment:
            raise Refused('Process lacks this run allocation identity.')
        stop_process(identity, sig)

    def stop_all(self):
        self.validate()
        self.reconcile_intents()
        # Children are recorded independently, so a dead Gunicorn master is safe.
        ordered = sorted(reversed(self.record['processes']), key=lambda entry: entry['kind'] != 'gunicorn')
        for entry in ordered:
            self.signal(entry['identity'])
        for entry in ordered:
            identity = entry['identity']
            try:
                wait_until(lambda: process_identity(identity['pid']) is None, 5, 'process exit')
            except TimeoutError:
                self.signal(identity, signal.SIGKILL)
                wait_until(lambda: process_identity(identity['pid']) is None, 5, 'forced exit')
        # Reconcile a worker spawned immediately before its master saw TERM.
        previous = len(self.record['processes'])
        self.reconcile_intents()
        for entry in self.record['processes'][previous:]:
            self.signal(entry['identity'], signal.SIGKILL)
            wait_until(lambda: process_identity(entry['identity']['pid']) is None, 5, 'last owned child exit')

    def reconcile_intents(self):
        """Recover the spawn/record window using exact argv AND private run identity."""
        known = {item['identity']['pid'] for item in self.record['processes']}
        marker = ('PROAM_LAB_RUN_ID=' + self.record['run_id']).encode()
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit() or int(proc.name) in known:
                continue
            try:
                identity = process_identity(int(proc.name))
                if identity is None or identity['uid'] != os.getuid():
                    continue
                environment = (proc / 'environ').read_bytes().split(b'\0')
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            matches = [item for item in self.record['intents']
                       if item['command'] == identity['command']]
            if marker in environment and matches:
                self.remember(identity, matches[0]['kind'])
            elif marker in environment:
                profiles = [str(Path(item['path']) / 'profile')
                            for item in self.record.get('scratch', [])]
                if any('--user-data-dir=' + profile in identity['command'] for profile in profiles):
                    self.remember(identity, 'browser')

    def scratch(self):
        # TMPDIR is deliberately private and can be long. Chromium's Unix
        # singleton socket needs this separately owned short Linux path.
        path = Path('/tmp') / ('pam-' + self.record['run_id'])
        no_alias(path)
        self.record.setdefault('scratch', []).append({'path': str(path), 'identity': None})
        self.save()
        path.mkdir(mode=0o700)
        publish(path / 'owner.json', json.dumps({'run_id': self.record['run_id']}))
        info = path.stat()
        self.record['scratch'][-1]['identity'] = [info.st_dev, info.st_ino, info.st_uid]
        self.save()
        return path

    def cleanup_scratch(self):
        for item in self.record.get('scratch', []):
            path = no_alias(item['path'])
            if not path.exists():
                continue
            info = path.stat()
            identity = [info.st_dev, info.st_ino, info.st_uid]
            marker = json.loads(no_alias(path / 'owner.json').read_text())
            if ((item['identity'] is not None and identity != item['identity'])
                    or marker != {'run_id': self.record['run_id']} or info.st_uid != os.getuid()):
                raise Refused('External scratch identity cannot be established.')
            # Browser-generated interior singleton links are unlinked, never
            # followed. rmtree uses the platform's fd-based symlink protection.
            if not shutil.rmtree.avoids_symlink_attacks:
                raise Refused('Safe descriptor-based scratch removal unavailable.')
            shutil.rmtree(path)

    def close(self):
        self.lock.close()


def child_environment(private, pg_bin, library_path=None):
    """No operational environment values cross the launcher boundary."""
    private = Path(private)
    environment = {
        'PATH': f'{pg_bin}:{Path(os.sys.executable).parent}:/usr/bin:/bin',
        'HOME': str(private), 'TMPDIR': str(private), 'LANG': 'C.UTF-8',
        'PYTHONDONTWRITEBYTECODE': '1', 'FLASK_SKIP_DOTENV': '1',
        'FLASK_ENV': 'development', 'WEB_CONCURRENCY': '1',
        'STRUCTURED_LOGGING': '0', 'JOB_HEARTBEAT_INTERVAL_SECONDS': '5',
        'JOB_LEASE_TIMEOUT_SECONDS': '30',
        'UPLOAD_FOLDER': str(private / 'uploads'),
        'LOCAL_BACKUP_DIR': str(private / 'backups'),
        'EVENT_ORDER_CONFIG_PATH': str(private / 'event-order.json'),
        'STRATHMARK_DB_PATH': str(private / 'strathmark.sqlite'),
        'STRATHMARK_TEST_DB': '1',
    }
    if library_path:
        environment['LD_LIBRARY_PATH'] = library_path
    return environment
