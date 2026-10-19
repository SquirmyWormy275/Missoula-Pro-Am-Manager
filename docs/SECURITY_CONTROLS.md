# Security controls and rollout

Release 2.14.19 adds the final verification-scan repairs to the existing account,
access-control, resource-limit and transport protections. Existing administrator credentials, stored portal PINs and
tournament results are preserved. This release adds no schema migration.

## Final verification follow-up (2.14.19)

Relay payout changes require a judge or administrator, including when a scorer
has a valid form token and current state digest. Scorers can read the amounts;
the form explains that only judges and administrators can edit them. Ordinary
scoring, relay operation and scheduling permissions remain unchanged.

The anonymous handicap-input feed publishes only completed chopping events and
completed result rows, matching public historical results. Staff exports retain
the complete operational data. This changes publication only; stored scores,
placements and payouts are not rewritten.

A staff account assigned to a tournament can use management routes only for that
tournament. This applies before route reads, schedule locks and mutations, and
also before shadow preparation or signed provider requests. An account with no
tournament assignment remains global within its role. Existing assignments are
honored without migrating accounts or changing roles. Public spectator access
is unchanged, and the shadow workflow remains separate from official scoring.

The legacy calculator dependency and the installed shadow-contract rehearsal use
the same immutable STRATHMARK 3.0.0rc14 source,
`c382b3f4e719a8621a373e3fa2ec2400bdb9ecd4`. The published shadow contract and
official mark calculations retain their existing behavior. Existing saved shadow
runs keep their frozen model, source and contract identities.

## Verification follow-up (2.14.18)

Flight holding-bin labels render stored names as literal text. Content security
policy also restricts form destinations to this application and disallows base
URL overrides. Language switches accept only safe local redirect paths.

Scorers retain score entry and scheduling. Entry-fee configuration and payment
tracking require an administrator, judge or registrar. Payout configuration and
templates require a judge or administrator; the existing settlement toggles
remain administrator-only. Financial controls are disabled for read-only roles.

Both competitor PIN entry paths use the same current-hash session proof and
same target rate bucket. A replaced PIN revokes the older personal-results
session too. A legacy Boolean session must unlock once with the existing PIN.

Public event results build only the requested ranking or heat view, batch heat
rosters, and cache plain results for five seconds on SQLite. The kiosk fetches
only ten earners and batches active heats. PostgreSQL retains its existing
uncached behavior to avoid cross-worker stale results. Both routes allow thirty
requests per minute per source, comfortably above the display refresh cadence.

Offline score pages require explicit preparation by the current account. General
score-page HTML caches are retired. Logout and account changes invalidate prepared
pages and lock already-open pages from the previous account. Pending scores stay
on this device, but queue display, export, import, removal and replay belong to
the original account. Sign back in as that account to reconcile them. Scores
without an original account binding are retained for manual recovery, never
silently assigned to the new account. Do not clear browser site data before all
scores are reconciled. After this update, prepare offline pages again while
online; existing pending scores are preserved.

Repair-script follow-up instructions refer to the privately configured
`DATABASE_URL` instead of printing credentials or URL query parameters.

The optional production audit runs only from `main`, uses a minimal, completely
hash-locked wheel set (`requirements-audit.lock`, Python 3.10/Linux x86_64), and
installs no application or model plugins. Secret-bearing workflows pin actions
to commit IDs. Backup PostgreSQL server and clients use the same official 18.6
image digest; age 1.3.2 is verified against its release SHA-256 before execution.
A separate CI job rehearses dump, encryption, decryption and restore using only
disposable synthetic data. Updating these pins requires reviewing the official
release provenance and rerunning that rehearsal. Existing recipient approval,
read-only dump credentials, local-only restore guards and ciphertext-only
artifact retention remain enforced.

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
