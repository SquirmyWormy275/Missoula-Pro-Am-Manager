"""Shared operator feedback for Saturday college spillover integration."""
from flask import flash


def spillover_result_payload(result: dict | None) -> dict:
    """Return a detached payload without dropping planner diagnostics."""
    return dict(result or {})


def flash_spillover_result(result: dict | None, *, include_success: bool = True) -> None:
    """Surface spillover changes and any required judge review."""
    payload = spillover_result_payload(result)
    integrated = int(payload.get('integrated_heats') or 0)
    ignored_ids = list(payload.get('ignored_non_college_event_ids') or [])
    conflicts = list(payload.get('unavoidable_stand_conflicts') or [])

    if integrated and include_success:
        flash(
            f'Integrated {integrated} college spillover heat(s) into Saturday flights.',
            'success',
        )
    if ignored_ids:
        flash(
            f'Ignored {len(ignored_ids)} selected event(s) because Saturday '
            'spillover accepts college events only.',
            'warning',
        )
    if conflicts:
        flash(
            f'Spillover placement requires judge review: {len(conflicts)} '
            'shared-stand conflict(s) could not be avoided without violating '
            'competitor spacing. Run Preflight Check before printing the '
            'Saturday schedule.',
            'warning',
        )
    if not integrated and not ignored_ids and not conflicts and payload.get('message'):
        flash(str(payload['message']), 'info')
