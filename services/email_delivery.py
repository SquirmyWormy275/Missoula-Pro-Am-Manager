"""Generic email-delivery service for the Print Hub + ALA report.

Single source of truth for SMTP sends in this app. Consolidates the pattern
previously inlined in routes/reporting.py::_send_ala_email.

API:
  - ``is_configured() -> bool``
  - ``validate_recipients(list) -> tuple[list_valid, list_invalid]``
  - ``send_document(...) -> EmailResult``  (synchronous SMTP — used by worker)
  - ``queue_document_email(...) -> int``   (writes PrintEmailLog, submits to
    background_jobs, returns log id)

Design rules:
  * ``send_document`` is stateless: no DB writes. Easy to mock in tests.
  * ``queue_document_email`` is the only public entry point from route
    handlers — guarantees every send attempt is logged.
  * SMTP credentials NEVER appear in flash messages or PrintEmailLog.error.
  * The domain allowlist (``EMAIL_ALLOWED_DOMAINS`` env var) is opt-in.
    Unset = permissive.
"""

from __future__ import annotations

import logging
import os
import re
import smtplib
from dataclasses import dataclass
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


# RFC-5322 local-part is complex; we accept anything with a single @ and a
# reasonable domain (one or more labels separated by dots, TLD >=2 chars).
# Good enough to catch typos; real delivery will reject anything the MTA
# doesn't like.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


@dataclass
class EmailResult:
    """Output of a single SMTP send attempt."""

    status: str  # 'sent' or 'failed'
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def is_configured() -> bool:
    """True iff SMTP_HOST, SMTP_USER, and SMTP_PASSWORD are all set."""
    return bool(
        os.environ.get("SMTP_HOST")
        and os.environ.get("SMTP_USER")
        and os.environ.get("SMTP_PASSWORD")
    )


def _allowed_domains() -> Optional[set]:
    """Parse EMAIL_ALLOWED_DOMAINS. Returns None if unset (permissive)."""
    raw = os.environ.get("EMAIL_ALLOWED_DOMAINS", "").strip()
    if not raw:
        return None
    return {d.strip().lower() for d in raw.split(",") if d.strip()}


# ---------------------------------------------------------------------------
# Recipient validation
# ---------------------------------------------------------------------------


def validate_recipients(recipients: Iterable[str]) -> tuple[list[str], list[str]]:
    """Partition recipients into (valid, invalid).

    An address is invalid if it fails the shape regex OR (when
    ``EMAIL_ALLOWED_DOMAINS`` is set) its domain is not in the allowlist.
    Invalid addresses are returned as-is so the UI can flash them back to
    the user.
    """
    allowed = _allowed_domains()
    valid: list[str] = []
    invalid: list[str] = []

    seen = set()
    for raw in recipients or []:
        addr = (raw or "").strip().lower()
        if not addr or addr in seen:
            continue
        seen.add(addr)
        if not _EMAIL_RE.match(addr):
            invalid.append(addr)
            continue
        if allowed is not None:
            domain = addr.rsplit("@", 1)[-1]
            if domain not in allowed:
                invalid.append(addr)
                continue
        valid.append(addr)

    return valid, invalid


# ---------------------------------------------------------------------------
# Synchronous send (called by the worker thread)
# ---------------------------------------------------------------------------


