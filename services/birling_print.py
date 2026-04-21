"""
Birling blank bracket print context builder.

Produces a deep-copied, result-scrubbed view of a birling bracket so the
print template can render the round-1 matchups (and empty TBD slots beyond)
without leaking winners / losers / placements / fall history from a
partially-played bracket.  Used by the show-prep workflow: judges get a
printable bracket sheet to fill in by hand, then results are entered into
the system after the bracket runs.

Design constraints (from docs/VIDEO_JUDGE_BRACKET_PLAN.md, 2026-04-20):
    - Deep copy.  The live Event.payouts must never be mutated.
    - Strip winner / loser / falls / placements from EVERY match.
    - Rounds 2+ of winners and losers brackets lose all competitor slots
      (they were only populated because rounds advanced).
    - Finals and true_finals go completely blank.
    - Round 1 winners bracket keeps its seeded competitors intact —
      that's the whole point of printing the seeded bracket.
"""

from __future__ import annotations

from copy import deepcopy

from models import Event


def build_birling_print_context(event: Event) -> dict | None:
    """Return a scrubbed, deep-copied bracket dict ready for printing.

    Args:
        event: The bracket event (must have scoring_type == 'bracket').

    Returns:
        A dict with keys 'bracket' (scrubbed rounds + finals), 'competitors'
        (seeded list preserved for display name lookup), and 'comp_lookup'
        (str(id) -> name) for easy template rendering.  None if the bracket
        has not been generated yet (caller should flash + redirect).
    """
    if event is None or event.scoring_type != "bracket":
        return None

    try:
        from services.birling_bracket import BirlingBracket
    except ImportError:
        return None

    bb = BirlingBracket(event)
    data = bb.bracket_data
    winners = data.get("bracket", {}).get("winners") or []
    if not winners:
        # No bracket generated yet — nothing meaningful to print.
        return None

    # Deep copy every branch we'll mutate — the live Event.payouts stays untouched.
    scrubbed = {
        "bracket": {
            "winners": deepcopy(winners),
            "losers": deepcopy(data.get("bracket", {}).get("losers") or []),
            "finals": deepcopy(data.get("bracket", {}).get("finals") or {}),
            "true_finals": deepcopy(data.get("bracket", {}).get("true_finals") or {}),
        },
        "competitors": deepcopy(data.get("competitors") or []),
        "seeding": deepcopy(data.get("seeding") or []),
    }

    # --- Winners bracket ---
    # Round 1 keeps competitors (that's what we're printing).  Strip result
    # fields only: winner, loser, falls, is_bye flag stays so TBD byes still
    # show.  Rounds 2+ go back to all-TBD.
    for round_idx, round_matches in enumerate(scrubbed["bracket"]["winners"]):
        for match in round_matches:
            match["winner"] = None
            match["loser"] = None
            match["falls"] = []
            if round_idx > 0:
                match["competitor1"] = None
                match["competitor2"] = None

    # --- Losers bracket ---
    # All slots go TBD.  Losers were only populated when winners advanced,
    # so scrubbing results + resetting slots gives an empty printable skeleton.
    for round_matches in scrubbed["bracket"]["losers"]:
        for match in round_matches:
            match["winner"] = None
            match["loser"] = None
            match["falls"] = []
            match["competitor1"] = None
            match["competitor2"] = None
            match["eliminated_position"] = None

    # --- Finals + true_finals ---
    for key in ("finals", "true_finals"):
        match = scrubbed["bracket"][key]
        if match:
            match["competitor1"] = None
            match["competitor2"] = None
            match["winner"] = None
            match["loser"] = None
            match["falls"] = []
            if key == "true_finals":
                match["needed"] = False

    scrubbed["comp_lookup"] = {
        str(c.get("id")): c.get("name", f"ID:{c.get('id')}")
        for c in scrubbed["competitors"]
    }
    return scrubbed
