"""
Birling double-elimination bracket service.
Handles bracket generation and match progression for Birling events.
"""
from database import db
from models import Event, EventResult
import math
import json


class BirlingBracket:
    """Manages a double-elimination bracket for Birling."""

    def __init__(self, event: Event):
        self.event = event
        self.bracket_data = self._load_bracket_data()

    def _load_bracket_data(self) -> dict:
        """Load bracket data from event or create new."""
        # Store bracket in event's payouts field (repurposed for bracket events)
        try:
            data = json.loads(self.event.payouts or '{}')
            if 'bracket' in data:
                return data
        except:
            pass

        return {
            'bracket': {
                'winners': [],  # Winners bracket matches
                'losers': [],   # Losers bracket matches
                'finals': None,  # Grand finals
                'true_finals': None  # True finals if needed
            },
            'competitors': [],
            'seeding': [],
            'current_round': 'winners_1',
            'placements': {}  # competitor_id -> final_position
        }

    def _save_bracket_data(self):
        """Save bracket data to event."""
        self.event.payouts = json.dumps(self.bracket_data)
        db.session.commit()

    def generate_bracket(self, competitors: list, seeding: list = None):
        """
        Generate a double-elimination bracket.

        Args:
            competitors: List of competitor dicts with 'id' and 'name'
            seeding: Optional list of competitor IDs in seed order (1st seed first)
        """
        num_competitors = len(competitors)

        if num_competitors < 2:
            raise ValueError("Need at least 2 competitors for a bracket")

        # Store competitors
        self.bracket_data['competitors'] = competitors

        # Apply seeding or use provided order
        if seeding:
            self.bracket_data['seeding'] = seeding
        else:
            self.bracket_data['seeding'] = [c['id'] for c in competitors]

        # Calculate bracket size (next power of 2)
        bracket_size = 2 ** math.ceil(math.log2(num_competitors))
        num_byes = bracket_size - num_competitors

        # Generate first round pairings (1 vs N, 2 vs N-1, etc.)
        seeded = self.bracket_data['seeding']
        pairings = []

        for i in range(bracket_size // 2):
            seed1 = i
            seed2 = bracket_size - 1 - i

            comp1 = seeded[seed1] if seed1 < len(seeded) else None  # BYE
            comp2 = seeded[seed2] if seed2 < len(seeded) else None  # BYE

            pairings.append({
                'match_id': f'W1_{i+1}',
                'round': 'winners_1',
                'competitor1': comp1,
                'competitor2': comp2,
                'winner': None,
                'loser': None,
                'is_bye': comp1 is None or comp2 is None
            })

            # Handle byes - auto-advance
            if comp1 is None and comp2 is not None:
                pairings[-1]['winner'] = comp2
            elif comp2 is None and comp1 is not None:
                pairings[-1]['winner'] = comp1

        self.bracket_data['bracket']['winners'] = [pairings]

        # Generate subsequent winners bracket rounds
        matches_in_round = len(pairings) // 2
        round_num = 2

        while matches_in_round >= 1:
            round_matches = []
            for i in range(matches_in_round):
                round_matches.append({
                    'match_id': f'W{round_num}_{i+1}',
                    'round': f'winners_{round_num}',
                    'competitor1': None,  # TBD from previous round
                    'competitor2': None,
                    'winner': None,
                    'loser': None,
                    'is_bye': False
                })
            self.bracket_data['bracket']['winners'].append(round_matches)
            matches_in_round //= 2
            round_num += 1

        # Generate losers bracket (more complex structure)
        self._generate_losers_bracket(bracket_size)

        # Generate finals
        self.bracket_data['bracket']['finals'] = {
            'match_id': 'F1',
            'round': 'finals',
            'competitor1': None,  # Winners bracket champion
            'competitor2': None,  # Losers bracket champion
            'winner': None,
            'loser': None
        }

        self.bracket_data['bracket']['true_finals'] = {
            'match_id': 'F2',
            'round': 'true_finals',
            'competitor1': None,
            'competitor2': None,
            'winner': None,
            'loser': None,
            'needed': False  # Only if losers champ beats winners champ
        }

        self._save_bracket_data()

    def _generate_losers_bracket(self, bracket_size: int):
        """Generate losers bracket structure."""
        losers_rounds = []

        # Losers bracket has roughly 2x the rounds of winners
        # Each winners round feeds losers into the losers bracket
        winners_rounds = len(self.bracket_data['bracket']['winners'])

        for w_round in range(winners_rounds):
            # Losers from winners round W drop down
            # Then play against each other or survivors from previous losers round

            if w_round == 0:
                # First losers round: losers from W1 play each other
                num_matches = bracket_size // 4
            else:
                # Subsequent rounds alternate between:
                # - Playing losers dropping from winners
                # - Playing within losers bracket
                num_matches = max(1, bracket_size // (2 ** (w_round + 2)))

            round_matches = []
            for i in range(num_matches):
                round_matches.append({
                    'match_id': f'L{len(losers_rounds)+1}_{i+1}',
                    'round': f'losers_{len(losers_rounds)+1}',
                    'competitor1': None,
                    'competitor2': None,
                    'winner': None,
                    'loser': None,
                    'eliminated_position': None  # Will be set when they lose
                })

            if round_matches:
                losers_rounds.append(round_matches)

        self.bracket_data['bracket']['losers'] = losers_rounds

    def record_match_result(self, match_id: str, winner_id: int):
        """
        Record the result of a match.

        Args:
            match_id: Match identifier (e.g., 'W1_1', 'L2_3', 'F1')
            winner_id: ID of the winning competitor
        """
        match = self._find_match(match_id)
        if not match:
            raise ValueError(f"Match {match_id} not found")

        # Determine loser
        if match['competitor1'] == winner_id:
            loser_id = match['competitor2']
        elif match['competitor2'] == winner_id:
            loser_id = match['competitor1']
        else:
            raise ValueError(f"Winner {winner_id} not in match {match_id}")

        match['winner'] = winner_id
        match['loser'] = loser_id

        # Handle advancement
        if match_id.startswith('W'):
            # Winner advances in winners bracket
            self._advance_winner(match)
            # Loser drops to losers bracket
            self._drop_to_losers(match)
        elif match_id.startswith('L'):
            # Winner advances in losers bracket
            self._advance_loser_winner(match)
            # Loser is eliminated - record placement
            self._record_elimination(loser_id)
        elif match_id == 'F1':
            # Finals
            if winner_id == match['competitor2']:
                # Losers champ won - need true finals
                self.bracket_data['bracket']['true_finals']['needed'] = True
                self.bracket_data['bracket']['true_finals']['competitor1'] = match['competitor1']
                self.bracket_data['bracket']['true_finals']['competitor2'] = match['competitor2']
            else:
                # Winners champ won - they're the champion
                self.bracket_data['placements'][str(winner_id)] = 1
                self.bracket_data['placements'][str(loser_id)] = 2
        elif match_id == 'F2':
            # True finals
            self.bracket_data['placements'][str(winner_id)] = 1
            self.bracket_data['placements'][str(loser_id)] = 2

        self._save_bracket_data()

    def _find_match(self, match_id: str) -> dict:
        """Find a match by ID."""
        # Search winners bracket
        for round_matches in self.bracket_data['bracket']['winners']:
            for match in round_matches:
                if match['match_id'] == match_id:
                    return match

        # Search losers bracket
        for round_matches in self.bracket_data['bracket']['losers']:
            for match in round_matches:
                if match['match_id'] == match_id:
                    return match

        # Check finals
        if self.bracket_data['bracket']['finals']['match_id'] == match_id:
            return self.bracket_data['bracket']['finals']
        if self.bracket_data['bracket']['true_finals']['match_id'] == match_id:
            return self.bracket_data['bracket']['true_finals']

        return None

    def _advance_winner(self, match: dict):
        """Advance winner to next winners bracket round."""
        # Find next round match and slot winner in
        pass  # Implementation depends on bracket structure

    def _drop_to_losers(self, match: dict):
        """Drop loser to losers bracket."""
        # Find appropriate losers bracket match
        pass  # Implementation depends on bracket structure

    def _advance_loser_winner(self, match: dict):
        """Advance winner in losers bracket."""
        pass  # Implementation depends on bracket structure

    def _record_elimination(self, competitor_id: int):
        """Record a competitor's elimination and final placement."""
        # Count how many are already eliminated
        current_eliminations = len(self.bracket_data['placements'])
        total_competitors = len(self.bracket_data['competitors'])

        # Position is total - eliminations (so first eliminated is last place)
        position = total_competitors - current_eliminations
        self.bracket_data['placements'][str(competitor_id)] = position

    def get_current_matches(self) -> list:
        """Get matches that are ready to be played."""
        ready = []

        # Check all brackets for matches with both competitors but no winner
        for round_matches in self.bracket_data['bracket']['winners']:
            for match in round_matches:
                if (match['competitor1'] is not None and
                    match['competitor2'] is not None and
                    match['winner'] is None and
                    not match.get('is_bye', False)):
                    ready.append(match)

        for round_matches in self.bracket_data['bracket']['losers']:
            for match in round_matches:
                if (match['competitor1'] is not None and
                    match['competitor2'] is not None and
                    match['winner'] is None):
                    ready.append(match)

        # Check finals
        finals = self.bracket_data['bracket']['finals']
        if (finals['competitor1'] is not None and
            finals['competitor2'] is not None and
            finals['winner'] is None):
            ready.append(finals)

        true_finals = self.bracket_data['bracket']['true_finals']
        if (true_finals.get('needed', False) and
            true_finals['competitor1'] is not None and
            true_finals['competitor2'] is not None and
            true_finals['winner'] is None):
            ready.append(true_finals)

        return ready

    def get_placements(self) -> dict:
        """Get final placements (1st through 6th for Birling)."""
        return self.bracket_data['placements']

    def finalize_to_event_results(self):
        """Write final placements to EventResult records."""
        placements = self.get_placements()
        competitors = {c['id']: c for c in self.bracket_data['competitors']}

        for comp_id_str, position in placements.items():
            comp_id = int(comp_id_str)
            comp = competitors.get(comp_id, {})

            result = EventResult.query.filter_by(
                event_id=self.event.id,
                competitor_id=comp_id
            ).first()

            if not result:
                result = EventResult(
                    event_id=self.event.id,
                    competitor_id=comp_id,
                    competitor_type=self.event.event_type,
                    competitor_name=comp.get('name', 'Unknown')
                )
                db.session.add(result)

            result.final_position = position
            result.status = 'completed'

        self.event.status = 'completed'
        db.session.commit()


def create_birling_bracket(event: Event, competitors: list, seeding: list = None):
    """
    Create a new Birling bracket for an event.

    Args:
        event: Birling event
        competitors: List of competitor dicts
        seeding: Optional seeding order

    Returns:
        BirlingBracket instance
    """
    bracket = BirlingBracket(event)
    bracket.generate_bracket(competitors, seeding)
    return bracket


def get_birling_bracket(event: Event) -> BirlingBracket:
    """Get existing Birling bracket for an event."""
    return BirlingBracket(event)
