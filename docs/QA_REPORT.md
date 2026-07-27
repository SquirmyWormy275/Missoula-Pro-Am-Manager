# Pre-Deployment QA Report
## Date: April 10, 2026
## Testers: Claude Code (automated) + OpenAI Codex (cross-model review)
## Database state: 115 competitors, 45 events, 131 heats

### Executive Summary
This QA pass found multiple live-ops defects that should be fixed before the April 24-25, 2026 event. The highest-confidence issues are in score validation, scratch propagation, and heat-generation integrity. Concurrent heat mutation also remains a material operational risk because scoring itself has optimistic locking, but day-of scheduling mutations do not. The application is not yet ready for live use without targeted fixes and a rerun of the QA suite.

Local-runtime caveat: this QA session ran against the local SQLite runtime database because `DATABASE_URL` was not set in this shell. Findings are still valid for application behavior, but PostgreSQL- or Railway-specific runtime effects were not exercised here.

### Cross-Model Consensus (Claude + Codex agree)
- Negative timed scores are accepted and persisted instead of being rejected. Claude reproduced this in `BUG-EDGE-001`; Codex independently flagged the same validation gap in `CODEX-DATA-004` and `CODEX-EDGE-001`.
- Pro scratch does not fully propagate into operational schedule state. Claude reproduced this in `BUG-EDGE-002`; Codex independently flagged it in `CODEX-DATA-006` and `CODEX-EDGE-002`.
- Heat generation does not satisfy the SB/UH alternation rule. Claude reproduced this in `BUG-INT-001`; Codex independently traced the cause to the lack of cross-event rotation state in `CODEX-DATA-001`.
- Heat mutation integrity is weak during day-of operations because scheduling edits do not use the same optimistic version checks as scoring. Codex flagged this in `CODEX-DATA-002`, and the overall concurrency risk remained a top item in the final synthesis.
- Heat/schedule state can diverge from underlying assignment data. Codex flagged missing `HeatAssignment` synchronization in `CODEX-DATA-003`, and the edge review also surfaced orphaned or duplicate heat roster risks in `CODEX-EDGE-004` and `CODEX-EDGE-005`.

### Claude-Only Findings
- `BUG-SMOKE-001`: `/api/public/tournaments/1/standings-stream` returns 500 due to `Decimal` JSON serialization.
- `BUG-SMOKE-002`: `/api/v1/public/tournaments/1/standings-stream` returns 500 due to `Decimal` JSON serialization.
- `BUG-SMOKE-003`: `POST /tournament/1/clone` returns 500 due to `NOT NULL constraint failed: teams.school_abbreviation`.
- `BUG-SMOKE-004`: `GET /tournament/1/setup` returns 500 due to `ValueError: could not convert string to float: 'drawn'`.
- `BUG-SMOKE-005`: `GET /registration/1/pro/gear-sharing/print` returns 500 due to Jinja `No test named 'search'`.
- The scoring stale-write path handled the reproduced concurrent heat-entry test correctly by returning `409` on the stale second submission.

### Codex-Only Findings
- `CODEX-SMOKE-001`: tournament activation is a state-changing GET route.
- `CODEX-SMOKE-002`: audit log pagination trusts malformed `page` input.
- `CODEX-SMOKE-003`: `/health/diag` exposes deployment details without auth.
- `CODEX-SMOKE-004`: database restore accepts any SQLite-header file and replaces the live DB.
- `CODEX-SMOKE-005`: export job-status route does not bind `job_id` to tournament context.
- `CODEX-SMOKE-006`: scheduling preflight job-status route can leak unrelated job details.
- `CODEX-SMOKE-007`: throw-off submission lacks placement range and uniqueness validation.
- `CODEX-SMOKE-008`: competitor portal path suppresses event-parse exceptions instead of surfacing them.
- `CODEX-EDGE-003`: `max_stands <= 0` can crash heat generation.
- `CODEX-EDGE-004`: orphaned competitor IDs in heat JSON disappear from scoring forms.
- `CODEX-EDGE-005`: duplicate competitor IDs in a heat are neither normalized nor rejected.

