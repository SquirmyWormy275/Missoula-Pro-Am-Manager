"""
Repair script for the L/R springboard importer bug (2026-04-20).

Before this PR, services/pro_entry_importer.py collapsed the Google Form
checkboxes 'Springboard (L)' and 'Springboard (R)' into canonical
'Springboard' but never populated ProCompetitor.is_left_handed_springboard.
Every pro competitor imported via xlsx in 2026 defaulted to False (right-
handed), which silently broke the heat generator's LH spreading rule.

This script runs ONCE after PR A deploys. It re-parses the original xlsx
with the fixed parser, writes is_left_handed_springboard where the form
captured a signal, and regenerates heats for pro springboard events so
they reflect the corrected handedness.

Usage:
    flask shell
    >>> from scripts.repair_springboard_handedness import repair
    >>> repair(tournament_id=1, xlsx_path='uploads/<uuid>.xlsx')

Or as a standalone script from the project root:
    python -m scripts.repair_springboard_handedness <tournament_id> <xlsx_path>

Skips heat regeneration for any pro springboard event with:
  - event.is_finalized True, or
  - any Heat with status in ('in_progress', 'completed')

so live / scored events are never mutated.  For those, the script updates
the is_left_handed_springboard flag only and logs a notice that the admin
must rebuild manually.
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)


def repair(tournament_id: int, xlsx_path: str, dry_run: bool = False) -> dict:
    """
    Re-apply is_left_handed_springboard from an xlsx to existing competitors
    and regenerate pro springboard heats where safe.

    Args:
        tournament_id: Target tournament ID.
        xlsx_path: Path to the original Google Forms xlsx export.
        dry_run: If True, report what would change but commit nothing.

    Returns:
        Dict summarising the repair: flag_updates, heat_regenerations,
        skipped_events, unmatched_emails, errors.
    """
    from database import db
    from models import Event, Heat, ProCompetitor, Tournament
    from services.pro_entry_importer import parse_pro_entries

    summary = {
        "tournament_id": tournament_id,
        "xlsx_path": xlsx_path,
        "dry_run": dry_run,
        "flag_updates": [],  # list of {email, name, old, new}
        "heat_regenerations": [],  # list of event display names regenerated
        "skipped_events": [],  # list of {event, reason}
        "unmatched_emails": [],  # list of xlsx emails not found in DB
        "errors": [],
    }

    tournament = Tournament.query.get(tournament_id)
    if tournament is None:
        summary["errors"].append(f"Tournament {tournament_id} not found.")
        return summary

    entries = parse_pro_entries(xlsx_path)
    logger.info("repair: parsed %d entries from %s", len(entries), xlsx_path)

    # --- Phase 1: update is_left_handed_springboard from xlsx signal ---
    for entry in entries:
        email = (entry.get("email") or "").strip()
        if not email:
            continue
        lh_flag = entry.get("is_left_handed_springboard")
        if lh_flag is None:
            continue  # sentinel: no signal in this xlsx, skip

        competitor = ProCompetitor.query.filter_by(
            tournament_id=tournament_id,
            email=email,
        ).first()
        if competitor is None:
            summary["unmatched_emails"].append(email)
            continue

        old_flag = bool(competitor.is_left_handed_springboard)
        new_flag = bool(lh_flag)
        if old_flag == new_flag:
            continue

        summary["flag_updates"].append(
            {
                "email": email,
                "name": competitor.name,
                "old": old_flag,
                "new": new_flag,
            }
        )

        if not dry_run:
            competitor.is_left_handed_springboard = new_flag

    if not dry_run:
        db.session.flush()

    # --- Phase 2: regenerate pro springboard heats where safe ---
    springboard_events = Event.query.filter_by(
        tournament_id=tournament_id,
        event_type="pro",
        stand_type="springboard",
    ).all()

    for event in springboard_events:
        if event.is_finalized:
            summary["skipped_events"].append(
                {
                    "event": event.display_name,
                    "reason": "event is finalized — manual rebuild only",
                }
            )
            continue

        live_heat = (
            Heat.query.filter_by(event_id=event.id)
            .filter(Heat.status.in_(("in_progress", "completed")))
            .first()
        )
        if live_heat is not None:
            summary["skipped_events"].append(
                {
                    "event": event.display_name,
                    "reason": f"heat #{live_heat.heat_number} already {live_heat.status}",
                }
            )
            continue

        if dry_run:
            summary["heat_regenerations"].append(f"(dry-run) {event.display_name}")
            continue

        # Regenerate.  Uses the fixed heat generator which spreads LH cutters.
        from services.heat_generator import generate_event_heats

        try:
            generate_event_heats(event)
            summary["heat_regenerations"].append(event.display_name)
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(f"regenerate {event.display_name}: {exc!s}")

    if not dry_run:
        db.session.commit()

    return summary


def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(sys.argv) < 3:
        print(
            "usage: python -m scripts.repair_springboard_handedness "
            "<tournament_id> <xlsx_path> [--dry-run]"
        )
        return 2

    tid = int(sys.argv[1])
    path = sys.argv[2]
    dry = "--dry-run" in sys.argv[3:]

    from app import create_app

    app = create_app()
    with app.app_context():
        summary = repair(tid, path, dry_run=dry)

    print(
        f'Tournament {summary["tournament_id"]}  xlsx={summary["xlsx_path"]}'
        f'  dry_run={summary["dry_run"]}'
    )
    print(f'Flag updates: {len(summary["flag_updates"])}')
    for u in summary["flag_updates"]:
        print(f'  {u["email"]:<40} {u["name"]:<30} {u["old"]} -> {u["new"]}')
    print(f'Heats regenerated: {len(summary["heat_regenerations"])}')
    for name in summary["heat_regenerations"]:
        print(f"  {name}")
    if summary["skipped_events"]:
        print(f'Skipped events: {len(summary["skipped_events"])}')
        for s in summary["skipped_events"]:
            print(f'  {s["event"]}: {s["reason"]}')
    if summary["unmatched_emails"]:
        print(f'Unmatched xlsx emails: {len(summary["unmatched_emails"])}')
        for email in summary["unmatched_emails"]:
            print(f"  {email}")
    if summary["errors"]:
        print("Errors:")
        for err in summary["errors"]:
            print(f"  {err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main())
