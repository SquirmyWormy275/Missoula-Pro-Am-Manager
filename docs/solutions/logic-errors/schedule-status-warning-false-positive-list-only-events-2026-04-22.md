---
title: Run Show warning banner falsely flagged signup-only and state-machine events as missing heats
date: 2026-04-22
category: logic-errors
module: services/schedule_status
problem_type: logic_error
component: service_object
severity: high
symptoms:
  - "Run Show page (/scheduling/<tid>/events) flashed '6 college event(s) have no heats yet: Men's Axe Throw, Women's Axe Throw, Peavey Log Roll, Men's Caber Toss, Women's Caber Toss, Pulp Toss' on every page load, even after Generate All Heats + Build Flights ran cleanly (174 heats + 7 flights built on T2)"
  - "Same page also flashed '1 pro event(s) have no heats yet: Partnered Axe Throw' (and Pro-Am Relay on tournaments where it had been configured) despite both being state-machine events that legitimately produce no Heat rows pre-build"
  - "Race-weekend operator believed Generate All Heats was broken, lost trust in the green-path indicator one day before show"
  - "Run Show page Generate buttons had no num_flights input — sizing only configurable on the separate /flights/build page, so the action used the persisted default with no override path"
root_cause: logic_error
resolution_type: code_fix
related_components:
  - rails_controller
  - rails_view
tags:
  - schedule-status
  - false-positive
  - list-only-events
  - state-machine-events
  - flight-sizing
  - run-show-page
  - race-weekend
  - v2-14-1
  - operator-trust
---

# Run Show warning banner falsely flagged signup-only and state-machine events as missing heats

## Problem

The Run Show page warning banner showed false positives for college signup-only events (Axe Throw, Caber Toss, Peavey Log Roll, Pulp Toss) and pro state-machine events (Partnered Axe Throw, Pro-Am Relay), making race-weekend operators think heat generation was broken when it had actually succeeded. Bundled into the same fix: the flight-count input only existed on `/flights/build`, leaving the Run Show page with no way to override `num_flights` inline.

## Symptoms

After clicking Generate All Heats + Build Flights on `/scheduling/<tid>/events`, heats and flights built successfully, but the Current Schedule warning panel rendered two false-positive warnings on every page load:

- "6 college event(s) have no heats yet: Men's Axe Throw, Women's Axe Throw, Peavey Log Roll, Men's Caber Toss, Women's Caber Toss, Pulp Toss"
- "1 pro event(s) have no heats yet: Partnered Axe Throw"

The warnings persisted regardless of how many times the operator regenerated heats. The events flagged never produce regular `Heat` rows by design:

- Axe Throw, Caber Toss, Peavey Log Roll, Pulp Toss are college come-and-go signup-only events (enumerated in `config.LIST_ONLY_EVENT_NAMES`).
- Partnered Axe Throw runs a prelims to finals state machine stored in `Event.payouts` JSON.
- Pro-Am Relay synthesises a single pseudo-Heat at flight-build time via `integrate_proam_relay_into_final_flight`.

## What Didn't Work

1. **Running the full test suite.** All 298 targeted tests passed. The existing `test_open_college_event_without_heats_not_warned` covered only the `is_open=True` path, leaving the `is_open=False` plus name-in-LIST case untested. Tests gave false confidence.

2. **Killing the dev server with `pkill`.** The bash-wrapped command targeted the wrong process and the old Flask process survived on port 5055. Had to use `taskkill /F /IM python.exe` and restart on a fresh port to confirm the fix served the new code (per `feedback_dev_server_version_first.md`, this is the standing-order first diagnostic).

3. **Assuming V2.14.0 regressed something.** It did not. The false positive has lived in `services/schedule_status.py` since the Run Show panel was added in PR #64 (V2.12.1, 2026-04-22 02:42 UTC). V2.14.0 ship day just made it visible because users finally clicked Generate All Heats and saw the misleading banner.

4. **(session history)** The original `_is_open_list_only` helper was author-time wrong, not regression-broken. Skeleton trace from the PR #64 session (`5252ba10`) shows ~15 minutes from "let me look at what each page actually shows" to commit. The fragment-vs-flag distinction (`event.is_open` vs the `LIST_ONLY_EVENT_NAMES` name set already maintained elsewhere in the codebase) was never raised in the design conversation; the model just picked `is_open` and shipped. State-machine pro events (Partnered Axe Throw, Pro-Am Relay) were never mentioned in that session's planning at all.

5. **(session history)** A parallel Claude Code session (`8fa4f1a6`) on 2026-04-23 detected the EXACT 4 in-progress files of this fix as untracked work and correctly refused to touch them per `feedback_never_stash_unknown_changes.md`. User confirmed at 04:15 UTC: "Leave the other files. Its probably someone elses parallel work." Cross-session coordination via the working tree worked exactly as the feedback memory prescribes.

## Solution

