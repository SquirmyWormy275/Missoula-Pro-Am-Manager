# BIRLING BRACKET SYSTEM — RECON REPORT

**Original date:** 2026-04-12
**Last refreshed:** 2026-04-21 (see Status Update below)
**Scope:** Full discovery, read, dependency trace, and gap analysis of all birling/bracket-related code in Missoula-Pro-Am-Manager.

---

## STATUS UPDATE — 2026-04-21

Refreshed via `/ce:compound-refresh` after the 2026-04-13 losers-bracket rewrite and 2026-04-21 print + nav surfacing work landed. The audit below captures the 2026-04-12 snapshot for historical context; this section supersedes its file sizes, route table, and gap list.

### File size drift since 2026-04-12

| File | Apr 12 | Apr 21 | Change |
|------|--------|--------|--------|
| `services/birling_bracket.py` | 461 | 883 | +91% (fall recording, undo, sweep-byes, `get_undoable_matches`, PDF print helpers) |
| `routes/scheduling/birling.py` | 261 | 484 | +85% (5 routes → 9 routes) |
| `templates/scheduling/birling_manage.html` | 443 | 517 | +17% (fall cards, undo buttons, drag-drop seeding) |
| `templates/scoring/birling_bracket.html` | 125 | 133 | +6% (minor polish; legacy route redirects here → birling_manage) |

### Routes added since 2026-04-12 (9 total, was 5)

| Route | Method | URL | Purpose |
|-------|--------|-----|---------|
| `birling_record_fall` | POST | `.../birling/record-fall` | **NEW** — records individual falls within a match (best-of-3); resolves gap #2 |
| `birling_undo_match` | POST | `.../birling/undo` | **NEW** — reverses the last match result without wiping the bracket; resolves gap #3 |
| `birling_print_blank` | GET | `.../birling/print-blank` | **NEW** — WeasyPrint PDF of empty bracket for paper backup |
| `birling_print_all` | GET | `/scheduling/<tid>/birling/print-all` | **NEW** — combined PDF across every birling event in the tournament |

### Gap list — status as of 2026-04-21

Renumbering references the GAP SUMMARY below for traceability.

