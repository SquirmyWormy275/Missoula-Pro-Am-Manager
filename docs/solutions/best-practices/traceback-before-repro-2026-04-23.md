---
module: debugging/workflow
date: 2026-04-23
problem_type: best_practice
component: development_workflow
severity: high
applies_when:
  - "A production 500 / 502 / 503 has been reported and the cause is not yet known"
  - "Railway, Heroku, Fly.io, or any log-stream-accessible prod environment is running the affected code"
  - "A developer is tempted to write a local repro test before gathering prod evidence"
  - "The local test environment has different data shape or seed volume than production"
related_components:
  - testing_framework
  - tooling
tags:
  - debugging
  - production-incidents
  - railway-logs
  - repro-discipline
  - investigation-workflow
  - sharp-tool-use
---

# Pull the Production Traceback Before Writing a Local Repro

## Context

In the V2.14.5 hotfix session on 2026-04-23, a race-weekend operator reported a 500 when saving a payout configuration on prod. The natural reflex — and the one I followed — was to open the payout route code, reason about the save paths, and write a pytest that exercises each one against synthetic data.

I wrote 8 variants:
- GET payouts page
- POST `action=save` with empty payouts
- POST `action=save` with 3 positions
- POST `action=save_template` (per-event + tournament-manager variants)
- POST `action=apply_template` on a fresh event
- POST `action=apply_template` on a finalized event with results
- POST `action=bulk_apply` (tournament-manager)
- POST `action=clear_event`

Every single one returned 302. Zero 500s. Roughly 20 minutes of work.

Then I asked the user for the Railway log. The actual traceback showed:

```
File "/app/templates/scoring/tournament_payouts.html", line 330
  {% for pos, amt in tpl.get_payouts().items()|sort(attribute='0', key=int) %}
TypeError: do_sort() got an unexpected keyword argument 'key'
```

The bug was a Jinja template filter — triggered only when a `PayoutTemplate` row existed. Every one of my 8 local tests started from an empty `PayoutTemplate` table, so the outer `{% if templates %}` short-circuited and the bomb never rendered. The blind spot in my repro suite was **the exact same blind spot** that hid the bug in the existing smoke test for its entire lifetime. Local synthesis inherited the broken mental model and validated itself.

## Guidance

### Rule 1 — Traceback first, code second

When a production 500 is reported, **the first diagnostic action is to pull the prod traceback**. Not the second, not after forming a hypothesis, not after reading the route code — first. On this stack:

```bash
railway logs | tail -200              # newest-first recent output
railway logs --tail 500               # or a wider window if the error is older
```

If `railway` CLI isn't logged in or the sandbox denies the read, ask the user to run it in their terminal and paste ~40-60 lines. A screenshot of the Railway dashboard logs panel works too. The traceback includes the exact file, line number, and exception class — which collapses the investigation from "which of 8 paths is broken" to "line 330 of this one template is broken" in 30 seconds.

### Rule 2 — Synthesize a local repro *from* the traceback, not *toward* it

Once you have the traceback, the repro test almost writes itself:
- File and line → the rendering code path
- Exception class and message → the trigger condition
- Any data visible in the trace (tournament_id, event_id, user_id) → the prod state that triggered it

Writing a repro **before** you have the traceback is a form of begging the question: you're predicting the bug, and the synthetic paths you pick will be exactly the ones your (still-wrong) mental model expects. If your mental model were right, you wouldn't have shipped the bug.

### Rule 3 — Treat "tests pass locally" as anti-evidence when prod is broken

When every local test returns 200/302 but prod returns 500, **that is data**. It doesn't mean "the bug is intermittent" or "prod data is weird" — it means **your local test environment is missing the state that triggers the bug**. The gap between local and prod is where the bug lives. Find the gap (data volume, migration state, row count in a specific table, row content in a specific column) and the bug falls out.

The failure mode is believing the local suite is authoritative. A bug that ships to prod despite a green suite is by definition a bug the suite cannot see — continuing to add local tests of the same style is continuing to not-see it, louder.

## Why This Matters

**Time.** 20 minutes of local synthesis on a race-weekend incident is 20 minutes of judges staring at a 500 page. Pulling Railway logs first would have resolved V2.14.5 in under 5 minutes from symptom to PR.

**Blast radius.** The longer the bug lives on prod, the more users hit it. Payout config was broken for every user of every tournament with a saved template — a feature central to Saturday morning operations during a live race weekend.

