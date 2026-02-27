"""Helpers for writing security audit logs."""
import json
from flask import request
try:
    from flask_login import current_user
except ModuleNotFoundError:
    class _AnonymousCurrentUser:
        is_authenticated = False
        id = None

    current_user = _AnonymousCurrentUser()
from database import db
from models.audit_log import AuditLog


def log_action(action: str, entity_type: str, entity_id: int | None = None, details: dict | None = None) -> None:
    """Append a best-effort audit log record."""
    try:
        actor_id = current_user.id if getattr(current_user, 'is_authenticated', False) else None
    except Exception:
        actor_id = None

    try:
        ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
        user_agent = (request.user_agent.string or '')[:255]
    except Exception:
        ip_address = None
        user_agent = None

    entry = AuditLog(
        actor_user_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        ip_address=ip_address,
        user_agent=user_agent,
        details_json=json.dumps(details or {}),
    )
    db.session.add(entry)
