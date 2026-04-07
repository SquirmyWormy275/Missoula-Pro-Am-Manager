"""
Tests for SMS notification and backup services.

Covers:
  - services/sms_notify.py: is_configured(), send_sms(), notify_competitor()
  - services/backup.py: is_s3_configured(), _timestamp(), _db_path_from_uri(),
    backup_to_local(), backup_to_s3()

Run:
    pytest tests/test_sms_backup.py -v

Design notes:
  - Uses unittest.mock.patch for env vars and external clients.
  - Uses pytest's tmp_path fixture for filesystem tests.
  - No Flask app context required — these services are pure functions
    (sms_notify depends only on env vars and the twilio package).
"""
import os
from unittest.mock import MagicMock, patch

import pytest

# =====================================================================
# SMS Notify Tests
# =====================================================================

class TestSmsIsConfigured:
    """Test sms_notify.is_configured() behaviour."""

    @patch.dict(os.environ, {}, clear=True)
    def test_returns_false_when_env_vars_missing(self):
        """is_configured() returns False when no TWILIO env vars are set."""
        # Also need to mock the twilio import to succeed so we test the env-var path
        with patch('services.sms_notify.is_configured') as _:
            # Re-import to get fresh module state is tricky; instead just call directly
            pass
        from services.sms_notify import is_configured
        # Twilio package may or may not be installed in test env,
        # but env vars are definitely missing — either path returns False.
        assert is_configured() is False

    @patch('services.sms_notify.os.environ', {
        'TWILIO_ACCOUNT_SID': 'ACtest123',
        'TWILIO_AUTH_TOKEN': 'authtoken456',
        'TWILIO_FROM_NUMBER': '+15551234567',
    })
    def test_returns_true_when_all_env_vars_set(self):
        """is_configured() returns True when all TWILIO env vars are present
        and the twilio package is importable."""
        from services.sms_notify import is_configured
        # Mock the twilio import inside is_configured
        fake_client_class = MagicMock()
        with patch.dict('sys.modules', {'twilio': MagicMock(), 'twilio.rest': MagicMock(Client=fake_client_class)}):
            assert is_configured() is True

    @patch('services.sms_notify.os.environ', {
        'TWILIO_ACCOUNT_SID': 'ACtest123',
        'TWILIO_AUTH_TOKEN': 'authtoken456',
        'TWILIO_FROM_NUMBER': '+15551234567',
    })
    def test_returns_false_when_twilio_not_installed(self):
        """is_configured() returns False when twilio package is not importable."""
        import builtins

        from services.sms_notify import is_configured
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == 'twilio.rest' or name == 'twilio':
                raise ImportError('No module named twilio')
            return real_import(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=mock_import):
            assert is_configured() is False


class TestSendSms:
    """Test sms_notify.send_sms() behaviour."""

    def test_send_sms_noop_when_not_configured(self):
        """send_sms() returns False and does not raise when unconfigured."""
        from services.sms_notify import send_sms
        with patch.dict(os.environ, {}, clear=True):
            result = send_sms('+15559876543', 'Test message')
            assert result is False

    def test_send_sms_returns_false_for_empty_number(self):
        """send_sms() returns False when to_number is empty."""
        from services.sms_notify import send_sms
        assert send_sms('', 'Hello') is False

    def test_send_sms_returns_false_for_empty_message(self):
        """send_sms() returns False when message is empty."""
        from services.sms_notify import send_sms
        assert send_sms('+15551234567', '') is False

    def test_send_sms_with_mock_twilio_client(self):
        """send_sms() calls client.messages.create() and returns True."""
        from services.sms_notify import send_sms

        mock_client = MagicMock()
        mock_client.messages.create.return_value = MagicMock(sid='SM123')

        env = {
            'TWILIO_ACCOUNT_SID': 'ACtest',
            'TWILIO_AUTH_TOKEN': 'token',
            'TWILIO_FROM_NUMBER': '+15550001111',
        }
        with patch('services.sms_notify.os.environ', env):
            with patch('services.sms_notify._get_twilio_client', return_value=mock_client):
                result = send_sms('+15559876543', 'Flight 1 starting!')
                assert result is True
                mock_client.messages.create.assert_called_once_with(
                    body='Flight 1 starting!',
                    from_='+15550001111',
                    to='+15559876543',
                )

    def test_send_sms_graceful_failure_on_twilio_error(self):
        """send_sms() returns False and does not raise on Twilio error."""
        from services.sms_notify import send_sms

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception('Twilio API error')

        env = {
            'TWILIO_ACCOUNT_SID': 'ACtest',
            'TWILIO_AUTH_TOKEN': 'token',
            'TWILIO_FROM_NUMBER': '+15550001111',
        }
        with patch('services.sms_notify.os.environ', env):
            with patch('services.sms_notify._get_twilio_client', return_value=mock_client):
                result = send_sms('+15559876543', 'Test')
                assert result is False

    def test_send_sms_normalizes_number_without_plus(self):
        """send_sms() prepends +1 to numbers missing a + prefix."""
        from services.sms_notify import send_sms

        mock_client = MagicMock()
        mock_client.messages.create.return_value = MagicMock(sid='SM456')

        env = {
            'TWILIO_ACCOUNT_SID': 'ACtest',
            'TWILIO_AUTH_TOKEN': 'token',
            'TWILIO_FROM_NUMBER': '+15550001111',
        }
        with patch('services.sms_notify.os.environ', env):
            with patch('services.sms_notify._get_twilio_client', return_value=mock_client):
                send_sms('5559876543', 'Hello')
                call_args = mock_client.messages.create.call_args
                assert call_args.kwargs['to'] == '+15559876543'


