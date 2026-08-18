# Missoula Pro-Am Domain Contract

This is the canonical operating contract for core tournament behavior. When this
file conflicts with older docs, this file wins and the older doc should be
updated or marked stale.

## Authority Order

1. `docs/DOMAIN_CONTRACT.md`
2. Executable workflow tests that cite this contract
3. `FlightLogic.md` for flight-builder algorithm detail not repeated here
4. Feature recon docs and `docs/solutions/**`
5. Historical chat notes and archived audits

## Core Workflow

The normal operator workflow is:

1. Import or enter competitors.
2. Configure events, Friday Night Feature, Saturday spillover, and flight sizing.
3. Run preflight and resolve blocking data problems.
4. Generate heats.
5. Build pro flights.
6. Place Pro-Am Relay in the final flight before college spillover.
7. Integrate Saturday college spillover.
8. Recompute dependent saw-block/field-prep state.
9. Score, publish standings, and settle payouts.

Any one-click or async workflow that claims to build the show must execute the
same sequence. Route shortcuts must not skip relay, spillover, or dependent
recompute steps.

## Partnered Events

Partnered events require a real pair. A competitor must not be placed solo in a
partnered heat by default.

A valid generated pair requires:

- Both competitors are active and entered in the same partnered event.
- Each side's partner name resolves to the other side.
- Self-references are invalid.
- Blank, unresolved, or nonreciprocal partner references are blocking data
  issues for heat generation.

Preflight should report partner problems, but heat generation must also enforce
them. Reporting a partner problem is not enough.

Preflight recovery may normalize reciprocal typo matches and pair only entrants
with neither an outbound declaration nor an inbound claim. Every named,
one-sided, ambiguous, self-referential, or gender-invalid declaration requires
an operator decision in the partner repair queue. Completed result records are
never rewritten during partner repair. Partner repair is locked once any result
is completed or the event is finalized; pairing declarations must not diverge
from scored history.

Allowed partner-event shapes:

- Jack and Jill: mixed-gender saw pair.
- Double Buck: same-event saw pair; gender rules come from event config.
- Partnered Axe Throw: managed by its dedicated prelim/final state machine, not
  standard heat generation. Its authoritative pairs live in `Event.event_state`;
  competitor partner fields are not a duplicate source of truth. Preflight must
  prove that every entered competitor appears in exactly one state-machine pair,
  every pair has a prelim score, and the finalists have been advanced before a
  show card is built.
- College partnered events: same enforcement standard as pro partnered events.

## Heat Generation

Heat generation owns event-level placement only. It must:

- Use active competitors who are entered in the event.
- Respect event-specific capacity.
- Use ability ordering before resource spreading when rankings exist.
- Keep reciprocal partner units together as one stand unit.
- Reject generation when a partnered entrant is invalid; do not publish a
  partial event or show with that entrant omitted or placed solo.
- Keep every competitor or partnered unit that shares the same declared gear in
  a different heat. Expand the heat count when needed rather than forcing a
  same-heat conflict.
- Treat `HeatAssignment` as the authoritative roster and stand-assignment store;
  use the `Heat` roster APIs rather than writing a second representation.
- For dual-run events, assign every entrant to a different physical stand or
  course in run 2, including a one-person heat.

Generation must prove that every eligible entrant can be placed without a
same-heat gear conflict before replacing an existing schedule. If event-specific
constraints make a valid placement impossible, it fails without mutating the
current heats. Direct event generation validates declarations relevant to the
event and its gear family; malformed declarations for unrelated events remain a
Preflight concern and do not block the current event.

## Flight Generation

Flights are a Saturday pro-show construct. Friday college heats do not enter
flights except selected Saturday spillover.

Flight sizing modes:

- Count mode: use the operator's saved `num_flights`.
- Minutes mode: compute `ceil(total_pro_run1_heats * minutes_per_heat /
  target_minutes_per_flight)` after the relevant heats exist.
- The builder may clamp unsafe counts to preserve minimum useful flight size.

One-click generation must not resolve minutes-mode flight count before fresh
heats are generated. A saved minutes-mode config is only meaningful against the
generated heat count.

Every flight rebuild clears existing flight assignments and is forbidden once
any flighted heat is completed. Before scoring begins, every rebuild path must
immediately rerun:

1. Pro-Am Relay final-flight placement.
2. College spillover integration.
3. Saw-block/field-prep recompute.

Manual flight edits may rearrange heats only when they preserve each event's
ascending heat-number sequence across the whole Saturday show. Reorder APIs
must reject an invalid order; an audit warning after the schedule has changed
is not sufficient.

## College Spillover

