# Print Hub + Pro Saturday Checkout Roster + Per-Event Results Print + Email Delivery — Implementation Plan

**Date:** 2026-04-21
**Branch:** main (feature branch to be created: `feat/print-hub-and-checkout-roster`)
**Requested by:** Alex (lead judge feedback from legacy judges)
**Review:** eng-review (this doc)

---

## Problem

Legacy judges are asking for paper printouts of tournament documents this year to integrate with their existing workflow. Four asks:

1. **Print Hub tab** — a single page that lists every printable document the app generates, with status dots (red/green for "configured") and a staleness indicator (was the underlying data updated since the doc was last printed).
2. **Pro Saturday checkout roster** — a simple printed sheet, one row per pro competitor, with name + events signed up for + notes box + "present" checkbox. Used by the lead judge on Saturday morning for check-in.
3. **Per-event results sheet** — one printable page per event, ranking every competitor with time/score, available (light turns green) as soon as the event is finalized, so the judge can walk a printed results sheet out to the spectator area for each event as it wraps.
4. **Email/PDF delivery** — send any document in the Print Hub to one or more recipients as a PDF attachment, directly from the Hub row. Lets judges who want the PDF on their phone or in their inbox receive it without finding a printer; also useful for post-event recap (mail a judge the full standings after the show).

---

## Scope

### In scope
- New route `/scheduling/<tid>/print-hub` (judge-only) that lists the printable documents with status dots and stale/fresh/never-printed indicators, plus a Print button per row that links to the existing print/PDF route. The catalog has two kinds of entries:
  - **Fixed docs** (15 existing + 1 new = 16): one row each, identified by `doc_key`.
  - **Dynamic docs** (new, 1 kind): expanded to one row per entity — currently only "Event Results" per event, which renders one row for every `Event` in the tournament. Each event row goes green once `event.is_finalized == True` and stays gated red while the event is in progress.
- New table `PrintTracker(tournament_id, doc_key, entity_id, last_printed_at, last_printed_fingerprint, last_printed_by)` with UNIQUE(tournament_id, doc_key, entity_id). `entity_id` is nullable — fixed docs store NULL, dynamic docs store the event_id (or other entity FK).
- New service `services/print_catalog.py` holding:
  - `PRINT_DOCUMENTS` — module-level list of `PrintDoc` and `DynamicPrintDoc` entries. Each has (label, section, endpoint, status_fn, fingerprint_fn). Dynamic entries additionally have an `enumerate_fn(tournament) -> list[entity]` that yields the per-row entities.
  - `PrintDocStatus(configured: bool, reason: str | None)`.
  - `record_print(doc_key, entity_id_kwarg=None)` decorator wrapping each existing print route. Fixed docs pass no entity; dynamic docs declare which kwarg (e.g. `event_id`) identifies the per-row entity so the decorator can persist it on the tracker row.
- Wrap all 15 existing print routes + 1 new route with `@record_print("<key>")`. The event-results print route declares `entity_id_kwarg="event_id"` so each event gets its own tracker row.
- New route `/scheduling/<tid>/pro/checkout-roster/print` + template `templates/scheduling/pro_checkout_roster_print.html`. Uses `services/print_response.py::weasyprint_or_html` for PDF-or-HTML.
- Sidebar entry: "Print Hub" under **Run Show** section, `bi-printer-fill` icon, links to the new hub page.
- **Email delivery:**
  - New service `services/email_delivery.py` — generic "email a rendered document" helper. Exposes `is_configured() -> bool`, `send_document(to, subject, body, attachment_bytes, attachment_name, attachment_mime) -> EmailResult`, and integrates with the existing `background_jobs.submit()` thread pool so sends are non-blocking.
  - **Refactor** `routes/reporting.py::_send_ala_email` to call the new service (DRY — eliminates the second SMTP path). Zero behavior change on the ALA route; regression tests on the ALA send still pass.
  - New route `POST /scheduling/<tid>/print-hub/email` — accepts `doc_key`, optional `entity_id`, `user_ids[]` (checkbox selection of existing User accounts with email) and `extra_emails` (comma-separated ad-hoc addresses). Renders the underlying print template, generates the PDF (or HTML fallback), hands off to `email_delivery.send_document()` via background job.
  - New table `PrintEmailLog(id, tournament_id, doc_key, entity_id, recipients_json, subject, sent_at, sent_by_user_id, status, error)` — every send attempt logged here AND to `AuditLog` via `log_action`. Status is `queued` / `sent` / `failed`.
  - UI: each Print Hub row gets an "Email" button alongside "Print". Button opens a Bootstrap modal with: checkboxes for existing Users that have an email, a free-text field for additional recipients (one per line), and a Send button. Button is disabled with tooltip if `email_delivery.is_configured()` is False.
  - Rate limit: `write_limit()` decorator, capped at 20/min per user (following the existing pattern in `routes/api.py`).
  - CSRF: standard form-POST, `{{ csrf_token() }}` in the modal form.
  - Optional hardening (flag, not default-on): `EMAIL_ALLOWED_DOMAINS` env var, comma-separated; if set, the send route rejects recipients outside those domains with a flash error. Leave unset for the 2026 season; turn on if we see ad-hoc abuse.
