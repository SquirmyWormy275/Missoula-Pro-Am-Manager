# SCORING_AUDIT.md — College Division Scoring Correctness Audit

**Date:** 2026-04-08
**Auditor:** Claude (read-only correctness audit)
**Scope:** College division scoring pipeline — entry, placement, points, individual & team standings, Bull/Belle of the Woods.
**Methodology:** Read-only static analysis against the GROUND TRUTH spec provided by the auditor. No code modified.

> **Audit posture:** This audit assumes the GROUND TRUTH spec (two-attempt average, fractional split-tie points, knowledge events, Bull/Belle first-place tiebreak chain) is the correct rulebook. The current code implements a *materially different* model in several load-bearing places. Where the deviation is large enough that "the spec might be the thing that's wrong" is a reasonable defense, that ambiguity is called out — but the auditor was explicit that the spec is the ground truth, so all deviations are reported as findings.

---

## 1. Executive Summary

1. **CRITICAL — There is no two-attempt averaging anywhere in the codebase.** The spec says "scoring time = AVERAGE(Time1, Time2). NOT best-of." The code implements **single-attempt** for almost every event and **best-of-two** for Speed Climb / Chokerman's Race / Caber Toss via `EventResult.calculate_best_run()` (`models/event.py:195-210`). No model field, route handler, template, or test contains the concept of an averaged-attempt score for college events. If the spec is correct, **every college event ranking is wrong on race day.**

2. **CRITICAL — Fractional placement points are silently truncated by the database schema.** The spec mandates split ties (e.g., two tied for 5th → each gets `(2+1)/2 = 1.5`). All three points columns are declared `Integer`:
   - `EventResult.points_awarded` — `models/event.py:176` — `db.Integer`
   - `CollegeCompetitor.individual_points` — `models/competitor.py:31` — `db.Integer`
   - `Team.total_points` — `models/team.py:21` — `db.Integer`
   - Migration `migrations/versions/3a1d40ce3c6a_initial_schema.py:91, 103, 124` confirms `sa.Integer()` in DDL.
   Even if the engine were patched to compute `1.5`, SQLite/Postgres would silently coerce to `1` on insert. **Half-point ties cannot be represented.**

3. **CRITICAL — Tie handling does not split — it duplicates.** `services/scoring_engine.py:192-206` assigns the same position to all tied rows and gives **each** of them the **full** points for that position. The tests bake this in: `tests/test_scoring_college_points.py:576-584` asserts both tied 1st-place finishers get 10 points each. The spec says they should get `(10+7)/2 = 8.5` each. This is the same correctness defect as #1/#2 from a different angle.

4. **CRITICAL — Partner events only credit ONE partner unless the operator manually double-enters the time.** `_save_heat_results_submission` (`routes/scoring.py:138-219`) processes one POST field per `competitor_id`. The heat template (`templates/scoring/enter_heat.html:271-396`) renders one input row per competitor. There is **no auto-mirror of a partner's time onto the partner's row**. If the judge enters Double Buck time only against John Pork, his teammate Tom Oly's `EventResult` row stays `result_value=None`/`status='pending'` and `calculate_positions()` (`services/scoring_engine.py:157`) filters him out. Tom Oly receives **zero** points for an event he placed in. The synthetic test fixtures (`tests/fixtures/synthetic_data.py:720-754` Double Buck M, Jack & Jill College) reinforce this bug: each pair has only **one** results row with the partner's name as a string. There is no test that exercises both partners receiving points.

5. **HIGH — Bull/Belle of the Woods has no spec-compliant tiebreak.** Spec: "more 1st-place finishes wins; then 2nd; then coin flip." Current implementation (`models/tournament.py:85-104`, `services/scoring_engine.py:388-405`) sorts only by `individual_points DESC` with name as the secondary sort. There is no query path that counts placement frequency per competitor. Two athletes tied on total points will be ranked alphabetically — the wrong rule entirely.

6. **HIGH — Knowledge events (Cruise, Traverse, Dendrology, Wood ID) are not implemented.** The spec includes them in the event taxonomy and requires them to feed into the same points pipeline. No `Event` row, no `scoring_type='knowledge'`, no template path, no test references them. They appear only in `README.md` and `CLAUDE.md` as documentation tokens.

7. **HIGH — Heat undo leaves phantom points on competitors and teams.** `routes/scoring.py:536-586` deletes `EventResult` rows but never decrements `individual_points` or recalculates `team.total_points`. If an undone heat had triggered auto-finalization (`routes/scoring.py:236-237`), competitors keep the points awarded from a now-deleted result row.

8. **HIGH — DNF/DQ may keep stale points after correction.** `calculate_positions()` strips previously-awarded points using a per-result query loop (`services/scoring_engine.py:139-155`). For team recalc it only iterates `touched_team_ids` derived from `completed` results (`services/scoring_engine.py:216`), so a team whose only contribution from an event was just-DQ'd is not recomputed in that pass. The strip side-effect is correct on the competitor row but the team aggregate can lag.

9. **MEDIUM — Outlier flagging mean/stdev are computed on `_metric()` output, which is negated for `highest_wins` events.** `services/scoring_engine.py:229-253` and `347-381`. The numeric comparisons (`abs(fv - mean) > 2*stdev`) still work mathematically because the negation is symmetric, but the `mean` displayed in `outlier_check()` (`services/scoring_engine.py:373-377`) is also negated and will look wrong to a judge reviewing a Hard-Hit or Caber Toss outlier modal.

10. **MEDIUM — Single-attempt entry has no validation that the spec's two-attempt rule was actually followed.** Because there is no Time-1 / Time-2 schema, the system cannot tell whether an operator entered the average, the best, or just one attempt. The DB will accept whatever number the operator types and rank on it.

---

## 2. Step 0 — Codebase Map

### 2.1 File inventory (Python files >500 lines flagged for size)

