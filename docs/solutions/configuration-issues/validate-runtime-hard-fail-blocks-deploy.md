---
type: bug
problem_type: configuration-issue
severity: high
symptoms:
  - "Railway email: Deployment crashed for run-flask-db-upgrade"
  - "App refuses to start when STRATHMARK_SUPABASE_* env vars missing"
  - "Optional integrations block core deploys"
tags:
  - "deploy"
  - "runtime-validation"
  - "strathmark"
confidence: high
created: 2026-04-15
source: "knowledge-seed from CLAUDE.md and git history"
---

# `validate_runtime()` hard-fail on optional env vars blocks deploys

## Problem
`validate_runtime()` raised `RuntimeError` when optional integration env vars (e.g., `STRATHMARK_SUPABASE_URL`, `STRATHMARK_SUPABASE_KEY`) were missing. Every Railway deploy without those vars crashed during release phase.

## Root Cause
Intent was a fail-loud signal for the show director. Reality: gate blocked every unrelated deploy.

## Solution
Gate hard-fails on `ENV_NAME == 'production'` only. In non-prod, log a warning but allow boot. Make optional integrations truly optional:

```python
if os.environ.get('ENV_NAME') == 'production':
    # only fail-loud in prod
    if not all_strathmark_vars_set:
        raise RuntimeError(...)
else:
    logger.warning("STRATHMARK env vars missing — integration disabled")
```

## Prevention
- Default posture for optional integrations: graceful no-op (see `services/strathmark_sync.py`, `services/sms_notify.py`, `services/backup.py`).
- Reserve hard-fails for core infra (DB URL, SECRET_KEY) in production only.
- Every integration module should expose `is_configured()` and short-circuit calls when false.
