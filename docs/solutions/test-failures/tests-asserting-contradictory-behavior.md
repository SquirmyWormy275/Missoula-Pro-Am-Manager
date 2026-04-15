---
type: bug
problem_type: test-failure
severity: medium
symptoms:
  - "Single test fails on every CI run but passes locally"
  - "Test mocks flask_migrate.upgrade but CI uses db.create_all()"
  - "Test asserts function returns X but env forces function to return Y"
tags:
  - "pytest"
  - "ci"
  - "test-hygiene"
confidence: high
created: 2026-04-15
source: "knowledge-seed from CLAUDE.md and git history"
---

# Tests that assert self-contradictory behavior under CI env

## Problem
`tests/test_flask_reliability.py` had tests that could never pass under CI:
- One asserted `_normalized_database_url()` returned the project default even when `DATABASE_URL=sqlite:///test.db` was in env (function honors DATABASE_URL — it cannot return anything else).
- Another mocked `flask_migrate.upgrade` and asserted it was called, but CI sets `TEST_USE_CREATE_ALL=1` which routes to `db.create_all()` instead.

Both were papered over with CI ignore-list entries. Every subsequent PR's CI went red.

## Root Cause
Tests written against one env configuration (local dev), run under a different env (CI with different flags). The test assertions were incompatible with the env — not a flaky bug, a correctness bug in the test.

## Solution
- Delete or rewrite the test to match actual code behavior under the target env.
- Don't patch over test failures with CI ignore-lists — fix the root cause. The ignore-list compounds: every new PR runs red, developers stop reading CI output.

## Prevention
- When a test fails on CI only, first check: does the test's assertion match the function's behavior under CI's env vars?
- `TEST_USE_CREATE_ALL`, `DATABASE_URL`, `ENV_NAME` all change code paths — tests must be written for the env they'll run in.
- Resist "just add it to the ignore list." That is tech debt that degrades the signal of every future run.