class TestNotifyCompetitor:
    """Test sms_notify.notify_competitor() behaviour."""

    def test_notify_no_channels(self):
        """notify_competitor() returns both False when no channels available."""
        from services.sms_notify import notify_competitor
        result = notify_competitor(
            phone=None, email=None, phone_opted_in=False,
            message='Test',
        )
        assert result == {'sms': False, 'email': False}

    def test_notify_sms_only_when_opted_in(self):
        """notify_competitor() attempts SMS when phone_opted_in is True."""
        from services.sms_notify import notify_competitor
        with patch('services.sms_notify.send_sms', return_value=True) as mock_sms:
            with patch('services.sms_notify.send_email', return_value=False):
                result = notify_competitor(
                    phone='+15551234567', email=None,
                    phone_opted_in=True, message='Your flight is up!',
                )
                assert result['sms'] is True
                mock_sms.assert_called_once_with('+15551234567', 'Your flight is up!')

    def test_notify_skips_sms_when_not_opted_in(self):
        """notify_competitor() does not attempt SMS when phone_opted_in is False."""
        from services.sms_notify import notify_competitor
        with patch('services.sms_notify.send_sms') as mock_sms:
            with patch('services.sms_notify.send_email', return_value=False):
                notify_competitor(
                    phone='+15551234567', email=None,
                    phone_opted_in=False, message='Test',
                )
                mock_sms.assert_not_called()


class TestEmailIsConfigured:
    """Test sms_notify.email_is_configured()."""

    @patch.dict(os.environ, {}, clear=True)
    def test_returns_false_when_missing(self):
        from services.sms_notify import email_is_configured
        assert email_is_configured() is False

    @patch.dict(os.environ, {
        'SMTP_HOST': 'smtp.example.com',
        'SMTP_USER': 'user@example.com',
        'SMTP_PASSWORD': 'secret',
    })
    def test_returns_true_when_set(self):
        from services.sms_notify import email_is_configured
        assert email_is_configured() is True


# =====================================================================
# Backup Service Tests
# =====================================================================

class TestBackupIsS3Configured:
    """Test backup.is_s3_configured() behaviour."""

    @patch.dict(os.environ, {}, clear=True)
    def test_returns_false_when_env_vars_missing(self):
        """is_s3_configured() returns False when AWS env vars are absent."""
        from services.backup import is_s3_configured
        assert is_s3_configured() is False

    @patch('services.backup.os.environ', {
        'BACKUP_S3_BUCKET': 'my-bucket',
        'AWS_ACCESS_KEY_ID': 'AKIA1234',
        'AWS_SECRET_ACCESS_KEY': 'secret',
    })
    def test_returns_true_when_all_env_vars_set(self):
        """is_s3_configured() returns True when all AWS env vars are present
        and boto3 is importable."""
        from services.backup import is_s3_configured
        fake_boto3 = MagicMock()
        with patch.dict('sys.modules', {'boto3': fake_boto3}):
            assert is_s3_configured() is True

    @patch('services.backup.os.environ', {
        'BACKUP_S3_BUCKET': 'my-bucket',
        'AWS_ACCESS_KEY_ID': 'AKIA1234',
        'AWS_SECRET_ACCESS_KEY': 'secret',
    })
    def test_returns_false_when_boto3_not_installed(self):
        """is_s3_configured() returns False when boto3 is not importable."""
        import builtins

        from services.backup import is_s3_configured
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == 'boto3':
                raise ImportError('No module named boto3')
            return real_import(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=mock_import):
            assert is_s3_configured() is False