### Route Smoke Test Results
- Routes discovered: 207
- Routes executed: 201
- Skipped due to missing real IDs: 6
- Pass rate: 196/201
- 500 errors found:
  - `BUG-SMOKE-001` `api.standings_stream`
  - `BUG-SMOKE-002` `api_v1.standings_stream`
  - `BUG-SMOKE-003` `main.clone_tournament`
  - `BUG-SMOKE-004` `main.tournament_setup`
  - `BUG-SMOKE-005` `registration.pro_gear_print`

### Integration Test Results
- Workflows tested: 7
- Pass rate: 6/7
- Critical findings:
  - `BUG-INT-001` SB/UH Heat 1 competitors were not rotated apart

### Edge Case Test Results
- Scenarios tested: 5
- Pass rate: 3/5
- Surprising behaviors:
  - `BUG-EDGE-001` negative timed scores were accepted with HTTP 200 and saved
  - `BUG-EDGE-002` pro scratch left a generated-heat competitor in place

### Bug Registry (all sources, deduplicated)

CRITICAL (must fix before April 24):
1. Negative and non-finite score input is accepted in scoring paths, allowing impossible results to be saved and ranked. Sources: Claude `BUG-EDGE-001`; Codex `CODEX-DATA-004`, `CODEX-EDGE-001`.
2. Pro scratch does not deschedule competitors from heats or reliably transition result state, so scratched competitors can still appear on heat sheets and remain scoreable. Sources: Claude `BUG-EDGE-002`; Codex `CODEX-DATA-006`, `CODEX-EDGE-002`.
3. Day-of heat mutation routes lack optimistic version checks on affected heats, creating a real risk of lost updates or inconsistent mirrored heat state under concurrent staff use. Source: Codex `CODEX-DATA-002`.
4. Heat generation does not implement cross-event SB/UH rotation, so the same ranked competitors can be placed into Heat 1 repeatedly across events. Sources: Claude `BUG-INT-001`; Codex `CODEX-DATA-001`.
5. Public standings stream endpoints already 500 due to `Decimal` JSON serialization. If these back live/public displays, the failure will be visible immediately. Sources: Claude `BUG-SMOKE-001`, `BUG-SMOKE-002`.

HIGH (fix if time permits):
1. Tournament setup page 500s on current data because `'drawn'` is parsed as float. Source: Claude `BUG-SMOKE-004`.
2. College scratch/delete cleanup updates heat JSON without rebuilding `HeatAssignment`, allowing schedule representations to drift. Source: Codex `CODEX-DATA-003`.
3. Heat generation trusts invalid `max_stands` values and can crash on `0` or negative sizes. Source: Codex `CODEX-EDGE-003`.
4. Clone tournament POST 500s on team abbreviation integrity constraints. Source: Claude `BUG-SMOKE-003`.
5. Pro gear-sharing print route 500s due to a Jinja test mismatch. Source: Claude `BUG-SMOKE-005`.
6. Throw-off result submission accepts arbitrary placements without enforcing sensible ranking constraints. Source: Codex `CODEX-SMOKE-007`.

MEDIUM (post-event):
1. CSV result import accepts arbitrary statuses and can import malformed partial data as completed results. Source: Codex `CODEX-DATA-005`.
2. Orphaned competitor IDs in heat JSON can disappear from scoring forms, producing schedule/scoring mismatches. Source: Codex `CODEX-EDGE-004`.
3. Duplicate competitor IDs in a heat are not normalized or rejected. Source: Codex `CODEX-EDGE-005`.
4. Audit log pagination can 500 on malformed `page` values. Source: Codex `CODEX-SMOKE-002`.
5. Export and preflight job-status endpoints can leak unrelated job information. Sources: Codex `CODEX-SMOKE-005`, `CODEX-SMOKE-006`.
6. Portal event parsing swallows exceptions and can hide data issues from users. Source: Codex `CODEX-SMOKE-008`.