Saturday college spillover is not an independent schedule. It is integrated into
the Saturday pro flight sequence after pro flights are built.

Mandatory rules:

- Chokerman Run 2 is Saturday spillover and closes the show.
- Pro-Am Relay is placed before college spillover so mandatory closing events can
  still land after it when configured.
- Selected non-mandatory spillover events are distributed by the flight builder's
  spillover integration rules, not by ad hoc route code.

## Physical Stand Rules

Hand-saw events use the saw stand field as two reset groups.

Stock Saw rule:

- ALL Stock Saw — pro and college — runs on physical saw stands 7 and 8 only.
- Solo heats alternate 7, 8, 7, 8... so the off-stand can be set up while the
  on-stand runs. Pair heats use 7 + 8.
- Docs that say "Stock Saw stands 1-2" or that draw a pro/college distinction
  on stand numbers are stale.

Springboard rule:

- Left-handed springboard cutters use the configured left-handed dummy stand.
- The generator spreads left-handed cutters before the general fill.
- The generator adds heats until no heat needs the left-handed dummy more than
  once. It fails before replacement if the configured stand set cannot support
  that layout.

## Preflight And Blocking

Preflight is a safety gate, not a substitute for service-layer validation.

Every synchronous and background full-show build runs a fresh pre-generation
preflight while holding the schedule-writer lock. The pre-generation phase
checks configured inputs; it must not treat not-yet-generated flights, relay,
spillover, or saw blocks as input blockers. After generation, the same build
validates the completed show before its one final commit. A failure in any phase
rolls the entire request back.

Blocking for generation:

- Partnered-event blank, unresolved, self-reference, or nonreciprocal pairs.
- Partnered Axe state that has missing/duplicate entrants, incomplete prelims,
  or no confirmed finals bracket.
- Invalid, unknown, self-referential, or unmapped gear-sharing declarations.
- Invalid event capacity.
- Missing required event configuration.
- Any placement that would put competitors sharing declared gear in the same
  heat.
- Generated-show corruption that would make printouts or scoring wrong.

Completed Partnered Axe prelim rows are qualifier evidence, not immutable finals
history. A first finals-card build may replace the preliminary show heats. Once a
final score exists, a finals heat is completed, or the state reaches `completed`,
the finals card is protected like every other scored event.

Warning-only:

- Advisory schedule quality warnings when a valid but suboptimal schedule can
  still run.
- Left-handed springboard overflow that the field crew can consciously accept.

## Handicap Marks And Scoring

For handicap time events, every active entrant must have an explicitly
reviewed start mark before their heat can be scored. A zero-second mark is a
valid intentional scratch and must be recorded as reviewed; it must never be
assumed merely because a result row has the database default of `0.0`.

Preflight reports unreviewed handicap entrants. Heat scoring enforces the same
rule and redirects the judge to the mark-review page before any score is
written. Heat regeneration preserves scored heat history by refusing to
regenerate events with completed results, including bulk and Friday workflows.
Once a heat is completed, its roster cannot be moved, expanded, deleted, or
renumbered through heat-board scheduling controls. Its flight placement and
stand assignments are also historical record: manual reorder and saw-block
recompute must preserve them.

For events with `handicap_authority_mode = shadow`, STRATHMARK V2 output is a
separate recommendation-only record. The field is prepared and issued
atomically; per-row selection or applying the recommendation to
`EventResult.handicap_factor`, `predicted_time`, or `mark_assigned_at` is
forbidden. Missoula owns official scoring. STRATHMARK receives only stable
pseudonymous identities and eligible numeric settlement/void evidence through
the separately authenticated shadow contract.

Authoritative handicap numbers are deterministic. No LLM provider participates
in numeric prediction, mark optimization, shadow receipt authority, or official
scoring. Newer language models may assist engineering or optional reviewed
narrative work, but they must remain outside these race-day authority paths.

## Production Parity

Local SQLite success does not prove Railway/PostgreSQL behavior. Local Python
success does not prove production Python behavior.

Any deploy-gating validation must either run with production-shaped dependencies
or state the mismatch plainly. Tests that only prove SQLite/Python-local behavior
must not be described as production-ready evidence.

## Stale Or Contradictory Docs To Reconcile

- Older `FlightLogic.md` revisions and historical notes allowed unpaired
  partnered entrants to be placed solo. That rule is stale under this contract.
- Older requirements mention Stock Saw stands 1-2. Per operator decision
  (2026-04-27), ALL Stock Saw runs on stands 7-8. There is no pro/college
  stand distinction.
- Any route or doc that presents preflight as optional for blocking partner data
  is incomplete. Generation must enforce the same invariant.