- Full test coverage — ~90 test cases across 6 new test files + regression pass on existing print-route tests + regression pass on the ALA email path.
- Alembic migration for `print_trackers` + `print_email_logs` tables. PG-safe (`op.create_table` + `op.create_index`, no batch_alter_table).

### NOT in scope (explicitly deferred — justification required if expanded)
- **Per-user print history / audit trail** for paper prints. Not asked for (emails DO get an audit trail — see `PrintEmailLog`). Can be layered on top of `PrintTracker.last_printed_by` later, but not in this PR.
- **Per-judge presets ("Jane always wants 7, 11, 12").** No evidence of demand.
- **Agent-native print discovery / JSON API of catalog.** The catalog is Python. If an agent needs it, expose a `/api/public/print-catalog` later.
- **Adding `updated_at` columns to domain models** (Heat, Event, ProCompetitor, etc.). Deferred — a multi-migration refactor with no payoff for this feature. Fingerprint functions handle staleness without it.
- **Bulk "print everything" button.** Judges want to pick what they print; bulk is a paper-waste hazard.
- **Sorting/filtering the hub list.** 16 rows grouped into 4 sections. Readable as-is; sorting UI is premature.

### What already exists (reuse, don't rebuild)
- `services/print_response.py::weasyprint_or_html` — already handles PDF-or-HTML fallback.
- 15 existing print routes + 15 `*_print.html` / `*_print_pdf.html` templates — zero changes to any of them except adding one decorator line.
- Sidebar + judge auth (`require_judge_for_management_routes` in `app.py`) — Print Hub uses the same gate by joining `scheduling` blueprint.
- Tournament navigation + `_sidebar.html` — new link drops in.
- `services/audit.py::log_action` — writes `AuditLog` rows. Used for email sends.
- **SMTP pipeline** in `routes/reporting.py::_send_ala_email` — reads `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` env vars; uses `smtplib.SMTP.starttls()`. Refactor into `services/email_delivery.py` so both the ALA flow and the new Print Hub email flow share one code path. Env vars unchanged — no new secrets to rotate, no Railway re-config needed.
- `services/background_jobs.py` — thread pool executor with `submit()` + app context. SMTP sends run here, not inline in the request.
- `models/user.py::User.email` — already present. The "pick a recipient" modal reads from existing User rows with non-null email. Admins/judges already have accounts.

---

## Architecture

### Data model

```
+---------------------------------------------------+
|  print_trackers (NEW)                             |
|---------------------------------------------------|
|  id                 INTEGER PK                    |
|  tournament_id      INTEGER NOT NULL FK           |
|  doc_key            TEXT NOT NULL                 |
|  entity_id          INTEGER NULL                  |
|  last_printed_at    DATETIME NOT NULL             |
|  last_printed_fingerprint  TEXT NOT NULL          |
|  last_printed_by_user_id   INTEGER NULL FK users  |
|  UNIQUE(tournament_id, doc_key, entity_id)        |
+---------------------------------------------------+

+---------------------------------------------------+
|  print_email_logs (NEW)                           |
|---------------------------------------------------|
|  id                 INTEGER PK                    |
|  tournament_id      INTEGER NOT NULL FK           |
|  doc_key            TEXT NOT NULL                 |
|  entity_id          INTEGER NULL                  |
|  recipients_json    TEXT NOT NULL                 |
|  subject            TEXT NOT NULL                 |
|  sent_at            DATETIME NOT NULL             |
|  sent_by_user_id    INTEGER NULL FK users         |
|  status             TEXT NOT NULL                 |  -- queued / sent / failed
|  error              TEXT NULL                     |
|  INDEX (tournament_id, sent_at DESC)              |
+---------------------------------------------------+
```

