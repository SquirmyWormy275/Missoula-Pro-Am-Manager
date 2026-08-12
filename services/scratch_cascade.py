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
from datetime import timedelta
from typing import TYPE_CHECKING

from database import db
from services.time_utils import utc_now_naive

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
    #
    # Matched against BOTH name forms deliberately.  EventResult.partner_name
    # is written as the partner's display_name (that is what makes
    # scoring_engine._pair_key_for collapse, since competitor_name is also a
    # display_name, and it is what scratch_cascade.py's own partner lookup
    # below already assumes).  ProCompetitor.display_name IS the bare name, so
    # every pro row ever written satisfies both forms and nothing about pro
    # changes.  CollegeCompetitor.display_name carries the team suffix,
    # 'Nell Horgan (FVC-A)', so a bare-name-only comparison would silently
    # return zero back-references for every college pair and the scratch would
    # leave the partner standing in a pair that no longer exists.
    back_ref_results = EventResult.query.filter(
        EventResult.partner_name.in_(
            {competitor.name, competitor.display_name}
        ),
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
        ev = events_by_id.get(event_id) or db.session.get(Event, event_id)
        event_name = ev.name if ev else f"Event #{event_id}"

        # Resolve the partner competitor's status.
        if pr.competitor_type == "college":
            owning_comp = db.session.get(CollegeCompetitor, pr.competitor_id)
        else:
            owning_comp = db.session.get(ProCompetitor, pr.competitor_id)

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
                    "payout_settled": bool(r.payout_settled),
                    "final_position": r.final_position,
                }
            )

    from models.competitor import CollegeCompetitor as _CC
    snapshot = {
        "competitor_type": "college" if isinstance(competitor, _CC) else "pro",
        "competitor_status": competitor.status,
        "total_earnings": (
            float(competitor.total_earnings or 0.0)
            if not isinstance(competitor, _CC) else None
        ),
        "results": snapshot_results,
        "partner_json": competitor.partners,
        "unfinalized_events": [],
        # Populated inside the transaction below, by the heat-removal loop, at
        # the moment each heat is mutated.  It cannot be built here: the heats
        # a competitor is actually removed from are decided by the loop's own
        # status filter, not by the effects list.
        "heats": [],
        # Populated inside the transaction below, by the partner branch, at the
        # moment each partner row is mutated.  Like "heats" it cannot be built
        # here: which partner rows actually get touched is decided by the
        # branch's own name-match guard, not by the effects list.  Without it
        # the undo restored the scratched competitor perfectly and left every
        # counterparty stripped, which reads on screen as a successful undo.
        "partners": [],
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
                    awarded = float(r.payout_amount or 0.0)
                    r.status = "scratched"
                    r.points_awarded = 0
                    r.payout_amount = 0
                    r.payout_settled = False
                    if awarded and r.competitor_type == "pro":
                        pro_competitor = db.session.get(ProCompetitor, r.competitor_id)
                        if pro_competitor is not None:
                            pro_competitor.total_earnings = max(
                                0.0, pro_competitor.total_earnings - awarded
                            )
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
                    pr = db.session.get(EventResult, partner_result_id)
                # Both name forms, for the reason documented on the Direction A
                # query above: college partner_name carries the team suffix.
                if pr is not None and pr.partner_name in (
                    competitor.name, competitor.display_name
                ):
                    if pr.competitor_type == "college":
                        partner_comp = db.session.get(CollegeCompetitor, pr.competitor_id)
                    else:
                        partner_comp = db.session.get(ProCompetitor, pr.competitor_id)

                    # This effect is scoped to ONE event: the one this partner
                    # result belongs to.  partners is event_id -> partner_name
                    # (models/competitor.py), so the key to drop is this event's.
                    # The previous filter dropped every key whose VALUE was the
                    # scratched competitor, so ticking the Double Buck effect
                    # also stripped Partnered Axe Throw, an effect the judge had
                    # left unchecked.  Acting on an unchecked effect is the
                    # defect; the checkbox is a consent control, not a hint.
                    partner_event_key = str(pr.event_id)
                    partner_data = {}
                    dropped_value = None
                    if partner_comp is not None:
                        try:
                            partner_data = json.loads(partner_comp.partners or "{}")
                        except (json.JSONDecodeError, TypeError):
                            partner_data = {}
                        # Both name forms again: the guard above already treats
                        # them as the same human, so the JSON has to as well or
                        # a college pairing survives the scratch half-cleared.
                        if partner_data.get(partner_event_key) in (
                            competitor.name, competitor.display_name
                        ):
                            dropped_value = partner_data.pop(partner_event_key)
                            partner_comp.partners = json.dumps(partner_data)

                    # Capture BEFORE nulling partner_name.  reverse_cascade has
                    # no other way to learn what this row said: the scratched
                    # competitor's own partners JSON is snapshotted separately
                    # and does not carry the counterparty's row state.
                    snapshot["partners"].append(
                        {
                            "result_id": pr.id,
                            "result_partner_name": pr.partner_name,
                            "event_id": pr.event_id,
                            "competitor_id": (
                                partner_comp.id if partner_comp is not None else None
                            ),
                            "competitor_type": pr.competitor_type,
                            # None when the key was already absent or named
                            # somebody else, which is the signal to leave the
                            # partner's JSON alone on undo.
                            "partner_json_value": dropped_value,
                        }
                    )

                    pr.partner_name = None
                    effects_applied += 1

            elif effect.effect_type == "relay_team":
                relay_event_id = effect.affected_entity_id
                relay_event = db.session.get(Event, relay_event_id)
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
                    ev = db.session.get(Event, ev_id)
                    if ev is not None and ev.is_finalized:
                        ev.is_finalized = False
                        snapshot["unfinalized_events"].append({
                            "event_id": ev.id,
                            "result_versions": {},
                        })
                        affected_event_ids.add(ev_id)
                # Rebuild college individual points for affected competitors.
                if affected_college_competitor_ids:
                    _rebuild_individual_points(list(affected_college_competitor_ids))
                effects_applied += 1

        # --- Remove competitor from unfinished heats -------------------------
        # Scratching means the competitor shouldn't appear on upcoming heat
        # sheets. Only touch non-completed heats so past heats stay intact.
        from models.heat import Heat
        heat_type = "college" if isinstance(competitor, _CC) else "pro"
        heats_q = (
            Heat.query.join(Event)
            .filter(
                Event.tournament_id == tournament.id,
                Event.event_type == heat_type,
                Heat.status != "completed",
            )
        )
        touched_event_ids: set[int] = set()
        emptied_heats: list[tuple] = []
        heat_snapshot: list = snapshot["heats"]
        for heat in heats_q.all():
            comp_ids = heat.get_competitors()
            if competitor.id in comp_ids:
                # Capture BEFORE mutating.  Nothing else in this function
                # records heat state, which is why reverse_cascade could restore
                # a competitor to active status with an empty schedule.
                #
                # Position is captured, not just membership.  The competitors
                # JSON is the running order the judge sheet prints, and
                # Heat.add_competitor appends, so restoring by append would put
                # the competitor last in every heat they were pulled from.
                _pre_assignments = heat.get_stand_assignments()
                _h_snap = {
                    "heat_id": heat.id,
                    "index": comp_ids.index(competitor.id),
                    "stand": _pre_assignments.get(str(competitor.id)),
                    # Captured for the auto-complete below.  auto_completed is
                    # the flag reverse_cascade keys off: the undo must restore
                    # a status THIS function wrote and must not touch one the
                    # judge wrote afterwards.
                    "status": heat.status,
                    "auto_completed": False,
                }
                heat_snapshot.append(_h_snap)
                # One roster write, and the mirror of what reverse_cascade
                # does to put him back.  Was a `remove_competitor`, a
                # hand-rolled delete out of the stand dict, and a
                # `sync_assignments` to copy the pair into the rows.
                comp_ids.remove(competitor.id)
                assignments = dict(_pre_assignments)
                assignments.pop(str(competitor.id), None)
                heat.set_roster(heat_type, comp_ids, assignments)
                # A heat this scratch just emptied is done, and closing it is
                # not cosmetic.  routes/scheduling/heats.py scratch_competitor
                # already does exactly this for the other scratch entry point,
                # and flashes about it.  Left 'pending' it is the one heat
                # status the operator cannot clear: POSTing the enter page of
                # an empty heat redirects back to itself and leaves the status
                # alone.  So it pins all_heats_complete False forever
                # (scoring_workflow.py:414), next_unscored_heat sends the judge
                # to an empty stand (scoring.py:518), and next_incomplete_event
                # never advances past the event (scoring.py:530).
                #
                # Scoped to heats THIS cascade emptied, on purpose.  Heats that
                # were already empty never enter this branch at all, because
                # the loop body only runs when the competitor is in the heat.
                # Nineteen such heats ship in the 2026 database, six of them in
                # the Birling bracket, which scores through birling_bracket.py
                # and not through these rows.  Closing those is a different fix
                # with a different blast radius.
                if not heat.get_competitors():
                    heat.status = "completed"
                    _h_snap["auto_completed"] = True
                    # Named by event, not by bare heat number.  A cascade
                    # spans the whole tournament: scratching one college
                    # competitor off the real 2026 data closes five heat rows
                    # across four different events, and "Heats 1, 3, 4" tells
                    # the operator nothing about which board they came off.
                    _ev = heat.event
                    emptied_heats.append(
                        (getattr(_ev, "display_name", None) or getattr(
                            _ev, "name", f"event {heat.event_id}"),
                         heat.heat_number)
                    )
                touched_event_ids.add(heat.event_id)

        # --- Stock Saw solo-stand rebalance ----------------------------------
        # The V2.14.13 rebalance is wired into the heats-page scratch route but
        # NOT this cascade path. Without it, scratching the stand-7 seat from a
        # pair heat leaves the survivor stuck on stand 8, and judges end up
        # running six solo heats in a row on the same physical stand. Walk the
        # events we actually mutated above and re-alternate 7/8 for each.
        # Scope is gated by `rebalance_stock_saw_solo_stands` itself —
        # all Stock Saw (pro + college) runs on stands 7-8 per
        # DOMAIN_CONTRACT; non-stock-saw events early-return.
        if touched_event_ids:
            try:
                from services.heat_generator import rebalance_stock_saw_solo_stands
                for ev_id in touched_event_ids:
                    ev = db.session.get(Event, ev_id)
                    if ev is not None:
                        rebalance_stock_saw_solo_stands(ev)
            except Exception:
                # Rebalance must never break a scratch — log and continue.
                logger.warning(
                    "scratch_cascade: stock saw rebalance failed for competitor %s; "
                    "stands left as-is",
                    competitor.id,
                    exc_info=True,
                )

        if snapshot["unfinalized_events"]:
            db.session.flush()
            for event_snapshot in snapshot["unfinalized_events"]:
                event_snapshot["result_versions"] = {
                    str(result.id): result.version_id
                    for result in EventResult.query.filter_by(
                        event_id=event_snapshot["event_id"],
                    ).all()
                }

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
        # Reported, not folded into "message": scratch_confirm discards
        # message entirely and builds its own flash, so anything written
        # there would never reach the operator.  The heats-page scratch
        # already tells the judge when a heat closes under him; this is the
        # same notice for the other entry point.  (event name, heat number),
        # deduplicated because a two-run event has one heat number per run.
        "emptied_heats": sorted(set(emptied_heats)),
    }


