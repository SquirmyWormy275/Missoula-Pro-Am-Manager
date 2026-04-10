# PLAN_REVIEW.md — 5-Phase Scoring System Fix

**Reviewer:** Session B (Claude `/plan-eng-review`)
**Date:** 2026-04-08
**Plan source:** User-provided 5-phase fix plan, derived from `SCORING_AUDIT.md`
**Source-of-truth checked:** [models/event.py](models/event.py), [models/competitor.py](models/competitor.py), [models/team.py](models/team.py), [services/scoring_engine.py](services/scoring_engine.py), [routes/scoring.py](routes/scoring.py), CLAUDE.md
**Posture:** READ-ONLY review. No source files modified by this turn.

> **TL;DR (CORRECTED 2026-04-08 after user feedback):** The plan correctly diagnoses every CRITICAL finding from `SCORING_AUDIT.md`. The original review had two findings (A1, A2) that misread the user's intent — those have been REWRITTEN below. The corrected understanding: **every timed event in the system, college and pro, gets two judge timer readings per run, averaged into the run's "scored time."** Dual-run vs single-run is orthogonal to the dual-timer rule and is determined by the canonical event list in `config.py` + `ProAM requirements`. This makes the schema change cleaner than the original plan suggested. The remaining blocker (A3 — PG migration safety) still stands. The other 14 items are unchanged.
>
> **CHANGE LOG:**
> - 2026-04-08 v1: Initial review, 18 findings, 3 critical blockers (A1/A2/A3).
> - 2026-04-08 v2: A1 and A2 rewritten after user feedback. New finding A0 added (canonical attempt counts). New finding A8 added (Obstacle Pole config bug discovered while reading the canonical docs).
> - 2026-04-08 v3 (LOCKED): User decisions recorded.
>   - **A1 → CONFIRMED.** Adopt the per-run four-column dual-timer schema described below. User instruction: "Take your time and do it RIGHT. This is critical. Make no mistakes."
>   - **A2 → CONFIRMED.** Schema applies to both pro and college timed events. Phase 3 college guard is narrow.
>   - **A3 → CONFIRMED.** Migration MUST be PG-safe (server_default, postgresql_using, working downgrade, integrity test pass) per CLAUDE.md Section 6. User explicitly authorized "take the time" to do it right.
>   - **A8 → DECIDED FOR 2026.** Both college AND pro Obstacle Pole are 1-run for the 2026 season. No code change required (config.py already reflects this). The `requires_dual_runs` schema is forward-compatible if this is reversed in future seasons.
>   - **Plan status: READY FOR PHASE 1 EXECUTION.** Awaiting user direction on whether session_C (this session) executes, or whether a fresh execution session is started after session_A finishes shipping the gear audit branch.

---

## Step 0 — Scope Challenge

### What already exists in this codebase

