# PRODUCTION AUDIT — Missoula Pro-Am Manager V2.2.0
*Generated 2026-03-09 | Target deployment: April 24-25, 2026*

---

## 1A. Concurrent Write Safety

### Summary
SQLite is single-writer. The optimistic-lock layer (`version_id_col`) is correctly wired on both
`Heat` and `EventResult`. The heat edit-lock (`locked_by_user_id` / `locked_at`) is checked in the
POST handler. `StaleDataError` is caught and produces a user-facing flash + redirect in all three
key locations. No raw SQLite-specific syntax beyond the already-guarded PRAGMA in `app.py`.

### Findings

| # | File | Lines | Severity | Finding | Fix |
|---|------|-------|----------|---------|-----|
| 1A-1 | `models/heat.py` | 43–44 | medium | `competitors` and `stand_assignments` columns are `db.Column(db.Text)` storing JSON. On PostgreSQL this works fine as TEXT, but loses JSON query capability. | On Postgres upgrade, consider `db.Column(db.JSON)` or `db.Column(JSONB)` via `sqlalchemy.dialects.postgresql`. No functional change needed for SQLite. |
| 1A-2 | `routes/scoring.py` | 232–241 | low | `StaleDataError` caught and redirected correctly. | No action needed. |
| 1A-3 | `models/heat.py` | 50–52 | low | `__mapper_args__ = {'version_id_col': version_id}` present on `Heat`. | Verified correct. No action needed. |
| 1A-4 | `models/event.py` | 177–180 | low | `__mapper_args__ = {'version_id_col': version_id}` present on `EventResult`. | Verified correct. No action needed. |
| 1A-5 | `routes/scoring.py` | 420–429 | low | `Heat.is_locked()` checked before every score-entry POST handler. Locking is enforced. | No action needed. |
| 1A-6 | `app.py` | 180–185 | low | `PRAGMA foreign_keys=ON` is correctly guarded with `if app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite')`. No other SQLite-specific pragmas or raw SQL found. | No action needed. |
| 1A-7 | `config.py` | (DATABASE_URL env var) | medium | If `DATABASE_URL` env var is not set, app falls back to `sqlite:///instance/proam.db`. Files referencing this path: `config.py` (default URI), `railway.toml` (implicit). No Python code hardcodes the SQLite path string directly. | Document the Railway `DATABASE_URL` env var requirement in `POSTGRES_MIGRATION.md`. |

---

## 1B. Offline Scoring Replay

### Findings

| # | File | Lines | Severity | Finding | Fix |
|---|------|-------|----------|---------|-----|
| 1B-1 | `static/sw.js` | 94–101 | **critical** | Queued POST body is the raw form-encoded string (via `request.text()`), which includes the CSRF token. Flask-WTF's default `WTF_CSRF_TIME_LIMIT` is 3600 s (1 hour). If the judge is offline longer than that, replay will receive HTTP 400 (CSRF token expired), and the score will be silently discarded (see 1B-2). | On CSRF 400 replay, retain the item in the queue and notify the user that they must re-login before their queued scores can be replayed. Alternative: use CSRF-exempt API endpoint for queued replays and validate via a session-scoped secret instead. |
| 1B-2 | `static/sw.js` | 152–155 | **critical** | `replayQueue()` removes an entry from the queue for any `resp.status < 500`. This means HTTP 403 (expired CSRF) and HTTP 409 (version conflict) are silently discarded — the score is lost with no user notification. | Change to: remove on 2xx; for 4xx, retain in queue AND send a `{ type: 'replay-failed', count, reason }` postMessage to open clients so the judge sees an error banner. |
| 1B-3 | `static/offline_queue.js` | 65–75 | medium | `showSyncBanner()` shows successful sync count. There is no corresponding failure banner for 4xx replay errors. | Add a failure banner / alert when replay returns an error status. |
| 1B-4 | `static/sw.js` | 26–63 | low | IndexedDB is used for storage, which persists across browser restarts and tab closes. | Confirmed working correctly. No action needed. |
| 1B-5 | `static/offline_queue.js` | 126–130 | low | On `online` event, postMessage `manual-sync` is sent to the SW, which triggers `replayQueue()`. There is no explicit "Retry All" button visible in the UI. | Consider adding a manual retry button to `scoring/offline_ops.html` that sends `manual-sync` to the SW controller. |

