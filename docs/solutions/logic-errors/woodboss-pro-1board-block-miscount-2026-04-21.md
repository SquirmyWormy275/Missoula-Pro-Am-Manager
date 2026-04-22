---
title: Pro 1-Board chopping blocks silently merged into 2-Board Springboard bucket in Virtual Woodboss
date: 2026-04-21
category: logic-errors
module: woodboss
problem_type: logic_error
component: service_object
severity: high
symptoms:
  - "Pro 1-Board chopping blocks missing from Wood Count Report planning output"
  - "Pro 1-Board competitors silently inherit Springboard/2-Board species and diameter from block_springboard_pro config"
  - "No dedicated config row or report row for Pro 1-Board in Virtual Woodboss UI"
  - "Wood order under-counts actual race-day requirement — risk of wood shortage on block-turning day"
  - "Dummy-tree counts on the same report were correct — only the blocks section was broken, making the mismatch look like a display glitch"
root_cause: logic_error
resolution_type: code_fix
related_components:
  - rails_view
tags:
  - woodboss
  - wood-count
  - pro-events
  - springboard
  - 1-board
  - block-config
  - event-mapping
  - silent-undercount
---

> **Note on the code snippets below.** The Solution section preserves the narrative of
> how the bug was diagnosed and what the first pass looked like. The final shipped fix
> consolidated three duplicated copies of the fragment-matching loop into a single
> module-level helper (`_match_block_cfg_keys`) with two module-level fragment
> constants — see the **Post-review consolidation** subsection at the end of Solution
> for the canonical form. Two independent reviewers (code-simplicity + Kieran Python)
> caught the duplication and a third unguarded call site in `get_lottery_view`.

# Pro 1-Board chopping blocks silently merged into 2-Board Springboard bucket in Virtual Woodboss

## Problem

The Virtual Woodboss block-count calculator in `services/woodboss.py` lumped Pro Springboard (2-Board) and Pro 1-Board competitor counts into a single `block_springboard_pro` config bucket. Pro 1-Board competitors silently inherited 2-Board species and diameter on block-turning day, and the Wood Count Report had no dedicated Pro 1-Board row — so the block-prep crew cut the wrong height/species for roughly half the pro springboard field.

The three pro springboard events — `Springboard` (2-Board), `Pro 1-Board`, and `3-Board Jigger` — each use physically different wood dummies (different heights; 3-Board Jigger also uses fewer runs per dummy than 1/2-Board). Each needs its own wood spec.

## Symptoms

- Only two pro springboard rows on the Wood Count Report (By-Species view): "Springboard — Pro" and "3-Board Jigger — Pro". No "Pro 1-Board" row — it simply did not exist on the page.
- "Springboard — Pro" count equalled the sum of 2-Board + 1-Board competitors, making it look artificially high. A judge cross-checking against enrollment would see the number tie out to two events combined but have no way to see it was combined.
- Config UI exposed one species/diameter pair for "Springboard — Pro" that silently controlled both event types. Setting white pine 14" for 2-Board forced the same onto 1-Board athletes even though 1-Board traditionally uses a shorter tree.
- Dummy-tree counts on the same report **were correct** because `calculate_springboard_dummies()` walks event names independently of `BLOCK_EVENT_GROUPS`. The block-count side — the only broken path — only surfaced on the species/size config surface, which fewer people check. The mismatch looked like a display glitch, not a calculation bug.
- No error, no warning, no flash message. Silent under-count.

## What Didn't Work

**V2.8.1 patch note claimed the split was already done — it was half-shipped.** MEMORY.md patch V2.8.1 (2026-04-10) reads: *"Woodboss: split pro springboard into 2-board / 1-board / 3-board buckets; enforce exclusivity to prevent double-counting."* The V2.8.1 commit (`0eaae43`, authored directly by the user outside any Claude session per session history) introduced the `block_1board_pro` **key** into `apply_preset()` loop iteration and added exclusivity scaffolding, but never actually added `block_1board_pro` to `BLOCK_CONFIG_LABELS` and never updated the fragment mappings in `BLOCK_EVENT_GROUPS` to route 1-board events to it. The routing remained `('1-board', 'pro', None, 'block_springboard_pro', ...)`. Two subsequent audit sweeps (V2.8.2, V2.8.3) trusted the note and did not re-verify the 1-Board side. **The patch note described intent; the code shipped half of it.** (session history)

