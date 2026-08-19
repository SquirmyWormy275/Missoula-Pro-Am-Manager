"""TTL cache for report payloads.

Storage strategy (in priority order):
  1. In-process memory dict for SQLite/local operation.
  2. No cross-process disk layer. Report invalidation generations are
     process-local, so sharing a shelf across overlapping boots would let an
     old process repopulate stale data after a new process invalidates it.
  3. PostgreSQL deployments bypass this cache. A rolling deployment can have
     two live processes, and a write in one cannot invalidate memory in the
     other without a shared generation store.

Callers should still hand this module plain data. Process-local mapped entities
can expire with their SQLAlchemy session, so caching them is allowed only in L1
for backward compatibility and emits a warning.
"""
from __future__ import annotations

import builtins
import logging
import threading
import time
import weakref
from collections.abc import Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

_cache: dict = {}
_lock = threading.Lock()
_fill_locks: weakref.WeakValueDictionary[str, threading.Lock] = (
    weakref.WeakValueDictionary()
)
_T = TypeVar('_T')

# A cache miss records the generation for that key in the reading thread. If
# an invalidation affecting the key lands before the reader calls set(), the
# stale fill is discarded. Generations are prefix-specific so an update to one
# tournament does not suppress a concurrent fill for another tournament.
_generation_counter = 0
_prefix_generations: dict[str, int] = {}
_read_state = threading.local()

# Resolved once on first use; None means disk layer is unavailable.
_shelf_path: str | None = None
_shelf_resolved = False


def _cache_enabled() -> bool:
    try:
        from flask import current_app, has_app_context
    except ModuleNotFoundError:
        return True
    if not has_app_context():
        return True
    uri = str(current_app.config.get('SQLALCHEMY_DATABASE_URI', '')).lower()
    return not (uri.startswith('postgresql://') or uri.startswith('postgres://'))


def _matches_prefix(key: str, prefix: str) -> bool:
    """Return whether ``key`` belongs to a delimited cache namespace.

    A trailing colon is the tournament boundary. It matches both the namespace
    root (for example ``portal:college:1``) and children below it, but never a
    larger tournament ID such as ``portal:college:10``.
    """
    if not prefix:
        return True
    if prefix.endswith(':'):
        return key == prefix[:-1] or key.startswith(prefix)
    return key.startswith(prefix)


def _generation_for_key_locked(key: str) -> int:
    return max(
        (
            generation
            for prefix, generation in _prefix_generations.items()
            if _matches_prefix(key, prefix)
        ),
        default=0,
    )


def _miss_generations() -> dict[str, int]:
    misses = getattr(_read_state, 'misses', None)
    if misses is None:
        misses = {}
        _read_state.misses = misses
    return misses


def _get_shelf_path() -> str | None:
    """Return ``None`` because cache generations are process-local."""
    global _shelf_path, _shelf_resolved
    _shelf_resolved = True
    _shelf_path = None
    return None


def _shelf_get(key: str):
    return None


def _shelf_set(key: str, value, expires_at: float) -> None:
    return None


def _shelf_delete(key: str) -> None:
    return None


def _shelf_delete_prefix(prefix: str) -> None:
    return None


# Deep enough for a report payload (dict -> list -> row dict -> entity), and
# bounded so a value that nests unexpectedly cannot turn a cache write into a
# recursion error.
_MAX_SCAN_DEPTH = 6

# builtins.set, not set: this module defines a public function called ``set``
# that shadows the builtin at module scope, so a bare ``set`` in an isinstance
# check below resolves to that function and raises TypeError.
_CONTAINER_TYPES = (list, tuple, builtins.set, frozenset)


def _contains_orm_entity(value, depth: int = 0) -> bool:
    """True if value is, or contains, a SQLAlchemy-mapped instance.

    Every mapped instance carries ``_sa_instance_state``; nothing else in a
    report payload does. Strings and bytes are not treated as containers, so
    this never walks into a character.
    """
    if hasattr(value, '_sa_instance_state'):
        return True
    if depth >= _MAX_SCAN_DEPTH:
        return False
    if isinstance(value, dict):
        return any(
            _contains_orm_entity(k, depth + 1) or _contains_orm_entity(v, depth + 1)
            for k, v in value.items()
        )
    if isinstance(value, _CONTAINER_TYPES):
        return any(_contains_orm_entity(item, depth + 1) for item in value)
    return False


def get(key: str):
    if not _cache_enabled():
        return None
    now = time.time()
    with _lock:
        generation = _generation_for_key_locked(key)
        item = _cache.get(key)
        if item:
            if item['expires_at'] >= now:
                _miss_generations().pop(key, None)
                return item['value']
            _cache.pop(key, None)

        # Keep disk access serialized with invalidation. A reader that starts
        # after an invalidation cannot observe the pre-delete shelf entry.
        value = _shelf_get(key)
        if value is not None:
            # Warm L1 — TTL already validated by _shelf_get.
            _cache[key] = {'value': value, 'expires_at': now + 60}
            _miss_generations().pop(key, None)
            return value

        _miss_generations()[key] = generation
        return None


def set(key: str, value, ttl_seconds: int) -> None:
    if not _cache_enabled():
        return
    ttl_seconds = max(1, int(ttl_seconds))
    expires_at = time.time() + ttl_seconds
    contains_orm_entity = _contains_orm_entity(value)
    miss_generation = _miss_generations().pop(key, None)
    with _lock:
        if (
            miss_generation is not None
            and miss_generation != _generation_for_key_locked(key)
        ):
            logger.debug('Discarding stale cache fill for %s after invalidation.', key)
            return
        _cache[key] = {'value': value, 'expires_at': expires_at}
        if contains_orm_entity:
            logger.warning(
                'Cache payload for %s contains SQLAlchemy entities. Serialize '
                'to plain data so cached values do not outlive their session.', key)
            return
        _shelf_set(key, value, expires_at)


def get_or_compute(key: str, ttl_seconds: int, builder: Callable[[], _T]) -> _T:
    """Return a cached value, coalescing concurrent fills for one key.

    Public standings can receive a cold burst after a local restart. Without a
    per-key fill lock, every reader repeats the same queries before the first
    result reaches the cache. Different keys retain independent locks.
    """
    if not _cache_enabled():
        return builder()

    cached = get(key)
    if cached is not None:
        return cached

    with _lock:
        fill_lock = _fill_locks.setdefault(key, threading.Lock())

    with fill_lock:
        cached = get(key)
        if cached is not None:
            return cached
        value = builder()
        set(key, value, ttl_seconds)
        return value


def reset_for_testing() -> None:
    """Reset process-local state between isolated test applications."""
    global _generation_counter, _shelf_path, _shelf_resolved
    with _lock:
        _cache.clear()
        _fill_locks.clear()
        _prefix_generations.clear()
        _generation_counter = 0
    _read_state.misses = {}
    _shelf_path = None
    _shelf_resolved = True


def invalidate_prefix(prefix: str) -> None:
    global _generation_counter
    with _lock:
        _generation_counter += 1
        _prefix_generations[prefix] = _generation_counter
        doomed = [k for k in _cache.keys() if _matches_prefix(k, prefix)]
        for key in doomed:
            _cache.pop(key, None)
        _shelf_delete_prefix(prefix)

