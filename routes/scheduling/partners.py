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

from . import scheduling_bp

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers (importable by tests)
# ---------------------------------------------------------------------------


def _load_competitor(comp_id, comp_type):
    """Load a competitor by ID and type string ('pro' or 'college')."""
    Model = ProCompetitor if comp_type == "pro" else CollegeCompetitor
    return Model.query.get(comp_id)


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


def validate_reassignment(event, orphan, new_partner):
    """
    Validate that new_partner is a valid reassignment for orphan in event.

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
        # Check if that existing partner is scratched (would make this one also orphaned)
        existing_partner_comp = _find_competitor_by_name(
            existing_name,
            event.tournament_id,
            "pro" if isinstance(new_partner, ProCompetitor) else "college",
        )
        if not existing_partner_comp or existing_partner_comp.status != "scratched":
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
    event = Event.query.get_or_404(eid)
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
        if c.id in orphan_ids:
            continue
        partners = c.get_partners()
        existing = partners.get(str(eid))
        if existing:
            # Check if their existing partner is scratched (they'd be orphaned too)
            ep = _find_competitor_by_name(existing, tid, comp_type)
            if ep and ep.status != "scratched":
                continue  # has active partner, skip
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
    event = Event.query.get_or_404(eid)
    if event.tournament_id != tid:
        abort(404)

    orphan_id = request.form.get("orphan_id", type=int)
    orphan_type = request.form.get("orphan_type", "pro")
    new_partner_id = request.form.get("new_partner_id", type=int)
    new_partner_type = request.form.get("new_partner_type", "pro")

    if not orphan_id or not new_partner_id:
        flash("Missing competitor selection.", "error")
        return redirect(url_for("scheduling.partner_queue", tid=tid, eid=eid))

    orphan = _load_competitor(orphan_id, orphan_type)
    new_partner = _load_competitor(new_partner_id, new_partner_type)

    if not orphan or not new_partner:
        flash("Competitor not found.", "error")
        return redirect(url_for("scheduling.partner_queue", tid=tid, eid=eid))

    # Validate
    ok, error = validate_reassignment(event, orphan, new_partner)
    if not ok:
        flash(error, "error")
        return redirect(url_for("scheduling.partner_queue", tid=tid, eid=eid))

    # Apply bidirectional update
    set_partner_bidirectional(orphan, new_partner, event)
    db.session.commit()

    flash(f"Reassigned {orphan.name} with new partner {new_partner.name}.", "success")
    logger.info(
        "Partner reassignment: %s → %s for event %s (tid=%d)",
        orphan.name,
        new_partner.name,
        event.name,
        tid,
    )

    return redirect(url_for("scheduling.partner_queue", tid=tid, eid=eid))