class TestBackupTimestamp:
    """Test backup._timestamp() helper."""

    def test_returns_iso_format_string(self):
        """_timestamp() returns a string matching YYYYMMDD_HHMMSS format."""
        from services.backup import _timestamp
        ts = _timestamp()
        assert isinstance(ts, str)
        assert len(ts) == 15  # YYYYMMDD_HHMMSS = 8 + 1 + 6
        assert ts[8] == '_'
        # All chars except underscore should be digits
        assert ts.replace('_', '').isdigit()

    def test_timestamp_uses_utcnow(self):
        """_timestamp() uses datetime.utcnow() for consistency."""
        from datetime import datetime

        from services.backup import _timestamp
        with patch('services.backup.datetime') as mock_dt:
            mock_dt.utcnow.return_value = datetime(2026, 3, 20, 14, 30, 45)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = _timestamp()
            assert result == '20260320_143045'
            mock_dt.utcnow.assert_called_once()


class TestDbPathFromUri:
    """Test backup._db_path_from_uri() helper."""

    def test_extracts_absolute_path_from_sqlite_uri(self, tmp_path):
        """_db_path_from_uri() extracts path from absolute sqlite URI."""
        from services.backup import _db_path_from_uri
        # Create a temporary DB file
        db_file = tmp_path / 'test.db'
        db_file.write_bytes(b'SQLite format 3')
        db_path = str(db_file).replace('\\', '/')
        uri = f'sqlite:///{db_path}'
        result = _db_path_from_uri(uri, str(tmp_path))
        assert result is not None
        assert os.path.exists(result)

    def test_returns_none_for_non_sqlite_uri(self):
        """_db_path_from_uri() returns None for PostgreSQL and other URIs."""
        from services.backup import _db_path_from_uri
        assert _db_path_from_uri('postgresql://user:pass@host/db', '/instance') is None

    def test_returns_none_for_mysql_uri(self):
        """_db_path_from_uri() returns None for MySQL URI."""
        from services.backup import _db_path_from_uri
        assert _db_path_from_uri('mysql://user:pass@host/db', '/instance') is None

    def test_returns_none_when_file_does_not_exist(self, tmp_path):
        """_db_path_from_uri() returns None when the referenced file does not exist."""
        from services.backup import _db_path_from_uri
        uri = f'sqlite:///{tmp_path}/nonexistent.db'
        result = _db_path_from_uri(uri, str(tmp_path))
        assert result is None

    def test_relative_path_joined_with_instance_path(self, tmp_path):
        """_db_path_from_uri() joins relative path with instance_path."""
        from services.backup import _db_path_from_uri
        db_file = tmp_path / 'proam.db'
        db_file.write_bytes(b'SQLite format 3')
        # Relative URI (no leading slash after sqlite:///)
        uri = 'sqlite:///proam.db'
        result = _db_path_from_uri(uri, str(tmp_path))
        # Since 'proam.db' is not absolute, it gets joined with instance_path
        assert result is not None
        assert result.endswith('proam.db')