### File 1: `services/schedule_status.py`

The aggregator was reading `event.is_open`, the OPEN/CLOSED registration toggle from the Event Setup page, instead of name-based classification. CLAUDE.md §3 explicitly allows operators to configure traditionally-OPEN events as CLOSED, so the `is_open` signal is not a reliable proxy for "never produces heats."

```python
import re
from config import LIST_ONLY_EVENT_NAMES

_STATE_MACHINE_PRO_NAMES = {"partneredaxethrow", "proamrelay"}

# Warning aggregator now excludes the correct event classes:
college_missing = [
    e for e in college_events
    if not heats_by_event.get(e.id) and not _is_signup_only_college(e)
]
pro_missing = [
    e for e in pro_events
    if not heats_by_event.get(e.id) and not _is_state_machine_pro(e)
]

# New helpers (added below _is_open_list_only, which is retained for existing callers):
def _is_signup_only_college(event: Event) -> bool:
    """College events that never produce heats — Axe Throw, Caber Toss,
    Peavey Log Roll, Pulp Toss. Run come-and-go signup-list format
    no matter how the operator toggled OPEN/CLOSED on the setup page.
    """
    if event.event_type != "college":
        return False
    if bool(getattr(event, "is_open", False)):
        return True
    normalized = re.sub(r"[^a-z0-9]+", "", str(event.name or "").lower())
    return normalized in LIST_ONLY_EVENT_NAMES

def _is_state_machine_pro(event: Event) -> bool:
    """Pro events whose progression is stored in Event.payouts JSON, not
    Heat rows. Partnered Axe Throw runs prelims to finals via state machine;
    Pro-Am Relay synthesises a single pseudo-Heat at flight-build time.
    """
    if event.event_type != "pro":
        return False
    normalized = re.sub(r"[^a-z0-9]+", "", str(event.name or "").lower())
    return normalized in _STATE_MACHINE_PRO_NAMES
```

### Files 2-4: Inline flight sizing on the Run Show page

The flight-count input only existed on `/flights/build`. Operators had to leave the Run Show page to retune flight count after seeing build results. Added the controls inline.

- `routes/scheduling/__init__.py::_build_pro_flights_if_possible` now accepts a `num_flights` kwarg and threads it into `build_pro_flights`.
- `routes/scheduling/events.py` adds `_resolve_num_flights_from_form()`, which reuses the same `_read_flight_sizing_config` and `_persist_flight_sizing_config` helpers as `/flights/build`. Both pages write the same `schedule_config` keys (`flight_sizing_mode`, `target_minutes_per_flight`, `minutes_per_heat`, `num_flights`), so the choice round-trips between pages with no drift.
- `templates/scheduling/events.html` renders a 3-field form (num_flights, target_minutes_per_flight, minutes_per_heat) with a count/minutes mode radio, pre-filled from persisted config.

## Why This Works

**Classification source of truth.** The heat generator already treats list-only-by-name as authoritative via `routes/scheduling/__init__.py::_is_list_only_event`. The status aggregator was using a different and weaker signal (`is_open`). Aligning the aggregator with the generator produces a single source of truth that cannot drift — both surfaces now agree on which events "never produce heats."

**(session history)** Codex review session `019dae51` (2026-04-21) had `LIST_ONLY_EVENT_NAMES` in working memory while critiquing a different plan. The Claude Code session that wrote `_is_open_list_only` 21 hours later did not consult it. Cross-tool blind spot worth noting: a different reviewer already knew the right answer, but reviewers don't see each other's contexts. The fix here is to make the codebase itself surface the authoritative list (now imported into `schedule_status.py`) instead of relying on any single reviewer remembering it.

**Config reuse.** The flight sizing choice was already persisted in `Tournament.schedule_config` by `/flights/build` (V2.14.0 Phase 3). The Run Show form now reads and writes the same keys via the same helper functions — no new table, no new column, no new migration. Two forms writing the same semantic value through one helper is how you prevent page drift.

## Prevention

### 1. Name-based classification is authoritative

Anywhere code decides whether an event should produce heats (heat generator, scheduling UI, status aggregator, Woodboss material planning, print catalog), use `LIST_ONLY_EVENT_NAMES` or `_is_list_only_event()`, never `is_open`. Consider renaming `_is_open_list_only` to `_is_open_event` to eliminate future confusion at the call site.

### 2. Regression test gap to close

