"""Helpers for writing security audit logs."""
import json
import logging
from datetime import datetime

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

logger = logging.getLogger(__name__)

# Dialects that let a second connection commit a row while the first connection
# still holds an open write transaction. Stated as an allowlist so a backend
# added later is treated as unsafe until somebody checks it, rather than being
# silently opted in and discovered at the point a live request dies.
_CONCURRENT_WRITE_DIALECTS = frozenset({'postgresql', 'mysql', 'mariadb'})


def _supports_independent_write() -> bool:
    try:
        return db.engine.dialect.name in _CONCURRENT_WRITE_DIALECTS
    except Exception:
        # No engine, no app context, or a bind that cannot be resolved. Treat
        # that as unsafe and use the caller's session.
        return False


def log_action(action: str, entity_type: str, entity_id: int | None = None, details: dict | None = None) -> None:
    """Append an audit log record on its own connection.

    Why this does not simply ``db.session.add()``, and why it does not simply
    ``db.session.commit()`` either.

    The old body added the row to the request session and stopped there. The
    house idiom throughout this codebase is commit-then-log, so nothing commits
    afterwards and the pending row is discarded at teardown. Measured against a
    copy of the production database: zero audit rows for payout settlement, fee
    payment and Saturday ordering, all of which call this function.

    Committing ``db.session`` from in here would trade that for something worse.
    There are 115 call sites. 23 of them log with no commit on either side, and
    19 sit within a few lines of a ``rollback()``. A commit inside this function
    would flush whatever half-finished work those callers still had pending,
    turning a lost-attribution bug into a partial-write bug on live money paths.

    So the row is written on an independent connection and committed there. The
    audit record lands regardless of what the caller's transaction goes on to do,
    and the caller's transaction is never touched. If that write cannot be made,
    fall back to the old session-add rather than raising: this is instrumentation,
    and instrumentation must never take down a live request mid-show.

    SQLite is excluded from the independent write, and the exclusion is checked
    up front rather than discovered by catching the failure. SQLite locks the
    whole database file, so a second connection writing while the caller holds a
    write transaction cannot succeed by construction. Merely attempting it is
    destructive: the first version of this function tried and caught, and the
    caught attempt still left the file locked hard enough that the caller's own
    later commit raised. Measured, not inferred. On a tree with this fix reverted
    the same 24 tests set up cleanly; with the try-and-catch version they errored
    at ``sqlite3.OperationalError: database is locked`` on the login audit row,
    and three more failed the same way inside the route's commit.

    So on SQLite this function does exactly what it did before: add to the
    caller's session and let the caller decide. That preserves the old
    lost-attribution bug on SQLite and nowhere else. Production is PostgreSQL,
    which is where payout settlement, fee payment and Saturday ordering actually
    run, and PostgreSQL is where the durable write applies.
    """
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

    values = {
        'actor_user_id': actor_id,
        'action': action,
        'entity_type': entity_type,
        'entity_id': entity_id,
        'ip_address': ip_address,
        'user_agent': user_agent,
        'details_json': json.dumps(details or {}),
        # Set explicitly. A Core insert does apply the column's Python-side
        # default, but naming it here keeps this independent of the model.
        'created_at': datetime.utcnow(),
    }

    if _supports_independent_write():
        try:
            with db.engine.connect() as conn:
                conn.execute(AuditLog.__table__.insert().values(**values))
                conn.commit()
            return
        except Exception:
            logger.warning(
                'audit: independent write failed for action=%s entity=%s/%s, '
                'falling back to the request session',
                action, entity_type, entity_id, exc_info=True,
            )

    try:
        db.session.add(AuditLog(**values))
    except Exception:
        logger.warning('audit: could not record action=%s at all', action, exc_info=True)