| File | Lines | Notes |
|---|---|---|
| `routes/registration.py` | 1394 | LARGE |
| `tests/test_strathmark_sync.py` | 1357 | LARGE |
| `services/gear_sharing.py` | 1349 | LARGE |
| `routes/portal.py` | 1274 | LARGE |
| `tests/test_models_full.py` | 1129 | LARGE |
| `routes/scoring.py` | 1033 | LARGE — primary scoring entry path |
| `services/flight_builder.py` | 1034 | LARGE |
| `tests/test_mark_assignment.py` | 989 | LARGE |
| `services/woodboss.py` | 941 | LARGE |
| `services/excel_io.py` | 932 | LARGE |
| `tests/test_heat_gen_integration.py` | 838 | LARGE |
| `tests/fixtures/synthetic_data.py` | 815 | LARGE |
| `services/strathmark_sync.py` | 796 | LARGE |
| `tests/test_portal_hardening.py` | 775 | LARGE |
| `tests/test_migration_integrity.py` | 779 | LARGE |
| `services/mark_assignment.py` | 770 | LARGE |
| `tests/test_point_calculator.py` | 758 | LARGE |
| `tests/test_flight_builder_integration.py` | 755 | LARGE |
| `strings.py` | 713 | LARGE |
| `services/heat_generator.py` | 687 | LARGE |
| `tests/test_scoring_college_points.py` | 658 | LARGE — primary college scoring tests |
| `tests/test_scoring_engine_integration.py` | 653 | LARGE |
| `tests/test_gear_sharing.py` | 653 | LARGE |
| `services/scoring_engine.py` | **656** | LARGE — single source of truth for ranking |
| `routes/main.py` | 634 | LARGE |
| `tests/test_woodboss.py` | 629 | LARGE |
| `routes/scheduling/events.py` | 629 | LARGE |
| `tests/test_spectator_portal.py` | 629 | LARGE |
| `routes/reporting.py` | 615 | LARGE |
| `tests/test_pro_entry_importer.py` | 556 | LARGE |
| `tests/test_model_json_safety.py` | 571 | LARGE |
| `tests/test_scoring.py` | 551 | LARGE |
| `tests/test_role_access_control.py` | 544 | LARGE |
| `tests/test_axe_throw_qualifiers.py` | 533 | LARGE |
| `tests/test_flight_builder_25_pros.py` | 531 | LARGE |
| `tests/test_schedule_builder.py` | 527 | LARGE |
| `tests/test_scoring_full_event.py` | 523 | LARGE |
| `tests/test_excel_io.py` | 516 | LARGE |
| `models/competitor.py` | 322 | OK |
| `models/event.py` | 220 | OK |
| `models/team.py` | 86 | OK |
| `models/tournament.py` | 105 | OK |
| `services/point_calculator.py` | 21 | DEPRECATED — re-exports `scoring_engine` |
| `routes/scheduling/__init__.py` | 269 | OK |
| `routes/scheduling/heats.py` | 357 | OK |
| `config.py` | 444 | OK |

### 2.2 Route handlers touching scoring

College-relevant scoring/results/standings endpoints:

- `routes/scoring.py:117` `_save_heat_results_submission` — heat-result POST
- `routes/scoring.py:332` `event_results` — per-event results page
- `routes/scoring.py:352` `live_standings` — JSON poll
- `routes/scoring.py:363` `finalize_preview`
- `routes/scoring.py:374` `finalize_event`
- `routes/scoring.py:420` `enter_heat_results`
- `routes/scoring.py:519` `release_heat_lock`
- `routes/scoring.py:536` `undo_heat_save`
- `routes/scoring.py:593` `record_throwoff`
- `routes/scoring.py:623` `import_results`
- `routes/scoring.py:885` `heat_sheet_pdf`
- `routes/scoring.py:934` `birling_bracket`
- `routes/api.py:99` `public_standings`
- `routes/api.py:165` `public_results`
- `routes/api.py:212` `standings_poll`
- `routes/api.py:279` `standings_stream` (SSE)
- `routes/main.py` — tournament dashboards (multiple touchpoints to `total_points`/`individual_points`)
- `routes/portal.py` — competitor self-service `my-results`, school dashboard, spectator
- `routes/reporting.py` — printable college standings reports

### 2.3 Model fields touched by scoring

| Model | Field | Type | File:line |
|---|---|---|---|
| `Event` | `scoring_type` | `String(20)` | `models/event.py:26` |
| `Event` | `scoring_order` | `String(20)` | `models/event.py:27` |
| `Event` | `requires_dual_runs` | `Boolean` | `models/event.py:42` |
| `Event` | `requires_triple_runs` | `Boolean` | `models/event.py:45` |
| `Event` | `is_handicap` | `Boolean` | `models/event.py:33` |
| `Event` | `is_finalized` | `Boolean` | `models/event.py:62` |
| `EventResult` | `result_value` | `Float` | `models/event.py:139` |
| `EventResult` | `run1_value` | `Float` | `models/event.py:143` |
| `EventResult` | `run2_value` | `Float` | `models/event.py:144` |
| `EventResult` | `run3_value` | `Float` | `models/event.py:148` |
| `EventResult` | `best_run` | `Float` | `models/event.py:145` |
| `EventResult` | `tiebreak_value` | `Float` | `models/event.py:152` |
| `EventResult` | `final_position` | `Integer` | `models/event.py:173` |
| `EventResult` | **`points_awarded`** | **`Integer`** | **`models/event.py:176`** |
| `EventResult` | `payout_amount` | `Float` | `models/event.py:179` |
| `EventResult` | `is_flagged` | `Boolean` | `models/event.py:182` |
| `EventResult` | `status` | `String(20)` | `models/event.py:185` |
| `EventResult` | `version_id` | `Integer` | `models/event.py:186` |
| `EventResult` | `throwoff_pending` | `Boolean` | `models/event.py:156` |
| `EventResult` | `handicap_factor` | `Float` | `models/event.py:162` |
| `EventResult` | `predicted_time` | `Float` | `models/event.py:170` |
| `CollegeCompetitor` | **`individual_points`** | **`Integer`** | **`models/competitor.py:31`** |
| `Team` | **`total_points`** | **`Integer`** | **`models/team.py:21`** |
| `Heat` | `version_id`, `locked_*` | various | `models/heat.py` |