```python
class TestListOnlyNamedCollegeEvents:
    def test_closed_signup_only_event_not_warned(self, app, tournament):
        """is_open=False + name in LIST_ONLY_EVENT_NAMES should produce no warning."""
        with app.app_context():
            ev = Event(tournament_id=tournament.id, name="Axe Throw",
                       event_type="college", gender="M",
                       scoring_type="hits", stand_type="axe_throw",
                       is_open=False)  # the previously-broken path
            db.session.add(ev); db.session.commit()
        with app.test_request_context("/"):
            s = build_schedule_status(tournament)
        assert not any("no heats" in w["title"] for w in s["warnings"])

class TestStateMachineProEventsSkipped:
    @pytest.mark.parametrize("name", ["Partnered Axe Throw", "Pro-Am Relay"])
    def test_state_machine_pro_event_not_warned(self, app, tournament, name):
        with app.app_context():
            ev = Event(tournament_id=tournament.id, name=name,
                       event_type="pro", scoring_type="hits", is_open=False)
            db.session.add(ev); db.session.commit()
        with app.test_request_context("/"):
            s = build_schedule_status(tournament)
        assert not any("pro" in w["title"] and "no heats" in w["title"]
                       for w in s["warnings"])
```

### 3. Meta-lesson — third release in the same blind-spot trilogy

Same class of test gap three releases in a row:

- **V2.13.0** shipped a `background_jobs.submit` mock whose signature matched the buggy call, hiding the prod bug. See [mock-signature-matches-buggy-call-site-2026-04-22](../test-failures/mock-signature-matches-buggy-call-site-2026-04-22.md).
- **V2.14.0** shipped a relay template test whose fixture shape matched what the template expected, not what the service emits. See [hand-written-fixture-shape-divergence-2026-04-22](../test-failures/hand-written-fixture-shape-divergence-2026-04-22.md).
- **V2.14.1** (this fix) shipped a warning-aggregator helper whose test covered only the path that already worked (`is_open=True`). The other path (`is_open=False` + name-in-LIST) was the one operators actually hit on race-weekend minus one day.

The pattern to break: **for every conditional or filter helper, the unit test matrix must include at least one negative path where the helper returns `False` and the production caller must still be correct.** A test that only covers the suppressing branch gives full coverage of nothing. This generalises `feedback_mock_signature_matches_bug.md` and `feedback_memory_describes_intent.md` — MEMORY.md text describes intent at commit time; code review and adversarial tests are the only verification.

### 4. Config reuse pattern

When the same user choice is relevant on two pages, persist it in `Tournament.schedule_config` (JSON bag) and have both pages read and write through the same helper module (e.g., `_read_flight_sizing_config` and `_persist_flight_sizing_config`). Two independent forms writing the same semantic value is how pages drift apart; one helper called twice keeps them aligned with no schema change. The Run Show flight-sizing form is the third consumer of `schedule_config` after `/flights/build` (V2.14.0 PR #70) and `saturday_college_placement_mode` (V2.14.0 PR #69) — the precedent is now cemented.

### 5. (session history) The panel was supposed to prevent this exact UX failure

The Current Schedule status panel was built in PR #64 specifically to reduce post-Build-Flights round-trips when the operator was clearing prod DB and re-entering data ("OK WAIT. There has GOT to be a better way to do this"). The false-positive banner this fix repairs was failing the exact UX it was created to deliver, exactly as the operator started doing serious data entry. When you ship a trust signal, the test matrix has to cover the "no false alarms while everything is actually fine" case, not just the "alarms when something is broken" case. The first path is the one that destroys trust silently.

## Related

- [flight-builder-per-event-stacking-2026-04-21](./flight-builder-per-event-stacking-2026-04-21.md) — sibling logic-error in flight builder; both broke operator trust in the same week.
- [rebuild-flights-orphans-saturday-spillover-2026-04-21](../integration-issues/rebuild-flights-orphans-saturday-spillover-2026-04-21.md) — chained-operations bug that motivated the V2.14.0 atomic-commit pattern reused by `_resolve_num_flights_from_form`.
- [preflight-gear-sharing-using-prefix-false-positives-2026-04-21](../data-integrity/preflight-gear-sharing-using-prefix-false-positives-2026-04-21.md) — same class of "wolf-cried" false-positive UX that erodes operator confidence.
- [mock-signature-matches-buggy-call-site-2026-04-22](../test-failures/mock-signature-matches-buggy-call-site-2026-04-22.md) — first instance of the test-blind-spot trilogy this fix completes.
- [hand-written-fixture-shape-divergence-2026-04-22](../test-failures/hand-written-fixture-shape-divergence-2026-04-22.md) — second instance of the same trilogy.
- [ability-sort-before-resource-spread-2026-04-22](../best-practices/ability-sort-before-resource-spread-2026-04-22.md) — adjacent V2.14.0 follow-up doc; both fixes refine the heat/flight pipeline.
- [sequential-ship-pattern-parallel-claude-sessions-2026-04-23](../best-practices/sequential-ship-pattern-parallel-claude-sessions-2026-04-23.md) — the multi-session workflow context for this fix. THIS V2.14.1 fix is the sibling session's "claim the V2.14.2 slot" work that the sequential-ship doc uses as its case study. The `tests/test_schedule_status.py:108,137` "V2.14.2 regression" docstrings were the public commitment markers that resolved the version-slot collision toward Option C.
