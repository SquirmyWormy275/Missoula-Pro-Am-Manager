"""
ProEventRank model for ability-based heat grouping in the pro division.

Stores a judge-assigned ability rank for a competitor within a specific event
category, scoped to a tournament. Rank 1 = best; higher numbers = weaker.

Event categories:
  'springboard'     — covers Springboard, Pro 1-Board, 3-Board Jigger
  'underhand'       — Underhand Butcher Block
  'standing_block'  — Standing Block (Speed and Hard Hit)
  'obstacle_pole'   — Obstacle Pole
  'singlebuck'      — Single Buck
  'doublebuck'      — Double Buck (same-gender partnered)
  'jack_jill'       — Jack & Jill Sawing (mixed-gender partnered)

Competitors with no rank record for a category are treated as unranked and
placed after all ranked competitors before the snake draft begins.
"""
# Re-export constants from the canonical location so existing imports from this module
# continue to work while routes/services can also import directly from config.
from config import (  # noqa: F401
    CATEGORY_DESCRIPTIONS,
    CATEGORY_DISPLAY_NAMES,
    RANKED_CATEGORIES,
)
from database import db


class ProEventRank(db.Model):
    """Per-tournament, per-category ability rank for a pro competitor."""

    __tablename__ = 'pro_event_ranks'

    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=False)
    competitor_id = db.Column(db.Integer, db.ForeignKey('pro_competitors.id'), nullable=False)

    # One of the 7 ranked categories defined in this module's docstring.
    event_category = db.Column(db.String(32), nullable=False)

    # 1 = best competitor; higher = weaker. Must be >= 1.
    rank = db.Column(db.Integer, nullable=False)

    __table_args__ = (
        db.UniqueConstraint(
            'tournament_id', 'competitor_id', 'event_category',
            name='uq_pro_event_rank_tournament_comp_cat'
        ),
    )

    def __repr__(self):
        return (
            f'<ProEventRank t={self.tournament_id} c={self.competitor_id}'
            f' cat={self.event_category} rank={self.rank}>'
        )


# RANKED_CATEGORIES, CATEGORY_DISPLAY_NAMES, and CATEGORY_DESCRIPTIONS are defined in
# config.py and re-exported at the top of this module for backward compatibility.
