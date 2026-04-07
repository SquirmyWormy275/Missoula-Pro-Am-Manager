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
import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

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

class TestReportCache:
    """Tests for the in-memory L1 TTL cache (disk layer bypassed)."""

    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        """Clear the module-level cache dict before each test."""
        from services import report_cache
        with report_cache._lock:
            report_cache._cache.clear()
        yield
        with report_cache._lock:
            report_cache._cache.clear()

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

    def test_clear_via_invalidate_prefix_empty_string(self):
        """invalidate_prefix('') should match all keys."""
        from services.report_cache import get, invalidate_prefix, set
        set('a', 1, ttl_seconds=60)
        set('b', 2, ttl_seconds=60)
        invalidate_prefix('')
        assert get('a') is None
        assert get('b') is None

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

    def test_configure_changes_max_workers(self):
        from services import background_jobs
        old_executor = background_jobs._executor
        background_jobs.configure(4)
        assert background_jobs._executor is not old_executor
        assert background_jobs._executor._max_workers == 4
        # Restore default
        background_jobs.configure(2)


# ===================================================================
# CacheInvalidation tests  (services/cache_invalidation.py)
# ===================================================================

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
                'portal:college:7',
                'portal:pro:7',
                'api:standings-poll:7',
            ]
            actual_prefixes = [call.args[0] for call in mock_inv.call_args_list]
            assert actual_prefixes == expected_prefixes
