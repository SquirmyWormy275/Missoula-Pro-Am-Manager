"""
Infrastructure / utility service tests -- report_cache, audit, background_jobs,
cache_invalidation.

Covers the smaller service modules that had no dedicated test coverage.

Run:
    pytest tests/test_infrastructure.py -v

Requirements:
    pytest (pip install pytest)
    All app dependencies installed.
"""
import gc
import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from database import db
from database import db as _db

# ---------------------------------------------------------------------------
# Fixtures (Flask app + DB -- same pattern as test_woodboss.py)
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def app():
    """Create a test Flask app with temp-file SQLite built via flask db upgrade."""
    import os

    from tests.db_test_utils import create_test_app
    _app, db_path = create_test_app()

    with _app.app_context():
        yield _app
        _db.session.remove()
    try:
        os.unlink(db_path)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def db_session(app):
    """Wrap each test in a transaction and roll back afterward."""
    with app.app_context():
        _db.session.begin_nested()
        yield _db.session
        _db.session.rollback()


# ===================================================================
# ReportCache tests  (services/report_cache.py)
# ===================================================================

@pytest.fixture
def l1_cache_enabled(app, monkeypatch):
    """Exercise cache mechanics independently of the active test database."""
    monkeypatch.setitem(
        app.config,
        'SQLALCHEMY_DATABASE_URI',
        'sqlite:///:memory:',
    )