**The separate `calculate_springboard_dummies()` function masked the bug.** That function walks each competitor's `events_entered` and categorizes each event string into 2-board / 1-board / 3-board *independently* of `BLOCK_EVENT_GROUPS`, so the dummy-tree section of the report correctly showed 1-Board competitors getting their own shorter dummies. Any spot-check focused on dummy totals looked right. The block-count side was the only broken path. Earlier session `9a50f933` (2026-04-11) flagged the same event-name-walk divergence between `calculate_blocks` and `calculate_springboard_dummies` but the thread was cut off before the fragment-routing gap was pursued. (session history)

**V2.8.3 woodboss audit (14 new tests) did not cover this case.** Tests exercised species-only presets, None-write roundtripping, negative count_override, and college name-vs-ID fallback — none parametrized over each pro event name to assert a unique config bucket. The 1-Board/2-Board collision flew under the coverage.

## Solution

Three edits in `services/woodboss.py`.

### 1. Split the fragment mappings in `BLOCK_EVENT_GROUPS`

Before:

```python
# Pro springboard events: 1-Board, 3-Board Jigger, Pro 1-Board are all open gender
('springboard',  'pro', None, 'block_springboard_pro', 'Springboard — Pro'),
('1-board',      'pro', None, 'block_springboard_pro', 'Springboard — Pro'),
('one board',    'pro', None, 'block_springboard_pro', 'Springboard — Pro'),
('2-board',      'pro', None, 'block_springboard_pro', 'Springboard — Pro'),
('2 board',      'pro', None, 'block_springboard_pro', 'Springboard — Pro'),
('two board',    'pro', None, 'block_springboard_pro', 'Springboard — Pro'),
('3-board',      'pro', None, 'block_3board_pro',      '3-Board Jigger — Pro'),
# ...
```

After:

```python
# Pro springboard events — three distinct wood categories:
#   Pro Springboard (2-board) → block_springboard_pro
#   Pro 1-Board                → block_1board_pro
#   3-Board Jigger             → block_3board_pro
# All are open gender.
('2-board',      'pro', None, 'block_springboard_pro', 'Springboard (2-Board) — Pro'),
('2 board',      'pro', None, 'block_springboard_pro', 'Springboard (2-Board) — Pro'),
('two board',    'pro', None, 'block_springboard_pro', 'Springboard (2-Board) — Pro'),
('springboard',  'pro', None, 'block_springboard_pro', 'Springboard (2-Board) — Pro'),
('1-board',      'pro', None, 'block_1board_pro',      'Pro 1-Board'),
('one board',    'pro', None, 'block_1board_pro',      'Pro 1-Board'),
('3-board',      'pro', None, 'block_3board_pro',      '3-Board Jigger — Pro'),
# ...
```

### 2. Register the new key in `BLOCK_CONFIG_LABELS`

```python
'block_springboard_pro':       'Springboard (2-Board) — Pro',
'block_1board_pro':            'Pro 1-Board',   # NEW
'block_3board_pro':            '3-Board Jigger — Pro',
```

### 3. Defensive exclusivity gate in `calculate_blocks()`

Custom renames like `"Pro 1-Board Springboard"` would otherwise match BOTH the `1-board` fragment → `block_1board_pro` AND the `springboard` fragment → `block_springboard_pro`, double-counting one competitor into two buckets. This gate makes `1-board` / `3-board` / `jigger` exclusive from the generic `springboard` fallback:

```python
for (event_lower, comp_type, gender), n in counts.items():
    matched_cfg_keys = set()

    is_pro_one_board = comp_type == 'pro' and (
        '1-board' in event_lower or '1 board' in event_lower
        or 'one board' in event_lower or 'one-board' in event_lower
    )
    is_pro_three_board = comp_type == 'pro' and (
        '3-board' in event_lower or '3 board' in event_lower
        or 'three-board' in event_lower or 'three board' in event_lower
        or 'jigger' in event_lower
    )
    skip_pro_springboard_fallback = is_pro_one_board or is_pro_three_board

    for (fragment, grp_type, grp_gender, cfg_key, _label) in BLOCK_EVENT_GROUPS:
        if fragment not in event_lower:
            continue
        if comp_type != grp_type:
            continue
        if grp_gender is not None and gender != grp_gender:
            continue
        if skip_pro_springboard_fallback and cfg_key == 'block_springboard_pro':
            continue
        matched_cfg_keys.add(cfg_key)

    for cfg_key in matched_cfg_keys:
        key_counts[cfg_key] += n
```

### Post-review consolidation (the canonical shipped form)

After two independent reviewers (code-simplicity + Kieran Python) flagged that the exclusivity block was duplicated between `_active_block_keys()` and `calculate_blocks()`, **and** that `get_lottery_view()` had the same fragment-matching loop WITHOUT the exclusivity guard (meaning the print-out of lottery block cards would silently double-count custom renames even though the count report was now correct), the three sites were consolidated into a single module-level helper.

