# 2027 Race-Day Certification Ledger

This ledger is the authoritative map from owner-authored operating rules to
executable evidence for the 2027 Missoula Pro-Am. A green unit test is evidence
for its named behavior only. It is not, by itself, proof of PostgreSQL,
multi-device, offline, deployment, or production readiness.

## Authority Model

`docs/Alex's Docs` contains the event owner's business rules.
`docs/DOMAIN_CONTRACT.md` is the canonical, reconciled implementation contract.
`FlightLogic.md` supplies flight detail where the contract is silent. Tests and
code are evidence, not authority. There is no blanket rule that lets an older
document or current implementation silently win a conflict: each conflicting
requirement must name the controlling clause or a dated owner decision, and the
Domain Contract must then be updated to match.

The handover names four historical authority files that are not present in the
repository, enclosing project directory, desktop documents, or supplied
attachments: `CODEX_RECON.md`, `CODEX_WORK_ORDER_05.md`,
`PROAM_2026_C97_DECISION_BOARD.md`, and
`PROAM_2026_C98_T4A_SHIPPED_AND_WRONG_TREE.md`. Their absence is an evidence
gap; this ledger does not invent their contents.

## Evidence States

- **Certified**: the acceptance behavior has current automated and operational
  evidence in every environment named by the requirement.
- **Implemented**: code and focused tests exist, but a required environment or
  rehearsal is still missing.
- **Partial**: only part of the acceptance behavior is implemented.
- **Blocked**: a known defect can violate the acceptance behavior.
- **Owner decision**: the repository cannot supply the required business
  decision.

## Reliability Requirements

Evidence is intentionally split from evidence still required. This workflow is
now deployed at merge `bac84ea2396602abc95b61165c90b87c14e61129` through
PR #117. Static inspection is not a passing test, and local evidence does not
stand in for hosted CI, a production mirror, or production smoke evidence.

