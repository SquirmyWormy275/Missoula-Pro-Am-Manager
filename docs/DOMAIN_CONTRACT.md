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

Allowed partner-event shapes:

- Jack and Jill: mixed-gender saw pair.
- Double Buck: same-event saw pair; gender rules come from event config.
- Partnered Axe Throw: managed by its dedicated prelim/final state machine, not
  standard heat generation.
- College partnered events: same enforcement standard as pro partnered events.

## Heat Generation

Heat generation owns event-level placement only. It must:

- Use active competitors who are entered in the event.
- Respect event-specific capacity.
- Use ability ordering before resource spreading when rankings exist.
- Keep reciprocal partner units together as one stand unit.
- Hold back invalid partnered entrants instead of placing them solo.
- Write `Heat.competitors`, stand assignments, and `HeatAssignment` rows in sync.

If code updates `Heat.competitors` directly, it must sync `HeatAssignment` before
the state is considered valid.

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

Every flight rebuild clears existing flight assignments. Therefore every rebuild
path must immediately rerun:

1. Pro-Am Relay final-flight placement.
2. College spillover integration.
3. Saw-block/field-prep recompute.

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
- Overflow is allowed only with an explicit operator warning.

## Preflight And Blocking

Preflight is a safety gate, not a substitute for service-layer validation.

Blocking for generation:

- Partnered-event blank, unresolved, self-reference, or nonreciprocal pairs.
- Invalid event capacity.
- Missing required event configuration.
- Heat/assignment sync corruption that would make printouts or scoring wrong.

Warning-only:

- Advisory schedule quality warnings when a valid but suboptimal schedule can
  still run.
- Left-handed springboard overflow that the field crew can consciously accept.

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
