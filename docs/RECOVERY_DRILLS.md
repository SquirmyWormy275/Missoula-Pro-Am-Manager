# Synthetic recovery drills

This Linux development lab exercises the real Flask application and its normal
one-worker, four-thread Gunicorn process model. It creates a new local PostgreSQL
cluster, migrates it, signs in through the real CSRF-backed form, scores synthetic
entrants, and compares committed state using separate database connections.
It accepts no external database URL, existing database, container or process ID.
Use [ROLLBACK_SOP.md](ROLLBACK_SOP.md) for actual incident policy. These commands
are not production restoration, provider failover, PITR or race-day certification.

## Setup and one reproduction route

Use a clean checkout, Python 3.10, PostgreSQL server/client executables of the
same major, and Linux `unshare`, `ip`, flock and pidfd support. Run as a normal
user with permission to create a private user/network namespace. Installation
is a separate network-enabled preparation step; no host packages or services
are automatically changed by the runner.

```bash
python3.10 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-shadow-verifier.txt
.venv/bin/python scripts/recovery_drills.py doctor --pg-bin /usr/lib/postgresql/16/bin
.venv/bin/python scripts/recovery_drills.py list
.venv/bin/python scripts/recovery_drills.py run --suite core \
  --pg-bin /usr/lib/postgresql/16/bin --output /tmp/proam-core-first
.venv/bin/python scripts/recovery_drills.py report --run /tmp/proam-core-first
```

The output directory must be absolute and entirely new. Choose a new directory
for every attempt. Existing reports, symlinks, hard-link aliases, traversal and
operational storage such as `instance/` or `uploads/` are refused. `doctor` and
`list` do not start applications, seed data or connect to databases. The runtime
has only loopback networking and an allowlisted environment; operational cloud,
database, proxy, signing and service credentials are not inherited. A machine
that prohibits namespace creation cannot execute this process lane; do not
rename a mock or ordinary unit test as a passing crash drill.

The default core suite is R01–R03. A single scenario and the full extended suite
use the same lifecycle:

```bash
.venv/bin/python -m pip install -r requirements-recovery-drills.txt
# Extended prerequisites: local Chromium, age/age-keygen, and Node.js on PATH.
.venv/bin/python scripts/recovery_drills.py run --scenario browser-offline-replay \
  --pg-bin /usr/lib/postgresql/16/bin --output /tmp/proam-browser-first
.venv/bin/python scripts/recovery_drills.py run --suite all \
  --pg-bin /usr/lib/postgresql/16/bin --output /tmp/proam-all-first
```

`--tools-bin /absolute/tool-directory` supplies optional tools without importing
the caller's environment. `--pg-library /absolute/library-directory` supports
locally extracted PostgreSQL binaries. Neither option changes host settings.
Browser installation and version selection happen before the offline run; the
runner does not download browsers or models. Native Windows/SQLite tests remain
separate from the Linux process lane.

## What executes

| ID / CLI scenario | Expected observation |
| --- | --- |
| R01 `restore-fresh-postgres` | Actual application `backup_database()` custom dump restores checkpoint A into a new database before app startup. A known checkpoint-B receipt is absent. Normal app and read-only grants are checked; restored login, reports, partial-timer warning and a new finalized score work. |
| R02 `score-precommit-crash` | Score, audit and receipt are flushed but invisible to another connection. SIGKILL of the owned worker rolls them back. Original request succeeds once after the master respawns it. |
| R03 `score-postcommit-response-loss` | Another connection sees the real commit before SIGKILL and response loss. Exact retry reads the receipt; changed payload/user/heat are rejected. An old receipt cannot overwrite a later correction. |
| R04 `database-outage` | Owned database shutdown breaks an established score connection and a new request without acceptance. Restart recovers reads/writes. A real parent-row lock is observed, then deliberately released. |
| R05 `bad-backup` | Truncation, modification, incomplete output and wrong identity/digest/schema are refused. An incompatible restored candidate never becomes active. A new synthetic age identity encrypts/decrypts the retained ciphertext; the decrypted bytes are restored. Wrong key and altered ciphertext fail. |
| R06 `browser-offline-replay` | Chromium's real service worker supplies a cold offline scoring page. The actual persistent queue survives reload, lost acknowledgment and origin restart. Verified retries, expired CSRF/token renewal, session/user refusal, stale package and newer-score conflict are checked against saved state. |
| R07 `background-worker-crash` | Real async export dies while running with a partial file. The default 30-second lease marks it interrupted. Old boot completion is rejected, including against a replacement's running job. Operator retry yields a verified workbook. |
| R08 `shadow-delivery` | Missoula's real outbox talks to a clearly simulated authenticated receiver. Pending intent survives outages, sender crashes, lost acknowledgment and older client restoration. A competing worker skips the locked delivery; exact replay produces one receiver effect. Official marks/results/payouts stay unchanged. |
| R09 `code-schema-rollback` | Archived baseline `3902481` serves its compatible current schema, retains accepted receipts, commits a new scratch score and returns to candidate code without undoing data. No downgrade is manufactured. |
| R10 `sqlite-offline-restore` | Existing online-backup, staged-restore, exclusive-fence and abrupt sidecar-journal process tests rerun from owned scratch storage. Web requests stage rather than replace the live database. |

