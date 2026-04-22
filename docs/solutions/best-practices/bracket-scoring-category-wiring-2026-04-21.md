---
title: Wiring a bracket scoring category end-to-end (service + seeding + UI)
date: 2026-04-21
category: best-practices
module: routes/scheduling
problem_type: best_practice
component: rails_controller
severity: medium
applies_when:
  - "Adding a new event with scoring_type='bracket' or any tournament-progression format"
  - "A scoring service class exists in services/ but has no judge-facing UI"
  - "A new ability-ranking category needs to appear alongside the existing pro categories"
  - "Connecting Event.payouts-as-JSON state to both a ranking surface and a management surface"
related_components:
  - service_object
  - rails_view
  - database
tags:
  - birling
  - bracket-scoring
  - double-elimination
  - ability-rankings
  - seeding
  - event-payouts-json
  - scoring-category-wiring
---

# Wiring a bracket scoring category end-to-end (service + seeding + UI)

## Context

A complete `BirlingBracket` service lived in `services/birling_bracket.py` with every code path the show needs (winner advancement, losers-bracket drop, grand finals, true finals, undo, placement). A read-only bracket viewer rendered it. And yet judges could not drive a single match from the browser — bracket state lives as JSON in the repurposed `Event.payouts` column, and no surface mutated that JSON. The service was dead code on race day.

The parallel gap: the existing Pro Ability Rankings page (`/scheduling/<tid>/ability-rankings`) let judges rank pro competitors within 7 `ProEventRank` categories for snake-draft heat balancing, but Birling had no analogous seeding surface. Without seeding, the bracket's output was a random sort of whichever order `get_events_entered()` returned — a registration-order artifact, not a competitive signal, which meant the two strongest competitors could collide in round 1.

The pattern below applies any time a scoring service class exists but its UI is missing, and any time a new category needs to plug into the existing ranking system.

## Guidance

Four atomic pieces wire a bracket scoring category through config → routes → template → registration. All four must land together or the feature is half-surfaced.

### 1. Extend `RANKED_CATEGORIES`, `event_rank_category()`, and the display maps in `config.py` together

These four constants are read by the ability-rankings route, the heat generator, and the ranking template. Missing any one causes a silent omission from the UI:

```python
# config.py
def event_rank_category(event: Event) -> str | None:
    # existing branches for springboard, pro_1board, underhand, etc.
    if event.stand_type == 'birling':
        return 'birling'
    return None

RANKED_CATEGORIES = {
    'springboard', 'pro_1board', '3board_jigger',
    'underhand', 'standing_block', 'obstacle_pole',
    'singlebuck', 'doublebuck', 'jack_jill',
    'birling',          # new
}

CATEGORY_DISPLAY_NAMES['birling'] = 'Birling'
CATEGORY_DESCRIPTIONS['birling'] = "Double-elimination bracket seeding (Men's and Women's)"
```

Also add the category slug to the `ordered_cats` list in `templates/scheduling/ability_rankings.html` — otherwise the rendered page has no loop iteration for it.

### 2. Co-locate bracket state, seeding, and pre-seedings in one JSON blob

`BirlingBracket` already serializes to `Event.payouts`. Store seeding inside the same blob rather than adding a column. The ability-rankings page can write `pre_seedings` to the same column; the bracket generator can fall back to them when no manual seeds are posted:

```python
# routes/scheduling/birling.py — generate route
# Read manual seeds from the form, falling back to pre_seedings in the same JSON blob.
# int() on POST data is wrapped per CLAUDE.md §6 form-input rule.
def _parse_seed(raw: str) -> int | None:
    try:
        return int(raw) if raw.strip() else None
    except (TypeError, ValueError):
        return None

manual = {c.id: _parse_seed(request.form.get(f'seed_{c.id}', '')) for c in competitors}
has_manual = any(v is not None for v in manual.values())

if has_manual:
    seed_for = manual.get
else:
    # Fall back to pre_seedings written by the ability-rankings page.
    # Event.get_payouts() is the guarded reader; it returns {} on JSONDecodeError.
    pre_seedings = event.get_payouts().get('pre_seedings', {})
    seed_for = lambda cid: pre_seedings.get(str(cid))

# Seeded first (ascending), unseeded after (alphabetical by display name).
def _sort_key(c):
    seed = seed_for(c.id)
    return (0, seed, c.name.lower()) if seed is not None else (1, 0, c.name.lower())

ordered = sorted(competitors, key=_sort_key)

bb.generate_bracket(
    [{'id': c.id, 'name': c.display_name} for c in ordered],
    seeding=[c.id for c in ordered],
)
```

