---
module: integrations
date: 2026-04-15
problem_type: best_practice
component: service_object
severity: high
tags:
  - "strathmark"
  - "sms"
  - "backup"
  - "integrations"
  - "architecture"
---

# Optional external integrations must be non-blocking and graceful no-op

## Context
On race day, an Ollama outage, Twilio latency, Supabase downtime, or a missing S3 bucket CANNOT take the tournament app offline. This is a timebox-critical live-event system.

## Pattern
Every optional integration exposes `is_configured()` and short-circuits when false. All external calls are wrapped to catch every exception, log a warning, and return `False`/`None` — never re-raise:

```python
def push_pro_event_results(event):
    if not is_configured():
        return None
    try:
        # external call
    except Exception as e:
        logger.warning("STRATHMARK push failed: %s", e)
        return False
```

Applied to:
- `services/strathmark_sync.py` — Supabase enrollment + result push.
- `services/sms_notify.py` — Twilio (graceful no-op if `twilio` not installed or env vars unset).
- `services/backup.py` — S3 if env vars set, local `instance/backups/` fallback.

## Rationale
Intent to fail-loud has repeatedly caused worse outcomes than fail-soft. The `validate_runtime()` hard-fail on missing STRATHMARK env vars bricked every Railway deploy for weeks until the gate was scoped to `ENV_NAME == 'production'` only.

## Examples
- Core infra (DB URL, SECRET_KEY in production) — fail-loud is correct.
- Optional integrations (SMS, backup, STRATHMARK sync, Sentry) — graceful no-op.
- Every finalize/commit path calls integrations AFTER `db.session.commit()` so an integration failure cannot roll back tournament state.
