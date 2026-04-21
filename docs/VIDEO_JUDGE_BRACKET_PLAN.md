# Plan: Video Judge Workbook + Birling Blank Bracket + Birling Nav + L/R Springboard Bug Fix

Branch: `reporting-export-service`
Target ship: April 24-25, 2026
Repo: Missoula-Pro-Am-Manager (Flask 3.1, SQLAlchemy 2.0, pandas 2.1, openpyxl 3.1, Windows/Railway PG prod, SQLite dev)

**Revision: v2 (post-Codex)**. Incorporates 15 findings from Codex outside-voice review (2026-04-20). See "Changes from v1" section at bottom.

## Context

Three show-prep deliverables for the April 24-25 Missoula Pro-Am event:

1. **Video Judge Excel Workbook** — one xlsx, tab per event, long-format rows (one per
   competitor per run; one per partnered pair per run), two video-judge-time columns per
   row. Generated during show-prep after events are configured. Sync and async routes.

2. **Birling Blank Bracket Print** — printable (PDF/HTML) double-elimination bracket
   with round-1 matchups populated and all advancement slots blank. Filled by hand by
   the birling judge, returned for data entry.

3. **Birling Nav Surfacing** — multiple discoverable entry points to the bracket
   management page so judges in training can find it.

During the recon for these three, we found six pre-existing production bugs in the
pro-competitor xlsx importer and springboard heat/flight logic. Must be fixed as one
atomic PR BEFORE the three deliverables ship.

## Existing infrastructure (reuse, do not rebuild)

- `services/reporting_export.py` — **canonical export service** (extracted this branch, commit `cbe8802`):
  - `build_results_export(tournament) -> dict` (path + download_name + kind)
  - `build_chopping_export(tournament) -> dict`
  - `safe_download_name(tournament, suffix) -> str` (handles space→underscore)
  - `_reserve_export_path(tid, suffix, label) -> str` (tempfile.mkstemp wrapper)
  - `submit_results_export_job(tid) -> job_id`
  - `resolve_completed_export_path(tid, job_id, job_getter) -> dict | None`
  - **VJ workbook must follow this pattern, not inline `mkstemp` in routes.**
- `services/excel_io.py:1054` — `export_results_to_excel()` — reference for pandas+openpyxl xlsx emission
- `services/handicap_export.py:62` — `export_chopping_results_to_excel()` — second reference
- `routes/reporting.py` (current state) — delegates all xlsx flows to `services.reporting_export`; do NOT import `_safe_filename_part` from `routes/scoring.py` (private helper)
- `routes/scoring.py:1527-1537` — WeasyPrint-optional PDF response (`try: from weasyprint...except ImportError: return html`); extract into a shared helper (`services/print_response.py`) if reused by birling print
- `services/judge_sheet.py:64` — `get_event_heats_for_judging()` — similar data shape but filters run 2 and lacks partner resolution
- `services/birling_bracket.py:48` — `BirlingBracket.generate_bracket()` — populates round-1 in `Event.payouts` JSON, leaves later slots `None`
- `templates/scheduling/heat_sheets_print.html:443-570` — `hs-bracket-*` CSS + `bracket_match` macro already render `TBD` for `None` slots
- `templates/scoring/judge_sheet.html` — standalone inline-CSS template with `@page` declaration (pattern for new print templates)
- `routes/scheduling/__init__.py:130` + `routes/scheduling/heat_sheets.py:25-49, 147, 218` — partner resolution via `_resolve_partner_name` + `_lookup_partner_cid` + `consumed` set (DRY bleeding — extract before 4th copy)

## Decisions

