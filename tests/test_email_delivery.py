"""Tests for services/email_delivery.py."""

import smtplib

import pytest

from services import email_delivery

# ---------------------------------------------------------------------------
# is_configured()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host,user,password,expected",
    [
        ("smtp.example.com", "user", "pw", True),
        ("", "user", "pw", False),
        ("smtp.example.com", "", "pw", False),
        ("smtp.example.com", "user", "", False),
    ],
)
def test_is_configured(monkeypatch, host, user, password, expected):
    monkeypatch.setenv("SMTP_HOST", host)
    monkeypatch.setenv("SMTP_USER", user)
    monkeypatch.setenv("SMTP_PASSWORD", password)
    assert email_delivery.is_configured() is expected


# ---------------------------------------------------------------------------
# validate_recipients()
# ---------------------------------------------------------------------------


def test_validate_recipients_accepts_valid_addresses(monkeypatch):
    monkeypatch.delenv("EMAIL_ALLOWED_DOMAINS", raising=False)
    valid, invalid = email_delivery.validate_recipients(
        [
            "alice@example.com",
            "bob@proam.org",
            "carol+tag@x.co.uk",
        ]
    )
    assert set(valid) == {"alice@example.com", "bob@proam.org", "carol+tag@x.co.uk"}
    assert invalid == []


def test_validate_recipients_rejects_malformed(monkeypatch):
    monkeypatch.delenv("EMAIL_ALLOWED_DOMAINS", raising=False)
    valid, invalid = email_delivery.validate_recipients(
        [
            "notanemail",
            "missing@.com",
            "@nodomain",
            "ok@example.com",
        ]
    )
    assert valid == ["ok@example.com"]
    assert len(invalid) == 3


def test_validate_recipients_dedups(monkeypatch):
    monkeypatch.delenv("EMAIL_ALLOWED_DOMAINS", raising=False)
    valid, invalid = email_delivery.validate_recipients(
        [
            "a@x.com",
            "A@X.COM",
            "a@x.com",
        ]
    )
    assert valid == ["a@x.com"]
    assert invalid == []


def test_validate_recipients_domain_allowlist(monkeypatch):
    monkeypatch.setenv("EMAIL_ALLOWED_DOMAINS", "proam.org,allowed.com")
    valid, invalid = email_delivery.validate_recipients(
        [
            "ok@proam.org",
            "also-ok@allowed.com",
            "nope@external.com",
        ]
    )
    assert set(valid) == {"ok@proam.org", "also-ok@allowed.com"}
    assert invalid == ["nope@external.com"]


def test_validate_recipients_no_allowlist_permissive(monkeypatch):
    monkeypatch.delenv("EMAIL_ALLOWED_DOMAINS", raising=False)
    valid, _ = email_delivery.validate_recipients(["any@anywhere.io"])
    assert valid == ["any@anywhere.io"]


# ---------------------------------------------------------------------------
# send_document() — mocked SMTP
# ---------------------------------------------------------------------------


