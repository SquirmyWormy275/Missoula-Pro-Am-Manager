"""
Standalone verification script for birling bracket losers bracket structure.

For each field size 4-16, generates a bracket and prints:
  - bracket_size (next power of 2)
  - byes
  - winners bracket rounds (matches per round)
  - losers bracket rounds (matches per round)
  - expected losers bracket rounds for standard double elimination

Standard double elimination losers bracket structure:
  For bracket_size B, the losers bracket has 2*(log2(B)-1) rounds.
  Rounds alternate:
    - "drop-down" rounds (losers from winners bracket enter)
    - "play-down" rounds (losers bracket survivors consolidate)

  For B=4:   LB rounds = 2*(2-1) = 2   → [1, 1]
  For B=8:   LB rounds = 2*(3-1) = 4   → [2, 2, 1, 1]
  For B=16:  LB rounds = 2*(4-1) = 6   → [4, 4, 2, 2, 1, 1]

Run:  python tests/verify_losers_bracket.py
"""

import math
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import MagicMock, patch


def mock_event(payouts="{}", event_type="college"):
    ev = MagicMock()
    ev.payouts = payouts
    ev.event_type = event_type
    ev.id = 1
    ev.status = "pending"
    return ev


def expected_losers_bracket(bracket_size):
    """
    Compute the correct losers bracket structure for standard double elimination.

    In standard double elimination for bracket_size B:
      - Winners bracket has log2(B) rounds
      - Losers bracket has 2*(log2(B) - 1) rounds

    The losers bracket alternates between two types of rounds:
      Round type A (odd rounds 1, 3, 5, ...): "drop-down" round
        - Losers from the corresponding winners round drop down and play
          against survivors from the previous losers round (or each other
          in round 1).
        - Match count = winners_round_losers / 2 for round 1,
          or = previous_round_survivors for subsequent A rounds.

      Round type B (even rounds 2, 4, 6, ...): "play-down" round
        - No new drop-downs. Losers bracket survivors play each other.
        - Match count halves.

    For B=4 (2 winners rounds):
      LB rounds: 2
      L1: 1 match (W1 losers play each other: 2 losers → 1 match)
      L2: 1 match (L1 winner vs W2 loser → 1 match)

    For B=8 (3 winners rounds):
      LB rounds: 4
      L1: 2 matches (W1 losers: 4 losers → 2 matches)
      L2: 2 matches (L1 winners [2] vs W2 losers [2] → 2 matches)
      L3: 1 match (L2 winners [2] play each other → 1 match)
      L4: 1 match (L3 winner vs W3 loser → 1 match)

    For B=16 (4 winners rounds):
      LB rounds: 6
      L1: 4 matches (W1 losers: 8 losers → 4 matches)
      L2: 4 matches (L1 winners [4] vs W2 losers [4] → 4 matches)
      L3: 2 matches (L2 winners [4] play each other → 2 matches)
      L4: 2 matches (L3 winners [2] vs W3 losers [2] → 2 matches)
      L5: 1 match  (L4 winners [2] play each other → 1 match)
      L6: 1 match  (L5 winner vs W4 loser → 1 match)
    """
    num_winners_rounds = int(math.log2(bracket_size))
    num_losers_rounds = 2 * (num_winners_rounds - 1)

    rounds = []
    # W1 produces bracket_size/2 losers
    survivors = bracket_size // 2  # losers from W1

    for lr in range(1, num_losers_rounds + 1):
        if lr == 1:
            # Round 1: W1 losers play each other
            matches = survivors // 2
            survivors = matches  # winners advance
        elif lr % 2 == 0:
            # Even round (drop-down): survivors from prev LB round vs
            # losers dropping from the corresponding winners round.
            # Which winners round drops here? W_round = (lr // 2) + 1
            w_round_idx = lr // 2  # 0-indexed winners round that drops losers
            # The number of losers dropping = matches in that winners round
            w_round_matches = bracket_size // (2 ** (w_round_idx + 1))
            # Each drop-down loser pairs with one LB survivor
            matches = min(survivors, w_round_matches)
            survivors = matches
        else:
            # Odd round > 1 (play-down): LB survivors play each other
            matches = survivors // 2
            survivors = matches

        rounds.append(matches)

    return rounds


def run_verification():
    from services.birling_bracket import BirlingBracket

    print("=" * 80)
    print("BIRLING BRACKET LOSERS BRACKET VERIFICATION")
    print("=" * 80)
    print()

    all_correct = True

    for n in range(4, 17):
        bracket_size = 2 ** math.ceil(math.log2(n))
        byes = bracket_size - n
        num_winners_rounds = int(math.log2(bracket_size))

        # Generate bracket using the actual service
        with patch("services.birling_bracket.db"):
            ev = mock_event()
            b = BirlingBracket(ev)
            comps = [{"id": i, "name": f"Comp{i}"} for i in range(1, n + 1)]
            b.generate_bracket(comps)

        # Extract actual structure
        winners = b.bracket_data["bracket"]["winners"]
        losers = b.bracket_data["bracket"]["losers"]

        actual_winners = [len(r) for r in winners]
        actual_losers = [len(r) for r in losers]
        expected = expected_losers_bracket(bracket_size)

        match = actual_losers == expected
        status = "OK" if match else "MISMATCH"
        if not match:
            all_correct = False

        print(f"N={n:2d}  bracket_size={bracket_size:2d}  byes={byes}")
        print(f"  Winners rounds: {actual_winners}")
        print(f"  Losers actual:  {actual_losers}")
        print(f"  Losers expected:{expected}")
        print(f"  Status: {status}")

        if not match:
            print(
                f"  >>> PROBLEM: Expected {len(expected)} losers rounds, "
                f"got {len(actual_losers)}"
            )

        # Also trace a W1_1 loser drop-down path
        w1 = winners[0] if winners else []
        if w1:
            m1 = w1[0]
            print(f"  W1_1 loser drop target: losers[0][(1-1)//2] = losers[0][0]")
            if losers:
                target = losers[0][0] if losers[0] else None
                print(f"    Target match: {target['match_id'] if target else 'NONE'}")

        print()

    print("=" * 80)
    if all_correct:
        print("ALL FIELD SIZES CORRECT")
    else:
        print("MISMATCHES FOUND — losers bracket structure needs fixing")
    print("=" * 80)


if __name__ == "__main__":
    run_verification()
