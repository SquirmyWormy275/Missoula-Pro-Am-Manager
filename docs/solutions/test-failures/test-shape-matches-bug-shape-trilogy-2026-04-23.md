---
module: testing
date: 2026-04-23
last_updated: 2026-04-23
problem_type: test_failure
component: testing_framework
severity: high
root_cause: test_isolation
resolution_type: test_fix
symptoms:
  - "Feature ships. Unit tests and smoke tests pass. Production 500s (or fails to deploy) on the feature's first real user interaction."
  - "Post-mortem shows the test harness had the same shape as the bug: zero-row collection, wrong-signature mock, hand-written fixture key the real service never emits, or local Python interpreter that accepts syntax production rejects."
  - "Reverting the fix makes the new regression test fail, but none of the pre-existing tests fail — they never exercised the buggy branch (or never ran under the same conditions as production)."
tags:
  - pytest
  - template-rendering
  - smoke-tests
  - fixture-shape
  - mock-signatures
  - meta-pattern
  - quartet
  - python-version-skew
  - environment-parity
---

# The Test-Shape-Matches-Bug-Shape Meta-Pattern (2026 Quartet)

> **Note on filename:** This doc was originally written as the "trilogy" of three bugs. The V2.14.10 deploy-fail surfaced a fourth instance and we extended it to a quartet rather than splitting. Filename retained for cross-link stability.

## Problem