The core fixture has two tournaments, four pro entrants and two heats of a
simple timed scratch event. Timer pairs average to hand-computable durations.
The second tournament is part of whole-database semantic comparisons. R08 adds
a separate synthetic shadow field using the existing reviewed contract fixtures.
This receiver checks schema, bearer, actor HMAC, digest and stable payload; it
does not independently qualify the remote STRATHMARK service. Keep the installed
contract gate separate:

```bash
.venv/bin/python scripts/verify_strathmark_shadow_contract.py
```

The ordinary dependency remains STRATHMARK commit
`da5c44d07311b226c1e9842104477efaf61253fa`. No consumer pin, profile authority,
prediction algorithm or official shadow-mark writer is changed by this lab.

## Transaction and evidence boundaries

Normal authentication, CSRF, role/scope, heat identity and score-state checks
precede scoring. A scoring GET may separately commit an edit lock. The runner's
pre-submit checkpoint is taken after that legitimate change. The writer locks
the tournament, heat and event, updates results, and flushes. Auto-finalization
and shadow capture use a nested transaction: valid partial saves or deferred
finalization keep their documented accepted outcome and warning. Scoring audit
rows and the durable request receipt join the outer score commit. Cache
invalidation and HTTP response creation follow it.

Fault wrappers are installed only by `recovery_drills.worker`, after the normal
factory loads inside the Gunicorn worker. Normal `app:create_app()` does not
import the adapter. No header, route parameter or ambient environment variable
can select a crash point. Each barrier names the exact request; missing barriers
are invalid runs. Signals require allocation membership, a matching private run
marker, UID, boot, PID start time, executable and argv, followed by pidfd signaling.
The runner records allocation intent, concrete cluster/process identities and
an exclusive live-run lock before cleanup can act.

`report.json` is the canonical result, and `report.md` is generated from the same
object. They include commit/dirty-patch identity, runtime/dependency/schema
versions, per-check expected/actual values, field-level state differences,
known snapshot gap, fault observations and measured monotonic durations. Login
audits, password hashes, edit locks/heat optimistic versions and job heartbeats
are explicitly volatile in semantic comparisons; result versions and accepted
receipts remain checked. Reports retain original failed attempts and never
equate an absent browser or skipped required drill with a pass.

Exit `0` means all selected checks and cleanup passed. Exit `1` means selected
work failed, was blocked/invalid, or cleanup failed. Exit `2` means a refused or
invalid CLI operation. `incomplete.json` survives interruption/publication
failure. A login redirect, partial response or health endpoint alone cannot
establish scoring success. `NOT_RUN` counters remain unknown rather than zero.
R01 records deliberately excluded checkpoint-B work as an expected snapshot gap;
it does not reconstruct it from the external client ledger or claim an RPO.

[Executed R03 example](recovery-example.json) is a sanitized extract from source
`7453f4b88353f14397ba7e8f4fd862d83a83a39f`, not a template. It records the
post-commit worker kill, receipt recovery and cleanup. Its measured interval is
local synthetic evidence, not an approved recovery objective or production RTO.

Raw process diagnostics and secrets live only in private scratch storage. Final
reports omit cookies, CSRF/replay authority and private paths. Plaintext dumps,
temporary keys, browser profiles and database files are removed after the run.
R05 retains synthetic ciphertext and its digest, but deletes its temporary key.
Normal deletion is not forensic secure erasure. Browser screenshots contain only
synthetic operator data. The hosted core job uploads report JSON/Markdown only.

