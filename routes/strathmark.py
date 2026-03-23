"""
STRATHMARK sync status page — Change 4.

Route:  GET /strathmark/status

Returns a minimal HTML page showing:
  - Whether STRATHMARK env vars are configured
  - Last successful push timestamp (from local cache)
  - Count of results in the global STRATHMARK database for this show
  - List of college competitor names skipped during result pushes

No authentication is required — this page is intended for localhost use by
the show director only.  Do not expose it on a public network without adding
auth.
"""
from __future__ import annotations

from flask import Blueprint, render_template

strathmark_bp = Blueprint('strathmark', __name__)

_SHOW_NAME = 'Missoula Pro-Am'


@strathmark_bp.route('/status')
def status():
    """Render the STRATHMARK sync status page."""
    from services.strathmark_sync import (
        is_configured,
        read_sync_cache,
        get_skipped_competitors,
    )

    configured = is_configured()

    # Last push info from local cache
    cache = read_sync_cache()
    last_push = cache.get('last_push_timestamp', 'Never')
    last_count = cache.get('last_push_count', 0)

    # Result count in global STRATHMARK for this show
    global_count = None
    global_count_error = None
    if configured:
        try:
            from strathmark import pull_results
            df = pull_results()
            if not df.empty and 'show_name' in df.columns:
                global_count = int((df['show_name'] == _SHOW_NAME).sum())
            else:
                global_count = 0
        except Exception as exc:
            global_count_error = str(exc)

    # Skipped college competitors
    skipped = get_skipped_competitors()

    return render_template(
        'strathmark/status.html',
        configured=configured,
        last_push=last_push,
        last_count=last_count,
        global_count=global_count,
        global_count_error=global_count_error,
        skipped=skipped,
        show_name=_SHOW_NAME,
    )
