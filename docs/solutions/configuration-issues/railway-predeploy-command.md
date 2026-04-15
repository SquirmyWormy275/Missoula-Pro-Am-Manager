---
type: bug
problem_type: configuration-issue
severity: critical
symptoms:
  - "Railway deploys succeed but migrations never run"
  - "Production schema stuck at an old head for weeks"
  - "Deploy logs show no output from `flask db upgrade`"
tags:
  - "railway"
  - "deploy"
  - "flask-migrate"
  - "postgres"
confidence: high
created: 2026-04-15
source: "knowledge-seed from CLAUDE.md and git history"
---

# Railway silently ignores `releaseCommand` in railway.toml

## Problem
`railway.toml` had `releaseCommand = "flask db upgrade"`. Railway's Railpack builder silently ignored the field — migrations never ran on deploy. Production DB sat schemaless against code HEAD for ~2 weeks, culminating in a race-day-week incident.

## Root Cause
`releaseCommand` is a legacy Heroku-buildpack-era field name. Railway's modern builder reads `preDeployCommand` (docs: https://docs.railway.com/deployments/pre-deploy-command). Unknown fields are silently ignored — no warning, no error.

## Solution
Use `preDeployCommand` in `railway.toml`:

```toml
[deploy]
preDeployCommand = "flask db upgrade"
```

## Prevention
- After any `railway.toml` change, check the next deploy's logs for output from the configured command between "Starting Container" and "Starting gunicorn".
- If no output appears, the field name is wrong or the command crashed silently.
- Treat Railway config fields as a closed allowlist — if the docs don't name it, it doesn't exist.
