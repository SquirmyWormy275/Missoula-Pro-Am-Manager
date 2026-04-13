---
title: "feat: Race-Day Integrity — scratch cascade, relay payouts, operations dashboard"
type: feat
status: active
date: 2026-04-12
origin: docs/brainstorms/2026-04-12-race-day-integrity-requirements.md
---

# feat: Race-Day Integrity

## Overview

Add a unified scratch cascade service, relay payout infrastructure, payout settlement tracking, and a race-day operations dashboard. Separates the overloaded `Event.payouts` field into dedicated `event_state` + `payouts` columns. Wires both scratch entry points (registration + heat) through the cascade. Provides scratch undo within a 30-minute window.

## Problem Frame

The Missoula Pro-Am Manager handles happy-path tournament operations but fails under race-day chaos. Scratching a competitor does not cascade to relay teams, partners, or standings. Relay events have zero payout infrastructure. The `Event.payouts` field is overloaded by three event types storing state JSON. (see origin: `docs/brainstorms/2026-04-12-race-day-integrity-requirements.md`)

## Requirements Trace

- R1. Unified scratch cascade service with compute/execute phases
- R2. Both scratch entry points wired through cascade
- R3. Atomic cascade execution (single savepoint)
- R4. Preview modal with opt-out checkboxes
- R5. Scratch audit logging via log_action()
- R6. IDOR guard on scratch route (tournament_id check)
- R7. Scratch undo within 30-minute window
- R8. event_state column; relay/axe/birling migrate off Event.payouts
- R9. Transaction composability (_save_relay_data commit=False)
- R10. Standings filter Competitor.status='active'
- R11. Unfinalized event indicator on standings
- R12. Scratch sets is_finalized=False on affected events
- R13. Re-finalization warning badge on event cards
- R14. Per-team lump sum relay payouts
- R15. Relay payouts do not use PayoutTemplate
- R16. Relay team health indicator (green/yellow/red)
- R17. replace_competitor re-validates team balance
- R18. payout_settled column on EventResult; relay settlement in event_state
- R19. Mark as Paid toggles + settlement report
- R20. Partner reassignment queue
- R21. Race-Day Operations Dashboard at /tournament/<tid>/ops-dashboard

## Scope Boundaries

- No offline-first architecture
- No multi-tenant scratch or org-level audit
- Scratch undo is time-windowed (30 min), not unlimited
- Relay payouts are per-team lump sums; per-competitor splitting is offline
- Settlement is binary (paid/unpaid); no partial payments

### Deferred to Separate Tasks

- Partnered axe + birling migration to event_state (same PR, but could be split if needed)

## Context & Research

### Relevant Code and Patterns

- `services/scoring_engine.py` — idempotent calculate_positions(), savepoint pattern, _rebuild_individual_points()
- `services/proam_relay.py` — ProAmRelay class, _save_relay_data(), _load_relay_data(), replace_competitor()
- `services/partnered_axe.py` — PartneredAxeThrow class, _save_state()
- `services/audit.py` — log_action(action, **kwargs) stores JSON payload
- `routes/scoring.py` — heat-level scratch, finalize_event, enter_heat_results
- `routes/registration.py:229, :1289` — registration-level scratch (inline status update, no cascade)
- `models/event.py` — Event.payouts, Event.uses_payouts_for_state, EventResult
- `models/tournament.py` — _bull_belle_query(), get_bull_belle_with_tiebreak_data()
- `models/competitor.py` — ProCompetitor.partners, CollegeCompetitor.partners (JSON dicts)
- `routes/scheduling/heats.py:372` — heat-level scratch with partner warning flash

### Institutional Learnings

- Event.payouts is overloaded by 3 event types (relay, partnered axe, birling) — Codex cross-model finding
- ProAmRelay._save_relay_data() commits internally, breaking transaction composability — Codex finding
- Registration-level scratch and heat-level scratch are two separate code paths — Codex finding

## Key Technical Decisions

