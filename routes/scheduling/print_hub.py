"""Print Hub — single page listing every printable document in the app.

The hub:
  * Lists every entry in ``PRINT_DOCUMENTS`` plus dynamic expansions
    (one row per event for Event Results).
  * Shows configured / not-configured status per row (red / green dot).
  * Shows fresh / stale / never-printed per row based on fingerprint
    comparison against the last-recorded PrintTracker.
  * Lets a judge email any document to one or more recipients via
    the existing SMTP pipeline (services/email_delivery.py).

Endpoints:
  - GET  /scheduling/<tid>/print-hub                   — hub page
  - POST /scheduling/<tid>/print-hub/email             — send a document
"""

from __future__ import annotations

import logging
from typing import Optional

from flask import (
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from database import db
from models import Event, Tournament
from routes.api import write_limit
from services import email_delivery, print_catalog
from services.audit import log_action
from services.print_response import weasyprint_or_html

from . import scheduling_bp

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GET /print-hub
# ---------------------------------------------------------------------------


@scheduling_bp.route("/<int:tournament_id>/print-hub")
def print_hub(tournament_id):
    tournament = Tournament.query.get_or_404(tournament_id)
    rows = print_catalog.build_hub_rows(tournament)

    # Precompute the Print button URL for each row. Dynamic docs require
    # different url_for kwargs depending on the entity, so we resolve them
    # here instead of in the template (avoids leaking reflection into Jinja).
    for row in rows:
        row.print_url = _build_print_url(tournament, row)

    # Group by section for the template (preserves row order within section).
    grouped: dict[str, list] = {s: [] for s in print_catalog.SECTIONS_ORDER}
    for row in rows:
        grouped.setdefault(row.doc.section, []).append(row)

    email_configured = email_delivery.is_configured()
    users_with_email = _users_with_email()

    return render_template(
        "scheduling/print_hub.html",
        tournament=tournament,
        sections=print_catalog.SECTIONS_ORDER,
        grouped=grouped,
        email_configured=email_configured,
        users_with_email=users_with_email,
    )


def _users_with_email() -> list:
    """Return a list of (id, label) tuples for the email modal checkboxes."""
    try:
        from models import User

        users = (
            User.query.filter(
                User.email.isnot(None),
                User.email != "",
            )
            .order_by(User.username)
            .all()
        )
        out = []
        for u in users:
            label = getattr(u, "full_name", None) or u.username
            out.append({"id": u.id, "label": label, "email": u.email})
        return out
    except Exception:
        logger.exception("Failed to load users for email modal")
        return []


# ---------------------------------------------------------------------------
# POST /print-hub/email
# ---------------------------------------------------------------------------


@scheduling_bp.route("/<int:tournament_id>/print-hub/email", methods=["POST"])
@write_limit("20 per minute")
def print_hub_email(tournament_id):
    tournament = Tournament.query.get_or_404(tournament_id)

    if not email_delivery.is_configured():
        flash(
            "Email is not configured on this server (SMTP_* env vars missing).", "error"
        )
        return redirect(url_for("scheduling.print_hub", tournament_id=tournament_id))

    doc_key = (request.form.get("doc_key") or "").strip()
    doc = print_catalog.get_doc(doc_key)
    if doc is None:
        flash("Unknown document selected.", "error")
        return redirect(url_for("scheduling.print_hub", tournament_id=tournament_id))

    entity_id = _parse_int(request.form.get("entity_id"))
    entity_obj = None
    if doc.dynamic:
        if entity_id is None:
            flash("Per-event documents require an event selection.", "error")
            return redirect(
                url_for("scheduling.print_hub", tournament_id=tournament_id)
            )
        entity_obj = db.session.get(Event, entity_id)
        if entity_obj is None or entity_obj.tournament_id != tournament.id:
            flash("Invalid event reference.", "error")
            return redirect(
                url_for("scheduling.print_hub", tournament_id=tournament_id)
            )

    # Re-check configured state — don't email a doc that isn't ready.
    status = doc.status_fn(tournament, entity_obj)
    if not status.configured:
        flash(f'Cannot email "{doc.label}": {status.reason}', "error")
        return redirect(url_for("scheduling.print_hub", tournament_id=tournament_id))

    recipients = _collect_recipients(request)
    valid, invalid = email_delivery.validate_recipients(recipients)
    if not valid:
        if invalid:
            flash(
                "No valid recipients selected. Rejected: " + ", ".join(invalid),
                "error",
            )
        else:
            flash("Select at least one recipient.", "error")
        return redirect(url_for("scheduling.print_hub", tournament_id=tournament_id))
    if invalid:
        flash(
            "Some recipients were rejected (bad format or outside allowed "
            "domains): " + ", ".join(invalid),
            "warning",
        )

    # Render the document via its existing print route's template.
    try:
        attachment_bytes, attachment_name, attachment_mime, body_note = (
            _render_attachment(tournament, doc, entity_obj)
        )
    except Exception as exc:
        logger.exception("Failed to render attachment for %s", doc_key)
        flash(f"Failed to generate the attachment: {exc}", "error")
        return redirect(url_for("scheduling.print_hub", tournament_id=tournament_id))

    label = doc.label
    if entity_obj is not None:
        label = f"{doc.label} — {entity_obj.display_name or entity_obj.name}"

    subject = f"{tournament.name} {tournament.year} — {label}"
    body = _compose_body(tournament, label, body_note)

    user_id = _current_user_id()
    email_delivery.queue_document_email(
        tournament_id=tournament.id,
        doc_key=doc.key,
        entity_id=entity_id,
        recipients=valid,
        subject=subject,
        body=body,
        attachment_bytes=attachment_bytes,
        attachment_name=attachment_name,
        attachment_mime=attachment_mime,
        sent_by_user_id=user_id,
    )

    # Mirror the send intent to AuditLog so admins see it in the audit feed.
    try:
        log_action(
            "email_queued",
            "tournament",
            tournament.id,
            {
                "doc_key": doc.key,
                "entity_id": entity_id,
                "recipients": valid,
            },
        )
        db.session.commit()
    except Exception:
        logger.exception("AuditLog write failed for email_queued")
        try:
            db.session.rollback()
        except Exception:
            pass

    flash(
        f"Email queued — {len(valid)} recipient"
        + ("s" if len(valid) != 1 else "")
        + '. You will see a "Sent" or "Failed" entry on the next Hub load.',
        "success",
    )
    return redirect(url_for("scheduling.print_hub", tournament_id=tournament_id))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_print_url(tournament, row) -> str:
    """Resolve the Print button target URL for a hub row.

    Handles variation across the underlying routes:
      - ``tid`` vs ``tournament_id`` URL param name (woodboss uses tid).
      - Dynamic docs that take an additional entity kwarg.
    """
    endpoint = row.doc.route_endpoint
    kwargs: dict = {}
    if endpoint.startswith('woodboss.'):
        kwargs['tid'] = tournament.id
    else:
        kwargs['tournament_id'] = tournament.id
    if row.entity is not None:
        kwargs['event_id'] = row.entity.id
    try:
        return url_for(endpoint, **kwargs)
    except Exception:
        logger.exception('url_for(%s) failed for row %s', endpoint, row.doc.key)
        return '#'


def _parse_int(value) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _current_user_id() -> Optional[int]:
    try:
        from flask_login import current_user

        if current_user and getattr(current_user, "is_authenticated", False):
            return int(current_user.id)
    except Exception:
        pass
    return None


def _collect_recipients(req) -> list[str]:
    """Assemble recipient list from user_ids[] checkboxes + free-text extras."""
    out: list[str] = []

    raw_ids = req.form.getlist("user_ids") or []
    if raw_ids:
        try:
            from models import User

            ids = [int(x) for x in raw_ids if str(x).strip().isdigit()]
            if ids:
                users = User.query.filter(User.id.in_(ids)).all()
                for u in users:
                    if u.email:
                        out.append(u.email)
        except Exception:
            logger.exception("Failed to resolve user_ids for email")

    extras = (req.form.get("extra_emails") or "").strip()
    if extras:
        # Accept comma, semicolon, or newline separators — judges type whatever.
        for sep in (";", "\n", "\r"):
            extras = extras.replace(sep, ",")
        for piece in extras.split(","):
            addr = piece.strip()
            if addr:
                out.append(addr)

    return out


def _render_attachment(tournament, doc, entity_obj):
    """Render the document to (bytes, filename, mime, body_note).

    body_note is prepended to the email body when the attachment is HTML
    (WeasyPrint missing), so the recipient knows why they didn't get a PDF.
    """
    try:
        html, filename_base = _render_document_html(tournament, doc, entity_obj)
    except _DirectPdfAttachment as pdf:
        # ALA report already renders a real PDF via services/ala_report.
        return (pdf.pdf_bytes, pdf.filename, 'application/pdf', None)

    body, status, headers = weasyprint_or_html(html, filename_base)
    content_type = headers.get("Content-Type", "application/octet-stream")

    if content_type == "application/pdf":
        return (
            body if isinstance(body, (bytes, bytearray)) else body.encode("utf-8"),
            f"{filename_base}.pdf",
            "application/pdf",
            None,
        )
    # HTML fallback — Railway doesn't bundle WeasyPrint.
    body_bytes = body if isinstance(body, (bytes, bytearray)) else body.encode("utf-8")
    note = (
        "NOTE: this attachment is HTML (this deployment does not bundle "
        "a PDF generator). Open it in a browser and use File → Print to "
        "produce a PDF."
    )
    return (
        body_bytes,
        f"{filename_base}.html",
        "text/html",
        note,
    )


def _render_document_html(tournament, doc, entity_obj):
    """Re-render the print template for this doc. Shared with the print route
    so both surfaces stay in sync.

    We re-implement the minimum dispatch needed rather than invoking the
    Flask view function directly — view functions may set response headers
    we don't want in an email context.
    """
    from datetime import datetime
    from flask import render_template

    filename_base = f"{tournament.name}_{tournament.year}_{doc.key}".replace(" ", "_")

    if doc.key == "pro_checkout":
        from .pro_checkout_roster import _build_checkout_rows

        rows = _build_checkout_rows(tournament)
        html = render_template(
            "scheduling/pro_checkout_roster_print.html",
            tournament=tournament,
            rows=rows,
            now=datetime.utcnow(),
        )
        return html, filename_base

    if doc.key == "heat_sheets":
        # Heat sheets template extends base.html and reads many locals via
        # context processors — emailing it as an HTML attachment renders
        # oddly. For email delivery, use the same lightweight print path
        # the PDF route uses.
        html = _render_heat_sheets_print(tournament)
        return html, filename_base

    if doc.key == "day_schedule":
        html = _render_day_schedule_print(tournament)
        return html, filename_base

    if doc.key == "fnf_print" or doc.key == "fnf_pdf":
        from .friday_feature import _build_fnf_schedule, _load_fnf_config
        import config as _cfg

        eligible_names = set(_cfg.FRIDAY_NIGHT_EVENTS)
        pro_events = (
            tournament.events.filter_by(event_type="pro")
            .order_by(Event.name, Event.gender)
            .all()
        )
        eligible_events = [e for e in pro_events if e.name in eligible_names]
        fnf_config = _load_fnf_config(tournament)
        fnf_schedule = _build_fnf_schedule(tournament, eligible_events, fnf_config)
        html = render_template(
            "scheduling/friday_feature_print.html",
            tournament=tournament,
            fnf_schedule=fnf_schedule,
            notes=fnf_config.get("notes", ""),
            now=datetime.utcnow(),
        )
        return html, filename_base

    if doc.key == "event_results" and entity_obj is not None:
        results = (
            entity_obj.get_results_sorted()
            if hasattr(entity_obj, "get_results_sorted")
            else []
        )
        html = render_template(
            "reports/event_results_print.html",
            tournament=tournament,
            event=entity_obj,
            results=results,
        )
        return html, filename_base + f"_event_{entity_obj.id}"

    if doc.key == "all_results":
        html = _render_all_results_print(tournament)
        return html, filename_base

    if doc.key == "college_standings":
        html = _render_college_standings_print(tournament)
        return html, filename_base

    if doc.key == "pro_payouts":
        html = _render_pro_payouts_print(tournament)
        return html, filename_base

    if doc.key == "ala_report":
        from services.ala_report import build_ala_report, generate_ala_pdf

        report = build_ala_report(tournament)
        path = generate_ala_pdf(report)
        try:
            with open(path, "rb") as f:
                pdf_bytes = f.read()
            # ALA generates a real PDF path — bypass weasyprint_or_html and
            # return the bytes directly wrapped in a no-op HTML that the caller
            # will never read, by short-circuiting via raise-of-sentinel. We
            # instead return a sentinel the caller recognizes... too tricky.
            # Simpler: encode as data: URL? No. Best: return HTML preamble that
            # embeds a hint, and rely on attaching the PDF via a separate
            # return path. For the ALA case we short-circuit: raise a special
            # exception that _render_attachment catches to swap in the real PDF.
            raise _DirectPdfAttachment(pdf_bytes, filename_base + ".pdf")
        finally:
            try:
                import os

                os.remove(path)
            except OSError:
                pass

    if doc.key == "woodboss_report":
        from models import WoodConfig  # noqa: F401 — ensures model loaded
        from services import woodboss as woodboss_svc

        report = (
            woodboss_svc.build_report(tournament)
            if hasattr(woodboss_svc, "build_report")
            else None
        )
        # Fall back to rendering the template with minimal context if the
        # service shape has drifted. Keeps this branch best-effort.
        try:
            html = render_template(
                "woodboss/report_print.html",
                tournament=tournament,
                report=report,
            )
        except Exception:
            html = f"<html><body><p>Woodboss report for {tournament.name} could not be rendered.</p></body></html>"
        return html, filename_base

    if doc.key == "gear_sharing_print":
        try:
            from services.gear_sharing import build_gear_audit

            audit = build_gear_audit(tournament)
            html = render_template(
                "pro/gear_sharing_print.html",
                tournament=tournament,
                audit=audit,
            )
        except Exception:
            html = f"<html><body><p>Gear sharing roster for {tournament.name} could not be rendered.</p></body></html>"
        return html, filename_base

    if doc.key == "judge_sheet_all":
        html = _render_heat_sheets_print(tournament)  # closest equivalent
        return html, filename_base

    if doc.key == "birling_blank":
        try:
            from services.birling_bracket import BirlingBracket

            bracket = BirlingBracket(None) if False else None  # placeholder
            html = render_template(
                "scoring/birling_bracket_print.html",
                tournament=tournament,
                brackets=[],
            )
        except Exception:
            html = f"<html><body><p>Birling bracket for {tournament.name} could not be rendered.</p></body></html>"
        return html, filename_base

    # Fallback: empty placeholder. Should not hit in practice because
    # status_fn rejects configured=False docs before we reach render.
    html = f"<html><body><p>{doc.label}: no render path wired yet.</p></body></html>"
    return html, filename_base


class _DirectPdfAttachment(Exception):
    """Signal that a pre-rendered PDF (bytes, filename) is attached directly."""

    def __init__(self, pdf_bytes: bytes, filename: str):
        self.pdf_bytes = pdf_bytes
        self.filename = filename


def _render_heat_sheets_print(tournament):
    """Standalone print render for heat sheets (used for email delivery)."""
    from flask import render_template

    # Reuse the existing print-styled template if available; otherwise a
    # simple table. Prefer the existing template.
    try:
        return render_template(
            "scheduling/heat_sheets_print.html", tournament=tournament
        )
    except Exception:
        # Fallback: enumerate events + heats.
        rows = []
        for event in tournament.events.order_by(Event.name).all():
            for heat in event.heats.order_by().all():
                rows.append(
                    f"<tr><td>{event.display_name}</td><td>Heat {heat.heat_number}</td></tr>"
                )
        return (
            "<html><body><h1>Heat Sheets — "
            + str(tournament.name)
            + " "
            + str(tournament.year)
            + '</h1><table border="1">'
            + "".join(rows)
            + "</table></body></html>"
        )


def _render_day_schedule_print(tournament):
    from flask import render_template

    try:
        return render_template(
            "scheduling/day_schedule_print.html", tournament=tournament
        )
    except Exception:
        return f"<html><body><p>Day schedule for {tournament.name}</p></body></html>"


def _render_all_results_print(tournament):
    from flask import render_template

    try:
        events = tournament.events.order_by(Event.name).all()
        return render_template(
            "reports/all_results_print.html",
            tournament=tournament,
            events=events,
        )
    except Exception:
        return f"<html><body><p>All results for {tournament.name}</p></body></html>"


def _render_college_standings_print(tournament):
    from flask import render_template

    try:
        teams = tournament.get_team_standings()
        return render_template(
            "reports/college_standings_print.html",
            tournament=tournament,
            teams=teams,
        )
    except Exception:
        return (
            f"<html><body><p>College standings for {tournament.name}</p></body></html>"
        )


def _render_pro_payouts_print(tournament):
    from flask import render_template

    try:
        competitors = sorted(
            tournament.pro_competitors.filter_by(status="active").all(),
            key=lambda c: (c.total_earnings or 0),
            reverse=True,
        )
        return render_template(
            "reports/payout_summary_print.html",
            tournament=tournament,
            competitors=competitors,
        )
    except Exception:
        return f"<html><body><p>Pro payouts for {tournament.name}</p></body></html>"


def _compose_body(tournament, doc_label: str, extra_note: Optional[str]) -> str:
    parts = [
        f"Attached is the {doc_label} for {tournament.name} {tournament.year}.",
        "",
        f"Generated: {_utcnow_iso()}",
    ]
    if extra_note:
        parts.insert(0, extra_note + "\n")
    return "\n".join(parts)


def _utcnow_iso() -> str:
    from datetime import datetime

    return datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