@pytest.mark.usefixtures('l1_cache_enabled')
class TestReportCache:
    """Tests for the in-memory L1 TTL cache (disk layer bypassed)."""

    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        """Clear the module-level cache dict before each test."""
        from services import report_cache
        report_cache.reset_for_testing()
        yield
        report_cache.reset_for_testing()

    # -- Disable disk layer for deterministic unit tests --
    @pytest.fixture(autouse=True)
    def _no_disk(self):
        """Stub the shelf functions so tests only exercise L1."""
        with patch('services.report_cache._shelf_get', return_value=None), \
             patch('services.report_cache._shelf_set'), \
             patch('services.report_cache._shelf_delete'), \
             patch('services.report_cache._shelf_delete_prefix'):
            yield

    def test_get_missing_key_returns_none(self):
        from services.report_cache import get
        assert get('nonexistent:key') is None

    def test_set_then_get_round_trip(self):
        from services.report_cache import get, set
        payload = {'scores': [1, 2, 3]}
        set('test:round_trip', payload, ttl_seconds=60)
        assert get('test:round_trip') == payload

    def test_get_or_compute_coalesces_concurrent_cache_miss(self):
        from services import report_cache

        reader_count = 8
        ready = threading.Barrier(reader_count)
        builder_started = threading.Event()
        release_builder = threading.Event()
        calls = 0
        calls_lock = threading.Lock()
        results = []
        worker_errors = []

        def builder():
            nonlocal calls
            with calls_lock:
                calls += 1
            builder_started.set()
            assert release_builder.wait(timeout=10)
            return {'standings': [1, 2, 3]}

        def reader():
            try:
                ready.wait(timeout=10)
                results.append(
                    report_cache.get_or_compute('public:1', 60, builder)
                )
            except Exception as exc:
                worker_errors.append(exc)

        readers = [threading.Thread(target=reader) for _ in range(reader_count)]
        for reader_thread in readers:
            reader_thread.start()
        assert builder_started.wait(timeout=10)
        release_builder.set()
        for reader_thread in readers:
            reader_thread.join(timeout=10)

        assert all(not reader_thread.is_alive() for reader_thread in readers)
        assert worker_errors == []
        assert calls == 1
        assert results == [{'standings': [1, 2, 3]}] * reader_count

    def test_get_or_compute_retires_idle_fill_lock(self):
        from services import report_cache

        assert report_cache.get_or_compute(
            'public:retired',
            60,
            lambda: {'ready': True},
        ) == {'ready': True}
        gc.collect()

        with report_cache._lock:
            assert 'public:retired' not in report_cache._fill_locks

    def test_get_or_compute_releases_key_after_builder_error(self):
        from services import report_cache

        def broken_builder():
            raise RuntimeError('synthetic builder failure')

        with pytest.raises(RuntimeError, match='synthetic builder failure'):
            report_cache.get_or_compute('public:error', 60, broken_builder)

        assert report_cache.get_or_compute(
            'public:error',
            60,
            lambda: {'recovered': True},
        ) == {'recovered': True}

    def test_ttl_expiry(self):
        """Value should expire after TTL elapses."""
        from services import report_cache

        # Minimum TTL enforced by set() is 1 second.  We mock time to
        # simulate expiry without actually sleeping.
        base_time = time.time()
        with patch('services.report_cache.time') as mock_time:
            mock_time.time.return_value = base_time
            report_cache.set('test:ttl', 'hello', ttl_seconds=5)

            # Still within TTL
            mock_time.time.return_value = base_time + 3
            assert report_cache.get('test:ttl') == 'hello'

            # Expired
            mock_time.time.return_value = base_time + 6
            assert report_cache.get('test:ttl') is None

    def test_invalidate_prefix_removes_matching_keys(self):
        from services.report_cache import get, invalidate_prefix, set
        set('reports:1:standings', 'data1', ttl_seconds=60)
        set('reports:1:payouts', 'data2', ttl_seconds=60)
        set('reports:2:standings', 'data3', ttl_seconds=60)

        invalidate_prefix('reports:1:')

        assert get('reports:1:standings') is None
        assert get('reports:1:payouts') is None
        # Other prefix untouched
        assert get('reports:2:standings') == 'data3'

    def test_stale_reader_cannot_repopulate_after_invalidation(self):
        from services import report_cache

        cache_key = 'reports:1:standings'
        reader_missed = threading.Event()
        invalidation_finished = threading.Event()

        def stale_reader():
            assert report_cache.get(cache_key) is None
            reader_missed.set()
            assert invalidation_finished.wait(timeout=2)
            report_cache.set(cache_key, 'stale', ttl_seconds=60)

        reader = threading.Thread(target=stale_reader)
        reader.start()
        assert reader_missed.wait(timeout=2)

        report_cache.invalidate_prefix('reports:1:')
        invalidation_finished.set()
        reader.join(timeout=2)

        assert not reader.is_alive()
        assert report_cache.get(cache_key) is None

    def test_unrelated_invalidation_does_not_block_cache_fill(self):
        from services import report_cache

        cache_key = 'reports:1:standings'
        assert report_cache.get(cache_key) is None

        report_cache.invalidate_prefix('reports:2:')
        report_cache.set(cache_key, 'fresh', ttl_seconds=60)

        assert report_cache.get(cache_key) == 'fresh'

    def test_clear_via_invalidate_prefix_empty_string(self):
        """invalidate_prefix('') should match all keys."""
        from services.report_cache import get, invalidate_prefix, set
        set('a', 1, ttl_seconds=60)
        set('b', 2, ttl_seconds=60)
        invalidate_prefix('')
        assert get('a') is None
        assert get('b') is None

    def test_test_app_isolation_clears_memory_and_disables_disk(self):
        from services import report_cache
        from tests.db_test_utils import _isolate_report_cache

        report_cache.set('api:standings-poll:1', {'teams': ['stale']}, 60)
        _isolate_report_cache()

        assert report_cache.get('api:standings-poll:1') is None
        assert report_cache._shelf_resolved is True
        assert report_cache._shelf_path is None

    def test_disk_cache_is_disabled_across_process_boots(self, tmp_path, monkeypatch):
        from services import report_cache

        monkeypatch.setattr(report_cache, '_shelf_resolved', False)
        monkeypatch.setattr(
            report_cache,
            '_shelf_path',
            str(tmp_path / 'shared-cache'),
        )

        assert report_cache._get_shelf_path() is None
        assert report_cache._shelf_path is None

    def test_postgres_bypasses_process_local_cache(self, app, monkeypatch):
        from services import report_cache

        monkeypatch.setitem(
            app.config,
            'SQLALCHEMY_DATABASE_URI',
            'postgresql://synthetic/not-connected',
        )
        with app.app_context():
            report_cache.set('reports:9:standings', 'stale', ttl_seconds=60)
            assert report_cache.get('reports:9:standings') is None

    def test_multiple_keys_independent(self):
        from services.report_cache import get, set
        set('key:alpha', 'AAA', ttl_seconds=60)
        set('key:beta', 'BBB', ttl_seconds=60)

        assert get('key:alpha') == 'AAA'
        assert get('key:beta') == 'BBB'

        # Overwrite one -- other unaffected
        set('key:alpha', 'CCC', ttl_seconds=60)
        assert get('key:alpha') == 'CCC'
        assert get('key:beta') == 'BBB'

    def test_set_enforces_minimum_ttl_of_one_second(self):
        """TTL values < 1 are clamped to 1."""
        from services import report_cache

        base_time = time.time()
        with patch('services.report_cache.time') as mock_time:
            mock_time.time.return_value = base_time
            # Pass ttl_seconds=0 -- should be clamped to 1
            report_cache.set('test:min_ttl', 'val', ttl_seconds=0)

            # At 0.5s it should still be alive (ttl = 1)
            mock_time.time.return_value = base_time + 0.5
            assert report_cache.get('test:min_ttl') == 'val'


