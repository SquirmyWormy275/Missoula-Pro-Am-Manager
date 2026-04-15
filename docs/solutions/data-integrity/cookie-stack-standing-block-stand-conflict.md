---
type: knowledge
problem_type: best-practice
severity: high
tags:
  - "heat-generator"
  - "flight-builder"
  - "domain-rule"
confidence: high
created: 2026-04-15
source: "knowledge-seed from CLAUDE.md and git history"
---

# Cookie Stack and Standing Block share physical stands — mutual exclusion required

## Context
These two events share the same 5 physical stands at the venue. Scheduling heats from both events simultaneously — or within the same flight slot — is physically impossible.

## Pattern
Any code touching heat generation or flight scheduling MUST enforce mutual exclusivity of `stand_type: cookie_stack` and `stand_type: standing_block`.

Flight builder enforces this via `_CONFLICTING_STANDS` in `services/flight_builder.py`. Heat generator is expected to respect it at heat-sheet rendering time (warning badges on heat sheets flag violations).

## Rationale
Domain rule from the physical venue. Not derivable from the code — must be hardcoded and defended.

## Examples
- `_CONFLICTING_STANDS = {('cookie_stack', 'standing_block'), ...}` in flight builder.
- Heat sheet templates render conflict warning badges when a flight contains both.
- Known gap: `heat_generator.py` does NOT currently enforce this — it's a documented open gap in CLAUDE.md Section 5. New heat-gen code must add the check.
