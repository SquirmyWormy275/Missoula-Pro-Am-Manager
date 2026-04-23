"""Sidebar aggregator helpers — small, cached counts surfaced in the
collapsible tournament sidebar (templates/_sidebar.html).

Lives here so app.py's inject_strings context processor doesn't carry DB
query logic — CLAUDE.md §6 says "Do not create new database logic in
app.py — all DB logic belongs in models/ or services/". The previous
inline Heat.query.join(...) inside the context processor coupled app.py
to ORM internals and ran on every templated request.
"""

from __future__ import annotations


def unscored_heats_count(tournament_id: int) -> int:
    """Count pending heats for a tournament (sidebar badge data).

    Returns 0 on any failure — context-processor callers must never raise.
    The query joins Heat → Event so heats with the wrong tournament_id are
    excluded; the join also defends against stale Heat rows whose Event
    was deleted (left-join semantics aren't needed here, just count).
    """
    if not tournament_id:
        return 0
    try:
        from models import Event, Heat

        return (
            Heat.query.join(Event, Heat.event_id == Event.id)
            .filter(Event.tournament_id == int(tournament_id), Heat.status == "pending")
            .count()
        )
    except Exception:
        return 0
