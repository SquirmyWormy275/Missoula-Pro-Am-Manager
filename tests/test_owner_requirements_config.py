"""Executable checks for unambiguous owner-authored event rules."""

from config import COLLEGE_CLOSED_EVENTS, PRO_EVENTS


def _event(events, name):
    return next(event for event in events if event['name'] == name)


def test_obstacle_pole_run_counts_match_owner_requirements():
    """College runs both courses; Pro is explicitly allotted one run."""
    college = _event(COLLEGE_CLOSED_EVENTS, 'Obstacle Pole')
    pro = _event(PRO_EVENTS, 'Obstacle Pole')

    assert college['requires_dual_runs'] is True
    assert pro.get('requires_dual_runs', False) is False