Cascade: `ON DELETE CASCADE` on `tournament_id` (drop tournament → drop trackers). `last_printed_by_user_id` is `SET NULL` on user delete (user cleanup should not lose the fact of the print). `entity_id` is **not** a foreign key — dynamic docs point at different entity tables (events today, potentially heats or competitors later) and a real FK would force a type column. The decorator is responsible for only writing entity_ids that still exist. If an event is deleted, its tracker rows linger as orphans until the tournament is removed — acceptable, since PrintTracker is historical bookkeeping.

**Note on UNIQUE with nullable entity_id:** SQLite treats NULL as distinct in UNIQUE constraints, PostgreSQL does too by default. For fixed docs that always store NULL, this means duplicate NULL rows could theoretically be inserted. The upsert helper must handle fixed-doc rows by querying on `(tournament_id, doc_key)` when entity_id is None, and on `(tournament_id, doc_key, entity_id)` when set. This is tested.

### Module map

```
models/print_tracker.py           (~60 LOC)
models/print_email_log.py         (~40 LOC)
services/print_catalog.py         (~350 LOC — registry + 16 status_fn + 16 fingerprint_fn + decorator)
services/email_delivery.py        (~180 LOC — SMTP send, is_configured, background_jobs integration)
routes/scheduling/print_hub.py    (~120 LOC — hub GET + email POST)
templates/scheduling/print_hub.html (~180 LOC)
templates/scheduling/_email_modal.html (~60 LOC)
templates/scheduling/pro_checkout_roster_print.html (~90 LOC)
routes/scheduling/pro_checkout_roster.py (~50 LOC)
migrations/versions/<rev>_add_print_trackers_and_email_logs.py (~70 LOC)

Modifications (each: 1-line decorator addition):
  routes/scheduling/heat_sheets.py       (2 decorators: heat_sheets, day_schedule_print)
  routes/scheduling/friday_feature.py    (2 decorators: friday_feature_print, friday_feature_pdf)
  routes/scheduling/birling.py           (2 decorators: birling_print_blank, birling_print_all)
  routes/scoring.py                      (3 decorators: heat_sheet_pdf, judge_sheet_for_event, judge_sheets_all)
  routes/reporting.py                    (5 decorators + ALA email refactor:
                                          college_standings_print, event_results_print,
                                          pro_payout_summary_print, all_results_print, ala_membership_report_pdf;
                                          _send_ala_email body replaced with a call to
                                          services/email_delivery.py::send_document)
  routes/registration.py                 (1 decorator: gear_sharing_print)
  routes/woodboss.py                     (1 decorator: report_print)
  templates/_sidebar.html                (add Print Hub link under Run Show section)
```

### Fingerprint strategy

Each `fingerprint_fn(tournament)` returns a short deterministic string:

```python
def _fp_heat_sheets(tournament):
    # Include: number of heats, latest heat status, tournament.updated_at,
    # count of scratched pros (scratches hide heat rows).
    heats = Heat.query.join(Event, Heat.event_id == Event.id) \
        .filter(Event.tournament_id == tournament.id).all()
    scratched = ProCompetitor.query.filter_by(
        tournament_id=tournament.id, status='scratched').count()
    payload = f"{len(heats)}|{tournament.updated_at.isoformat()}|{scratched}|" + \
        "|".join(f"{h.id}:{h.status}:{h.flight_id}" for h in sorted(heats, key=lambda x: x.id))
    return hashlib.sha1(payload.encode()).hexdigest()[:16]
```

**Design rule (enforced in code review):** every `fingerprint_fn` must include the full set of data that determines the rendered output — not just top-level counts. A fingerprint that misses `Heat.flight_id` would falsely report "fresh" after flights are rebuilt. Tests enforce this per-doc.

### Why fingerprints, not timestamps

