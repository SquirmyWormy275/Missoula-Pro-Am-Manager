"""
Hand-saw stand block alternation service.

Assigns the two physical saw-stand blocks (A = stands 1-4, B = stands 5-8)
across every hand-saw heat in the tournament so that consecutive hand-saw
heats in the day's run order land on opposite blocks. This lets the next
team set in on the free block while the current team runs.

Scope: all events with stand_type == 'saw_hand' — Single Buck, Double Buck,
Jack & Jill Sawing, across both divisions and genders.

Continuity: the block of heat N+1 depends only on the block of the most
recent hand-saw heat before it in the day's run order. Non-saw heats
between two saw heats do NOT reset the alternation.

Day boundary: alternation resets at each day. Friday starts on Block A,
Saturday starts on Block A, independently.

State source: authoritative run order comes from
`schedule_builder.get_friday_ordered_heats()` and
`schedule_builder.get_saturday_ordered_heats()`.

Idempotent. Safe to call after any mutation that affects run order or
heat composition.
"""

from __future__ import annotations

import logging

from database import db
from models import Heat, Tournament
from services.schedule_builder import (
    get_friday_ordered_heats,
    get_saturday_ordered_heats,
)

logger = logging.getLogger(__name__)

BLOCK_A: list[int] = [1, 2, 3, 4]
BLOCK_B: list[int] = [5, 6, 7, 8]
SAW_STAND_TYPE = "saw_hand"


def assign_saw_blocks(tournament: Tournament) -> dict:
    """Recompute and persist hand-saw stand block assignments for every
    hand-saw heat in the tournament.

    Iterates Friday then Saturday run order, flipping between Block A and
    Block B at each hand-saw heat encountered. Skips non-saw heats while
    preserving alternation state. Day boundary resets to Block A.

    Commits once at the end. Rolls back on any exception and re-raises.

    Returns:
        {
            'friday_saw_heats': int,
            'saturday_saw_heats': int,
            'heats_updated': int,
            'heats_unchanged': int,
        }
    """
    summary = {
        "friday_saw_heats": 0,
        "saturday_saw_heats": 0,
        "heats_updated": 0,
        "heats_unchanged": 0,
    }

    try:
        # Friday
        block = BLOCK_A
        for heat in get_friday_ordered_heats(tournament):
            event = heat.event
            if not event or event.stand_type != SAW_STAND_TYPE:
                continue
            summary["friday_saw_heats"] += 1
            changed = remap_heat_to_block(heat, block)
            if changed:
                summary["heats_updated"] += 1
            else:
                summary["heats_unchanged"] += 1
            block = BLOCK_B if block is BLOCK_A else BLOCK_A

        # Saturday — day boundary: reset to Block A independent of Friday
        block = BLOCK_A
        for heat in get_saturday_ordered_heats(tournament):
            event = heat.event
            if not event or event.stand_type != SAW_STAND_TYPE:
                continue
            summary["saturday_saw_heats"] += 1
            changed = remap_heat_to_block(heat, block)
            if changed:
                summary["heats_updated"] += 1
            else:
                summary["heats_unchanged"] += 1
            block = BLOCK_B if block is BLOCK_A else BLOCK_A

        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    return summary


def remap_heat_to_block(heat: Heat, target_block: list[int]) -> bool:
    """Remap the heat's stand_assignments to use target_block's 4 stand
    numbers, preserving pair-sharing structure for partnered events.

    Algorithm:
      1. Load current stand_assignments JSON.
      2. Collect the unique stand numbers actually in use, sorted ascending.
      3. Build a slot map: unique[i] -> target_block[i].
      4. If the mapping is an identity (heat already on target block),
         return False — no write.
      5. Apply the slot map via heat.set_stand_assignment().
      6. Call heat.sync_assignments() with the event's comp type to
         rebuild HeatAssignment rows.
      7. Return True.

    Returns:
        True if any assignment changed, False if no write was needed.
    """
    assignments = heat.get_stand_assignments()
    if not assignments:
        logger.debug("saw_block: heat %s has no stand_assignments, skipping", heat.id)
        return False

    # Collect unique stand numbers in use; drop None/0 (unassigned)
    used_stands = sorted(
        {
            int(stand)
            for stand in assignments.values()
            if stand is not None and int(stand) > 0
        }
    )

    if not used_stands:
        logger.debug("saw_block: heat %s has no valid stand numbers, skipping", heat.id)
        return False

    if len(used_stands) > 4:
        raise ValueError(
            f"Heat {heat.id} uses {len(used_stands)} unique stands "
            f"({used_stands}) — exceeds saw block capacity of 4."
        )

    # Build slot map: sorted-unique -> target_block positions (same length)
    slot_map = {old: target_block[i] for i, old in enumerate(used_stands)}

    # Identity check — if every old stand already equals its target, no-op
    if all(old == new for old, new in slot_map.items()):
        return False

    for comp_id_str, old_stand in list(assignments.items()):
        if old_stand is None:
            continue
        try:
            old_int = int(old_stand)
        except (TypeError, ValueError):
            continue
        new_stand = slot_map.get(old_int)
        if new_stand is None:
            continue
        try:
            comp_id_int = int(comp_id_str)
        except (TypeError, ValueError):
            continue
        heat.set_stand_assignment(comp_id_int, new_stand)

    event = heat.event
    if event and event.event_type in ("college", "pro"):
        heat.sync_assignments(event.event_type)
    else:
        logger.warning(
            "saw_block: heat %s has unexpected event_type=%r; "
            "stand_assignments written but HeatAssignment rows not synced",
            heat.id,
            getattr(event, "event_type", None),
        )

    return True


def trigger_saw_block_recompute(tournament: Tournament) -> dict | None:
    """Route-hook wrapper: call assign_saw_blocks, log result, flash on failure.

    Use this inside route handlers after the primary commit so a saw-block
    failure cannot roll back the route's real work. On exception: logs,
    flashes a warning, and returns None. On success: logs summary and
    returns the summary dict.
    """
    from flask import current_app, flash

    try:
        summary = assign_saw_blocks(tournament)
        current_app.logger.info("saw_blocks recomputed: %s", summary)
        return summary
    except Exception as exc:
        current_app.logger.error(
            "saw_block_assignment failed: %s", exc, exc_info=True,
        )
        flash(
            "Stand block alternation failed to update. Run it manually "
            "from the Saw Block Status page.",
            "warning",
        )
        return None
