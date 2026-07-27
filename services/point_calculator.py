"""
DEPRECATED — Point Calculator Service.

All logic has been consolidated into services/scoring_engine.py.
This module re-exports the surviving public functions so existing call
sites continue to work without modification.  Do not add new code here.

Removal plan: update all import sites to use scoring_engine directly,
then delete this file.
"""
from services.scoring_engine import (
    get_individual_standings,
    get_team_standings,
    recalculate_all_team_points,
)

__all__ = [
    'get_individual_standings',
    'get_team_standings',
    'recalculate_all_team_points',
]