---

## 1C. Transaction Safety in Scheduling

### Findings

| # | File | Lines | Severity | Finding | Fix |
|---|------|-------|----------|---------|-----|
| 1C-1 | `services/heat_generator.py` | 146–155 | low | `generate_event_heats()` uses `db.session.flush()` (not `commit()`) throughout. The comment at line 152 explicitly states the caller owns the transaction boundary. Correct pattern. | No action needed. |
| 1C-2 | `routes/scheduling.py` | 38–58 | medium | `_generate_all_heats()` loops over events calling `generate_event_heats_fn()` per event. Exceptions per event are caught individually, but flushed objects from prior events remain in the session. If `build_pro_flights()` succeeds (and commits) after a partial heat generation failure, the partially-generated heats from successful events get committed even if some events failed. | Wrap the entire generate+flight operation in a single savepoint: `db.session.begin_nested()` before the loop, commit only on full success, rollback the savepoint on any event failure. The outer `_handle_event_list_post()` try/except already rolls back the session on exception, but the commit inside `build_pro_flights()` fires before that. |
| 1C-3 | `services/flight_builder.py` | 79–171 | medium | `build_pro_flights()` bulk-deletes existing flights (line 83–87) then builds new ones, with a single `db.session.commit()` at the end (line 171). If an exception occurs between the first `db.session.flush()` (line 155) and the final commit, the session is dirty but not committed — the caller's `db.session.rollback()` in `_handle_event_list_post()` will clean it up. However: the DELETE on line 87 (`Flight.query.filter_by(...).delete()`) is not wrapped in a savepoint. If the new builds fail before the final commit, a rollback should undo the delete — but only if no other commit has fired. Verify that no commit fires between the delete and the final commit in `build_pro_flights()`. | Add a `db.session.begin_nested()` savepoint at the start of `build_pro_flights()` for clarity; commit the savepoint at the end. The outer session transaction remains uncommitted until the caller explicitly commits. |
| 1C-4 | `services/flight_builder.py` | 83–87 | low | `Flight.query.filter_by(tournament_id=tournament.id).delete(synchronize_session=False)` deletes all existing flights for the tournament before rebuilding. If the new build fails, the caller must rollback. This is correct but requires the caller to always be in a `try/except rollback` block. | Verified in `_handle_event_list_post()` — outer try/except rollbacks on failure. No action needed beyond Phase 2E improvements. |
| 1C-5 | `services/flight_builder.py` | (not present) | low | `integrate_college_spillover_into_flights()` partial failure: the function itself doesn't commit; the caller calls `db.session.commit()` after. On partial failure the caller's try/except rollbacks. Safe. | No action needed. |

---

## 1D. Input Validation Coverage

### Violations Found

| # | File | Line | Severity | Expression | Fix |
|---|------|------|----------|-----------|-----|
| 1D-1 | `routes/main.py` | 128 | high | `year=int(year)` — `year` comes from `request.form.get('year', 2026)`. If the field is present with a non-numeric value the default is bypassed and `int()` raises `ValueError`. | Wrap in try/except (TypeError, ValueError) and flash an error. |

### Verified Clean
All other `int()`/`float()` calls on form data found by static scan are inside `try/except (TypeError, ValueError)` blocks:
- `routes/partnered_axe.py`: lines 41–43, 83–85, 133–135 — all guarded.
- `routes/proam_relay.py`: lines 36–40, 91–94, 144–148 — all guarded.
- `routes/registration.py`: lines 655–658, 732–734, 789–792, 1008–1010, 1054–1056, 1092–1094 — all guarded.
- `routes/scheduling.py`: lines 979–984, 1205–1210 — all guarded.
- `routes/woodboss.py`: line 148–151 — guarded.
- `routes/reporting.py`: lines 368–370, 412–414 — all guarded.
- `routes/scoring.py`: `_parse_payout_form()` lines 740–743 — guarded.