Four production bugs shipped within ten days in April 2026, all caused by different surface mechanisms (wrong function signature, wrong JSON key, empty collection smoke test, Python version skew). Every instance had **passing unit tests and passing smoke tests right up until real user data hit prod (or right up until the deploy ran on prod's actual Python interpreter)**. Every instance had the same underlying meta-pattern: **the test harness's shape OR environment matched the buggy code's shape OR environment**, so the two sides agreed on the bug and the suite stayed green.

Any one of these could be written off as a bad day. Four in ten days is a pattern.

## Symptoms

The family signature — watch for any of these:

1. **Mock copies the production caller.** You stub a function in a test. Your stub accepts `(fn, label, *args)` because the production call site passes arguments in that order. The real function's signature is `(label, fn, *args)`. Tests pass. Prod crashes.
2. **Fixture hand-written to match the template.** A JSON-over-service feature has a service emitter producing `{pro_members: [...], college_members: [...]}`. Your test fixture hand-writes `{members: [...]}` because that's what the template happens to read. Template iterates `members`, fixture has `members`, test renders happily. Real service never emits `members`; prod renders empty rows.
3. **Empty collection sidesteps the broken branch.** Smoke test seeds zero rows in a table. Template has `{% if items %}{% for item in items %}...{% endif %}`. Outer conditional is False, broken `{% for %}` never renders. Test returns 200. Prod 500s the moment any row exists.
4. **Local interpreter parses syntax production rejects.** Local dev runs Python 3.13; production runs 3.10. You write an f-string with a backslash inside the expression part (PEP 701, lifted in 3.12). Local app import succeeds. Local pytest passes. CI lint passes (CI runner's Python is also newer than prod). Railway deploy fails at `flask db upgrade` with `SyntaxError: f-string expression part cannot include a backslash`. Production stays on the previous version with no obvious signal.

In all four: the specific symptom changes, the meta-pattern is identical. **The test touches only the paths production would touch if the bug didn't exist — OR the test runs in an environment where the bug doesn't exist.**

## What Didn't Work

- Running the full suite — green. Suite tests the wrong shape against the wrong shape.
- Adding more smoke tests of the same style — more green. Same blind spot duplicated.
- Trusting "tests pass" as evidence of correctness — the bugs shipped *because* the tests passed.
- Local repro without real prod conditions — V2.14.5 burned ~20 min writing 8 synthetic POST-path tests before pulling Railway logs; none of the synthetic paths had the `PayoutTemplate` row that triggered the bug, same blind spot as the smoke test being debugged.
- Local app-import smoke (`python -c "from app import create_app; ..."`) — V2.14.10 false-green because local Python 3.13 accepts PEP 701 syntax that production Python 3.10 rejects. Same class of failure: the verification environment matched the bug's environment instead of production's.

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

### Rule 4 — Pin the verification environment to production
**Every check that gates a deploy must run against the same Python version (and library versions) production uses.** Local interpreter smoke tests, CI lint runners, and editor-integrated linters all default to whatever Python is locally installed — typically newer than the deploy target. PEP 701 (Python 3.12+) lifts the no-backslash-in-f-string-expression restriction; Python 3.10 still enforces it; ruff doesn't catch it without `target-version = "py310"`; CI's `py_compile` runs against CI's Python, not prod's.

The cheapest pre-push reflex:

```bash
for f in $(git diff --name-only HEAD~1 -- '*.py'); do
  python -c "import ast; ast.parse(open('$f').read(), feature_version=(3,10))" 2>&1 \
    | grep -q SyntaxError && echo "FAIL: $f"
done
```

The `feature_version=(3,10)` argument tells Python's AST parser to apply 3.10's grammar rules regardless of which interpreter is locally installed. The CI version of this should pull `feature_version` from `pyproject.toml`'s `requires-python` so it stays in sync if the runtime moves. Pin `target-version = "py310"` in ruff for the related class (UP rules) as defense in depth. Add a `.python-version` file so pyenv / mise / uv default new dev machines to the prod minor version.

This rule also covers the broader environment-parity surface: SQLite-only local DB vs PG prod (test against both — see `tests/test_pg_migration_safety.py` for an existing example), missing env vars caught at app start (validated by `routes.main._health_diag`), library version drift between `requirements.txt` and the lockfile.

## Why This Works

Every test is a claim of the form "under condition X, output matches Y." The meta-bug here is that the test author and the production code author **both held the same wrong mental model of the data shape**, so the test's condition X never instantiated the production condition that triggers the bug. Green tests prove the code matches the test, not that the code is correct.

The three rules defeat this by forcing the test harness to obtain its shape **from a source outside the author's head**:

- Rule 1: shape comes from the real callee's parameter list, not the author's memory of it.
- Rule 2: shape comes from the real service emitter's actual output, not the author's guess at what keys exist.
- Rule 3: shape includes the non-empty branch because you explicitly seeded it — the branch can't be skipped by accident.

When the shape source is external, the author's bad mental model stops propagating into the test, and the test can actually disagree with the production code.

Rule 4 is the same idea applied to environment instead of shape: the verification *environment* (Python version, DB engine, env vars, library versions) must come from the deploy target's pin, not from "whatever's installed locally." Once the verification environment is external, local tooling can no longer paper over a prod-only failure.

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
- [../build-errors/railway-python-310-fstring-backslash-deploy-failure-2026-04-23.md](../build-errors/railway-python-310-fstring-backslash-deploy-failure-2026-04-23.md) — Instance 4 (V2.14.10, 2026-04-23, PR #91 / hotfix #92): f-string backslash inside expression part shipped on local Python 3.13, crashed Railway deploy on Python 3.10. Same meta-pattern; environment instead of shape.

## Release Timeline

| Instance | Version | Ship Date | PR | Surface mechanism | Bugged shape source |
|---|---|---|---|---|---|
| 1 | V2.13.0 | 2026-04-22 | #66 / #67 | Wrong arg order to `submit()` | Mock copied caller, not callee |
| 2 | V2.14.0 | 2026-04-22 | #73 | Wrong JSON key (`members` vs `{pro,college}_members`) | Fixture hand-written to match template |
| 3 | V2.14.5 | 2026-04-23 | #85 | `\|sort(key=int)` — Jinja kwarg doesn't exist | Empty-DB smoke test skipped `{% if templates %}` branch |
| 4 | V2.14.10 | 2026-04-23 | #91 / #92 | Backslash inside f-string expression — PEP 701 (3.12+) syntax | Local Python 3.13 parsed cleanly; prod Python 3.10 rejected at app import |

The fact that all four shipped in ten days is load-bearing evidence: this isn't an accident, it's a class of bug that our development style is fluent in producing. The rules above are the fix for the class, not the instances. Note Instance 4 confirms the pattern is broader than "test-shape" — it's "verification surface matches bug surface," whether the matching dimension is data shape (1, 2, 3) or runtime environment (4).
