---
module: testing
date: 2026-04-23
problem_type: test_failure
component: testing_framework
severity: high
root_cause: test_isolation
resolution_type: test_fix
symptoms:
  - "Feature ships. Unit tests and smoke tests pass. Production 500s on the feature's first real user interaction."
  - "Post-mortem shows the test harness had the same shape as the bug: zero-row collection, wrong-signature mock, or hand-written fixture key the real service never emits."
  - "Reverting the fix makes the new regression test fail, but none of the pre-existing tests fail — they never exercised the buggy branch."
tags:
  - pytest
  - template-rendering
  - smoke-tests
  - fixture-shape
  - mock-signatures
  - meta-pattern
  - trilogy
---

# The Test-Shape-Matches-Bug-Shape Meta-Pattern (2026 Trilogy)

## Problem

Three production bugs shipped within a week in April 2026, all caused by different surface mechanisms (wrong function signature, wrong JSON key, empty collection smoke test). Every instance had **passing unit tests and passing smoke tests right up until real user data hit prod**. Every instance had the same underlying meta-pattern: **the test harness's shape matched the buggy code's shape**, so the two sides agreed on the bug and the suite stayed green.

Any one of these could be written off as a bad day. Three in nine days is a pattern.

## Symptoms

The family signature — watch for any of these:

1. **Mock copies the production caller.** You stub a function in a test. Your stub accepts `(fn, label, *args)` because the production call site passes arguments in that order. The real function's signature is `(label, fn, *args)`. Tests pass. Prod crashes.
2. **Fixture hand-written to match the template.** A JSON-over-service feature has a service emitter producing `{pro_members: [...], college_members: [...]}`. Your test fixture hand-writes `{members: [...]}` because that's what the template happens to read. Template iterates `members`, fixture has `members`, test renders happily. Real service never emits `members`; prod renders empty rows.
3. **Empty collection sidesteps the broken branch.** Smoke test seeds zero rows in a table. Template has `{% if items %}{% for item in items %}...{% endif %}`. Outer conditional is False, broken `{% for %}` never renders. Test returns 200. Prod 500s the moment any row exists.

In all three: the specific symptom changes, the meta-pattern is identical. **The test touches only the paths production would touch if the bug didn't exist.**

## What Didn't Work

- Running the full suite — green. Suite tests the wrong shape against the wrong shape.
- Adding more smoke tests of the same style — more green. Same blind spot duplicated.
- Trusting "tests pass" as evidence of correctness — the bugs shipped *because* the tests passed.
- Local repro without real prod conditions — V2.14.5 burned ~20 min writing 8 synthetic POST-path tests before pulling Railway logs; none of the synthetic paths had the `PayoutTemplate` row that triggered the bug, same blind spot as the smoke test being debugged.

## Solution

The fix is rule-based, applied **at test-authoring time** and during code review:

### Rule 1 — Mock fidelity
**Test mocks must copy the real callee's signature, never the caller's.** When you stub `background_jobs.submit`, read the real definition in `services/background_jobs.py` and mirror its parameter names + order exactly. If you can't be bothered to look it up, use `unittest.mock.create_autospec(real_callable)` — autospec enforces the real signature and the buggy caller fails at stub time, before a single assertion runs.

### Rule 2 — Fixture round-trip
**Test fixtures for JSON-over-service features MUST round-trip through the real service emitter, never hand-write the shape.** Instead of:

```python
# BAD — hand-written, invents keys the real emitter never produces
teams = [{"members": [...]}]
event.payouts = json.dumps({"teams": teams, "status": "drawn"})
```

Do:

```python
# GOOD — use the real emitter, get the real shape
relay = ProAmRelay(tournament)
relay.run_lottery()  # emits {pro_members, college_members}, not {members}
teams = relay.get_state()["teams"]
```

If running the real emitter is expensive or stateful, extract a shape-only helper (`ProAmRelay.empty_team_shape()` → `{"pro_members": [], "college_members": []}`) and have BOTH the real emitter and the test fixture consume it. Now if the shape drifts, the emitter drifts with the fixture and the test is always honest.

### Rule 3 — Non-empty seed
**Every collection-gated branch in a rendered template (or every conditional path that depends on collection non-emptiness) needs at least one non-empty seed row in its smoke test.** If the template is:

```jinja
{% if templates %}
  {% for tpl in templates %}
    {% for pos, amt in tpl.get_payouts().items()|sort(attribute='0', key=int) %}
      ...
```

then the smoke test needs `PayoutTemplate.query.count() >= 1` with `get_payouts()` returning a non-empty dict. Zero-row is a trivial pass — it only proves that the `{% if %}` short-circuit works, which is a built-in Jinja feature, not your code. `pytest.mark.parametrize("n_rows", [0, 1])` is the cheapest way to cover both branches at once.

