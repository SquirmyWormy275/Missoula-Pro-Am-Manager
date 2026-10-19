# Security controls and rollout

Release 2.14.17 addresses the October 19, 2026 Security Cloud findings for revision
`3a61ec7` with stronger account provisioning, access controls, resource limits and
transport checks. Existing administrator credentials, stored portal PINs and
tournament results are preserved. This release adds no schema migration.

## Account and portal provisioning

The first administrator is created with `flask auth create-admin` on the trusted
server console after `flask db upgrade`. HTTP bootstrap cannot create an account.
Existing administrator accounts are unchanged. The command refuses to provision
when users already exist.

Manage Users provides **Provision competitor or school PIN** for the selected
tournament. An administrator verifies identity and authority in person before
generating an eight-digit random PIN for an exact competitor or school. The PIN
is displayed only in the no-store response and must be handed to that person.
The generated PIN is hashed in the existing credential field. No first-claim
capability remains on public routes. Replacement invalidates earlier portal
sessions using a keyed fingerprint of the current PIN hash. Existing portal
sessions must unlock once with their current PIN after deployment; stored PINs
and administrator passwords are unchanged.

A disabled user is rejected by the loader and the management request gate.
The scan's claim that Flask-Login 0.6.3 treats inactive users as authenticated was
not accurate: its UserMixin already returns `is_active` from `is_authenticated`.
The new explicit checks are defense in depth, with a regression verifying that
an existing disabled-user session cannot enter management routes.

Only judges/admins may apply or remove team validation overrides; a reason is
required and committed to the existing actor-attributed audit log.

## Admission limits

Flask-Limiter 4.1.1 is required and decorators bind before app initialization.
Authentication attempts have source and target limits; changing IP addresses
cannot evade the account/profile bucket. Management limits identify the user.
Expensive report exports allow three per minute and thirty per hour per route;
print email allows three per minute and twenty per hour.

The supported deployment remains one Gunicorn worker with four request threads,
as configured in Procfile and enforced by the background-job process contract.
The default in-memory limiter is shared by those threads. Configure
`RATELIMIT_STORAGE_URI` with a shared supported backend **before adding replicas**;
install that backend's client dependency at the same time. Restarting the worker
resets in-memory rate windows. No production setting is changed by this patch.

The unused long-lived SSE endpoint responds immediately with 410 (404 for an
unknown tournament). The spectator UI already polls standings every thirty
seconds and continues to do so. No request thread waits for public streams.

Background work admits at most sixteen queued/running jobs, two per user and
four per tournament. Saturation returns 503 instead of growing an executor queue.
Terminal job records expire after twenty-four hours and are capped at one
thousand, pruning on subsequent admission. Existing export artifact pruning is
retained. SMTP submissions never fall back to an immediate send when admission
fails. Each message has at most ten recipients and a five-MiB attachment; domain
restrictions in `EMAIL_ALLOWED_DOMAINS` are enforced by the shared send service.

## Files and transport

XLSX validation checks archive entry count (256), expanded bytes (20 MiB), member
bytes (8 MiB), compression ratio (100:1), worksheet dimensions (10,000 rows and
100 columns), and cell/string/row node count (100,000 per XML member) before
pandas/openpyxl parsing. Defused XML parsing rejects entity expansion, including
UTF-16 declarations. Oversized workbooks receive an actionable import error.
Original college/pro uploads are unlinked in a finally block on success and
failure. The pro review JSON/report uses one slot per operator/tournament rather
than accumulating a fresh UUID on each upload. Existing retained uploads are
not deleted by this change; operators may review historical files separately.

All three XLSX export writers force text cells to literal string type, including
formula-leading names, schools, event labels, and tournament titles. Numeric
results stay numeric. SMTP STARTTLS uses a default verifying SSL context and
reissues EHLO before login. Certificate failures do not reach login or delivery.
PostgreSQL backup preserves configured libpq TLS options in the child environment
and fails on unsupported options. Passwords remain out of command arguments;
raw pg_dump errors are not surfaced. CSRF failures only redirect to safe local
paths.

## Validation

`tests/test_security_cloud_regressions.py` exercises hostile account claiming,
provision/unlock/reset workflows, account disablement, rotating-IP throttling,
judge-only overrides, retired SSE, malicious Referer values, oversized/XML
workbooks, literal spreadsheet cells, TLS failure, backup options and bounded
job admission. Existing import, export, email, authorization and job tests cover
ordinary flows. No production data is used.
