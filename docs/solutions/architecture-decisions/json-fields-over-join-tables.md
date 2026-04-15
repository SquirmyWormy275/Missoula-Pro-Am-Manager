---
type: knowledge
problem_type: architecture-decision
severity: medium
tags:
  - "data-model"
  - "sqlalchemy"
  - "json"
confidence: high
created: 2026-04-15
source: "knowledge-seed from CLAUDE.md and git history"
---

# JSON fields for lists/dicts, repurposed for complex state

## Context
The app had to ship fast for an annual one-weekend event. Join tables for every many-to-many relationship would have multiplied migration burden and tangled the validation logic.

## Pattern
List/dict relationships are stored as JSON TEXT columns, not join tables:
- `competitors`, `stand_assignments` (on `Heat`)
- `events_entered`, `partners`, `gear_sharing`, `entry_fees`, `fees_paid` (on competitor models)
- `payouts` (on `Event`)

Complex state machines serialize themselves into `Event.payouts` rather than getting their own tables:
- `ProAmRelay` — team rosters + draw state
- `PartneredAxeThrow` — prelim/final state machine
- `BirlingBracket` — double-elim bracket tree

**Flexible competitor references** — `EventResult.competitor_id` and `Heat.competitors` are NOT SQLAlchemy FKs. A `competitor_type` string (`'college'` | `'pro'`) tells code which table to query. One Event/Heat/Result system serves both divisions without polymorphic inheritance.

## Rationale
- Single-event-per-year app; per-tournament rows are small (hundreds).
- Join tables would require migrations on every schema tweak for ergonomic fields.
- JSON keeps the model file small and the mental model flat.

Tradeoffs accepted:
- Can't query inside JSON easily (SQLite especially).
- Corrupt JSON in one row can break list views (mitigated by `JSONDecodeError` guards — see `json-decode-errors-from-corrupt-fields.md`).
- Two-representation drift risk (see `heat-competitors-vs-heatassignment-sync.md`).

## Examples
- `Event.payouts` holds competitor payouts for normal events AND full state for Relay/Axe/Birling — the consumer checks `Event.uses_payouts_for_state` to decide.
- Before adding a new join table, check whether a JSON dict on an existing row suffices.
