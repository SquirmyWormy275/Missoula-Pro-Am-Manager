"""
Notification service: SMS via Twilio and email via SMTP.

Both channels degrade gracefully — calls are silent no-ops when credentials
are absent or the required library is not installed.

SMS env vars:   TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER
Email env vars: SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD,
                SMTP_FROM (e.g. "Missoula Pro Am <noreply@example.com>")
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def _get_twilio_client():
    """Return a Twilio REST client, or None if unavailable."""
    try:
        from twilio.rest import Client  # type: ignore
    except ImportError:
        return None

    sid = os.environ.get('TWILIO_ACCOUNT_SID', '').strip()
    token = os.environ.get('TWILIO_AUTH_TOKEN', '').strip()
    if not sid or not token:
        return None

    return Client(sid, token)


def send_sms(to_number: str, message: str) -> bool:
    """
    Send an SMS to *to_number* with *message*.

    Returns True on success, False on any failure (including unconfigured).
    Never raises — all errors are logged and swallowed.
    """
    if not to_number or not message:
        return False

    from_number = os.environ.get('TWILIO_FROM_NUMBER', '').strip()
    if not from_number:
        logger.debug('SMS skipped: TWILIO_FROM_NUMBER not set')
        return False

    client = _get_twilio_client()
    if client is None:
        logger.debug('SMS skipped: Twilio client unavailable (missing package or credentials)')
        return False

    # Normalise number — ensure it has a leading +
    normalized = to_number.strip()
    if normalized and not normalized.startswith('+'):
        normalized = '+1' + normalized.lstrip('0')

    try:
        client.messages.create(
            body=message,
            from_=from_number,
            to=normalized,
        )
        logger.info('SMS sent to %s', normalized)
        return True
    except Exception as exc:
        logger.warning('SMS delivery failed to %s: %s', normalized, exc)
        return False


def is_configured() -> bool:
    """Return True if Twilio credentials are present and twilio package is installed."""
    try:
        from twilio.rest import Client  # noqa: F401  type: ignore
    except ImportError:
        return False
    sid = os.environ.get('TWILIO_ACCOUNT_SID', '').strip()
    token = os.environ.get('TWILIO_AUTH_TOKEN', '').strip()
    frm = os.environ.get('TWILIO_FROM_NUMBER', '').strip()
    return bool(sid and token and frm)


# ---------------------------------------------------------------------------
# Email via SMTP
# ---------------------------------------------------------------------------

def email_is_configured() -> bool:
    """Return True if SMTP credentials are set in environment."""
    return bool(
        os.environ.get('SMTP_HOST', '').strip()
        and os.environ.get('SMTP_USER', '').strip()
        and os.environ.get('SMTP_PASSWORD', '').strip()
    )


def send_email(to_address: str, subject: str, body_text: str, body_html: str | None = None) -> bool:
    """
    Send an email to *to_address*.

    body_html is optional — if provided a multipart/alternative message is sent
    so clients that support HTML receive the richer version.

    Returns True on success, False on any failure (including unconfigured).
    Never raises — all errors are logged and swallowed.
    """
    if not to_address or not subject or not body_text:
        return False

    host = os.environ.get('SMTP_HOST', '').strip()
    user = os.environ.get('SMTP_USER', '').strip()
    password = os.environ.get('SMTP_PASSWORD', '').strip()
    if not host or not user or not password:
        logger.debug('Email skipped: SMTP credentials not configured')
        return False

    try:
        port = int(os.environ.get('SMTP_PORT', '587'))
    except (ValueError, TypeError):
        port = 587

    from_addr = os.environ.get('SMTP_FROM', user).strip()

    try:
        if body_html:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = from_addr
            msg['To'] = to_address
            msg.attach(MIMEText(body_text, 'plain'))
            msg.attach(MIMEText(body_html, 'html'))
        else:
            msg = MIMEText(body_text, 'plain')
            msg['Subject'] = subject
            msg['From'] = from_addr
            msg['To'] = to_address

        with smtplib.SMTP(host, port, timeout=10) as server:
            server.ehlo()
            server.starttls()
            server.login(user, password)
            server.sendmail(from_addr, [to_address], msg.as_string())

        logger.info('Email sent to %s — %s', to_address, subject)
        return True
    except Exception as exc:
        logger.warning('Email delivery failed to %s: %s', to_address, exc)
        return False


def notify_competitor(
    *,
    phone: str | None,
    email: str | None,
    phone_opted_in: bool,
    message: str,
    subject: str = 'Missoula Pro Am — Flight Update',
) -> dict:
    """
    Send a notification to a competitor via whichever channels are available
    and opted-in.

    Args:
        phone: Competitor phone number (may be None)
        email: Competitor email address (may be None)
        phone_opted_in: Whether the competitor opted into SMS
        message: Plain-text message body (used for both SMS and email)
        subject: Email subject line

    Returns:
        Dict with keys 'sms' and 'email', each True/False for send outcome.
    """
    sms_ok = False
    email_ok = False

    if phone_opted_in and phone:
        sms_ok = send_sms(phone, message)

    if email:
        email_ok = send_email(email, subject, message)

    return {'sms': sms_ok, 'email': email_ok}
