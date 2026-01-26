"""
Partnered Axe Throw prelims/finals management.

The Partnered Axe Throw event has a unique format:
1. All pairs compete in prelims (hits-based scoring)
2. Top 4 pairs advance to finals
3. Finals determine final placements

Each pair consists of two competitors throwing at the same target,
alternating throws. Score is total hits combined.
"""
from database import db
from models import Event, Heat, HeatAssignment, EventResult
from models.competitor import ProCompetitor
import json


class PartneredAxeThrow:
    """Manages the Partnered Axe Throw prelims/finals flow."""

    def __init__(self, event: Event):
        self.event = event
        self._load_state()

    def _load_state(self) -> dict:
        """Load partnered axe state from event payouts field."""
        try:
            self.state = json.loads(self.event.payouts or '{}')
        except:
            self.state = {}

        if 'stage' not in self.state:
            self.state = {
                'stage': 'prelims',  # prelims, finals, completed
                'prelim_results': [],
                'finalists': [],
                'final_results': [],
                'pairs': []
            }

        return self.state

    def _save_state(self):
        """Save state to event."""
        self.event.payouts = json.dumps(self.state)
        db.session.commit()

    def get_stage(self) -> str:
        """Get current stage: prelims, finals, or completed."""
        return self.state.get('stage', 'prelims')

    def get_pairs(self) -> list:
        """Get all registered pairs."""
        return self.state.get('pairs', [])

    def register_pair(self, competitor1_id: int, competitor2_id: int) -> dict:
        """
        Register a pair for the event.

        Returns pair info dict.
        """
        comp1 = ProCompetitor.query.get(competitor1_id)
        comp2 = ProCompetitor.query.get(competitor2_id)

        if not comp1 or not comp2:
            raise ValueError("One or both competitors not found")

        pair = {
            'pair_id': len(self.state['pairs']) + 1,
            'competitor1': {
                'id': comp1.id,
                'name': comp1.name
            },
            'competitor2': {
                'id': comp2.id,
                'name': comp2.name
            },
            'prelim_score': None,
            'final_score': None,
            'final_position': None
        }

        self.state['pairs'].append(pair)
        self._save_state()

        return pair

    def record_prelim_result(self, pair_id: int, hits: int):
        """
        Record a pair's prelim result.

        Args:
            pair_id: The pair ID
            hits: Total hits scored by the pair
        """
        for pair in self.state['pairs']:
            if pair['pair_id'] == pair_id:
                pair['prelim_score'] = hits
                break

        # Update prelim_results sorted by score (descending)
        self.state['prelim_results'] = sorted(
            [p for p in self.state['pairs'] if p['prelim_score'] is not None],
            key=lambda x: x['prelim_score'],
            reverse=True
        )

        self._save_state()

    def get_prelim_standings(self) -> list:
        """Get prelim standings sorted by score (highest first)."""
        pairs_with_scores = [p for p in self.state['pairs'] if p['prelim_score'] is not None]
        return sorted(pairs_with_scores, key=lambda x: x['prelim_score'], reverse=True)

    def can_advance_to_finals(self) -> bool:
        """Check if we have enough results to advance to finals."""
        scored = [p for p in self.state['pairs'] if p['prelim_score'] is not None]
        return len(scored) >= 4 and len(scored) == len(self.state['pairs'])

    def advance_to_finals(self) -> list:
        """
        Advance top 4 pairs to finals.

        Returns list of finalist pairs.
        """
        if not self.can_advance_to_finals():
            raise ValueError("Cannot advance to finals - not all prelim results recorded")

        standings = self.get_prelim_standings()
        self.state['finalists'] = standings[:4]
        self.state['stage'] = 'finals'
        self._save_state()

        return self.state['finalists']

    def get_finalists(self) -> list:
        """Get the finalist pairs."""
        return self.state.get('finalists', [])

    def record_final_result(self, pair_id: int, hits: int):
        """
        Record a pair's final result.

        Args:
            pair_id: The pair ID
            hits: Total hits scored in finals
        """
        for pair in self.state['finalists']:
            if pair['pair_id'] == pair_id:
                pair['final_score'] = hits
                break

        # Check if all finals complete
        all_scored = all(p.get('final_score') is not None for p in self.state['finalists'])

        if all_scored:
            # Sort by final score and assign positions
            sorted_finals = sorted(
                self.state['finalists'],
                key=lambda x: x['final_score'],
                reverse=True
            )

            for position, pair in enumerate(sorted_finals, 1):
                pair['final_position'] = position
                # Update in main pairs list too
                for main_pair in self.state['pairs']:
                    if main_pair['pair_id'] == pair['pair_id']:
                        main_pair['final_position'] = position
                        main_pair['final_score'] = pair['final_score']

            self.state['finalists'] = sorted_finals
            self.state['final_results'] = sorted_finals
            self.state['stage'] = 'completed'

            # Save results to EventResult table
            self._save_event_results()

        self._save_state()

    def _save_event_results(self):
        """Save final results to EventResult table."""
        for pair in self.state['finalists']:
            # Create result for each competitor in the pair
            for competitor_key in ['competitor1', 'competitor2']:
                competitor = pair[competitor_key]

                result = EventResult(
                    event_id=self.event.id,
                    competitor_type='pro',
                    competitor_id=competitor['id'],
                    competitor_name=competitor['name'],
                    result_value=pair['final_score'],
                    final_position=pair['final_position']
                )
                db.session.add(result)

        db.session.commit()

    def get_final_standings(self) -> list:
        """Get final standings (only available after finals complete)."""
        if self.state['stage'] != 'completed':
            return []
        return self.state.get('final_results', [])

    def get_full_standings(self) -> list:
        """
        Get full standings combining prelims and finals.

        Returns:
            List of all pairs with their positions:
            - 1st-4th from finals
            - 5th+ from prelim standings
        """
        finalists_ids = {p['pair_id'] for p in self.state.get('finalists', [])}
        prelim_standings = self.get_prelim_standings()

        # Start with final results
        results = list(self.state.get('final_results', []))

        # Add non-finalists from prelims
        position = 5
        for pair in prelim_standings:
            if pair['pair_id'] not in finalists_ids:
                pair_copy = dict(pair)
                pair_copy['final_position'] = position
                results.append(pair_copy)
                position += 1

        return results

    def reset(self):
        """Reset the event to initial state."""
        self.state = {
            'stage': 'prelims',
            'prelim_results': [],
            'finalists': [],
            'final_results': [],
            'pairs': []
        }
        self._save_state()


def get_partnered_axe_throw(tournament_id: int) -> PartneredAxeThrow:
    """Get the Partnered Axe Throw manager for a tournament."""
    event = Event.query.filter_by(
        tournament_id=tournament_id,
        name='Partnered Axe Throw'
    ).first()

    if not event:
        from models import Tournament
        tournament = Tournament.query.get(tournament_id)
        event = Event(
            tournament_id=tournament_id,
            name='Partnered Axe Throw',
            event_type='pro',
            scoring_type='hits',
            is_partnered=True,
            status='pending'
        )
        db.session.add(event)
        db.session.commit()

    return PartneredAxeThrow(event)
