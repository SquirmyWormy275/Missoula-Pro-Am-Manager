"""Simple TTL cache for report payloads."""
from __future__ import annotations

import time
import threading


_cache = {}
_lock = threading.Lock()


def get(key: str):
    now = time.time()
    with _lock:
        item = _cache.get(key)
        if not item:
            return None
        if item['expires_at'] < now:
            _cache.pop(key, None)
            return None
        return item['value']


def set(key: str, value, ttl_seconds: int) -> None:
    ttl_seconds = max(1, int(ttl_seconds))
    with _lock:
        _cache[key] = {
            'value': value,
            'expires_at': time.time() + ttl_seconds,
        }


def invalidate_prefix(prefix: str) -> None:
    with _lock:
        doomed = [k for k in _cache.keys() if k.startswith(prefix)]
        for key in doomed:
            _cache.pop(key, None)