# ===================================================================
# Audit tests  (services/audit.py)
# ===================================================================

class TestAudit:
    """Tests for log_action() — requires Flask app context + DB."""

    def test_log_action_creates_audit_record(self, app, db_session):
        from models.audit_log import AuditLog
        from services.audit import log_action

        with app.test_request_context('/test', method='POST'):
            log_action('test_action', 'Tournament', entity_id=42,
                       details={'key': 'value'})
            db_session.flush()

        record = db_session.query(AuditLog).filter_by(action='test_action').first()
        assert record is not None
        assert record.entity_type == 'Tournament'
        assert record.entity_id == 42
        assert json.loads(record.details_json) == {'key': 'value'}

    def test_log_action_minimal_params(self, app, db_session):
        from models.audit_log import AuditLog
        from services.audit import log_action

        with app.test_request_context('/test'):
            log_action('minimal_action', 'Event')
            db_session.flush()

        record = db_session.query(AuditLog).filter_by(action='minimal_action').first()
        assert record is not None
        assert record.entity_id is None
        assert json.loads(record.details_json) == {}

    def test_log_action_with_all_optional_params(self, app, db_session):
        from models.audit_log import AuditLog
        from services.audit import log_action

        details = {'old_value': 10, 'new_value': 20}
        with app.test_request_context('/test', headers={'X-Forwarded-For': '10.0.0.1'}):
            log_action('update_score', 'EventResult', entity_id=99, details=details)
            db_session.flush()

        record = db_session.query(AuditLog).filter_by(action='update_score').first()
        assert record is not None
        assert record.entity_type == 'EventResult'
        assert record.entity_id == 99
        assert record.ip_address == '10.0.0.1'
        parsed = json.loads(record.details_json)
        assert parsed == details

    def test_log_action_does_not_commit(self, app):
        """log_action() adds to session but does not commit — caller controls transaction."""
        from models.audit_log import AuditLog
        from services import audit

        with app.test_request_context('/test'):
            before_count = AuditLog.query.count()
            audit.log_action('test_no_commit', 'Tournament', entity_id=99)
            _db.session.flush()
            after_count = AuditLog.query.count()
            assert after_count == before_count + 1

    def test_log_action_outside_request_context(self, app):
        """log_action() should work even without a Flask request context
        (ip_address and user_agent will be None)."""
        from models.audit_log import AuditLog
        from services.audit import log_action

        with app.app_context():
            # No test_request_context -- request will not be available
            log_action('background_task', 'Heat', entity_id=7)
            _db.session.flush()

            record = _db.session.query(AuditLog).filter_by(action='background_task').first()
            assert record is not None
            assert record.ip_address is None
            assert record.user_agent is None


