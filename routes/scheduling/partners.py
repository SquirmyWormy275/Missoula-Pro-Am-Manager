"""
Partner reassignment queue — orphaned partner detection and reassignment.

Routes:
  GET  /<tid>/events/<eid>/partner-queue     — list orphaned partners
  POST /<tid>/events/<eid>/reassign-partner  — assign new partner
"""

import json
import logging

from flask import abort, flash, redirect, render_template, request, url_for

from database import db
from models.competitor import CollegeCompetitor, ProCompetitor
from models.event import Event, EventResult
from services.audit import log_action

from . import _competitor_entered_event, scheduling_bp

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers (importable by tests)
# ---------------------------------------------------------------------------


def _load_competitor(comp_id, comp_type):
    """Load a competitor by ID and type string ('pro' or 'college')."""
    Model = ProCompetitor if comp_type == "pro" else CollegeCompetitor
    return db.session.get(Model, comp_id)


def get_orphaned_competitors(event):
    """
    Return list of dicts describing orphaned competitors for a partnered event.

    A competitor is orphaned when they have an active EventResult whose
    partner_name references a competitor with status == 'scratched'.

    Returns:
        [{'competitor': <Competitor>, 'old_partner_name': str, 'result': <EventResult>}, ...]
    """
    results = EventResult.query.filter(
        EventResult.event_id == event.id,
        EventResult.partner_name.isnot(None),
        EventResult.partner_name != "",
        EventResult.status.in_(["pending", "completed"]),
    ).all()

    orphans = []
    seen = set()

    for r in results:
        # Look up the referenced partner by name in this tournament
        partner_name = r.partner_name
        partner = _find_competitor_by_name(
            partner_name, event.tournament_id, r.competitor_type
        )

        if partner and partner.status == "scratched" and r.competitor_id not in seen:
            comp = _load_competitor(r.competitor_id, r.competitor_type)
            if comp and comp.status == "active":
                orphans.append(
                    {
                        "competitor": comp,
                        "competitor_type": r.competitor_type,
                        "old_partner_name": partner_name,
                        "result": r,
                    }
                )
                seen.add(r.competitor_id)

    return orphans


def _find_competitor_by_name(name, tournament_id, comp_type):
    """Find a competitor by name in a tournament."""
    Model = ProCompetitor if comp_type == "pro" else CollegeCompetitor
    return Model.query.filter_by(
        tournament_id=tournament_id,
        name=name,
    ).first()


def _is_entered_in_event(competitor, event):
    """Return whether a competitor is currently entered in an event."""
    entered = (
        competitor.get_events_entered()
        if hasattr(competitor, "get_events_entered")
        else []
    )
    return _competitor_entered_event(event, entered)


def _is_current_orphan(event, competitor, competitor_type):
    """Return whether the competitor still has a scratched event partner."""
    result = EventResult.query.filter_by(
        event_id=event.id,
        competitor_id=competitor.id,
        competitor_type=competitor_type,
    ).filter(EventResult.status.in_(["pending", "completed"])).first()
    if not result or not result.partner_name:
        return False

    previous_partner = _find_competitor_by_name(
        result.partner_name,
        event.tournament_id,
        competitor_type,
    )
    return bool(previous_partner and previous_partner.status == "scratched")


def validate_reassignment_context(event, orphan, new_partner):
    """Validate current roster eligibility for a reassignment submission."""
    expected_type = event.event_type
    orphan_type = "pro" if isinstance(orphan, ProCompetitor) else "college"
    new_type = "pro" if isinstance(new_partner, ProCompetitor) else "college"
    if orphan_type != expected_type or new_type != expected_type:
        return False, "Partners must be in the event's competition division."
    if orphan.id == new_partner.id:
        return False, "A competitor cannot be assigned as their own partner."
    if (
        orphan.tournament_id != event.tournament_id
        or new_partner.tournament_id != event.tournament_id
    ):
        return False, "Both competitors must belong to this tournament."
    if orphan.status != "active" or new_partner.status != "active":
        return False, "Both competitors must be active to form a partnership."
    if not _is_entered_in_event(orphan, event) or not _is_entered_in_event(
        new_partner, event
    ):
        return False, "Both competitors must be entered in this event."
    if not _is_current_orphan(event, orphan, orphan_type):
        return False, "This competitor no longer needs a partner for this event."

    return True, None


def validate_reassignment(event, orphan, new_partner):
    """
    Validate that new_partner is a compatible partner for orphan in event.

    Current roster eligibility is validated separately at the POST boundary.
    This helper remains usable for compatibility checks before a queue is shown.

    Returns:
        (ok: bool, error: str|None)
    """

    # Check gender requirement
    gender_req = getattr(event, "partner_gender_requirement", "any")

    if gender_req == "mixed":
        if orphan.gender == new_partner.gender:
            return (
                False,
                f"Mixed-gender event requires opposite gender. Both are {orphan.gender}.",
            )
    elif gender_req == "same":
        if orphan.gender != new_partner.gender:
            return (
                False,
                f"Same-gender event requires matching gender. {orphan.gender} vs {new_partner.gender}.",
            )

    # Check not already partnered for this event
    existing_partners = new_partner.get_partners()
    existing_name = existing_partners.get(str(event.id))
    if existing_name:
        return (
            False,
            f"{new_partner.name} already has a partner ({existing_name}) for this event.",
        )

    return True, None


