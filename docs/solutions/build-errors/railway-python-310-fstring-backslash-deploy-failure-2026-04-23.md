---
title: "Railway deploy fails: f-string expression backslash incompatible with Python 3.10"
date: 2026-04-23
category: build-errors
module: routes/scheduling
problem_type: build_error
component: development_workflow
severity: critical
symptoms:
  - "Railway pre-deploy command (flask db upgrade) exits 11s into deploy with SyntaxError"
  - "App import crashes before Flask context loads: 'f-string expression part cannot include a backslash'"
  - "Local pytest + dev server pass cleanly on Python 3.13; production stays on previous version"
  - "Build phase succeeds, only deploy step shows FAILED in Railway dashboard"
  - "/health endpoint never updates to new version string post-merge"
root_cause: incomplete_setup
resolution_type: code_fix
related_components:
  - tooling
  - testing_framework
tags:
  - python-310
  - f-string
  - pep-701
  - railway-deploy
  - syntax-error
  - pre-deploy-command
  - flask-db-upgrade
  - version-skew
  - ast-validation
---

# Railway deploy fails: f-string expression backslash incompatible with Python 3.10

## Problem

V2.14.10 deploy to Railway failed at the pre-deploy `flask db upgrade` step because commit `07ce115` introduced a Python 3.12+ f-string construct (backslash inside an expression part, lifted by [PEP 701](https://peps.python.org/pep-0701/)) into `routes/scheduling/__init__.py:271-275`. Production runs Python 3.10 and could not import the app. Production kept serving V2.14.9 — no downtime, but the entire fix bundle (partner pairing + persisted-config audit) silently failed to ship until a hotfix landed ~19 minutes later.

This is the **third** occurrence of the same Python-version-skew pattern on this project (V2.6.x `scripts/smoke_test.py`, an earlier session, and now V2.14.10). The first two were latent — a script that was never actually run on prod. This one bricked an active deploy. (session history)

## Symptoms

- Railway deployment marked FAILED ~10:44 UTC, 11 seconds into the pre-deploy step.
- Build phase succeeded; only the deploy step (`flask db upgrade`, configured as `preDeployCommand` in `railway.toml`) failed.
- Pre-deploy step errored on app import with the exact traceback:
  ```
  File "/app/routes/scheduling/__init__.py", line 274
      for r in rows[:3]
      ^^^
  SyntaxError: f-string expression part cannot include a backslash
  ```
- Production `/health` continued serving the previous version (V2.14.9); the new container never replaced the running one.
- Local sanity checks all passed:
  - `python -c "from app import create_app; app = create_app(); print('IMPORT OK')"` → `IMPORT OK`
  - `python -m flask db upgrade` → exited 0 (DB already at HEAD)
  - `pytest` → 252/252 passed
- The only signal of failure was the Railway dashboard showing FAILED on the latest deploy.

## What Didn't Work

1. **Local app-import smoke test.** Running `python -c "from app import create_app; ..."` printed `IMPORT OK` and produced false confidence. The local interpreter is Python 3.13 — PEP 701 is in effect, the syntax parses fine, the app imports cleanly. The test answered the wrong question (does it import on this machine?) instead of the question that mattered (does it import on the deploy target?).

2. **Local `flask db upgrade`.** Same false-green. The local DB was already at HEAD so Alembic exited 0 without re-importing the broken module under prod-version semantics. Even if a migration had been pending, the local interpreter would still parse the file fine.

3. **First Railway log fetch via `railway logs --build`.** Returned BUILD logs, which had succeeded. The actual error lived in DEPLOY logs and required `railway deployment list` to find the failed deployment ID, then `railway logs -d <id>` to surface the SyntaxError traceback. Without the deployment ID the right logs were invisible.

4. **CI lint pass (`ruff check .`).** Did not catch the syntax error. The CI lint job runs against whichever Python the runner is built with — not pinned to the production minor version. `ruff` parses files against its own bundled Python parser; backslash-in-f-string-expression is specifically a Python *version* restriction (3.10 rejects, 3.12 accepts), not a universal parse failure. (session history) The V2.6.x occurrence was caught by ruff only because it was a *different* class of f-string syntax that did fail universal parsing — that exposure created false confidence that ruff was a complete guard for this pattern.

## Solution

Hotfix in `routes/scheduling/__init__.py` (commit `d54617b`, PR #92). Replaced the f-string conditional with a local helper using `str.format()` so the backslash-bearing string literals live in regular function bodies, not inside an f-string expression slot.

**Before** (commit `07ce115`, fails on Python 3.10):

```python
for ev, rows in unpaired_by_event:
    names = ', '.join(
        f"{r['comp_name']}{(' → \"' + r['partner_name'] + '\"') if r['partner_name'] else ''}"
        for r in rows[:3]
    )
    extra = f' (+{len(rows) - 3} more)' if len(rows) > 3 else ''
```

**After** (commit `d54617b`, Python 3.10-safe):

```python
for ev, rows in unpaired_by_event:
    # Build each name blurb without backslashes inside the f-string
    # expression (Python 3.10 / pre-PEP-701 constraint). Prod runs 3.10.
    def _blurb(row: dict) -> str:
        partner = row.get('partner_name') or ''
        if partner:
            return '{} → "{}"'.format(row.get('comp_name', ''), partner)
        return row.get('comp_name', '')
    names = ', '.join(_blurb(r) for r in rows[:3])
    extra = f' (+{len(rows) - 3} more)' if len(rows) > 3 else ''
```

Production verified at `/health` → `{"version":"2.14.10","status":"ok"}` ~11:03 UTC.

## Why This Works

PEP 701 (Python 3.12+) lifted the restriction "f-string expression parts cannot contain backslashes." Python 3.10 (the production runtime) still enforces it. The escaped quote `\"` inside the conditional `(' → \"' + r['partner_name'] + '\"') if r['partner_name'] else ''` lives inside the f-string's `{...}` expression part — that's the part 3.10 forbids backslashes in.

Moving the string assembly into a regular function body puts the backslash-bearing literal outside any f-string expression. Inside `_blurb()`, `'{} → "{}"'.format(...)` is a normal expression; the backslashes never enter an f-string slot. The 3.10 parser is satisfied.

**Safe vs unsafe patterns under Python 3.10:**

| Pattern | Status | Note |
|---------|--------|------|
| `f"foo\nbar {x}"` | ✅ Safe | Backslash in literal portion |
| `f"{x} \"between\" {y}"` | ✅ Safe | Backslash in literal portion BETWEEN expressions |
| `f"{x.replace(\"a\",\"b\")}"` | ❌ Fails | Backslash inside `{...}` expression |
| `f"{(' → \"' + x) if x else ''}"` | ❌ Fails | String with backslash inside expression |
| `s = ' → "' + x; f"{s}"` | ✅ Safe | Extracted to local variable |
| `'{} → "{}"'.format(a, b)` | ✅ Safe | Plain `.format()` instead |

Verified during hotfix that the visually-similar f-strings in `services/preflight.py:228` and `routes/scheduling/heats.py:179` are FINE — those `\"` characters live in the LITERAL portion between `}` and `{`, not inside an expression. A human eyeballing the diff cannot reliably tell them apart from the broken case. **Only `ast.parse(..., feature_version=(3,10))` does.**

## Prevention

### Pre-push reflex (run on any commit touching f-strings)

```bash
for f in $(git diff --name-only HEAD~1 -- '*.py'); do
  python -c "import ast; ast.parse(open('$f').read(), feature_version=(3,10))" 2>&1 \
    | grep -q SyntaxError && echo "FAIL: $f"
done
```

The `feature_version=(3,10)` argument tells Python's AST parser to apply 3.10's grammar rules regardless of which interpreter is locally installed. This is the only check that catches the version-restricted syntax, because:

- Local `python -c "from app import ..."` uses the local interpreter (3.13 here) → false green.
- `ruff check` parses against ruff's bundled Python → does not enforce a project-pinned target version unless `target-version = "py310"` is set in `pyproject.toml` AND ruff has rules enabled to check version compatibility (UP rules).
- `py_compile` in CI uses the CI runner's Python → typically newer than prod.

### CI integration (recommended next step)

Add an `ast-validate` job to `.github/workflows/ci.yml` that runs the AST one-liner above against every changed `*.py`, with the version pinned to whatever `pyproject.toml`'s `requires-python` declares. Pin the value via a lookup so the check stays in sync if the runtime moves:

```yaml
- name: AST-validate against production Python version
  run: |
    PROD_PY=$(grep -oP '(?<=requires-python = ")[^"]+' pyproject.toml | grep -oP '\d+\.\d+' | head -1)
    MAJ=${PROD_PY%.*}; MIN=${PROD_PY#*.}
    for f in $(git diff --name-only origin/main HEAD -- '*.py'); do
      python -c "import ast; ast.parse(open('$f').read(), feature_version=($MAJ,$MIN))" \
        || { echo "FAIL: $f"; exit 1; }
    done
```

### Pin local Python to match production

Add a `.python-version` file (read by pyenv / `mise` / `uv`) declaring `3.10.x`. New developer machines and CI runners then default to the prod minor version. Existing developers running newer Python via `pyenv` will still type-check against prod via the AST check above.

### Configure ruff target-version (defense in depth)

In `pyproject.toml`:

```toml
[tool.ruff]
target-version = "py310"
```

This lets ruff's `UP` rules (pyupgrade) flag syntax that's only available in newer Python. It's not a complete guard for this case (PEP 701 lifted a restriction rather than added syntax — it's invisible to UP rules), but it tightens the lint bar and would catch related class of issues (match statements when targeting 3.9, `type` aliases targeting 3.11, etc.).

## Related Issues

This is part of a meta-pattern that has produced four documented incidents in the same week — all sharing the shape "a verification surface in dev passed, prod failed."

- [test-shape-matches-bug-shape-trilogy-2026-04-23.md](../test-failures/test-shape-matches-bug-shape-trilogy-2026-04-23.md) — sibling meta-pattern. The three test-shape variants are all "fixture / mock / call-site shape matches the bug shape, so the test passes alongside the bug." This V2.14.10 incident is a fourth instance: the test runner's Python version matches the dev machine's, so the bug only surfaces on the deploy target. Consider promoting trilogy → quartet, or cross-link from there. *(Refresh candidate — see Phase 2.5 below.)*
- [traceback-before-repro-2026-04-23.md](../best-practices/traceback-before-repro-2026-04-23.md) — would have pinpointed line 273 immediately. The investigation here started with a local-import smoke (false green), then local `flask db upgrade` (false green), then build logs (wrong logs), then finally deploy logs which named the file and line in two seconds. Pulling the deploy traceback first is always cheaper than reproducing locally.
- [railway-predeploy-command.md](../configuration-issues/railway-predeploy-command.md) — same operational surface (`preDeployCommand` running `flask db upgrade`), different root cause (silent config field rename vs syntax error). Both fail in the same place.
- [railway-postgres-operational-playbook-2026-04-21.md](../best-practices/railway-postgres-operational-playbook-2026-04-21.md) — comprehensive Railway operational playbook. The "deploy fails silently while CI is green" case studies are the natural home for an interpreter-version-skew subsection.
- [stale-flask-dev-server-after-code-update-2026-04-21.md](../developer-experience/stale-flask-dev-server-after-code-update-2026-04-21.md) — same diagnostic discipline (`curl /health` to confirm running version matches expected). The post-deploy `/health` poll caught V2.14.10 not advancing past V2.14.9.
- [sequential-ship-pattern-parallel-claude-sessions-2026-04-23.md](../best-practices/sequential-ship-pattern-parallel-claude-sessions-2026-04-23.md) — same week's sister doc. V2.14.10 stacked on V2.14.9 (parallel-session ship); the sequential discipline kept the version slots from colliding even though the deploy itself failed.

**Notable absence.** The V2.6.x `scripts/smoke_test.py` fix referenced in MEMORY.md ("nested f-string syntax (Python 3.12+) that crashes on 3.10 — fixed in V2.6.x patch via local var extraction") was never written up — it was caught as a side effect of adding ruff to CI in PR #2 and treated as a "trivial" one-line fix bundled into the same commit as ruff autofixes. (session history) **No `docs/solutions/` entry existed for this problem class until this doc.** The pattern recurred because the institutional memory was a one-liner in MEMORY.md, not a searchable solution doc with prevention rules. This doc is the canonical record going forward.

## Auto Memory Cross-References

- `feedback_python_310_fstring_compat.md` (auto memory [claude]) — operator-facing rule: "AST-validate every `*.py` against `feature_version=(3,10)` before pushing."
- `feedback_dev_server_version_first.md` (auto memory [claude]) — `/health` is the first diagnostic when "fix isn't working locally" — directly applicable here, would have surfaced the failure 60s after merge.
