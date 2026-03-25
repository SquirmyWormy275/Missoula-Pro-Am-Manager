"""Structured logging with request IDs and optional error monitoring."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from flask import g, request


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
        }
        # Attach request ID if available (set by request_id_middleware)
        request_id = getattr(g, 'request_id', None) if _has_request_context() else None
        if request_id:
            payload['request_id'] = request_id
        if record.exc_info:
            payload['exception'] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True)


def _has_request_context() -> bool:
    """Check if we're inside a Flask request context without importing has_request_context."""
    try:
        _ = request.method  # noqa: F841
        return True
    except RuntimeError:
        return False


def configure_logging(structured: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    if structured:
        for handler in root.handlers:
            handler.setFormatter(JsonFormatter())


def configure_error_monitoring(dsn: str) -> None:
    if not dsn:
        return
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=dsn, traces_sample_rate=0.0)
    except Exception:
        logging.getLogger(__name__).warning('Sentry SDK not available; DSN configured but monitoring disabled.')


def request_id_middleware(app):
    """Register a before_request hook that generates a unique request ID.

    The ID is stored on flask.g and included in all structured log lines
    via JsonFormatter. It is also returned in the X-Request-ID response header.
    """
    @app.before_request
    def _set_request_id():
        # Accept client-provided request ID (e.g. from a load balancer) or generate one
        g.request_id = request.headers.get('X-Request-ID', str(uuid.uuid4())[:12])

    @app.after_request
    def _add_request_id_header(response):
        rid = getattr(g, 'request_id', None)
        if rid:
            response.headers['X-Request-ID'] = rid
        return response