class TestBackupToLocal:
    """Test backup.backup_to_local() with real filesystem via tmp_path."""

    def test_creates_backup_file(self, tmp_path):
        """backup_to_local() copies the source DB to dest_dir."""
        from services.backup import backup_to_local
        # Create a fake source DB
        src = tmp_path / 'source.db'
        src.write_bytes(b'SQLite format 3' + b'\x00' * 100)

        dest_dir = tmp_path / 'backups'
        result = backup_to_local(str(src), str(dest_dir), tournament_id=1)

        assert result['ok'] is True
        assert os.path.exists(result['dest'])
        assert result['size_bytes'] > 0
        assert result['error'] is None

    def test_returns_dict_with_expected_keys(self, tmp_path):
        """backup_to_local() returns dict with ok, dest, size_bytes, error."""
        from services.backup import backup_to_local
        src = tmp_path / 'source.db'
        src.write_bytes(b'test data')

        dest_dir = tmp_path / 'backups'
        result = backup_to_local(str(src), str(dest_dir), tournament_id=42)

        assert set(result.keys()) >= {'ok', 'dest', 'size_bytes', 'error'}
        assert 'proam_t42_' in result['dest']
        assert result['dest'].endswith('.db')

    def test_creates_dest_dir_if_missing(self, tmp_path):
        """backup_to_local() creates the destination directory if it does not exist."""
        from services.backup import backup_to_local
        src = tmp_path / 'source.db'
        src.write_bytes(b'test')

        nested_dir = tmp_path / 'a' / 'b' / 'c'
        assert not nested_dir.exists()

        result = backup_to_local(str(src), str(nested_dir), tournament_id=1)
        assert result['ok'] is True
        assert nested_dir.exists()

    def test_graceful_failure_on_missing_source(self, tmp_path):
        """backup_to_local() returns ok=False when source file does not exist."""
        from services.backup import backup_to_local
        result = backup_to_local(
            str(tmp_path / 'nonexistent.db'),
            str(tmp_path / 'backups'),
            tournament_id=1,
        )
        assert result['ok'] is False
        assert result['error'] is not None


class TestBackupToS3:
    """Test backup.backup_to_s3() with mocked boto3."""

    def test_returns_error_when_not_configured(self):
        """backup_to_s3() returns ok=False when S3 is not configured."""
        from services.backup import backup_to_s3
        with patch.dict(os.environ, {}, clear=True):
            result = backup_to_s3('/tmp/test.db', tournament_id=1)
            assert result['ok'] is False
            assert 'not configured' in result['error'].lower()

    def test_upload_called_with_mocked_boto3(self, tmp_path):
        """backup_to_s3() calls s3.upload_file() when configured."""
        from services.backup import backup_to_s3

        # Create a fake source DB
        src = tmp_path / 'source.db'
        src.write_bytes(b'SQLite format 3' + b'\x00' * 100)

        mock_s3_client = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_s3_client

        env = {
            'BACKUP_S3_BUCKET': 'test-bucket',
            'AWS_ACCESS_KEY_ID': 'AKIA1234',
            'AWS_SECRET_ACCESS_KEY': 'secret123',
        }

        mock_boto3 = MagicMock()
        mock_boto3.Session.return_value = mock_session

        with patch('services.backup.os.environ', env):
            with patch('services.backup.is_s3_configured', return_value=True):
                with patch.dict('sys.modules', {'boto3': mock_boto3}):
                    result = backup_to_s3(str(src), tournament_id=5)

        assert result['ok'] is True
        assert result['bucket'] == 'test-bucket'
        assert 'tournament_5' in result['key']
        assert result['size_bytes'] > 0
        assert result['error'] is None
        mock_s3_client.upload_file.assert_called_once()

    def test_graceful_failure_on_s3_error(self, tmp_path):
        """backup_to_s3() returns ok=False on S3 upload error."""
        from services.backup import backup_to_s3

        src = tmp_path / 'source.db'
        src.write_bytes(b'test')

        mock_s3_client = MagicMock()
        mock_s3_client.upload_file.side_effect = Exception('Access Denied')
        mock_session = MagicMock()
        mock_session.client.return_value = mock_s3_client

        env = {
            'BACKUP_S3_BUCKET': 'test-bucket',
            'AWS_ACCESS_KEY_ID': 'AKIA1234',
            'AWS_SECRET_ACCESS_KEY': 'secret123',
        }

        mock_boto3 = MagicMock()
        mock_boto3.Session.return_value = mock_session

        with patch('services.backup.os.environ', env):
            with patch('services.backup.is_s3_configured', return_value=True):
                with patch.dict('sys.modules', {'boto3': mock_boto3}):
                    result = backup_to_s3(str(src), tournament_id=5)

        assert result['ok'] is False
        assert 'Access Denied' in result['error']