| ID | Topic | Decision |
|---|---|---|
| Q1 | Partner pairing | Extract `services/partner_resolver.py` first with 3 regression tests before building VJ workbook |
| Q2 | Blank bracket print UX if bracket not yet generated | Flash + redirect to seeding page |
| Q3 | VJ workbook routes | Ship both sync and async (mirror `export_results` pattern) |
| Q4 | Multi-run row layout | Split dual-run day-split events (Chokerman, Speed Climb) into one tab per run. Triple-run events (Axe Throw) stay on one tab with 3 rows per competitor. |
| Q5 | Event scope in VJ workbook | Include hits/score events with `Score 1 / Score 2` columns (flip labels by scoring_type). Skip only: bracket events (Birling) and LIST_ONLY events (no heats). |
| Q6 | Bracket PDF aggregation | Per-event "Print Blank Bracket" button on each `birling_manage` page + tournament-wide "Print All Birling Brackets" button |
| Q7 | Birling nav surfacing | All three surfaces: sidebar entry under Run Show + "Bracket" action button on events.html bracket rows + tournament_detail phase-link card |
| Q8a | VJ workbook filename | `{tournament.name}_{year}_video_judge_sheets.xlsx` |
| Q8b | Partnered event row format | Single cell `"Smith / Jones"` (matches heat_sheets pattern) |
| Q8c | Left-handed flag in VJ rows | Skip — handedness doesn't affect video timing |
| Priority | Bugs vs features | Fix all 6 L/R bugs atomically first. Feature work blocked until bug PR merges. |

## Bugs to fix in PR A (atomic, must ship first)

| # | File:Line | Severity | Problem |
|---|---|---|---|
| 1 | `services/pro_entry_importer.py:165-171` | P2 | `events_entered` can contain duplicates if both `Springboard (L)` and `Springboard (R)` boxes are Yes. Loop appends per matched form header with no canonical dedup. |
| 2 | `services/pro_entry_importer.py:168-171` | P2 | `chopping_fees += 10` fires per matched form header → $20 charged for one entry if both L+R boxes Yes. Written to `competitor.total_fees`. |
| 3 | `services/pro_entry_importer.py` + `routes/import_routes.py:274` | **P1 active** | `is_left_handed_springboard` is NEVER set by the xlsx importer. Only manual-form flows set it. Every xlsx-imported competitor defaults to `False`. Template `templates/pro/import_upload.html:67` literally advertises "left-handed and slow-heat designation (when present in sheet)" but only slow-heat is wired. |
| 4 | `services/pro_entry_importer.py:20-47` | P2 | Same dedup trap on Pro 1-Board (3 keys→1 event), Jack & Jill (3→1), Partnered Axe Throw (2→1), Stock Saw (2→1), Speed Climb / Pole Climb (2→1). Currently latent (usually only one header variant in a given form) but same code path as #1. |
| 5 | `services/heat_generator.py:566-615` | **P1 active** | `_generate_springboard_heats` GROUPS all left-handed cutters into heat 0 (docstring says `"Left-handed cutters need to be grouped into the same heat."`). User's actual stated rule: SPREAD one LH cutter per heat. Only 1 LH-configured springboard dummy exists on site. Grouping all LH cutters into heat 0 means they'd all need the LH dummy simultaneously — impossible. Fixing #3 without #5 would actively break the show. |
| 5b | `services/flight_builder.py` | **P1 active** | Flight builder has no "max 1 LH-containing springboard heat per flight" constraint. User's rule: 3 LH cutters across 3 flights → each flight has exactly 1 LH-containing heat. Current flight builder cares about event variety + 4-heat spacing + Cookie Stack/Standing Block conflict. No LH awareness. |

## PR breakdown

### PR A — L/R springboard bug atomic fix (priority 1, blocks all else)

Modified:
- `services/pro_entry_importer.py` `parse_pro_entries`
  - **Preserve raw L/R state** in entry dict as `_raw_springboard_l: bool`, `_raw_springboard_r: bool` (underscore prefix = internal, stripped before DB write)
  - Add `seen_canonical_events: set` dedup so L+R-both-checked doesn't produce duplicate events/fees
  - Compute `is_left_handed_springboard` from `_raw_springboard_l` (True iff L is checked; R-only and neither-checked → False; both-checked → True with warning flag)
  - When both L and R are column-absent from the form (different form version), use sentinel `None` so re-import doesn't clobber manually-corrected flag
- `services/pro_entry_importer.py` `compute_review_flags`
  - Warn (Yellow): `entry['_raw_springboard_l'] and entry['_raw_springboard_r']` → `'CONFLICT: BOTH L AND R SPRINGBOARD CHECKED'` (flag reads raw state, not deduped events)
- `routes/import_routes.py` `confirm_pro_entries` around line 274
  - `lh = entry.get('is_left_handed_springboard')`
  - `if lh is not None: competitor.is_left_handed_springboard = bool(lh)` (None sentinel → preserve existing value; protects against re-import wiping manual corrections)
