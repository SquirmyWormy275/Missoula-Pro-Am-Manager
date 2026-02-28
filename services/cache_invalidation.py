"""Centralized cache invalidation helpers for tournament view data."""
from services.report_cache import invalidate_prefix


def invalidate_tournament_caches(tournament_id: int) -> None:
    """Invalidate cached payloads affected by tournament data mutations."""
    tid = int(tournament_id)
    invalidate_prefix(f'reports:{tid}:')
    invalidate_prefix(f'portal:college:{tid}')
    invalidate_prefix(f'portal:pro:{tid}')
    invalidate_prefix(f'api:standings-poll:{tid}')
