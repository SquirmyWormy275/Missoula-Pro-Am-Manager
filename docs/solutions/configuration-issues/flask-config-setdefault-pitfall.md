---
type: bug
problem_type: configuration-issue
severity: high
symptoms:
  - "SESSION_COOKIE_SECURE=False in production despite being set in create_app()"
  - "SESSION_COOKIE_SAMESITE=None despite being set"
  - "Security config silently ignored"
tags:
  - "flask"
  - "security"
  - "session-cookies"
confidence: high
created: 2026-04-15
source: "knowledge-seed from CLAUDE.md and git history"
---

# `app.config.setdefault()` silently skips Flask-preseeded keys

## Problem
`create_app()` used `app.config.setdefault('SESSION_COOKIE_SECURE', True)` etc. to harden session cookies. `/health/diag` showed the values weren't actually applied — cookies went out over HTTP with no SameSite protection.

## Root Cause
Flask pre-seeds `SESSION_COOKIE_SECURE=False`, `SESSION_COOKIE_SAMESITE=None`, `SESSION_COOKIE_HTTPONLY=True` into `app.config` during `Flask.__init__`. `setdefault()` checks for key existence, not truthiness — it sees the Flask defaults already there and skips. Only `HTTPONLY` was right, and only by coincidence.

## Solution
Use direct assignment for config keys Flask pre-populates:

```python
if os.environ.get('ENV_NAME') == 'production':
    app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True
```

## Prevention
- Never use `setdefault()` for Flask-managed config keys (`SESSION_*`, `PERMANENT_SESSION_LIFETIME`, `SECRET_KEY`, etc.).
- Verify via `/health/diag` (or equivalent) that config values in production match what `create_app()` intended.
- When hardening security config, add a diag assertion or integration test that reads `app.config` after boot.
