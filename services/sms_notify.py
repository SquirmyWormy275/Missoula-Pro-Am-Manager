"""
SMS notification service via Twilio.

Gracefully degrades: if twilio is not installed or credentials are missing,
all send_sms() calls are silent no-ops. Enable by setting env vars:
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER
"""
from __future__ import annotations

import logging
import os

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
