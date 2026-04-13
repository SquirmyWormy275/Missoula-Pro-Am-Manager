---
date: 2026-04-12
topic: race-day-integrity
---

# Race-Day Integrity

## Problem Frame

Missoula Pro-Am Manager (V2.9.0) handles happy-path tournament operations but fails under race-day chaos. A 5-area recon audit found two systemic gaps:

1. **Scratch cascade is missing.** Scratching a competitor is an isolated action. It does not propagate to their relay team (team races incomplete), their partner in partnered events (partner left with orphaned reference), or their Bull/Belle standings (completed EventResult rows stay in the SUM). Two separate scratch entry points exist (registration-level in `routes/registration.py` and heat-level in `routes/scoring.py`) and neither cascades.

2. **Relay prize money has zero code.** The spec says college competitors can "win money" from the relay. No payout amounts, distribution logic, or collection flow exists. Relay results live entirely in JSON state inside `Event.payouts`, which is also used by partnered axe throw and birling bracket for their own state. This dual-purpose field blocks payout UI for relay events.

**Who is affected:** Head organizer, judges, and competitors at a live timbersports tournament.

**Why it matters:** Corrupted standings mid-event erode trust in the entire system. Manual payout tracking defeats the purpose of the software.

## Requirements

**Scratch Cascade Service**
- R1. A unified `services/scratch_cascade.py` provides `compute_scratch_effects(competitor, tournament)` returning a list of typed, serializable effects (event_result, partner, relay_team, standings). Pure computation, no DB writes.
- R2. Both scratch entry points (`routes/registration.py` registration-level scratch and `routes/scoring.py` heat-level scratch) call the cascade service instead of performing inline status updates.
- R3. Cascade execution is atomic. All checked effects execute in a single `db.session.begin_nested()` savepoint. If any effect fails, the entire cascade rolls back.
- R4. The scratch route presents a preview modal listing every downstream effect with checkboxes (all checked by default). The judge can uncheck effects to opt out before confirming. On POST, effects are re-computed from fresh DB state and filtered to only checked items.
- R5. Each cascade execution is logged via the existing `services/audit.py::log_action()` infrastructure with timestamp, acting judge, competitor, and full effect list as JSON payload.
- R6. Tournament ID is verified on the scratch route (`competitor.tournament_id == tournament_id`; abort 403 on mismatch) to prevent cross-tournament IDOR.

**Scratch Undo**
- R7. After a cascade executes, a pre-scratch state snapshot is stored (in the audit log or a dedicated field). Within a configurable window (default 30 minutes), a judge can reverse the cascade and restore the competitor, their results, partner links, and relay team membership.

