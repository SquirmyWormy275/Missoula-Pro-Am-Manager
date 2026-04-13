"""
Scratch Cascade Service — pure computation of downstream effects.

``compute_scratch_effects`` examines a competitor and returns a list of
CascadeEffect dataclasses describing every row that would be touched by a
scratch operation.  No DB writes are performed here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models.competitor import CollegeCompetitor, ProCompetitor
    from models.tournament import Tournament

logger = logging.getLogger(__name__)

SCRATCH_UNDO_WINDOW_MINUTES = 30


@dataclass
class CascadeEffect:
    effect_type: str  # 'event_result' | 'partner' | 'relay_team' | 'standings'
    description: str  # human-readable summary for preview modal
    affected_entity_id: int  # PK of the affected row
    affected_entity_type: str  # 'event_result' | 'competitor' | 'event'
    metadata: dict = field(default_factory=dict)


# Statuses that represent an active (non-terminal) entry.
_ACTIVE_STATUSES = {"pending", "completed"}


def compute_scratch_effects(competitor, tournament) -> list[CascadeEffect]:
    """Return all downstream CascadeEffects of scratching *competitor*.

    Pure computation — no session writes.

    Args:
        competitor: ProCompetitor or CollegeCompetitor instance.
        tournament: Tournament instance.

    Returns:
        List[CascadeEffect]

    Raises:
        ValueError: if competitor.tournament_id != tournament.id (IDOR guard).
    """
    # --- IDOR guard ----------------------------------------------------------
    if competitor.tournament_id != tournament.id:
        raise ValueError(
            f"competitor.tournament_id={competitor.tournament_id} does not match "
            f"tournament.id={tournament.id}"
        )

    from models.competitor import CollegeCompetitor, ProCompetitor
    from models.event import Event, EventResult

    is_college = isinstance(competitor, CollegeCompetitor)
    comp_type = "college" if is_college else "pro"

    effects: list[CascadeEffect] = []

    # --- 1. EventResult rows -------------------------------------------------
    active_results = EventResult.query.filter(
        EventResult.competitor_id == competitor.id,
        EventResult.competitor_type == comp_type,
        EventResult.status.in_(_ACTIVE_STATUSES),
    ).all()

    # Preload events for the affected results (avoid repeated per-row queries).
    event_ids = {r.event_id for r in active_results}
    events_by_id: dict[int, Event] = {}
    if event_ids:
        for ev in Event.query.filter(Event.id.in_(event_ids)).all():
            events_by_id[ev.id] = ev

    finalized_event_ids: set[int] = set()

    for result in active_results:
        ev = events_by_id.get(result.event_id)
        event_name = ev.name if ev else f"Event #{result.event_id}"

        effects.append(
            CascadeEffect(
                effect_type="event_result",
                description=f"Remove from {event_name}",
                affected_entity_id=result.id,
                affected_entity_type="event_result",
                metadata={"event_name": event_name, "event_id": result.event_id},
            )
        )

        if ev and ev.is_finalized:
            finalized_event_ids.add(ev.id)

    # --- 2. Partner effects --------------------------------------------------
    # Two directions:
    #   A) Back-references: other competitors' active results where
    #      partner_name == competitor.name  → they need a new partner.
    #   B) Forward-references: this competitor's own active results where
    #      partner_name is set → the named partner is affected even if their
    #      own result is already scratched/terminal.
    #
    # We deduplicate by (event_id, partner_name) so a mutual pairing only
    # generates one effect per event.

    tournament_event_ids = {
        ev.id for ev in Event.query.filter_by(tournament_id=tournament.id).all()
    }

    # Direction A — back-references (other comp listed this competitor as partner).
    back_ref_results = EventResult.query.filter(
        EventResult.partner_name == competitor.name,
        EventResult.event_id.in_(tournament_event_ids),
        EventResult.status.in_(_ACTIVE_STATUSES),
    ).all()

    # Direction B — forward-references (this competitor's own results with a partner).
    forward_ref_results = [r for r in active_results if r.partner_name]

    # Build a unified set keyed by (event_id, affected_result_id) to avoid duplicates.
    seen_partner_effects: set[tuple[int, int]] = set()
    partner_effect_rows: list[tuple] = []  # (result_row, partner_name_to_flag, event_id)

    for pr in back_ref_results:
        key = (pr.event_id, pr.id)
        if key not in seen_partner_effects:
            seen_partner_effects.add(key)
            partner_effect_rows.append((pr, pr.competitor_name, pr.event_id))

    for fr in forward_ref_results:
        # Find the partner's result in the same event.
        partner_result = EventResult.query.filter_by(
            event_id=fr.event_id,
            competitor_name=fr.partner_name,
        ).first()
        if partner_result:
            key = (partner_result.event_id, partner_result.id)
            if key not in seen_partner_effects:
                seen_partner_effects.add(key)
                partner_effect_rows.append(
                    (partner_result, partner_result.competitor_name, fr.event_id)
                )
        else:
            # Partner has no result row yet — still flag by name using this result.
            key = (fr.event_id, fr.id)
            if key not in seen_partner_effects:
                seen_partner_effects.add(key)
                partner_effect_rows.append((fr, fr.partner_name, fr.event_id))

    for pr, partner_display_name, event_id in partner_effect_rows:
        ev = events_by_id.get(event_id) or Event.query.get(event_id)
        event_name = ev.name if ev else f"Event #{event_id}"

        # Resolve the partner competitor's status.
        if pr.competitor_type == "college":
            owning_comp = CollegeCompetitor.query.get(pr.competitor_id)
        else:
            owning_comp = ProCompetitor.query.get(pr.competitor_id)

        # For forward-ref rows the "owning_comp" is this competitor, not the
        # partner — resolve the partner instead when we have a partner name.
        partner_comp = None
        if pr.competitor_id == competitor.id:
            # Forward-ref: find partner competitor by name in this tournament.
            partner_comp = ProCompetitor.query.filter_by(
                tournament_id=tournament.id, name=partner_display_name
            ).first()
            if partner_comp is None:
                partner_comp = CollegeCompetitor.query.filter_by(
                    tournament_id=tournament.id, name=partner_display_name
                ).first()
        else:
            partner_comp = owning_comp

        partner_scratched = partner_comp is not None and partner_comp.status == "scratched"
        scratch_note = " (already scratched)" if partner_scratched else ""

        effects.append(
            CascadeEffect(
                effect_type="partner",
                description=(
                    f"Flag {partner_display_name}{scratch_note} as needing new "
                    f"partner for {event_name}"
                ),
                affected_entity_id=pr.id,
                affected_entity_type="event_result",
                metadata={
                    "partner_name": partner_display_name,
                    "event_name": event_name,
                    "partner_already_scratched": partner_scratched,
                },
            )
        )

    # --- 3. Relay team effects -----------------------------------------------
    relay_events = (
        Event.query.filter_by(tournament_id=tournament.id)
        .filter(Event.event_state.isnot(None))
        .all()
    )

    for relay_ev in relay_events:
        try:
            state = json.loads(relay_ev.event_state or "{}")
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "scratch_cascade: corrupt event_state on Event id=%s; skipping",
                relay_ev.id,
            )
            continue

        teams = state.get("teams", [])
        for team in teams:
            team_number = team.get("team_number", "?")
            # Only check the division matching the competitor type to avoid
            # cross-division ID collisions (pro PK 7 != college PK 7).
            division_key = "pro_members" if comp_type == "pro" else "college_members"
            for member in team.get(division_key, []):
                if member.get("id") == competitor.id:
                    effects.append(
                        CascadeEffect(
                            effect_type="relay_team",
                            description=(
                                f"Remove from Relay Team {team_number} "
                                f"({relay_ev.name})"
                            ),
                            affected_entity_id=relay_ev.id,
                            affected_entity_type="event",
                            metadata={
                                "relay_event_name": relay_ev.name,
                                "team_number": team_number,
                            },
                        )
                    )

    # --- 4. Standings rebuild effect -----------------------------------------
    if finalized_event_ids:
        n = len(finalized_event_ids)
        effects.append(
            CascadeEffect(
                effect_type="standings",
                description=(
                    f"Recalculate standings — {n} finalized "
                    f'event{"s" if n != 1 else ""} affected'
                ),
                affected_entity_id=tournament.id,
                affected_entity_type="tournament",
                metadata={"finalized_event_ids": sorted(finalized_event_ids)},
            )
        )

    return effects


def execute_cascade(competitor, effects, judge_user_id, tournament) -> dict:
    """Atomically execute all provided cascade effects in a single savepoint.

    Stores a pre-scratch snapshot in the audit log for undo.

    Args:
        competitor: The competitor being scratched.
        effects: List[CascadeEffect] — the checked effects from the preview.
        judge_user_id: ID of the judge performing the scratch.
        tournament: Tournament instance.

    Returns:
        dict with 'success': bool, 'message': str, 'effects_applied': int.
    """
    from database import db
    from models.competitor import CollegeCompetitor, ProCompetitor
    from models.event import Event, EventResult
    from services.audit import log_action
    from services.proam_relay import ProAmRelay
    from services.scoring_engine import _rebuild_individual_points

    # --- Build pre-scratch snapshot ------------------------------------------
    result_ids = [
        e.affected_entity_id
        for e in effects
        if e.effect_type == "event_result"
    ]
    snapshot_results = []
    if result_ids:
        for r in EventResult.query.filter(EventResult.id.in_(result_ids)).all():
            snapshot_results.append(
                {
                    "id": r.id,
                    "status": r.status,
                    "points_awarded": float(r.points_awarded) if r.points_awarded is not None else None,
                    "payout_amount": float(r.payout_amount) if r.payout_amount is not None else None,
                    "final_position": r.final_position,
                }
            )

    from models.competitor import CollegeCompetitor as _CC
    snapshot = {
        "competitor_type": "college" if isinstance(competitor, _CC) else "pro",
        "competitor_status": competitor.status,
        "results": snapshot_results,
        "partner_json": competitor.partners,
        "relay_teams": [
            e.metadata.get("team_number")
            for e in effects
            if e.effect_type == "relay_team"
        ],
    }

    effects_applied = 0

    with db.session.begin_nested():
        # --- Set competitor scratched ----------------------------------------
        competitor.status = "scratched"

        # --- Process effects -------------------------------------------------
        # Pre-load EventResult rows for event_result and partner effects.
        event_result_map: dict[int, EventResult] = {}
        if result_ids:
            for r in EventResult.query.filter(EventResult.id.in_(result_ids)).all():
                event_result_map[r.id] = r

        affected_event_ids: set[int] = set()
        affected_college_competitor_ids: set[int] = set()

        for effect in effects:
            if effect.effect_type == "event_result":
                r = event_result_map.get(effect.affected_entity_id)
                if r is not None:
                    r.status = "scratched"
                    r.points_awarded = 0
                    r.payout_amount = 0
                    affected_event_ids.add(r.event_id)
                    if r.competitor_type == "college":
                        affected_college_competitor_ids.add(r.competitor_id)
                    effects_applied += 1

            elif effect.effect_type == "partner":
                # Clear the scratched competitor from the partner result's partner_name
                # and remove from partners JSON on the partner competitor record.
                partner_result_id = effect.affected_entity_id
                pr = event_result_map.get(partner_result_id)
                if pr is None:
                    pr = EventResult.query.get(partner_result_id)
                if pr is not None and pr.partner_name == competitor.name:
                    pr.partner_name = None
                    # Update partner competitor's partners JSON if they have one.
                    if pr.competitor_type == "college":
                        partner_comp = CollegeCompetitor.query.get(pr.competitor_id)
                    else:
                        partner_comp = ProCompetitor.query.get(pr.competitor_id)
                    if partner_comp is not None:
                        try:
                            partner_data = json.loads(partner_comp.partners or "{}")
                        except (json.JSONDecodeError, TypeError):
                            partner_data = {}
                        partner_data = {
                            k: v
                            for k, v in partner_data.items()
                            if v != competitor.name
                        }
                        partner_comp.partners = json.dumps(partner_data)
                    effects_applied += 1

            elif effect.effect_type == "relay_team":
                relay_event_id = effect.affected_entity_id
                relay_event = Event.query.get(relay_event_id)
                if relay_event is not None:
                    relay = ProAmRelay(tournament)
                    # Remove the competitor from all member lists in all teams.
                    teams = relay.relay_data.get("teams", [])
                    for team in teams:
                        for list_key in ("pro_members", "college_members"):
                            team[list_key] = [
                                m for m in team.get(list_key, [])
                                if m.get("id") != competitor.id
                            ]
                    relay._save_relay_data(commit=False)
                    effects_applied += 1

            elif effect.effect_type == "standings":
                finalized_event_ids = effect.metadata.get("finalized_event_ids", [])
                for ev_id in finalized_event_ids:
                    ev = Event.query.get(ev_id)
                    if ev is not None and ev.is_finalized:
                        ev.is_finalized = False
                        affected_event_ids.add(ev_id)
                # Rebuild college individual points for affected competitors.
                if affected_college_competitor_ids:
                    _rebuild_individual_points(list(affected_college_competitor_ids))
                effects_applied += 1

        # --- Audit log -------------------------------------------------------
        log_action(
            "competitor_scratched",
            entity_type="competitor",
            entity_id=competitor.id,
            details={
                "judge_id": judge_user_id,
                "effects": [e.description for e in effects],
                "scratch_snapshot": snapshot,
            },
        )

    return {
        "success": True,
        "message": f"Competitor scratched. {effects_applied} effect(s) applied.",
        "effects_applied": effects_applied,
    }


def reverse_cascade(competitor_id: int, judge_user_id: int, tournament) -> dict:
    """Reverse a scratch cascade by restoring from the audit log snapshot.

    Only works within SCRATCH_UNDO_WINDOW_MINUTES of the original scratch.

    Args:
        competitor_id: PK of the competitor whose scratch to reverse.
        judge_user_id: ID of the judge performing the undo.
        tournament: Tournament instance.

    Returns:
        dict with 'success': bool, 'message': str.
    """
    from database import db
    from models.audit_log import AuditLog
    from models.competitor import CollegeCompetitor, ProCompetitor
    from models.event import Event, EventResult
    from services.audit import log_action
    from services.proam_relay import ProAmRelay
    from services.scoring_engine import _rebuild_individual_points

    cutoff = datetime.utcnow() - timedelta(minutes=SCRATCH_UNDO_WINDOW_MINUTES)

    audit_entry = (
        AuditLog.query.filter(
            AuditLog.action == "competitor_scratched",
            AuditLog.entity_id == competitor_id,
            AuditLog.created_at >= cutoff,
        )
        .order_by(AuditLog.id.desc())
        .first()
    )

    if audit_entry is None:
        # Check whether any entry exists at all (outside window).
        any_entry = (
            AuditLog.query.filter(
                AuditLog.action == "competitor_scratched",
                AuditLog.entity_id == competitor_id,
            )
            .first()
        )
        if any_entry is not None:
            return {"success": False, "message": "Undo window expired"}
        return {"success": False, "message": "No scratch to undo"}

    try:
        details = json.loads(audit_entry.details_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return {"success": False, "message": "Audit entry corrupt; cannot undo"}

    snapshot = details.get("scratch_snapshot", {})

    with db.session.begin_nested():
        # --- Restore competitor status ---------------------------------------
        comp_type = snapshot.get("competitor_type", "pro")
        CompModel = CollegeCompetitor if comp_type == "college" else ProCompetitor
        comp = CompModel.query.get(competitor_id)

        if comp is not None:
            comp.status = snapshot.get("competitor_status", "active")
            # Restore partners JSON
            if "partner_json" in snapshot and snapshot["partner_json"] is not None:
                comp.partners = snapshot["partner_json"]

        # --- Restore EventResult rows ----------------------------------------
        affected_college_competitor_ids: set[int] = set()
        previously_finalized_event_ids: set[int] = set()

        for r_snap in snapshot.get("results", []):
            r = EventResult.query.get(r_snap["id"])
            if r is not None:
                r.status = r_snap["status"]
                r.points_awarded = r_snap.get("points_awarded")
                r.payout_amount = r_snap.get("payout_amount") or 0.0
                r.final_position = r_snap.get("final_position")
                if r.competitor_type == "college":
                    affected_college_competitor_ids.add(r.competitor_id)

        # --- Restore relay team membership ----------------------------------
        relay_team_numbers = snapshot.get("relay_teams", [])
        if relay_team_numbers and comp is not None:
            # Re-add competitor to relay teams they were removed from.
            # We need the original relay event state — reload from DB and re-add by
            # team_number.  We add back a minimal member dict.
            relay_events = (
                Event.query.filter_by(tournament_id=tournament.id)
                .filter(Event.event_state.isnot(None))
                .all()
            )
            for relay_ev in relay_events:
                try:
                    state = json.loads(relay_ev.event_state or "{}")
                except (json.JSONDecodeError, TypeError):
                    continue
                modified = False
                for team in state.get("teams", []):
                    if team.get("team_number") not in relay_team_numbers:
                        continue
                    # Determine member list key by competitor type.
                    is_college = isinstance(comp, CollegeCompetitor)
                    list_key = "college_members" if is_college else "pro_members"
                    members = team.get(list_key, [])
                    # Only re-add if not already present.
                    if not any(m.get("id") == comp.id for m in members):
                        members.append(
                            {"id": comp.id, "name": comp.name, "gender": getattr(comp, "gender", "")}
                        )
                        team[list_key] = members
                        modified = True
                if modified:
                    relay_ev.event_state = json.dumps(state)

        # --- Re-finalize events that were un-finalized ----------------------
        # Collect event IDs from snapshot results that were in finalized events.
        # We re-set is_finalized=True only if the event was finalized at snapshot time.
        # The snapshot only un-finalizes via the standings effect — check which
        # events those were by inspecting which results belonged to finalized events.
        # We restore is_finalized from the effects list stored in audit details.
        # Simpler approach: for each affected event, if ALL results are now non-scratched
        # status, restore finalization to True — but that's risky.  Instead we track
        # which events were un-finalized via the effects metadata.
        for r_snap in snapshot.get("results", []):
            r = EventResult.query.get(r_snap["id"])
            if r is not None:
                ev = Event.query.get(r.event_id)
                if ev is not None and not ev.is_finalized:
                    # Only re-finalize if the original result was in a completed state
                    # (meaning the event was finalized at scratch time).
                    if r_snap.get("status") == "completed":
                        ev.is_finalized = True
                        previously_finalized_event_ids.add(ev.id)

        # --- Rebuild college points -----------------------------------------
        if affected_college_competitor_ids:
            _rebuild_individual_points(list(affected_college_competitor_ids))

        # --- Audit log the undo ---------------------------------------------
        log_action(
            "scratch_undone",
            entity_type="competitor",
            entity_id=competitor_id,
            details={
                "judge_id": judge_user_id,
                "restored_from": audit_entry.id,
            },
        )

    return {"success": True, "message": "Scratch reversed successfully."}
