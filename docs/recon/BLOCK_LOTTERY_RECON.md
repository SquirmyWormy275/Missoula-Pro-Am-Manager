# BLOCK LOTTERY / BLOCK CARD PRINT — RECON

Read-only reconnaissance. No code changes made. Facts only.

Working repo: Missoula-Pro-Am-Manager
Generated: 2026-04-23


## 1. EXISTING FEATURE STATUS

**A "Block Lottery" feature already exists in the codebase.** It is live,
reachable from multiple entry points, and has a printable view. It does
NOT currently produce cut-apart notecards — it produces a screen/print
list of competitors per block group.

### Route / endpoint

| Endpoint             | URL                             | Handler                           |
|----------------------|---------------------------------|-----------------------------------|
| `woodboss.lottery`   | `GET /woodboss/<int:tid>/lottery` | `routes/woodboss.py:387` `lottery(tid)` |

There is also a public HMAC-share route that targets the printable wood
REPORT (not the lottery): `woodboss_public_bp.share` →
`/woodboss/<int:tid>/share?token=...` at `routes/woodboss.py:412`. The
lottery itself has no public share route.

No other routes matched the search terms `lottery`, `block_lottery`,
`block_assign`, `block_card`, `block_draw` (excluding `proam_relay`
routes and docs references).

### Template

- `templates/woodboss/lottery.html` (250 lines).
- Extends `base.html`. Renders two layouts through a single template:
  on-screen dashboard view AND browser print view (`@media print` block
  forces `.lottery-columns { grid-template-columns: repeat(3, 1fr); }`
  with a white print palette).
- Renders as columns keyed by `(species, size)`. Each column contains
  event-section headers and a `<ul class="comp-list">` list of
  competitor rows. Each row has: empty checkbox div, competitor name,
  optional affiliation badge.
- Has Print button (`data-print`) and Back-to-Woodboss-dashboard button.
- No @page CSS, no WeasyPrint code path — browser Ctrl-P only.

### Service

- `services/woodboss.py:851` `get_lottery_view(tournament_id)`.
- Returns a list of column dicts:
  ```
  [{
    'species': str,
    'size_display': str,           # e.g. '13"'
    'total_blocks': int,
    'sections': [
        {'config_label': str,      # e.g. 'Underhand — College Men'
         'event_name': str,        # e.g. 'Underhand Hard Hit'
         'competitors': [{'name': str, 'affiliation': str}]},
        ...
    ]
  }, ...]
  ```
- Relay blocks emit placeholder names `"Relay Team {i+1}"` when
  `count_override` is set on `block_relay_underhand` /
  `block_relay_standing` WoodConfig rows.

### Current output

