---
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
execution: code
title: Fail-Closed Show Readiness
date: 2026-08-17
owner: Codex
---

# Fail-Closed Show Readiness

## Direction

Make the tournament schedule impossible to report as successfully built when
partner, gear-sharing, or downstream field-preparation invariants were skipped.
The implementation must preserve the owner-authored show order and existing
scored/finalized-history protections.

## Authority

- `docs/DOMAIN_CONTRACT.md` owns the canonical workflow and requires preflight,
  heat generation, pro flights, relay, spillover, and dependent field-prep in
  that order.
- `docs/Alex's Docs/GEAR_SHARING_DOMAIN.md` requires competitors sharing gear
  to be placed in different heats, including cross-event and multi-person
  sharing groups.
- `docs/Alex's Docs/ProAM requirements` owns event format, physical stands,
  partnered-event, dual-run, relay, and Saturday-spillover rules.
- `FlightLogic.md` owns flight ordering detail not repeated in the domain
  contract.

## Product Contract

### In Scope

1. Heat generation expands the heat count when necessary to separate feasible
   gear-sharing conflicts. It never silently places sharing competitors in the
   same heat.
2. When a safe layout cannot be produced, generation raises a structured safety
   error before deleting or replacing an existing layout.
3. Partnered-event entrants with blank, unresolved, self-referential,
   nonreciprocal, or gender-invalid declarations block the affected event. They
   are not silently held back while the remaining schedule is committed.
4. Full-show web and background generation use one application service and the
   same transaction boundary.
5. Full-show generation runs fresh pre-generation blockers before mutation and
   validates generated schedule blockers before commit.
6. Every successful full-show build performs heat generation, pro-flight build,
   relay placement, college spillover, and saw-block assignment in canonical
   order.
7. Operator messages identify blocking issue codes, affected events or people,
   and the Preflight repair path without exposing raw exception text.
8. Partnered Axe readiness is evaluated from its state-machine pairs and stages,
   not duplicate registration partner fields. Prelim result rows permit the
   first finals-card build; finals scores and completed finals history do not.
9. Every dual-run entrant changes physical stand or course between runs, even in
   a one-person heat, and College gear checks use bare names rather than team-
   suffixed display labels.

### Out Of Scope

- Redesigning free-text gear parsing.
- Adding an operator override that permits an unsafe schedule.
- Changing scoring, payouts, event rules, physical stand allocations, or flight
  ordering policy.
- Accessing production, private mirror data, or PII.
- Adding a persisted preflight approval receipt. Every build instead runs a
  fresh gate against current database state, avoiding stale approvals.

## Key Technical Decisions

### KTD-1: Fresh gate, not a persisted approval

- Decision: full builds evaluate current preflight input blockers immediately
  after taking the tournament schedule lock.
- Provenance: user-approved through the standing request to execute repository
  rules and the canonical domain contract.
- Rejected alternative: store an operator-approved preflight fingerprint in
  `Tournament.schedule_config`.
- Reason: fresh evaluation is stronger, avoids a schema/config lifecycle, and
  cannot become stale between approval and generation.

### KTD-2: Expand before blocking

- Decision: event-local placement may add heats to separate gear-sharing units,
  while preserving event capacity, partnered units, ability ordering, slow
  springboard grouping, and left-handed dummy rules.
- Provenance: user-directed owner requirements in
  `docs/Alex's Docs/GEAR_SHARING_DOMAIN.md`.
- Rejected alternative: retain the current fallback and merely warn after
  forcing a same-heat conflict.
- Reason: the warned schedule still violates the operating rule and may be
  physically unusable.

### KTD-3: One full-build service

- Decision: synchronous and asynchronous full-show entry points delegate to one
  orchestration service with a single commit.
- Provenance: user-approved through `docs/DOMAIN_CONTRACT.md` and the request
  for an ambitious unattended workflow.
- Rejected alternative: patch every route independently.
- Reason: independent call chains already drifted; the background path omits
  saw-block assignment.

### KTD-4: No production-shaped claim from SQLite alone

- Decision: local verification includes focused SQLite tests and every available
  disposable PostgreSQL CI lane; standard pytest is reported separately from
  `proam_regression/`.
- Provenance: user-approved repository validation boundary.
- Rejected alternative: treat the ordinary green pytest suite as PostgreSQL
  and private-mirror evidence.
- Reason: `pytest.ini` excludes `proam_regression/`, and SQLite does not prove
  PostgreSQL behavior.

## Implementation Units

### U1: Gear-Safe Event Generation

Owned files:

- `services/heat_generator.py`
- `tests/test_heat_generator.py`
- `tests/test_heat_gen_integration.py`
- focused gear-sharing generator tests as needed

Work:

1. Replace forced-conflict fallback placement with deterministic safe heat
   expansion for standard, saw, and springboard events.
