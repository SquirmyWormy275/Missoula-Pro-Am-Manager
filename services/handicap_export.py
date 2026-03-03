"""
Shared helpers for chopping-focused exports used by handicap tooling.
"""
from __future__ import annotations

import re
import pandas as pd
from models import Event, Tournament

_CHOPPING_KEYWORDS = (
    'underhand',
    'standing block',
    'springboard',
    '1-board',
    '3-board',
    'jigger',
)


def _normalized(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', str(value or '').lower()).strip()


def is_chopping_event(event: Event) -> bool:
    name = _normalized(event.name)
    return any(keyword in name for keyword in _CHOPPING_KEYWORDS)


def build_chopping_rows(tournament: Tournament) -> list[dict]:
    rows: list[dict] = []
    for event in tournament.events.order_by(Event.event_type, Event.name, Event.gender).all():
        if not is_chopping_event(event):
            continue
        for result in event.get_results_sorted():
            rows.append({
                'tournament_id': tournament.id,
                'tournament_name': tournament.name,
                'year': tournament.year,
                'event_id': event.id,
                'event_name': event.display_name,
                'event_type': event.event_type,
                'gender': event.gender or '',
                'scoring_type': event.scoring_type,
                'scoring_order': event.scoring_order,
                'competitor_id': result.competitor_id,
                'competitor_type': result.competitor_type,
                'competitor_name': result.competitor_name,
                'status': result.status,
                'run1_value': result.run1_value,
                'run2_value': result.run2_value,
                'best_run': result.best_run,
                'result_value': result.result_value,
                'final_position': result.final_position,
                'points_awarded': result.points_awarded,
                'payout_amount': result.payout_amount,
            })
    return rows


def export_chopping_results_to_excel(tournament: Tournament, filepath: str) -> None:
    rows = build_chopping_rows(tournament)
    with pd.ExcelWriter(filepath, engine='openpyxl') as writer:
        pd.DataFrame(rows).to_excel(writer, sheet_name='Chopping Results', index=False)