**Module-level constants** (near `BLOCK_EVENT_GROUPS`):

```python
PRO_ONE_BOARD_FRAGMENTS = ('1-board', '1 board', 'one board', 'one-board')
PRO_THREE_BOARD_FRAGMENTS = ('3-board', '3 board', 'three-board', 'three board', 'jigger')
```

**Single helper** — called by `_active_block_keys`, `calculate_blocks`, AND `get_lottery_view`:

```python
def _match_block_cfg_keys(event_lower, comp_type, gender):
    """Return the set of block cfg_keys that this event maps to, with pro
    1-Board / 3-Board exclusivity applied so a 1-Board or 3-Board event
    name does not also land in block_springboard_pro."""
    is_pro_one_board = comp_type == 'pro' and any(
        f in event_lower for f in PRO_ONE_BOARD_FRAGMENTS
    )
    is_pro_three_board = comp_type == 'pro' and any(
        f in event_lower for f in PRO_THREE_BOARD_FRAGMENTS
    )
    skip_pro_springboard_fallback = is_pro_one_board or is_pro_three_board

    keys = set()
    for (fragment, grp_type, grp_gender, cfg_key, _label) in BLOCK_EVENT_GROUPS:
        if fragment not in event_lower:
            continue
        if comp_type != grp_type:
            continue
        if grp_gender is not None and gender != grp_gender:
            continue
        if skip_pro_springboard_fallback and cfg_key == 'block_springboard_pro':
            continue
        keys.add(cfg_key)
    return keys
```

The three call sites collapse to a single line each:

```python
# _active_block_keys
active |= _match_block_cfg_keys(event_lower, event.event_type, event.gender)

# calculate_blocks
for cfg_key in _match_block_cfg_keys(event_lower, comp_type, gender):
    key_counts[cfg_key] += n

# get_lottery_view  (the previously-unguarded third site)
for cfg_key in _match_block_cfg_keys(event_lower, comp_type, gender):
    key_event_comps[cfg_key][event_name].append({...})
```

**Keep `PRO_ONE_BOARD_FRAGMENTS` and `PRO_THREE_BOARD_FRAGMENTS` aligned with `BLOCK_EVENT_GROUPS`.** One regression this session: the exclusivity constant initially included `'1 board'` (space) but `BLOCK_EVENT_GROUPS` only had `'1-board'` (hyphen). A custom rename `"Pro 1 Board"` tripped the exclusivity (skipped springboard fallback) but had no matching fragment, so it landed in zero buckets — worse than the original bug. The `test_1board_spellings_all_land_in_1board_bucket` parametrize caught it; `BLOCK_EVENT_GROUPS` was then extended to include every spelling in the constant. Keep the sets synchronized.

### Verification

- `pytest tests/test_woodboss.py` → 60/60 pass (up from 28; 14 new parametrized regression tests added in `TestBlockConfigLabelsIntegrity`, `TestProSpringboardExclusivity`, and `TestCalculateBlocksEmitsAllLabelledKeys`).
- Hand-traced synthetic test: 15 pro 2-Board + 20 pro 1-Board + 8 three-Board Jigger + 99 pathological `"1-Board Springboard"` pro all land in correct buckets with zero double-count.

## Why This Works

**Root cause.** `BLOCK_EVENT_GROUPS` matches via substring fragments against the lowercased event name, in declaration order, with no exclusivity. Both `"1-board"` and `"springboard"` are legitimate substrings of `"Pro 1-Board"` (and of `"1-Board Springboard"`). The pre-fix table assigned the same `cfg_key = 'block_springboard_pro'` to both fragments, so the lack of exclusivity didn't cause a visible double-count — it meant 1-Board competitors hit an already-mapped-to-2-Board bucket and disappeared into it. No physical config row existed for 1-Board because `BLOCK_CONFIG_LABELS` had no entry for one.

**Why the fix closes it.**
- Change 1 gives each physical wood category its own `cfg_key`, so for canonical event names `matched_cfg_keys` ends up containing exactly one bucket per competitor per event.
- Change 2 surfaces the new bucket on the UI and report — the data no longer silently drains into a bucket that has no label.
- Change 3 defends the now-distinct buckets against name collisions from custom tournament event names. If a tournament types `"Pro 1-Board Springboard"`, the `1-board` detector fires, the generic `springboard` fallback is skipped, and the competitor counts exactly once in `block_1board_pro`.