2. Treat partnered pairs as one placement unit and check gear conflicts between
   units, never within the pair itself.
3. Ensure left-handed springboard and slow-heat constraints remain valid when
   extra heats are required.
4. Validate the final candidate layout before `_delete_event_heats()`.
5. Raise `HeatGenerationSafetyError` with actionable, non-sensitive details if
   the layout remains unsafe or an entrant cannot be represented.
6. Replace contradictory warning-acceptance tests with fail-closed or expanded-
   layout assertions.

Red evidence:

- Two sharing competitors on a two-stand event currently rebuild into one heat
  and log a warning.
- A larger sharing group can currently be accepted with same-heat conflicts.

Acceptance:

- No generated heat contains a gear-sharing conflict.
- Every eligible entrant appears exactly once in run 1, except no entrant may be
  omitted as a successful outcome.
- Existing layouts survive a failed regeneration unchanged.
- Dual-run rosters remain mirrored and stand assignments remain valid.

### U2: Shared Full-Show Build And Preflight Gates

Owned files:

- `services/preflight.py`
- `services/schedule_generation.py`
- `routes/scheduling/events.py`
- `routes/scheduling/flights.py`
- `routes/scheduling/preflight.py`
- `routes/scheduling/__init__.py` only where route-only orchestration is removed
- `tests/test_schedule_generation.py`
- `tests/test_one_click_and_fnf.py`
- `tests/test_workflow_e2e.py`
- focused route tests as needed

Work:

1. Split blocking codes into pre-generation input blockers and post-generation
   schedule blockers while preserving the aggregate `BLOCKING_CODES` contract.
2. Add a structured readiness exception/result carrying issue codes and concise
   repair details.
3. Move route-owned bulk heat orchestration into the schedule-generation
   service and return a structured summary for UI flashes and background jobs.
4. Route Run Show, Flights one-click, and asynchronous generation through that
   same service.
5. Run saw-block assignment before the service commits.
6. Roll back the whole build on heat, flight, relay, spillover, saw-block, or
   post-build readiness failure.
7. Make background jobs return failure semantics that the polling UI reports as
   failure, not a successful job with `ok: false` hidden in the payload.

Red evidence:

- `get_blocking_issues()` is not called by generation entry points.
- background generation omits `assign_saw_blocks()`.
- unresolved partnered entrants can be held back while the remaining show is
  committed.

Acceptance:

- All full-build entry points reject the same blockers before mutation.
- All entry points produce equivalent heats, flights, relay, spillover, and
  saw-block artifacts for the same fixture.
- A failure in any phase leaves the pre-request schedule unchanged.
- Completed/finalized history remains protected and unchanged.

### U3: Operator Surface And Durable Documentation

Owned files:

- `templates/scheduling/events.html`
- `templates/scheduling/preflight.html`
- `docs/DOMAIN_CONTRACT.md`
- `docs/HEAT_FLIGHT_AUDIT.md`
- `docs/RELEASE_CHECKLIST.md` only if commands or gates change
- focused presentation/route tests

Work:

1. Present Preflight before Generate in the workflow sequence.
2. Show a blocking state with a direct repair path and avoid success language
   when generation was rolled back.
3. Update audit documentation to remove the intentional forced-conflict and
   held-back-success behavior.
4. Document sync/background equivalence and saw-block completion.

Acceptance:

- Buttons and labels match real command behavior.
- Blocking messages fit mobile and desktop layouts and use existing design
  tokens/components.
- No public CDN or new frontend dependency is introduced.

## Verification

### Focused

- Heat generator unit and integration tests.
- Preflight, schedule-generation, one-click, workflow E2E, route, and operator
  presentation tests.
- Regression tests for rollback preserving prior heat/flight state.

### Repository Gates

- `ruff check .`
- Python 3.10 AST parse for changed Python files.
- standard `python -m pytest` with an isolated base temp; report explicitly that
  it excludes `proam_regression/`.
- `tests/test_postgres_runtime_smoke.py`
- `tests/test_pg_migration_safety.py`
- `tests/test_migration_integrity.py::TestMigrationIntegrity`
- affected disposable PostgreSQL unit tests when the local service is available;
  otherwise require the repository `unit-postgres` CI job before merge.
- `git diff --check`.

### Browser QA

- Migrated synthetic database only.
- Events page and Preflight page at desktop and mobile widths.
- Confirm blockers prevent generation, success appears only after a clean build,
  and no console error, overlap, horizontal overflow, or inaccessible action is
  introduced.

## Shipping Gate

Open a feature PR only after focused and standard local gates pass. Merge only
after all required GitHub checks pass on the exact head. Because `main`
auto-deploys to Railway, verify `/health`, one authenticated judge page, and one
public page after deploy. Do not use production credentials or mutate production
data during verification.