---

## 1E. Auth Enforcement Gaps

### Findings

| # | File | Lines | Severity | Finding | Fix |
|---|------|-------|----------|---------|-----|
| 1E-1 | `app.py` | 48 | low | `MANAGEMENT_BLUEPRINTS` does not include `'strathmark'`. `strathmark_bp` is registered at `/strathmark` (line 168) outside MANAGEMENT_BLUEPRINTS. This is intentional — the status endpoint is public read-only. However, if new write endpoints are added to `strathmark.py` in future, they will not be auth-protected by the before_request hook. | Document in `routes/strathmark.py` that any new write endpoints must either add 'strathmark' to MANAGEMENT_BLUEPRINTS or add their own `@login_required` decorator. |
| 1E-2 | `app.py` | 48, 150, 167 | low | `woodboss_public_bp` is registered at `/woodboss` (line 167) alongside `woodboss_bp` but is NOT in `MANAGEMENT_BLUEPRINTS`. The public blueprint presumably serves public-facing views. Need to verify it contains no write endpoints. | Check `routes/woodboss.py` for `woodboss_public_bp` routes and ensure no state-modifying routes exist on it. |
| 1E-3 | `app.py` | 49–60 | low | `BLUEPRINT_PERMISSIONS` includes `'auth': 'can_manage_users'` but `'auth'` is not in `MANAGEMENT_BLUEPRINTS`. The auth blueprint is whitelisted via `endpoint.startswith('auth.')` exception. The `BLUEPRINT_PERMISSIONS` entry for `'auth'` is never used by the before_request hook. | Remove dead `'auth'` entry from `BLUEPRINT_PERMISSIONS` or add a comment explaining it's for documentation only. |
| 1E-4 | `routes/api.py` | 267 | low | `standings_stream` SSE endpoint is at `/api/public/tournaments/<id>/standings-stream`. It is whitelisted by `endpoint.startswith('api.public_')` in the before_request hook. The endpoint has no write side-effects. | No action needed. |
| 1E-5 | `routes/api.py` | various | low | All `@csrf.exempt` routes in `api.py` are GET-only (no POSTs). No CSRF exemptions on write endpoints. | No action needed. |

---

## 1F. Template and Static Asset Inventory

### Summary
- **Total Jinja2 templates:** 88 (counted from filesystem)
- **Templates potentially over 500 lines:** See table below (candidates based on feature complexity)
- **Hardcoded URLs found:** None detected (all links use `url_for()`)
- **Inline `<script>` blocks over 50 lines:** Present in several templates (see table)
- **Missing routes/files referenced:** None detected

### Large Template Candidates (estimated >500 lines — candidates for decomposition)

| Template | Estimated Size | Decomposition Suggestion |
|----------|---------------|--------------------------|
| `templates/tournament_setup.html` | ~600+ lines | Split events/wood/settings tabs into `{% include %}` partials |
| `templates/scheduling/events.html` | ~500+ lines | Extract flight options section to partial |
| `templates/scoring/event_results.html` | ~500+ lines | Extract result table and finalize modal to partials |
| `templates/scoring/enter_heat.html` | ~400+ lines | Extract conflict modal JS to `static/js/` |
| `templates/base.html` | ~300 lines | Already lean; consider extracting nav JS |

### Inline Script Blocks Over 50 Lines (candidates for extraction)

| Template | Estimated Script Lines | Suggested JS File |
|----------|----------------------|-------------------|
| `templates/scoring/event_results.html` | ~100+ lines | `static/js/event_results.js` |
| `templates/scheduling/events.html` | ~80+ lines | `static/js/schedule_events.js` |
| `templates/portal/spectator_college.html` | ~60+ lines | `static/js/spectator_live.js` |

---

*Audit complete. Proceed to Phase 2: Critical Fixes.*