# ===================================================================
# BackgroundJobs tests  (services/background_jobs.py)
# ===================================================================

class TestBackgroundJobs:
    """Tests for the in-process ThreadPoolExecutor job manager."""

    @pytest.fixture(autouse=True)
    def _reset_jobs(self):
        """Clear the module-level jobs dict before each test."""
        from services import background_jobs
        with background_jobs._lock:
            background_jobs._jobs.clear()
        app = getattr(background_jobs, '_app', None)
        if app is not None:
            from models.background_job import BackgroundJob
            with app.app_context():
                BackgroundJob.query.delete()
                background_jobs.db.session.commit()

    def test_submit_returns_job_id_string(self):
        from services.background_jobs import submit
        job_id = submit('test-label', lambda: 42)
        assert isinstance(job_id, str)
        assert len(job_id) == 32  # uuid4 hex

    def test_get_returns_dict_with_status(self):
        from services.background_jobs import get, submit
        job_id = submit('simple-job', lambda: 'ok')
        info = get(job_id)
        assert isinstance(info, dict)
        assert 'status' in info
        assert info['label'] == 'simple-job'

    def test_get_unknown_job_returns_none(self):
        from services.background_jobs import get
        assert get('nonexistent_id') is None

    def test_submitted_job_eventually_completes(self):
        from services.background_jobs import get, submit
        event = threading.Event()
        job_id = submit('completing', lambda: (event.set(), 'done')[1])

        # Wait up to 2 seconds for the background thread
        event.wait(timeout=2.0)
        # Give the done callback a moment to run
        time.sleep(0.1)

        info = get(job_id)
        assert info['status'] == 'completed'
        assert info['result'] == 'done'
        assert info['error'] is None
        assert info['finished_at'] is not None

    def test_failed_job_returns_error_status(self):
        from services.background_jobs import get, submit
        event = threading.Event()

        def failing_fn():
            try:
                raise ValueError('intentional test failure')
            finally:
                event.set()

        job_id = submit('failing', failing_fn)
        event.wait(timeout=2.0)
        time.sleep(0.1)

        info = get(job_id)
        assert info['status'] == 'failed'
        assert 'intentional test failure' in info['error']
        assert info['result'] is None

    def test_ok_false_result_is_a_failed_job(self):
        from services.background_jobs import get, submit

        job_id = submit(
            'truthful-failure',
            lambda: {'ok': False, 'error': 'backup validation failed'},
            metadata={'tournament_id': 1, 'kind': 'backup'},
        )
        deadline = time.time() + 2.0
        info = get(job_id)
        while info['status'] not in {'completed', 'failed'} and time.time() < deadline:
            time.sleep(0.02)
            info = get(job_id)

        assert info['status'] == 'failed'
        assert info['error'] == 'backup validation failed'
        assert info['result']['ok'] is False

    def test_multiple_jobs_tracked_independently(self):
        from services.background_jobs import get, submit
        barrier = threading.Barrier(2, timeout=2.0)

        def job_a():
            barrier.wait()
            return 'A'

        def job_b():
            barrier.wait()
            return 'B'

        id_a = submit('job-a', job_a)
        id_b = submit('job-b', job_b)

        # Wait for both to finish
        time.sleep(1.0)

        info_a = get(id_a)
        info_b = get(id_b)
        assert info_a['label'] == 'job-a'
        assert info_b['label'] == 'job-b'

    def test_executor_backlog_remains_queued_until_worker_starts(self, app):
        from models.background_job import BackgroundJob
        from services import background_jobs

        release_first = threading.Event()
        first_started = threading.Event()
        second_started = threading.Event()
        background_jobs.configure(1, app)

        def blocking_job():
            first_started.set()
            release_first.wait(timeout=2.0)
            return 'first'

        try:
            background_jobs.submit('worker-blocker', blocking_job)
            assert first_started.wait(timeout=2.0)
            queued_id = background_jobs.submit(
                'executor-backlog',
                lambda: (second_started.set(), 'second')[1],
            )

            with app.app_context():
                row = _db.session.get(BackgroundJob, queued_id)
                assert row.status == 'queued'
                assert row.started_at is None

            release_first.set()
            assert second_started.wait(timeout=2.0)
        finally:
            release_first.set()
            background_jobs.configure(2, app)

    def test_terminal_persistence_retries_transient_database_failure(
        self, app, monkeypatch,
    ):
        from sqlalchemy.exc import SQLAlchemyError

        from models.background_job import BackgroundJob
        from services import background_jobs

        actual_persist = background_jobs._persist_job
        terminal_attempts = []

        def transient_terminal_failure(job_id, **updates):
            if updates.get('status') in {'completed', 'failed'}:
                terminal_attempts.append(updates['status'])
                if len(terminal_attempts) == 1:
                    raise SQLAlchemyError('transient terminal write failure')
            return actual_persist(job_id, **updates)

        monkeypatch.setattr(
            background_jobs,
            '_persist_job',
            transient_terminal_failure,
        )
        job_id = background_jobs.submit('retry-terminal-write', lambda: 'done')

        deadline = time.time() + 2.0
        persisted_status = None
        while time.time() < deadline:
            with app.app_context():
                _db.session.expire_all()
                persisted_status = _db.session.get(BackgroundJob, job_id).status
            if persisted_status == 'completed':
                break
            time.sleep(0.02)

        assert terminal_attempts == ['completed', 'completed']
        assert persisted_status == 'completed'

    def test_configure_changes_max_workers(self):
        from services import background_jobs
        old_executor = background_jobs._executor
        background_jobs.configure(4)
        assert background_jobs._executor is not old_executor
        assert background_jobs._executor._max_workers == 4
        # Restore default
        background_jobs.configure(2)

    def test_list_recent_returns_newest_first(self):
        from services.background_jobs import list_recent, submit

        first_id = submit('first-job', lambda: 'first', metadata={'tournament_id': 1})
        second_id = submit('second-job', lambda: 'second', metadata={'tournament_id': 1})
        time.sleep(0.1)

        rows = list_recent(limit=2)
        assert len(rows) == 2
        assert rows[0]['id'] == second_id
        assert rows[1]['id'] == first_id
        assert rows[0]['metadata']['tournament_id'] == 1

    def test_list_recent_scopes_tournament_before_limit(self):
        from services.background_jobs import list_recent, submit

        wanted_id = submit(
            'wanted-job', lambda: 'wanted', metadata={'tournament_id': 101},
        )
        for index in range(4):
            submit(
                f'other-job-{index}',
                lambda value=index: value,
                metadata={'tournament_id': 202},
            )
        time.sleep(0.1)

        rows = list_recent(limit=1, tournament_id=101)

        assert [row['id'] for row in rows] == [wanted_id]

    def test_reconcile_marks_only_prior_boot_jobs_interrupted(self, app):
        from datetime import timedelta

        from models.background_job import BackgroundJob
        from services import background_jobs
        from services.time_utils import utc_now_naive

        now = utc_now_naive()
        with app.app_context():
            prior = BackgroundJob(
                id='prior-boot-job',
                label='prior',
                status='running',
                owner_boot_id='old-boot',
                owner_heartbeat_at=now - timedelta(seconds=31),
            )
            overlapping = BackgroundJob(
                id='overlapping-boot-job',
                label='overlapping',
                status='running',
                owner_boot_id='still-alive-boot',
                owner_heartbeat_at=now - timedelta(seconds=2),
            )
            current = BackgroundJob(
                id='current-boot-job',
                label='current',
                status='running',
                owner_boot_id=background_jobs.boot_id(),
                owner_heartbeat_at=now - timedelta(seconds=31),
            )
            _db.session.add_all([prior, overlapping, current])
            _db.session.commit()
            prior_id = prior.id
            overlapping_id = overlapping.id
            current_id = current.id

        changed = background_jobs.reconcile_interrupted_jobs(
            app,
            now=now,
            lease_timeout_seconds=30,
        )

        with app.app_context():
            assert changed == 1
            assert _db.session.get(BackgroundJob, prior_id).status == 'interrupted'
            assert _db.session.get(BackgroundJob, prior_id).finished_at is not None
            assert _db.session.get(BackgroundJob, overlapping_id).status == 'running'
            assert _db.session.get(BackgroundJob, current_id).status == 'running'

    def test_interrupted_job_rejects_late_owner_callback(self, app):
        from models.background_job import BackgroundJob
        from services import background_jobs

        with app.app_context():
            row = BackgroundJob(
                id='late-callback-job',
                label='late callback',
                status='interrupted',
                owner_boot_id=background_jobs.boot_id(),
            )
            _db.session.add(row)
            _db.session.commit()
            row_id = row.id

        persisted = background_jobs._persist_job(
            row_id,
            status='completed',
            result={'ok': True},
            require_active_owner=True,
        )

        with app.app_context():
            assert persisted is False
            assert _db.session.get(BackgroundJob, row_id).status == 'interrupted'

    @pytest.mark.parametrize(
        'table_exists, columns',
        [
            (False, set()),
            (True, {'id', 'status'}),
        ],
    )
    def test_reconcile_defers_cleanly_before_boot_owner_migration(
        self, app, monkeypatch, table_exists, columns,
    ):
        from services import background_jobs

        class PreMigrationInspector:
            def has_table(self, _table_name):
                return table_exists

            def get_columns(self, _table_name):
                return [{'name': name} for name in columns]

        monkeypatch.setattr(
            background_jobs,
            'inspect',
            lambda _engine: PreMigrationInspector(),
        )

        assert background_jobs.reconcile_interrupted_jobs(app) == 0

    def test_testing_app_never_starts_production_heartbeat(self, app, monkeypatch):
        from services import background_jobs

        monkeypatch.setitem(app.config, 'TESTING', True)
        monkeypatch.setitem(
            app.config,
            'SQLALCHEMY_DATABASE_URI',
            'postgresql://test-only/example',
        )
        monkeypatch.setattr(
            background_jobs,
            '_heartbeat_once',
            lambda _app: pytest.fail('testing app started the production heartbeat'),
        )

        background_jobs.start_heartbeat(app)

    def test_postgres_unit_mode_never_starts_production_heartbeat(
        self, app, monkeypatch,
    ):
        from services import background_jobs

        monkeypatch.setitem(app.config, 'TESTING', False)
        monkeypatch.setitem(
            app.config,
            'SQLALCHEMY_DATABASE_URI',
            'postgresql://test-only/example',
        )
        monkeypatch.delenv('FLASK_ENV', raising=False)
        monkeypatch.delenv('TESTING', raising=False)
        monkeypatch.setenv('PROAM_UNIT_PG', '1')
        monkeypatch.setattr(
            background_jobs,
            '_heartbeat_once',
            lambda _app: pytest.fail('PostgreSQL unit mode started a heartbeat'),
        )

        background_jobs.start_heartbeat(app)

    def test_jobs_are_persisted_to_database(self, app):
        from models.background_job import BackgroundJob
        from services.background_jobs import submit

        job_id = submit('persisted-job', lambda: 'stored', metadata={'tournament_id': 7})
        time.sleep(0.1)

        with app.app_context():
            row = _db.session.get(BackgroundJob, job_id)
            assert row is not None
            assert row.label == 'persisted-job'
            assert row.tournament_id == 7