LOW (cosmetic):
1. `/health/diag` exposes unnecessary environment details without authentication. Source: Codex `CODEX-SMOKE-003`.
2. Tournament activation uses a state-changing GET instead of a protected POST. Source: Codex `CODEX-SMOKE-001`.

### Gaps (features expected but not found)
- No pro-side equivalent of the college scratch cleanup helper that removes competitors from heats and scratches related results.
- No server-side validation layer enforcing non-negative score input or positive heat-size constraints.
- No automatic repair path for orphaned competitor IDs inside `Heat.competitors`; the current sync tool only reconciles `HeatAssignment`.
- Live-event QA did not exercise Railway/PostgreSQL runtime behavior because this shell was pointed at local SQLite.
- Offline/mobile retry behavior under unreliable WiFi was not reproduced directly in this session.

### Passing Highlights (things that work correctly)
- Base test suite remained strong overall at Phase 0: 2272 passed, 0 failed, with only environment-related permission errors.
- Smoke coverage exercised 201 routes and kept the failure list narrow and concrete.
- Registration-to-heat workflow worked end-to-end in the QA harness.
- Timed scoring persisted results and allowed clean re-entry updates in the tested workflow.
- Concurrent heat scoring produced the expected stale-version `409` on the second submission in the reproduced case.
- Day-of scratch, move, add-to-heat, and delete-empty-heat routes worked coherently in the tested isolated workflow.
- ALA membership report rendered HTML and generated a non-empty PDF.
- Heat generation handled exactly `1`, `max_per_heat`, and `max_per_heat + 1` competitors without malformed heat counts.
- Unassigned competitor views and all-scratched-heat rendering did not crash in the tested cases.
- Scored event regeneration did not silently replace heats without confirmation.

### Codex Adversarial Summary
Codex was most useful on three fronts: identifying schedule/data-integrity gaps not directly exercised by the happy-path tests, separating live-event risks from lower-priority security findings, and surfacing state combinations the current code does not defend against. The strongest cross-model agreement was on score validation, pro scratch propagation, and heat-generation integrity. Codex did not contradict the reproduced bugs; it mainly added structural risks around concurrency, assignment synchronization, malformed heat data, and invalid configuration values.

### Cross-Model Comparison
- Findings both models agree on:
  - Invalid negative score input is accepted
  - Pro scratch does not propagate into operational heat/result state
  - SB/UH heat generation does not satisfy the intended alternation rule
- Findings only Claude caught:
  - Specific crashing routes already returning 500 in the current runtime
  - Concrete stale-write `409` success on the reproduced concurrent scoring path
- Findings only Codex caught:
  - Missing version checks on day-of heat mutation routes
  - Missing `HeatAssignment` synchronization in college scratch/delete cleanup
  - Invalid `max_stands`, orphaned heat competitors, and duplicate heat competitor risks
  - Job-status and restore-path issues outside the tested workflows
- Contradictions:
  - None on the reproduced bugs
  - One prioritization caveat: not every Codex smoke finding is equally relevant to visible on-floor failure on April 24-25, 2026

### Review of New Test Files
- Review target: [test_route_smoke.py](/c:/Users/Alex%20Kaper/Desktop/John%20Ruffato%20Startup%20Challenge/Python/Missoula%20Pro%20Am/Missoula-Pro-Am-Manager/tests/test_route_smoke.py), [test_integration_qa.py](/c:/Users/Alex%20Kaper/Desktop/John%20Ruffato%20Startup%20Challenge/Python/Missoula%20Pro%20Am/Missoula-Pro-Am-Manager/tests/test_integration_qa.py), [test_edge_cases.py](/c:/Users/Alex%20Kaper/Desktop/John%20Ruffato%20Startup%20Challenge/Python/Missoula%20Pro%20Am/Missoula-Pro-Am-Manager/tests/test_edge_cases.py)
- Findings: no test-correctness issue was found that invalidates the reported bug list
- Residual risk: the QA harness uses copied local SQLite databases and direct admin session injection, so it validates application behavior but not Railway/PostgreSQL-specific runtime behavior or real venue-network retries
