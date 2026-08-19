---
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
execution: code
certification_readiness: blocked-owner-and-operations-decisions
title: 2027 Race-Day Certification And Recovery
date: 2026-08-18
owner: Codex
---

# 2027 Race-Day Certification And Recovery

## Direction

Make race-day scoring, concurrent operation, backup, and recovery fail closed
and produce evidence that operators can inspect. Preserve the owner-authored
event rules and show order. Do not make MNEMEX, STRATHMARK, Railway, or any WAN
service a live scoring dependency.

## Product Contract

### In Scope

1. One canonical, operator-visible offline score queue.
2. Prepared-device cold-offline access to every scheduled heat-entry page.
3. Durable, user-bound scoring request IDs that make retries idempotent.
4. Expired-session replay through the existing HMAC-authorized endpoint.
5. Stale-write rejection for confirmed relay, partner, scratch, flight, and
   Birling concurrency gaps.
6. Consistent SQLite snapshots, validated/rollback-capable SQLite restore, and
   truthful background-job lifecycle.
7. Automated restore verification for scheduled PostgreSQL backups.
8. Checked-in browser rehearsals, PostgreSQL concurrency tests, recovery
   runbooks, and the requirements-to-evidence ledger.
9. Tournament-safe cache invalidation and truthful export-artifact lifecycle.

### Out Of Scope

- Changing event format, show order, placements, payouts, handicaps, partner
  policy, gear-sharing policy, or physical stand policy.
- Enabling in-app PostgreSQL restore against production.
- Interactive reading or mutation of production competitor data, or exposing
  backup contents. The existing hosted backup may perform an automated dump
  read only through a dedicated least-privilege role; no agent/operator step
  inspects rows.
- Making an external ecosystem service race-day critical.
- Claiming an owner-approved RPO, RTO, or 2027 event window before the owner
  supplies or confirms it.
- Silently resolving the annual/event-rule decisions listed in the Owner Rule
  Ledger. Reliability work may ship without those decisions; certification may
  not.

## Planning Contract

- Implementation uses an isolated feature worktree from current `origin/main`.
- Tests use disposable databases only. Browser fixtures are synthetic.
- Every behavior change starts with a failing focused test.
- Ordinary pytest, disposable PostgreSQL, production-mirror, browser, CI, and
  production smoke evidence are reported as separate lanes.
- No PR merges until exact-head required checks pass. `main` deployment is
  verified without production writes.

## Key Technical Decisions

### KTD-1: LocalStorage queue is canonical; IndexedDB becomes legacy-drain only

- Decision: the heat page owns new offline submissions. The service worker no
  longer intercepts score POSTs, but can replay previously queued legacy
  IndexedDB entries until they drain.
- Provenance: current code trace and R27-005.
- Rejected alternative: keep both queues and deduplicate during replay.
- Reason: two independent queues cannot give operators one truthful inventory.

### KTD-2: Idempotency is a database receipt, not a browser heuristic

- Decision: every heat submission carries a UUID and payload fingerprint. A
  receipt committed with the score stores the accepted outcome. The same user,
  heat, ID, and fingerprint returns that outcome; mismatched reuse fails.
- Provenance: lost-response audit finding and R27-005.
- Rejected alternative: treat a version conflict as presumed success.
- Reason: a conflict does not prove which payload committed.

Receipt contract:

- Table `score_submission_receipts`: UUID request ID primary key, tournament
  ID, heat ID, issuing user ID, canonical payload SHA-256, stored accepted
  outcome JSON, and creation timestamp; indexed by tournament/heat/user. The
  tournament is the owning historical aggregate and cascades on explicit
  tournament deletion. The heat ID is a non-FK historical snapshot so heat
  regeneration cannot erase the tombstone. User deletion sets the issuer to
  null, retains the receipt, and permanently disables replay.
- Canonical payload excludes transport credentials (`csrf_token` and
  `replay_token`) and includes every score/status/reason, heat identity, and
  posted version in sorted key/value form.
- Receipt, score/result changes, heat state, and saved-score audit row commit in
  one transaction. A duplicate matching binding returns the stored accepted
  outcome without a second mutation/audit. Any changed binding is `409`.
- PostgreSQL uniqueness races re-query the committed receipt after rollback;
  they do not guess success. Automatic queue replay is limited to 30 days.
  After the seven-day token window, only the original reauthenticated user may
  renew replay authority. Older entries require manual reconciliation and
  never mutate automatically. Receipts/tombstones have no automatic deletion
  in this phase and remain with tournament history, so an exported old queue
  can never make a request ID reusable merely by waiting.