- `services/heat_generator.py` `_generate_springboard_heats`
  - Replace `_place_group(left_handed, left_heat_idx)` cluster logic with SPREAD: `for i, lh in enumerate(left_handed[:num_heats]): heats[i].append(lh)` (one LH cutter per heat 0..N-1)
  - **Overflow rule (user-specified):** if `len(left_handed) > num_heats`, extra LH cutters go into the FINAL heat (`heats[num_heats - 1]`), mixing with RH cutters there. Return a warning tuple the caller can flash: `f'Warning: {overflow_count} LH cutter(s) overflow into heat {num_heats}. Expect LH dummy contention.'`
  - Preserve existing slow_heat placement; add test for LH + slow_heat collision at `left_heat_idx=0` vs `slow_heat_idx=num_heats-1` (which is also the overflow target — document the interaction)
- `services/flight_builder.py` **(architecture-correct approach)**
  - Flight builder is global-optimize-then-slice, not per-flight placement. Cannot use "skip this flight" loop.
  - **Chosen approach: scoring penalty in `_score_ordering`.** Add a negative score term: for each flight window of `heats_per_flight` heats in the flat ordering, if >1 heat contains an LH-tagged competitor, add penalty `LH_FLIGHT_PENALTY = 1000`. Large enough to always dominate unless spacing would be catastrophically broken.
  - **Load LH flags explicitly**: `build_pro_flights` currently loads `Heat.get_competitors()` as integer IDs only (line 120). Need to batch-load `ProCompetitor.is_left_handed_springboard` with one `.in_()` query keyed by the union of competitor IDs across all springboard-stand-type heats. Cache in `all_heats[i]['contains_lh'] = bool`. No N+1.
  - **Post-slice sanity check**: after slicing, walk each flight and log a warning if any flight has >1 LH-containing heat (penalty wasn't strong enough or all permutations violate). Fail open (proceed with the schedule) but flash a warning to the admin.

New tests (~18):
- `tests/test_pro_entry_importer_handedness.py` (~8 tests)
  - `test_springboard_l_only_sets_true`
  - `test_springboard_r_only_keeps_false`
  - `test_both_checked_sets_true_and_flags_conflict`
  - `test_neither_checked_defaults_false`
  - `test_columns_absent_sets_none_sentinel`
  - `test_reimport_preserves_manual_handedness_when_sentinel_none`
  - `test_pro_1board_three_headers_dedup_to_one_entry_one_fee`
  - `test_jack_jill_three_headers_dedup_to_one_entry_one_fee`
- `tests/test_heat_generator_lh_spread.py` (~6 tests)
  - `test_one_lh_cutter_placed_in_heat_0`
  - `test_two_lh_cutters_placed_in_heat_0_and_heat_1_not_clustered`
  - `test_four_lh_cutters_four_heats_one_per_heat`
  - `test_overflow_lh_goes_to_final_heat_with_warning`
  - `test_lh_and_slow_heat_collision_final_heat_mixes_both`
  - `test_regression_slow_heat_placement_unchanged_when_no_lh`
  - `test_heat_assignment_rows_sync_after_lh_reassignment`
  - `test_gear_sharing_conflict_respected_among_prespread_lh`
- `tests/test_flight_builder_lh_constraint.py` (~4 tests)
  - `test_three_lh_heats_three_flights_scoring_penalty_spreads_them`
  - `test_two_lh_heats_three_flights_optimizer_puts_them_in_different_flights`
  - `test_more_lh_heats_than_flights_overflow_logs_warning`
  - `test_single_lh_heat_placed_respecting_spacing`
  - `test_build_pro_flights_loads_is_left_handed_springboard_in_single_query`

Repair script (new, ships with PR A):
- `scripts/repair_springboard_handedness.py` or route `POST /admin/repair/springboard-handedness/<tid>` (admin-only, audit-logged)
  - Scan `uploads/` directory for the last xlsx import for the target tournament (or take a path param)
  - Re-run `parse_pro_entries` with the fixed parser
  - For each competitor matched by email: update `is_left_handed_springboard` if currently False and xlsx said L
  - After update: identify all pro springboard events (`stand_type == 'springboard'`) and re-run `generate_event_heats` for each (if heats exist already, regenerate — users get a confirmation prompt)
  - Audit log each update
  - 2 tests: `test_repair_sets_flag_on_xlsx_l_row`, `test_repair_regenerates_pro_springboard_heats`

Estimated effort: ~2.5 hours CC (up from ~75 min — scope grew with repair script, flight_builder correctness, expanded tests).

### PR B — partner_resolver extraction (foundation for PR C)

New:
- `services/partner_resolver.py` with `pair_competitors_in_heat(event, comp_ids, competitor_lookup) -> list[PairRow]` returning `consumed`-filtered pairs
- `tests/test_partner_resolver.py` (~8 tests, 3 CRITICAL regressions marked below)
  - `test_non_partnered_event_one_row_per_cid`
  - `test_partnered_event_both_partners_present`
  - `test_partnered_event_partner_missing_from_pool`
  - `test_partnered_event_first_name_fuzzy_match`
  - `test_consumed_set_prevents_double_row`
  - `test_regression_heat_sheets_serialize_heat_detail_output_identical`  # CRITICAL
  - `test_regression_heat_sheets_route_body_output_identical`  # CRITICAL
  - `test_regression_first_name_fallback_behavior_unchanged`  # CRITICAL

Modified:
- `routes/scheduling/heat_sheets.py:147` `_serialize_heat_detail` uses partner_resolver
- `routes/scheduling/heat_sheets.py:218` main heat_sheets route body uses partner_resolver

Estimated effort: ~25 min CC.

### PR C — Video Judge workbook (depends on PR B)

New:
- `services/video_judge_export.py`
  - `build_video_judge_rows(tournament) -> dict[sheet_name, list[Row]]` walks heats, uses partner_resolver, includes run 2, emits row per pair per run
  - **Stable row order** (Codex C9): `event.id → heat.heat_number → heat.run_number → stand_number → competitor_name`
  - `write_workbook(rows_by_sheet, path)`:
    - Truncate sheet names to 31 chars (openpyxl limit)
    - Strip Excel-invalid chars `[]:*?/\\` from sheet names
    - **Dedup after truncation**: if two events produce the same truncated name, append ` (2)`, ` (3)`, etc.
    - Wrap workbook save in try/except; on any openpyxl error, raise a custom `VideoJudgeWorkbookError` the route flashes cleanly
  - Skips `event.scoring_type == 'bracket'` and `LIST_ONLY_EVENT_NAMES`
  - Dual-run day-split events emit two sheets: `"Speed Climb - Run 1"`, `"Speed Climb - Run 2"`
  - Hits/score events use `Score 1 / Score 2` column labels (flip based on scoring_type); timed events use `Timer 1 / Timer 2`
- **Extend `services/reporting_export.py`** (Codex C12):
  - `build_video_judge_export(tournament) -> dict` — returns `{path, download_name, format, kind}` following existing pattern
  - `build_video_judge_export_for_job(tid) -> str`
  - `submit_video_judge_export_job(tid) -> job_id`
  - Reuses `_reserve_export_path` and `safe_download_name('video_judge_sheets.xlsx')` helpers
- `tests/test_video_judge_export.py` (~12 tests, expanded per Codex C10 feedback)
  - `test_simple_timed_event_single_run`
  - `test_partnered_event_row_per_pair`
  - `test_dual_run_event_two_tabs`
  - `test_triple_run_event_stacked_rows`
  - `test_skips_bracket_scoring_type`
  - `test_skips_list_only_events`
  - `test_college_event_includes_team_code`
  - `test_pro_event_no_team_code`
  - `test_hits_event_uses_score_labels`
  - `test_sheet_name_truncated_to_31_chars`
  - `test_sheet_name_invalid_chars_stripped`
  - `test_sheet_name_duplicate_after_truncation_suffixed`
  - `test_stable_row_ordering`
  - `test_openpyxl_write_error_raises_custom_exception`
- `tests/test_routes_video_judge.py` (~5 tests)
  - `test_sync_get_returns_xlsx`
  - `test_sync_unauthorized_redirects`
  - `test_async_post_returns_job_id`
  - `test_async_status_shows_download_when_complete`
  - `test_missing_tournament_404`
  - `test_empty_tournament_graceful_empty_workbook_or_flash`

Modified:
- `routes/reporting.py`
  - `GET /reporting/<tid>/video-judge-workbook` (sync) — calls `build_video_judge_export`, uses `send_file` + `@after_this_request`
  - `POST /reporting/<tid>/video-judge-workbook/async` — calls `submit_video_judge_export_job`; redirect to status page
  - Status page reuses `resolve_completed_export_path`
- `templates/tournament_detail.html` (+ button in "Ready for Game Day" action bar + phase-link card in Before-the-Show)
- `templates/_sidebar.html` (+ "Video Judge Workbook" entry as `sidebar-child` under Run Show, near Heat Sheets)
- `strings.py` (+ `NAV['video_judge_workbook'] = 'Video Judge Workbook'`)

Filename pattern: `{tournament.name}_{year}_video_judge_sheets.xlsx` via `safe_download_name(tournament, 'video_judge_sheets.xlsx')` from `services/reporting_export.py`. Do NOT import `_safe_filename_part` from `routes/scoring.py` (Codex C13 — private helper).

Estimated effort: ~75 min CC (up from 45 — service extension + 17 tests).

### PR D — Birling blank bracket + nav surfacing (serial with PR C — template conflicts)

New:
- `services/birling_print.py`
  - `build_birling_print_context(event) -> dict | None` — returns `None` if bracket not generated; caller handles redirect
  - **Blank-bracket scrub (Codex C14):** explicitly strip result fields from match copies before render:
    - Drop `winner`, `loser`, `falls`, `placements` from every match
    - For losers bracket rounds ≥ 2: set all `competitor1`/`competitor2` back to `None` (only round-1 matchups render)
    - For winners bracket rounds ≥ 2: same — all `None`
    - For `finals` + `true_finals`: blank everything
    - Function returns a DEEP COPY of `bracket_data`, never mutates the live `event.payouts`
- `services/print_response.py` (new, extracted per Codex C13)
  - `weasyprint_or_html(html: str, filename: str) -> tuple` — shared WeasyPrint-optional response helper
  - Used by judge_sheet, heat_sheet PDF, birling print
  - Replaces private `_judge_sheet_response` in `routes/scoring.py` with a public import
- `templates/scoring/birling_bracket_print.html` — standalone (no base.html extend), inline CSS, `@page { size: Letter landscape; ... }`, `@bottom-center` page counter. Copies `hs-bracket-*` CSS and `bracket_slot`/`bracket_match` macros from `heat_sheets_print.html:453-516`.
- `tests/test_birling_print.py` (~7 tests, expanded per Codex C14/C15)
  - `test_generated_bracket_round_1_populated`
  - `test_ungenerated_bracket_returns_none`
  - `test_bracket_size_8_with_byes`
  - `test_bracket_size_16_full_layout`
  - `test_mens_and_womens_separate_events_both_render`
  - `test_partially_played_bracket_strips_winners_and_placements_from_print_context`
  - `test_context_does_not_mutate_live_event_payouts`
- `tests/test_routes_birling_print.py` (~5 tests)
  - `test_print_before_generate_flashes_and_redirects`
  - `test_print_after_generate_returns_200_html_or_pdf`
  - `test_non_bracket_event_404`
  - `test_wrong_tournament_404`
  - `test_print_all_mixed_generation_skips_ungenerated_with_warning`

Modified:
- `routes/scheduling/birling.py`
  - `GET /scheduling/<tid>/event/<eid>/birling/print-blank` (per-event)
  - `GET /scheduling/<tid>/birling/print-all` (combined)
  - **Print-all mixed state (Codex C15):** iterate all bracket events; skip ungenerated (log and flash "Skipped N birling event(s) that have not been seeded yet: [names]"); render the rest in one combined document
  - Uses `services/print_response.weasyprint_or_html` helper
- `templates/scheduling/birling_manage.html` (+ "Print Blank Bracket" button next to "Finalize Results" / "Reset Bracket" in Actions row)
- `templates/scheduling/events.html:627-636` (+ "Bracket" action button on `event.scoring_type == 'bracket'` rows, linking to `scheduling.birling_manage`; keeps existing "Always Last" badge or consolidates into button label)
- `templates/tournament_detail.html` (+ Birling Brackets phase-link card in Before-the-Show panel, between "Preflight Check" and "Print Heat Sheets")
- `templates/_sidebar.html` (+ "Birling Brackets" entry as `sidebar-child` under Run Show, near Heat Sheets)
- `strings.py` (+ `NAV['birling_bracket'] = 'Birling Brackets'`)

Estimated effort: ~60 min CC (up from 35 — shared print helper extraction + scrub logic + mixed-state print-all + 2 extra tests).

## Sequencing (revised per Codex C16)

PR A ships alone first (bug fix is atomic, must merge before any feature work).
After PR A merges:
- PR B runs alone (foundation refactor with regression tests).
- **PR C runs after PR B** (PR C depends on `services/partner_resolver.py`).
- **PR D runs after PR C** — NOT in parallel. Both PR C and PR D modify `templates/tournament_detail.html`, `templates/_sidebar.html`, and `strings.py`. Merging in parallel would produce three-way conflicts on each file. Agree NAV keys up front (`video_judge_workbook`, `birling_bracket`) so both PRs import the same constant, minimizing actual merge churn.
- **Optional parallel**: PR D's `services/birling_print.py` + `services/print_response.py` + standalone `birling_bracket_print.html` template are independent of PR C. If a worktree-splitter is confident about merging the shared template/sidebar/strings diffs manually, these three new files could start in parallel and merge the template bits serially. Cost-benefit marginal at this scale; default to strict serial.

Total: 4 PRs, **~5.5 hours CC** (up from 3), **~50 new tests** (up from 40), expected merge window: **2-3 days** (up from 1-2).

## Risk and failure modes

| Codepath | Failure mode | Mitigation |
|---|---|---|
| `build_video_judge_rows` | Partner name has no match in pool | `partner_resolver` returns raw name fallback (no crash, row mildly wrong) |
| `write_workbook` | Sheet name > 31 chars | Truncate to 31 chars in writer; test covers it |
| Birling print route | `event.payouts` corrupt JSON | `_load_bracket_data` has bare except returning default shape; print route checks `has_bracket` and redirects if missing |
| VJ workbook route | Openpyxl raises during write | Try/except wraps write; flash error + redirect to tournament_detail |
| Heat generator LH spread | Only 1 LH cutter but 0 heats (edge) | Fallback to old single-heat placement |
| Flight builder LH constraint | More LH heats than flights | Overflow: once every flight has 1 LH heat, additional LH heats use spacing-only placement. Log a warning. |
| PR B partner_resolver extraction | Regression in heat_sheets output | 3 CRITICAL regression tests gate the PR |

## NOT in scope (deferred)

- Dedicated Reports/Exports hub page — scattered exports remain scattered
- Fix for same dedup trap on Jack & Jill / Partnered Axe / Stock Saw / Speed Climb non-L/R collisions beyond PR A's Springboard-specific fix — PR A's `seen_canonical_events` set fixes all of them but only LH handedness is behavior-tested; other collisions are latent
- A3/multi-page bracket layout for N > 8 birling competitors — Letter landscape assumed sufficient
- Left-handed column in VJ workbook rows (Q8c decided: skip)
- Async variant of birling bracket print — output is small HTML, no reason to async
- Partner pair support in paper judge_sheet.html — pre-existing gap, not created by VJ scope
- Pro birling references — already confirmed absent from config

## Open risks to watch

1. **Flight builder LH scoring penalty interaction.** `LH_FLIGHT_PENALTY = 1000` must dominate the existing spacing penalties but not trash variety. Risk: if the optimizer is at a local minimum that needs 2 LH in one flight to unlock spacing elsewhere, the penalty breaks it. Mitigation: test with realistic roster data (full 2025 if available); tune penalty if needed; the post-slice sanity check catches any permutation that ends up with 2 LH in one flight anyway.
2. **Heat generator LH spread + slow_heat + overflow all land on final heat.** User rule: overflow LH goes to heat `num_heats-1`. Existing rule: slow_heat also goes to `heats[num_heats-1]`. If both overflow AND slow-heat cutters exist, the final heat gets stuffed. Add test. Document behavior: "overflow + slow_heat stack in final heat; admin should reduce one of the two pools."
3. **Repair script side effects on partially-played events.** If some heats have already been scored when admin runs the repair, regenerating springboard heats destroys the results. Repair script must check `event.is_finalized` and `heat.status in ('in_progress', 'completed')` — skip regeneration if any event is live, only update the flag and log a warning that admin must manually rebuild the affected heats.
4. **Re-import idempotency.** Importing the same xlsx twice should be a no-op on `is_left_handed_springboard`. With the `None`-sentinel approach: first import writes True/False; re-import sees the column is still there and writes the same True/False. OK. Edge case: admin manually flips a competitor's LH flag, then re-imports a newer xlsx that has a DIFFERENT L/R answer. Plan: re-import wins. Document this. Add test.
5. **PR C needs the partner_resolver from PR B.** If a developer starts PR C in a worktree before PR B merges, they'll need to stub the service. Prefer strict serial unless worktree-splitter is CI-confident.

---

## Changes from v1 (Codex outside-voice review, 2026-04-20)

Codex challenged 15 points. User-decided tensions are ABOVE; objective corrections applied to the plan are listed here for audit.

**Architecture corrections (applied):**
- C4: Flight builder is global-optimize-then-slice. Replaced "track `flights_with_lh_springboard_heat: set[int]`" loop with scoring penalty in `_score_ordering` + post-slice sanity check.
- C5: Flight builder was not loading `is_left_handed_springboard`. Added explicit batched `.in_()` query path in PR A.
- C12: `routes/reporting.py` already delegates to `services/reporting_export.py` (commit `cbe8802` on this branch). Updated PR C to extend the service module instead of adding inline `mkstemp` to the route.
- C13: Replaced reference to private `_safe_filename_part` from `routes/scoring.py` with public `safe_download_name` from `services/reporting_export.py`. Added `services/print_response.py` extraction in PR D so judge_sheet + heat_sheet + birling_print share one WeasyPrint-optional helper.

**Test corrections (applied):**
- C7: Expanded test list for `_generate_springboard_heats` to include LH+slow_heat collision, overflow-to-final-heat, gear-sharing-conflict-among-prespread-LH, HeatAssignment sync after reassignment. Total PR A tests: 18 (up from 13).
- C10: Expanded VJ workbook tests to cover sheet-name dedup-after-truncation, invalid-char stripping, custom exception on openpyxl write failure. Total PR C tests: 17 (up from 14).

**Underspecification fixes (applied):**
- C2: Preserve raw L/R checkbox state in entry dict as `_raw_springboard_l`/`_raw_springboard_r` BEFORE canonical dedup so `compute_review_flags` can surface the both-checked conflict.
- C3: Use `None`-sentinel in entry dict when L/R columns are column-absent (vs explicitly False). Confirmer only overwrites `competitor.is_left_handed_springboard` when entry value is not None. Protects manually-corrected flags from being clobbered on re-import.
- C6: Changed LH overflow rule per user decision: spread 1 per heat 0..N-1, overflow goes to FINAL heat with warning flash. (Codex suggested hard fail; user overrode with pragmatic rule.)
- C9: Defined stable VJ row order: `event.id → heat.heat_number → heat.run_number → stand_number → competitor_name`.
- C10: Defined sheet name rules: truncate to 31 chars, strip `[]:*?/\\`, suffix ` (2)` / ` (3)` for dedup collisions.
- C14: Birling blank print context does a deep copy and explicitly strips `winner`/`loser`/`falls`/`placements` from all matches; rounds 2+ get `None` competitors. Bracket data on live event is never mutated.
- C15: "Print all birling brackets" skips ungenerated events with a flash listing which were skipped.

**Scope and sequencing (applied):**
- C1: Added one-time repair script (`scripts/repair_springboard_handedness.py` or admin route) to PR A. Updates `is_left_handed_springboard` for existing imported competitors and regenerates affected pro springboard heats (skipping any that are live/finalized). User-decided.
- C8: PR B extraction kept on the ship path (user override — chose DRY over Codex's defer recommendation). 3 CRITICAL regression tests gate it.
- C11: VJ workbook includes hits/score events with Score 1 / Score 2 labels (user reaffirmed original decision).
- C16: PR C and PR D serialized (not parallel) due to shared `tournament_detail.html`, `_sidebar.html`, `strings.py`.
- C17: Effort estimate revised upward: ~5.5 hours CC (from 3), ~50 tests (from 40), 2-3 day ship window (from 1-2).

**Unchallenged (kept from v1):**
- Q1-Q8 decisions (partner resolver extraction, flash+redirect blank bracket UX, sync+async VJ routes, split day-split runs, include hits/score events, per-event + combined bracket PDF, all-three nav surfacing, filename pattern, partner cell format, skip LH column in VJ rows).
- Priority: PR A (bug fix) ships first before any feature work.