| ID | Requirement | Authority | State | Current evidence | Required certification evidence |
|---|---|---|---|---|---|
| R27-001 | Friday/Pro Saturday show order and spillover follow the contract. | Domain Contract 15-30, 123-134; FlightLogic | Partial | Full isolated SQLite suite passed; flight/order tests passed inside it | Approved 2027 event matrix, exact PostgreSQL order lane, and full-show browser digest |
| R27-003 | Gear sharing fails closed without unsafe same-heat/overlap placement. | GEAR_SHARING_DOMAIN; Domain Contract 69-92 | Partial | Full isolated SQLite suite passed, including generator/readiness coverage | Exact disposable PostgreSQL result and synthetic browser sharing rehearsal |
| R27-004 | A prepared device can restart during WAN loss and open every scheduled heat page and local asset. | Domain Contract local-first boundary | Implemented | Bound manifest verifies content hashes; 2026-08-19 browser rehearsal prepared 15/15 items and cold-reloaded a heat with the local application origin stopped | Physical-device restart rehearsal on the approved 2027 schedule |
| R27-005 | One request produces at most one committed scoring action across duplicate, lost-response, retry, reconnect, and transfer paths; request tombstones remain with tournament history. | Owner scoring integrity | Implemented | Durable receipts/tombstones, rollback and duplicate tests, eight PostgreSQL races, lifecycle FK tests, two browser replays with matching UUID/SHA-256 receipts, and exact-head hosted CI run 32302853628 | Production-like multi-device rehearsal |
| R27-006 | Queues are inspectable and transferable as data, while replay authority remains bound to the issuing user and current role/tournament access; old entries stop automatic mutation. | Local-first and authorization controls | Implemented | Canonical LocalStorage queue; issuer/role/tournament/schedule binding; authenticated 8-30 day renewal; 30-day cutoff; export/import and stale-context tests; browser queue inspection | Physical cross-device transfer rehearsal and approved operator procedure |
| R27-007 | Compatible concurrent operations survive; conflicting, duplicate, stale, and out-of-order operations receive deterministic outcomes without silent loss. | Owner workflows; Domain Contract | Implemented | Stale-writer fences cover relay, payout, partner, scratch, flight, Birling, scoring, and background completion; eight disposable PostgreSQL races passed; lock-only scoring version race passed in two browser sessions; eight simultaneous same-account scoring GETs produced one atomic lock update and eight successful responses; hosted PostgreSQL unit and smoke jobs passed on PR #117 | Complete approved 2027 full-show adversarial rehearsal |
| R27-008 | Background work reports failed, interrupted, expired, and completed truthfully and is scoped before tournament limits are applied. | Operator recovery integrity | Implemented | Boot ownership, heartbeat, interrupted reconciliation, atomic completion, truthful return-state tests, and tournament-scope tests passed | Hosted restart/reconciliation rehearsal with retained operator evidence |
| R27-009 | SQLite backup is consistent; restore is quiesced, validated, journaled outside the DB, rollback-capable, and never reported ambiguously. | Existing SQLite recovery surface | Implemented | Process fence, consistent snapshot, restore package v2, schema/FK/index/trigger fingerprint, private permissions, WAL/SHM intent journal, quarantine, rollback, and 31 restore tests passed | Operator rehearsal on release hardware; provenance remains transport/host evidence, not a signature claim |
| R27-010 | A PostgreSQL artifact is restored only into a runner-local disposable target and passes schema/migration checks before publication. | Production PostgreSQL boundary | Implemented | Workflow enforces a least-privilege dump URL, runner-local target guard, current-head schema checks, encrypted-only upload, and plaintext cleanup; local PostgreSQL drill passed | Hosted workflow run with the approved secrets and retained artifact evidence |
| R27-011 | Backup cadence and recovery objectives match the approved 2027 event window and owner-approved tolerances. | Release operations | Owner decision | Stale 2026 race-window schedule exists | Approved dates/objectives recorded in release docs and rehearsed against the configured cadence |
| R27-012 | Operator controls and recovery states work at desktop/phone widths without console errors, hidden actions, overlap, or overflow. | Operator UI requirement | Implemented | Browser QA at 1440x900 and 390x844 found no document overflow or page/console errors; commands remained visible and screenshots were reviewed in the 2026-08-19 full-dress rehearsal | Checked-in browser scenario and physical-phone rehearsal on release head |
| R27-013 | SQLite, disposable PostgreSQL, production mirror, migration, browser, hosted CI, and production smoke evidence are separate. | Release Checklist; isolation rule | Implemented | Separate receipts now cover isolated SQLite, disposable PostgreSQL, migration, browser, production mirror, six-job hosted CI, exact Railway deployment, and commit-pinned public production smoke | Repeat the separated evidence set for each release candidate; authenticated production and physical-device checks remain separately gated |
| R27-014 | MNEMEX/STRATHMARK are not live-scoring dependencies and Missoula owns provisional results. | Domain Contract | Partial | Architecture and failure-handling code exists; not rerun here | Explicit network-denial scoring/build browser test |
| R27-021 | Cache invalidation cannot evict another tournament or resurrect stale standings after commit. | Multi-tournament integrity | Implemented | Delimited cache keys, generation fencing, PostgreSQL process-cache bypass, and controlled race tests passed | Hosted multi-process cache rehearsal |
| R27-022 | A completed export resolves to an existing checksum-verified artifact; missing/tampered ephemeral files become expired. | Operator recovery integrity | Implemented | Same-handle checksum, retry window, retention, restart/missing/tampered/download/tournament-scope tests passed | Hosted ephemeral-filesystem rehearsal |
| R27-023 | Backup confidentiality has approved encryption, key custody, least-privilege dump access, retention, deletion, and audit controls; plaintext is never published. | PII and recovery boundary | Partial | Workflow refuses plaintext publication, pins the configured `age` recipient by SHA-256, uses a dedicated dump URL, cleans plaintext/logs, and explicitly does not claim ciphertext recovery verification | Approved separately held recipient key, verified read-only dump role, hosted encrypt run, separate retained-artifact decrypt-and-restore rehearsal, retention/deletion review, and download audit |

## Owner Rule Ledger

Each row must be resolved into `docs/DOMAIN_CONTRACT.md`, configuration, exact
tests, and an operator step before final certification. `Owner decision` means
the annual choice or conflict cannot be inferred. `Blocked` means the existing
owner rule is clear and the implementation must be corrected unless the owner
changes it.