## Failure, cleanup and another operator

Read failed checks before aggregate status. A functional pass with failed cleanup
is still an overall failure. Interrupted work is attributable through the owner
record, incomplete marker and bounded diagnostics. To reconcile only that run:

```bash
.venv/bin/python scripts/recovery_drills.py cleanup --run /tmp/proam-core-first
```

The run lock refuses live cleanup. PID reuse, changed ownership or an unproven
resource leaves it intact and returns an error. Cleanup never prunes Docker,
stops a host database daemon, drops by name prefix, or removes retained reports.
The browser's short temporary directory is separately recorded and validated.
Each normal run cleans itself; repeat cleanup is safe after the runner exits.

Second-operator checklist (independent human reproduction: **NOT_PERFORMED**):

1. Use the setup above in a clean checkout and verify `doctor` prerequisites.
2. Run `--scenario restore-fresh-postgres` with a new absolute output path.
3. Inspect the actual dump digest and pre-startup checkpoint-A equality, including
   both tournaments, relationships, schema constraints, receipts and audit rows.
4. Confirm the reported checkpoint-B receipt is absent, read-only mutation was
   denied, and the restored partial-timer save did not finalize the event.
5. Confirm the runner's real authenticated new score persisted durations
   `11.1, 12.1, 10.1, 15.1`, positions `2, 3, 1, 4` and payouts `50, 25, 100, 10`.
   This step executes the new write; it does not insert oracle values into the
   restored database. Read the judge/spectator checks and cleanup result.
6. Run a fresh core suite again. Compare outcomes, not incidental timings/UUIDs.

Core-release readiness requires R01–R03 plus relevant regressions and cleanup.
Extended-suite completion additionally requires applicable R04–R10 results.
Local PostgreSQL 18 evidence does not establish the live server major; hosted
PostgreSQL 15/16 lanes are separately identified. Chromium emulation does not
qualify phones, radios, browser-process/device reboot or iOS background behavior.
The synthetic age exercise does not establish operational key availability.
Provider recovery and independent human reproduction remain separate gates.

## Defect case: a retained score could not renew replay authority

The real browser retained a post-commit score after the origin was killed and
recovered its durable receipt after restart. Replacing its expired CSRF token
then failed: Flask-WTF was registered with `scoring.replay_offline_score`, a
blueprint endpoint string, while its exemption lookup uses a Python view
module/name. Existing fixtures disabled both CSRF settings and missed the path.
The factory now registers the actual replay callable. New tests enable both
CSRF settings, prove ordinary form protection remains active, reject missing or
forged current-timestamp HMACs, and read committed result/receipt state.
The same drill also exposed an edit-lock version incorrectly changing the
prepared schedule fingerprint. It now uses the existing heat identity; a real
stand change still invalidates the package, and newer scores still conflict.

## Managed-event extensions (R11–R12)

R01–R10 retain their identities and assertions. R08 uses a simulated shadow
receiver; it does not prove MNEMEX official-result ingestion.

- R11 `managed-component-portability` runs the installed STRATHMARK CLI through
  two different synthetic configurations, full qualifying/semi/final progression,
  restart, correction and per-attempt export.
- R12 `managed-acceptance-crashes` uses real PostgreSQL/Gunicorn plus the separately
  installed numerical component. It kills the owned worker before host acceptance
  commit and after commit before response; retries prove atomic attempt/delivery
  projection and no duplicate award effect. Fault injection exists only in the
  owned launcher, with no public fault endpoint/header.

Supply `--managed-python /absolute/installed/env/bin/python` for these scenarios.
For example, append that option to the normal `run --scenario
managed-acceptance-crashes --output /new/owned/path --pg-bin ...` invocation.
Absent interpreters/tools produce explicit blocked evidence. `--suite all` also
selects the managed extensions; core CI retains its existing three scenarios.

The actual cross-repository HTTP proof is
`PROAM_MANAGED_TEST_PYTHON=/absolute/producer/python
PROAM_MNEMEX_TEST_PYTHON=/absolute/mnemex/python python -m pytest
tests/test_managed_events.py`. Its receiver subprocess uses the installed Django
application and new SQLite storage, tests downtime, correction/void streams and
a genuinely committed lost HTTP response, and terminates only its owned server.
It never contacts providers or activates production.