Two surfaces (ability rankings + bracket management) mutate the same JSON blob on `Event.payouts`, so they stay in sync without a migration or a shared service. The cost is a column whose name lies; that tradeoff is already paid by `ProAmRelay`, `PartneredAxeThrow`, and `BirlingBracket`.

### 3. The management GET route reconstructs display state from the JSON each request

No client-side bracket state; no session caching. The GET route rebuilds everything from the authoritative JSON so seeding, playable matches, and placements always match what the service just wrote:

```python
# routes/scheduling/birling.py
@scheduling_bp.route('/<int:tournament_id>/event/<int:event_id>/birling', methods=['GET'])
def birling_manage(tournament_id: int, event_id: int):
    event = Event.query.get_or_404(event_id)
    if event.tournament_id != tournament_id or event.scoring_type != 'bracket':
        abort(404)

    bb = BirlingBracket(event)
    bracket_data = bb.bracket_data
    has_bracket = bool(bracket_data.get('bracket', {}).get('winners'))

    # Reconstruct view-model state from the JSON each request — no caching, no drift.
    # (competitor list, current playable matches, placements, undoable matches
    # all come from the same bracket_data blob.)
    return render_template(
        'scheduling/birling_manage.html',
        event=event,
        bracket=bracket_data.get('bracket', {}),
        has_bracket=has_bracket,
        current_matches=bb.get_current_matches() if has_bracket else [],
        placements=bracket_data.get('placements', {}),
        undoable_match_ids=bb.get_undoable_matches() if has_bracket else set(),
        # ... plus whatever view-model shaping the template needs (seeded competitor list,
        # comp_lookup for ID→name rendering, etc.)
    )
```

Any per-competitor shaping — seeded/unseeded sort, team-code lookup, gender badges — goes into a small view-model helper or into the template itself. The route's only job is to read state and hand it over. When sorting seeded vs unseeded, prefer an explicit `is None` check over a truthy fallback so a literal seed of `0` does not get mistaken for unseeded.

### 4. One-click winner buttons via sibling forms — no JS, full POST-redirect-GET

Two sibling `<form>` elements, each containing one submit button with `name="winner_id"` and `value="{competitor_id}"`. Every action is a full round trip that re-reads the bracket state from JSON, so there is no possibility of client/server drift:

```html
<!-- templates/scheduling/birling_manage.html -->
<form method="POST" action="{{ url_for('scheduling.birling_record_match', ...) }}">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <input type="hidden" name="match_id"  value="{{ match.match_id }}">
  <button type="submit" name="winner_id" value="{{ match.competitor1 }}"
          class="btn btn-outline-success btn-sm text-start">
    <i class="bi bi-trophy"></i> {{ name1 }}
  </button>
</form>
<form method="POST" action="...">
  ... same pattern with competitor2 ...
</form>
```

This avoids radio buttons, modals, or JS bracket state. Judges see a card per ready match, click the winner's name, and the page re-renders with the bracket advanced.

### 5. Register the sub-module in `routes/scheduling/__init__.py` AFTER the blueprint is defined

The scheduling package defines `scheduling_bp` and shared helpers in `__init__.py`, then imports sub-modules at the bottom so decorators execute. Add the new file the same way:

```python
# routes/scheduling/__init__.py — at the bottom, after scheduling_bp is defined
from . import (
    ability_rankings, assign_marks,
    birling,            # new sub-module
    events, flights, friday_feature,
    heat_sheets, heats, preflight, show_day,
)
```

No change to `app.py` — the scheduling blueprint is already registered there.

## Why This Matters

1. **Services without UIs are invisible to users.** A 600-line `BirlingBracket` with every code path for double-elimination progression was useless on race day until seeding, generation, match recording, and finalization had routes + templates. The lesson: shipping a service without its surface is shipping dead code. The feature is not "built" until a judge can drive it from a browser.

2. **Seeding is not cosmetic; it is fairness.** Double-elimination brackets punish low seeds who meet in round 1 and eliminate each other before the show warms up. Without a seeding surface, the bracket's output is registration order — a scheduling artifact, not a competitive signal. The only reliable way to seed is a deliberate judge-facing ranking surface.