| ID | Rule family | Controlling clause/current conflict | State | Operator step | Required evidence |
|---|---|---|---|---|---|
| REG-TEAM-001 | College teams have no more than eight competitors and at least two men and two women. | ProAM requirements 8-10 | Partial | Import/register team and resolve eligibility warnings | Registration validation and browser setup test |
| CFG-EVENT-001 | Open/closed event selection, field limits, and the college six-event cap are annual/operator controlled. | ProAM requirements 43-72, 127-159 | Owner decision | Approve matrix, then configure Events | Approved 2027 matrix and configuration enforcement tests |
| PAIR-VALID-001 | Partner units are reciprocal, active, entered, gender-valid, and atomic. | Domain Contract 33-67 | Partial | Repair Partner Preflight blockers | Partner/preflight tests on both DBs and concurrent claim test |
| HEAT-ATOMIC-001 | Heat generation uses active entrants, capacity, ability order, partner units, dual-run stand swaps, and authoritative assignments. | Domain Contract 69-92 | Partial | Run Preflight, then Generate Show | Exact focused tests, PostgreSQL lane, full-show digest |
| OBSTACLE-RUN-001 | College Obstacle Pole runs twice; Pro Obstacle Pole runs once. | ProAM requirements 73-81 and 167 | Implemented | Review both generated College runs and stand/course change | `tests/test_owner_requirements_config.py`, dual-run generator/scoring tests, PostgreSQL and browser evidence |
| RESOURCE-SPRING-001 | Springboard capacity/left-handed dummy behavior is fail-closed or an explicitly authorized exception. | Domain Contract 81-87 conflicts with 150-154, 183-188 and FlightLogic | Owner decision | Resolve Springboard Preflight blocker | Dated ruling, reconciled text, generator tests |
| FLIGHT-BUILD-001 | Saturday flights derive from generated heat sequence and protect completed history. | Domain Contract 94-121 | Partial | Build/review Flights after heats | Flight build/rebuild tests and PostgreSQL digest |
| ORDER-RELAY-001 | Relay closes normal Saturday flights; mandatory Chokerman run 2 closes spillover. | Domain Contract 123-134 | Partial | Review Show Day closing order | Full-show order tests and browser rehearsal |
| BIRLING-2027-001 | Annual 2027 Pro Birling inclusion/exclusion and, if held, last-event/bracket rules are explicit. | ProAM requirements 165-175; current config excludes it | Owner decision | Approve matrix; seed/run bracket if enabled | Dated decision and bracket/order tests if enabled |
| RELAY-COMP-001 | Relay composition, exclusions, team count, one-flight behavior, participation guarantee, and $5 fee are reconciled. | ProAmRelay 5-21; ProAM requirements 190-212; implementation docs differ | Owner decision | Configure, validate, draw, and score Relay | Dated ruling plus composition/fee/flight tests |
| SCORE-POINTS-001 | College points are 10/7/5/3/2/1 with event-specific score direction. | ProAM requirements 27-39 | Partial | Enter, review, and finalize event scores | Scoring calculation tests on both DBs |
| SCORE-TIE-001 | Tie splitting and two-timer averaging are authorized for 2027. | Historical plan/current code; weak business authority | Owner decision | Approve policy; resolve flagged ties/timers | Dated ruling and retained/revised scoring tests |
| MARK-REVIEW-001 | Handicap marks require review; reviewed zero is intentional scratch; scored history is immutable. | Domain Contract 190-204 | Partial | Review marks before scoring | Preflight/scoring/history tests on both DBs |
| REPORT-AWARDS-001 | Top-five team/men/women, Bull/Belle, payouts, settlement, and event results are correct and printable. | ProAM requirements reporting sections | Partial | Finalize, review, print/export, settle | Calculation, export/print, and browser workflow tests |
| REG-PRO-001 | ALA self-report, first-time status, shirts, annual fees, and related registration data are preserved. | ProAM requirements registration sections | Partial | Import/register and run fee/ALA reports | Import/form/report tests using synthetic data |

## 2026-08-18 Evidence Receipt

All local data in this receipt was synthetic. Tests used isolated temporary
SQLite databases, a disposable local PostgreSQL database, and
`instance/browser_certification.db`. No production database or PII was accessed.

- **Complete SQLite suite:** 4,384 collected; 4,342 passed and 42 expected
  skips in 791.256 seconds. Nine existing SQLAlchemy identity-map warnings in
  flight-builder tests remain visible; the run had no failure or error.
- **Scoring/offline/lock regression:** 444 tests passed after adding the
  lock-independent scoring-state digest. The digest rejects changed scores or
  heat structure but permits replay after lock-only `Heat.version_id` changes.
