"""TTL cache for report payloads.

Storage strategy (in priority order):
  1. In-process memory dict — always used as L1 for speed.
  2. Disk shelve in instance/report_cache/ — used as L2 so that cache entries
     survive gunicorn worker recycling and Railway redeploys.  The disk layer
     is skipped gracefully if the instance directory is not writable.
"""
from __future__ import annotations

import logging
import os
import shelve
import threading
import time

logger = logging.getLogger(__name__)

_cache: dict = {}
_lock = threading.Lock()

# Resolved once on first use; None means disk layer is unavailable.
_shelf_path: str | None = None
_shelf_resolved = False


def _get_shelf_path() -> str | None:
    """Return the path prefix for the shelve file, or None if unavailable."""
    global _shelf_path, _shelf_resolved
    if _shelf_resolved:
        return _shelf_path
    _shelf_resolved = True
    try:
        # Walk up from this file to find the instance/ directory.
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cache_dir = os.path.join(base, 'instance', 'report_cache')
        os.makedirs(cache_dir, exist_ok=True)
        _shelf_path = os.path.join(cache_dir, 'cache')
    except Exception as exc:
        logger.debug('Disk cache unavailable: %s', exc)
        _shelf_path = None
    return _shelf_path


def _shelf_get(key: str):
    path = _get_shelf_path()
    if not path:
        return None
    try:
        with shelve.open(path, flag='c') as shelf:
            item = shelf.get(key)
        if not item:
            return None
        if item['expires_at'] < time.time():
            _shelf_delete(key)
            return None
        return item['value']
    except Exception as exc:
        logger.debug('Disk cache read error for %s: %s', key, exc)
        return None


def _shelf_set(key: str, value, expires_at: float) -> None:
    path = _get_shelf_path()
    if not path:
        return
    try:
        with shelve.open(path, flag='c') as shelf:
            shelf[key] = {'value': value, 'expires_at': expires_at}
    except Exception as exc:
        logger.debug('Disk cache write error for %s: %s', key, exc)


def _shelf_delete(key: str) -> None:
    path = _get_shelf_path()
    if not path:
        return
    try:
        with shelve.open(path, flag='c') as shelf:
            shelf.pop(key, None)
    except Exception:
        pass


def _shelf_delete_prefix(prefix: str) -> None:
    path = _get_shelf_path()
    if not path:
        return
    try:
        with shelve.open(path, flag='c') as shelf:
            doomed = [k for k in shelf.keys() if k.startswith(prefix)]
            for k in doomed:
                del shelf[k]
    except Exception as exc:
        logger.debug('Disk cache prefix-delete error: %s', exc)


def get(key: str):
    now = time.time()
    with _lock:
        item = _cache.get(key)
        if item:
            if item['expires_at'] >= now:
                return item['value']
            _cache.pop(key, None)

    # L2: disk
    value = _shelf_get(key)
    if value is not None:
        # Warm L1 — TTL already validated by _shelf_get.
        with _lock:
            _cache[key] = {'value': value, 'expires_at': now + 60}
    return value


def set(key: str, value, ttl_seconds: int) -> None:
    ttl_seconds = max(1, int(ttl_seconds))
    expires_at = time.time() + ttl_seconds
    with _lock:
        _cache[key] = {'value': value, 'expires_at': expires_at}
    _shelf_set(key, value, expires_at)


def invalidate_prefix(prefix: str) -> None:
    with _lock:
        doomed = [k for k in _cache.keys() if k.startswith(prefix)]
        for key in doomed:
            _cache.pop(key, None)
    _shelf_delete_prefix(prefix)