Domain models (Heat, Event, ProCompetitor) do NOT have `updated_at` columns today. Only Tournament and User do. Adding `updated_at` across 10+ models is a multi-migration refactor with real schema-drift risk (see MEMORY.md open tech debt #8 — 58 drift entries still to retire). A fingerprint approach sidesteps the schema change entirely.

Trade-off: fingerprints are coarser. A fingerprint that changes but doesn't actually affect the rendered print will show a false-positive "stale" badge, driving a re-print. For a race-day paper tool, false-positive stale (judge reprints unnecessarily) is strictly better than false-negative fresh (judge trusts a stale printout and misassigns a competitor). We fail safe on the paper side.

### Decorator semantics

```python
def record_print(doc_key: str):
    def wrap(view):
        @functools.wraps(view)
        def inner(*args, **kwargs):
            response = view(*args, **kwargs)
            # Response generated successfully. Now write tracker row.
            try:
                _upsert_tracker(doc_key, kwargs.get('tournament_id'))
            except Exception:
                logger.exception(
                    'PrintTracker upsert failed (non-fatal) doc=%s tid=%s',
                    doc_key, kwargs.get('tournament_id'))
            return response
        return inner
    return wrap
```

Rules (tested):
1. Decorator runs AFTER the view returns — if view raises, no tracker row is written.
2. Tracker write failure is swallowed — an audit bookkeeping failure must never break the print itself. Judges need the paper; they do not need the history.
3. User attribution uses `flask_login.current_user.id` when authenticated; NULL otherwise.
4. Decorator reads `kwargs['tournament_id']` for the tournament scope — standard across existing print routes, so no inference ambiguity.

### ASCII state machine for a doc row on the Hub

```
                         +------------------+
                         |  Not Configured  |   (red dot, "Print" disabled, reason tooltip)
                         +------------------+
                                  |
              configured=True     |    configured=False (after scratching events etc.)
                                  v
                         +------------------+
                         | Configured +     |   (green dot, Print enabled,
                         | Never Printed    |    "Never printed" badge)
                         +------------------+
                                  |
                          print route hit
                                  v
                         +------------------+
                         | Configured +     |   (green dot, Print enabled,
                         | Fresh            |    "Printed X min ago" badge)
                         +------------------+
                                  |
                 fingerprint drift (data changed since print)
                                  v
                         +------------------+
                         | Configured +     |   (green dot with yellow ring, Print enabled,
                         | Stale            |    "STALE — data changed since last print" badge)
                         +------------------+
```

---

## Email delivery architecture

### Flow

```
  User clicks Email on a Print Hub row
          |
          v
  Bootstrap modal opens:
   - checkbox list of Users with email
   - free-text "additional recipients" box
   - Send button (CSRF token)
          |
          v (POST /scheduling/<tid>/print-hub/email)
  routes/scheduling/print_hub.py::email_document
   1. Validate doc_key + optional entity_id against catalog
   2. Validate recipients (non-empty, valid email format,
      EMAIL_ALLOWED_DOMAINS if set)
   3. Render the document's print template to a string
   4. Convert to PDF via weasyprint_or_html; if HTML fallback
      (WeasyPrint missing), attach as .html with a note
   5. Write PrintEmailLog row with status='queued'
   6. Submit to background_jobs.submit(send_email_task,
      log_id=log.id, ...)
   7. Flash "Email queued — N recipients" and redirect to Hub
          |
          v (worker thread)
  services/email_delivery.py::_worker_send
   1. Open SMTP connection, starttls, login
   2. Send message
   3. On success: update PrintEmailLog row to status='sent'
      AND write AuditLog row 'email_sent'
   4. On failure: update PrintEmailLog status='failed' with
      error text, write AuditLog 'email_failed'
   5. Close connection
```

### services/email_delivery.py API (locked)

```python
from dataclasses import dataclass

@dataclass
class EmailResult:
    status: str          # 'sent' or 'failed'
    error: str | None

def is_configured() -> bool:
    """True iff SMTP_HOST, SMTP_USER, SMTP_PASSWORD are set."""

def validate_recipients(recipients: list[str]) -> tuple[list[str], list[str]]:
    """Returns (valid, invalid). Invalid includes malformed addresses and
    addresses not in EMAIL_ALLOWED_DOMAINS when set."""

def send_document(
    to: list[str],
    subject: str,
    body: str,
    attachment_bytes: bytes,
    attachment_name: str,
    attachment_mime: str = 'application/pdf',
) -> EmailResult:
    """Synchronous send. Used directly by the background job worker.
    Routes should NOT call this synchronously — use queue_document_email."""

def queue_document_email(
    tournament_id: int,
    doc_key: str,
    entity_id: int | None,
    recipients: list[str],
    subject: str,
    body: str,
    attachment_bytes: bytes,
    attachment_name: str,
    sent_by_user_id: int | None,
) -> int:
    """Writes PrintEmailLog (status='queued'), submits to background_jobs.
    Returns the log id for UI follow-up (e.g., status page)."""
```

**Design rules:**
1. `send_document` is pure SMTP — no DB writes. Stateless. Easy to mock in tests.
2. `queue_document_email` is the only public entry point from route handlers. This ensures every send attempt is logged.
3. Background job failures write `status='failed'` AND `AuditLog 'email_failed'`. Dual logging is intentional — AuditLog is the admin-visible timeline; PrintEmailLog is the per-tournament status view.
4. SMTP credentials NEVER appear in flash messages or logs. The existing ALA path has a minor leak (flashes `f'Email failed: {exc}'`) — fix as part of the refactor.

### UI: email modal

```
+----------------------------------------+
|  Email: Heat Sheets                    |
|----------------------------------------|
|  Select recipients:                    |
|                                        |
|   [x] Alex Kaper  (alex@example.com)   |
|   [ ] Jane Judge  (jane@example.com)   |
|   [ ] Tim Timer   (tim@example.com)    |
|                                        |
|  Additional emails (one per line):     |
|  +------------------------------+      |
|  | scorekeeper@paper.org        |      |
|  |                              |      |
|  +------------------------------+      |
|                                        |
|  Subject preview:                      |
|  "Missoula Pro-Am 2026 — Heat Sheets"  |
|                                        |
|         [Cancel]  [Send]               |
+----------------------------------------+
```

No rich-text body editor — not worth the complexity. Subject is auto-generated, body is a one-paragraph template with tournament + doc name + generated-at timestamp + sender name.

### Rate limiting

`write_limit()` decorator (see `routes/api.py::_init_write_limiter`), capped at **20 sends per minute per user**. A judge fat-fingering Send 10 times in 30 seconds is realistic; 20+ in a minute is abuse or a stuck button.

### Optional domain allowlist

Env var `EMAIL_ALLOWED_DOMAINS` — comma-separated list (e.g. `missoulaproam.org,university.edu`). When set, recipients NOT in the allowlist are rejected at `validate_recipients()` time with a clear flash error ("jane@external.com is outside the allowed domains list"). Default: unset (permissive). Turn on as hardening if we see ad-hoc abuse, not pre-emptively.

### What gets attached when WeasyPrint is missing

On Railway production, WeasyPrint is NOT bundled (see `services/print_response.py` notes). The email send path must handle this:

- **PDF branch (WeasyPrint available):** attach `heat_sheets.pdf`, `Content-Type: application/pdf`.
- **HTML branch (Railway prod):** attach `heat_sheets.html`, `Content-Type: text/html`, AND prepend a note to the email body: "NOTE: this attachment is HTML. Open it in a browser and use File → Print to produce a PDF. (This deployment does not bundle the PDF generator.)"

This makes the failure mode visible to the recipient instead of silently delivering a file they can't print cleanly. Documented in the UI tooltip: hover on the Email button shows "Attachments sent as HTML on Railway (PDF requires local WeasyPrint install)."

---

## UI: Print Hub page

Single `<table class="table">`, grouped by section via `<tbody>` with an `<th colspan>` section header per group. Four sections matching the sidebar taxonomy:

- **Setup** — Woodboss report, Day schedule, Friday Night Feature (print + pdf).
- **Run Show** — Heat sheets, Judge sheets (per event + all), Single heat PDF, Birling (blank + seeded), Gear sharing roster, **Pro Checkout Roster (NEW)**.
- **Results** — College standings, All results (combined), Pro payouts, **Event Results (one row per event, dynamic)**.
- **Compliance** — ALA report.

**Event Results section (dynamic).** For every `Event` in the tournament, render one row: "Event Results — {Event.display_name}". Grouped visually under a sub-header within the Results section. Green dot appears only when `event.is_finalized == True` (the canonical "scoring is locked, payouts distributed" signal). Before finalization: red dot, tooltip "Event not finalized yet." Print button disabled. Once finalized, the Print button links to the existing `/reporting/<tid>/event/<eid>/results/print` route. Ordering: day-of-show order (college events first if Friday, pro events first if Saturday, alphabetical within day) — matches what the judge expects when pulling prints in the order events finish.

Each row:

```
| Document Name                 | Status       | Last Printed              | Actions         |
|-------------------------------|--------------|---------------------------|-----------------|
| (green dot) Heat Sheets       | Fresh        | 15 min ago by Alex        | [Print] [Email] |
| (yellow dot) Pro Payouts      | STALE        | 2 hours ago by Alex       | [Print] [Email] |
| (green dot) Gear Sharing      | Never printed| —                         | [Print] [Email] |
| (red dot) Birling (seeded)    | Not ready    | — (tooltip: "no seedings")|    —            |
```

Email button is disabled (with tooltip "SMTP not configured") when `email_delivery.is_configured()` is False. Click opens the email modal (Bootstrap modal, lazy-loaded partial `_email_modal.html`, posts to `/scheduling/<tid>/print-hub/email`).

No JavaScript beyond the existing Bootstrap tooltip and modal init. Page is fully server-rendered; the modal is a hidden form that becomes visible on button click.

---

## Checkout Roster

Template `pro_checkout_roster_print.html` — standalone (NOT extending base.html), inline CSS, `@page` landscape A4, auto-print via nonce-injected `addEventListener`. Follows the pattern of `templates/scheduling/friday_feature_print.html` exactly.

Layout:

```
+------------------------------------------------------------------+
|  Missoula Pro-Am 2026 — Pro Saturday Checkout                    |
|  Printed: 2026-04-25 07:52  (Judge: Alex Kaper)                  |
+------------------------------------------------------------------+
| # | Name              | Events Entered        | Notes       | ☐ |
|---|-------------------|-----------------------|-------------|---|
| 1 | Alex Kaper        | SB, UH, Standing Blk  |             | ☐ |
| 2 | Ben Jones         | 1-Board, Single Buck  |             | ☐ |
| 3 | Casey Smith       | SB, Double Buck, JJ   |             | ☐ |
...
+------------------------------------------------------------------+
```

Event names: joined with `, `. Uses abbreviated names if available (e.g. "SB" for Springboard) to fit on one line; fall back to full name if no abbreviation configured. Use existing event `display_name` property.

Landscape orientation required — with ~10 events a pro might enter, the Events column needs horizontal space. Portrait truncates.

Sort: alphabetical by `name`. Status filter: `status != 'scratched'`. Include left-handed springboard cutters — the judge does checkout, not flight placement.

---

## Test Plan

### Coverage target

100% of new code paths. See the coverage diagram in the eng review for the full path-by-path list. Summary:

- `tests/test_print_catalog.py` (~55 parametrized tests — 16 fixed docs + 1 dynamic doc × 3 states each)
- `tests/test_print_tracker_model.py` (~10 tests: uniqueness with and without entity_id, cascade, upsert semantics for both fixed and dynamic rows, null-user attribution)
- `tests/test_print_hub_route.py` (~14 tests: auth, all state transitions, empty tournament, dynamic-event rows with `is_finalized` gating, event-order within section)
- `tests/test_pro_checkout_roster.py` (~8 tests: sort order, scratched exclusion, WeasyPrint path, HTML fallback, empty list, auth, tracker write, content-disposition filename)
- `tests/test_print_decorator_coverage.py` (~1 test: AST scan of `routes/` verifies every `*_print` / `*_pdf` route has `@record_print`)
- `tests/test_email_delivery.py` (~14 tests — service layer):
  - `is_configured()` returns True/False based on env vars (parametrize 4 combos)
  - `validate_recipients()` accepts well-formed addresses
  - `validate_recipients()` rejects malformed addresses
  - `validate_recipients()` with `EMAIL_ALLOWED_DOMAINS` set rejects out-of-domain
  - `validate_recipients()` with domain allowlist unset is permissive
  - `send_document()` success path (SMTP mocked) returns `status='sent'`
  - `send_document()` SMTP auth failure returns `status='failed'` with error
  - `send_document()` network timeout returns `status='failed'`
  - `send_document()` never leaks SMTP credentials in error messages
  - `send_document()` attaches PDF with correct filename + MIME
  - `send_document()` falls back to HTML attachment when mime is text/html
  - `queue_document_email()` writes PrintEmailLog row with status='queued'
  - `queue_document_email()` submits to background_jobs
  - `queue_document_email()` worker path updates PrintEmailLog to status='sent' on success
- `tests/test_print_hub_email_route.py` (~10 tests):
  - GET Print Hub renders Email buttons enabled when configured, disabled when not
  - POST /print-hub/email with valid recipients → 302 + flash + PrintEmailLog row
  - POST /print-hub/email with 0 recipients → 400 + flash error
  - POST /print-hub/email with malformed email → 400 + flash error
  - POST /print-hub/email when SMTP not configured → flash error, no send
  - POST /print-hub/email with `EMAIL_ALLOWED_DOMAINS` rejects out-of-domain
  - POST /print-hub/email records AuditLog entry
  - POST /print-hub/email respects rate limit (20/min)
  - POST /print-hub/email requires is_judge (regression: 403 for non-judge)
  - Existing ALA email route still works after refactor (regression)
- `tests/test_print_email_log_model.py` (~5 tests): CRUD, cascade on tournament, status enum values, sent_by_user_id SET NULL on user delete, index exists

**Critical dynamic-doc tests:**
- Event row red before `is_finalized = True`, Print button disabled, reason "Event not finalized yet."
- Event row goes green the moment `is_finalized` flips; Print button becomes enabled.
- After print, that event's tracker row is written with `entity_id = event.id`.
- Rescoring a finalized event (if allowed) updates the fingerprint; row renders "STALE — data changed since last print."
- Deleting an event removes it from the hub listing.
- Tournament with 0 events → Event Results sub-header renders an empty-state message, no 500.

### Regression pass (critical)

Re-run every existing print-route test after decorating its route. Any divergence in response body, Content-Type, Content-Disposition, or status code is a blocker. The decorator MUST be body-transparent.

### Specific regression tests

```python
def test_record_print_does_not_mutate_response_body(client, seeded_tournament):
    # Golden-master the /heat-sheets response body before and after decorator
    # application. SHA-256 must match.

def test_record_print_swallows_tracker_failure(client, seeded_tournament, monkeypatch):
    # Force PrintTracker.upsert to raise. Assert print response still returns 200.

def test_failed_render_does_not_write_tracker(client, seeded_tournament, monkeypatch):
    # Force the view to raise. Assert no PrintTracker row is created.

def test_fingerprint_changes_after_scratch(client, seeded_tournament):
    # Compute heat-sheets fingerprint. Scratch a pro. Recompute. Assert different.

def test_pro_checkout_roster_excludes_scratched(client, seeded_tournament):
    # Scratch one pro. GET the print route. Assert response body does not contain
    # that pro's name.
```

### Migration safety

Migration file must pass `pytest tests/test_pg_migration_safety.py`. No `batch_alter_table`, `server_default='0'` on a boolean, or `PRAGMA`. Use `op.create_table` + `op.create_index` directly. Downgrade drops the table.

---

## Failure modes

For each new codepath, one realistic production failure scenario + whether tests/handling cover it:

| Codepath                      | Failure Mode                                     | Test? | Handled? | User sees? |
|-------------------------------|--------------------------------------------------|-------|----------|------------|
| `record_print` decorator      | PrintTracker.upsert raises IntegrityError        | YES   | YES (swallow + log) | Nothing — print succeeds |
| `record_print` decorator      | `current_user` is AnonymousUserMixin             | YES   | YES (NULL user_id) | Nothing |
| `fingerprint_fn` per doc      | Underlying table is empty                        | YES   | YES (returns sha1 of empty payload) | Fresh/stale correct |
| `status_fn` per doc           | Raises (e.g., WoodConfig missing)                | YES   | YES (returns configured=False) | Red dot + reason |
| `print_hub` route             | 0 pros, 0 heats, 0 results (brand-new tournament)| YES   | YES | All red dots |
| `pro_checkout_roster` route   | WeasyPrint not installed                         | YES   | YES (HTML fallback via print_response) | HTML page, browser-printable |
| `pro_checkout_roster` route   | 0 pros                                           | YES   | YES | Empty roster page |
| `pro_checkout_roster` route   | One pro has 0 events entered                     | YES   | YES | Events column empty, row still renders |
| Email modal                   | SMTP env vars missing                            | YES   | YES (button disabled, tooltip reason) | Disabled button, clear tooltip |
| Email POST                    | 0 valid recipients                               | YES   | YES (400 + flash)                      | Flash error, stay on Hub |
| Email POST                    | SMTP auth failure at send time                   | YES   | YES (queued row → status='failed', AuditLog, email NOT sent) | Judge sees failed status in Hub after retry-check |
| Email POST                    | SMTP network timeout                             | YES   | YES (exception caught, status='failed', logged) | Same |
| Email POST                    | WeasyPrint missing on Railway                    | YES   | YES (attach as .html with recipient-facing note) | Recipient sees HTML attachment + explanation |
| Email POST                    | Rate limit exceeded                              | YES   | YES (429, flash "too many emails sent — wait") | Judge rate-limited with clear message |
| Email POST                    | Domain allowlist blocks a recipient              | YES   | YES (400 + flash with domain reason)   | "jane@external.com outside allowed domains" |
| Email POST                    | SMTP credentials leak into flash                 | YES   | YES (sanitized message — no password substring check in test) | Generic "send failed" |
| Background job                | Flask app context not available in worker        | YES   | YES (background_jobs already wraps with current_app._get_current_object) | Worker runs, DB commits |
| Background job                | DB connection dropped mid-send                   | partial | YES (log failed, retry once before giving up) | Stays in status='failed' |
| `PrintEmailLog`               | Retention growth                                 | NO    | Accepted (table is cheap; prune later if needed) | No user-facing impact |

**Critical gaps:** None. Every failure mode has a test AND error handling AND a clear-not-silent user outcome.

---

## TODOS (proposed)

- **TODO-A (defer):** Extend `@record_print` to write AuditLog rows so admins can see a "who printed what, when" history. ~30 min work. Worth it AFTER we see how judges use the hub — if they don't care, don't build it.
- **TODO-B (defer):** Add a `/api/public/print-catalog` JSON endpoint for agent-native discovery. Wait for demand.
- **TODO-C (defer):** Add a "bulk print selected" checkbox flow. Could cause paper waste; ask a judge before building.
- **TODO-D (defer):** "Email history" tab in Print Hub that lists recent `PrintEmailLog` entries with status. Currently queryable only via direct SQL / AuditLog. Build the UI when judges ask, "did that email actually go?"
- **TODO-E (defer):** Distribution-list feature — save frequently-used recipient groups ("all judges", "ALA board"). For 2026, just type or check-select each time.
- **TODO-F (defer):** Retention policy for `PrintEmailLog` — prune rows older than 1 year. Deferred until the table actually grows.

---

## Parallelization

Mostly sequential within a single PR — the print catalog, tracker, and sidebar changes all touch shared surface. BUT email delivery is genuinely independent and can be built in parallel once the catalog exists:

| Lane | Modules touched | Depends on |
|------|----------------|------------|
| A (catalog core) | models/print_tracker.py, services/print_catalog.py, routes/scheduling/print_hub.py (GET), templates/scheduling/print_hub.html, sidebar, existing print-route decorators, migration (trackers) | — |
| B (checkout roster) | routes/scheduling/pro_checkout_roster.py, templates/scheduling/pro_checkout_roster_print.html, registered in catalog as a fixed doc | A (needs catalog DS) |
| C (email delivery) | models/print_email_log.py, services/email_delivery.py, routes/scheduling/print_hub.py (POST), templates/scheduling/_email_modal.html, ALA refactor, migration (email logs) | A (needs catalog + Print Hub page to wire the button) |

**Execution:** build A first (core). Once A lands on the branch, B and C can be developed in parallel worktrees — B touches roster-only surface, C touches email-only surface. Merge C after B so the email modal can reference the checkout roster doc_key in its tests.

**Conflict flag:** both B and C touch the migration chain. Land them sequentially on the branch even if developed in parallel. Alembic won't forgive two new revisions both pointing to the same down_revision.

Estimated ~7-10 hours with gstack for the full three-lane plan (A: 4h, B: 2h, C: 3h).

---

## Open decisions

1. **Icon for the sidebar link** — `bi-printer-fill` (filled) vs `bi-printer` (outline). Default: filled, to distinguish from existing `bi-printer` "Heat Sheets" link.
2. **Section taxonomy** — proposal above matches sidebar (Setup / Run Show / Results / Compliance). Alternative: just one flat list sorted alphabetically. Go with the section grouping — matches the judge's mental model of the app.
3. **Staleness threshold** — right now, a fingerprint mismatch is "stale." No "warn after 1 hour of staleness" threshold. Keep simple; add if judges report false-positive fatigue.
4. **Email domain allowlist default** — unset (permissive) by default for 2026 season. Easy to turn on via `EMAIL_ALLOWED_DOMAINS` if abuse appears. Alternative: ship with an allowlist that includes the organization's primary domain and require the env var to be cleared for ad-hoc sends. Going with permissive-by-default because the user base is small and known.
5. **Background job vs sync SMTP** — going async via `background_jobs.submit()` so the Hub UI doesn't freeze for 3-5 seconds on send. Alternative: sync send with spinner (simpler, one less moving part). Async wins because race-day judges cannot have a frozen UI.
6. **Attachment format on Railway** — HTML attachments when WeasyPrint is missing, with a recipient-facing note in the email body explaining "open in browser and print." Alternative: install WeasyPrint on Railway (changes prod dependency surface, not small). Defer that decision — the HTML fallback is acceptable for now.