# ===================================================================
# CacheInvalidation tests  (services/cache_invalidation.py)
# ===================================================================

@pytest.mark.usefixtures('l1_cache_enabled')
class TestCacheInvalidation:
    """Tests for invalidate_tournament_caches()."""

    def test_runs_without_error(self):
        from services.cache_invalidation import invalidate_tournament_caches
        # Should not raise for any tournament id
        invalidate_tournament_caches(1)
        invalidate_tournament_caches(999)

    def test_clears_tournament_specific_cache_keys(self):
        """Verify that cache keys for the given tournament are removed."""
        from services import report_cache
        from services.cache_invalidation import invalidate_tournament_caches

        # Bypass disk layer
        with patch('services.report_cache._shelf_get', return_value=None), \
             patch('services.report_cache._shelf_set'), \
             patch('services.report_cache._shelf_delete'), \
             patch('services.report_cache._shelf_delete_prefix'):

            # Seed keys for tournament 5
            report_cache.set('reports:5:standings', 'data', ttl_seconds=60)
            report_cache.set('portal:college:5:overview', 'data', ttl_seconds=60)
            report_cache.set('portal:pro:5:overview', 'data', ttl_seconds=60)
            report_cache.set('api:standings-poll:5:latest', 'data', ttl_seconds=60)

            # Seed a key for tournament 6 -- should survive
            report_cache.set('reports:6:standings', 'other', ttl_seconds=60)

            invalidate_tournament_caches(5)

            assert report_cache.get('reports:5:standings') is None
            assert report_cache.get('portal:college:5:overview') is None
            assert report_cache.get('portal:pro:5:overview') is None
            assert report_cache.get('api:standings-poll:5:latest') is None
            # Other tournament untouched
            assert report_cache.get('reports:6:standings') == 'other'

    def test_tournament_prefixes_do_not_match_larger_ids(self):
        from services import report_cache
        from services.cache_invalidation import invalidate_tournament_caches

        keys = (
            'reports:{tid}:standings',
            'portal:college:{tid}',
            'portal:pro:{tid}',
            'api:standings-poll:{tid}',
        )
        with patch('services.report_cache._shelf_get', return_value=None), \
             patch('services.report_cache._shelf_set'), \
             patch('services.report_cache._shelf_delete_prefix'):
            for tournament_id in (1, 10, 11):
                for key in keys:
                    report_cache.set(
                        key.format(tid=tournament_id),
                        f'tournament-{tournament_id}',
                        ttl_seconds=60,
                    )

            invalidate_tournament_caches(1)

            for key in keys:
                assert report_cache.get(key.format(tid=1)) is None
                assert report_cache.get(key.format(tid=10)) == 'tournament-10'
                assert report_cache.get(key.format(tid=11)) == 'tournament-11'

    def test_invalidation_with_string_tournament_id(self):
        """Tournament ID is cast to int internally -- string input should work."""
        from services.cache_invalidation import invalidate_tournament_caches
        # Should not raise
        invalidate_tournament_caches('42')

    def test_calls_invalidate_prefix_for_all_prefixes(self):
        """Verify the exact prefixes passed to invalidate_prefix."""
        with patch('services.cache_invalidation.invalidate_prefix') as mock_inv:
            from services.cache_invalidation import invalidate_tournament_caches
            invalidate_tournament_caches(7)

            expected_prefixes = [
                'reports:7:',
                'portal:college:7:',
                'portal:pro:7:',
                'api:standings-poll:7:',
            ]
            actual_prefixes = [call.args[0] for call in mock_inv.call_args_list]
            assert actual_prefixes == expected_prefixes