3. **Reusing the existing mental model drives training cost to zero.** The Pro Ability Rankings page already teaches judges "here is a list per category, drag to order, strongest first." Extending `RANKED_CATEGORIES` to include `'birling'` and wiring seeding through the same `pre_seedings` JSON key means judges see one concept across every event that needs it — no new workflow, no second screen.

4. **One JSON blob, two consumer surfaces, zero migrations.** `Event.payouts` was already repurposed for state-events; piggybacking seeding onto it costs nothing. The cost of the column's misleading name was already paid — new consumers get the pattern for free. This is the correct default for any new stateful service in this codebase.

5. **Losers-bracket math is subtle — do not trust the first implementation.** (session history) The initial `_generate_losers_bracket()` produced the wrong round count for every field size except 4, and bye propagation in the losers bracket stalled until a targeted sweep was added. When adding new bracket sizes, test at least 4, 6, 8, 12, and 16 competitors — not just the power-of-2 cases. See `services/birling_bracket.py` for the corrected implementation.

## When to Apply

- Any event with `scoring_type='bracket'` in the event config (currently only Birling, but the pattern generalizes).
- Any event that uses tournament-style progression (single-elim, double-elim, round-robin) rather than timed runs ranked by metric.
- Any time a service class exists in `services/` but has no corresponding route entry point or template — the service is not done.
- When adding a new `ProEventRank`-style category — always update `RANKED_CATEGORIES`, `CATEGORY_DISPLAY_NAMES`, `CATEGORY_DESCRIPTIONS`, `event_rank_category()`, and the template's `ordered_cats` list as one atomic change.
- Whenever seeding quality materially changes the competitive outcome (always for brackets, sometimes for flighted events).

## Examples

### Before — bracket service unreachable

```
services/birling_bracket.py          ← 600 lines of bracket logic
templates/scoring/birling_bracket.html ← read-only viewer
                                     ← no route to generate
                                     ← no route to record a match
                                     ← no seeding UI
                                     ← no finalize button
```

Judges ran Birling on paper, transcribed placements back in via the generic event-results screen, and had no audit trail for individual match results.

### After — a sub-module, a template, and four config lines

```
routes/scheduling/birling.py
  birling_manage        GET  — load + render state
  birling_generate      POST — seed + generate_bracket()
  birling_record_match  POST — record_match_result()
  birling_record_fall   POST — record_fall() for best-of-3
  birling_undo_match    POST — undo_match_result()
  birling_reset         POST — clear Event.payouts
  birling_finalize      POST — finalize_to_event_results()
  birling_print_blank   GET  — WeasyPrint PDF of blank bracket
  birling_print_all     GET  — combined PDF across every birling event

templates/scheduling/birling_manage.html
  ├─ Seeding table (typed ranks, falls back to pre_seedings)
  ├─ "Matches Ready to Play" cards with one-click winner buttons
  ├─ Winners/Losers/Finals bracket visualization (macro-rendered)
  ├─ Placements table (1st/2nd/3rd gold/silver/bronze badges)
  └─ Reset/Finalize/Print actions
```

### Extending `RANKED_CATEGORIES` — the whole ranking page lights up for free

See Guidance §1 for the four constants + `event_rank_category()` edit, plus the one-line `ordered_cats` template addition. That is the entire change — no new template, no new route, no migration — and Birling appears in the existing ranking UI inheriting the `.rank-list` / SortableJS drag-and-drop wiring built for the pro categories.

### Sub-module registration is one import line

See Guidance §5. The scheduling package's `__init__.py` already has a bottom-of-file import block for sub-modules; append `birling` to it. No change to `app.py` — the scheduling blueprint is already registered there.

## Related

- `docs/BIRLING_RECON.md` — full status audit of birling files and gaps as of 2026-04-12; the pattern above fills the gaps identified there.
- `docs/solutions/architecture-decisions/json-fields-over-join-tables.md` — documents the `Event.payouts`-as-JSON decision this pattern builds on.
- `docs/solutions/best-practices/drag-drop-competitors-with-holding-bin-2026-04-21.md` — complementary pattern for the drag-drop UI used by the ability-rankings surface.
- `CLAUDE.md` §3 (College Division — Birling), §4 (ProEventRank description), §5 (Features Functionally Complete — Ability Rankings, Birling bracket).
- `services/birling_bracket.py` — canonical implementation of the service side of the pattern.
