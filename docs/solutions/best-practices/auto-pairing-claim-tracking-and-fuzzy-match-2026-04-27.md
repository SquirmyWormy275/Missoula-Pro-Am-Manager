---
title: "Auto-pairing partners: claim-tracking + fuzzy-match ladder"
date: 2026-04-27
problem_type: best_practice
category: best-practices
module: services/partner_matching
component: service_object
severity: high
related_components:
  - services/name_match
  - routes/registration
tags:
  - partner-matching
  - fuzzy-matching
  - claim-tracking
  - levenshtein
  - auto-assignment
  - reciprocity
  - data-quality
applies_when:
  - "Building auto-assignment systems where users self-declare a counterpart by free-text name"
  - "Free-text identifier fields are prone to typos, casing, and asymmetric formatting (first vs full name)"
  - "Reciprocity matters — A claiming B must not steal B from someone else who also claimed B"
  - "An operator needs actionable triage of unresolved cases, not silent drops or bad auto-pairs"
related_docs:
  - docs/solutions/data-integrity/preflight-gear-sharing-using-prefix-false-positives-2026-04-21.md
  - docs/solutions/test-failures/test-shape-matches-bug-shape-trilogy-2026-04-23.md
  - docs/solutions/test-failures/hand-written-fixture-shape-divergence-2026-04-22.md
---

# Auto-pairing partners: claim-tracking + fuzzy-match ladder

## Context

Race-weekend partner pairing on the Missoula Pro-Am Manager kept failing in three distinct ways that all looked like the same bug to the operator:

1. **Typo'd partner names.** Free-text entry on registration forms produced `McKinley` vs `Mickinley`, `Elise` vs `Eloise`, `Kayla` vs `Kaylah`. A plain normalized-name lookup missed every one of these.
2. **Asymmetric-length partner strings.** One side wrote a full name (`Mickinley Verhulst`), the other side wrote a first-name-only typo (`McKinley`). Full-name Levenshtein distance was 9, blowing past the ≤2 cap, so the V2.14.10 ladder still failed (V2.14.11 fix).
3. **One-sided claims silently corrupting unrelated pairings.** A entered B as their partner. B's partner field was blank. The auto-pair pass cheerfully threw B into the unclaimed pool and mated B with random C, leaving A "unmatched" and the operator manually unwinding two broken partnerships at race time.

The fix shipped in three increments — V2.14.10 (extracted shared `services/name_match.py`), V2.14.11 (added Tier 4 first-token fuzzy for asymmetric strings), V2.14.16 (the resolver rewrite with claim tracking). Each step came from a separate race-weekend incident (session history). The V2.14.16 directive came verbatim from the operator after reviewing the Domain Conflict Review Board: *"BE SURE to write something that checks if someone else has already claimed a partner before you throw them into the unpaired pool."*

## Guidance

Build the resolver as **two layers**: a stateless matching ladder, and a stateful 3-phase resolver that uses the ladder.

### Layer 1 — 4-tier matching ladder