def set_partner_bidirectional(orphan, new_partner, event):
    """
    Set partner JSON on both competitors and update EventResult.partner_name.
    """
    # Update partner JSON on both sides
    orphan.set_partner(event.id, new_partner.name)
    new_partner.set_partner(event.id, orphan.name)

    # Update EventResult.partner_name for the orphan
    orphan_type = "pro" if isinstance(orphan, ProCompetitor) else "college"
    result = EventResult.query.filter_by(
        event_id=event.id,
        competitor_id=orphan.id,
        competitor_type=orphan_type,
    ).first()
    if result:
        result.partner_name = new_partner.name

    # Update or create EventResult.partner_name for the new partner
    new_type = "pro" if isinstance(new_partner, ProCompetitor) else "college"
    new_result = EventResult.query.filter_by(
        event_id=event.id,
        competitor_id=new_partner.id,
        competitor_type=new_type,
    ).first()
    if new_result:
        new_result.partner_name = orphan.name


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@scheduling_bp.route("/<int:tid>/events/<int:eid>/partner-queue")
def partner_queue(tid, eid):
    """GET: Show orphaned partners needing reassignment."""
    event = db.get_or_404(Event, eid)
    if event.tournament_id != tid:
        abort(404)
    if not getattr(event, "is_partnered", False):
        abort(404)

    tournament = event.tournament
    orphans = get_orphaned_competitors(event)

    # Build list of available partners (active, not already partnered for this event)
    comp_type = event.event_type  # 'pro' or 'college'
    Model = ProCompetitor if comp_type == "pro" else CollegeCompetitor
    all_active = Model.query.filter_by(
        tournament_id=tid,
        status="active",
    ).all()

    # Filter to those not already partnered for this event
    available = []
    orphan_ids = {o["competitor"].id for o in orphans}
    for c in all_active:
        if c.id in orphan_ids or not _is_entered_in_event(c, event):
            continue
        partners = c.get_partners()
        existing = partners.get(str(eid))
        if existing:
            continue
        available.append(c)

    return render_template(
        "scheduling/partner_queue.html",
        tournament=tournament,
        event=event,
        orphans=orphans,
        available=available,
    )


@scheduling_bp.route("/<int:tid>/events/<int:eid>/reassign-partner", methods=["POST"])
def reassign_partner(tid, eid):
    """POST: Assign a new partner to an orphaned competitor."""
    event = db.get_or_404(Event, eid)
    if event.tournament_id != tid:
        abort(404)

    orphan_id = request.form.get("orphan_id", type=int)
    orphan_type = request.form.get("orphan_type", "pro")
    new_partner_id = request.form.get("new_partner_id", type=int)
    new_partner_type = request.form.get("new_partner_type", "pro")

    if not orphan_id or not new_partner_id:
        flash("Missing competitor selection.", "error")
        return redirect(url_for("scheduling.partner_queue", tid=tid, eid=eid))

    if orphan_type != event.event_type or new_partner_type != event.event_type:
        flash("Partners must be in the event's competition division.", "error")
        return redirect(url_for("scheduling.partner_queue", tid=tid, eid=eid))

    orphan = _load_competitor(orphan_id, orphan_type)
    new_partner = _load_competitor(new_partner_id, new_partner_type)

    if not orphan or not new_partner:
        flash("Competitor not found.", "error")
        return redirect(url_for("scheduling.partner_queue", tid=tid, eid=eid))

    # Check pairing compatibility before the current-roster error so an
    # operator gets the specific correction for a visibly invalid selection.
    ok, error = validate_reassignment(event, orphan, new_partner)
    if not ok:
        flash(error, "error")
        return redirect(url_for("scheduling.partner_queue", tid=tid, eid=eid))

    ok, error = validate_reassignment_context(event, orphan, new_partner)
    if not ok:
        flash(error, "error")
        return redirect(url_for("scheduling.partner_queue", tid=tid, eid=eid))

    # Apply bidirectional update
    previous_partner_name = orphan.get_partners().get(str(event.id))
    set_partner_bidirectional(orphan, new_partner, event)
    db.session.commit()
    log_action(
        "partner_reassigned",
        "event",
        event.id,
        {
            "tournament_id": tid,
            "event_id": event.id,
            "event_name": event.display_name,
            "orphan_id": orphan.id,
            "orphan_type": orphan_type,
            "previous_partner_name": previous_partner_name,
            "new_partner_id": new_partner.id,
            "new_partner_type": new_partner_type,
        },
    )

    flash(f"Reassigned {orphan.name} with new partner {new_partner.name}.", "success")
    logger.info(
        "Partner reassignment: %s → %s for event %s (tid=%d)",
        orphan.name,
        new_partner.name,
        event.name,
        tid,
    )

    return redirect(url_for("scheduling.partner_queue", tid=tid, eid=eid))
