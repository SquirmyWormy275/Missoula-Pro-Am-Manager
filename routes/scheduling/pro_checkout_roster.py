"""Pro Saturday Checkout Roster — printable roster with per-pro checkbox + notes.

Used by the lead judge on Saturday morning to tick off pros as they arrive.
Renders the same template for both the HTML print view and the PDF download
via services.print_response.weasyprint_or_html (PDF when WeasyPrint is
installed, HTML fallback otherwise).
"""

from __future__ import annotations

from datetime import datetime

from flask import render_template

from models import Event, Tournament
from services.print_catalog import record_print
from services.print_response import weasyprint_or_html

from . import scheduling_bp


def _build_checkout_rows(tournament: Tournament) -> list[dict]:
    """One row per active pro competitor, alphabetical by name.

    Events column resolves every entry in `events_entered` (which stores
    event NAMES today, not IDs — see CLAUDE.md §4) back to an Event
    display_name so the roster matches what the judge sees on entry forms.
    """
    pros = tournament.pro_competitors.filter_by(status="active").all()
    pros = sorted(pros, key=lambda p: (p.name or "").lower())

    events = tournament.events.filter_by(event_type="pro").all()
    by_id = {str(e.id): e for e in events}
    by_name = {(e.name or "").strip().lower(): e for e in events}
    by_display = {(e.display_name or "").strip().lower(): e for e in events}

    rows = []
    for pro in pros:
        entered_raw = (
            pro.get_events_entered() if hasattr(pro, "get_events_entered") else []
        )
        labels = []
        for raw in entered_raw or []:
            key = str(raw).strip()
            if not key:
                continue
            event = (
                by_id.get(key)
                or by_name.get(key.lower())
                or by_display.get(key.lower())
            )
            if event is not None:
                labels.append(event.display_name or event.name)
            else:
                # Fall back to raw text so the judge sees *something* rather
                # than dropping the entry silently.
                labels.append(key)
        rows.append(
            {
                "id": pro.id,
                "name": pro.name,
                "events": sorted(labels, key=str.lower),
            }
        )
    return rows


@scheduling_bp.route("/<int:tournament_id>/pro/checkout-roster/print")
@record_print("pro_checkout")
def pro_checkout_roster_print(tournament_id):
    """HTML print view — judge loads in browser and Ctrl-P."""
    tournament = Tournament.query.get_or_404(tournament_id)
    rows = _build_checkout_rows(tournament)
    return render_template(
        "scheduling/pro_checkout_roster_print.html",
        tournament=tournament,
        rows=rows,
        now=datetime.utcnow(),
    )


@scheduling_bp.route("/<int:tournament_id>/pro/checkout-roster/pdf")
@record_print("pro_checkout")
def pro_checkout_roster_pdf(tournament_id):
    """PDF download (WeasyPrint if installed, HTML fallback on Railway)."""
    tournament = Tournament.query.get_or_404(tournament_id)
    rows = _build_checkout_rows(tournament)
    html = render_template(
        "scheduling/pro_checkout_roster_print.html",
        tournament=tournament,
        rows=rows,
        now=datetime.utcnow(),
    )
    filename = f"{tournament.name}_{tournament.year}_pro_checkout_roster".replace(
        " ", "_"
    )
    body, status, headers = weasyprint_or_html(html, filename)
    return body, status, headers