- **Disposable PostgreSQL:** eight race tests passed for request receipts and
  lifecycle, relay team and payout, partner claim, flight reorder, Birling, and
  background completion/reconciliation. The rebased release lane also passed
  the populated shadow-schema round trip and locked-delivery worker test, for
  ten successful PostgreSQL checks on the combined branch.
- **Recovery:** 31 restore-workflow tests passed. Restore package v2 verifies
  complete schema, foreign keys, indexes, and triggers and exercises quiesce,
  intent journal, quarantine, rollback, and interrupted recovery paths.
- **Migrations:** 24 migration tests passed with two expected skips. The local
  PostgreSQL migration template was rebuilt from the complete current chain
  before the race module ran.
- **Focused reliability:** 50 receipt-lifecycle/offline/daily-workflow tests and
  141 Birling-focused tests passed. The broader combined scoring, scratch,
  offline, restore, and day-of-operations lane passed 144 tests.
- **Static/runtime:** full Ruff, compileall, Python 3.10 grammar parsing, JS
  syntax checks, both Node offline suites, and `git diff --check` passed.
- **Installed STRATHMARK contract:** the exact Git-pinned artifact at
  `da5c44d07311b226c1e9842104477efaf61253fa` exposed the expected seven-route
  contract and digest. The migrated synthetic rehearsal proved exact receipt
  replay and duplicate numeric-outcome handling with no network or production
  data. The release-readiness and PostgreSQL migration-safety modules passed
  all 12 checks.
- **Browser package:** Chrome prepared and byte-verified 14 of 14 manifest
  items. A forced reload with network disabled retained the authenticated heat
  page under service-worker control.
- **Browser replay:** request `0a98cf2b-585d-41b1-840b-b55e91ca345d`
  replayed 11.00/11.20 to 11.10 and produced a receipt with payload SHA-256
  `8e88de872c06ffa55b3604704ce4dc74f2843fdbab18d292d668a15fae842469`.
- **Lock-refresh browser race:** a second authenticated session advanced the
  lock-only heat version from 5 to 6 while request
  `09322d30-303e-4277-905c-4f367b6c5d19` remained offline. Replay accepted the
  unchanged scoring digest, persisted 12.00/12.20 as 12.10, created the matching
  receipt, and cleared the queue.
- **Responsive browser QA:** 1440x900 and 390x844 had no document overflow,
  hidden primary command, page error, or console error. The scoring table used
  contained horizontal scrolling at phone width.
- **Not run:** hosted GitHub Actions, a production mirror, deployment, and
  production smoke. The PostgreSQL backup workflow still requires the approved
  `RAILWAY_PG_READONLY_DUMP_URL`, `BACKUP_AGE_RECIPIENT`, and a separately held
  private `age` identity before its hosted evidence can exist.

## 2026-08-19 Full-Dress Rehearsal Receipt

The follow-up local rehearsal is recorded in
`docs/2027_FULL_DRESS_REHEARSAL.md`. It adds a deterministic guarded load
harness, a passing 260-user operator-paced load gate, safe run-owned PostgreSQL
cleanup and migration-head template isolation, cold-cache request coalescing,
anonymous public-request query reduction, deterministic full-response load
measurement with activation, authentication, role, and endpoint gates, atomic
heat-lock acquisition under simultaneous judge page loads, 160 SQLite
operations checks, 14 disposable PostgreSQL checks, migration safety evidence,
a two-session stale-score conflict, responsive desktop/phone inspection, and a
15-of-15 origin-down offline reload.

It does not change any `Owner decision` to `Certified` and does not stand in
for a physical device, hosted CI, production mirror, deployment, production
smoke, or backup-key-custody receipt.

## 2026-08-19 Production Release Receipt

The certification hardening branch was published as PR #117 at exact head
`5a32eb901b5414d3b32d66c17e154502393dc264` and merged without head drift as
`bac84ea2396602abc95b61165c90b87c14e61129`.

- **Hosted CI:** GitHub Actions run `32302853628` passed all six jobs: `test`,
  `unit-postgres`, `postgres-smoke`, `migration-safety`, `lint`, and
  `pip-audit`.
- **Final isolated local suite:** 4,467 tests were collected; the suite reached
  100% and exited 0. The documented environment-specific skips remained, and
  the only warnings were nine pre-existing SQLAlchemy identity-map warnings in
  flight rebuild tests. The strengthened session guard confirmed that neither
  `instance/proam.db` nor checkout-local `.qa_tmp` existed after the run.