class _FakeSMTP:
    """Minimal smtplib.SMTP stand-in. Records everything for assertions."""

    instances = []

    def __init__(self, host, port, timeout=None, raise_on=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.logged_in = False
        self.sent_messages = []
        _FakeSMTP.instances.append(self)
        self._raise_on = raise_on or set()

    def __enter__(self):
        if "connect" in self._raise_on:
            raise smtplib.SMTPConnectError(421, "boom")
        return self

    def __exit__(self, *args):
        return False

    def starttls(self):
        if "starttls" in self._raise_on:
            raise smtplib.SMTPException("tls failed")

    def login(self, user, password):
        if "login" in self._raise_on:
            raise smtplib.SMTPAuthenticationError(535, b"auth failed")
        self.logged_in = True
        self.user = user
        self.password = password

    def send_message(self, msg):
        if "send" in self._raise_on:
            raise smtplib.SMTPException("send failed")
        self.sent_messages.append(msg)


@pytest.fixture()
def smtp_env(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_PORT", "2525")
    monkeypatch.setenv("SMTP_USER", "tester")
    monkeypatch.setenv("SMTP_PASSWORD", "secret-pw-12345")
    monkeypatch.setenv("SMTP_FROM", "noreply@proam.test")
    _FakeSMTP.instances = []


def test_send_document_success(monkeypatch, smtp_env):
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    result = email_delivery.send_document(
        to=["alice@example.com"],
        subject="Test Subject",
        body="Test body.",
        attachment_bytes=b"%PDF-1.4 fake",
        attachment_name="test.pdf",
        attachment_mime="application/pdf",
    )
    assert result.status == "sent"
    assert result.error is None
    assert len(_FakeSMTP.instances) == 1
    sent = _FakeSMTP.instances[0].sent_messages
    assert len(sent) == 1
    assert sent[0]["Subject"] == "Test Subject"


def test_send_document_auth_failure_sanitized(monkeypatch, smtp_env):
    def factory(host, port, timeout=None):
        return _FakeSMTP(host, port, timeout=timeout, raise_on={"login"})

    monkeypatch.setattr(smtplib, "SMTP", factory)
    result = email_delivery.send_document(
        to=["alice@example.com"],
        subject="x",
        body="y",
        attachment_bytes=b"z",
        attachment_name="n.pdf",
    )
    assert result.status == "failed"
    assert result.error is not None
    # Credential MUST NOT leak.
    assert "secret-pw-12345" not in result.error


def test_send_document_smtp_exception_sanitized(monkeypatch, smtp_env):
    def factory(host, port, timeout=None):
        return _FakeSMTP(host, port, timeout=timeout, raise_on={"send"})

    monkeypatch.setattr(smtplib, "SMTP", factory)
    result = email_delivery.send_document(
        to=["alice@example.com"],
        subject="x",
        body="y",
        attachment_bytes=b"z",
        attachment_name="n.pdf",
    )
    assert result.status == "failed"
    assert "secret-pw-12345" not in (result.error or "")


def test_send_document_no_recipients_fails():
    result = email_delivery.send_document(
        to=[],
        subject="x",
        body="y",
        attachment_bytes=b"z",
        attachment_name="n.pdf",
    )
    assert result.status == "failed"


def test_send_document_not_configured_fails(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    result = email_delivery.send_document(
        to=["a@b.com"],
        subject="x",
        body="y",
        attachment_bytes=b"z",
        attachment_name="n.pdf",
    )
    assert result.status == "failed"
    assert "configured" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# queue_document_email() — writes PrintEmailLog + background send
# ---------------------------------------------------------------------------


def test_queue_document_email_writes_log_and_sends(
    app, db_session, monkeypatch, smtp_env
):
    from models import PrintEmailLog
    from tests.conftest import make_tournament

    t = make_tournament(db_session)
    db_session.commit()

    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)

    # Force synchronous send — background_jobs may not be usable in tests.
    # Match the real signature: submit(label, fn, *args, **kwargs).
    def sync_submit(label, fn, *args, **kwargs):
        fn(*args, **kwargs)

    import services.background_jobs as bj

    monkeypatch.setattr(bj, "submit", sync_submit)

    log_id = email_delivery.queue_document_email(
        tournament_id=t.id,
        doc_key="heat_sheets",
        entity_id=None,
        recipients=["alice@example.com"],
        subject="subj",
        body="body",
        attachment_bytes=b"pdf",
        attachment_name="heat_sheets.pdf",
        attachment_mime="application/pdf",
        sent_by_user_id=None,
    )

    log = db_session.get(PrintEmailLog, log_id)
    assert log is not None
    assert log.status == "sent"
    assert log.get_recipients() == ["alice@example.com"]


def test_queue_document_email_marks_failed_on_smtp_error(
    app, db_session, monkeypatch, smtp_env
):
    from models import PrintEmailLog
    from tests.conftest import make_tournament

    t = make_tournament(db_session)
    db_session.commit()

    def factory(host, port, timeout=None):
        return _FakeSMTP(host, port, timeout=timeout, raise_on={"login"})

    monkeypatch.setattr(smtplib, "SMTP", factory)

    # Match the real signature: submit(label, fn, *args, **kwargs).
    def sync_submit(label, fn, *args, **kwargs):
        fn(*args, **kwargs)

    import services.background_jobs as bj

    monkeypatch.setattr(bj, "submit", sync_submit)

    log_id = email_delivery.queue_document_email(
        tournament_id=t.id,
        doc_key="heat_sheets",
        entity_id=None,
        recipients=["alice@example.com"],
        subject="subj",
        body="body",
        attachment_bytes=b"pdf",
        attachment_name="heat_sheets.pdf",
    )
    log = db_session.get(PrintEmailLog, log_id)
    assert log is not None
    assert log.status == "failed"
    assert log.error is not None
    assert "secret-pw-12345" not in log.error