- **Schema for two-attempt averaging:** does not exist. There are no `time_1`/`time_2`/`average_time` fields. The closest analog is `run1_value`/`run2_value`/`best_run` which model **two separate heats** (Speed Climb run 1 / run 2), not two timer readings of one attempt. Plan must NOT collide with this.
- **Centralized scoring:** `services/scoring_engine.py` already exists ([scoring_engine.py:124-226](services/scoring_engine.py#L124-L226)). All ranking goes through `_metric()`, `_sort_key()`, `calculate_positions()`. Plan correctly targets this file.
- **Tie detection (broken):** [scoring_engine.py:192-206](services/scoring_engine.py#L192-L206) — gives full points to every tied row. Plan correctly identifies and replaces this.
- **Strip-then-reassign idempotency:** Already exists ([scoring_engine.py:138-155](services/scoring_engine.py#L138-L155)). Plan's Phase 3 must not duplicate this.
- **Throw-off resolution:** `record_throwoff_result()` ([scoring_engine.py:302-345](services/scoring_engine.py#L302-L345)) does its own +/− points math against `comp.individual_points`. **Plan never mentions it.** Phase 3's "rebuild from SUM" approach would need to be applied here too, or the throw-off path will diverge.
- **Heat undo:** [scoring.py:536-586](routes/scoring.py#L536-L586) — `EventResult.query.delete()` with no points strip. Plan correctly identifies this.
- **Auto-finalize on last heat:** [scoring.py:235-237](routes/scoring.py#L235-L237) — calls `engine.calculate_positions(event)` outside any savepoint. Plan correctly identifies this.
- **STRATHMARK residual recording:** `_record_prediction_residuals_for_pro_event()` reads `result_value` after pro finalization. Plan says "do not modify STRATHMARK paths" but does not enforce this in Phase 1/2.
- **Handicap subtraction:** `_metric()` ([scoring_engine.py:45-71](services/scoring_engine.py#L45-L71)) subtracts `handicap_factor` from `result.best_run` (dual-run) or `result.result_value` (single-run) when `is_handicap`. **Plan never mentions this.** If Phase 3 stops populating `result_value` for college timed events, handicap math is undefined.
- **`Team.recalculate_points()`:** Already exists per CLAUDE.md. Plan invents a new SUM-based rebuild without referencing it.

### Complexity check
- 5 phases, ~12 source files touched, 1 new migration, 1 new admin route, multiple test rewrites. Above the 8-file threshold but **justified** — this is a correctness fix to load-bearing code, not feature work. Sequential gating is correct.

### NOT in scope (per plan, confirmed)
- Knowledge events (Cruise, Traverse, Dendro, Wood ID) — explicitly out of scope. The audit's HIGH finding on these is deferred. Acceptable, but **flag prominently in release notes**: the system still cannot score knowledge events after this fix lands.
- Pro division paths — supposed to be untouched. **The plan must enforce this in code, not just promise it.** See A2 below.
- Adding/removing/renaming events.

---

## Section 1 — Architecture Review

### A0 — Canonical event taxonomy (read this first)

**Severity: REFERENCE** | sources: `ProAM requirements`, [config.py:307-355](config.py#L307-L355)

**Number of physical attempts (runs) per event** — derived from the canonical docs, not the original review's guesses:

| Event | Division | Runs | Scoring type | Source |
|---|---|---|---|---|
| Underhand Speed | college | **1** | time | config.py:323, no `requires_dual_runs` |
| Underhand Hard Hit | college | **1** | hits (tiebreak: time) | config.py:322 |
| Standing Block Speed | college | **1** | time | config.py:325 |
| Standing Block Hard Hit | college | **1** | hits (tiebreak: time) | config.py:324 |
| Single Buck | college | **1** | time | config.py:326 |
| Double Buck | college | **1** | time (partnered) | config.py:327 |
| Jack & Jill Sawing | college | **1** | time (partnered, mixed) | config.py:328 |
| Stock Saw | college | **1** | time | config.py:329 |
| **Speed Climb** | college | **2** | time (best counts) | config.py:330 `requires_dual_runs=True` |
| Obstacle Pole | college | **1 in code, 2 per req doc** | time | config.py:331 — **see A8 below** |
| **Chokerman's Race** | college | **2** | time (best counts) | config.py:332 `requires_dual_runs=True` |
| Birling | college | bracket | bracket | config.py:333 |
| 1-Board Springboard | college | **1** | time | config.py:334 |
| **Caber Toss** | college | **2** | distance (farthest counts) | config.py:314 `requires_dual_runs=True` |
| Peavey Log Roll | college | **1** | time | config.py:311 |
| Pulp Toss | college | **1** | time (partnered, mixed) | config.py:316 |
| Axe Throw | college | 3 throws | score (sum) | config.py:309 `requires_triple_runs=True` |
| Springboard / Pro 1-Board / 3-Board Jigger | pro | **1** | time | config.py:339-341, no dual-run |
| Underhand | pro | **1** | time | config.py:342 |
| Standing Block | pro | **1** | time | config.py:343 |
| Stock Saw | pro | **1** | time | config.py:344 |
| Hot Saw | pro | **1** | time | config.py:345 |
| Single Buck | pro | **1** | time | config.py:346 |
| Double Buck | pro | **1** | time (partnered) | config.py:347 |
| Jack & Jill Sawing | pro | **1** | time (partnered, mixed) | config.py:348 |
| Partnered Axe Throw | pro | 3 throws | score (sum) | config.py:350 `requires_triple_runs=True` |
| Obstacle Pole | pro | **1** | time | config.py:352 ("pro Obstacle Pole will only be allotted ONE run") |
| Pole Climb | pro | **1** | time | config.py:353 |
| Cookie Stack | pro | **1** | time | config.py:354 |

**Per-run timer rule (user's correction, confirmed):** EVERY timed event — both divisions, single-run AND dual-run — has TWO judge stopwatches on each physical run. The two readings are averaged → that's the run's recorded time. For 2-run events (Speed Climb, Chokerman, Caber, and Obstacle Pole if A8 is fixed), the average is computed PER RUN, then `min(run1_avg, run2_avg)` is the final result for time events; `max(...)` for distance.

**This means:** the schema change adds two raw timer columns per run, NOT a single global pair. The averages flow into the existing `run1_value`/`run2_value`/`result_value` fields and all downstream code keeps working unchanged.

---

### A1 — REWRITTEN: Schema design that works for every event type

**Severity: CRITICAL** | confidence 9/10 | sources above

**The original plan's `time_1` / `time_2` columns are too narrow.** They model only single-run events. The correct schema models the canonical event taxonomy in A0:

**Add four new columns to `EventResult`:**

```python
# Two judge stopwatch readings for run 1 (or the only run, for single-run events).
# Average of the two becomes run1_value (or result_value for single-run events).
# Both NULL until the heat is scored. Both required to mark the result 'completed'.
t1_run1 = db.Column(db.Numeric(8, 2), nullable=True)
t2_run1 = db.Column(db.Numeric(8, 2), nullable=True)

# Two judge stopwatch readings for run 2 (only used by requires_dual_runs events:
# Speed Climb, Chokerman's Race, Caber Toss, and Obstacle Pole if A8 is fixed).
# Both NULL for single-run events.
t1_run2 = db.Column(db.Numeric(8, 2), nullable=True)
t2_run2 = db.Column(db.Numeric(8, 2), nullable=True)
```

**Computed semantics (no new hybrid property needed — preserve the existing fields):**

- For ANY timed event: when both `t1_runN` and `t2_runN` are populated, set `runN_value = (t1_runN + t2_runN) / 2`. Same averaging for distance (Caber Toss).
- For single-run events: `result_value = run1_value` (already true today; keep as-is). The "scored time" is the average of t1_run1 and t2_run1.
- For dual-run events: `best_run = min(run1_value, run2_value)` for `lowest_wins` events, `max(...)` for `highest_wins`. **`calculate_best_run()` already does this** at [models/event.py:195-210](models/event.py#L195-L210). It just needs to be called after the averaging step.
- The Hard-Hit `tiebreak_value` (currently a single number) ALSO needs the dual-timer treatment. Add `t1_tiebreak`, `t2_tiebreak` columns. The averaged value populates `tiebreak_value`. **Or:** keep the model simpler and only apply dual-timer to the primary metric for now, leaving Hard-Hit tiebreak as a single judge entry. **Recommend the second option** — the tiebreak time is already a low-stakes secondary metric; the complexity isn't worth it.

**Why this design is the right one:**

1. **Pro events are included** (matches the user's "ALL PRO EVENTS" requirement).
2. **Dual-run events still work** because `run1_value`/`run2_value`/`best_run` retain their existing semantics — the plumbing now flows from raw timers → averaged run value → best run, instead of single time entry → run value.
3. **Downstream code is unchanged.** `_metric()`, `_sort_key()`, `calculate_positions()`, handicap subtraction at [scoring_engine.py:65-69](services/scoring_engine.py#L65-L69), and STRATHMARK residual recording all read the same fields they read today (`result_value`, `best_run`, `run1_value`, `run2_value`).
4. **The migration data backfill is clean:** for existing rows with non-null values in `result_value`/`run1_value`/`run2_value`, set `t1_runN = t2_runN = the existing value`. Historical data is preserved (and the average of two identical numbers is the original number — no drift).
5. **The tie-split fix (Phase 3) is independent** of the schema change and still applies as written.
6. **STRATHMARK is unaffected** — pro single-buck residuals still read `result_value`, which is now the average of two timer readings instead of a single entry. Same field, same units.

**Form parsing change in `_save_heat_results_submission()`:** instead of reading `result_{competitor_id}`, read `t1_{competitor_id}` and `t2_{competitor_id}`. Compute the average in Python, store it in:
- `run1_value` if the heat is run 1 of a dual-run event OR if the event is single-run (the heat is by definition the only run)
- `run2_value` if the heat is run 2 of a dual-run event
- `result_value` if the event is single-run
- Then call `calculate_best_run()` if `requires_dual_runs`

**Form parsing for pro/college: same code path, no `event_type` guard needed.** Pro and college both use the same `(t1, t2)` form fields.

**Tests of this design against the canonical events:**

| Event | Heat input | Stored fields after save |
|---|---|---|
| Pro Underhand (1 run) | t1=14.32, t2=14.36 | result_value=14.34, run1_value=14.34, t1_run1=14.32, t2_run1=14.36 |
| College Single Buck (1 run) | t1=8.55, t2=8.59 | result_value=8.57, run1_value=8.57, t1_run1=8.55, t2_run1=8.59 |
| College Speed Climb run 1 | t1=22.10, t2=22.14 | run1_value=22.12, t1_run1=22.10, t2_run1=22.14 |
| College Speed Climb run 2 | t1=22.05, t2=22.09 | run2_value=22.07, t1_run2=22.05, t2_run2=22.09, best_run=22.07, result_value=22.07 |
| College Caber Toss run 1 (highest_wins) | t1=18.50, t2=18.52 (feet) | run1_value=18.51, ... |
| College Caber Toss run 2 (highest_wins) | t1=19.00, t2=19.02 (feet) | run2_value=19.01, best_run=19.01, result_value=19.01 |
| Pro Hot Saw (1 run) | t1=5.84, t2=5.86 | result_value=5.85, run1_value=5.85 |
| College Underhand Hard Hit | hits=20, tiebreak=14.5 (single judge — recommended scope reduction) | result_value=20, tiebreak_value=14.5 |

This matches the user's description exactly: "for climbing there WOULD be 2 attempts made with 2 times per run, for a total of 4 times (the average of the times for the first run would be Time 1 and the average of the two judged times for run 2 would be Time 2)" — except the user is calling the per-run averages "Time 1" and "Time 2", which in this codebase become `run1_value` and `run2_value`. And: "FOR ALL PRO EVENTS THERE IS ONE ATTEMPT, WITH TWO TIMES TAKEN AND AVERAGED TO FORM THE FINAL, POSTED, RECORDED TIME" — confirmed: every pro event in `PRO_EVENTS` is single-run, so `result_value = (t1 + t2) / 2`.

---

### A2 — REWRITTEN: Pro events ARE part of the schema change

**Severity: HIGH** | confidence 10/10 | user clarification

The original review said "every dual-timer code path must be guarded by `event_type == 'college'`." That's wrong. The user explicitly stated: **"IN ALL PRO EVENTS THERE IS ONE ATTEMPT, WITH TWO TIMES TAKEN AND AVERAGED."** Pro events are subject to the same dual-timer rule as college timed events.

**Corrected guidance:**

1. **The schema change applies globally.** All four new columns (`t1_run1`, `t2_run1`, `t1_run2`, `t2_run2`) are added to `EventResult` regardless of `competitor_type`.
2. **The form parsing logic is universal.** `_save_heat_results_submission()` reads `t1_{competitor_id}` / `t2_{competitor_id}` for ANY event with `scoring_type in ('time', 'distance')`. No `event_type` branch.
3. **The data backfill applies to BOTH pro and college historical rows.** Set `t1_runN = t2_runN = existing_value` for any non-null `result_value`/`run1_value`/`run2_value` regardless of `competitor_type`.
4. **Skip non-time/non-distance events.** `Axe Throw` (score), `Birling` (bracket), and Hard-Hit primary score (hits) are NOT averaged. Their existing input paths stay as-is.
5. **Phase 3 tie-split fix STILL needs `event_type == 'college'` guards** because tie-splitting points only applies to college (pro uses `payout_amount`, which already supports decimals). This part of the original A2 was correct — just narrower than I claimed. Specifically: [scoring_engine.py:201-206](services/scoring_engine.py#L201-L206) (the `+= points` block) is the only place that needs the college guard for the tie-split rewrite.

**STRATHMARK is unaffected** because `result_value` for pro events continues to be a single time number; it just happens to be derived from an average of two timer readings instead of a single entry. Same field, same units, same residual math.

---

### A8 — Obstacle Pole config bug (discovered during canonical review)

**Severity: MEDIUM** | confidence 9/10 | `ProAM requirements` lines 75-79, [config.py:331](config.py#L331), CLAUDE.md Section 3

The canonical requirements doc says:

> "Chokerman's Race, Obstacle Pole, and Climb will require 2x runs built into the schedule. For each of these events, you are running head-to-head against another opponent on a nearly similar course. A second run helps eliminate bias or an unfair advantage that one course has over the other. In these timed events, the fastest of these two times is used to score the event."

But [config.py:331](config.py#L331) defines college Obstacle Pole as a SINGLE-run event:

```python
{'name': 'Obstacle Pole', 'scoring_type': 'time', 'stand_type': 'obstacle_pole', 'is_gendered': True},
```

No `requires_dual_runs=True`. The code only marks Speed Climb and Chokerman's Race as dual-run.

CLAUDE.md Section 3 explicitly contradicts the requirements doc:
> "Two-run events: Chokerman's Race and Speed Climb give each competitor two runs on different courses... Obstacle Pole is single-run in both college and pro divisions."

**Two possible truths:**
- (a) The requirements doc is the spec; the code is wrong; college Obstacle Pole should be `requires_dual_runs=True` and the dual-run heat generator should produce 2 heats per Obstacle Pole.
- (b) The requirements doc is outdated; college Obstacle Pole has been intentionally simplified to a single run; CLAUDE.md is the current truth.

For pro: the requirements doc says "The pro Obstacle Pole will only be allotted ONE run" — pro Obstacle Pole is unambiguously single-run. So this only affects the COLLEGE Obstacle Pole.

**This is OUT OF SCOPE for the current scoring fix plan.** Flagging it because:
1. The canonical doc and the running config disagree.
2. If college Obstacle Pole becomes 2-run later, the schema change in A1 already supports it (the `t1_run2`/`t2_run2` columns are there) — so the schema change is forward-compatible.
3. Whoever owns the 2026 tournament setup should be told before they configure events.

**Recommendation:** Add to TODOS (not this PR). Ask the user which truth is current. If the requirements doc is right, this becomes a one-line config change + a note in the migration chain.

---

### A3 — BLOCKER: PG migration safety constraints not addressed

**Severity: HIGH** | confidence 9/10 | CLAUDE.md "Migration Protocol", `tests/test_pg_migration_safety.py`, `tests/test_migration_integrity.py`

CLAUDE.md is explicit: production runs Postgres on Railway, every migration must pass `tests/test_pg_migration_safety.py` AND `tests/test_migration_integrity.py`. The plan's migration spec is loose on three points:

1. **`server_default` is not specified.** Plan says `default=0.00` for `points_awarded`, `individual_points`, `total_points`. In SQLAlchemy, `default=` is Python-side only. PG-safe Alembic needs `server_default=sa.text("'0.00'")` or similar, AND the model column declaration must match — otherwise `test_migration_integrity.py` fails with a parity error.
2. **Type change from `Integer` to `Numeric` is not a free op in PG.** PG will require `USING column::numeric(8,2)` in the `ALTER COLUMN ... TYPE` clause. SQLite ignores type changes. Plan must specify the `op.alter_column(... postgresql_using='column::numeric(8,2)')` form.
3. **Downgrade path is not mentioned.** CLAUDE.md migration protocol requires a working `downgrade()`. Numeric→Integer downgrade is lossy if any fractional values exist; must `ROUND()` and document the loss.

**Recommendation:** Add an explicit Phase 1.4.1 step: "Run `pytest tests/test_migration_integrity.py tests/test_pg_migration_safety.py -x` after generating the migration. Both must pass before Phase 1 closes."

---

### A4 — Hard-hit and handicap events not addressed

**Severity: HIGH** | confidence 9/10 | [scoring_engine.py:45-92](services/scoring_engine.py#L45-L92)

`_metric()` and `_tiebreak_metric()` have three special-case paths the plan never mentions:

- **Hard-hit events:** primary metric is `hits` (count, not time). Tiebreak is `tiebreak_value` (a time, lowest wins). Does the new dual-timer rule apply to `tiebreak_value`? Plan is silent.
- **Handicap events:** `_metric()` subtracts `result.handicap_factor` from `result.best_run` or `result.result_value`. If Phase 3 changes `_metric()` to read `average_time` instead of `result_value` for college timed events, the handicap branch must read `average_time - handicap_factor`. Plan does not name this.
- **Combined run sum tiebreak:** `_tiebreak_metric()` at [scoring_engine.py:84-92](services/scoring_engine.py#L84-L92) sums `run1_value + run2_value` as the secondary tiebreak for non-hard-hit events. If `run1_value`/`run2_value` are no longer populated for non-dual-run college events, this tiebreak silently returns 0+0=0 for everyone, breaking ties differently than today.

**Recommendation:** Phase 3 spec must explicitly update `_metric()` AND `_tiebreak_metric()` to:
1. Use `average_time` as the primary metric for college timed events (single-heat).
2. Subtract `handicap_factor` from `average_time` when `event.is_handicap`.
3. Define a new tiebreak rule for college timed events (the spec doesn't say what to do if two competitors have identical averages — likely the smaller spread `|t1-t2|` wins, or it's just an unbreakable tie that splits per the new tie-split rule).

---

### A5 — Partner-event auto-mirror needs heat composition, not name lookup

**Severity: HIGH** | confidence 8/10 | [scoring.py:117-219](routes/scoring.py#L117-L219), [models/event.py:131-136](models/event.py#L131-L136)

The plan says: "after saving the primary competitor's EventResult ... check whether a partner competitor_id exists in the heat. If yes, write or upsert an EventResult for the partner with the same time_1/time_2 values." Verified: `EventResult.partner_name` is a **string**, not a competitor FK. The plan would need to look up the partner by name within the tournament — fragile.

**Better design (already supported by the existing code):** for partner events, `Heat.competitors` JSON already contains BOTH partner `competitor_id`s. The route loops over them at [scoring.py:138](routes/scoring.py#L138). The form already renders both competitor rows. The fix is:

1. The Phase 2 template, when `event.is_partnered`, renders BOTH competitor rows visually grouped, with ONE pair of `time_1`/`time_2` inputs whose `name=` attribute uses a synthetic `pair_id` (e.g., `time_1_pair_42`).
2. The route looks up which `competitor_id`s belong to that pair (from `Heat.competitors` ordering), and writes the same `time_1`/`time_2` to BOTH `EventResult` rows in a single loop iteration.

No name lookup, no second SELECT, atomic by construction.

**Recommendation:** Replace Phase 2.2's "upsert partner by name lookup" with the heat-composition approach above. Phase 2.1 template shape changes accordingly.

---

### A6 — Throw-off path will diverge from rebuild logic

**Severity: HIGH** | confidence 9/10 | [scoring_engine.py:302-345](services/scoring_engine.py#L302-L345)

Phase 3 says "rebuild `individual_points` as `SUM(points_awarded)` after finalization." But `record_throwoff_result()` does its own `comp.individual_points = max(0, comp.individual_points + diff)` math at [scoring_engine.py:324](services/scoring_engine.py#L324). After Phase 3, this in-place delta math will fight the rebuild approach. **Plan does not mention it.**

**Recommendation:** Phase 3.3 must replace the in-place delta math in `record_throwoff_result()` with the same batched-SUM rebuild. Otherwise axe-throw throw-off events will silently re-introduce the bug.

---

### A7 — Auto-finalize savepoint location needs care

**Severity: MEDIUM** | confidence 8/10 | [scoring.py:234-242](routes/scoring.py#L234-L242)

Phase 4.2 says wrap auto-finalize in `db.session.begin_nested()`. Fine. But the surrounding code already does `db.session.commit()` at [scoring.py:242](routes/scoring.py#L242) AFTER `engine.calculate_positions(event)`. A nested savepoint inside an outer transaction means: if `calculate_positions` raises, the savepoint rolls back, the outer commit still runs, and the heat is saved as `completed` but the points are not awarded. **This is the same partial-commit state Phase 4.2 is trying to prevent**, just at a different layer.

**Recommendation:** Phase 4.2 should specify: wrap the auto-finalize in `try` / `except` and on exception, set `event.is_finalized = False` (already done in `calculate_positions` at [scoring_engine.py:160](services/scoring_engine.py#L160)) AND **do not flash the success message**. Return an error response with HTTP 500 OR a 200 with a warning message that says "Heat saved but auto-finalization failed; please retry from the event page." The savepoint alone is not sufficient.

---

## Section 2 — Code Quality Review

### C1 — `_metric()` is the function to change in Phase 3, name it

**Severity: MEDIUM** | confidence 10/10 | [scoring_engine.py:45-71](services/scoring_engine.py#L45-L71)

Phase 3.1 says "Replace any use of result_value or a single time field as the sort key for timed events with average_time." The function that does this is `_metric(result, event)`. Plan should name it. Otherwise an executor may try to update every call site individually.

**Recommendation:** Phase 3.1 must explicitly say: "Update `_metric()` to return `result.average_time` (the new hybrid property) when `event.event_type == 'college'` and `event.scoring_type == 'time'` and not `event.requires_dual_runs`. All other branches of `_metric()` are unchanged."

### C2 — Strip-then-rebuild path duplicates work

**Severity: LOW** | confidence 9/10 | [scoring_engine.py:138-155, 201-206](services/scoring_engine.py#L138-L206)

Phase 3.3 introduces a "rebuild from SUM" pass. The existing code at [scoring_engine.py:138-155](services/scoring_engine.py#L138-L155) strips previously-awarded points first, then [scoring_engine.py:206](services/scoring_engine.py#L206) does `comp.individual_points += points` inline. After Phase 3, both the strip AND the inline +=  become redundant (the SUM rebuild handles both).

**Recommendation:** Phase 3.3 should explicitly remove the strip block (lines 138-155, college branch only) and the inline `comp.individual_points += points` at line 206. Replace with: assign `result.points_awarded` only, then call `_rebuild_individual_points(competitor_ids)` once after the loop. Pro path (`payout_amount`, `total_earnings`) is unchanged.

### C3 — Bull/Belle query design is muddled

**Severity: MEDIUM** | confidence 9/10 | Phase 5.1

Phase 5.1 says "single GROUP BY query, not a Python loop" for `get_placement_counts`. But the function signature is per-competitor. A single query per competitor is N+1 across the standings table. The right shape is **one tournament-wide query** that returns one row per competitor with all placement counts pivoted:

```sql
SELECT c.id, c.name, c.individual_points,
       COUNT(*) FILTER (WHERE er.final_position = 1) AS p1,
       COUNT(*) FILTER (WHERE er.final_position = 2) AS p2,
       COUNT(*) FILTER (WHERE er.final_position = 3) AS p3,
       COUNT(*) FILTER (WHERE er.final_position = 4) AS p4,
       COUNT(*) FILTER (WHERE er.final_position = 5) AS p5,
       COUNT(*) FILTER (WHERE er.final_position = 6) AS p6
FROM college_competitors c
LEFT JOIN event_results er
       ON er.competitor_id = c.id
      AND er.competitor_type = 'college'
      AND er.status = 'completed'
LEFT JOIN events e
       ON e.id = er.event_id
      AND e.is_finalized = TRUE
WHERE c.tournament_id = :tid
  AND c.gender = :gender
  AND c.status = 'active'
GROUP BY c.id, c.name, c.individual_points
ORDER BY c.individual_points DESC, p1 DESC, p2 DESC, p3 DESC, p4 DESC, p5 DESC, p6 DESC, c.name ASC;
```

`COUNT(*) FILTER (WHERE ...)` is PG-native (and SQLite supports it as of 3.30). One query for the whole standings, sorted in the database, no Python loop.

**Recommendation:** Phase 5.1 + 5.2 should be merged into a single SQL-based standings function. Drop the per-competitor `get_placement_counts(competitor_id, ...)` helper entirely.

### C4 — Reuse `Team.recalculate_points()` instead of inventing a new path

**Severity: LOW** | confidence 8/10 | CLAUDE.md, [scoring_engine.py:217-220](services/scoring_engine.py#L217-L220)

`Team.recalculate_points()` already exists per CLAUDE.md and is called at line 220. Phase 3.4 invents a "rebuild Team.total_points as SUM(individual_points)" path. If `recalculate_points()` already does this correctly, reuse it. If it doesn't, fix it once and all callers benefit (including the existing `recalculate_all_team_points()` at [scoring_engine.py:426-431](services/scoring_engine.py#L426-L431)).

**Recommendation:** Phase 3.4 should say "ensure `Team.recalculate_points()` uses a single batched SUM query, not row-by-row iteration. Reuse this method in the Phase 3.3 rebuild pass." If it's already correct, the plan is just "call it after the rebuild."

### C5 — Repair route needs CSRF exemption or token

**Severity: MEDIUM** | confidence 9/10 | CLAUDE.md "csrf"

CLAUDE.md: "Flask-WTF CSRFProtect active... If a new POST endpoint returns JSON rather than HTML, apply `@csrf.exempt`." Phase 4.3 specifies a JSON-returning POST repair route but does not mention CSRF. It will 400 on every call.

**Recommendation:** Phase 4.3 must add `@csrf.exempt` (with admin auth as the gate) OR render a small HTML form with `csrf_token()`. Pick one and document.

### C6 — Repair route should write audit log entries

**Severity: LOW** | confidence 8/10 | `services/audit.py`, Phase 4.3

Phase 4.3 says "logs all changes at INFO level." The codebase already has `services.audit.log_action()` for permanent audit trail. A repair tool that mutates `individual_points` and `total_points` is exactly the thing that needs an audit row, not just a logger call.

**Recommendation:** Phase 4.3 must call `log_action('points_repaired', 'tournament', tournament_id, {'before': N, 'after': M, 'competitors_repaired': K})` for each affected entity.

### C7 — Status value `'partial'` is new, document it

**Severity: LOW** | confidence 8/10 | [models/event.py:185](models/event.py#L185)

Phase 2.2 introduces `status='partial'` for incomplete entries. The current schema comment lists `pending, completed, scratched, dnf`. Adding a new value:
- Update the column docstring at [models/event.py:185](models/event.py#L185).
- Update [scoring_engine.py:157](services/scoring_engine.py#L157) which filters `r.status == 'completed'` — `'partial'` rows are correctly excluded, no change needed there, but **document why**.
- Update any template that renders status badges (`templates/scoring/event_results.html`).
- Update any test fixture that constructs `EventResult` with a status string literal.

### C8 — `time_1`, `time_2` in form field names risk collision

**Severity: LOW** | confidence 7/10 | Phase 2.1

`time_1_[competitor_id]` and `time_2_[competitor_id]` collide with potentially-existing `time_*` form fields elsewhere. Suggest namespacing: `t1_comp_42`, `t2_comp_42`, or `time_1[42]`. Minor — pick one and stick with it.

---

## Section 3 — Test Review

### Coverage diagram (post-fix expected behavior)

```
SCORING SYSTEM (post Phase 1-5)
================================
[+] services/scoring_engine.py
    │
    ├── _metric()
    │   ├── [GAP]  college time event (single heat) → average_time path
    │   ├── [GAP]  college time event + handicap → average_time - handicap_factor
    │   ├── [★★ TESTED] dual-run lowest_wins → best_run (existing test_scoring.py)
    │   ├── [★★ TESTED] dual-run highest_wins → best_run (existing)
    │   ├── [GAP]  pro time event → result_value (regression test required)
    │   └── [GAP]  hard-hit event → tiebreak_value branch unchanged
    │
    ├── calculate_positions()
    │   ├── [GAP]  fractional split-tie 2-way (each gets 6.0)
    │   ├── [GAP]  fractional split-tie 3-way (each gets 7.33)
    │   ├── [GAP]  fractional split-tie at boundary (5th-6th-7th, only 2 in points table)
    │   ├── [GAP]  partner event awards both partners full points (REGRESSION TEST — REQUIRED)
    │   ├── [GAP]  DNF after completed → team total recalculated (touched_team_ids fix)
    │   ├── [★★ TESTED] strip-then-rebuild idempotent (existing test_scoring_integration.py)
    │   └── [GAP]  rebuild SUM matches inline += under correction
    │
    ├── record_throwoff_result()
    │   └── [GAP]  rebuild SUM applied here too (A6)
    │
    └── get_bull_belle_standings()  [new]
        ├── [GAP]  tiebreak by 1st place count
        ├── [GAP]  tiebreak by 2nd place count
        ├── [GAP]  full tie → 'TIE' flag in output
        ├── [GAP]  null gender competitor → excluded with warning
        └── [GAP]  Bull excludes F, Belle excludes M (regression)

[+] routes/scoring.py
    │
    ├── _save_heat_results_submission()
    │   ├── [GAP]  college timed event → t1+t2 stored, no result_value write
    │   ├── [GAP]  partner event → both EventResult rows get same t1/t2
    │   ├── [GAP]  partial entry (only t1) → status='partial'
    │   ├── [GAP]  pro event → unchanged behavior (REGRESSION TEST — REQUIRED)
    │   ├── [GAP]  dual-run college event → still uses run1/run2 (REGRESSION)
    │   └── [GAP]  hard-hit college event → still uses tiebreak_value (REGRESSION)
    │
    ├── undo_heat_save()
    │   ├── [GAP]  points stripped before delete
    │   ├── [GAP]  individual_points rebuilt for affected
    │   ├── [GAP]  team total_points rebuilt for affected
    │   └── [GAP]  savepoint rollback on partial failure
    │
    └── repair_points()  [new]
        ├── [GAP]  full repair on a tournament with known drift
        ├── [GAP]  CSRF/auth gate (401 unauth)
        ├── [GAP]  audit log row written
        └── [GAP]  idempotent (running twice → no change second run)

USER FLOW COVERAGE
===================
[+] Judge enters dual-timer result
    ├── [GAP] [→E2E] full happy path (open heat → enter t1+t2 → see live avg → save → see avg in results)
    ├── [GAP]        partial save (only t1 entered, save → 'partial' badge shown)
    └── [GAP]        finalization blocked when any row is 'partial'

[+] Judge enters partner event result
    ├── [GAP] [→E2E] J&J pair: enter one t1/t2 → both partners have rows on save
    └── [GAP]        team standings reflect points twice (regression for AWFC dual-credit rule)

[+] Admin runs repair tool
    └── [GAP] [→E2E] tournament with broken cache → run repair → standings now correct

REGRESSION TESTS (mandatory — IRON RULE)
==========================================
- [GAP] R-01: pro single-buck timing path produces same result_value as before
- [GAP] R-02: Speed Climb dual-run produces same best_run as before
- [GAP] R-03: Hard-Hit event ranks by hits desc, tiebreak_value asc as before
- [GAP] R-04: Handicap event subtracts start mark from average (was: from result_value)
- [GAP] R-05: STRATHMARK residual recording reads valid result for pro events
- [GAP] R-06: existing tests/test_scoring_college_points.py:576-584 — REWRITTEN to assert split tie

────────────────────────────────────────────
COVERAGE DELTA: 30+ new tests required across 4 test files
────────────────────────────────────────────
```

### T1 — Plan understates the test rewrite scope

**Severity: HIGH** | confidence 9/10 | CLAUDE.md (37 test files)

The plan calls out updating two files: `tests/test_scoring_college_points.py:576-584` and `tests/fixtures/synthetic_data.py`. **The actual cascade is much larger.** Any test that:
- Constructs an `EventResult` with `result_value=N` for a college timed event → must add `time_1=N, time_2=N`
- Asserts `points_awarded == 10` for a tied 1st → must update to `points_awarded == 8.5` (or `Decimal('8.5')`)
- Asserts integer math anywhere on `individual_points` or `total_points` → must accept Decimal
- Tests `_metric()` directly → must cover the new average_time branch

Likely affected files (from CLAUDE.md inventory): `test_scoring.py`, `test_scoring_engine_integration.py`, `test_scoring_full_event.py`, `test_scoring_integration.py`, `test_partnered_events_realistic.py`, `test_axe_throw_qualifiers.py`, `test_models.py`, `test_models_full.py`, `test_point_calculator.py`, `test_fuzz_scoring.py`, plus the synthetic fixture file.

**Recommendation:** Phase 3.6 must include: "Run `pytest tests/ -x` after the engine rewrite. Triage every failure into one of: (a) test asserts old wrong behavior → rewrite; (b) test asserts shared infrastructure that broke → fix the engine, not the test; (c) test is unrelated → leave alone." Budget for 30-50 test rewrites across ~10 files.

### T2 — Mandatory regression test for partner events

**Severity: CRITICAL** | confidence 10/10 | Phase 3.5

The plan already requires this. Make sure it's actually written and passing **before** Phase 3 closes. The exact assertions:

```python
def test_partner_event_dual_credit_full_chain():
    """J&J: both partners get full points; team gets points twice."""
    # setup: 2 schools, A team has Mike + Mary, B team has Bob + Beth
    # event: Jack & Jill (partnered)
    # heat: A pair (Mike+Mary) wins with avg 22.0; B pair (Bob+Beth) places 2nd with 24.0
    # finalize event
    assert mike.individual_points == Decimal('10')   # A pair wins → 10 each
    assert mary.individual_points == Decimal('10')
    assert bob.individual_points == Decimal('7')     # B pair 2nd → 7 each
    assert beth.individual_points == Decimal('7')
    assert team_a.total_points == Decimal('20')      # 10 + 10 = 20 (the spec's "twice" rule)
    assert team_b.total_points == Decimal('14')      # 7 + 7 = 14
    # negative regression: J&J event_result row count is 4 (one per competitor), not 2
    assert EventResult.query.filter_by(event_id=jj_event.id).count() == 4
```

Plus a fractional version: 2 pairs tied for 1st → both partners on each tied pair get `(10+7)/2 = 8.5`, team gets `17.0`.

### T3 — Migration data preservation test

**Severity: HIGH** | confidence 8/10 | Phase 1.5

Phase 1.5 says "Run the existing test suite. No new failures." That's necessary but not sufficient. Add an explicit test:

```python
def test_phase1_migration_preserves_existing_results():
    """Pre-migration: result_value=22.5; post-migration: time_1=22.5 AND time_2=22.5."""
    # use the migration test harness already in tests/test_migration_integrity.py
    # run downgrade then upgrade; verify no data loss
    # specifically verify Numeric(8,2) round-trip with values like 22.55, 100.99
```

### T4 — Migration test for existing fractional points = 0 case

**Severity: LOW** | confidence 7/10

Existing `points_awarded` rows are all whole integers. Phase 1.4 backfill is no-op. Add one assertion to confirm no precision loss on the type-change ALTER.

---

## Section 4 — Performance Review

### P1 — N+1 risk in repair route

**Severity: MEDIUM** | confidence 8/10 | Phase 4.3

`POST /admin/scoring/repair_points?tournament_id=N` iterates events, then for each event re-runs distribution, then rebuilds individual_points for every competitor, then rebuilds team totals. If implemented naïvely this is O(events × competitors × heats). For a tournament with 25 events × 80 competitors that's 2000 result lookups.

**Recommendation:** Phase 4.3 should batch:
1. One query to fetch all `EventResult` rows for the tournament with `Event.is_finalized = True`.
2. Loop in Python to compute `points_awarded` per row using the same `calculate_positions` engine (per event).
3. One UPDATE per event_result row (or bulk UPDATE via `db.session.bulk_update_mappings`).
4. **One** rebuild query per competitor table (the SUM rebuild from C3 above) for individual_points.
5. **One** rebuild query for team totals.

Total: O(events) writes for positions + 2 batch rebuild queries. Acceptable race-day perf.

### P2 — Bull/Belle standings under P1 fix is fine

**Severity: LOW** | confidence 9/10

If C3's single-query design is adopted, Bull/Belle is one query. No perf concern.

### P3 — Live standings poll polls every 10s — Decimal serialization

**Severity: LOW** | confidence 7/10 | [scoring.py:352-356](routes/scoring.py#L352-L356), [scoring_engine.py:438-478](services/scoring_engine.py#L438-L478)

`live_standings_data()` builds a dict with `points` values that, after Phase 1, are `Decimal`. `jsonify()` does NOT serialize `Decimal` natively — it raises `TypeError`. Plan does not mention this.

**Recommendation:** Phase 3 must include: convert all `Decimal` points to `float` (for display) or `str` (for exact precision) when building JSON responses. Apply at the boundary in `live_standings_data()`, `preview_positions()`, and the new repair route.

---

## NOT in Scope (per the plan)

- Knowledge events (Cruise, Traverse, Dendro, Wood ID) — deferred. Document this in release notes.
- Pro division paths — should be unchanged. Plan must enforce in code (see A2).
- Adding/removing/renaming events.
- STRATHMARK integration — should be unchanged. Plan must enforce that pro paths are skipped (see A2).
- Friday Night Feature flow — appears unaffected, but no one verified. Worth a smoke test.
- Heat sheet PDF generation — display layer; needs to format Decimal correctly.

---

## Failure Modes

For each new code path, the realistic production failure and whether the plan handles it:

| Path | Failure mode | Plan handles? |
|---|---|---|
| Phase 1 migration | PG `ALTER COLUMN ... TYPE numeric` without `USING` clause → migration crash | NO (A3) |
| Phase 1 backfill | Pro rows get time_1=time_2=result_value, corrupts STRATHMARK residuals | NO (A2) |
| Phase 2 form save | Judge enters only t1, hits save, t2 missing | YES (`status='partial'`) |
| Phase 2 form save | Judge enters t1=22.5, t2=22.55; FE shows 22.525, BE stores 22.53 (Numeric(8,2) rounding) | NO — display drift |
| Phase 2 partner | Partner not in heat (data integrity bug) → no second row written silently | NO — needs explicit error |
| Phase 3 finalize | Decimal serialization in `jsonify` → TypeError on every standings poll | NO (P3) |
| Phase 3 finalize | `_metric()` reads `result_value` for handicap math but result_value is stale | NO (A4) |
| Phase 3 finalize | Throw-off path uses old in-place delta math, diverges from rebuild | NO (A6) |
| Phase 4 undo | Network blip mid-rebuild; partial state | YES (savepoint) — but see A7 |
| Phase 4 auto-finalize | calculate_positions raises but heat marked complete | PARTIAL (A7) |
| Phase 4 repair | Race condition: judge enters new heat while repair runs | NO — needs row-level locking |
| Phase 5 Bull/Belle | competitor with NULL gender | YES (5.4) |
| Phase 5 Bull/Belle | All placements identical, infinite tie | YES (UI flag) |

**Critical gaps (unhandled failure modes):** A2, A3, A4, A6, P3, partner-data-integrity, repair race condition. Seven total.

---

## Worktree Parallelization Strategy

Per CLAUDE.md, Phase 1 → 2 → 3 → 4 → 5 are sequentially gated by the user. **Within each phase, no parallelism.** But across phases, two streams can advance concurrently in worktrees once Phase 1 lands:

| Lane | Phase | Modules | Depends on |
|---|---|---|---|
| A | Phase 2 (template + route) | `templates/scoring/`, `routes/scoring.py` | Phase 1 schema |
| B | Phase 5 (Bull/Belle) | `services/scoring_engine.py` (standings only) | Phase 1 schema |

Phase 2 (Lane A) and Phase 5 (Lane B) touch disjoint regions of `scoring_engine.py` (Phase 2 doesn't touch the engine; Phase 5 only touches the standings functions, not `calculate_positions`). After Phase 1 ships, A and B can run in parallel worktrees and merge independently. Phase 3 must wait for both because it rewrites `calculate_positions` which is the integration point.

**Conflict flag:** Both lanes will touch `tests/test_scoring_*.py`. Coordinate via `handoffs.md`.

---

## Unresolved Decisions (the plan punts on these)

1. **Dual-run × dual-timer interaction** (A1) — biggest unknown.
2. **Tiebreak rule when two college timed-event averages are exactly equal** — spec doesn't say. Default to split-points-equally?
3. **Display format for non-time events that show "avg (T1/T2)"** — partial entries? Format `22.5 (22.5 / —)`?
4. **Repair tool — async or sync?** A 25-event tournament with 80 competitors × full re-run might take >30s. Sync risks request timeout. Use the existing `background_jobs.submit()` pattern from CLAUDE.md.
5. **Decimal vs float in API responses** (P3) — pick one.
6. **Test fixture migration** (T1) — how aggressive? Auto-rewrite or hand-rewrite?

---

## Prioritized Fix List (apply BEFORE Phase 1 ships)

| # | Item | Phase | Severity |
|---|------|-------|----------|
| 1 | Adopt the per-run dual-timer schema (A1 rewritten): add `t1_run1`, `t2_run1`, `t1_run2`, `t2_run2`. Existing `run1_value`/`run2_value`/`result_value`/`best_run` are populated by averaging upstream. | 1 | CRITICAL |
| 2 | Apply schema globally to pro AND college (A2 rewritten); narrow college guards to ONLY the tie-split `+= points` block in scoring_engine.py:201-206 | 1, 3 | HIGH |
| 3 | Specify `server_default` + PG `USING` clause + downgrade (A3) | 1 | CRITICAL |
| 4 | Update `_metric()` for handicap + hard-hit + tiebreak_metric for combined-run-sum (A4) | 3 | HIGH |
| 5 | Replace name-lookup partner mirror with heat-composition design (A5) | 2 | HIGH |
| 6 | Update `record_throwoff_result()` to use rebuild path (A6) | 3 | HIGH |
| 7 | Auto-finalize savepoint + flash message handling (A7) | 4 | MEDIUM |
| 8 | Name `_metric()` and `_tiebreak_metric()` explicitly in Phase 3 (C1) | 3 | MEDIUM |
| 9 | Drop redundant strip block; reuse Team.recalculate_points (C2, C4) | 3 | LOW |
| 10 | Single-query Bull/Belle with COUNT(*) FILTER (C3) | 5 | MEDIUM |
| 11 | CSRF exemption + audit log on repair route (C5, C6) | 4 | MEDIUM |
| 12 | Document `status='partial'` everywhere (C7) | 2 | LOW |
| 13 | Decimal → float at JSON boundary (P3) | 3 | MEDIUM |
| 14 | Partner event regression test (T2) | 3 | CRITICAL |
| 15 | Migration data preservation test (T3) | 1 | HIGH |
| 16 | Triage 30-50 cascading test failures (T1) | 3 | HIGH |
| 17 | Repair route batched + idempotency test (P1) | 4 | MEDIUM |

---

## Completion Summary

- Step 0: Scope Challenge — accepted as-is, 5 phases, sequentially gated. Knowledge events deferred.
- Architecture Review: **7 issues** (3 CRITICAL blockers, 3 HIGH, 1 MEDIUM)
- Code Quality Review: **8 issues** (1 LOW, 5 MEDIUM, 2 LOW)
- Test Review: diagram produced, **30+ test gaps** identified, **6 mandatory regression tests**
- Performance Review: **3 issues** (1 MEDIUM, 2 LOW)
- NOT in scope: written
- What already exists: written
- Failure modes: **7 critical gaps** flagged
- Outside voice: not run (no Codex CLI; subagent route would duplicate this review's findings)
- Parallelization: 2 lanes possible after Phase 1 ships (Phase 2 || Phase 5); Phase 3 must wait for both
- Unresolved decisions: **6** (listed above)

---

## Verdict (LOCKED v3)

**PLAN CLEARED FOR EXECUTION.** All three blockers resolved by user on 2026-04-08:

| Blocker | v1 verdict | v3 verdict |
|---|---|---|
| **A1** schema design | HOLD — ambiguous | RESOLVED — per-run four-column design adopted |
| **A2** pro vs college scope | HOLD — overcorrected | RESOLVED — applies to both; narrow college guard at one site |
| **A3** PG migration safety | HOLD — unaddressed | RESOLVED — user authorized doing it right |

**A8** (college Obstacle Pole 1-run vs 2-run) → user decided 1-run for 2026, no code change.

The other 14 items in this review are smaller, executor-time concerns. They are documented and the executor session must read them all before starting each phase.

### Execution prerequisites (do NOT start Phase 1 until ALL of these are true)

1. **session_A's gear audit branch must be merged to main.** Currently on `fix/gear-sharing-hardening-g1-g8`, phase `gear_audit_rebase_and_ship`, awaiting user go-ahead. The scoring fix needs a clean main to branch from. If session_C starts editing source files now, it will land changes inside session_A's working tree and violate standing rules 1 and 4 in `instance/coordination.md`.
2. **A new feature branch** `fix/scoring-engine-dual-timer-tiesplit` (or similar) created from fresh main.
3. **`pytest tests/test_migration_integrity.py tests/test_pg_migration_safety.py` baseline run** on the new branch — record the pass count, then re-run after each migration step to detect drift introduced by Phase 1.
4. **`flask db current` confirmed at HEAD** (`e9f0a1b2c3d4` per MEMORY.md) before generating the new migration.
5. **`instance/coordination.md` phase advanced** to `scoring_fix_in_progress` with session_C (or whichever session is executing) marked as the actor.

### Phase-by-phase reminders for the executor

- **Phase 1:** The migration MUST set `nullable=True` explicitly on all four new columns (they're NULL until a heat is scored). The `points_awarded` / `individual_points` / `total_points` Integer→Numeric type changes MUST use `op.alter_column(... type_=sa.Numeric(8,2), postgresql_using='column_name::numeric(8,2)', existing_nullable=False, existing_server_default=sa.text("'0'"), server_default=sa.text("'0.00'"))`. The `downgrade()` MUST be tested by running `flask db downgrade -1 && flask db upgrade` on a fresh dev database. After all of that, run BOTH `tests/test_migration_integrity.py` AND `tests/test_pg_migration_safety.py` and they MUST pass clean.
- **Phase 2:** Form parser change is one function (`_save_heat_results_submission`) at [routes/scoring.py:117](routes/scoring.py#L117). Read `t1_{competitor_id}` and `t2_{competitor_id}`, average them in Python, store the average in the existing field (`run1_value` / `run2_value` / `result_value` based on heat type), then call `calculate_best_run()` if `requires_dual_runs`. Both pro and college use the same code path. Hard-Hit primary score (hits) is NOT averaged — leave that input as-is. Hard-Hit `tiebreak_value` stays single-judge for v1 (scope-reduction call from A1 above).
- **Phase 3:** The college tie-split guard is ONLY at [scoring_engine.py:201-206](services/scoring_engine.py#L201-L206) — the `comp.individual_points += points` block. The Decimal vs float JSON serialization fix (P3) is required at every `jsonify()` call site that returns points data. The `record_throwoff_result()` rebuild path (A6) is mandatory. The `_metric()` and `_tiebreak_metric()` updates (A4) are mandatory.
- **Phase 4:** Heat undo points strip (4.1), auto-finalize savepoint (4.2 + A7), repair route with `@csrf.exempt` and audit log (4.3 + C5 + C6), team DNF recalc fix (4.4) — all four are required.
- **Phase 5:** Single tournament-wide query with `COUNT(*) FILTER (WHERE final_position = N)` pivots (C3). Multi-key sort in SQL, not Python. Tie flag in UI. Gender isolation with null-gender exclusion.

### Recommended execution sequence

1. Wait for session_A gear audit to merge.
2. Create new branch from fresh main.
3. Run baseline tests (full suite + migration integrity + PG safety).
4. Execute Phase 1, run integrity tests, commit, push, get human approval.
5. Execute Phase 2, run full suite, commit, push, get human approval.
6. Execute Phase 3, run full suite + the new partner-event regression test (T2), commit, push, get human approval.
7. Execute Phase 4, run full suite + repair route idempotency test, commit, push, get human approval.
8. Execute Phase 5, run full suite + Bull/Belle tiebreak test, commit, push, get human approval.
9. After all 5 phases land on main, run `/document-release` to update CHANGELOG / DEVELOPMENT.md / MEMORY.md.

**The executor MUST re-read this PLAN_REVIEW.md document before starting each phase.** Findings A0–A8, C1–C8, T1–T4, P1–P3 are not optional reminders — they are the contract.

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | **HOLD** | 18 issues, 7 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | — |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

**VERDICT:** HOLD — 3 CRITICAL blockers must be resolved with the user before Phase 1 executes. See "Prioritized Fix List" items 1-3.