- **Atomic cascade with preview opt-out:** All checked effects in one savepoint. Preview modal with checkboxes for judge control. (see origin)
- **Generic event_state column:** All three overloading event types migrate, not just relay. (Codex tension #4)
- **Scratch undo via audit log snapshot:** log_action stores pre-scratch state as JSON. Undo queries most recent entry. No dedicated column.
- **Partner reassignment route:** New `POST /scheduling/<tid>/events/<eid>/reassign-partner`. Dedicated route, not registration edit.
- **Ops dashboard polling:** setInterval(30000), consistent with show_day 60s auto-refresh pattern. SSE deferred.

## Open Questions

### Resolved During Planning

- Undo snapshot format: audit log JSON with `scratch_snapshot` key containing full pre-scratch state
- Partner reassignment POST target: new dedicated route in scheduling blueprint
- Ops dashboard update mechanism: polling (30s interval)

### Deferred to Implementation

- Exact CSS class names for health indicator dots and re-finalization badge
- Partner reassignment form field layout (depends on available competitors query shape)
- Whether birling bracket state migration needs special handling for in-progress brackets

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification.*

```
SCRATCH CASCADE FLOW:

  Judge clicks "Scratch" on competitor
        │
        ▼
  GET /scratch-preview/<competitor_id>
        │
        ▼
  compute_scratch_effects(competitor, tournament)
  Returns: List[CascadeEffect]
    - type: event_result | partner | relay_team | standings
    - description: human-readable
    - affected_entity_id: int
        │
        ▼
  Render modal with checkboxes (all checked by default)
  Judge unchecks effects to skip, clicks "Confirm Scratch"
        │
        ▼
  POST /scratch-confirm/<competitor_id>
        │
        ▼
  1. Store pre-scratch snapshot in audit log
  2. Re-compute effects from fresh DB state
  3. Filter to only checked effect types/IDs
  4. Execute ALL in single begin_nested() savepoint:
     - Set Competitor.status = 'scratched'
     - Set EventResult.status = 'scratched' for affected results
     - Clear partner JSON on orphaned partners
     - Remove from relay team in event_state JSON
     - Rebuild standings via _rebuild_individual_points()
     - Set is_finalized = False on affected finalized events
  5. log_action('competitor_scratched', snapshot=..., effects=...)
  6. Commit outer transaction
        │
        ▼
  Redirect with flash: "Scratched [name]. N effects applied."
```

## Implementation Units

### Phase 1: Foundation (migration + field separation + transaction fix)

- [ ] **Unit 1: Migration — event_state column + payout_settled column**

**Goal:** Add the two new columns to the database schema.

**Requirements:** R8, R18

**Dependencies:** None

**Files:**
- Create: `migrations/versions/add_event_state_and_settlement.py`
- Modify: `models/event.py` (add event_state column to Event, payout_settled to EventResult)

**Approach:**
- `event_state` = Text, nullable=True on Event
- `payout_settled` = Boolean, server_default='false' on EventResult
- Data migration in upgrade(): for each Event where name in ('Pro-Am Relay', partnered axe names, birling names), copy payouts to event_state, set payouts to '{}'
- Downgrade reverses the copy

**Execution note:** Start with the migration. Verify with `flask db upgrade` on dev SQLite before proceeding.

**Patterns to follow:**
- Migration `a1b2c3d4e5f8` (add status_reason) for simple column addition pattern
- Migration `e9f0a1b2c3d4` (schema parity fix) for data migration within upgrade()

**Test scenarios:**
- Happy path: migration applies cleanly on empty DB, alembic_version updated
- Happy path: migration applies on DB with existing relay event, payouts copied to event_state, payouts cleared
- Edge case: relay event with malformed payouts JSON — migration logs warning, skips row
- Edge case: no relay events in DB — migration completes without data changes

**Verification:**
- `flask db upgrade` succeeds
- `flask db current` shows new HEAD revision
- Existing relay event has event_state populated and payouts cleared

- [ ] **Unit 2: Transaction composability — _save_relay_data and _save_state**

**Goal:** Allow relay and partnered axe services to participate in outer transactions.

**Requirements:** R9

**Dependencies:** Unit 1

**Files:**
- Modify: `services/proam_relay.py` (_save_relay_data gains commit=True param)
- Modify: `services/partnered_axe.py` (_save_state gains commit=True param)
- Modify: `services/proam_relay.py` (_load_relay_data reads from event_state)
- Test: `tests/test_transaction_composability.py`

**Approach:**
- Add `commit=True` default param to both save methods
- When commit=False, skip the db.session.commit() call
- Update _load_relay_data and _load_state to read from event_state column
- Update _save_relay_data and _save_state to write to event_state column
- All existing callers unchanged (they use the default commit=True)

**Patterns to follow:**
- `services/scoring_engine.py::_rebuild_individual_points()` — accepts session context from caller

**Test scenarios:**
- Happy path: _save_relay_data(commit=True) commits as before
- Happy path: _save_relay_data(commit=False) writes but does not commit; caller can rollback
- Happy path: _load_relay_data reads from event_state (not payouts)
- Integration: outer savepoint wraps _save_relay_data(commit=False); rollback restores prior state
- Edge case: event_state is None — _load_relay_data returns empty default structure

**Verification:**
- All existing relay tests still pass
- New transaction composability tests pass

### Phase 2: Scratch Cascade Service

- [ ] **Unit 3: compute_scratch_effects()**

**Goal:** Pure computation function that identifies all downstream effects of scratching a competitor.

**Requirements:** R1, R4

**Dependencies:** Unit 2

**Files:**
- Create: `services/scratch_cascade.py`
- Test: `tests/test_scratch_cascade.py`

**Approach:**
- Query all EventResult rows for the competitor with status in ('pending', 'completed')
- For each result: create an event_result effect (set status='scratched')
- For each partnered result: create a partner effect (clear partner JSON on the other competitor)
- Query relay teams via event_state JSON for competitor membership: create relay_team effect
- If any affected event is finalized: create standings effect (rebuild points)
- Return list of CascadeEffect dataclass instances (type, description, affected_entity_id)
- IDOR check: verify competitor.tournament_id matches (R6)

**Patterns to follow:**
- `services/scoring_engine.py::validate_finalization()` — pure computation, returns warnings

**Test scenarios:**
- Happy path: competitor in 1 event, returns 1 event_result effect + 1 standings effect
- Happy path: competitor with partner, returns event_result + partner effects
- Happy path: competitor on relay team, returns relay_team effect
- Happy path: competitor in 3 events + relay + partner, returns all effect types
- Edge case: competitor with no events — returns empty list
- Edge case: competitor already scratched — returns empty list
- Edge case: competitor.tournament_id mismatch — raises 403
- Edge case: partner already scratched — partner effect skipped (idempotent)

**Verification:**
- compute_scratch_effects returns correct effects for all competitor configurations

- [ ] **Unit 4: execute_cascade() + scratch undo snapshot**

**Goal:** Atomic execution of cascade effects with audit logging and undo snapshot.

**Requirements:** R3, R5, R7, R12

**Dependencies:** Unit 3

**Files:**
- Modify: `services/scratch_cascade.py` (add execute_cascade, reverse_cascade)
- Test: `tests/test_scratch_cascade.py` (extend)

**Approach:**
- execute_cascade(competitor, effects, judge_user_id):
  1. Build pre-scratch snapshot: {competitor_status, results: [{id, status, points}], partner_json, relay_membership}
  2. db.session.begin_nested() — single outer savepoint
  3. Set Competitor.status = 'scratched'
  4. For each effect, apply it (using with_for_update on PG for row locks)
  5. For affected finalized events: set is_finalized = False
  6. Call _rebuild_individual_points for affected college competitors
  7. log_action('competitor_scratched', competitor_id=, effects=, scratch_snapshot=)
  8. Commit savepoint
- reverse_cascade(competitor_id, judge_user_id):
  1. Find most recent 'competitor_scratched' audit entry within 30 min
  2. Restore from scratch_snapshot JSON
  3. log_action('scratch_undone', competitor_id=, restored_from=audit_entry_id)

**Patterns to follow:**
- `routes/scoring.py:582` — savepoint wrapping calculate_positions()
- `routes/scoring.py:746` — undo_heat_save with session-based undo token

**Test scenarios:**
- Happy path: execute_cascade sets competitor status, event result statuses, logs audit
- Happy path: reverse_cascade within 30 min restores all state
- Edge case: reverse_cascade after 30 min — returns error "Undo window expired"
- Edge case: execute_cascade with one effect failing — entire savepoint rolls back
- Edge case: execute_cascade with empty effects list — only sets competitor status
- Integration: execute + reverse round-trip leaves DB in original state
- Error path: DB lock conflict during execute — savepoint rolls back, returns conflict error
- Error path: no audit entry found for undo — returns "No scratch to undo"

**Verification:**
- Full cascade round-trip (scratch + undo) leaves DB in original state
- Audit log entries created for both scratch and undo

### Phase 3: Route Wiring + UI

- [ ] **Unit 5: Scratch cascade routes (both entry points)**

**Goal:** Wire both scratch paths through the cascade service with preview modal.

**Requirements:** R2, R4, R6

**Dependencies:** Unit 4

**Files:**
- Modify: `routes/scoring.py` (add scratch_preview GET, scratch_confirm POST)
- Modify: `routes/registration.py` (replace inline scratch with cascade call)
- Create: `templates/scoring/scratch_preview.html` (modal with checkboxes)
- Test: `tests/test_scratch_routes.py`

**Approach:**
- GET /tournament/<tid>/competitor/<cid>/scratch-preview: compute effects, render modal
- POST /tournament/<tid>/competitor/<cid>/scratch-confirm: execute cascade with checked effects
- POST /tournament/<tid>/competitor/<cid>/scratch-undo: reverse cascade
- Registration scratch routes: replace inline Competitor.status='scratched' with cascade_scratch() call
- Modal template: Bootstrap 5 modal, checkbox per effect, "Confirm Scratch" / "Cancel" buttons

**Patterns to follow:**
- `routes/scoring.py::finalize_preview` + `finalize_event` — preview GET + confirm POST pattern
- `templates/scoring/enter_heat.html` — Bootstrap 5 modal pattern with CSRF token

**Test scenarios:**
- Happy path: GET preview returns effect list as JSON
- Happy path: POST confirm with all effects checked — cascade executes, redirects with flash
- Happy path: POST confirm with some effects unchecked — only checked effects execute
- Happy path: POST undo within window — restores state, redirects with flash
- Edge case: POST confirm with no effects checked — no cascade, flash "No changes applied"
- Error path: POST confirm on wrong tournament — 403
- Error path: POST undo after window — flash "Undo window expired"
- Integration: registration scratch path calls cascade instead of inline update

**Verification:**
- Both scratch paths produce the same cascade behavior
- Preview modal renders with correct effects for test competitor

- [ ] **Unit 6: Standings integrity fixes**

**Goal:** Filter scratched competitors from standings and show unfinalized event indicator.

**Requirements:** R10, R11

**Dependencies:** Unit 4

**Files:**
- Modify: `models/tournament.py` (_bull_belle_query adds Competitor.status='active' filter)
- Modify: `templates/reports/college_standings.html` (unfinalized event note)
- Test: `tests/test_bull_belle_tiebreak.py` (extend with scratched competitor case)

**Approach:**
- Add `.filter(CollegeCompetitor.status == 'active')` to _bull_belle_query join
- Add template logic: query Event where tournament_id=tid AND is_finalized=False AND has completed results, list names
- Render note: "Includes results from N unfinalized events: [names]"

**Patterns to follow:**
- Existing `_bull_belle_query` filter chain
- `templates/reports/college_standings.html` existing layout

**Test scenarios:**
- Happy path: scratched competitor excluded from standings
- Happy path: active competitor with points appears in standings
- Happy path: unfinalized event listed in indicator note
- Edge case: all events finalized — no indicator shown
- Edge case: competitor scratched after finalization — excluded after standings recalc

**Verification:**
- Bull/Belle query returns only active competitors
- Standings page shows unfinalized event note when applicable

- [ ] **Unit 7: Re-finalization warning badge**

**Goal:** Show visual indicator when a finalized event has been un-finalized.

**Requirements:** R12, R13

**Dependencies:** Unit 4

**Files:**
- Modify: event card templates (various — `templates/scoring/event_results.html`, `templates/scoring/tournament_events.html`)
- Test: `tests/test_routes_smoke.py` (extend)

**Approach:**
- Jinja conditional: if event.status == 'in_progress' and event has any completed results and not event.is_finalized → show yellow badge "Pending re-finalization"
- CSS class: `badge bg-warning text-dark`

**Test scenarios:**
- Happy path: finalized event shows no badge
- Happy path: un-finalized event with completed results shows badge
- Edge case: new event with no results — no badge
- Edge case: in-progress event that was never finalized — no badge (no completed results)

**Verification:**
- Badge appears only on events that were previously finalized and then un-finalized

### Phase 4: Relay Features

- [ ] **Unit 8: Relay payout configuration**

**Goal:** Allow organizer to configure per-team lump sum payouts for relay.

**Requirements:** R14, R15

**Dependencies:** Unit 1

**Files:**
- Modify: `routes/proam_relay.py` (add relay_payouts GET/POST route)
- Create: `templates/proam_relay/configure_payouts.html`
- Test: `tests/test_proam_relay.py` (extend)

**Approach:**
- GET /proam-relay/<tid>/payouts: render form with position 1-N amount fields
- POST: validate amounts (max(0, float)), save to Event.payouts as {"1": amount, ...}
- Event.uses_payouts_for_state updated to check event_state instead of payouts (since state moved)

**Patterns to follow:**
- `routes/scoring.py::configure_payouts` — existing payout form pattern
- `templates/scoring/configure_payouts.html` — form layout

**Test scenarios:**
- Happy path: configure 3 placement amounts, saved to Event.payouts
- Happy path: load existing payouts into form fields
- Edge case: negative amount — clamped to 0
- Edge case: non-numeric input — flash error, re-render form
- Edge case: no relay event exists — 404

**Verification:**
- Relay event has payout amounts in Event.payouts after configuration

- [ ] **Unit 9: Relay team health indicator**

**Goal:** Show green/yellow/red health dots on relay dashboard.

**Requirements:** R16, R17

**Dependencies:** Unit 2

**Files:**
- Modify: `services/proam_relay.py` (add compute_team_health())
- Modify: `templates/proam_relay/dashboard.html` (health dots)
- Modify: `services/proam_relay.py` (replace_competitor re-validates balance)
- Test: `tests/test_proam_relay.py` (extend)

**Approach:**
- compute_team_health(team, tournament) checks each member's Competitor.status
- Green: all 8 active. Yellow: 1-2 inactive but >=3 active per division with >=1M+1F each. Red: below threshold.
- replace_competitor: after replacement, call compute_team_health to verify team stays valid
- Template: colored circle span with tooltip showing status detail

**Patterns to follow:**
- Existing relay dashboard team cards layout

**Test scenarios:**
- Happy path: full roster — green
- Happy path: 1 scratched member — yellow (if balance ok)
- Happy path: 3 scratched from same division — red
- Edge case: 0 college females active — red
- Integration: scratch cascade removes member, health updates to yellow/red

**Verification:**
- Health dots accurately reflect team member status

### Phase 5: Settlement + Partner Queue

- [ ] **Unit 10: Payout settlement flow**

**Goal:** Mark as Paid toggles and settlement report.

**Requirements:** R18, R19

**Dependencies:** Unit 1

**Files:**
- Modify: `routes/scoring.py` (add toggle_settlement POST route)
- Modify: `templates/reports/payout_summary.html` (add toggle buttons, outstanding totals)
- Test: `tests/test_settlement.py`

**Approach:**
- POST /tournament/<tid>/result/<rid>/toggle-settled: flip payout_settled boolean
- Template: per-row toggle button (green check if settled, gray if not)
- Settlement summary card: total purse, total settled, outstanding balance
- Relay settlement: separate toggle stored in event_state team JSON

**Patterns to follow:**
- Existing payout_summary.html table layout
- `routes/scoring.py::configure_payouts` POST pattern

**Test scenarios:**
- Happy path: toggle unsettled → settled, button turns green
- Happy path: toggle settled → unsettled
- Happy path: settlement summary shows correct totals
- Edge case: toggle on result with 0 payout — no-op (nothing to settle)
- Error path: toggle on wrong tournament — 403

**Verification:**
- Settlement toggles persist across page reloads
- Summary totals match sum of individual payouts

- [ ] **Unit 11: Partner reassignment queue**

**Goal:** Visible orphaned partner list with reassignment form.

**Requirements:** R20

**Dependencies:** Unit 4

**Files:**
- Create: `templates/scheduling/partner_queue.html`
- Modify: `routes/scheduling/events.py` (add partner_queue GET, reassign_partner POST)
- Test: `tests/test_partner_reassignment.py`

**Approach:**
- GET /scheduling/<tid>/events/<eid>/partner-queue: query competitors with partner_name pointing to a scratched competitor
- Render list: "[Name] — partner scratched, needs reassignment" with dropdown of available competitors
- POST /scheduling/<tid>/events/<eid>/reassign-partner: update partner JSON bidirectionally
- Validate: new partner must match gender requirement (Event.partner_gender_requirement)

**Patterns to follow:**
- `services/partner_matching.py::_set_partner_bidirectional()` for partner update logic
- `templates/scheduling/assign_marks.html` for form layout in scheduling blueprint

**Test scenarios:**
- Happy path: orphaned partner appears in queue after cascade
- Happy path: reassignment updates both competitors' partner JSON
- Edge case: no orphaned partners — "No orphaned partners" message
- Edge case: reassign to wrong gender — flash error (mixed-gender event)
- Edge case: reassign to competitor already partnered — flash error

**Verification:**
- Queue shows correct orphaned partners after a scratch cascade
- Reassignment updates both sides of the partner relationship

### Phase 6: Capstone

- [ ] **Unit 12: Race-Day Operations Dashboard**

**Goal:** Single-page mission control for the head organizer.

**Requirements:** R21

**Dependencies:** Units 4, 6, 7, 9, 10

**Files:**
- Create: `templates/ops_dashboard.html`
- Modify: `routes/main.py` or create `routes/ops_dashboard.py` (new route)
- Test: `tests/test_routes_smoke.py` (extend)

**Approach:**
- Route: GET /tournament/<tid>/ops-dashboard
- Template sections:
  1. Live scratch feed: query last 20 audit entries where action='competitor_scratched', display timestamp + description
  2. Relay team health: call compute_team_health() per team, render dots
  3. Standings integrity: count unfinalized events with completed results, count scratched competitors with completed results
  4. Payout status: sum payout_amount, sum where payout_settled=True, compute outstanding
  5. Event strip: all events with status + is_finalized badge
- Auto-refresh: meta refresh tag or JS setInterval(30000)
- Requires is_judge role (management blueprint)

**Patterns to follow:**
- `routes/scheduling/show_day.py` — existing dashboard with 60s auto-refresh
- `templates/scheduling/show_day.html` — card-based layout

**Test scenarios:**
- Happy path: dashboard loads with all 5 sections populated
- Happy path: auto-refresh reloads data
- Edge case: no audit entries — "No recent scratches" message
- Edge case: no relay event — relay section hidden
- Edge case: no payouts configured — payout section shows "$0"

**Verification:**
- Dashboard shows live data matching individual pages
- All 5 sections render without errors

## System-Wide Impact

- **Interaction graph:** Scratch cascade touches: scoring routes, registration routes, relay service, partner matching, standings query, audit service. All through the cascade service (single entry point).
- **Error propagation:** Cascade failures roll back the entire savepoint. Individual route errors flash to judge and redirect. No silent failures.
- **State lifecycle risks:** Event.payouts → event_state migration is the biggest risk. Reversible downgrade mitigates. Partial migration (some events migrated, others not) prevented by migration running in a single transaction.
- **API surface parity:** API v1 endpoints (GET-only) unaffected. No new API endpoints.
- **Integration coverage:** Scratch cascade + standings recalc is the critical cross-layer scenario. Must test: scratch → standings update → re-finalization flag → ops dashboard display.
- **Unchanged invariants:** Scoring engine calculate_positions() unchanged. Heat generation unchanged. Gear sharing manager unchanged.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Data migration corrupts relay state | Reversible downgrade. Test on dev DB copy first. Skip malformed rows with logging. |
| Scratch cascade partial failure | Single savepoint ensures atomicity. Entire cascade rolls back on any failure. |
| FOR UPDATE locks not tested in SQLite dev | Acceptable. Concurrency protection matters only in prod PG. |
| Ops dashboard performance with large audit log | Limit query to last 20 entries. Add index on audit_log.action if slow. |
| Settlement tracking adds column to high-traffic table | payout_settled is a simple boolean with server_default. No table lock on PG. |

## Documentation / Operational Notes

- Update CLAUDE.md with event_state column, cascade service, settlement tracking
- Update DEVELOPMENT.md changelog with V2.10.0 entry
- Railway: migration runs automatically via releaseCommand

## Sources & References

- **Origin document:** [docs/brainstorms/2026-04-12-race-day-integrity-requirements.md](docs/brainstorms/2026-04-12-race-day-integrity-requirements.md)
- **CEO plan:** [docs/designs/race-day-integrity.md](docs/designs/race-day-integrity.md)
- Related code: `services/scoring_engine.py`, `services/proam_relay.py`, `services/audit.py`
- Related recon: 5-area race-day audit (2026-04-12)