```python
# services/name_match.py
from collections.abc import Callable, Sequence
from typing import TypeVar

MAX_FUZZY_DISTANCE = 2

T = TypeVar("T")  # any roster entry — ORM row, dict, namedtuple, etc.


def find_partner_match(
    partner_name: str,
    pool: Sequence[T],
    name_getter: Callable[[T], str],
    exclude_key: int | None = None,
    *,
    enable_fuzzy: bool = True,
) -> T | None:
    target_full = normalize_alphanum(partner_name)         # alphanumeric lowercased
    target_first = first_token(partner_name)               # first whitespace token

    # Build (entry, normalized_full, first_token) tuples once. Re-walked per
    # tier so the helper stays inline-readable; pool sizes are < 200 entrants.
    entries = [
        (e, normalize_alphanum(name_getter(e)), first_token(name_getter(e)))
        for e in pool
        if getattr(e, "id", None) != exclude_key
    ]

    # Tier 1: exact normalized full-name match.
    for entry, norm, _first in entries:
        if norm == target_full:
            return entry

    # Tier 2: first-token (first-name) match — REFUSE on ambiguity.
    first_matches = [e for e, _n, f in entries if f == target_first]
    if len(first_matches) == 1:
        return first_matches[0]

    if not enable_fuzzy:
        return None

    # Tier 3: Levenshtein <= 2 on full normalized name — REFUSE on ambiguity.
    fuzzy = [e for e, n, _f in entries if levenshtein(n, target_full) <= MAX_FUZZY_DISTANCE]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if len(fuzzy) >= 2:
        return None  # do NOT descend on ambiguity

    # Tier 4: Levenshtein <= 2 on first-token. Handles asymmetric strings
    # like "Mckinley" vs "Mickinley Verhulst" where Tier 3 distance is 9.
    # Runs only when Tier 3 returned ZERO matches — never as a fallback after
    # Tier 3 ambiguity (key decision, session history).
    first_fuzzy = [e for e, _n, f in entries if levenshtein(f, target_first) <= MAX_FUZZY_DISTANCE]
    if len(first_fuzzy) == 1:
        return first_fuzzy[0]
    return None
```

Three invariants: (a) tighter tiers run first; (b) every fuzzy tier refuses on ambiguity rather than picking arbitrarily; (c) Tier 4 only runs when Tier 3 returned **zero** matches — never as a fallback after Tier 3 ambiguity.

### Layer 2 — 3-phase resolver with claim tracking

The 3-bucket return shape is the function's contract — model it explicitly so producer and consumer cannot drift:

```python
# services/partner_matching.py — V2.14.16
from typing import Literal, TypedDict

Reason = Literal[
    "one_sided_claim",       # A claims B; B's field is blank
    "non_reciprocal",        # A claims B; B claims C
    "unresolved",            # A's claim does not fuzzy-resolve to anyone in pool
    "self_reference",        # A claims own name (typo)
    "partner_already_paired",  # A's claim resolves to B, but B is already in a confirmed pair
]


class OneSidedClaim(TypedDict):
    reason: Reason
    competitor_id: int
    competitor_name: str
    claimed_partner_name: str
    matched_partner_id: int | None       # None when fuzzy resolution failed
    matched_partner_name: str | None


class AssignSummary(TypedDict):
    event_id: int
    confirmed_pairs: int                 # reciprocal pairs validated/healed in Phase 1
    assigned_pairs: int                  # NEW pairs created in Phase 3
    one_sided_claims: list[OneSidedClaim]  # operator review queue with reason codes
    unmatched: int                       # leftover (odd pool, gender imbalance) + needs_review
```

The 3-phase walk uses a `needs_review` set as the keystone. Phase 2 is intentionally elided — `paired` and `needs_review` are populated in Phase 1, then Phase 3 reads both:

```python
def auto_assign_event_partners(event: Event) -> AssignSummary:
    pool = _event_pool(event)
    summary: AssignSummary = {
        "event_id": event.id,
        "confirmed_pairs": 0,
        "assigned_pairs": 0,
        "one_sided_claims": [],
        "unmatched": 0,
    }

    paired: set[int] = set()
    needs_review: set[int] = set()       # the keystone — claimed-but-unconfirmed
    one_sided: list[OneSidedClaim] = []

    # Every non-reciprocal signal in Phase 1 HOLDS the comp + resolved partner.
    # Only a truly blank partner field releases comp to the unclaimed pool.
    for comp in pool:
        if comp.id in paired or comp.id in needs_review:
            continue
        partner_name = _read_partner_name(comp, event)
        if not partner_name:
            continue                     # blank → unclaimed pool

        if normalize_alphanum(partner_name) == normalize_alphanum(comp.name):
            needs_review.add(comp.id)
            one_sided.append(_claim(comp, partner_name, None, "self_reference"))
            continue

        partner = _resolve_partner(partner_name, pool, exclude_id=comp.id)
        if partner is None:
            needs_review.add(comp.id)
            one_sided.append(_claim(comp, partner_name, None, "unresolved"))
            continue

        their = _read_partner_name(partner, event)
        if not their:
            # A claims B, B blank → HOLD BOTH. Do NOT release B to auto-pair.
            needs_review.add(comp.id)
            needs_review.add(partner.id)
            one_sided.append(_claim(comp, partner_name, partner, "one_sided_claim"))
            continue

        # NEVER chain `.id` on a fuzzy lookup — Tier 4 can return None.
        their_match = _resolve_partner(their, pool, exclude_id=partner.id)
        if their_match is None or their_match.id != comp.id:
            needs_review.add(comp.id)
            needs_review.add(partner.id)
            one_sided.append(_claim(comp, partner_name, partner, "non_reciprocal"))
            continue

        _set_partner_bidirectional(comp, partner, event)  # heals typos on both sides
        paired.add(comp.id)
        paired.add(partner.id)
        summary["confirmed_pairs"] += 1

    # Phase 3: AUTO-PAIR only the genuinely unclaimed pool.
    unclaimed = [c for c in pool if c.id not in paired and c.id not in needs_review]
    summary["assigned_pairs"] = _greedy_pair(unclaimed, event, paired)
    summary["one_sided_claims"] = one_sided
    summary["unmatched"] = (len(pool) - len(paired)) - 0  # plus any odd-pool leftovers

    return summary


def _resolve_partner(name: str, pool: Sequence[Competitor], *, exclude_id: int) -> Competitor | None:
    """Bridge from `_read_partner_name` strings to roster entries."""
    return find_partner_match(name, pool, lambda c: c.name, exclude_key=exclude_id)


def _claim(comp, partner_name, partner, reason: Reason) -> OneSidedClaim:
    """One factory for the 5 reason codes — schema stays uniform."""
    return {
        "reason": reason,
        "competitor_id": comp.id,
        "competitor_name": comp.name,
        "claimed_partner_name": partner_name,
        "matched_partner_id": partner.id if partner else None,
        "matched_partner_name": partner.name if partner else None,
    }
```

The route flash surfaces all three buckets as separate flash messages so each gets its own visual treatment:

```python
# routes/registration.py
flash(f"Confirmed {confirmed} reciprocal pair(s). Auto-paired {new_pairs} ...", "success")
if one_sided:
    flash(f"{one_sided} one-sided claim(s) need operator review ...", "warning")
if unmatched:
    flash(f"{unmatched} competitor(s) could not be paired ...", "warning")
```

Each reason code maps to a different operator click-path in the UI — "open the partner field for A," "confirm the A↔B pair as canonical," "drop C from the event." A single "X unmatched" count cannot do that.

## Why This Matters

**Cost without claim tracking.** Suppose A→B (typed correctly), B→blank, C→blank. A naive resolver finds A's match, fails reciprocity, throws **everyone** unmatched into the auto-pair pool. Phase 3 pairs B with C. Now A is "unmatched," B is paired with C against B's intent, and the operator has to manually undo the B↔C link AND build the A↔B link. Two broken partnerships from one missing field.

With `needs_review`, B is reserved the moment A claims her. Phase 3 sees an unclaimed pool of just `{C}`, marks C as unmatched, and the operator gets one clean review queue: "A claimed B; do you want to confirm A↔B?" One click resolves it.

**Iteration history that proves each layer matters** (session history):

| Version | Failure mode | Fix |
|---|---|---|
| pre-V2.14.10 | Mckinley/Mickinley typos failed | Plain normalized lookup, no fuzzy |
| V2.14.10 | Asymmetric-length names ("McKinley" vs "Mickinley Verhulst") still failed | Shared 3-tier ladder (`services/name_match.py`) wired to preflight + heat_generator + partner_resolver |
| V2.14.11 | Tier 3 distance 9 on asymmetric strings | Added Tier 4 first-token Levenshtein |
| V2.14.16 | Auto-pair stage corrupted unrelated pairings | Added `needs_review` set + 3-phase resolver + 3-bucket return |

