---
module: heat-generation
date: 2026-04-15
problem_type: best_practice
component: service_object
severity: high
tags:
  - "heat-generator"
  - "flight-builder"
  - "domain-rule"
---

# Cookie Stack and Standing Block share physical stands — mutual exclusion required

**Status: resolved.** `services/flight_builder.py` reserves an eight-heat
separation through `_CONFLICTING_STANDS` whenever another heat is available.
If only conflicting heats remain, they run sequentially because a flight has
one heat per slot. This note is retained as the rationale for that constraint,
not as an open heat-generator task.

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
- Heat generation is event-local; cross-event conflicts are enforced when
  flights are constructed and must remain covered by flight-builder tests.