**Sharp-tool culture.** Railway CLI (`railway logs`), Fly's `fly logs`, Heroku's `heroku logs --tail` — these exist precisely to collapse the local/prod gap. Using them first is a cheap habit that pays back every incident. The same principle generalizes to `/health` endpoints (see [feedback_dev_server_version_first](../../../../.claude/projects/c--Users-Alex-Kaper-Desktop-John-Ruffato-Startup-Challenge-Python-Missoula-Pro-Am/memory/feedback_dev_server_version_first.md) — curl the running process's version before debugging the algorithm).

**Meta-pattern connection.** This rule is the process-side complement to the [test-shape-matches-bug-shape trilogy](../test-failures/test-shape-matches-bug-shape-trilogy-2026-04-23.md). The trilogy says: "your test harness matched the buggy code's shape, so your tests passed." This rule says: "when prod disagrees with your tests, believe prod." Both point at the same blind spot — local synthesis can be a hall of mirrors — from different angles.

## When to Apply

- **Production 500 / 502 / 503 reported by a user or monitoring alert** — always pull logs first.
- **Any bug where local and prod behavior differ** — the gap is the bug; prod logs locate the gap.
- **Any bug where you find yourself writing a "should I test path A, B, or C?" repro matrix** — you don't know which path yet; the traceback tells you.
- **NOT applicable when:** the bug is fully reproducible locally (local is authoritative), or prod logs are genuinely unavailable (offline deploy target, no log retention, etc.). In the latter case, synthesize locally but stay paranoid — treat "passes locally" as weak evidence until prod confirms.

## Examples

### Before (V2.14.5 actual trajectory)

```
06:24  User reports 500 on payout manager (prod)
06:26  I read routes/scoring.py tournament_payout_manager route
06:28  Write local pytest for POST action=save
06:30  Test passes (302). Write POST action=save_template test.
06:32  Test passes (302). Write POST action=apply_template on finalized event.
06:34  Test passes (302). Write POST action=bulk_apply test.
06:36  Test passes (302). Write POST action=clear_event test.
06:38  All 8 synthetic paths return 302. Stuck.
06:40  Ask user for Railway logs.
06:42  User pastes traceback. Bug is Jinja |sort(key=int) at templates/scoring/tournament_payouts.html:330.
06:45  Write targeted regression test, seed 2 PayoutTemplate rows, watch it fail on the buggy template.
06:48  Fix the three templates, regression test passes.
06:52  Ship V2.14.5.
```

**Elapsed from symptom to fix: 28 minutes. ~20 of those minutes were wasted on synthetic local repro that couldn't see the bug.**

### After (what the trajectory would have been)

```
06:24  User reports 500 on payout manager (prod)
06:25  Ask user for Railway logs OR pull them myself
06:27  Traceback in hand — Jinja filter TypeError at specific file:line
06:29  Write targeted regression test (seed PayoutTemplate rows, assert 200)
06:31  Watch it fail on the buggy template
06:33  Fix the three templates
06:35  Ship V2.14.5.
```

**Projected elapsed: ~11 minutes. 17 minutes saved, no false-confidence detour through green-on-synthetic-paths territory.**

The local tests *are* still written — but they're written from the traceback toward a targeted seed condition, not fished for blindly across the POST action matrix.

## Related

- [../test-failures/test-shape-matches-bug-shape-trilogy-2026-04-23.md](../test-failures/test-shape-matches-bug-shape-trilogy-2026-04-23.md) — Meta-pattern: your test harness's shape matched the buggy code's shape, so local tests agreed with the bug. This rule is the process complement — when prod disagrees with local, prod wins.
- [jinja-sort-filter-has-no-key-kwarg-2026-04-23.md](jinja-sort-filter-has-no-key-kwarg-2026-04-23.md) — The specific surface bug from the V2.14.5 incident this process rule was extracted from.
- [railway-postgres-operational-playbook-2026-04-21.md](railway-postgres-operational-playbook-2026-04-21.md) — Railway ops reference including log-pull commands, `/health` diagnostics, and deploy verification rituals.
- [railway-ssh-base64-python-pattern-2026-04-22.md](railway-ssh-base64-python-pattern-2026-04-22.md) — Related Railway-sharp-tooling pattern (base64-encoded Python through remote pipeline for prod DB ops).
- Auto-memory `feedback_dev_server_version_first.md` — Same rule family for the dev-server case: curl `/health` before debugging the algorithm.

## Meta-Lesson for the Agent

The agent that hit this wall wrote a clean systematic-debugging investigation and still wasted 20 minutes because the skill definition says "form a hypothesis and test it" **before** it says "gather environment state." The real first step for any production incident is evidence collection from the affected environment, not hypothesis formation. Future versions of the investigate skill should surface `railway logs` / `fly logs` / `heroku logs` as a mandatory step 0 for any prod incident, ahead of code-reading.