def send_document(
    to: list[str],
    subject: str,
    body: str,
    attachment_bytes: bytes,
    attachment_name: str,
    attachment_mime: str = "application/pdf",
) -> EmailResult:
    """Synchronous SMTP send. No DB writes — stateless & easy to mock.

    Returns EmailResult(status='sent'|'failed', error=...). Never raises.
    Credentials NEVER appear in the error string.
    """
    if not to:
        return EmailResult(status="failed", error="No recipients.")
    if not is_configured():
        return EmailResult(status="failed", error="SMTP not configured.")

    smtp_host = os.environ.get("SMTP_HOST", "")
    smtp_port = int(os.environ.get("SMTP_PORT", 587))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_password = os.environ.get("SMTP_PASSWORD", "")
    from_addr = os.environ.get("SMTP_FROM", smtp_user)

    try:
        msg = MIMEMultipart()
        msg["From"] = from_addr
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        # Decide major/minor type for the MIME attachment.
        if "/" in attachment_mime:
            maintype, subtype = attachment_mime.split("/", 1)
        else:
            maintype, subtype = "application", "octet-stream"

        part = MIMEBase(maintype, subtype)
        part.set_payload(attachment_bytes)
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f'attachment; filename="{attachment_name}"',
        )
        msg.attach(part)

        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)

        return EmailResult(status="sent", error=None)
    except smtplib.SMTPAuthenticationError:
        # Do NOT include the password or detailed auth response — could leak.
        return EmailResult(status="failed", error="SMTP authentication failed.")
    except smtplib.SMTPException as exc:
        msg_txt = str(exc)
        # Defensive scrub: never surface the password.
        if smtp_password and smtp_password in msg_txt:
            msg_txt = msg_txt.replace(smtp_password, "***")
        return EmailResult(status="failed", error=f"SMTP error: {msg_txt}")
    except Exception as exc:
        msg_txt = str(exc)
        if smtp_password and smtp_password in msg_txt:
            msg_txt = msg_txt.replace(smtp_password, "***")
        logger.exception("send_document failed")
        return EmailResult(status="failed", error=f"Delivery failed: {msg_txt}")


# ---------------------------------------------------------------------------
# Async queue entry point (used by routes)
# ---------------------------------------------------------------------------


def queue_document_email(
    tournament_id: int,
    doc_key: str,
    entity_id: Optional[int],
    recipients: list[str],
    subject: str,
    body: str,
    attachment_bytes: bytes,
    attachment_name: str,
    attachment_mime: str = "application/pdf",
    sent_by_user_id: Optional[int] = None,
) -> int:
    """Write a PrintEmailLog row (status='queued'), submit an SMTP send to
    the background-jobs thread pool, return the log id.

    The worker updates the PrintEmailLog row in place when the send
    succeeds or fails, and writes an AuditLog entry.
    """
    from database import db
    from models import PrintEmailLog

    log = PrintEmailLog(
        tournament_id=tournament_id,
        doc_key=doc_key,
        entity_id=entity_id,
        subject=subject[:300],
        sent_at=datetime.utcnow(),
        sent_by_user_id=sent_by_user_id,
        status="queued",
        error=None,
    )
    log.set_recipients(recipients)
    db.session.add(log)
    db.session.commit()
    log_id = log.id

    # Submit to the background-jobs thread pool. Fall back to synchronous
    # send if background_jobs isn't available (e.g., in tests where the
    # pool is shut down).
    try:
        from services import background_jobs

        # submit(label, fn, *args, ...) — label first, not fn.
        background_jobs.submit(
            f"email:{doc_key}",
            _worker_send,
            log_id,
            recipients,
            subject,
            body,
            attachment_bytes,
            attachment_name,
            attachment_mime,
        )
    except Exception:
        logger.exception("background_jobs.submit failed — falling back to sync send")
        _worker_send(
            log_id,
            recipients,
            subject,
            body,
            attachment_bytes,
            attachment_name,
            attachment_mime,
        )

    return log_id


def _worker_send(
    log_id: int,
    recipients: list[str],
    subject: str,
    body: str,
    attachment_bytes: bytes,
    attachment_name: str,
    attachment_mime: str,
) -> None:
    """Background worker: run the SMTP send, update PrintEmailLog + AuditLog.

    Runs inside the Flask app context provided by services.background_jobs.
    """
    from database import db
    from models import PrintEmailLog
    from services.audit import log_action

    result = send_document(
        to=recipients,
        subject=subject,
        body=body,
        attachment_bytes=attachment_bytes,
        attachment_name=attachment_name,
        attachment_mime=attachment_mime,
    )

    try:
        log = db.session.get(PrintEmailLog, log_id)
        if log is not None:
            log.status = result.status
            log.error = result.error
            db.session.commit()
    except Exception:
        logger.exception("Failed to update PrintEmailLog row %s", log_id)
        try:
            db.session.rollback()
        except Exception:
            pass

    try:
        if result.status == "sent":
            log_action(
                "email_sent",
                "print_email_log",
                log_id,
                {
                    "recipients": recipients,
                    "subject": subject,
                },
            )
        else:
            log_action(
                "email_failed",
                "print_email_log",
                log_id,
                {
                    "recipients": recipients,
                    "subject": subject,
                    "error": result.error,
                },
            )
    except Exception:
        logger.exception("AuditLog write failed for email log %s", log_id)