- **Production-shaped mirror:** the final normal 2026 PostgreSQL mirror lane
  passed at its standing `205 passed, 6 skipped, 2 xfailed` matrix. Its
  token-owned clones were removed; the pre-existing legacy clone was not
  touched.
- **Focused PostgreSQL:** all 10 scratch roster-token tests passed against a
  disposable PostgreSQL database, including populated-snapshot constant-query
  coverage, malformed-audit fail-closed behavior, exact undo tokens, stale
  control suppression, and role restrictions. Legacy and normalized Relay
  scratch/undo cases also passed on SQLite and PostgreSQL.
- **Independent review:** two separate final reviews reported no remaining
  actionable findings after legacy Relay storage migration, cross-division ID
  collision, stale control, and query-scaling defects were corrected.
- **Deployment:** Railway deployment
  `5977fb83-9048-4ee0-9bc2-835fd4385f80` reached `SUCCESS` for the exact merge.
  It built with Python 3.10.13 and Railpack 0.37.0, produced image
  `sha256:a52c825b59a145cfeed4f73c43e3e3116244847715151d785149dbf5c16246e3`,
  ran `flask db upgrade` against PostgreSQL, and booted Gunicorn normally.
- **Commit-pinned public smoke:** `/health`, the active spectator portal,
  `/api/public/tournaments/2/standings`, and `/auth/login` all returned HTTP
  200. Health matched merge `bac84ea2396602abc95b61165c90b87c14e61129`,
  application version `2.14.16`, and migration `a6b7c8d9e0f1`.
- **Explicitly not proved:** no production judge credential was used, no
  production mutation was attempted, and no physical-device, radio-disable,
  or cross-device rehearsal was represented by the public smoke.
- **External gates:** optional STRATHMARK variables remain absent and degrade
  only the non-blocking sync. GitHub has `RAILWAY_PG_PUBLIC_URL`, but not
  `RAILWAY_PG_READONLY_DUMP_URL`, `BACKUP_AGE_RECIPIENT`, or
  `BACKUP_AGE_RECIPIENT_SHA256`; hosted encrypted backup and retained-artifact
  decrypt/restore evidence therefore remain blocked on approved access and key
  custody.

This receipt upgrades the production-mirror, hosted-CI, deployment, and public
smoke evidence from absent to recorded. It does not approve any annual owner
decision and does not make the product 2027 race-day certified.

## Baseline Disposition

The 2026-08-18 baseline blockers were handled as follows:

1. **Resolved:** heat and service-worker queue behavior now converges on one
   canonical, race-tested LocalStorage queue.
2. **Resolved:** durable receipts make accepted retries deterministic and retain
   tournament-scoped history after heat deletion.
3. **Resolved locally:** prepared manifests contain every scheduled page and
   asset with byte hashes; cold-offline browser loading passed.
4. **Resolved locally:** background jobs have boot ownership, heartbeat,
   interrupted reconciliation, and atomic truthful completion.
5. **Resolved locally:** SQLite snapshot and restore now have quiesce, intent,
   validation, quarantine, rollback, and private-permission evidence.
6. **Resolved in code/local tests:** relay, partner, scratch, flight, Birling,
   scoring, and payout stale-writer gaps now fail deterministically.
7. **Resolved in workflow code:** hosted PostgreSQL backup restores into a
   runner-local target before encrypted publication; its hosted run is pending.
8. **Resolved locally:** browser storage, offline reconnect, concurrent browser
   lock refresh, service-worker loading, and responsive presentation were run.
9. **Open:** several 2027 business rules remain contradictory or unselected;
   code must not silently choose among them.
10. **Resolved locally:** cache keys/generations are fenced and completed export
    files are checksum-verified with explicit expiry behavior.

## Certification Gate

Reliability implementation may ship before annual business choices are made,
provided the release does not claim certification or silently change those
rules. Final certification requires every applicable reliability and owner-rule
row to be **Certified**, every `Owner decision` to be approved and incorporated
or explicitly signed not applicable, R27-011 to record the actual event window
and approved recovery objectives, and R27-023 to have an operable key policy.
Until then the product may be materially improved and deployable, but it must
not be described as 2027 race-day certified.
