# Missoula Pro-Am regression harness

A pytest suite that reproduces all 18 confirmed bugs in `v2026.final`
(commit `343cb92`) against a byte copy of the real April 2026 production
database.

Every test in here asserts CORRECT behavior. So on unpatched code, **every
test fails**. That is the point. A fix is not a fix until the matching test
goes green, and the test cannot go green by accident, because it is driving
the actual 2026 data through the actual HTTP routes.

Current baseline: **23 failed, 3 passed, 135 seconds.**

---

## Why there are no fixtures

The 2026 failure was not "we had no tests." The repo shipped a large green
test suite and the show still had to be run on paper. The suite was green
because the fixtures and the mocks were written by the same reasoning that
wrote the bugs, so the test shape matched the bug shape. The repo documents
this in its own postmortem:

    docs/solutions/test-failures/test-shape-matches-bug-shape-trilogy-2026-04-23.md

The only defense that survives that failure mode is to stop inventing inputs.
So:

* No factories. No mocks. No `conftest` fixtures that build competitors.
* Every test gets a private clone of the production mirror, then upgrades that
  clone through this checkout's migration chain before exercising current code.
  Before migration, the clone receives the documented exact-name repair for
  historical era-1 references; the repair fails closed on any ambiguity. The
  read-only mirror itself is never migrated or written.
* Every action goes through a real route with a real HTTP request.
* Every assertion reads the database back. Nothing trusts a return value or a
  flash message.

Three tests in `test_sev1_race_day.py` are guard tests that PASS on unpatched
code. They exist to prove the harness is looking at the right rows, not to
prove the app works. If a guard test ever starts failing, the mirror changed
and the other assertions in that file are suspect.

## The invented-value ledger

Four features in the mirror ship with EMPTY state, all for the same reason:
the show was run on paper, so the app was never driven that far. Reaching the
bug requires building that state first. Every invented value in the whole
suite is listed here, and nothing else is invented:

| Where | What is invented | Why it cannot be read from production |
|---|---|---|
| item 17, item 4 | Partnered Axe hit counts | `events.event_state` for event 40 is empty. PAT was never played in the app. |
| item 5, item 14 | stopwatch times on real heats | every `event_results` row in the mirror is `status='pending'`. |
| item 7 | three relay total times | `record_total_time` was never called in production. |
| item 9 | four Pole Climb times and one purse | zero events in the mirror are finalized, so no payout row exists to read. |

The competitors, the pairings, the relay teams the lottery actually drew, the
heats the scheduler actually built, the gear free text the competitors
actually typed, and the payout template the operator actually saved are all
production rows.

## Coverage

All 18 items in `PROAM_2026_AUDIT_FINAL_BACKLOG.md`.

| Item | Test file | Tests |
|---|---|---|
| 1 Birling bracket hangs the worker | `test_sev1_race_day.py` | 2 |
| 2 Scratch resolves pro-first, hits the wrong person | `test_sev1_race_day.py` | 1 + 1 guard |
| 3 Scratch-undo does not restore heat membership | `test_sev1_race_day.py` | 1 |
| 4 Generic Finalize overwrites the Partnered Axe standings | `test_sev1_remaining.py` | 1 |
| 5 Partnered scoring loses pair identity | `test_sev1_race_day.py` | 2 |
| 6 Failed relay redraw destroys the drawn teams | `test_sev1_remaining.py` | 1 |
| 7 Public relay results page 500s on total times | `test_sev1_remaining.py` | 1 |
| 8 Audit attribution silently lost | `test_sev1_race_day.py` | 1 |
| 9 Bulk payout apply and clear leave money stale | `test_sev1_remaining.py` | 2 |
| 10 College day-of late entry refused | `test_sev1_race_day.py` | 1 + 2 guards |
| 11 Heat undo destroys partner_name | `test_sev2_confirmed.py` | 1 |
| 12 Read-only roles can write money | `test_sev2_confirmed.py` | 2 (parametrized) |
| 13 Pro-detail save deletes the relay fee | `test_sev2_confirmed.py` | 1 |
| 14 Partial dual-timer entry auto-finalizes short | `test_sev2_confirmed.py` | 1 |
| 15 Async flight-build status page 500s | `test_sev2_confirmed.py` | 1 |
| 16 Manual relay builder renders nameless cards | `test_sev2_confirmed.py` | 1 |
| 17 Partnered Axe advance-to-finals is not idempotent | `test_sev3_confirmed.py` | 1 |
| 18 Gear parser collapses multiple sharing partners | `test_sev3_confirmed.py` | 2 |