### KTD-3: Offline operation requires explicit preparation and evidence

- Decision: Offline Operations exposes a preparation command that asks the
  service worker to cache all current scheduled heat-entry pages and required
  local assets, then reports per-page success/failure.
- Provenance: local-first authority boundary and cold-outage audit.
- Rejected alternative: imply that opportunistically visited pages constitute
  an offline show package.
- Reason: operators need a deterministic pre-show gate.

Offline package contract:

- Versioned manifest binds application build, schedule fingerprint,
  tournament, issuing user, current role, prepared timestamp, and URL digests.
- A schedule rebuild, application build change, logout, or user/role mismatch
  invalidates the package. Logout clears prepared page caches but does not
  silently delete unsynced queue data.
- Queue transfer is versioned data only. It carries issuer/tournament/schedule
  bindings, never a new user's authority. Import under another user is visible
  but cannot sync.
- CSRF expiry is a structured JSON error and alone may invoke HMAC replay.
  Session expiry requires reauthentication as the issuer. Authorization `403`
  and role/tournament loss remain blocked. Redirects/HTML never delete entries.
- Queue entries carry creation/expiry state. At 30 days they become
  `manual_reconciliation_required`; export remains available but Sync cannot
  mutate the database.

### KTD-4: Stale operators receive conflicts, never silent last-writer wins

- Decision: use the existing tournament writer lock for serialization and
  add narrow expected-state tokens/digests for stale-client detection. Enforce
  monotonic state machines where transitions are ordered.
- Provenance: concurrent-operator audit.
- Rejected alternative: add one global lock without stale-state validation.
- Reason: serialization alone still lets a later stale snapshot erase a valid
  earlier action.

### KTD-5: A backup is recoverable only after restore verification

- Decision: SQLite uses the online backup API and integrity checks. Scheduled
  PostgreSQL automation restores each new dump into an ephemeral PostgreSQL
  database and checks schema/migration invariants before artifact publication.
- Provenance: recovery audit and existing production PostgreSQL boundary.
- Rejected alternative: continue treating file size and SQL text as proof.
- Reason: an unreadable or non-restorable backup is not recovery evidence.

Hosted restore guard:

- Existing automation may use only a dedicated production dump role whose
  non-superuser, read-only privileges are verified before `pg_dump`; it must
  never print row data. The production URL secret is scoped only to the dump
  and privilege-check steps. Restore steps receive only job-local credentials
  and have no production secret or network target.
- The restore step rejects external/Railway hosts and production database
  names before any destructive flag or SQL is used.
- U6 must replace plaintext publication with recipient-based client-side
  encryption. The recipient public key and separately held recovery private
  key must be approved before merge. If encryption is absent or fails, the
  workflow skips upload, securely removes the runner copy, and fails loudly;
  an unencrypted dump is never published.

### KTD-6: Process-local jobs carry boot ownership

- Decision: each job records a boot/worker owner. Startup reconciles only jobs
  owned by a demonstrably dead prior boot; a returned result with `ok: false`
  is a failed job. The deployed one-process assumption is explicit and tested.
- Provenance: background-job audit and R27-008.
- Rejected alternative: leave stale rows running until manual interpretation.
- Reason: the executor cannot resume work after its process disappears.

### KTD-7: SQLite restore is an offline maintenance operation

- Decision: the web route validates and stages a restore package; actual file
  replacement runs under an offline maintenance command with a cross-process
  file fence, exclusive DB access, drained jobs/connections, same-volume
  staging, fsync, safety snapshot, post-reopen validation, and rollback.
- Provenance: restore security review and R27-009.
- Rejected alternative: replace the live SQLite file inside an ordinary web
  request.
- Reason: a request cannot prove that another process or in-flight writer has
  released the database.

## Implementation Units

### U1: Certification Ledger And Baseline

Owned files:

- `docs/2027_RACE_DAY_CERTIFICATION.md`
- this plan
- focused evidence reports produced during verification

Acceptance:

- Every owner-authored rule family has a stable ID, controlling clause,
  operator step, current evidence state, and required evidence.
- Missing historical documents and owner-only recovery objectives are not
  fabricated.

### U2: Exactly-Once Prepared Offline Scoring

Owned files:

- `static/sw.js`
- `static/offline_queue.js`
- `static/js/offline_queue_shared.js`
- `templates/scoring/enter_heat.html`
- `templates/scoring/offline_ops.html`
- `routes/scoring.py`
- scoring workflow/model/migration files needed for request receipts
- focused offline, replay, idempotency, and presentation tests

Work:

1. Stop new score POST interception in the service worker.
2. Add stable request IDs to queue entries and normal submissions.
3. Persist an idempotency receipt atomically with accepted scores.
4. Make queue sync fall back to the HMAC replay endpoint on CSRF rejection.
5. Bind queue transfer/replay to the issuing user and reject payload drift.
6. Add prepared-offline caching with visible completeness and refresh states.
7. Preserve replay of legacy IndexedDB entries without creating new ones.
8. Require verified JSON success or a matching durable receipt before any
   legacy or canonical queue entry is deleted; redirects and HTML never count.

Acceptance:

- Offline submit, reconnect, double-click, lost response, and explicit retry
  each cause one score mutation and one saved-score audit action.
- An ID reused with different payload or user is rejected.
- A prepared browser restarted offline can open every scheduled heat page.
- Operators see one current queue and a separate truthful legacy-drain status.
- Logout/user switch clears prepared pages, preserves unsynced data, and blocks
  replay until the original authorized user returns.

### U3: Concurrent Operator Integrity

Owned files:

- relay route/service/templates and tests
- partner route/service/templates and tests
- scratch route/service/templates and tests
- flight route/templates and tests
- Birling route/service/templates and tests

Work:

1. Lock and stale-check relay snapshot updates.
2. Serialize partner claims and re-evaluate reciprocal availability under lock.
3. Reject a scratch confirmation when its effect digest changed.
4. Add a flight-order digest and reject stale reorder payloads.
5. Enforce monotonic flight transitions.
6. Add expected fall state so duplicate Birling submissions are idempotent or
   rejected without adding a second physical fall.
7. Preserve existing Heat/EventResult optimistic-lock behavior and add it to a
   compatible/conflicting/duplicate/out-of-order operation matrix.

Acceptance:

- Two-client PostgreSQL tests prove compatible changes survive together and
  conflicting/stale requests produce exactly one winner plus an actionable
  conflict. Each test asserts response, mutation count, audit count, and final
  database digest.
- No scenario silently drops, duplicates, or regresses accepted state.

### U4: Backup, Restore, And Job Recovery

Owned files:

- `services/backup.py`
- `services/reporting_backup.py`
- `services/restore_workflow.py`
- `services/background_jobs.py`
- `routes/reporting.py`
- affected operator templates and focused tests

Work:

1. Create SQLite backups with the SQLite online backup API and validate the
   completed snapshot before upload/download.
2. Make the web route validate and stage restores only; file replacement moves
   to an offline maintenance command that proves exclusive access.
3. Validate integrity, foreign keys, revision, tournament identity, checksum,
   and provenance; keep a same-volume safety snapshot and roll back every
   injected replacement/reopen failure.
4. Append and fsync an out-of-database restore journal before and after each
   phase, recording actor, source/safety hashes, audit-chain head, validation,
   replacement, and rollback outcomes without row data.
5. Turn `ok: false` backup results into failed jobs.
6. Reconcile process-interrupted jobs by boot ownership and surface their
   status to operators.
7. Scope recent-job queries by tournament before applying the limit.

Acceptance:

- Active SQLite writes cannot produce a torn backup.
- Corrupt, foreign-key-invalid, or wrong-revision uploads never replace the DB.
- Injected post-replace validation failure restores the prior DB.
- Failed and interrupted jobs are never displayed as completed/running.
- Crash injection at journal, stage, safety snapshot, replacement, reopen, and
  validation boundaries yields either the old valid DB or new validated DB.

### U5: Cache And Export Durability

Owned files:

- `services/cache_invalidation.py`
- `services/report_cache.py`
- `services/reporting_export.py`
- affected routes and focused tests

Work:

1. Delimit every tournament cache prefix so tournament 1 cannot invalidate
   tournament 10 or 11.
2. Add an invalidation generation guard so a read begun before commit cannot
   repopulate a stale entry after invalidation in the current process model.
3. Store export checksums and verify file existence/checksum when resolving a
   completed job.
4. Mark missing or changed ephemeral artifacts expired/failed instead of
   offering a dead download.

Acceptance:

- Cross-tournament invalidation tests preserve unrelated cache entries.
- A controlled stale-reader race cannot repopulate invalid data.
- Restart/missing/tampered export tests never report a usable completion.

### U6: PostgreSQL Restore Proof And Runbooks

Owned files:

- `.github/workflows/daily-backup.yml`
- `docs/ROLLBACK_SOP.md`
- `docs/RELEASE_CHECKLIST.md`
- recovery drill scripts/tests as needed

Work:

1. Restore the new dump into ephemeral PostgreSQL in the backup workflow.
2. Verify required tables, migration metadata, and non-sensitive aggregate
   invariants before artifact upload.
3. Correct stale 2026 scheduling claims without guessing the 2027 event dates.
4. Document disposable restore rehearsal, evidence capture, and the explicit
   prohibition on in-place production restore from the application.
5. Verify the dump credential is a non-superuser read-only role before use,
   client-side encrypt the verified artifact to the approved recipient, upload
   only ciphertext, and delete plaintext in an unconditional cleanup step.

Acceptance:

- A deliberately invalid dump fails before artifact publication.
- A synthetic dump restores and passes schema checks locally and in CI.
- The runbook distinguishes provider recovery, disposable verification, and
  application rollback.
- The workflow never emits row data and hard-fails if its restore target is not
  the runner-local PostgreSQL service.
- No plaintext artifact is uploaded; encryption and decryption are both tested
  using the approved, separately held key path.

### U7: Browser And Adversarial Certification

Owned files:

- checked-in browser test configuration/specs
- synthetic fixture/bootstrap helpers
- certification evidence update

Work:

1. Exercise desktop and phone scoring with network transitions and browser
   restart.
2. Exercise two authenticated sessions on the same heat and each repaired
   stale-writer path.
3. Rehearse special events, scratch/undo, standings, payouts, export, backup,
   and recovery entry points with synthetic data.
4. Capture console/page errors, overflow, hidden controls, queue counts, audit
   counts, and database digests.

Acceptance:

- No console/page errors, hidden primary action, incoherent overlap, or
  horizontal overflow at tested viewports.
- Evidence names the exact commit, browser, database backend, and scenario.

## Verification Contract

Every evidence receipt records the exact commit, UTC date, backend, isolation
fixture/database, command or hosted artifact URL, and pass/fail result. Planned
tests are never entered as existing evidence.

### Focused Gates

- New red/green tests for every implementation unit.
- Existing scoring, relay, partner, scratch, flight, Birling, reporting,
  infrastructure, presentation, and lifecycle tests.

### Repository Gates

- `ruff check .`
- Python 3.10 AST parse for changed Python files.
- standard `python -m pytest` with isolated base temp, explicitly excluding
  `proam_regression/` per repository configuration.
- PostgreSQL runtime smoke, migration safety, migration integrity, and new
  two-client tests on disposable PostgreSQL.
- `git diff --check`.
- explicit `proam_regression/` production-mirror lane when legitimate isolated
  mirror access is available; absence is reported and cannot be replaced by
  SQLite or synthetic PostgreSQL claims.

### Recovery Gates

- Synthetic SQLite active-write backup and forced-rollback restore drill.
- Synthetic PostgreSQL dump/restore drill.
- Hosted backup workflow restore verification.

### Browser Gates

- Prepared cold-offline score entry.
- Lost-response and reconnect idempotency.
- Expired session and queue transfer.
- Two-operator conflicts.
- Desktop and phone layout/interaction review.

## Definition Of Done

### Reliability Release Complete

1. All in-scope reliability blockers are fixed. Annual owner decisions may
   remain visibly blocked, but backup encryption/key custody cannot: U6 does
   not merge while plaintext publication is possible.
2. Focused, standard, PostgreSQL, migration, recovery, and browser lanes pass
   on the exact branch head.
3. Independent structural, security, and operator reviews have no unresolved
   high-severity finding.
4. The PR passes required hosted checks and merges at its reviewed head.
5. Railway deploys the merge; health, one authenticated read-only page, and one
   public page pass without production data mutation.
6. The ledger records exact executed evidence and remaining gaps.

### 2027 Race-Day Certified

1. Every applicable reliability and owner-rule row is `Certified`.
2. Every owner-decision row is approved and incorporated into the Domain
   Contract/configuration or explicitly signed not applicable.
3. Event dates, recovery objectives, and encrypted-backup key custody are
   approved and rehearsed.
4. Exact-head browser, PostgreSQL, production-mirror, hosted CI, deploy, and
   read-only production smoke evidence is current.

## Execution Order

1. Complete the rule-level ledger and keep annual decisions blocked.
2. Add receipt/job persistence contracts and migrations; run SQLite,
   PostgreSQL upgrade, downgrade/rollback, migration integrity, and available
   production-mirror migration gates.
3. Implement transactional scoring/concurrency and backend recovery behavior.
4. Implement the offline client/package against the stable receipt API.
5. Add cache/export durability and hosted PostgreSQL restore proof against the
   final migration head.
6. Run browser/operator/adversarial certification, then PR/CI/deploy evidence.