Printable LIST of competitor names grouped by species/size/event, with
a checkbox beside each name. Designed as a "day-of note-card prep
worksheet" per the Woodboss dashboard card copy
(`templates/woodboss/dashboard.html:159` — "Day-of view for note card
prep. Lists every competitor per event, grouped by block species and
size. Printable.").

It is NOT a cut-apart notecard grid (1 card = 1 block). It is a wall /
clipboard sheet listing everyone who needs a card, which staff then
manually transcribe onto cards.

### Sample output content (from `templates/woodboss/lottery.html:196-244`)

```
[Column: Western White Pine 13" — 28 blocks]

   UNDERHAND — COLLEGE MEN       (count badge: 12)
   Underhand Hard Hit
   ☐ Andrews, J                 [MT]
   ☐ Beaumont, K                [HCC]
   ...

   UNDERHAND — PRO MEN           (count badge: 6)
   Underhand
   ☐ Carter, A                  [ ]
   ...
```

### Entry points already wired

| Location                                      | File:line                                 |
|-----------------------------------------------|-------------------------------------------|
| Woodboss dashboard card                       | `templates/woodboss/dashboard.html:151-170` |
| Tournament detail (Phase-B panel)             | `templates/tournament_detail.html:572-578`  |
| Scheduling events page — Build tab            | `templates/scheduling/events.html:620-632`  |
| Scheduling events page — Friday Ops quick act | `templates/scheduling/events.html:661-664`  |
| Sidebar — "Virtual Woodboss" (active-state)   | `templates/_sidebar.html:155-161`           |

The lottery is reachable today from the Woodboss sidebar entry (parent
link points at the Woodboss report, but the active-state highlight
includes `woodboss.lottery`), from the tournament dashboard, and from
the scheduling events page. No direct sidebar row is currently
dedicated to Block Lottery.

### NOT present in Print Hub catalog

`services/print_catalog.py:520-668 PRINT_DOCUMENTS` lists 16 PrintDoc
entries (Setup / Run Show / Results / Compliance). **No entry targets
`woodboss.lottery`.** Closest existing entry is `woodboss_report` →
`woodboss.report_print` for the wood/log inventory report — a different
document.


## 2. EVENT-TO-BLOCK MAPPING

Events that consume a dedicated physical wood block per competitor per
run. Sourced from `config.py` event definitions and
`services/woodboss.py:30-70 BLOCK_EVENT_GROUPS`.

### Block-consuming events (currently mapped in code)

| Event name (config)         | Division | `stand_type`     | Gendered | Partnered | `requires_dual_runs` | `requires_triple_runs` | Block config_key(s)                                   |
|-----------------------------|----------|------------------|----------|-----------|----------------------|------------------------|-------------------------------------------------------|
| Underhand Hard Hit          | college  | `underhand`      | yes      | no        | False                | False                  | `block_underhand_college_M` / `..._F`                 |
| Underhand Speed             | college  | `underhand`      | yes      | no        | False                | False                  | same as above                                         |
| Standing Block Hard Hit     | college  | `standing_block` | yes      | no        | False                | False                  | `block_standing_college_M` / `..._F`                  |
| Standing Block Speed        | college  | `standing_block` | yes      | no        | False                | False                  | same as above                                         |
| 1-Board Springboard         | college  | `springboard`    | yes      | no        | False                | False                  | `block_springboard_college_M` / `..._F`               |
| Underhand                   | pro      | `underhand`      | yes      | no        | False                | False                  | `block_underhand_pro_M` / `..._F`                     |
| Standing Block              | pro      | `standing_block` | yes      | no        | False                | False                  | `block_standing_pro_M` / `..._F`                      |
| Springboard (2-Board)       | pro      | `springboard`    | no       | no        | False                | False                  | `block_springboard_pro`                               |
| Pro 1-Board                 | pro      | `springboard`    | no       | no        | False                | False                  | `block_1board_pro`                                    |
| 3-Board Jigger              | pro      | `springboard`    | no       | no        | False                | False                  | `block_3board_pro`                                    |
| (Pro-Am Relay — Underhand)  | relay    | n/a              | n/a      | relay     | n/a                  | n/a                    | `block_relay_underhand` (manual `count_override`)     |
| (Pro-Am Relay — Standing)   | relay    | n/a              | n/a      | relay     | n/a                  | n/a                    | `block_relay_standing`   (manual `count_override`)    |

### Events NOT currently mapped to a block in code

| Event             | Division | `stand_type`   | Notes                                                                                                         |
|-------------------|----------|----------------|---------------------------------------------------------------------------------------------------------------|
| Chokerman's Race  | college  | `chokerman`    | `requires_dual_runs=True`. Stand labels `Course 1`/`Course 2`. No `BLOCK_EVENT_GROUPS` mapping. If the chopping portion consumes a block per competitor per run, this is a GAP. |
| Caber Toss        | college  | `caber`        | `requires_dual_runs=True`. Uses a caber (log), not a sawn block. No block mapping today, consistent with domain. |
| Axe Throw         | college  | `axe_throw`    | List-only (`LIST_ONLY_EVENT_NAMES` in config.py:464). No block.                                               |
| Peavey Log Roll   | college  | `peavey`       | List-only. No block.                                                                                          |
| Pulp Toss         | college  | `pulp_toss`    | List-only. No block.                                                                                          |
| Partnered Axe Thr | pro      | `axe_throw`    | `requires_triple_runs=True`. No block.                                                                        |
| Speed Climb       | college  | `speed_climb`  | `requires_dual_runs=True`. Pole-based. No block.                                                              |
| Pole Climb        | pro      | `speed_climb`  | Pole-based. No block.                                                                                         |
| Birling           | college  | `birling`      | Pond. No block.                                                                                               |
| Single Buck / Double Buck / Jack & Jill Sawing | both | `saw_hand` | Consume saw-log inches (not blocks). Tracked via `services/woodboss.py` SAW_EVENTS + log_general/log_stock. |
| Stock Saw         | both     | `stock_saw`    | Consumes stock-saw log inches, not blocks.                                                                    |
| Hot Saw           | pro      | `hot_saw`      | Hot-saw log inches, not blocks.                                                                               |
| Obstacle Pole     | both     | `obstacle_pole`| Uses log_op independently. Not blocks.                                                                        |
| Cookie Stack      | pro      | `cookie_stack` | Uses log_cookie (whole logs), not blocks.                                                                     |

### Run 1 vs Run 2 separation

- NONE of the chopping / springboard events currently declared in
  `config.py` carry `requires_dual_runs=True`. In the live data model,
  each of these events has only `run_number=1` Heat rows. Block count
  today = 1 physical block per enrolled competitor, period.
- `services/heat_generator.py:283-312` is the ONLY place that emits
  `run_number=2` Heat rows; it is gated on `event.requires_dual_runs`.
  The only dual-run events that exist are Chokerman's Race, Speed
  Climb, and Caber Toss — none of which are in `BLOCK_EVENT_GROUPS`.
- `Event.requires_triple_runs` (Axe Throw, Partnered Axe Throw) does
  NOT create a second heat — three throws are recorded on a single
  heat. No extra block implication.
- Hard Hit variants (Underhand Hard Hit, Standing Block Hard Hit) use
  `scoring_type='hits'` — they accumulate hit counts in one heat. They
  do NOT carry `requires_dual_runs=True` in config. Current model = 1
  block per competitor.

**Implication for the new feature**: if the new card-print feature has
to emit "Run 1" and "Run 2" cards for chopping events (user's stated
assumption), that Run 1 / Run 2 concept is NOT present in the current
data model for chopping events. It would have to be synthesized by the
new feature (e.g., by hardcoding "chopping events always use 2 blocks
per competitor" or by adding `requires_dual_runs=True` to those
events, which would materially change heat generation).

### Gender / partner shape per block event

| Event                  | Gendered | Partnered |
|------------------------|----------|-----------|
| Underhand (college)    | yes      | no        |
| Underhand (pro)        | yes      | no        |
| Standing Block (col)   | yes      | no        |
| Standing Block (pro)   | yes      | no        |
| Springboard (col)      | yes      | no        |
| Pro Springboard (2-Bd) | no (open)| no        |
| Pro 1-Board            | no       | no        |
| 3-Board Jigger         | no       | no        |

### Where per-event block count would be derived from in code

- **Block count** is enrollment-driven: `services/woodboss.py:467`
  `calculate_blocks(tournament_id)`. It calls `_count_competitors(tid)`
  at line 259 which queries `CollegeCompetitor.query.filter_by(
  tournament_id=tid, status='active').all()` and the matching pro
  query at line 315. Events per competitor come from
  `Competitor.get_events_entered()`.
- **Per-event active competitor list** (gender-filtered, partner-
  resolved, same shape as preflight and heat-gen): `services/
  preflight.py:31` `_signed_up_competitors_for_event(event)` returns
  `list[Competitor]`. This is the function that would yield the card
  rows for a single event.


## 3. BLOCK COUNT SOURCE

### Primary source of truth

`services/woodboss.py:259` `_count_competitors(tournament_id)`.

Signature: `_count_competitors(tournament_id) -> defaultdict(int)`.

Returns: `{(event_name_lower, 'college'|'pro', 'M'|'F'): int}`.

Behavior:
- Queries `CollegeCompetitor` and `ProCompetitor` with
  `status='active'` — SCRATCHED competitors are excluded. Status
  domain is enforced by `models/competitor.py:24` /
  `models/competitor.py:218` check constraints
  (`status IN ('active', 'scratched')`).
- Pro events: resolves event IDs (or name-string fallbacks from legacy
  Excel imports) to Event rows via `_get_pro_event_map`. Uses
  `event.gender` if set, else competitor gender.
- College events: resolves event names (or IDs) to Event rows via
  a name-map and id-map. Uses competitor gender.
- Does NOT differentiate Run 1 / Run 2 — there is no run dimension in
  the returned key. It is a flat enrollment count.
- Late additions: any competitor newly inserted with `status='active'`
  will be counted on the next call. The function has no caching
  besides the optional `counts` / `configs` kwargs on public callers
  (`calculate_blocks`, `get_lottery_view`, `calculate_saw_wood`). Each
  HTTP request re-queries.

### Downstream callers

- `services/woodboss.py:467 calculate_blocks()` — returns one row per
  BLOCK_CONFIG_LABELS key with `competitor_count`, `is_manual`,
  `count_override`. Feeds the `/woodboss/<tid>/report` UI.
- `services/woodboss.py:851 get_lottery_view()` — returns the columns
  used by `templates/woodboss/lottery.html` (used by the existing
  block lottery page).

### Per-event (not aggregated) competitor listing

`services/preflight.py:31 _signed_up_competitors_for_event(event) ->
list[CollegeCompetitor | ProCompetitor]`. Filters on
`status='active'`, matches gender if event is gendered, resolves
enrollment entries by event ID first then by name/display_name. This
mirrors heat generator's enrollment resolution.

### Scratched / late-adds summary

- Scratched: excluded everywhere block count is computed (both
  `_count_competitors` and `_signed_up_competitors_for_event` filter
  on `status='active'`).
- Late additions: counted immediately on the next call — no cache
  that would need invalidation for block count.
- Run 1 / Run 2 differentiation: **NOT in the current data model for
  chopping events**. For events that genuinely have Run 1 + Run 2
  heats (Chokerman, Speed Climb, Caber Toss), none are currently
  mapped to a block config_key.


## 4. PRINT INFRASTRUCTURE

### WeasyPrint status

- Shared helper: `services/print_response.py:18` `weasyprint_or_html(
  html: str, filename: str) -> (body, status, headers)`.
- Branch A (WeasyPrint importable): returns PDF bytes, `Content-Type:
  application/pdf`, `Content-Disposition: attachment;
  filename="<name>.pdf"`.
- Branch B (ImportError): returns the HTML body with `Content-Type:
  text/html` so the browser can Ctrl-P.
- Railway production does NOT bundle WeasyPrint (cairo/pango/gdk-
  pixbuf weight). All PDF routes degrade to HTML on Railway. This is
  known and documented (`docs/VIDEO_JUDGE_BRACKET_RECON.md:442-464`,
  `docs/plans/2026-04-21-002-feat-print-hub-and-pro-checkout-roster.md:338-345`).

### Routes that use `weasyprint_or_html`

| Endpoint                                  | File:line                                    |
|-------------------------------------------|----------------------------------------------|
| `scheduling.heat_sheets` (master print)   | `routes/scheduling/heat_sheets.py:368-392`   |
| `scheduling.friday_feature_pdf`           | `routes/scheduling/friday_feature.py:260-286`|
| `scheduling.pro_checkout_roster_print`    | `routes/scheduling/pro_checkout_roster.py:85-97` |
| `scheduling.birling_print_*`              | `routes/scheduling/birling.py:523, 597`      |
| `scheduling.print_hub_email`              | `routes/scheduling/print_hub.py:336-345`     |
| `scoring.heat_sheet_pdf`                  | `routes/scoring.py:1487-1511`                |
| `scoring.judge_sheet_for_event` / `_all`  | `routes/scoring.py:1529+`                    |

Routes that DON'T use the helper and rely on `@media print` fallback
only:
- `woodboss.report_print` (`routes/woodboss.py:370`) — the live wood
  report print page. Prints via browser only.
- `woodboss.lottery` — same (browser Ctrl-P).
- `scheduling.day_schedule_print`
- `scheduling.heat_sheets` internals (master template) — note: the
  separate `scheduling.heat_sheets` master route DOES go through
  weasyprint_or_html per the heat_sheets.py:368 reference above.

### Existing print templates (confirmed live)

| Template                                               | Kind              | Layout                      |
|--------------------------------------------------------|-------------------|-----------------------------|
| `templates/scoring/heat_sheet_print.html`              | Heat sheet        | Standalone, @page CSS        |
| `templates/scheduling/heat_sheets_print.html`          | Heat sheet master | Standalone                   |
| `templates/scheduling/day_schedule_print.html`         | Day schedule      | Standalone                   |
| `templates/scheduling/friday_feature_print.html`       | FNF schedule      | Standalone                   |
| `templates/scheduling/pro_checkout_roster_print.html`  | Roster            | Standalone                   |
| `templates/scheduling/relay_teams_sheet_print.html`    | Team roster       | Landscape, 2-col per team    |
| `templates/scoring/judge_sheet.html`                   | Judge sheet       | Standalone, inline CSS       |
| `templates/scoring/birling_bracket_print.html`         | Bracket           | Standalone, inline CSS       |
| `templates/reports/all_results_print.html`             | Results summary   | 2-col print grid of events   |
| `templates/reports/payout_summary_print.html` (et al)  | Payout reports    | Print-styled via base        |
| `templates/reports/college_standings_print.html`       | Standings         | Print-styled via base        |
| `templates/reporting/ala_membership_report.html`       | ALA report        | Generates real PDF (special, bypasses weasyprint_or_html) |
| `templates/woodboss/report_print.html`                 | Wood/log report   | 2-col species grid (blocks + logs) |
| `templates/woodboss/lottery.html`                      | Block lottery     | 3-col print grid in `@media print` |

### Existing multi-card-per-page / 4-up / 6-up / 8-up grid templates

**NONE exist.** `Grep` for `grid-template-columns` across
`templates/` surfaced 7 files. All are 2-col or 3-col dashboard/print
grids for report summaries (events grid, species grid, team roster
grid, hit-counter keypad). None is a cut-apart notecard sheet where
each grid cell is a physically detachable card.

Closest structural analogs:
- `templates/woodboss/lottery.html` — 3-col print grid with per-
  column stacked event sections (column = species/size, cell = event
  section).
- `templates/scheduling/relay_teams_sheet_print.html` — landscape
  page, 2-col per team table. Team label + member list.
- `templates/reports/all_results_print.html` — 2-col events grid for
  results.

If the new feature wants true N-up cut-apart cards (card borders,
crop marks, fixed card size like 3×5 notecards), it is a new template
pattern for this codebase.


## 5. NAV PLACEMENT

### Sidebar structure (`templates/_sidebar.html`)

Tournament sidebar has 5 sections (Overview standalone + 4 grouped):

```
Overview
SECTION 1: Competitors (sb-entries)
  - College Day
  - College Operations (child)
  - Pro Day
  - Pro Operations (child)
  - Event Fees (child)
  - Fee Tracker (child)
  - ALA Report (child)
SECTION 2: Setup (sb-config)
  - Configure Events
  - Fri Feature / Sat Overflow
  - Preflight Check
  - Virtual Woodboss             ← active-state covers woodboss.lottery
SECTION 3: Run Show (sb-scoring)
  - Build Schedule
  - Friday Operations
  - Saturday Operations
  - Heat Sheets                  (child)
  - Print Hub                    (child)
  - Video Judge Workbook         (child)
  - Saw Block Status             (child, conditional: saw_hand event exists)
  - Birling Brackets             (child)
  - Kiosk Display                (child, opens in new tab)
SECTION 4: Results (sb-results)
  - All Results
  - College Standings
  - Configure Payouts
  - Pro Payouts
SECTION 5 (admin-only): Admin (sb-admin)
  - Users
  - Audit Log
```

### Natural landing spots for a "Print Block Cards" entry

Three viable locations, ordered by cohesion with existing IA:

1. **Run Show group, as a sidebar-child under Heat Sheets / Print Hub
   / Saw Block Status** (`_sidebar.html:197-225`). This matches the
   user's suggested placement and is where operators are reaching for
   print deliverables on race day.

2. **Print Hub catalog entry** (`services/print_catalog.py:520-668
   PRINT_DOCUMENTS`). A new `PrintDoc(key='block_cards',
   section=SECTION_RUN_SHOW, ...)` would surface it on the existing
   Print Hub page without adding a new sidebar row. This is the
   low-chrome addition and matches how all other print deliverables
   are catalogued.

3. **Setup group, under Virtual Woodboss** (current location for the
   existing Block Lottery, via the sidebar active-state mapping). The
   existing lottery is reached through the Woodboss dashboard, not a
   direct sidebar child. Adding a direct child here would promote
   both the existing lottery AND the new card printer.

The existing conditional pattern at
`templates/_sidebar.html:217-225` for `Saw Block Status` (only shows
when a `saw_hand` event exists) is a precedent for gating the new
entry on "at least one chopping event is configured."


## 6. GAP SUMMARY

Enumerated list of what is missing for a cut-apart notecard print
feature that produces exactly one card per physical block.

1. **No notecard grid template.** Zero existing templates implement
   an N-up cut-apart card layout with card borders, fixed card
   dimensions, or crop marks. `templates/woodboss/lottery.html` is
   the closest analog but it is a list view with checkboxes, not
   separable cards.

2. **No per-card renderer service.** `get_lottery_view()` returns
   grouped columns of `{name, affiliation}`. There is no service
   function that yields a flat list of `{name, event, run_number,
   block_config_key, species, size}` card records. The new feature
   would need either a new service function or a template-side flat
   iteration of the existing nested structure.

3. **Run 1 / Run 2 dimension is not modeled for chopping events.**
   `Event.requires_dual_runs` is `False` for every event in
   `BLOCK_EVENT_GROUPS`. The data model currently says "one block
   per competitor per chopping event." If the new feature needs to
   print separate Run 1 and Run 2 cards (user's stated assumption in
   the recon brief), that run count has to come from somewhere new:
   either (a) a hardcoded `BLOCK_CARDS_PER_COMPETITOR` rule in the new
   service, (b) a new `blocks_per_competitor` column on Event, or (c)
   flipping `requires_dual_runs` to True on chopping events (which
   would change heat generation and Run 1 / Run 2 heat emission
   behavior — high-blast-radius change).

4. **Chokerman's Race is unmapped.** `Chokerman's Race` has
   `requires_dual_runs=True` and stand type `chokerman`. The recon
   brief says the chopping portion uses a block. There is NO entry
   in `BLOCK_EVENT_GROUPS` for `chokerman`. If Chokerman cards must
   be printed, this is a new mapping (plus a wood config — no
   `block_chokerman_*` config_key exists).

5. **No Print Hub catalog entry for the existing Block Lottery.**
   `services/print_catalog.py:PRINT_DOCUMENTS` has 16 docs; none
   targets `woodboss.lottery` or a new card route. A new `PrintDoc`
   entry (key, label, section, route_endpoint, status_fn,
   fingerprint_fn) is required if the new feature should surface
   through Print Hub's status/staleness tracking.

6. **No WeasyPrint branch on the existing lottery route.**
   `routes/woodboss.py:386 lottery()` renders the HTML template
   directly — no `weasyprint_or_html` call. If the new feature wants
   a PDF-per-request option (for Railway-local dev or ops with
   WeasyPrint installed), the new route needs to go through
   `services/print_response.py:weasyprint_or_html`.

7. **No sidebar row for Block Lottery / Block Cards.** The existing
   Block Lottery is reachable only indirectly — through the
   Woodboss dashboard, tournament_detail Phase-B panel, or the
   Scheduling Events page. No direct entry in `templates/_sidebar.html`.
   A new "Print Block Cards" sidebar-child under Run Show would be
   a new row.

8. **Block count source does not carry the per-run dimension.**
   `_count_competitors()` returns
   `{(event_name_lower, comp_type, gender): int}`. There is no
   `{..., run_number: int}` variant. Emitting "card per competitor
   per run" requires a new projection on top of the existing count.

9. **Affiliation field is empty for pro competitors.**
   `services/woodboss.py:406` sets `'affiliation': ''` for every pro
   competitor row in `_list_competitors()`. If the new card needs
   any pro-side identifier beyond the name (bib number, team,
   hometown), that data flow is not wired.

10. **No run-aware stand assignment on chopping-event heats.** The
    existing Hard Hit and block events emit `run_number=1` heats
    only; there is no second set of stand assignments to hang a "Run
    2 block card" off. Any Run 1 / Run 2 card feature has to
    fabricate the run dimension out of scope of the existing Heat
    model (see gap 3).

11. **No idempotent regeneration contract.** The existing lottery
    view is recomputed on every GET and is tolerant to late-adds
    and scratches. If the new feature persists card-to-block
    assignments (e.g. "Competitor Smith got block #17"), that
    persistence layer does not exist — no model, no table, no
    migration — and the idempotence semantics need to be defined
    (re-run wipes and re-draws? re-run preserves existing and
    only appends? mid-show scratch edits the existing draw?).

12. **No LIST_ONLY gating for block-consuming events.** The current
    `LIST_ONLY_EVENT_NAMES` set (`config.py:464`) excludes events
    that should never emit heats (axe, peavey, caber, pulp). It
    would not need to be consulted for the new card feature since
    none of the list-only events map to blocks, but a new feature
    that walks all events should include this as a guard to avoid
    emitting phantom cards for list-only events in the future.

---

End of recon. No changes were made to source code, configuration,
database, or any other file besides this document.