There are no fields named `time_1`, `time_2`, `attempt_1`, `attempt_2`, `average`, `avg`, or anything that hints at a two-attempt-averaging rule.

### 2.4 Templates referencing scoring/standings/points

- `templates/scoring/enter_heat.html`
- `templates/scoring/event_results.html`
- `templates/scoring/heat_sheet_print.html`
- `templates/scoring/birling_bracket.html`
- `templates/scoring/configure_payouts.html`
- `templates/scoring/import_results.html`
- `templates/scoring/offline_ops.html`
- `templates/scoring/tournament_payouts.html`
- `templates/reports/college_standings.html`
- `templates/reports/college_standings_print.html`
- `templates/college/dashboard.html`
- `templates/college/team_detail.html`
- `templates/portal/spectator_college.html`
- `templates/portal/school_dashboard.html`
- `templates/portal/kiosk.html`
- `templates/proam_relay/dashboard.html`

### 2.5 Test files exercising scoring

- `tests/test_scoring.py` — unit tests on `scoring_engine` (mock objects)
- `tests/test_scoring_college_points.py` — full college pipeline (DB)
- `tests/test_scoring_engine_integration.py` — engine integration
- `tests/test_scoring_full_event.py` — end-to-end events
- `tests/test_scoring_integration.py` — finalize / re-finalize flow
- `tests/test_point_calculator.py` — legacy points (now re-exports)
- `tests/test_fuzz_scoring.py` — fuzz/property tests
- `tests/test_axe_throw_qualifiers.py` — axe throw flow
- `tests/test_partnered_events_realistic.py` — partner events (key for finding #4)
- `tests/test_birling_bracket.py`, `test_birling_bracket_12.py`
- `tests/test_handicap_export.py`
- `tests/test_mark_assignment.py`
- `tests/test_heat_gen_integration.py`
- `tests/test_models.py`, `test_models_full.py`
- `tests/fixtures/synthetic_data.py` — canonical fixtures (encodes the bug from finding #4)

---

## 3. Step 1 — Two-Attempt Time Entry

**1.1 Are two separate attempt times stored per competitor per event? Show schema fields.**

No. The schema has `result_value`, `run1_value`, `run2_value`, `run3_value`, and `best_run`, plus `tiebreak_value`. The `runX_value` columns are NOT modeling "two attempts of the same heat" — they model **two physically separate runs (heats)** for events explicitly flagged `requires_dual_runs=True`. Comments at `models/event.py:40-42` are explicit:

> requires_dual_runs: two separate heats (run 1 & run 2); **best run counts**.
> Used by: Speed Climb, Chokerman's Race, Caber Toss.

`services/scoring_engine.py:54-57` and `models/event.py:195-210` (`calculate_best_run`) confirm `min(run1, run2)` for `lowest_wins` and `max(...)` for `highest_wins`. **There is no averaging.**

**1.2 Is scoring time = AVERAGE(t1,t2)? Where?**

No. **Search verified:** `grep -i "average|mean|avg|/ 2|sum.*2" services/scoring_engine.py` returns only `statistics.mean(values)` for outlier flagging (`services/scoring_engine.py:245, 365`). That `mean` is the cohort mean for std-dev outlier detection, not a per-competitor scoring average.

**1.3 If only Time 1 submitted, Time 2 missing — fallback to T1 alone, NULL, or error?**

For a `requires_dual_runs=True` event, `calculate_best_run` (`models/event.py:202-208`) does `runs = [v for v in [run1, run2] if v is not None]` then takes min/max of whatever's present. So a single-run entry is silently ranked as if it were the best run. For `requires_dual_runs=False` events (the vast majority), there is no "Time 2" at all — `result_value` is the single number the operator typed.

**1.4 Knowledge events: separate input schema? If shared, how is non-time score stored?**

Knowledge events do not exist in code. `Cruise`, `Traverse`, `Dendro`, `Wood ID` are not present in `config.COLLEGE_OPEN_EVENTS` or `config.COLLEGE_CLOSED_EVENTS` (`config.py:276-304`). They appear only in `README.md` and `CLAUDE.md`. There is no scoring path for them.

---

## 4. Step 2 — Placement & Fractional Points

**2.1 Where is placement calculated?**

`services/scoring_engine.py:124-226` `calculate_positions(event)`. Single canonical implementation. Called from:
- `routes/scoring.py:237` (auto-finalize when all heats complete)
- `routes/scoring.py:381` (`finalize_event` POST)

Standings are derived from `final_position` and the cached `individual_points` / `total_points` fields.

**2.2 Sort done on computed average or raw fields?**

Sort is done on `_sort_key()` (`services/scoring_engine.py:95-105`), which returns `(primary, tiebreak)` where `primary = _metric()` (`:45-71`). `_metric` returns `result.best_run` for dual-run events, otherwise `result.result_value`. **No averaging is applied anywhere in the sort path.**

**2.3 Does the system detect tied averages? What happens?**

It detects tied `(primary, tiebreak)` keys (`services/scoring_engine.py:194-197`) and assigns the **same position** to both rows. The next non-tied row's position is `i + 1` (skips the consumed positions). This is "competition ranking" / "1-1-3" — standard for individual sports but **wrong** if the spec says split-points-among-tied.

**2.4 CRITICAL — Does points support non-integer values?**

**No.** Three independent locations all declare INTEGER:

| Column | Model declaration | Migration declaration |
|---|---|---|
| `event_results.points_awarded` | `models/event.py:176` `db.Integer` | `migrations/versions/3a1d40ce3c6a_initial_schema.py:124` `sa.Integer()` |
| `college_competitors.individual_points` | `models/competitor.py:31` `db.Integer` | `migrations/versions/3a1d40ce3c6a_initial_schema.py:103` `sa.Integer()` |
| `teams.total_points` | `models/team.py:21` `db.Integer` | `migrations/versions/3a1d40ce3c6a_initial_schema.py:91` `sa.Integer()` |

`config.PLACEMENT_POINTS` (`config.py:150-157`) is `{1:10, 2:7, 3:5, 4:3, 5:2, 6:1}` — integer literals. There is **no** division or `/ 2` anywhere in the points-award path.

If the engine were ever patched to compute `(10+7)/2 = 8.5`, the assignment `r.points_awarded = 8.5` would be silently coerced to `8` by the integer column, and `comp.individual_points += 8.5` would also be coerced. **The schema actively prevents the spec from being implemented correctly.**

**2.5 DNF/DQ: ranked after finishers? Excluded from points?**

Excluded entirely. `services/scoring_engine.py:157` filters `completed = [r for r in all_results if r.status == 'completed']` before sorting. DNF/DQ rows keep `final_position=None` and `points_awarded=0` (cleared at `:142-146`). They never appear in the placement loop. Test confirms this at `tests/test_scoring_college_points.py:311-331`.

**Caveat:** if an operator marks someone DNF/DQ *after* the event was already finalized (and they had been awarded points), the strip-and-recalculate path at `:138-155` correctly subtracts the previously awarded points from the competitor row, but **only** if `calculate_positions()` is re-called. The edit path in `_save_heat_results_submission` resets `event.is_finalized = False` and re-calls `calculate_positions()` in the same request only when **all heats are complete** (`routes/scoring.py:235-237`); for partial edits, the `individual_points` running total can lag.

---

## 5. Step 3 — Two-Person (Partner) Events

**3.1 Stored as one row per pair or two rows?**

The data model **supports** two rows per pair: each `EventResult` has a `competitor_id` and a `partner_name` string. `services/heat_generator.py:253-282` walks all competitors for the event and creates an `EventResult` for each one (loop at `:253` over `all_comps`, with pairing logic in `_build_partner_units` at `:343-372` only used for snake-draft *heat assignment*, not result row creation). So in production, **both** partners get a results row at heat-generation time with `status='pending'` and `result_value=None`.

**3.2 Do BOTH competitors receive placement points in their individual records?**

**Only if the operator manually enters the time twice — once per competitor row.** `_save_heat_results_submission` (`routes/scoring.py:138-219`) iterates `competitor_ids` and processes one POST field `result_{comp_id}` per row. There is **no auto-mirror** of one row's value onto its partner's row. The heat entry template (`templates/scoring/enter_heat.html:271-396`) renders one input row per competitor — it does not gang partners together.

If the operator only types one value (the natural workflow for a "one time per pair" event):
- The unentered partner's row stays `result_value=None`, `status` defaults to `'completed'` from the form (line 139 default) but the `if not raw: continue` at `:143-144` skips the row entirely so its status is never updated.
- Wait — re-reading: the loop `continues` before any field gets touched, including status. So the partner's row keeps `status='pending'` (the heat-generator default).
- `calculate_positions()` filters on `status == 'completed'` (`services/scoring_engine.py:157`) and the partner is excluded from ranking.
- The entered partner gets full points; the un-entered partner gets **zero**.

The synthetic test fixture (`tests/fixtures/synthetic_data.py:720-754`) only ever defines one row per pair, with the partner stored as a string in `entry[5]`. There is **no test** that asserts both partners receive points.

**3.3 Does team total accumulate the points twice?**

If both rows actually have `status='completed'` and identical times (which only happens via manual double-entry), then yes — both end up in the sort, both get the same tied position, both get the position's points, and `team.recalculate_points()` (`models/team.py:63-67`) sums them. So the code path is correct **conditional on the operator double-entering**. The dependency on operator discipline is the bug.

**3.4 J&J mixed pairs: M+F pairing supported?**

Partially. The `partner_gender_requirement` is set to `'mixed'` for J&J in `config.py:297, 317`. The partner-pairing in `_build_partner_units` (`services/heat_generator.py:343-372`) is gender-agnostic — it pairs by name reference. So scheduling works. The same scoring bug from 3.2 applies regardless of gender mix. There is no test fixture for the M+F case at `tests/fixtures/synthetic_data.py:743-754`; the J&J entries are name pairs without gender annotation.

---

## 6. Step 4 — Individual Standings

**4.1 Computed on read or stored running total?**

**Stored running total** in `CollegeCompetitor.individual_points` (`models/competitor.py:31`). Updated incrementally inside `calculate_positions()` (`services/scoring_engine.py:206`: `comp.individual_points += points`). Stripped on re-finalize (`:142-145`).

**4.2 If stored: what recalculates on correction?**

Re-running `calculate_positions(event)` strips the old points (per result, per competitor) then adds the new ones. The strip path issues N+1 queries (`CollegeCompetitor.query.get(...)` inside the loop at `:142-145`).

`services/scoring_engine.py:426-431` exposes `recalculate_all_team_points(tournament_id)` for team-level recompute, but there is **no equivalent `recalculate_all_individual_points()`** that would rebuild a competitor's `individual_points` from scratch by summing their `EventResult.points_awarded`. The only way to fix a corrupted `individual_points` cache is to re-finalize every event the competitor was in.

The undo path (`routes/scoring.py:536-586`) does **not** strip — see Step 8 / Finding #7.

**4.3 If computed: single canonical query, or multiple implementations?**

There are several read paths for individual standings:
- `services/scoring_engine.py:388` `get_individual_standings(tournament_id, gender, limit)`
- `models/tournament.py:85` `get_bull_of_woods(limit)` and `:96` `get_belle_of_woods(limit)`
- Direct `CollegeCompetitor.query` calls in templates and routes

All read from the cached `individual_points` column, so they agree on data. They differ in **secondary sort**: `scoring_engine.get_individual_standings` does NO secondary sort and assigns rank by index gap; `tournament.get_bull_of_woods` adds `CollegeCompetitor.name` as a secondary sort. None of them implement the spec's "more 1st-place finishes" tiebreak.

**4.4 Is the Bull/Belle display gender-separated?**

Yes. `get_bull_of_woods` filters `gender='M'` and `get_belle_of_woods` filters `gender='F'` (`models/tournament.py:90, 101`).

---

## 7. Step 5 — Team Standings

**5.1 Where computed?**

Stored running total in `Team.total_points` (`models/team.py:21`). Recalculated by `Team.recalculate_points()` (`models/team.py:63-67`) which sums `individual_points` from all `active` members. Called from:
- `services/scoring_engine.py:217-220` after each event finalize, only for `touched_team_ids` derived from completed results.
- `services/scoring_engine.py:337-341` from `record_throwoff_result`.
- `services/scoring_engine.py:426-431` `recalculate_all_team_points` (manual / batch).

Read paths: `models/tournament.py:75` `get_team_standings`, `routes/api.py:225-238` `standings_poll`, `routes/api.py:111-114` `public_standings`. All sort by `Team.total_points DESC, Team.team_code`.

**5.2 Team membership: string name, FK, other?**

FK. `CollegeCompetitor.team_id` is `db.ForeignKey('teams.id')`, NOT NULL (`models/competitor.py:24`). A competitor belongs to exactly one team. There is no many-to-many or string-name fallback.

**5.3 Group by team (not school)? An "A" and "B" team from same school must produce two rows.**

Yes. `Team` has `team_code` (e.g. `UM-A`, `UM-B`) with a unique constraint per `(tournament_id, team_code)` (`models/team.py:32-34`). `school_name` and `school_abbreviation` are descriptive only. Two teams from one school produce two distinct `Team` rows and two standings entries.

**5.4 Trace the J&J flow.**

Given the bug in Step 3.2, the team contribution **does not** double correctly in the natural workflow — only the partner whose row was actually scored adds to their team's total. If the operator double-enters, both partners' `individual_points` go up by the same amount, both teams get +N (or one team gets +2N if both partners are on the same team — the AWFC-correct outcome).

A team where both J&J partners are members will get +2N points from one event when scored correctly. A team that fields a J&J pair where the partner is from a different team will only get +N (because the other team picks up the other +N). The spec is silent on cross-team J&J pairs, so this looks correct in principle — but the operator-discipline dependency makes the actual race-day outcome unpredictable.

---

## 8. Step 6 — Bull / Belle of the Woods

**6.1 Implemented?**

Yes. `models/tournament.py:85-104`.

**6.2 Gender-separated?**

Yes — Bull = M, Belle = F. See 4.4.

**6.3 Derived from totals or maintained as a separate counter?**

Derived from `CollegeCompetitor.individual_points` running total. No separate counter.

**6.4 Tiebreak beyond points? Does the schema let you query "count of 1st-place finishes per competitor"?**

**No spec-compliant tiebreak.** Current behavior: `ORDER BY individual_points DESC, name ASC`. Two athletes tied on points are ranked alphabetically — wrong rule.

**Schema introspection:** It IS theoretically possible to derive 1st-place counts from the existing data — `EventResult.final_position` is set by `calculate_positions()` and `competitor_id` joins back to `CollegeCompetitor`. A query like:

```
SELECT competitor_id, COUNT(*) FROM event_results
WHERE final_position = 1 AND status='completed'
  AND competitor_id IN (...) GROUP BY competitor_id
```

would work. **No code calls this query.** Implementing the tiebreak would require a new function and either a denormalized counter or per-display-time aggregation.

---

## 9. Step 7 — Knowledge / Skill Events

**7.1 Dendro / Wood ID: how stored? How converted to placement?**

**Not implemented.** No `Event` rows, no `scoring_type='knowledge'` or similar. `Event.scoring_type` enum (per `models/event.py:26`) is `'time' | 'score' | 'distance' | 'hits' | 'bracket'`. No "knowledge" or "quiz" type exists.

**7.2 Traverse: closer-to-pin wins — ascending sort?**

Not implemented.

**7.3 Cruise: which direction wins?**

Not implemented.

**7.4 Same placement→points pipeline as timed events?**

N/A — the events do not exist.

If admins create knowledge events ad-hoc with `scoring_type='score'` and `scoring_order='lowest_wins'` (closest to true), the system would rank them. There is no integrity check that warns about misuse. There is also no "answer key" UI.

---

## 10. Step 8 — Concurrency & Correction

**8.1 Multi-statement scoring writes without a transaction?**

Mixed. The main scoring write path (`_save_heat_results_submission` at `routes/scoring.py:117-290`) does `db.session.commit()` once at line `:242` after building up all changes — that part is fine. It catches `StaleDataError` (optimistic-lock collision via `version_id`) and `IntegrityError` and rolls back.

Inside `calculate_positions()`, the strip loop and the award loop both mutate `comp.individual_points` directly without any savepoint protection. If the loop crashes partway through, the session is left with partially-stripped points. The caller catches this in `finalize_event` (`routes/scoring.py:380-409`) by wrapping in `db.session.begin_nested()` (savepoint), so the crash will roll back to pre-finalize. The auto-finalize call from inside heat save (`routes/scoring.py:236-237`) does **not** wrap in a savepoint — a mid-recalc crash would mix half-stripped points with the heat-save commit. This is a HIGH risk on race day if `calculate_positions` ever raises mid-loop.

**8.2 Time correction → re-calc sequence?**

Edit a single competitor's time:
1. `_save_heat_results_submission` saves new value, sets `event.is_finalized = False` (`routes/scoring.py:215-217`).
2. If `all_heats_complete` is true (heats keep `status='completed'`), `calculate_positions()` is called immediately (`:236-237`) and points/positions are re-derived.
3. If a heat status was downgraded (e.g. via undo) `all_heats_complete` is false and points are NOT re-derived — the cached `individual_points` is stale until the next finalize.

So edit-then-keep-finalized is OK. **Undo + re-edit** is the dangerous path: undo deletes EventResult rows but never decrements points (Finding #7).

**8.3 Routes that read standings AND write results in same request?**

`finalize_event` writes `final_position`/`points_awarded`/`individual_points`/`total_points` in one request. The auto-finalize path inside `_save_heat_results_submission` does the same. The polling reader (`live_standings`, `standings_poll`) is read-only and uses report cache (`services/report_cache.py`). The cache TTL is 5s (`PUBLIC_CACHE_TTL_SECONDS`), so a spectator could see a 5-second-stale leaderboard during write storms — acceptable for spectators, but the in-arena dashboard (`event_results.html`) hits `live_standings` directly and gets the post-commit DB state.

There is no read-then-write race within a single transaction that affects scoring correctness.

---

## 11. Step 9 — Test Coverage Gaps

### 11.1 Existing coverage by topic

| Topic | Coverage location |
|---|---|
| Averaging two attempts | **NONE** — concept does not exist in code or tests |
| Average → placement | **NONE** |
| Fractional ties (split points) | **NONE** — tests at `tests/test_scoring_college_points.py:541-658` actively assert the OPPOSITE (both tied get full points) |
| Partner dual-credit | **NONE** — `tests/test_scoring_college_points.py:265-286` only checks the lead partner gets points, not the named partner. `tests/test_partnered_events_realistic.py` exists but does not assert both partners' `individual_points` |
| Team aggregation | `tests/test_scoring_college_points.py:337-456` |
| Bull/Belle ranking | `tests/test_scoring_college_points.py:457-540` (correctness of order, not first-place tiebreak) |
| 1st-place-count tiebreak | **NONE** |
| Best-run dual events | `tests/test_scoring.py` (TestMetric class), `tests/test_scoring_college_points.py:287-309` |
| Throw-off resolution (axe) | `tests/test_axe_throw_qualifiers.py` |
| Hard-Hit tiebreak (time) | `tests/test_scoring.py` |
| DNF/DQ exclusion | `tests/test_scoring_college_points.py:311-331` |
| Outlier flagging | `tests/test_scoring.py` |
| Concurrent edit / version_id | `tests/test_scoring_integration.py` |
| Undo flow | `tests/test_routes_post.py` (smoke only — no points-recalc assertion) |

### 11.2 Coverage check vs Steps 1-8

- **Step 1 (averaging):** No coverage. Cannot exist until the schema/engine is changed.
- **Step 2 (placement & fractional points):** Tie behavior covered, but the **wrong** behavior is locked in by tests.
- **Step 3 (partner double-credit):** No assertion that the second partner gets points.
- **Step 4 (individual standings recalc on correction):** No test that takes a finalized event, edits one result, and asserts `individual_points` is correct.
- **Step 5 (team aggregation):** Covered for happy-path. No test for "team has competitor whose only event was just-DQ'd".
- **Step 6 (Bull/Belle):** Order tested, tiebreak NOT tested.
- **Step 7 (knowledge events):** N/A.
- **Step 8 (concurrency / correction):** `version_id` collision tested. Undo's points-leak NOT tested.

### 11.3 Top 5 untested race-day-risk scenarios

1. **Partner-event single entry → second partner missing points.** Race day: judge enters Double Buck time for John Pork, Tom Oly's standings line shows zero. Discovery happens during awards ceremony.
2. **Tied placement with fractional spec → silent integer truncation.** Race day: two athletes tie for 3rd. Spec says each gets `(5+3)/2=4`. Code gives each 5. Team standings affected by 2 points per tie.
3. **Heat undo after auto-finalize → phantom points stay on competitors and team.** Race day: scoring entry typo, judge undoes within 30 s, re-enters; the original points were never stripped → competitor and team are inflated.
4. **Bull/Belle tied on points → alphabetical "winner" announced.** Race day: two athletes tie for Bull, the alphabetically-earlier name takes the title without a coin flip or first-place-count check.
5. **Edit single result on finalized event without all_heats_complete → stale individual_points.** Race day: a heat was undone earlier in the day, then someone edits a different heat's result; `calculate_positions` is not re-called and `individual_points` no longer matches the sum of `points_awarded`.

---

## 12. Findings List

```
FINDING 1 | SEVERITY: CRITICAL | FILE: services/scoring_engine.py:45-71, models/event.py:195-210, models/event.py:139-146 | DESCRIPTION: No two-attempt averaging is implemented anywhere. Single-attempt events use result_value verbatim; dual-run events (Speed Climb/Chokerman/Caber) use min/max best-of, not average. Spec says scoring time = AVERAGE(Time1, Time2) for college events. | RISK IF UNFIXED: Every college event ranking is wrong on race day per spec. Standings, Bull/Belle, and team totals all derive from these rankings.

FINDING 2 | SEVERITY: CRITICAL | FILE: models/event.py:176, models/competitor.py:31, models/team.py:21, migrations/versions/3a1d40ce3c6a_initial_schema.py:91,103,124 | DESCRIPTION: All three points columns (points_awarded, individual_points, total_points) are declared as Integer in both ORM and DDL. The spec mandates fractional split-tie points (e.g. 1.5 for tied 5th). Even if the engine were patched to compute fractional values, the DB layer would silently coerce 8.5 → 8. | RISK IF UNFIXED: Fractional ties cannot be represented. Any future fix to the engine alone is impossible without a migration.

FINDING 3 | SEVERITY: CRITICAL | FILE: services/scoring_engine.py:192-206; tests/test_scoring_college_points.py:541-658 | DESCRIPTION: Tie handling assigns the same position to all tied rows and gives EACH of them the FULL points for that position (10/10 instead of 8.5/8.5 for tied 1st). The tests actively encode this behavior, making future correction harder. | RISK IF UNFIXED: Athletes tied for 1st each receive the rank-1 points table value; the next athlete is bumped to position 3. Team standings inflated by ties.

FINDING 4 | SEVERITY: CRITICAL | FILE: routes/scoring.py:138-219, templates/scoring/enter_heat.html:271-396, services/scoring_engine.py:157, tests/fixtures/synthetic_data.py:720-754 | DESCRIPTION: Partner events render one input row per competitor; the operator must manually type the same time twice to credit both partners. There is no auto-mirror. If the operator types it once, the second partner stays status=pending and is excluded from calculate_positions(). The synthetic fixtures encode this bug — only one row per pair is ever created in test data, and no test asserts both partners receive points. | RISK IF UNFIXED: Tom Oly (Double Buck), Beverly Crease (J&J), and every partner-event teammate gets zero points at the natural workflow. Discovered at the awards ceremony when the announcer notices missing names.

FINDING 5 | SEVERITY: HIGH | FILE: models/tournament.py:85-104, services/scoring_engine.py:388-405 | DESCRIPTION: Bull/Belle of the Woods is sorted by individual_points DESC with name as secondary sort. Spec requires tiebreak chain: more 1st-place finishes → more 2nd-place finishes → coin flip. No code path counts placement frequency. | RISK IF UNFIXED: Two athletes tied on total points are ranked alphabetically. The wrong athlete gets the Bull/Belle title.

FINDING 6 | SEVERITY: HIGH | FILE: config.py:276-304 | DESCRIPTION: Knowledge events (Cruise, Traverse, Dendrology, Wood ID) are absent from COLLEGE_OPEN_EVENTS / COLLEGE_CLOSED_EVENTS. No Event rows are seeded for them, no scoring_type='knowledge' exists. They are mentioned only in README.md and CLAUDE.md. | RISK IF UNFIXED: Knowledge competitions cannot be scored at all in the system. Tournament organizers fall back to paper.

FINDING 7 | SEVERITY: HIGH | FILE: routes/scoring.py:536-586 | DESCRIPTION: Heat undo deletes EventResult rows via .delete(synchronize_session='fetch') but never decrements CollegeCompetitor.individual_points or recalculates Team.total_points. If the undone heat had triggered auto-finalization (which strips/awards points), the awarded points stay on the competitor and team. Only re-finalizing the event (when all heats are again complete) will repair them. | RISK IF UNFIXED: Phantom points on competitors and teams. Standings inflated until the next full finalize cycle.

FINDING 8 | SEVERITY: HIGH | FILE: services/scoring_engine.py:215-220 | DESCRIPTION: After calculate_positions() awards points, team.recalculate_points() is called only for team_ids derived from the still-completed results list. A team whose only contribution was a freshly-DQ'd row will have its competitor's individual_points correctly stripped (line 142-145) but its team total_points not refreshed in this pass. The discrepancy lasts until another event finalizes for that team or recalculate_all_team_points() is called manually. | RISK IF UNFIXED: Team standings can be a few points high until the next event runs.

FINDING 9 | SEVERITY: HIGH | FILE: routes/scoring.py:235-237 | DESCRIPTION: The auto-finalize-on-last-heat path calls calculate_positions() inside the heat-save commit cycle WITHOUT a savepoint. The explicit finalize_event route (line 380) wraps in db.session.begin_nested(); this auto path does not. If calculate_positions raises mid-loop, the session may commit partially-stripped points. | RISK IF UNFIXED: Race-condition data corruption window during the busiest scoring period (final heats of a stand).

FINDING 10 | SEVERITY: MEDIUM | FILE: services/scoring_engine.py:229-381 | DESCRIPTION: flag_score_outliers and outlier_check use _metric() output for mean/stdev. _metric() negates values for highest_wins events (Caber Toss, Axe Throw) so the comparison is mathematically symmetric, but the 'mean' value reported in outlier_check's modal payload (line 376) is the negated mean. A judge reading the warning will see a wrong-signed mean. | RISK IF UNFIXED: Confusing modal messaging during outlier review on Caber Toss / Axe Throw events; could cause a judge to dismiss a real outlier or accept a fake one.

FINDING 11 | SEVERITY: MEDIUM | FILE: routes/scoring.py:215-217 | DESCRIPTION: Editing a single result on a finalized event sets is_finalized=False but only triggers calculate_positions() if all_heats_complete (line 236) is still true at the moment of save. If a heat had been undone earlier (status='pending'), the recalc never happens and individual_points is left stale. | RISK IF UNFIXED: Stale standings until next full event finalize.

FINDING 12 | SEVERITY: MEDIUM | FILE: services/scoring_engine.py:139-145 | DESCRIPTION: The strip-previous-awards loop in calculate_positions issues N+1 queries (one CollegeCompetitor.query.get per result) instead of a batched .in_(). Functionally correct but slow on large events. | RISK IF UNFIXED: Slow finalize on the largest events (Stock Saw, Underhand) — could push the user past the 30s undo window before the page returns.

FINDING 13 | SEVERITY: MEDIUM | FILE: models/event.py:195-210 (calculate_best_run); routes/scoring.py:165-170 | DESCRIPTION: For requires_dual_runs events, if only Run 1 is entered (Run 2 missing), the system silently uses Run 1 as the "best" run and ranks the competitor on it. There is no validation that both runs are present before finalize. | RISK IF UNFIXED: A competitor who DNF'd Run 2 but had a fast Run 1 ranks alongside competitors who completed both — incorrect under "best of two" semantics if the missing run should be a DNF.

FINDING 14 | SEVERITY: MEDIUM | FILE: services/scoring_engine.py:388-405; models/tournament.py:75-83 | DESCRIPTION: There is no recalculate_all_individual_points() function analogous to recalculate_all_team_points(). The only way to repair a corrupted CollegeCompetitor.individual_points cache is to re-finalize every event the competitor participated in. | RISK IF UNFIXED: When a bug like Finding #7 introduces phantom points, there is no admin tool to fix the cache without re-running every event.

FINDING 15 | SEVERITY: LOW | FILE: services/scoring_engine.py:485-574 (import_results_from_csv) | DESCRIPTION: CSV import does not enforce competitor uniqueness or validate that imported competitors are entered in the event. Treats DQ/DNS/DNF case-insensitively but accepts any other string as parse error. | RISK IF UNFIXED: Operator could import results for a competitor not entered in the event; they would be created with status='completed' and ranked. Edge-case admin foot-gun.

FINDING 16 | SEVERITY: LOW | FILE: tests/fixtures/synthetic_data.py | DESCRIPTION: The 'expected position' and 'expected points' columns in COLLEGE_SCORES fixtures are computed from the buggy current behavior, so any future fix to the scoring engine will require regenerating the entire fixture. | RISK IF UNFIXED: Refactoring friction; not a runtime correctness issue.

FINDING 17 | SEVERITY: LOW | FILE: routes/api.py:99-122, routes/api.py:165-205 | DESCRIPTION: Public API responses serialize points_awarded as an integer (because the column is Integer) — would lose precision the moment the schema becomes Float. Any client decoding the JSON expects int. | RISK IF UNFIXED: When fractional points are introduced, all public-facing JSON consumers must update parsers.

FINDING 18 | SEVERITY: LOW | FILE: services/scoring_engine.py:108-117 (_detect_axe_ties) | DESCRIPTION: _detect_axe_ties uses result.result_value or 0 as the group key — a None value collides with a literal 0 score. Two competitors with no result yet would be flagged as a tie. | RISK IF UNFIXED: Spurious throw-off pending flags during partial entry; cleared on next finalize but visible in interim live standings.
```

---

## 13. Prioritized Fix List (race-day risk order)

| # | Fix | Findings | Why this order |
|---|---|---|---|
| 1 | **Add a partner-result mirror** so that entering a time on one partner's row auto-populates the partner's row with the same value and `status='completed'`. Server-side enforcement, not client-side. | #4 | Highest-frequency, lowest-detection risk; happens silently on every partner event. |
| 2 | **Migrate `points_awarded`, `individual_points`, `total_points` to `Numeric(6, 2)` / `Float`** with backfill. Required precondition for any fractional-points work. | #2 | Schema gate — blocks every other points-correctness fix. |
| 3 | **Implement spec-compliant split-tie points** in `calculate_positions()` and `record_throwoff_result()`. Replace integer constants with `Decimal` or float arithmetic. Update tests. | #3, #2 | Direct correctness defect; requires #2 first. |
| 4 | **Decide and implement two-attempt averaging vs. best-of** — confirm with the rules committee FIRST, then either: (a) add `time_1`/`time_2` columns and average them, or (b) update the spec to match the current best-of behavior and document. | #1 | Largest possible impact, but the right answer is rules-clarification before code. Do not "fix" without rules confirmation. |
| 5 | **Strip points on heat undo.** Either invoke a points-strip helper before deleting EventResult rows, or call `calculate_positions()` after undo to re-derive. | #7, #8 | High-frequency operator workflow; phantom-points bug. |
| 6 | **Wrap auto-finalize in a savepoint** identical to the explicit finalize path. | #9 | Cheap fix; closes the race window. |
| 7 | **Add `recalculate_all_individual_points(tournament_id)`** that rebuilds `individual_points` from `EventResult.points_awarded` summation. Surface as an admin tool. | #14, #7 | Repair tool for #5/#7/#8/#11. |
| 8 | **Implement Bull/Belle tiebreak chain.** Add per-competitor placement-count helper; sort by `(individual_points desc, count_1sts desc, count_2nds desc, ...)`. Manual coin-flip flag for ultimate tie. | #5 | Awards-ceremony correctness. |
| 9 | **Add knowledge events.** New `scoring_type='knowledge'` (or reuse `'score'` with `scoring_order='highest_wins'` and answer-key UI). Add Cruise / Traverse / Dendro / Wood ID to `COLLEGE_CLOSED_EVENTS` (or new list). Provide entry templates. | #6 | Cannot be scored at all today; only matters if the host runs them. Confirm with organizer first. |
| 10 | **Validate dual-run completeness** — refuse to finalize a `requires_dual_runs` event where any non-scratched competitor has only one run recorded. | #13 | Edge case but trivial to add. |
| 11 | **Fix outlier modal sign** for `highest_wins` events. Convert reported mean back to natural sign. | #10 | Cosmetic but confusing. |
| 12 | **Batch the strip-previous-awards loop** with `.in_()`. | #12 | Performance, not correctness. |
| 13 | **Tighten `_detect_axe_ties` None handling.** | #18 | Cosmetic. |
| 14 | **Tighten CSV import validation.** | #15 | Hardening. |

---

## 14. Things The Auditor Could Not Determine

- **Whether the spec's "AVERAGE(Time1, Time2)" is the actual AWFC rule for the events the host runs**, or whether the current best-of-two implementation is the historically correct rule and the spec was written incorrectly. The codebase, the in-tree docs (`README.md`, `CLAUDE.md`, `DEVELOPMENT.md`, `DESIGN.md`, `JUDGE_TRAINING_CURRICULUM.md`), and the synthetic fixtures all assume single-attempt or best-of-two. **The auditor was instructed to treat the user-supplied spec as ground truth, so this is reported as Finding #1, but a one-question rules confirmation should precede any code change.**
- **Whether spec's split-tie points rule applies to ALL events or only knowledge/scored events.** Many lumberjack circuits use the integer-duplicate rule the code currently implements. Same caveat as above.
- **Whether `tests/test_partnered_events_realistic.py` actually verifies dual-credit** — a deeper read of that 443-line file is warranted. The auditor checked the synthetic fixtures and the canonical pipeline tests but did not exhaustively audit every partner-event-related test file.
- **Whether the school-captain portal or self-service competitor portal expose stale-points views** that bypass the canonical standings query — only the main spectator/api paths were traced.
- **Race conditions under PostgreSQL locking semantics specifically** — the audit is static. No SQLite-vs-PG-specific behavior was empirically verified. The CI postgres-smoke job (per CLAUDE.md notes) does NOT run scoring scenarios.