def _snapshot_competitor_type(audit_entry) -> str:
    """Return the competitor_type recorded in an audit entry's scratch snapshot.

    Defaults to 'pro' to match the historical reader below, so entries written
    before the type was recorded keep their old behaviour.
    """
    try:
        details = json.loads(audit_entry.details_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return "pro"
    snapshot = details.get("scratch_snapshot", {})
    if not isinstance(snapshot, dict):
        return "pro"
    return snapshot.get("competitor_type", "pro")


def find_undoable_scratch(competitor_id: int, competitor_type: str | None = None):
    """Return the AuditLog row an undo would restore from, or None.

    Read-only.  Extracted so the confirmation page and the undo route cannot
    disagree about whether an Undo button should be offered: the page showing
    a button that reverse_cascade would then refuse is worse than no button,
    because on race day the judge reads the button as proof the scratch is
    still reversible.

    The snapshot type lives inside details_json, which is a TEXT column, so
    the type filter runs in Python.  JSON operators differ between SQLite and
    PostgreSQL and this code has to work on both.
    """
    from models.audit_log import AuditLog

    cutoff = utc_now_naive() - timedelta(minutes=SCRATCH_UNDO_WINDOW_MINUTES)
    return next(
        (
            entry
            for entry in AuditLog.query.filter(
                AuditLog.action == "competitor_scratched",
                AuditLog.entity_id == competitor_id,
                AuditLog.created_at >= cutoff,
            )
            .order_by(AuditLog.id.desc())
            .all()
            if competitor_type is None
            or _snapshot_competitor_type(entry) == competitor_type
        ),
        None,
    )


def find_undoable_scratches(competitor_ids, competitor_type: str | None = None) -> set:
    """The subset of competitor_ids whose scratch is still inside the window.

    Read-only, one query for a whole page.  The rosters need this for every
    scratched competitor in the table, and calling find_undoable_scratch per
    row would put a query per competitor on the busiest screen of race day.

    Same cutoff and same snapshot-type filter as find_undoable_scratch, so an
    Undo button on a roster and the one on the confirmation page cannot
    disagree about whether an undo is still possible.
    """
    from models.audit_log import AuditLog

    ids = [int(c) for c in competitor_ids]
    if not ids:
        return set()

    cutoff = utc_now_naive() - timedelta(minutes=SCRATCH_UNDO_WINDOW_MINUTES)
    return {
        entry.entity_id
        for entry in AuditLog.query.filter(
            AuditLog.action == "competitor_scratched",
            AuditLog.entity_id.in_(ids),
            AuditLog.created_at >= cutoff,
        ).all()
        if competitor_type is None
        or _snapshot_competitor_type(entry) == competitor_type
    }


def reverse_cascade(competitor_id: int, judge_user_id: int, tournament,
                    competitor_type: str | None = None) -> dict:
    """Reverse a scratch cascade by restoring from the audit log snapshot.

    Only works within SCRATCH_UNDO_WINDOW_MINUTES of the original scratch.

    Args:
        competitor_id: PK of the competitor whose scratch to reverse.
        judge_user_id: ID of the judge performing the undo.
        tournament: Tournament instance.
        competitor_type: 'college' or 'pro'.  College and pro competitor ids
            come from separate autoincrement sequences and collide, so the
            AuditLog rows for the two people share an entity_id.  Without this
            the newest matching row wins and the undo restores the twin.
            None keeps the pre-existing behaviour of taking the newest row.

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

    def _matches_type(entry) -> bool:
        if competitor_type is None:
            return True
        return _snapshot_competitor_type(entry) == competitor_type

    # Same lookup the confirmation page uses to decide whether to offer Undo.
    audit_entry = find_undoable_scratch(competitor_id, competitor_type)

    if audit_entry is None:
        # Check whether any entry exists at all (outside window).
        any_entry = next(
            (
                entry
                for entry in AuditLog.query.filter(
                    AuditLog.action == "competitor_scratched",
                    AuditLog.entity_id == competitor_id,
                )
                .all()
                if _matches_type(entry)
            ),
            None,
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
        comp_type = competitor_type or snapshot.get("competitor_type", "pro")
        CompModel = CollegeCompetitor if comp_type == "college" else ProCompetitor
        comp = db.session.get(CompModel, competitor_id)

        if comp is not None:
            comp.status = snapshot.get("competitor_status", "active")
            if isinstance(comp, ProCompetitor) and "total_earnings" in snapshot:
                comp.total_earnings = float(snapshot["total_earnings"] or 0.0)
            # Restore partners JSON
            if "partner_json" in snapshot and snapshot["partner_json"] is not None:
                comp.partners = snapshot["partner_json"]

        # --- Restore partner counterparties ----------------------------------
        # execute_cascade nulls the partner's EventResult.partner_name and drops
        # this event's key from the partner competitor's partners JSON.  Neither
        # was ever restored, so an undo handed back the scratched competitor
        # intact and left the partner unpaired, then flashed success.  Nothing
        # catches that afterwards: routes/scheduling/partners.py's orphan queue
        # only matches rows whose partner_name is set and names a scratched
        # competitor, and after an undo it is neither.  The pair silently fails
        # to be seated the next time heats are generated.
        #
        # Restored per-field rather than by re-writing the whole JSON blob: if
        # the judge re-paired the partner during the undo window, that new
        # pairing is the current truth and must survive the undo.
        for p_snap in snapshot.get("partners", []):
            p_result = db.session.get(EventResult, p_snap.get("result_id"))
            if p_result is not None and not p_result.partner_name:
                p_result.partner_name = p_snap.get("result_partner_name")

            p_comp_id = p_snap.get("competitor_id")
            p_value = p_snap.get("partner_json_value")
            if p_comp_id is None or p_value is None:
                continue
            PartnerModel = (
                CollegeCompetitor
                if p_snap.get("competitor_type") == "college"
                else ProCompetitor
            )
            p_comp = db.session.get(PartnerModel, p_comp_id)
            if p_comp is None:
                continue
            p_data = p_comp.get_partners()
            p_key = str(p_snap.get("event_id"))
            if p_key not in p_data:
                p_data[p_key] = p_value
                p_comp.partners = json.dumps(p_data)

        safe_to_refinalize: set[int] = set()
        for event_snapshot in snapshot.get("unfinalized_events", []):
            event_id = event_snapshot.get("event_id")
            ev = db.session.get(Event, event_id)
            expected_versions = event_snapshot.get("result_versions")
            if not isinstance(expected_versions, dict) or ev is None or ev.is_finalized:
                continue
            current_versions = {
                str(result.id): result.version_id
                for result in EventResult.query.filter_by(event_id=event_id).all()
            }
            if current_versions == expected_versions:
                safe_to_refinalize.add(event_id)

        # --- Restore EventResult rows ----------------------------------------
        affected_college_competitor_ids: set[int] = set()
        previously_finalized_event_ids: set[int] = set()

        for r_snap in snapshot.get("results", []):
            r = db.session.get(EventResult, r_snap["id"])
            if r is not None:
                r.status = r_snap["status"]
                r.points_awarded = r_snap.get("points_awarded")
                r.payout_amount = r_snap.get("payout_amount") or 0.0
                r.payout_settled = bool(r_snap.get("payout_settled", False))
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
        for event_snapshot in snapshot.get("unfinalized_events", []):
            ev = db.session.get(Event, event_snapshot.get("event_id"))
            if (
                ev is not None
                and ev.id in safe_to_refinalize
                and not ev.is_finalized
            ):
                ev.is_finalized = True
                previously_finalized_event_ids.add(ev.id)

        # --- Restore heat membership -----------------------------------------
        # execute_cascade strips the competitor out of every non-completed heat
        # (see "Remove competitor from unfinished heats" above), rewriting the
        # heat's `heat_assignments` rows.  Through D12-C commit F2 it also
        # mutated two JSON columns on `heats`, which revision t9b3c4d5e6f7
        # dropped; the rows are the whole of it now.
        #
        # None of it was ever captured and none of it was
        # ever restored, so an undo handed back a competitor who was active,
        # scored and paid, and on no heat sheet anywhere.  On race day that is
        # a competitor who does not get called to the stand.
        #
        # Restored per-heat rather than by regenerating: regeneration would
        # reshuffle every other competitor in the event.
        from models.heat import Heat

        restored_event_ids: set[int] = set()
        for h_snap in snapshot.get("heats", []):
            heat = db.session.get(Heat, h_snap.get("heat_id"))
            if heat is None:
                continue
            comp_ids = heat.get_competitors()
            if competitor_id not in comp_ids:
                idx = h_snap.get("index")
                if isinstance(idx, int) and 0 <= idx <= len(comp_ids):
                    comp_ids.insert(idx, competitor_id)
                else:
                    # Heat shrank since the scratch (another scratch, a move).
                    # Membership matters more than order; take the tail.
                    comp_ids.append(competitor_id)
            stands = heat.get_stand_assignments()
            stand = h_snap.get("stand")
            if stand is not None:
                stands[str(competitor_id)] = stand
            # Undo the auto-complete, and ONLY that.  The flag is the whole
            # point: a heat can also be 'completed' because the judge scored
            # the survivors after the scratch, and blindly writing the
            # snapshotted status back would reopen a scored heat and throw
            # away work the operator earned.  Without the restore, the undo
            # hands the competitor back into a heat next_unscored_heat will
            # never serve, and all_heats_complete goes true with someone who
            # has never been timed, which publishes the event with him at
            # position None.
            if h_snap.get("auto_completed") and heat.status == "completed":
                heat.status = h_snap.get("status") or "pending"
            # One roster write, membership and stand together.  This used to
            # be a `set_competitors`, then a `set_stand_assignment`, then a
            # `sync_assignments` to copy the result into the rows.  Writing the
            # rows directly is the same end state and does not pass through an
            # intermediate roster that has the competitor back without his
            # stand.
            heat.set_roster(comp_type, comp_ids, stands)
            restored_event_ids.add(heat.event_id)

        # Mirror of the scratch-side rebalance.  It is not optional symmetry:
        # the scratch left a solo Stock Saw heat and the rebalance moved the
        # survivor onto the alternating stand, so blindly restoring the
        # scratched competitor's original stand can land two competitors on the
        # same one.  rebalance_stock_saw_solo_stands forces a pair back onto
        # 7 and 8 in comp order, which repairs exactly that collision, and
        # early-returns for every event that is not Stock Saw.
        if restored_event_ids:
            try:
                from services.heat_generator import rebalance_stock_saw_solo_stands
                for ev_id in restored_event_ids:
                    ev = db.session.get(Event, ev_id)
                    if ev is not None:
                        rebalance_stock_saw_solo_stands(ev)
            except Exception:
                # A rebalance failure must never break an undo.
                logger.warning(
                    "scratch_cascade: stock saw rebalance failed on undo for "
                    "competitor %s; stands left as restored",
                    competitor_id,
                    exc_info=True,
                )

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