| # | Gap | Status |
|---|-----|--------|
| 1 | No Match model (matches are JSON dicts) | **Still open** — deliberate design choice; `Event.payouts` JSON pattern unchanged |
| 2 | No fall recording | **RESOLVED** — `BirlingBracket.record_fall()` + `POST .../birling/record-fall` route + fall cards in `birling_manage.html` |
| 3 | No match undo | **RESOLVED** — `BirlingBracket.undo_match_result()` + `get_undoable_matches()` + `POST .../birling/undo` route + undo buttons on bracket matches. Undo is shallow (last-result-only) — matches whose downstream advancement has already been played cannot be undone; `get_undoable_matches()` computes the safe set |
| 4 | No spectator bracket view | **Still open** — bracket remains judge-only |
| 5 | No format differentiation (ALA vs AWFC) | **Still open** — single format (standard double-elim with true finals) |
| 6 | No bracket seeding from STRATHMARK | **Still open** — manual + ability-rankings `pre_seedings` only |
| 7 | Losers bracket structure broken for non-power-of-2 fields | **RESOLVED** — rewrite on 2026-04-13. `_generate_losers_bracket()` now produces correct `2*(log2(B)-1)` round count. `_sweep_losers_byes()` + `_get_lb_sources()` added to handle bye propagation through the losers bracket (scoped to L1 only to avoid premature advancement). `finalize_to_event_results()` now also sets `points_awarded` via `PLACEMENT_POINTS_DECIMAL`. Verified for field sizes 4, 6, 8, 12, 16 |
| 8 | No bracket points integration | **RESOLVED** — `finalize_to_event_results()` now calls placement-points scoring (see gap #7 entry) |
| 9 | No match metadata (timestamps, pond conditions, etc.) | **Still open** — deliberate; match dict shape unchanged |
| 10 | No bracket progression validation | **RESOLVED** — `record_match_result()` now raises `ValueError` if `match_id` is not in `get_current_matches()`, preventing out-of-order advancement |
| 11 | Bracket not exposed via API | **Still open** — public API has no bracket endpoint |
| 12 | No bracket regeneration with preserved results | **Still open** — full wipe on regenerate |
| 13 | `create_birling_bracket()` / `get_birling_bracket()` factory functions unused | **Still open** — dead code, deferred cleanup |
| 14 | No SMS/notification integration | **Still open** — Twilio wired to flights only, not brackets |

**Net:** 5 of 14 gaps resolved in the two weeks between the audit and this refresh.

### Related docs added since 2026-04-12

- [`docs/solutions/best-practices/bracket-scoring-category-wiring-2026-04-21.md`](solutions/best-practices/bracket-scoring-category-wiring-2026-04-21.md) — distills the end-to-end wiring pattern (config + routes + template + registration + JSON seeding) that finished filling the gaps this recon identified. Read this if you are adding a new bracket-scoring category.

---

## HISTORICAL AUDIT (2026-04-12)

*The sections below are preserved as the original audit. File sizes, route counts, and gap entries are superseded by the Status Update above.*

---

## FILES FOUND

| # | File Path | Lines | Status |
|---|-----------|-------|--------|
| 1 | `services/birling_bracket.py` | 461 | **LOGIC** — Full `BirlingBracket` class: generate, advance, record, finalize |
| 2 | `routes/scheduling/birling.py` | 261 | **LOGIC** — 5 routes: manage, generate, record, reset, finalize |
| 3 | `templates/scheduling/birling_manage.html` | 443 | **LOGIC** — Full management UI: seeding table, match cards, bracket viz, placements |
| 4 | `templates/scoring/birling_bracket.html` | 125 | **LOGIC** — Read-only bracket viewer (linked from scoring results) |
| 5 | `templates/scheduling/heat_sheets_print.html` | ~130 lines birling section (L440-570) | **LOGIC** — Bracket rendering in heat sheet print view |
| 6 | `tests/test_birling_bracket.py` | 287 | **LOGIC** — 7 test classes, covers generation/advancement/finals |
| 7 | `tests/test_birling_bracket_12.py` | 299 | **LOGIC** — 12-competitor realistic sim, men's + women's brackets |
| 8 | `tests/fixtures/synthetic_data.py` | ~15 lines birling (L648-670, L793-804) | **DATA** — Birling M/F event fixtures + BIRLING_MEN/WOMEN_BRACKET name lists (12 each) |
| 9 | `config.py` | ~15 lines birling-specific | **LOGIC** — Birling in COLLEGE_CLOSED_EVENTS, STAND_CONFIGS, RANKED_CATEGORIES, NO_CONSTRAINT_STAND_TYPES |
| 10 | `models/event.py` | 3 lines birling-relevant | **LOGIC** — `scoring_type='bracket'` enum, `uses_payouts_for_state` check |
| 11 | `routes/scoring.py` | 6 lines (L1237-1241) | **LOGIC** — Legacy redirect route `birling_bracket` -> `scheduling.birling_manage` |
| 12 | `routes/scheduling/__init__.py` | 1 line (L18) | **REF** — Lists birling.py in sub-module docstring |
| 13 | `routes/scheduling/ability_rankings.py` | ~50 lines birling-related | **LOGIC** — College birling seedings: per-school drag-drop ordering, `pre_seedings` stored in `Event.payouts` JSON |
| 14 | `routes/scheduling/heat_sheets.py` | ~15 lines (L129-146) | **LOGIC** — Builds `birling_brackets` list for heat sheet print template |
| 15 | `routes/main.py` | 3 lines (L620-622) | **LOGIC** — Tournament clone: detects birling events to clear `payouts` state |

### Files with incidental `match`/`fall` references (NOT birling-specific)

The grep for `match` and `fall` returned 83 files. After review, none contain birling-specific match/fall logic outside the files listed above. The word "match" appears in gear-sharing partner matching, template pattern matching, and regex contexts. "Fall" appears only in generic fallback logic.

---

## MODEL STATE

### Exists

| Model | Location | Birling Role |
|-------|----------|-------------|
| `Event` | `models/event.py` | `scoring_type='bracket'` distinguishes birling events; `uses_payouts_for_state` returns True for bracket events; `stand_type='birling'` |
| `EventResult` | `models/event.py` | `finalize_to_event_results()` writes `final_position` + `status='completed'` per competitor; no birling-specific fields |

### ABSENT

| Model | Status |
|-------|--------|
| **Bracket** | ABSENT — No dedicated Bracket model. Bracket state is serialized as JSON in `Event.payouts` TEXT column (same pattern as ProAmRelay and PartneredAxeThrow). |
| **Match** | ABSENT — No dedicated Match model. Matches are dicts within the bracket JSON: `{match_id, round, competitor1, competitor2, winner, loser, is_bye}`. |
| **BirlingResult** | ABSENT — No model for individual match results (falls, times, etc). The `BirlingBracket` service only tracks winner/loser per match, not the scoring details (e.g., best-of-3 falls). |

---

## ROUTE STATE

### Exists

| Route | Method | URL Pattern | Handler |
|-------|--------|-------------|---------|
| `birling_manage` | GET | `/scheduling/<tid>/event/<eid>/birling` | `routes/scheduling/birling.py:18` |
| `birling_generate` | POST | `/scheduling/<tid>/event/<eid>/birling/generate` | `routes/scheduling/birling.py:81` |
| `birling_record_match` | POST | `/scheduling/<tid>/event/<eid>/birling/record` | `routes/scheduling/birling.py:165` |
| `birling_reset` | POST | `/scheduling/<tid>/event/<eid>/birling/reset` | `routes/scheduling/birling.py:212` |
| `birling_finalize` | POST | `/scheduling/<tid>/event/<eid>/birling/finalize` | `routes/scheduling/birling.py:231` |
| `birling_bracket` (legacy) | GET | `/scoring/<tid>/event/<eid>/birling-bracket` | `routes/scoring.py:1237` — redirects to `birling_manage` |
| `ability_rankings` (birling seedings section) | GET/POST | `/scheduling/<tid>/ability-rankings` | `routes/scheduling/ability_rankings.py:103-143` (POST), `:197-248` (GET) |

### Missing

| Route | Description |
|-------|-------------|
| **Match detail / fall recording** | No route exists to record individual falls within a match (best-of-3, best-of-5). The current `birling_record_match` only takes `match_id` + `winner_id`. |
| **Undo match result** | No route to undo/reverse a recorded match result. Only `birling_reset` exists (full bracket wipe). |
| **Bracket view for spectators** | No public/portal route for spectators to view the bracket. The existing routes require judge auth via `require_judge_for_management_routes`. |

---

## SERVICE STATE

### Exists

| Service | Location | Lines | Description |
|---------|----------|-------|-------------|
| `BirlingBracket` | `services/birling_bracket.py` | 461 | Full class: `generate_bracket()`, `record_match_result()`, `get_current_matches()`, `get_placements()`, `finalize_to_event_results()` |
| `create_birling_bracket()` | `services/birling_bracket.py:442` | — | Factory function (thin wrapper) |
| `get_birling_bracket()` | `services/birling_bracket.py:459` | — | Accessor function (thin wrapper) |

### Service Internals

The `BirlingBracket` class implements:

- **Bracket generation** (`generate_bracket`, L47-148): Takes competitor list + optional seeding. Uses actual competitor count for compact scaling. For even fields, standard mirrored seed pairings (1 vs N, 2 vs N-1). For odd fields, top seed gets first-round bye. Generates winners bracket rounds with ceil division, losers bracket based on actual winners round counts, finals, and true finals.
- **Bye handling** (`_propagate_byes`, L150-156): Sweeps both winners and losers brackets for auto-advancement when opponents won't arrive.
- **Losers bracket generation** (`_generate_losers_bracket`, L158-194): Creates losers bracket structure based on actual winners round counts, alternating consolidation and drop-down rounds.
- **Match result recording** (`record_match_result`, L196-246): Sets winner/loser on match dict. Dispatches to advancement logic based on match_id prefix (W/L/F).
- **Winner advancement** (`_advance_winner`, L270-298): `W{r}_{m}` winner goes to `winners[r][(m-1)//2]`. Odd match number -> `competitor1`, even -> `competitor2`. Final winners round champion goes to `finals.competitor1`.
- **Loser drop-down** (`_drop_to_losers`, L300-332): `W{r}` loser drops to `losers[r-1][(m-1)//2]`. Fills first available slot.
- **Losers bracket advancement** (`_advance_loser_winner`, L333-360): `L{r}` winner goes to `losers[r][(m-1)//2]`. Final losers round winner goes to `finals.competitor2`.
- **Elimination tracking** (`_record_elimination`, L362-370): Position = `total_competitors - current_eliminations`.
- **Grand finals** (L232-244): Winners champ wins -> champion. Losers champ wins -> triggers true finals.
- **True finals** (L244-246): Winner is champion, loser is runner-up.
- **Finalization** (`finalize_to_event_results`, L412-439): Creates/updates `EventResult` records with `final_position` and `status='completed'`. Sets `event.status = 'completed'`.
- **Persistence**: All state stored in `Event.payouts` JSON field. `_save_bracket_data()` calls `db.session.commit()`.

### Missing

| Service | Description |
|---------|-------------|
| **Fall recording / match scoring** | No service to record individual falls (best-of-3) within a match. The service only tracks binary winner/loser. Real birling uses best-of-3 falls (first to 2 wins the match). |
| **Modified double elimination (ALA format)** | The current implementation is **standard double elimination only**. The ALA's modified format (where the losers bracket champion must beat the winners bracket champion *twice* in grand finals) is partially implemented — `true_finals` exists, which IS the "must beat twice" mechanic. However, ALA-specific seeding rules or format variations are not documented or configurable. |
| **AWFC college format** | No differentiation between ALA and AWFC bracket formats. The service has one format: standard double elimination with true finals. |
| **Bracket export / print** | No service to export bracket as PDF or standalone printable. The heat sheet print template renders it inline. |

---

## TEMPLATE STATE

### Exists

| Template | Location | Lines | Description |
|----------|----------|-------|-------------|
| `birling_manage.html` | `templates/scheduling/birling_manage.html` | 443 | Full management UI: seeding table with seed inputs, generate/regenerate button, "Matches Ready to Play" cards with winner selection buttons, full bracket visualization (winners/losers/finals with round labels), placements table with gold/silver/bronze badges, finalize button, reset button |
| `birling_bracket.html` | `templates/scoring/birling_bracket.html` | 125 | Read-only bracket viewer: winners bracket, losers bracket, grand finals, true finals (if needed), placements table. Linked from scoring event results page. |
| `heat_sheets_print.html` | `templates/scheduling/heat_sheets_print.html` (L440-570) | ~130 | Print-optimized bracket rendering: winners/losers/finals with section-title color bands (blue/purple/green), match slots with winner/loser styling, placement badges |

### Template Macros

- `birling_manage.html`: `render_slot(comp_id, winner_id, loser_id)` — renders competitor name with winner/loser/TBD styling. `render_match(match, playable_ids)` — renders full match card with header, both slots, playable/completed state.
- `birling_bracket.html`: `render_slot(comp_id, winner_id)` — simpler variant. `render_match(match)` — simpler variant without playable state.
- `heat_sheets_print.html`: `bracket_slot(comp_id, winner_id, loser_id, lookup)` — print variant. `bracket_match(match, lookup)` — print variant with bye handling.

### Missing

| Template | Description |
|----------|-------------|
| **Fall recording UI** | No template for recording individual falls within a match (e.g., "Fall 1: Competitor A won", "Fall 2: Competitor B won", "Fall 3: Competitor A won -> A wins match"). The current UI is a single "click winner" button per match. |
| **Spectator bracket view** | No portal template for public bracket display. |
| **Match history / log** | No template showing the sequence of match results (who beat whom, when). |

---

## DEPENDENCY MAP

```
config.py
  ├── COLLEGE_CLOSED_EVENTS → {'name': 'Birling', 'scoring_type': 'bracket', 'stand_type': 'birling', 'is_gendered': True}
  ├── STAND_CONFIGS → {'birling': {'total': 1, 'labels': ['Pond']}}
  ├── NO_CONSTRAINT_STAND_TYPES → includes 'birling'
  ├── RANKED_CATEGORIES → includes 'birling'
  ├── CATEGORY_DISPLAY_NAMES → {'birling': 'Birling'}
  ├── CATEGORY_DESCRIPTIONS → {'birling': "Double-elimination bracket seeding (Men's and Women's)"}
  └── event_rank_category() → returns 'birling' for stand_type='birling'

models/event.py
  ├── Event.scoring_type → 'bracket' value used to identify birling events
  ├── Event.payouts → JSON TEXT column repurposed to store bracket state
  └── Event.uses_payouts_for_state → returns True when scoring_type == 'bracket'

services/birling_bracket.py
  ├── IMPORTS: database.db, models.Event, models.EventResult
  ├── IMPORTED BY: routes/scheduling/birling.py (deferred import inside functions)
  │                 routes/scheduling/heat_sheets.py (deferred import inside function)
  ├── TOUCHES DB MODELS: Event (reads/writes .payouts), EventResult (creates/updates in finalize)
  └── PERSISTENCE: Event.payouts JSON field

routes/scheduling/birling.py
  ├── IMPORTS: flask (abort, flash, jsonify, redirect, render_template, request, url_for)
  │            database.db, models (Event, EventResult, Tournament)
  │            models.competitor (CollegeCompetitor, ProCompetitor)
  │            services.audit (log_action)
  │            routes.scheduling (scheduling_bp, _signed_up_competitors)
  ├── DEFERRED IMPORTS: services.birling_bracket.BirlingBracket (in each route function)
  ├── SERVES TEMPLATES: scheduling/birling_manage.html
  └── AUDIT ACTIONS: birling_bracket_generated, birling_match_recorded, birling_bracket_reset, birling_bracket_finalized

routes/scheduling/heat_sheets.py
  ├── Builds birling_brackets list (L129-146) for heat_sheets_print.html
  └── DEFERRED IMPORT: services.birling_bracket.BirlingBracket

routes/scoring.py
  └── Legacy redirect: birling_bracket route (L1237-1241) → scheduling.birling_manage

routes/scheduling/ability_rankings.py
  ├── College birling seedings section (L103-143 POST, L197-248 GET)
  ├── Stores pre_seedings in Event.payouts JSON (per-school ordering)
  └── Template: scheduling/ability_rankings.html (birling_events_data passed to template)

routes/main.py
  └── Tournament clone (L618-622): detects birling events via stand_type='birling', clears payouts to empty state

tests/test_birling_bracket.py
  ├── IMPORTS: services.birling_bracket.BirlingBracket
  └── Uses unittest.mock to mock db and Event

tests/test_birling_bracket_12.py
  ├── IMPORTS: services.birling_bracket.BirlingBracket, tests.fixtures.synthetic_data (BIRLING_MEN_BRACKET, BIRLING_WOMEN_BRACKET)
  └── Uses unittest.mock to mock db and Event

tests/fixtures/synthetic_data.py
  ├── COLLEGE_EVENTS dict: 'Birling M' and 'Birling F' entries with scoring_type='bracket'
  ├── BIRLING_MEN_BRACKET: 12 competitor names
  └── BIRLING_WOMEN_BRACKET: 12 competitor names
```

---

## GAP ANALYSIS

### 1. Is there a Bracket model? A Match model? A BirlingResult model?

**Bracket model: ABSENT.** Bracket state is a JSON blob stored in `Event.payouts` (TEXT column). Structure: `{bracket: {winners: [[...]], losers: [[...]], finals: {...}, true_finals: {...}}, competitors: [...], seeding: [...], current_round: str, placements: {comp_id: position}}`.

**Match model: ABSENT.** Matches are plain dicts within the JSON: `{match_id: str, round: str, competitor1: int|None, competitor2: int|None, winner: int|None, loser: int|None, is_bye: bool}`. No timestamps, no fall counts, no metadata.

**BirlingResult model: ABSENT.** No per-match result recording (falls, times, etc). Only binary winner/loser.

### 2. Is there a bracket generation service?

**Yes** — `services/birling_bracket.py`, `BirlingBracket.generate_bracket()` (L47-148).

- **Seeding:** Yes. Accepts optional seeding list (ordered competitor IDs). Falls back to registration order. Route reads manual seed inputs from form OR pre_seedings from ability rankings page. Standard 1-vs-N bracket seeding (seed 1 vs seed N, seed 2 vs seed N-1, etc). (`services/birling_bracket.py:73-82`)
- **Bye assignment:** Yes. Computes `bracket_size = 2^ceil(log2(N))`, creates `bracket_size - N` bye matches, auto-advances non-bye competitor. (`services/birling_bracket.py:69-98`)
- **Modified double elimination (ALA format):** Partially. The `true_finals` mechanic IS the ALA "must beat the winners champ twice" rule — if the losers champ wins grand finals, a true finals is triggered. However, there is no format flag to distinguish ALA vs AWFC vs standard. The current implementation matches standard double elimination with optional true finals. (`services/birling_bracket.py:126-143, 232-244`)
- **Standard double elimination (AWFC college format):** The current implementation IS standard double elimination. No AWFC-specific logic differentiation exists.

### 3. Is there bracket advancement logic?

**Yes** — fully implemented.

- **Winner advancement:** `_advance_winner()` at `services/birling_bracket.py:270-298`. Winner of `W{r}_{m}` advances to `winners[r][(m-1)//2]`. Slot assignment: odd match → competitor1, even → competitor2. Final winners round champion → `finals.competitor1`.
- **Loser routing:** `_drop_to_losers()` at `services/birling_bracket.py:300-332`. Loser of `W{r}_{m}` drops to `losers[r-1][(m-1)//2]`.
- **Losers bracket advancement:** `_advance_loser_winner()` at `services/birling_bracket.py:333-360`. Same pattern as winners.
- **Grand finals + true finals:** `record_match_result()` at `services/birling_bracket.py:232-244`. Handles both F1 (grand finals) and F2 (true finals).

### 4. Is there a scoring interface for birling?

**Partial.** The current UI (`birling_manage.html:229-262`) shows "Matches Ready to Play" cards with two buttons — one per competitor. Clicking a button declares that competitor the winner of the entire match. There is **no fall-by-fall recording**. A judge cannot record "Fall 1: A won", "Fall 2: B won", "Fall 3: A won" — they can only declare the final match winner.

The `record_match_result()` service method (`services/birling_bracket.py:196`) takes `match_id` and `winner_id` only. No `falls` parameter exists.

### 5. Is birling in config.py's events list?

**College: YES.** `config.py:403` — `{'name': 'Birling', 'scoring_type': 'bracket', 'stand_type': 'birling', 'is_gendered': True}` in `COLLEGE_CLOSED_EVENTS`. Gender-segregated (creates separate M and F events).

**Pro: CORRECTLY ABSENT.** `config.py:408-425` — `PRO_EVENTS` does not include birling. Confirmed: no birling entry in the pro events list.

### 6. Are there templates for displaying a bracket?

**Yes — three templates:**

1. **`templates/scheduling/birling_manage.html`** (443 lines) — Full management interface with seeding, generation, match recording, bracket visualization, and finalization. Judge-only.
2. **`templates/scoring/birling_bracket.html`** (125 lines) — Read-only bracket viewer. Accessed via scoring results page. Judge-only.
3. **`templates/scheduling/heat_sheets_print.html`** (L440-570) — Bracket rendered inline in the heat sheet print view. Print-optimized CSS.

**Missing:** No spectator/public bracket view template.

### 7. What is the relationship between birling and the existing Heat/Flight system?

**Birling is entirely separate from Heats/Flights.** Evidence:

- `heat_generator.py` contains zero references to birling or bracket (`grep` returned no matches).
- `flight_builder.py` contains zero references to birling or bracket (`grep` returned no matches).
- `scoring_engine.py` contains zero references to birling or bracket (`grep` returned no matches).
- `routes/scheduling/heat_sheets.py:132` explicitly skips bracket events from heat card rendering: `if event.scoring_type == 'bracket': ... continue`.
- Birling events have `stand_type='birling'` with `STAND_CONFIGS['birling'] = {total: 1, labels: ['Pond']}` — 1 stand, no heat generation needed.
- The bracket service stores all state in `Event.payouts` JSON, not in `Heat` or `HeatAssignment` records.
- Bracket events are rendered separately on heat sheet print pages (dedicated section after all heat cards).

Birling does NOT use heats. It has its own parallel system: `BirlingBracket` service + birling routes + birling templates.

---

## GAP SUMMARY

Numbered list of everything missing for a fully functional college birling bracket system:

1. **No Match model** — Matches are anonymous JSON dicts with no database identity, no timestamps, no audit trail per match. Cannot query match history independently of the bracket blob.

2. **No fall recording** — Real birling is best-of-3 (or best-of-5) falls. The current system only records the final match winner. A judge has no way to record individual falls, track fall count, or correct a fall entry. The winner button is binary.

3. **No match undo** — Once a match result is recorded, the only way to correct it is to reset the entire bracket (`birling_reset`). No single-match undo or result correction exists.

4. **No spectator bracket view** — The bracket is only visible to judges (management routes behind `require_judge_for_management_routes`). Spectators in the portal cannot view the bracket.

5. **No format differentiation (ALA vs AWFC)** — The system has one format: standard double elimination with true finals. No flag to switch between ALA modified format and AWFC college format. The `true_finals` mechanic partially implements ALA rules, but there is no explicit format selection.

6. **No bracket seeding from STRATHMARK** — Seeding is either manual (form inputs) or from ability rankings page. No integration with STRATHMARK predictions for birling seeding.

7. **Losers bracket structure may be incorrect for non-power-of-2 fields** — The `_generate_losers_bracket()` method (L158-194) uses `bracket_size // (2 ** (w_round + 2))` for match counts, which may not produce the correct number of losers bracket rounds for all field sizes. For a 12-competitor (16-slot) bracket, this produces `[4, 2, 1, 1]` losers rounds, but standard double elimination for 16 should have more rounds to accommodate all drop-downs. This needs verification against actual ALA bracket sheets.

8. **No bracket points integration** — `finalize_to_event_results()` writes `final_position` but does not call `add_points()` or otherwise integrate with the college points system. The scoring/reporting routes may handle this separately, but the finalization path does not explicitly trigger point calculation.

9. **No match metadata** — No recording of: match duration, which competitor fell first in each fall, pond conditions, or any other match context. The match dict has only `match_id`, `competitor1`, `competitor2`, `winner`, `loser`, `is_bye`.

10. **No bracket progression validation** — The service does not validate that matches are played in correct order. A judge could theoretically record a round 3 match before round 2 is complete. The `get_current_matches()` method only returns matches where both competitors are filled and no winner is set — this provides implicit ordering, but there is no explicit round-completion check.

11. **Bracket data not exposed via API** — The public REST API (`routes/api.py`) has no endpoint for bracket state. Spectator-facing apps cannot fetch bracket data.

12. **No bracket regeneration with preserved results** — `birling_generate` always creates a fresh bracket. If a bracket is regenerated (e.g., due to a late scratch), all prior match results are lost.

13. **`create_birling_bracket()` and `get_birling_bracket()` factory functions (L442-461) are unused by route code** — Routes directly instantiate `BirlingBracket(event)`. The factory functions exist but are dead code.

14. **No SMS/notification integration** — No notification sent when a match is ready to play or when bracket is finalized. The SMS service exists but is not wired to birling events.
