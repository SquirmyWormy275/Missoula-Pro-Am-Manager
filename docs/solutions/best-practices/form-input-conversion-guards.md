---
type: knowledge
problem_type: best-practice
severity: medium
tags:
  - "flask"
  - "forms"
  - "validation"
  - "csrf"
confidence: high
created: 2026-04-15
source: "knowledge-seed from CLAUDE.md and git history"
---

# Form input conversion, CSRF, and auth conventions

## Context
Bad form data and missing auth/CSRF caused repeat 500s and silent failures. These conventions are non-negotiable for any new route.

## Pattern
**Input conversion** — every `int()` / `float()` on POST form data wrapped in `try/except`:

```python
try:
    points = int(request.form.get('points', '0'))
except (TypeError, ValueError):
    flash('Invalid points value', 'danger')
    return redirect(url_for('...'))
```

Never let `ValueError` from parsing user input propagate to a 500.

**CSRF** — Flask-WTF `CSRFProtect` is active globally. All POST form templates include:

```html
<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
```

JSON POST endpoints that don't submit HTML forms need `@csrf.exempt`. GET-only JSON endpoints need nothing.

**Auth** — new management routes must be in `MANAGEMENT_BLUEPRINTS` in `app.py` to pick up the `require_judge_for_management_routes` before_request hook. Seven roles: admin, judge, scorer, registrar, competitor, spectator, viewer. Public endpoints (static, `main.index`, `main.set_language`, `auth.*`, `portal.*`, `/sw.js`) are whitelisted.

**Error handling** — descriptive flash + redirect on recoverable errors. Never `except Exception: flash(str(e))` — that leaks stack detail to users (already caused an error-leakage fix in `routes/registration.py`).

## Rationale
These are the most common ways new routes 500 in this project. The conventions are enforced by code review, not by the framework.

## Examples
- Good: `routes/scoring.py` result entry — wraps every numeric input.
- Good: `routes/api.py` write endpoints — `@csrf.exempt` + `write_limit()` decorator.
- Anti-pattern retired in `routes/registration.py` commit: `except Exception: flash(str(e))`.