## Files

    rig.py                     clone / drop / orphan-reap the production mirror
    conftest.py                per-test clone, per-test app, sql and client fixtures
    flows.py                   multi-step preconditions built through real routes
    test_sev1_race_day.py      items 1, 2, 3, 5, 8, 10
    test_sev1_remaining.py     items 4, 6, 7, 9
    test_sev2_confirmed.py     items 11 through 16
    test_sev3_confirmed.py     items 17, 18

## Setup, once per machine

You need a PostgreSQL server, the app checked out, and the app's own venv.
Defaults are overridable by environment variable, see the constants at the top
of `rig.py`.

1. Create the role and the reference database.

       createuser proam --createdb --pwprompt
       createdb -O proam proam_prod_mirror

2. Load the production dump into it. This is the reference copy. Nothing ever
   writes to it; every test clones it.

       psql -h localhost -U proam -d proam_prod_mirror -f proam_production.sql

3. Point the rig at the app checkout if it is not at `/tmp/proam`.

       set PROAM_APP_ROOT=C:\path\to\Missoula-Pro-Am-Manager

The suite refuses to start if `proam_prod_mirror` is missing or holds no
`event_results` rows, and exits with code 3. That guard exists so a misconfigured
machine cannot quietly run the whole suite against an empty schema and report
something meaningless.

## Running it

    python -m pytest -q

By severity:

    python -m pytest -m sev1 -q
    python -m pytest -m "not slow" -q

One item:

    python -m pytest test_sev1_remaining.py -k payout -q

Keep the per-test clones for post-mortem inspection instead of dropping them:

    set PROAM_KEEP_CLONES=1

## Operational notes

**Postgres has to be up.** In a sandbox that suspends, the cluster dies with
it. On Linux, `pg_ctlcluster 16 main start`. Symptom is every test erroring in
the `dburl` fixture.

**Orphan clones get reaped automatically.** A killed run (Ctrl-C, CI timeout,
SIGKILL) never reaches the per-test `finally`, so `proam_rt_*` databases
survive. The session fixture drops them at startup and prints how many. If you
are using `PROAM_KEEP_CLONES=1`, note that the next run reaps them.

**Each test is fully isolated.** Its own database, its own `create_app()`.
Test order does not matter and a test that corrupts data cannot poison the
next one. That costs about a second per test in clone time and is worth it.

**Two tests turn off exception propagation.** `TESTING=True` re-raises app
exceptions into the test client, which would surface an opaque traceback
instead of the 500 the operator actually sees. Items 7 and 15 set
`PROPAGATE_EXCEPTIONS=False` so the assertion can read "returned 500."

**Nothing in this harness writes to the application checkout.** The suite is a
sibling directory. Adding it to the repo is a decision for whoever merges it.

## Reading a failure

Assertion messages are written to be read by someone who was not in the room.
They state what was observed, what was expected, and where the defect lives.
When a test starts passing, read the message it used to print and confirm the
fix addresses that mechanism rather than the symptom.

Watch for vacuous passes. Several tests carry explicit anti-vacuity guards,
for example "the manual team builder rendered no competitor cards at all, so
this test cannot tell blank cards from an empty pool." If you add a test,
ask whether it could pass on zero rows or on the wrong population, and if it
could, guard it.
