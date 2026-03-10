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

from flask import Blueprint

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

    html = _render_status_html(
        configured=configured,
        last_push=last_push,
        last_count=last_count,
        global_count=global_count,
        global_count_error=global_count_error,
        skipped=skipped,
    )
    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}


# ---------------------------------------------------------------------------
# Minimal HTML renderer (no Jinja template — keeps this self-contained)
# ---------------------------------------------------------------------------

def _render_status_html(
    configured: bool,
    last_push: str,
    last_count: int,
    global_count: 'int | None',
    global_count_error: 'str | None',
    skipped: list,
) -> str:
    yes_no = lambda b: '<b style="color:green">Yes</b>' if b else '<b style="color:red">No</b>'

    if global_count is not None:
        count_str = str(global_count)
    elif global_count_error:
        count_str = f'<span style="color:red">Error: {_esc(global_count_error)}</span>'
    else:
        count_str = 'N/A (env vars not set)'

    skipped_rows = ''
    if skipped:
        rows = ''.join(
            f'<tr><td>{_esc(s.get("name",""))}</td>'
            f'<td>{_esc(s.get("event",""))}</td>'
            f'<td>{_esc(s.get("skipped_at",""))}</td></tr>'
            for s in skipped
        )
        skipped_rows = f'''
        <h2>Skipped College Competitors</h2>
        <p>These college competitors had no matching profile in the global STRATHMARK
        database.  Their results were not pushed.  Manually enroll them in STRATHMARK
        and re-finalize the event to capture their results.</p>
        <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
          <thead><tr><th>Name</th><th>Event</th><th>Skipped At (UTC)</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>'''
    else:
        skipped_rows = '<p>No skipped competitors.</p>'

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>STRATHMARK Sync Status</title>
  <style>
    body {{ font-family: monospace; max-width: 800px; margin: 2em auto; padding: 0 1em; }}
    h1   {{ border-bottom: 2px solid #333; padding-bottom: .3em; }}
    h2   {{ margin-top: 1.5em; }}
    table {{ font-size: .9em; }}
    td, th {{ padding: 4px 10px; }}
  </style>
</head>
<body>
  <h1>STRATHMARK Sync Status</h1>

  <h2>Configuration</h2>
  <table>
    <tr><td>Env vars configured</td><td>{yes_no(configured)}</td></tr>
    <tr><td>Last successful push</td><td>{_esc(str(last_push))}</td></tr>
    <tr><td>Records written in last push</td><td>{last_count}</td></tr>
    <tr><td>Results in global DB for <em>{_SHOW_NAME}</em></td><td>{count_str}</td></tr>
  </table>

  {skipped_rows}
</body>
</html>'''


def _esc(s: str) -> str:
    """Minimal HTML escaping for plain text values."""
    return (s
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;'))