The `calculate_springboard_dummies()` path was already correct (it parses event names directly), so the fix brings the block-count path into parity without changing dummy-tree numbers users already trust.

## Prevention

### 1. Parametrized per-event unique-bucket test

Add to `tests/test_woodboss.py`. Would have caught the original bug and catches any future regression:

```python
import pytest
from services.woodboss import calculate_blocks

EXPECTED_PRO_BUCKETS = {
    'Springboard':     'block_springboard_pro',
    'Pro 1-Board':     'block_1board_pro',
    '3-Board Jigger':  'block_3board_pro',
    'Underhand':       'block_underhand_pro_M',   # parametrize per gender
    'Standing Block':  'block_standing_pro_M',
    # add every canonical pro event here
}

@pytest.mark.parametrize("event_name,expected_key", EXPECTED_PRO_BUCKETS.items())
def test_each_pro_event_maps_to_exactly_one_block_bucket(event_name, expected_key, seeded_tournament):
    # seed a single pro competitor in this event, run calculate_blocks,
    # assert exactly one non-zero bucket and it equals expected_key.
    ...
```

### 2. Schema-level regression guards

Cheap assertions that prevent silent drop-offs:

```python
def test_block_1board_pro_registered_in_labels():
    from services.woodboss import BLOCK_CONFIG_LABELS
    assert 'block_1board_pro' in BLOCK_CONFIG_LABELS
    assert BLOCK_CONFIG_LABELS['block_1board_pro'] == 'Pro 1-Board'

def test_all_block_event_group_keys_have_labels():
    from services.woodboss import BLOCK_EVENT_GROUPS, BLOCK_CONFIG_LABELS
    referenced = {cfg_key for (_frag, _t, _g, cfg_key, _lbl) in BLOCK_EVENT_GROUPS}
    missing = referenced - set(BLOCK_CONFIG_LABELS.keys())
    assert not missing, f"BLOCK_EVENT_GROUPS references keys with no label: {missing}"

def test_calculate_blocks_emits_row_per_labelled_key_even_with_zero_competitors(seeded_tournament):
    # Run calculate_blocks on an empty tournament and assert every key in
    # BLOCK_CONFIG_LABELS appears in the result list, regardless of count.
    ...
```

The last assertion prevents the "bucket exists in labels but zero competitors so it never renders" failure mode.

### 3. Collision-name fuzz test

Cover tournaments that rename events:

```python
@pytest.mark.parametrize("custom_name", [
    "Pro 1-Board Springboard",
    "1-Board (Springboard)",
    "Springboard 1-Board Pro",
])
def test_custom_oneboard_name_counts_once_as_1board(custom_name, seeded_pro_event):
    # seed a pro competitor in a custom-named event, run calculate_blocks,
    # assert block_1board_pro == 1 and block_springboard_pro == 0.
    ...
```

### 4. Process reminder: audit code, not notes

MEMORY.md patch text describes *intent at time of commit* and is not regenerated from code. If a future session reads `"the split was done"` in patch history, do not treat it as verification — open `BLOCK_EVENT_GROUPS` and grep for every expected `cfg_key` in `BLOCK_CONFIG_LABELS`. The V2.8.1 → V2.8.2 → V2.8.3 → V2.11.2 chain trusted the V2.8.1 note and shipped six months of follow-up without catching this.

Consider adding one line to project CLAUDE.md: **"Memory describes intent. For verification, read the code."**

## Related Issues

- `docs/solutions/data-integrity/events-entered-stores-names-not-ids.md` — V2.8.2 bug in the same file (`services/woodboss.py`) with the same user-visible symptom class (silent wood under-count) but different root cause (ID-vs-name lookup in `_count_competitors` / `_list_competitors`). Both bugs demonstrate that the woodboss count path is a high-risk area for silent failures; audit both functions together whenever either is touched.
- `docs/WOODBOSS_AUDIT.md` — V2.8.1 → V2.8.3 audit of the woodboss subsystem. The audit's H0 covers the events_entered name/ID bug; M1 discusses per-category block specs but never surfaced the 1-board/2-board silent merge. The audit's status table now claims all findings resolved — this is stale and should be updated with a new entry (H4 or LOW) covering this fix.
- `_active_block_keys(tournament_id)` helper in `services/woodboss.py` (introduced in session `9a50f933` on 2026-04-11, per session history) was already treating 1-Board as a distinct category in active-key logic on April 11, even though `BLOCK_EVENT_GROUPS` fragment routing hadn't been corrected to match. When auditing either function, cross-check that both use the same category taxonomy.