## Why This Works

Every test is a claim of the form "under condition X, output matches Y." The meta-bug here is that the test author and the production code author **both held the same wrong mental model of the data shape**, so the test's condition X never instantiated the production condition that triggers the bug. Green tests prove the code matches the test, not that the code is correct.

The three rules defeat this by forcing the test harness to obtain its shape **from a source outside the author's head**:

- Rule 1: shape comes from the real callee's parameter list, not the author's memory of it.
- Rule 2: shape comes from the real service emitter's actual output, not the author's guess at what keys exist.
- Rule 3: shape includes the non-empty branch because you explicitly seeded it — the branch can't be skipped by accident.

When the shape source is external, the author's bad mental model stops propagating into the test, and the test can actually disagree with the production code.

## Prevention

- **Grep for `|sort(` filters with unusual kwargs, `MagicMock(return_value=...)` without `spec=`, and `{% if collection %}...{% for item in collection %}` blocks in route-smoke test seed fixtures.** These are the three concrete bug surfaces from the trilogy.
- **When adding a route-smoke test for a page that renders a collection, add a companion test that seeds at least one row and asserts the rendered HTML contains an expected collection-member value.** Not just 200 — actual content. Otherwise the `{% if %}` short-circuit still hides render bugs.
- **Adopt `unittest.mock.create_autospec` as the default mocking style** for any function whose signature could plausibly change. `create_autospec(services.background_jobs.submit)` raises `TypeError` the instant a test passes the args in the wrong order. A plain `MagicMock()` will happily swallow anything.
- **Run codex (or any outside-voice adversarial review) on the diff before merge.** Each of the three trilogy bugs was caught by codex reading the real service class, not by our own test suite. That's not because codex is smarter — it's because codex reads production code without pre-loading your test harness's assumptions.
- **Add a one-line meta-check to the CI workflow:** `grep -rn '|sort([^)]*key=' templates/` (fails build if any match). Treat this class of latent-render-bomb the same way you treat type errors — block at CI time, not at runtime.

## Related

- [mock-signature-matches-buggy-call-site-2026-04-22.md](mock-signature-matches-buggy-call-site-2026-04-22.md) — Instance 1 (V2.13.0, 2026-04-22, PR #66 / codex PR #67): `background_jobs.submit` called with `(fn, label)` instead of `(label, fn)`; sync-test mock copied the same wrong order so both sides agreed on the bug.
- [hand-written-fixture-shape-divergence-2026-04-22.md](hand-written-fixture-shape-divergence-2026-04-22.md) — Instance 2 (V2.14.0 codex hotfix, 2026-04-22, PR #73): `ProAmRelay.run_lottery()` emits `{pro_members, college_members}` but test fixture hand-wrote `members`; template iterated `members`, fixture had `members`, test rendered happily; prod rendered empty rows.
- [../best-practices/jinja-sort-filter-has-no-key-kwarg-2026-04-23.md](../best-practices/jinja-sort-filter-has-no-key-kwarg-2026-04-23.md) — The specific Jinja filter misuse that was Instance 3's surface mechanism. Documents the grep-able bomb-finder and the model-method workaround.
- [../best-practices/traceback-before-repro-2026-04-23.md](../best-practices/traceback-before-repro-2026-04-23.md) — The investigation-process lesson from V2.14.5: when prod 500s, pull the Railway traceback before writing local repro tests. Local synthesis inherits the same zero-row blind spot that hid the bug in the first place.
- [tests-asserting-contradictory-behavior.md](tests-asserting-contradictory-behavior.md) — Adjacent meta-pattern: tests that assert on behavior the spec no longer requires, and pass because the code also still implements it.

## Release Timeline

| Instance | Version | Ship Date | PR | Surface mechanism | Bugged shape source |
|---|---|---|---|---|---|
| 1 | V2.13.0 | 2026-04-22 | #66 / #67 | Wrong arg order to `submit()` | Mock copied caller, not callee |
| 2 | V2.14.0 | 2026-04-22 | #73 | Wrong JSON key (`members` vs `{pro,college}_members`) | Fixture hand-written to match template |
| 3 | V2.14.5 | 2026-04-23 | #85 | `\|sort(key=int)` — Jinja kwarg doesn't exist | Empty-DB smoke test skipped `{% if templates %}` branch |

The fact that all three shipped in nine days is load-bearing evidence: this isn't an accident, it's a class of bug that our development style is fluent in producing. The rules above are the fix for the class, not the instances.
