"""
Birling double-elimination bracket service.
Handles bracket generation and match progression for Birling events.
"""
import json
import math
from datetime import datetime, timezone

from database import db
from models import Event, EventResult


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
                'falls': [],
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
                    'falls': [],
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
            'loser': None,
            'falls': []
        }

        self.bracket_data['bracket']['true_finals'] = {
            'match_id': 'F2',
            'round': 'true_finals',
            'competitor1': None,
            'competitor2': None,
            'winner': None,
            'loser': None,
            'falls': [],
            'needed': False  # Only if losers champ beats winners champ
        }

        # Propagate bye winners forward into subsequent rounds
        self._propagate_byes()

        self._save_bracket_data()

    def _propagate_byes(self):
        """Advance bye winners from round 1 into their next-round match slots."""
        winners = self.bracket_data['bracket']['winners']
        first_round = winners[0] if winners else []
        for match in first_round:
            if match.get('is_bye') and match.get('winner') is not None:
                self._advance_winner(match)

    def _generate_losers_bracket(self, bracket_size: int):
        """Generate losers bracket structure for standard double elimination.

        For bracket_size B with log2(B) winners rounds, the losers bracket
        has 2*(log2(B)-1) rounds that alternate between two types:

          Odd rounds (L1, L3, L5, ...): "consolidation" — LB survivors
            play each other, halving the field.  L1 is special: W1 losers
            play each other.

          Even rounds (L2, L4, L6, ...): "drop-down" — winners bracket
            losers from W{r} enter and face LB survivors from the
            preceding consolidation round.

        Match counts halve every 2 rounds:
          L1-L2: B/4 matches each
          L3-L4: B/8 matches each
          L5-L6: B/16 matches each
          ...final pair: 1 match each
        """
        num_winners_rounds = int(math.log2(bracket_size))
        num_losers_rounds = 2 * (num_winners_rounds - 1)
        losers_rounds = []

        for lr in range(1, num_losers_rounds + 1):
            # Match count: halves every 2 rounds starting at B/4
            # Rounds 1-2: B/4, rounds 3-4: B/8, rounds 5-6: B/16, ...
            pair_index = (lr - 1) // 2  # 0 for L1-L2, 1 for L3-L4, etc.
            num_matches = max(1, bracket_size // (2 ** (pair_index + 2)))

            round_matches = []
            for i in range(num_matches):
                round_matches.append({
                    'match_id': f'L{lr}_{i+1}',
                    'round': f'losers_{lr}',
                    'competitor1': None,
                    'competitor2': None,
                    'winner': None,
                    'loser': None,
                    'falls': [],
                    'eliminated_position': None  # Will be set when they lose
                })

            losers_rounds.append(round_matches)

        self.bracket_data['bracket']['losers'] = losers_rounds

    def _get_lb_sources(self, lr, m):
        """Get the two source matches that feed losers bracket match L{lr}_{m}.

        Returns a list of up to 2 match dicts (or None for out-of-bounds).

        Source mapping:
          L1 (consolidation from W1): W1_{2m-1} and W1_{2m}
          Even lr (drop-down): L{lr-1}_{m} winner + W{(lr+2)//2}_{m} loser
          Odd lr>1 (consolidation): L{lr-1}_{2m-1} and L{lr-1}_{2m}
        """
        losers = self.bracket_data['bracket']['losers']
        winners = self.bracket_data['bracket']['winners']

        def _safe(lst, idx):
            return lst[idx] if 0 <= idx < len(lst) else None

        if lr == 1:
            w1 = winners[0] if winners else []
            return [_safe(w1, 2 * m - 2), _safe(w1, 2 * m - 1)]
        elif lr % 2 == 0:
            prev = losers[lr - 2] if (lr - 2) < len(losers) else []
            w_round_idx = (lr + 2) // 2 - 1
            w_round = winners[w_round_idx] if w_round_idx < len(winners) else []
            return [_safe(prev, m - 1), _safe(w_round, m - 1)]
        else:
            prev = losers[lr - 2] if (lr - 2) < len(losers) else []
            return [_safe(prev, 2 * m - 2), _safe(prev, 2 * m - 1)]

    def _sweep_losers_byes(self):
        """Auto-advance lone competitors in losers bracket matches where
        the opponent will never arrive (all source matches are decided or dead).

        A source match is considered "dead" (will never produce output) only if:
          - It is None (out of bounds)
          - It is decided (winner already set)
          - It is a W1 or L1 match with no competitors (structurally dead
            because W1 byes were resolved at generation time)

        For L2+ matches with no competitors: conservatively treat as live
        because they may still receive competitors from a W drop-down or
        LB advancement that hasn't happened yet.
        """
        changed = True
        while changed:
            changed = False
            losers = self.bracket_data['bracket']['losers']
            for round_idx, round_matches in enumerate(losers):
                for match in round_matches:
                    if match['winner'] is not None:
                        continue
                    c1, c2 = match['competitor1'], match['competitor2']
                    if (c1 is None) == (c2 is None):
                        continue  # both present or both absent — not a bye candidate

                    # One slot filled, one empty.  Check every source match.
                    lr = round_idx + 1
                    m = int(match['match_id'].split('_')[1])
                    sources = self._get_lb_sources(lr, m)
                    stalled = True
                    for src in sources:
                        if src is None:
                            continue
                        if src['winner'] is not None:
                            continue  # decided — already produced output
                        if (src['competitor1'] is not None
                                or src['competitor2'] is not None):
                            stalled = False  # has competitors — still in play
                            break
                        # No competitors, no winner. Structurally dead?
                        src_id = src.get('match_id', '')
                        if src_id.startswith('L1_') or src_id.startswith('W'):
                            continue  # L1/W with no competitors → dead
                        # L2+ with no competitors → might still receive them
                        stalled = False
                        break
                    if stalled:
                        winner = c1 if c1 is not None else c2
                        match['winner'] = winner
                        match['is_bye'] = True
                        self._advance_loser_winner(match)
                        changed = True

    def record_fall(self, match_id: str, fall_winner_id: int) -> dict:
        """Record a single fall in a best-of-3 birling match.

        Args:
            match_id: Match identifier (e.g., 'W1_1', 'L2_3', 'F1')
            fall_winner_id: ID of the competitor who won this fall

        Returns:
            Dict with match_id, falls, match_decided (bool), winner (id or None).
        """
        match = self._find_match(match_id)
        if not match:
            raise ValueError(f"Match {match_id} not found")

        # Validate the match is currently playable
        playable_ids = {m['match_id'] for m in self.get_current_matches()}
        if match_id not in playable_ids:
            raise ValueError(
                f"Match {match_id} is not currently playable. "
                "Complete earlier rounds first."
            )

        if match['winner'] is not None:
            raise ValueError(f"Match {match_id} already decided")

        if fall_winner_id not in (match['competitor1'], match['competitor2']):
            raise ValueError(
                f"Competitor {fall_winner_id} is not in match {match_id}"
            )

        falls = match.get('falls', [])
        if len(falls) >= 3:
            raise ValueError(f"Match {match_id} already has 3 falls recorded")

        falls.append({
            'fall_number': len(falls) + 1,
            'winner': fall_winner_id,
            'recorded_at': datetime.now(timezone.utc).isoformat(),
        })
        match['falls'] = falls

        # Check for best-of-3 resolution: first to 2 falls wins
        fall_counts = {}
        for f in falls:
            fall_counts[f['winner']] = fall_counts.get(f['winner'], 0) + 1

        decided = False
        winner = None
        for comp_id, count in fall_counts.items():
            if count >= 2:
                decided = True
                winner = comp_id
                break

        if decided:
            self.record_match_result(match_id, winner, _from_fall=True)

        if not decided:
            self._save_bracket_data()

        return {
            'match_id': match_id,
            'falls': falls,
            'match_decided': decided,
            'winner': winner,
        }

    def record_match_result(self, match_id: str, winner_id: int,
                            _from_fall: bool = False):
        """Record the result of a match.

        Args:
            match_id: Match identifier (e.g., 'W1_1', 'L2_3', 'F1')
            winner_id: ID of the winning competitor
            _from_fall: Internal flag — True when called from record_fall()
                to skip redundant validation. Do not set externally.
        """
        match = self._find_match(match_id)
        if not match:
            raise ValueError(f"Match {match_id} not found")

        if not _from_fall:
            # Validate the match is currently playable
            playable_ids = {m['match_id'] for m in self.get_current_matches()}
            if match_id not in playable_ids:
                raise ValueError(
                    f"Match {match_id} is not currently playable. "
                    "Complete earlier rounds first."
                )

        # Determine loser
        if match['competitor1'] == winner_id:
            loser_id = match['competitor2']
        elif match['competitor2'] == winner_id:
            loser_id = match['competitor1']
        else:
            raise ValueError(f"Winner {winner_id} not in match {match_id}")

        match['winner'] = winner_id
        match['loser'] = loser_id

        # If called directly (not from record_fall), retroactively set falls
        if not _from_fall:
            falls = match.get('falls', [])
            if not falls:
                now = datetime.now(timezone.utc).isoformat()
                match['falls'] = [
                    {'fall_number': 1, 'winner': winner_id, 'recorded_at': now},
                    {'fall_number': 2, 'winner': winner_id, 'recorded_at': now},
                ]

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

        # Auto-advance lone competitors in losers bracket whose opponent
        # will never arrive (source match was a bye or dead).
        self._sweep_losers_byes()

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
        """Advance winner to next winners bracket round or to grand finals."""
        match_id = match['match_id']  # e.g. 'W1_1'
        rest = match_id[1:]
        try:
            r, m = (int(x) for x in rest.split('_'))
        except (ValueError, AttributeError):
            return

        winner_id = match['winner']
        winners = self.bracket_data['bracket']['winners']

        # Next round is at index r (0-indexed W{r+1})
        if r >= len(winners):
            # Final winners round — champion goes to grand finals slot 1
            self.bracket_data['bracket']['finals']['competitor1'] = winner_id
            return

        next_round = winners[r]
        next_match_idx = (m - 1) // 2
        if next_match_idx >= len(next_round):
            next_match_idx = len(next_round) - 1
        target = next_round[next_match_idx]

        # Odd-numbered match → competitor1; even → competitor2
        if m % 2 == 1:
            target['competitor1'] = winner_id
        else:
            target['competitor2'] = winner_id

    def _drop_to_losers(self, match: dict):
        """Drop winners bracket loser to the appropriate losers bracket round.

        Mapping (r is 1-based winners round from match_id W{r}_{m}):
          W1 losers → L1 (round_idx 0): two losers per match, idx=(m-1)//2
          W2 losers → L2 (round_idx 1): one loser per match, idx=m-1
          W3 losers → L4 (round_idx 3): one loser per match, idx=m-1
          W4 losers → L6 (round_idx 5): one loser per match, idx=m-1
          General for r>=2: round_idx = 2*r - 3
        """
        match_id = match['match_id']  # e.g. 'W1_1'
        rest = match_id[1:]
        try:
            r, m = (int(x) for x in rest.split('_'))
        except (ValueError, AttributeError):
            return

        loser_id = match['loser']
        losers = self.bracket_data['bracket']['losers']

        if r == 1:
            target_round_idx = 0
            target_match_idx = (m - 1) // 2
        else:
            target_round_idx = 2 * r - 3
            target_match_idx = m - 1

        if not losers or target_round_idx >= len(losers):
            self._record_elimination(loser_id)
            return

        target_round = losers[target_round_idx]
        if not target_round:
            self._record_elimination(loser_id)
            return

        if target_match_idx >= len(target_round):
            target_match_idx = len(target_round) - 1
        target = target_round[target_match_idx]

        if target['competitor1'] is None:
            target['competitor1'] = loser_id
        elif target['competitor2'] is None:
            target['competitor2'] = loser_id

    def _advance_loser_winner(self, match: dict):
        """Advance losers bracket winner to the next losers round or grand finals.

        Match indexing alternates based on round type (r is 1-based):
          From odd round (consolidation) → even round (drop-down):
            Each survivor gets their own match slot: idx = m-1
          From even round (drop-down) → odd round (consolidation):
            Two survivors pair up: idx = (m-1) // 2
        """
        match_id = match['match_id']  # e.g. 'L1_1'
        rest = match_id[1:]
        try:
            r, m = (int(x) for x in rest.split('_'))
        except (ValueError, AttributeError):
            return

        winner_id = match['winner']
        losers = self.bracket_data['bracket']['losers']

        # Next losers round is at index r (0-indexed L{r+1})
        if r >= len(losers):
            # Final losers round — winner goes to grand finals slot 2
            self.bracket_data['bracket']['finals']['competitor2'] = winner_id
            return

        next_round = losers[r]

        # Odd rounds (consolidation) feed into even rounds (drop-down): 1-to-1
        # Even rounds (drop-down) feed into odd rounds (consolidation): 2-to-1
        if r % 2 == 1:
            next_match_idx = m - 1
        else:
            next_match_idx = (m - 1) // 2

        if next_match_idx >= len(next_round):
            next_match_idx = len(next_round) - 1
        target = next_round[next_match_idx]

        if target['competitor1'] is None:
            target['competitor1'] = winner_id
        elif target['competitor2'] is None:
            target['competitor2'] = winner_id

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

    def get_undoable_matches(self) -> set:
        """Return set of match_ids whose results can be undone.

        A decided match is undoable only if NO downstream match
        (the winner's next match or the loser's losers bracket match)
        has been decided yet.
        """
        undoable = set()
        all_decided = self._all_decided_matches()
        for match in all_decided:
            if match.get('is_bye'):
                continue  # byes cannot be undone
            mid = match['match_id']
            winner_next = self._get_next_match_for_winner(match)
            loser_next = self._get_next_match_for_loser(match)
            # Undoable if no downstream match has been decided
            if (winner_next is None or winner_next['winner'] is None) and \
               (loser_next is None or loser_next['winner'] is None):
                undoable.add(mid)
        return undoable

    def _all_decided_matches(self) -> list:
        """Return all matches with a winner set."""
        decided = []
        for rnd in self.bracket_data['bracket']['winners']:
            for m in rnd:
                if m['winner'] is not None:
                    decided.append(m)
        for rnd in self.bracket_data['bracket']['losers']:
            for m in rnd:
                if m['winner'] is not None:
                    decided.append(m)
        finals = self.bracket_data['bracket']['finals']
        if finals['winner'] is not None:
            decided.append(finals)
        tf = self.bracket_data['bracket']['true_finals']
        if tf['winner'] is not None:
            decided.append(tf)
        return decided

    def _get_next_match_for_winner(self, match):
        """Get the match the winner advanced into (or None)."""
        mid = match['match_id']
        if mid == 'F1' or mid == 'F2':
            return None  # finals produce placements, not advancement
        if mid.startswith('W'):
            rest = mid[1:]
            r, m = (int(x) for x in rest.split('_'))
            winners = self.bracket_data['bracket']['winners']
            if r >= len(winners):
                return self.bracket_data['bracket']['finals']
            next_idx = (m - 1) // 2
            next_rnd = winners[r]
            return next_rnd[min(next_idx, len(next_rnd) - 1)]
        if mid.startswith('L'):
            rest = mid[1:]
            r, m = (int(x) for x in rest.split('_'))
            losers = self.bracket_data['bracket']['losers']
            if r >= len(losers):
                return self.bracket_data['bracket']['finals']
            next_rnd = losers[r]
            if r % 2 == 1:
                next_idx = m - 1
            else:
                next_idx = (m - 1) // 2
            return next_rnd[min(next_idx, len(next_rnd) - 1)]
        return None

    def _get_next_match_for_loser(self, match):
        """Get the losers bracket match the loser dropped into (or None)."""
        mid = match['match_id']
        if not mid.startswith('W'):
            return None  # losers bracket losers are eliminated, not rerouted
        rest = mid[1:]
        r, m = (int(x) for x in rest.split('_'))
        losers = self.bracket_data['bracket']['losers']
        if r == 1:
            target_round_idx = 0
            target_match_idx = (m - 1) // 2
        else:
            target_round_idx = 2 * r - 3
            target_match_idx = m - 1
        if target_round_idx >= len(losers):
            return None
        target_rnd = losers[target_round_idx]
        if target_match_idx >= len(target_rnd):
            target_match_idx = len(target_rnd) - 1
        return target_rnd[target_match_idx]

    def undo_match_result(self, match_id: str) -> dict:
        """Undo a decided match result, returning both competitors to the match.

        Only allowed if no downstream match has been decided.

        Returns:
            Dict with match_id, undone (bool), message (str).

        Raises:
            ValueError if the match cannot be undone.
        """
        match = self._find_match(match_id)
        if not match:
            raise ValueError(f"Match {match_id} not found")
        if match['winner'] is None:
            raise ValueError(f"Match {match_id} has no result to undo")
        if match.get('is_bye'):
            raise ValueError(f"Match {match_id} is a bye and cannot be undone")

        undoable = self.get_undoable_matches()
        if match_id not in undoable:
            raise ValueError(
                f"Cannot undo match {match_id}: a downstream match has "
                "already been played. Reset the bracket to correct earlier rounds."
            )

        winner_id = match['winner']
        loser_id = match['loser']

        # Remove winner from next match slot
        winner_next = self._get_next_match_for_winner(match)
        if winner_next is not None:
            if winner_next['competitor1'] == winner_id:
                winner_next['competitor1'] = None
            elif winner_next['competitor2'] == winner_id:
                winner_next['competitor2'] = None
            # Also check finals
        if match_id.startswith('W'):
            r = int(match_id[1:].split('_')[0])
            winners = self.bracket_data['bracket']['winners']
            if r >= len(winners):
                finals = self.bracket_data['bracket']['finals']
                if finals['competitor1'] == winner_id:
                    finals['competitor1'] = None

        # Remove loser from losers bracket slot (W matches only)
        if match_id.startswith('W') and loser_id is not None:
            loser_next = self._get_next_match_for_loser(match)
            if loser_next is not None:
                if loser_next['competitor1'] == loser_id:
                    loser_next['competitor1'] = None
                elif loser_next['competitor2'] == loser_id:
                    loser_next['competitor2'] = None

        # Remove loser from losers bracket for L matches — undo elimination
        if match_id.startswith('L') and loser_id is not None:
            placements = self.bracket_data['placements']
            loser_key = str(loser_id)
            if loser_key in placements:
                del placements[loser_key]

        # Remove finals placements for F1/F2
        if match_id in ('F1', 'F2'):
            for cid in (winner_id, loser_id):
                if cid is not None:
                    cid_key = str(cid)
                    if cid_key in self.bracket_data['placements']:
                        del self.bracket_data['placements'][cid_key]
            if match_id == 'F1':
                tf = self.bracket_data['bracket']['true_finals']
                tf['needed'] = False
                tf['competitor1'] = None
                tf['competitor2'] = None

        # Clear the match result
        match['winner'] = None
        match['loser'] = None
        match['falls'] = []

        self._save_bracket_data()

        return {
            'match_id': match_id,
            'undone': True,
            'message': 'Match result cleared. Both competitors returned to this match.',
        }

    def finalize_to_event_results(self):
        """Write final placements and points to EventResult records.

        Points follow the standard placement table:
        1st=10, 2nd=7, 3rd=5, 4th=3, 5th=2, 6th=1. Positions 7+ get 0.
        """
        from decimal import Decimal

        from services.scoring_engine import PLACEMENT_POINTS_DECIMAL

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

            # Award placement points (1st=10 through 6th=1, 7th+=0)
            idx = position - 1
            if 0 <= idx < len(PLACEMENT_POINTS_DECIMAL):
                result.points_awarded = PLACEMENT_POINTS_DECIMAL[idx]
            else:
                result.points_awarded = Decimal('0')

        self.event.status = 'completed'
        db.session.commit()

        # Recalculate college team totals if this is a college event
        if self.event.event_type == 'college':
            try:
                from models.competitor import CollegeCompetitor
                from models.team import Team
                comp_ids = [int(k) for k in placements.keys()]
                team_ids = set()
                for comp in CollegeCompetitor.query.filter(
                        CollegeCompetitor.id.in_(comp_ids)).all():
                    if comp.team_id:
                        team_ids.add(comp.team_id)
                for team in Team.query.filter(Team.id.in_(list(team_ids))).all():
                    team.recalculate_points()
                db.session.commit()
            except Exception:
                pass  # non-blocking — team totals can be recalculated manually


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