Each step shipped during a different race-weekend response, each from an actual operator complaint. The V2.14.10 deploy itself crashed Railway on the first attempt because of an unrelated PEP 701 f-string backslash (production runs Python 3.10 — see `docs/solutions/build-errors/railway-python-310-fstring-backslash-deploy-failure-2026-04-23.md`). The pattern only solidifies under that kind of pressure (session history).

**Resolved residual TODO** (auto memory + session history): the V2.14.11 MEMORY note flagged a remaining concern — *"Alex↔Alex Kaper and Emily↔Emily Milligan cases are exact first-token matches and SHOULD pass Tier 2 unless the pool has two competitors sharing a first name (Tier 2 refuses on ambiguity). Fix would be a last-name-initial tiebreak when the partner field has only a first name."* The V2.14.16 claim-tracking layer subsumes this: when Tier 2 refuses on ambiguity, the comp lands in `one_sided_claims` with reason `unresolved` and the operator gets a one-click resolution path. A last-name-initial tiebreak is no longer needed — ambiguity is now a first-class operator-review state, not a silent failure.

## When to Apply

This pattern fits any system where:

- Users name a counterparty by **free text** (partners, gear-sharing buddies, accountability pairs, plus-one invites, doubles tennis, ride-share matches, study-group buddies).
- The free text needs to **resolve against a roster** that has its own canonical names.
- The system has a **bulk auto-assign pass** that runs after manual entry.
- One-sided claims must NOT silently corrupt unrelated assignments.

Skip this pattern when (a) both sides of a relationship are entered atomically (e.g., a doubles registration form that takes two competitor IDs at once), or (b) there is no auto-assign / matching pass — only manual confirmation.

## Examples

### BEFORE (V2.14.10 implementation — no claim tracking)

```python
def auto_assign_event_partners(event):
    pool = _event_pool(event)
    paired = set()

    # Phase 1: walk pool, write partner JSON wherever the field resolves.
    for comp in pool:
        partner_name = _read_partner_name(comp, event)
        if not partner_name:
            continue
        partner = _normalized_lookup(partner_name, pool)    # tier 1 only — exact match
        if partner and partner.id != comp.id:
            _set_partner_bidirectional(comp, partner, event)
            paired.add(comp.id)
            paired.add(partner.id)

    # Phase 2: throw EVERYONE not paired into the auto-pair pool.
    unclaimed = [c for c in pool if c.id not in paired]
    # ...greedy pair...
```

Result: B (claimed by A but with a blank own field) lands in `unclaimed` and gets paired with C. A is reported "unmatched."

### AFTER (V2.14.16 — fuzzy ladder + 3-phase + claim tracking)

The keystone delta is small enough to inline next to the BEFORE for direct comparison:

```python
def auto_assign_event_partners(event: Event) -> AssignSummary:
    pool = _event_pool(event)
    paired: set[int] = set()
    needs_review: set[int] = set()       # NEW — claimed-but-unconfirmed reservation

    for comp in pool:
        # ... fuzzy resolve via _resolve_partner (4-tier ladder) ...
        # ... on ANY non-reciprocal signal, BOTH comp + partner go to needs_review ...
        if not their_partner_name:
            needs_review.add(comp.id)
            needs_review.add(partner.id)  # <-- the load-bearing line
            continue

    # Phase 3 reads BOTH sets — reserved competitors are never auto-paired.
    unclaimed = [c for c in pool if c.id not in paired and c.id not in needs_review]
    # ...greedy pair on unclaimed only...
```

Three structural changes from BEFORE:

1. `_resolve_partner` calls the 4-tier ladder, so typos and asymmetric strings resolve.
2. Every non-reciprocal signal in Phase 1 adds **both** comp and resolved partner to `needs_review` — the partner is reserved even when their own field is blank.
3. Phase 3 runs only over `pool - paired - needs_review`, so reserved competitors are never auto-paired.

### Test pattern: exercising the claim-tracking invariant

```python
# tests/test_partner_auto_assign_v2.py
# (no auto-mock — see feedback_mock_signature_matches_bug.md)
from services.partner_matching import auto_assign_event_partners


def test_one_sided_claim_holds_both_for_review(db_session):
    """A says B, B says nothing → both held; B is NOT auto-paired with C."""
    t = _make_tournament(db_session)
    ev = _make_event(db_session, t)
    alex = _make_pro(db_session, t, "Alex Kaper", "M", ev, partner_name="Bobbie Jones")
    bobbie = _make_pro(db_session, t, "Bobbie Jones", "F", ev, partner_name=None)
    cleo = _make_pro(db_session, t, "Cleo Wilson", "F", ev, partner_name=None)

    summary = auto_assign_event_partners(ev)

    assert summary["confirmed_pairs"] == 0
    assert summary["assigned_pairs"] == 0
    assert len(summary["one_sided_claims"]) == 1
    assert summary["one_sided_claims"][0]["reason"] == "one_sided_claim"
    assert summary["one_sided_claims"][0]["matched_partner_name"] == "Bobbie Jones"

    # The CRITICAL invariant: Bobbie did NOT get auto-paired with Cleo.
    db_session.refresh(alex)
    db_session.refresh(bobbie)
    db_session.refresh(cleo)
    assert alex.get_partners().get(str(ev.id)) == "Bobbie Jones"  # original claim preserved
    assert bobbie.get_partners().get(str(ev.id)) is None          # explicit None — not auto-paired
    assert cleo.get_partners().get(str(ev.id)) is None            # explicit None — not auto-paired
```

The test uses real `ProCompetitor` rows, real `Event` rows, and runs the actual `auto_assign_event_partners` service — no mocks. This is mandatory: a hand-written mock that copies the buggy V2.14.10 signature (no `needs_review` set) would silently make the bug invisible. The auto-memory rule `feedback_mock_signature_matches_bug.md` codifies this — tests for resolver patterns must round-trip through the real service emitter, not a parallel mock (auto memory [claude]). See also the related test-shape trilogy and hand-written-fixture-shape-divergence docs.

The companion tests in the same file cover the other reason codes: `test_typo_partner_confirms_as_reciprocal` (Tier 4 fuzzy match), `test_unclaimed_pool_auto_pairs_mixed` (Phase 3 mixed-gender priority), `test_self_reference_held_for_review` (own-name typo), `test_non_reciprocal_holds_both` (A→B / B→C cascade), and `test_idempotent_on_already_paired_roster` (re-running on a clean roster yields zero new pairs — protects against double-write bugs in Phase 1's bidirectional heal).

## Related Docs

- [`preflight-gear-sharing-using-prefix-false-positives-2026-04-21.md`](../data-integrity/preflight-gear-sharing-using-prefix-false-positives-2026-04-21.md) — adjacent domain (gear-sharing JSON USING/SHARING prefix). Different keys, but the centralized-accessor philosophy is the same: one shared resolver, every caller threads through it.
- [`test-shape-matches-bug-shape-trilogy-2026-04-23.md`](../test-failures/test-shape-matches-bug-shape-trilogy-2026-04-23.md) — meta-doc on test fixtures that match the buggy shape rather than real production behavior. The test pattern in this doc deliberately avoids that trap.
- [`hand-written-fixture-shape-divergence-2026-04-22.md`](../test-failures/hand-written-fixture-shape-divergence-2026-04-22.md) — sibling of the trilogy; same lesson applied to a different ship.