**Event State Field Separation**
- R8. A new `event_state` TEXT column (JSON) is added to the Event model. Relay team data, partnered axe throw state, and birling bracket state migrate from `Event.payouts` to `Event.event_state`. After migration, `Event.payouts` is freed for payout configuration on all event types.
- R9. `ProAmRelay._save_relay_data()` and `PartneredAxeThrow._save_state()` accept an optional `commit=False` parameter so they can participate in an outer transaction (the cascade service's savepoint). When called outside the cascade, they commit as before.

**Standings Integrity**
- R10. `tournament.py::_bull_belle_query()` filters to `Competitor.status='active'` so scratched competitors are excluded from Bull/Belle standings.
- R11. The college standings page displays a note listing unfinalized events whose results are included: "Includes results from N unfinalized events: [event names]."

**Re-finalization Visibility**
- R12. When a scratch cascade removes a result from a finalized event, that event's `is_finalized` is set to False.
- R13. Event cards display a yellow "Pending re-finalization" badge when `is_finalized` is False and the event has at least one completed result (i.e., it was previously finalized and then un-finalized by an edit or scratch).

**Relay Payouts**
- R14. Relay payouts are per-team lump sums. The organizer enters dollar amounts per placement (1st, 2nd, 3rd, etc.) on a relay-specific payout configuration page. Amounts are stored in `Event.payouts` JSON (now freed from relay state) as `{"1": 200.0, "2": 100.0}`.
- R15. Relay payouts do NOT use the `PayoutTemplate` model (that's for per-position individual payouts).

**Relay Team Health**
- R16. The relay dashboard displays a colored health indicator per team: green (all 8 members active), yellow (1-2 members scratched but team meets minimum: at least 3 active per division with at least 1M and 1F each), red (below minimum or gender/division balance broken; cannot race).
- R17. `replace_competitor()` re-validates team composition balance after replacement (division counts + gender balance).

**Payout Settlement**
- R18. A new `payout_settled` Boolean column (default False) on `EventResult` tracks whether each individual payout has been marked as paid. For relay, settlement is tracked per-team in `event_state` JSON as `team.payout_settled`.
- R19. The payout summary page displays "Mark as Paid" toggle buttons per competitor per event. A settlement report shows outstanding balances.

**Partner Reassignment**
- R20. When a partner is orphaned by a scratch cascade, they appear on a visible "needs partner" list on the event management page. Judges can assign a new partner from available competitors via a reassignment form.

**Race-Day Operations Dashboard (Capstone)**
- R21. A single page at `/tournament/<tid>/ops-dashboard` consolidates: live scratch feed (from audit log), relay team health overview, standings integrity monitor, payout status summary, and event finalization strip. Auto-refreshes every 30 seconds.

## Success Criteria

- A judge can scratch a competitor and see every downstream effect before confirming.
- Scratched competitors do not appear in Bull/Belle standings.
- Relay teams show clear health status and the organizer can configure/distribute payouts.
- The head organizer has a single dashboard showing all race-day operations state.
- All scratch cascades are audit-logged and reversible within the undo window.

## Scope Boundaries

- No offline-first or local-first architecture changes.
- No multi-tenant scratch or org-level audit.
- Scratch undo is time-windowed (30 min default), not unlimited history.
- Relay payouts are per-team lump sums; per-competitor splitting is not tracked in-system.
- Settlement tracking is binary (paid/unpaid); partial payments are not supported.

## Key Decisions

- **Atomic cascade with preview opt-out** over step-by-step: prevents partial cascade state. Judge gets control via checkboxes before execution, not during.
- **Generic event_state column** over relay-specific: all three state-overloading event types (relay, partnered axe, birling) migrate, eliminating the dual-purpose field entirely.
- **Both scratch paths wired**: registration-level scratch in `routes/registration.py` and heat-level scratch in `routes/scoring.py` both call `cascade_scratch()`.
- **Per-team lump sum relay payouts**: organizer enters amounts, distribution to individual members is offline.
- **Transaction composability fix**: `_save_relay_data()` and `_save_state()` gain `commit=False` param to participate in cascade savepoint.

## Dependencies / Assumptions

- Alembic migration adds `event_state` (Text, nullable) to events and `payout_settled` (Boolean, server_default='false') to event_results.
- Data migration copies existing `Event.payouts` JSON to `Event.event_state` for relay, partnered axe, and birling events, then clears `payouts` to `'{}'`. Migration is reversible.
- Railway `releaseCommand = "flask db upgrade"` handles automatic deployment.
- `FOR UPDATE` row locks are PostgreSQL-only; SQLite (dev/test) silently ignores them. Accepted.

## Outstanding Questions

### Deferred to Planning
- [Affects R7][Technical] What is the exact snapshot format for scratch undo? Audit log JSON payload vs. dedicated undo_snapshot column?
- [Affects R20][Needs research] Where should the partner reassignment form POST to? Existing registration edit route or new dedicated route?
- [Affects R21][Technical] Should the ops dashboard use server-sent events for live updates or polling with `setInterval`?

## Next Steps

-> `/ce:plan` for structured implementation planning
