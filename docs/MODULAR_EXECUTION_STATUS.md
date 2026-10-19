# Modular tournament execution status

Owner authorization: `CODEX_Modular_Tournament_Execution_Handover.md`, 2026-10-15.
Canonical [architecture agreement](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/MODULAR_ECOSYSTEM.md).

## Current delivery and resumption point

Documentation refreshed 2026-10-16 after fetching all four repositories. The
latest application-code commit is
`97769be6c1adc323d511ed2fa5dd6d851c8b6539`, with fresh 220-test SQLite,
157-test PostgreSQL, 146 legacy heat regressions and clean-source R09/R12
verification. All 31 findings raised so far are fixed/resolved. Earlier
documentation 96951ed passed all eight applicable CI jobs (production audit
skipped), before its review identified the latest three defects. The final
head's CI/review have their own status; consult the current PR checks.
Earlier full CI/review checkpoints below remain explicitly source-bound.
Later documentation commits must record their own checks.

| Component | Delivered state | Verified revision / record |
| --- | --- | --- |
| STRATHMARK | MERGED, VERIFIED, PUBLISHED | Main `f184e9f2e110bd01d5fec2abd2ef1bfe6f22e214`; package `3.0.0rc11`; [PR 35](https://github.com/SquirmyWormy275/STRATHMARK/pull/35) |
| STRATHEX | MERGED, VERIFIED; retro CLI preserved | Main `bcfe4e50e1449b2585a037215c79f5a7e955e9da`; version `7.4.3`; [PR 34](https://github.com/SquirmyWormy275/STRATHEX/pull/34) |
| MNEMEX | MERGED, VERIFIED; sandbox source-result contracts | Application main `7a315946c92e24a125fc213c7d139eef6278f12c`; [PR 7](https://github.com/SquirmyWormy275/mnemex/pull/7); full profiles/provider activation remain separate |
| Missoula | IMPLEMENTED, VERIFIED, PUSHED; merge BLOCKED | [PR 125](https://github.com/SquirmyWormy275/Missoula-Pro-Am-Manager/pull/125) remains draft; production main stays `3a61ec7e519744f90f3e80f1a95eaab526b34af0` |

[CI 38897926622](https://github.com/SquirmyWormy275/Missoula-Pro-Am-Manager/actions/runs/38897926622)
passed all eight applicable jobs on 47f9bb5, including full SQLite and PostgreSQL
suites. The production tournament audit stayed skipped. At that checkpoint automated review completed with no findings and all 18
then-existing review threads were resolved. A later review identified three
additional defects; their failure/fix evidence is recorded below.
The [final verification comment](https://github.com/SquirmyWormy275/Missoula-Pro-Am-Manager/pull/125#issuecomment-6215277443)
records these results without changing the tested commit.

Downloaded installed-integration evidence records **49 tests passed, zero
failures/errors/skips**. R01–R03 pass 90 checks, R11 portability passes seven,
and R12 managed acceptance passes 13; every cleanup passes. GitHub tested
synthetic merge `bc89099c97a01c6a682b34ac8f1cd0ae3b75acee`, whose parents are
production main and 47f9bb5. The managed receipts' dirty flag is solely generated
`managed-results.xml`; reconstructing its collector bytes yields
`e16f71a453059345780a389693645fcaef474bf4a575f9b6f148b74dea29b8d5`.
Local clean 0727e9b independently passes R12 and historical rollback R09, 13
checks each, cleanup PASS. Earlier failures and intermediate receipts below are historical evidence,
not current pass claims. Current follow-up review/check status is separate.

Durable workstation inventory: `../evidence/REPOSITORY_STATUS.json`, captured
2026-10-16 12:15 UTC, contains full repository refs, CI/review status, published
artifact digests, wiki heads and evidence hashes. Refresh remote refs before
resuming; this inventory is a dated receipt, not a live status service.

**Next operational step:** designated backup/recovery operators must supply the
exact-current-head hosted backup/runner restore/encrypted-artifact proof and the
separately held approved recovery-identity decryption/restore rehearsal required
by the [release checklist](RELEASE_CHECKLIST.md#hosted-backup-restore-proof).
Then verify current CI/review and merge through the permitted process. Any branch
change requires fresh exact-head proof. Main triggers Railway migrations and
deployment; perform the documented deployment smokes afterward. Do not merge or
dispatch operational-data tests merely because synthetic CI is green.

No new service was DEPLOYED or OFFICIAL-USE ENABLED. Private historical runs are
NOT_RUN; prospective evaluation and installation-specific numerical qualification
remain BLOCKED under their original input/identity requirements. The separate
[MNEMEX project memory](https://github.com/SquirmyWormy275/mnemex/blob/main/docs/project-memory/START_HERE.md)
provides the model-neutral design/context entry point; this file remains the
implementation evidence record.

## Follow-up from the documentation review — 2026-10-16

Documentation-only source `70109349dc668a73b30f1cd48fc33b201ab2375c` passed
all eight applicable jobs in
[CI 38982953991](https://github.com/SquirmyWormy275/Missoula-Pro-Am-Manager/actions/runs/38982953991).
Its fresh review identified three real consistency defects. Application commit
`62216f492facd3691293bd52d0d3884fe20974c1` fixes them:

- Awards revalidate current active registration, show wood and host profile under
  the tournament lock even after a finalized export was previously accepted.
- An unissued roster shrink removes only unused pending result placeholders.
  Any score, including a recorded zero timer, reviewed mark, award/settlement,
  flag or other result history blocks deletion and preserves the old mapping.
- Every projected source config validates fresh show wood and physical stands,
  even when a shared editor changes wood without changing the roster.

Five installed-component regression cases reproduced the defects before the fix.
The first RED pass also exposed three test-fixture setup errors; after correcting
unique engine identity and RPC/config envelope shape, the final three cases
failed for the actual shrink/wood defects. Both original logs are retained;
test setup errors are not claimed as production bug reproductions.
`host-context-red.xml` retains the two genuine award failures;
`host-context-red-valid.xml` retains the two shrink and one wood failures.
The corrected code passes **159 SQLite and 96 PostgreSQL tests, zero skips**, plus
ruff. PostgreSQL namespace/resource cleanup passes. Policy-only fixtures stub
only their literal synthetic peer; installed integration still uses the actual
component and current host registration.

Fresh R12 source/acceptance-crash and R09 historical rollback drills each pass all
13 checks with cleanup PASS at clean source 62216f4. Receipts:
`../evidence/host-context-final.xml`, `host-context-postgres/report.json`,
`recovery-host-context-managed-acceptance-crashes/report.json`, and
`recovery-host-context-code-schema-rollback/report.json`.
[Exact application CI 38984242587](https://github.com/SquirmyWormy275/Missoula-Pro-Am-Manager/actions/runs/38984242587)
passes all eight applicable jobs, including the complete host SQLite/PostgreSQL
suites and installed managed integration; the production audit stays skipped.
All three new findings are fixed/resolved; the latest automated review is
tracked separately on the PR. Later documentation-head checks remain distinct.
These proofs do not satisfy the hosted backup/key gate or authorize deployment.

## Host-context serialization follow-up — 2026-10-16

Review at 62216f4 identified three further defects, now fixed in application
`1fc0897fda0d9e5567328af75640ae4d3078fa5b`:

- Woodboss/gear writes can omit the tournament parent lock. Managed projection,
  acceptance and awards now lock underlying PostgreSQL wood/competitor rows, or
  reserve the real host SQLite writer, through the actual transaction commit.
  New parent-referencing inserts are protected by the PostgreSQL parent lock.
- New physical field projection validates current entries, divisions and gear
  even when the source roster bytes are unchanged. Receipt-only recovery of a
  committed forecast with no fields remains available after registration edits.
- Incomplete/nonfinite/nonpositive wood produces a managed validation message;
  invalid gear declarations are translated into the same handled error boundary.

Eleven new installed-component cases first produced nine genuine failures and
two already-passing invalid-size cases. An intermediate run then caught the
retained no-field forecast recovery regression and the previously uncaught gear
validation type; both were fixed without weakening their assertions. The final
suite passes **170 SQLite and 107 PostgreSQL tests, zero skips**, plus ruff;
PostgreSQL cleanup PASS. Four races use independent writers deliberately omitting
the parent lock and prove they cannot cross acceptance/award commit.

Fresh R12 and R09 each pass 13 checks with cleanup PASS at clean committed source
1fc0897. Receipts: `../evidence/host-context-serialization-red.xml`,
`host-context-lock-verified.xml`, `host-context-lock-verified-postgres/report.json`,
`recovery-context-lock-managed-acceptance-crashes/report.json` and
`recovery-context-lock-code-schema-rollback/report.json`. The first intermediate
failing run remains retained separately. All 24 findings raised so far are
fixed/resolved; the new exact-head review is tracked on the PR.
Documentation c201c9e independently passed all eight applicable jobs in
[CI 38984919421](https://github.com/SquirmyWormy275/Missoula-Pro-Am-Manager/actions/runs/38984919421).
The new implementation/final documentation receive fresh checks and do not
inherit that pass. The hosted backup/key merge gate remains unchanged.

## Initial binding and malformed-response follow-up — 2026-10-16

Review at e79ac4a raised four further findings, fixed in application
`e8bbc42f4250e2e79e3be4ed3aef3614aa4edae0`:

- Validate both supplied/stored wood diameters within the managed error boundary.
- Reconcile empty stale pending results on initial binding and refuse all recorded
  result history, including a zero timer, before delegating a fresh event.
- Lock and revalidate current host facts after the durable-intent commit, holding
  them across actual source creation, projection and host commit. Tests include
  wood/gear writers at the RPC boundary and edits in the intent-commit gap.
- Non-object MNEMEX acknowledgement JSON leaves delivery pending with its retryable
  invalid-receipt status instead of an uncaught AttributeError.

The related boundary audit also rejects non-object local RPC envelopes/results
and enforces the existing component show-ID grammar before allocating a link;
malformed scope values cannot cause SQL errors or an unrecoverable pending scope.
No engine, numerical policy, schema, production setting or legacy workflow changed.

The four findings produced 13 failing cases and one already-rejected NaN case.
The related RPC/show-ID audit reproduced another 11 failures. All 25 new cases
now pass. Consolidated final suites pass **195 SQLite and 132 PostgreSQL tests,
zero skips**, with PostgreSQL cleanup PASS; ruff/diff checks pass. The expanded
owned verification runner's ceiling increased to accommodate the added cases;
no source-operation timeout or assertion was weakened.

Fresh clean e8bbc42 R12 and R09 each pass all 13 checks with cleanup PASS.
Receipts: `../evidence/managed-binding-input-red.xml`, `managed-rpc-shape-red.xml`,
`managed-show-identity-red.xml`, `managed-binding-current-sqlite.xml`,
`managed-binding-current-postgres/report.json`,
`recovery-binding-final-managed-acceptance-crashes/report.json` and
`recovery-binding-final-code-schema-rollback/report.json`.
All 28 findings raised so far are fixed/resolved. Final checks/review are bound to
the current PR head; older source e79ac4a passed eight CI jobs in run 38985815198
but its subsequent review found these four defects. Earlier green reviews/runs
remain dated evidence, not a claim of permanent correctness. Merge remains blocked
by the same hosted backup and independent recovery-identity proofs.

## Repository record

All remotes fetched 2026-10-15 and refreshed throughout delivery on October 16;
no open PRs at reconnaissance. Initial work uses
`feature/modular-events-20261015`; MNEMEX review fixes use the corresponding
`fix/show-*` branches. All are isolated worktrees under
`~/Repo-Updates/modular-tournament-20261015`. Original checkouts and unrelated
branches are untouched.

| Repository | Baseline main | Process / consequences |
| --- | --- | --- |
| STRATHMARK | adbf67a908946d3467db02e3472654c562170c45 | PR, Required CI; new version automatically publishes wheel/sdist to PyPI and GitHub |
| Missoula | 3a61ec7e519744f90f3e80f1a95eaab526b34af0 | PR, all application/PG/recovery checks; main triggers Railway migration/deployment |
| STRATHEX | 4ab8b9a8630df1bb7de1da9a1ea6cddbfb73d2ff | PR, Required CI; separate wiki publication |
| MNEMEX | 207a3294576ec819416e4f4a70c4f078b4a3ae34 | PR, Linux/Windows Django and synthetic tests; no production activation |

## Baseline and compatibility

Fresh environments: Python 3.13.15 for STRATHMARK/STRATHEX/MNEMEX, Python
3.10.21 for Missoula. Dependencies installed from each checkout's declared
extras/requirements; STRATHMARK uses `requirements/v3-release.lock`.
All paths and workbooks are synthetic, unique and isolated before collection.
Evidence directory: `../evidence/baseline-{strathmark,missoula,strathex,mnemex}`.

| Baseline | Result |
| --- | --- |
| STRATHMARK `pytest tests -q --tb=short --basetemp <unique> -p no:cacheprovider`, portable Formula mode | 3222 passed, 47 skipped; one installed-wheel prerequisite failure (new uv venv lacked pip). Installed pip and reran that test: 1 passed. |
| STRATHEX same isolated full-suite command | 379 passed |
| MNEMEX isolated `mnemex.web.settings.test` full suite | 619 passed, 53 skipped |
| Missoula seeded route-smoke DB, isolated SQLite full suite excluding migration-integrity | Reached 100%, no failure output; baseline exit receipt was lost during interruption. No baseline pass is claimed; subsequent independently recorded full local and CI reruns are listed below. |

At reconnaissance, reusable work included real V2 calculations/receipts, full separate V3 Linux
lifecycle and backup, retro terminal, Missoula R01–R10 process recovery lab,
MNEMEX reviewed SB/UH export and Django identity/consent/Results Desk, historical
benchmark/accuracy/prospective/preview tools. Missing at that baseline: shared higher-level
operations, supplied-data CLI, managed host projection, optional shared GUI,
source result inbox/outbox integration and cross-show acceptance.

## Domain regression checklist

Run the existing full Missoula suite, including owner requirements, partner
claims/repair, gear groups, LH springboard, Stock Saw 7/8, run swaps, Saturday
relay/spillover and closing order, full-show atomicity, timer completeness,
scoring/ties/corrections, offline receipts, reporting and payout tests.
Managed mode must additionally block competing legacy mutations and retain
every attempt, source result revision and host acceptance receipt.

## Implemented delivery — 2026-10-16

`IMPLEMENTED` is separate from `VERIFIED`, `MERGED`, `PUBLISHED`, `DEPLOYED`
and `OFFICIAL-USE ENABLED`. The current implementation is synthetic-only.
No operational database, workbook, live signing authority, provider account or
private historical/training corpus was used for these checks.

| Repository | Implementation / delivery |
| --- | --- |
| STRATHMARK | final reviewed source `0839f640b8f1a02270a8275246e5801260458391`; [PR 35](https://github.com/SquirmyWormy275/STRATHMARK/pull/35) MERGED at `f184e9f2e110bd01d5fec2abd2ef1bfe6f22e214` after all 25 checks passed and all six review findings were fixed/resolved; rc11 PUBLISHED to [PyPI](https://pypi.org/project/strathmark/3.0.0rc11/) and [GitHub prerelease](https://github.com/SquirmyWormy275/STRATHMARK/releases/tag/v3.0.0rc11) after successful main CI and exact artifact/extra installation checks |
| Missoula | managed integration and follow-up fixes through application source `e8bbc42f4250e2e79e3be4ed3aef3614aa4edae0` (195 SQLite / 132 PostgreSQL, clean R09/R12); historical full-CI documentation checkpoints `47f9bb558816e07f35d2c54f3ed4454f0c6fc21e` and `70109349dc668a73b30f1cd48fc33b201ab2375c`; branch `feature/modular-events-20261015`; [PR 125](https://github.com/SquirmyWormy275/Missoula-Pro-Am-Manager/pull/125), draft; historical all-eight-job runs `38897926622` and `38982953991` passed; 28 findings fixed/resolved through e8bbc42 with current review/check status tracked separately; merge BLOCKED by hosted backup/recovery proof. Intermediate commits and failure/fix receipts are retained in the dated sections below. |
| STRATHEX | `bcf7ffc19c3796ac189c6dd8c2c59d2abb472f89`; [PR 34](https://github.com/SquirmyWormy275/STRATHEX/pull/34), MERGED at `bcfe4e50e1449b2585a037215c79f5a7e955e9da`; both findings fixed/resolved; all 11 PR checks and main run `38885236056` passed, including installed published rc11; version 7.4.3 |
| MNEMEX | implementation PR 5 MERGED at `7f96f798317ac3b1afd7e0a37eb021c6099389a4`; binding/scope PR 6 MERGED at `43b1d9382da0e0b7f56eaa98e368c5a5b095590d`; provenance/security source `83d9e6d296b990ba01884fc04c5662fea36c6eb6` in [PR 7](https://github.com/SquirmyWormy275/mnemex/pull/7) MERGED at `7a315946c92e24a125fc213c7d139eef6278f12c`. Each passed exact-head Linux/Windows CI; PR 7 automated review completed with no findings; all earlier eight findings fixed/resolved; no deployment |

### Working features and boundaries

STRATHMARK provides independent calculation-only requests, persistent shared
operations, supplied-data CLI, local trusted-process RPC and an optional scoped
Flask workspace. Native field/stand forms, whole-sheet review, separate issue
confirmation, dual raw timers, explicit partial-timer review, result acceptance,
advancement/tie decisions, finalization and corrections use one authority.
Root numeric/runtime selection, exact retry receipts and revision conflicts
survive restarts. New fields rebase using the real V2 predictor. Existing V2/V3
numeric code and frozen contracts were not changed. The explicit new
`mark3-ceil180.v1` policy differs from the preserved V2 optimizer policy.

Issued descendants remain frozen after corrections; unissued descendants are
invalidated and issued progression needs director review with retained reason,
actor/time/revision and exact issued/result digests. Finalization requires every
latest summary result to be accepted, including corrected earlier rounds. Root
engine, numeric runtime and mark policy remain fixed. Every accepted attempt
has source/revision/issue/field/clock evidence; the final summary is separate.
The optional GUI has scoped authenticated sessions, revocation, CSRF/origin
checks, native forms, responsive layout, sync status and independent print sheets.
CLI/GUI handover changes event revision without changing physical field identity.
The core-only published package has no Flask, MNEMEX or STRATHEX dependency.

Missoula deliberately links only fresh supported individual SB/UH events,
validates all active entries, division/wood, declared gear and physical stands,
and projects module fields for host scheduling. Retained host and shadow modes
remain independent. Legacy managed scoring/placing mutations refuse competing
authority. Host acceptance commits attempts, delivery intents, projection and
receipt together. Points/payouts use accepted source positions through an
explicit host action; payout edits preserve that boundary. MNEMEX delivery is a
bounded supervised batch, never part of scoring/finalization. College awards rebuild affected team totals; pro ties use the existing shared-position
purse, and saved templates use accepted source positions. A refused initial creation
can be corrected under a new durable request; an ambiguous existing source cannot
be overwritten. Source actor/request/config bind the accepted mapping, and superseded
initial intents remain conflicts. Host preparation rechecks live roster/gear/wood,
physical stand aliases and dual timers. Unissued configuration revisions reconcile
entry mappings; incompatible shared-client profiles and changing export revisions
are refused. ProductionConfig
forces managed functionality off. The host's Python 3.10 / STRATHMARK 2.x pin
is unchanged; the managed component has a separate interpreter.

STRATHEX retains the retro UI and original save/workbook/engine contracts.
`--managed-cli-python ... --managed ...` and `--managed-workspace-url ...
--managed-event-id ...` branch before legacy startup and access shared operations.
`requirements-managed-runtime.txt` pins rc11 separately. Existing V3 pair and
Linux competition CI pins remain unchanged. The new installed-client CI gate
requires the exact producer version; no automatic fallback or save migration.

MNEMEX accepts a closed, scoped synthetic show-result contract with expiring,
revocable hashed credentials and registered source ownership. Actual machine
HTTP ingestion/status preserve exact retries, reordered corrections, immutable
source revisions, durable conflicts and audit/idempotency atomicity. Rejected
validated envelopes and authenticating credential identities are
retained immutably; legacy unknown provenance is not invented. Idempotency is
source-scoped with safe recovery of earlier client-scoped receipts. Both machine
endpoints enforce activation/sandbox/source/credential checks. Credential files
are private from exclusive POSIX creation. All received
identities remain unlinked; no name linking, consent promotion, public career
publication or 2.x history-export widening occurs. Full profile portability and
all 14 hosted activation gates remain separate unfinished programs.

### Verification receipts

Evidence root on this workstation:
`~/Repo-Updates/modular-tournament-20261015/evidence`.
Fresh Python 3.13.15 producer/STRATHEX/MNEMEX and Python 3.10.21 host environments;
PostgreSQL 18.6 is an owned loopback-only cluster in a new network namespace.
Chromium 152.0.7977.82 ran through Playwright against the installed workspace.
No SQLite result is labeled PostgreSQL or Windows/provider qualification.

| Check | Actual result / retained receipt |
| --- | --- |
| STRATHMARK full isolated portable suite | 3242 passed, 47 skipped; `producer-full-final/{pytest.log,junit.xml}`. Final 0839 PR run `38878110636` passed all 25 checks; merged f184 main run `38881349305` attempt 2 passed all 25 checks. |
| STRATHMARK focused final authority/UI/process/evaluation | Original 20 passed; review reproduced seven additional failures, then final 29 passed in `producer-reviewed-commit/{pytest.log,junit.xml}`. Exact reviewed PR CI passed all 25 checks (`38878110636`). |
| Installed producer wheel and sdist, outside checkout | PASS on reviewed 0839 artifacts: supplied-data CLI, real two-show qualifying/semi/final lifecycle through separate processes, per-attempt export and synthetic evaluation; `producer-reviewed-{wheel,sdist}.log`; R11/R12 repeated against this wheel. |
| Core-only installation | PASS without Flask, MNEMEX or STRATHEX; `core-optional-absence.log` |
| Real installed browser | PASS login, CLI handover/native fields, real engine/review/issue, raw timers, both clocks, stale edit, normal CSRF, wrong-show access, slash-bearing heat print identity and mobile no-overflow; reviewed wheel `browser-workspace-9/report.json` plus synthetic screenshots/HTML. Native director retention additionally passes focused tests. |
| Installed two-show CLI | PASS; `installed-core-final/report.json`; Missoula and second configuration run unchanged component with different wood/capacity/heat structure |
| V3 replay / frozen contract | PASS; `producer-replay-final.log`, `producer-contract-final.log`; no frozen hashes rewritten |
| Missoula SQLite full fast lane | 4492 passed, 43 skipped (4535 total); `host-full-final.xml`; migration-integrity checks run separately |
| Missoula full PostgreSQL | 4521 passed, 33 skipped (4554 total), exit 0; `postgres-full-final/{junit.xml,pytest.log,report.json}`; owned cluster cleanup PASS |
| Selected component outage | PASS actual absent interpreter: retained pending request/projection, unrelated registration committed, exact recovery against restored interpreter; three integration tests passed in `host-component-outage.xml` |
| Delayed response / cached acceptance | Three RED failures reproduced read/write cache regression and stale ORM acceptance after parent lock. Host now reloads the locked link and updates fields/cache only forward, refuses export older than a known correction, and locks/reloads before awards. All eight managed tests pass on SQLite and PostgreSQL 18.6 (`host-projection-final.xml`, `host-projection-postgres-final/{junit.xml,report.json}`), cleanup PASS; 42 scoring/payout regressions pass (`host-projection-awards.xml`). Both runs include actual installed producer and MNEMEX HTTP. |
| Current payout changes | 94 passed, 3 skipped; `host-payout-broad.xml`; actual producer/MNEMEX contract and direct payout subset 7 passed in `host-payout-final2.xml` |
| Release/migration/recovery guards | 49 passed, 1 skipped; `host-release-current.xml`; PostgreSQL migration repair/concurrent delivery also executed in full PG lane |
| MNEMEX full isolated SQLite | 629 passed, 53 optional skipped; `mnemex-full-final.xml` |
| MNEMEX correction/scope follow-up | Three RED cases reproduced direct correction entry/issued-binding poisoning and cross-show conflict counts. Full fixed suite 632 passed, 53 skipped (`mnemex-binding-full.xml`); narrow PostgreSQL 13 passed, cleanup PASS (`mnemex-binding-postgres/report.json`); rebuilt installed wheel check and real Missoula HTTP test PASS (`host-hardened-receiver.xml`). Additive partners 0004 retains unattributed old conflicts as null/private. |
| MNEMEX provenance/security follow-up | Six RED cases reproduced remaining defects. Full 638 passed / 53 optional skips (`mnemex-provenance-full.xml`); narrow PostgreSQL 19 passed, cleanup PASS (`mnemex-provenance-postgres-2/report.json`). Additive partners 0005 retains legacy unknown evidence as nullable. Rebuilt installed receiver + real host HTTP: three passed (`host-provenance-components.xml`). Initial PG run passed 19 tests but cleanup refused pytest-current links; retained receipt and exact-owned link reconciliation in `mnemex-provenance-postgres/cleanup-reconciliation.json`; fresh rerun uses temp paths outside guarded private resources and passes cleanup. |
| MNEMEX narrow inbox on PostgreSQL | 10 passed; `postgres-full-final/{mnemex-junit.xml,mnemex.log}` |
| MNEMEX distribution | wheel/sdist built; installed wheel imported from site-packages outside checkout; Django check/migration drift PASS; `mnemex-installed-wheel-check2.log` |
| STRATHEX full suite | 381 passed after parser-level RED regression for portable encrypted-backup flags; `strathex-review-final.xml` (earlier 380 in `strathex-final.xml`). |
| STRATHEX installed retro + managed clients | Reviewed bcf7 wheel PASS: sdist inspection and disposable-workbook startup/exit; supplied-data creation/resume and wrong-show refusal without legacy startup; `strathex-reviewed-installed-{retro,managed}.log`. PR/main installed CI also passes against indexed rc11. |
| Ruff/docs/format/diff | PASS applicable repository checks; hosted CI repeats exact revisions |

The earlier complete implementation rerun, `recovery-qualified-final/report.json`, passes all
12 scenarios and 197 assertions with every cleanup PASS. The stronger R11
CLI-correction rerun, `recovery-r11-cli-correction/report.json`, additionally
voids a final attempt, accepts/re-finalizes on both shows through installed CLI
processes, preserves the issued identity and verifies 16 source revisions versus
eight summary entries. Earlier receipts preserve failures rather than overwriting evidence:
`recovery-all-final/report.json` records R01–R04 and R06–R12 PASS with cleanup
PASS, and R05 FAIL because the old harness restored a hard-coded old Alembic
stamp to its otherwise intact synthetic source. The corrected harness retains
the actual starting head. `recovery-r05-fixed/report.json` then passes all 15
invalid-artifact/encryption/decryption/semantic-restore checks, cleanup PASS.
`recovery-r09-current/report.json` separately passes with truthful baseline head
`a6b7c8d9e0f1`, candidate head `a6d6dda4e480`, additive schema changes true.
R09 retains the historical baseline commit, receipt retry and scratch scoring
assertions. Old code ignores new managed tables; this is not a downgrade of
accepted managed data.

R11 runs installed real-engine two-show CLI portability and corrections. R12 uses actual
PostgreSQL/Gunicorn worker death before acceptance commit and after commit before
response. Retries produce one acceptance and exact append-only attempt/delivery
counts with no award side effect. The independent three-repository test launches
the installed Django HTTP receiver, including downtime and an actually committed
lost response. R08 remains explicitly a simulated **shadow** peer.

Reviewed producer artifacts repeat both R11 and R12 with PASS/cleanup PASS in
`recovery-r11-reviewed` and `recovery-r12-reviewed`. Canonical tuple hashing now
prevents delimiter collisions in source attempt IDs. Root source-identity scheme
is immutable. The unpublished earlier 3e27 candidate wheel/sdist/checksums are
retained in `unreleased-3e27a3d`; old concatenated-ID checkpoints remain readable
but mutation is refused by the new runtime. Their original installed runtime
resumes them successfully (`legacy-candidate-compatibility/report.json`). No
existing receipt or frozen numerical identity was rewritten. Backup/transfer
destinations use exclusive atomic publication and refuse a late competing path.

Main producer run `38881349305` attempt 1 hit an existing timing-sensitive Python
3.11 shadow test: its 100 ms deadline expired during preflight instead of after
the deliberately stalled write. The API correctly reported unclaimed preflight
state. Five independent Python 3.11.16 runs of both preflight/post-admission
semantics and all 56 shadow API tests pass (`producer-311-timeout-diagnosis.log`,
`producer-311-shadow.xml`). Exact-commit attempt 2 passes all 25 checks. No timeout,
assertion, requirement or production behavior was relaxed. The automatic release
alert [36](https://github.com/SquirmyWormy275/STRATHMARK/issues/36) closed after the
same run recovered. Original failure evidence remains in `producer-main-311.log`.

The host review reproduced seven further policy/binding/guard defects in
`host-policy-red2.xml` (three earlier stale-state failures are separate).
Current code preserves college team totals and shared-position pro purses, applies
saved templates, protects tournament configuration and drag moves, refuses old
host heat layouts, and recovers refused/ambiguous initialization without deleting
its source or local intents. Further current-roster/timer/stand regressions retain
`host-policy-boundary-red.xml`: roster refusal was missing; two initial test cases
also exposed stale retained helper ORM cache, now scoped to each host validation.
The corrected 13 policy checks pass in `host-policy-boundaries-final.xml`.
The preceding shared-profile/source-export checks produce **65 passed** on SQLite and
PostgreSQL 18.6 with installed indexed rc11 and real installed MNEMEX HTTP:
`host-shared-profile-final.xml`, `host-shared-profile-postgres/{junit.xml,report.json}`;
PostgreSQL cleanup PASS. This includes the existing 42 scoring/payout regressions.
The exact source, commands, and final CI outcome remain attributable through
[PR 125](https://github.com/SquirmyWormy275/Missoula-Pro-Am-Manager/pull/125).

### Source-fenced host delivery and final review follow-up

The preceding exact head `68ec6444b5cd8fe6b0ecacacdebbb1668191a6f2` passed all
eight verification jobs in [38887910621](https://github.com/SquirmyWormy275/Missoula-Pro-Am-Manager/actions/runs/38887910621);
the production audit remained skipped. Its downloaded managed artifact contains
23 passing tests and R11/R12 PASS/cleanup PASS (`ci-final-managed`), plus the
three core drills (`ci-final-core`). Review then identified further concurrency,
competitor-cascade and pending-flight issues. Five regressions reproduce the old
behavior (`host-source-review-red.xml`). The pinned producer already refuses
non-handicap format; the additional host check now runs before linking/RPC rather
than allowing a locally pending invalid configuration.

Source `f35b3685fdd0bc81e803208fb10b4ed0483a5c7a` adds a private, exact-rc11
local export-fence adapter. It calls the unchanged component export operation,
then verifies its revision under the component's own SQLite transaction and holds
that writer fence through the host outer transaction. The 3.10 host never imports
the 3.13 component. Commit, rollback, session close and worker EOF release the
fence; nested savepoints retain it. Before commit the host checks worker liveness.
Bulk templates acquire one fence for the full selected managed batch before host
locks. Source changes before the fence refuse stale acceptance; fresh fenced
source state also gates awards. Accepted exact receipts remain immutable.
No numerical/progression implementation or package contents changed; published
rc11 remains the exact selected artifact. This is local process coordination,
not qualification of a hosted/distributed transaction profile.

Competitor-wide scratch cascades cannot rewrite mapped managed records. A
scheduled unissued field cannot disappear or change physically in place: the
operator explicitly confirms unscheduling from a pending flight, binding the
current placement digest, then recovers the retained source command and schedules
the replacement. Issued/completed/started-flight history remains protected.
Accepting editor-only metadata with an unchanged summary retains award finalization.

Verification: **108 passed** on SQLite, including 31 retained scratch-cascade
regressions (`host-source-fence-final.xml`); **77 passed** on owned PostgreSQL 18.6,
cleanup PASS (`host-source-fence-final-postgres/{junit.xml,report.json}`). Both use
indexed rc11 and actual installed MNEMEX. These tests include a real competing
component process blocked until host commit/rollback/close, nested savepoint
commit/rollback, a killed fence worker before commit, stale source export refusal,
metadata acceptance, two-event bulk payouts and explicit placement recovery.
The direct template guard additionally passes 32 checks (`host-template-fence-final.xml`).
The initial fixed run's sole remaining test failure assumed SQLite would never
reuse a deleted unissued Heat primary key; the corrected test asserts stable
module field identity, retained placement and the new field mapping instead.
Final clean f35b368 repeats R12 and R09: 13 checks each, cleanup PASS, in
`recovery-r12-source-fence` and `recovery-r09-source-fence`. No check or expected
pre/postcommit count was weakened. Latest exact-head CI/review is linked on PR 125.

### Host-owned mutex and serialized initial binding

The preceding head `853c2444b3b611e3d624d40a091666e458737773` passed all eight
verification jobs in [38891917392](https://github.com/SquirmyWormy275/Missoula-Pro-Am-Manager/actions/runs/38891917392),
production audit skipped. Downloaded `ci-source-fence-managed` records 35 passing
tests and R11/R12 PASS/cleanup PASS; `ci-source-fence-core` records R01–R03 PASS.
Further review identified the helper-death interval during DBAPI commit and
binding racing an already-running schedule writer. Both failures reproduce in
`host-owned-fence-red.xml`: a source writer advances after the old helper dies in
the engine commit hook, and old binding accepts after a legacy heat is generated.

The host-owned application slice `ee50f8c979aefd61bffa3b54942bce757aa1cf2d` supersedes
the helper-owned fence above. Private transport v2 obtains the exact rc11 export
and opaque verified state digests. It hands off to a host-owned, existing-file-only
SQLite writer mutex, then compares state-byte hashes again under that mutex.
An intervening source write is refused. The source helper has exited before host
result mutation begins, so helper death cannot release the fence during DBAPI
commit. The 3.10 host only locks and compares opaque bytes; all export, numerical,
judging and progression semantics remain in the unchanged selected component.
The exact rc11 schema/runtime binding is explicit. This is local coordination,
not hosted/distributed transaction qualification.

Binding now shares SQLite's in-process schedule-writer guard and PostgreSQL's
Tournament parent lock. It refreshes Event/link state and rechecks freshness
before committing the link. Authority-presence checks query the committed link
without flushing incidental edits, avoiding a stale relationship's cached absence.

Local evidence for this slice: **110 passed** on SQLite, including scratch regressions
(`host-owned-fence-final.xml`), and **79 passed** on owned PostgreSQL 18.6, cleanup
PASS (`host-owned-fence-final-postgres/{junit.xml,report.json}`). Tests include the
actual after-Session-before-DBAPI-commit helper exit interval with a competing
component writer, source connection loss before commit, binding versus a real
schedule writer, both savepoint outcomes, batch payouts and explicit placement
recovery. Exact clean ee50f8c passes R12 and R09 again, 13 checks each, cleanup
PASS (`recovery-r12-host-owned-fence`, `recovery-r09-host-owned-fence`). The latest
exact-head CI and review remain linked on PR 125. No numerical/package changes,
production-data dispatch, service deployment or gate waiver occurred.

### Registration, gear synchronization and correction acceptance closure

Head `0b38a431d1123fe309020a1c4ac02317c34e3cc5` passed all eight verification
jobs in [38895054087](https://github.com/SquirmyWormy275/Missoula-Pro-Am-Manager/actions/runs/38895054087),
production audit skipped. The downloaded `ci-host-owned-fence-38895054087-managed`
artifact records 37 passing tests and R11/R12 PASS/cleanup PASS; the corresponding
core artifact records R01–R03 PASS. Its tested synthetic merge checkout
`1d9c21a2c40cc1f956cb39ce34b13531afdb9818` has parents baseline main and 0b38a43.
The managed receipts' dirty patch digest is exactly reproducible from the
collector's `managed-results.xml` filename, NUL separator and downloaded bytes:
`7e6d456b608410fd967658e0de3bd62311901847264d1cc3f00d4b3fce8a1c49`.
Their sole patch is generated test output, not an unrecorded code modification.
The clean local source receipts remain independent evidence.

Further review found three host gaps: unchanged source configuration could hide
added/removed host entries at acceptance, registration gear synchronization could
rewrite managed fields, and accepting a correction before source re-finalization
could leave earlier host awards marked final. Seven RED failures reproduce them
in `registration-finality-red.xml`. Application source
`2171dcfc391066c8d42b6cc50c516f9823059ecc` fixes them:

- New acceptance takes the tournament parent lock after the source fence,
  refreshes Event and registration identities and revalidates the current active
  roster/divisions/gear against the source mapping, plus the host profile/wood.
  Already accepted exact retries return their immutable original receipt.
- Gear sync skips managed fields, whether pending or issued, reports those event
  IDs and explains the STRATHMARK action in the host UI. Independent host fields
  in the same show still repair normally.
- Accepting a non-finalized source revision reopens the host event, clears host
  finalization and accepted-summary award eligibility, and retains earlier award
  and settlement facts for explicit reconciliation. Re-finalization requires a
  fresh source acceptance and deliberate host award action; finalized metadata-only
  revisions continue to preserve award finalization.

Expanded local evidence: **136 passed** on SQLite, **87 passed** on owned
PostgreSQL 18.6, no skips/failures, cleanup PASS
(`registration-finality-final.xml`, `registration-finality-final-postgres`). The
initial fixed test run retains one new-test error: it expected an unreviewed source
correction to be a career attempt before source acceptance. The corrected test
asserts one retained official attempt before judge acceptance and two afterward;
the original seven RED failures and all official-acceptance requirements remain.
Tests cover additions, independent removal with a stale loaded identity, immutable
accepted retry, full correction/re-finalization/explicit award reconciliation, and
both direct service and actual registration route with pending/issued managed fields.
Exact clean 2171dcf passes R12 and R09 again, 13 checks each, cleanup PASS
(`recovery-r12-registration-finality`, `recovery-r09-registration-finality`). Ruff
and diff checks pass. Final exact-head CI/review is linked on PR 125. The producer
package/numeric identity and all operational merge/activation gates are unchanged.

### Pending registration deletion and retained gear-service contract

The boundary audit found the same authority problem in college competitor/team
removal: with all flights pending, the legacy completed-history check could delete
an entrant still mapped into a managed event. Four RED route cases reproduce
pending creation/unissued and issued-field deletion in `managed-deletion-red.xml`.
The shared deletion predicate now protects retained managed mappings as well as
recorded results/completed heats. Both deletion routes use it. Four fixed cases
also prove unrelated competitor/team deletion still works in the same tournament;
existing scored/scratched-history and active-flight deletion guards pass.

The preceding head d135cb0's [CI run 38897053396](https://github.com/SquirmyWormy275/Missoula-Pro-Am-Manager/actions/runs/38897053396)
failed the full SQLite lane's existing
`test_gear_repair_respects_the_left_handed_dummy[False]`: an empty new
`managed_events_skipped` key changed the legacy service's exact return contract.
`ci-registration-finality-test-failure.log` retains the original job log. The
implementation now adds that key only for an actual managed skip, and the UI
uses an optional lookup. The existing exact dictionary assertion and left-handed
stand behavior are unchanged. No test was relaxed to absorb the incompatibility.

Expanded boundary verification passes **154 SQLite tests**, including the existing
registration edge/writer checks, and **91 PostgreSQL tests** with cleanup PASS
(`managed-boundary-final.xml`, `managed-boundary-final-postgres`). Those runs
precede the return-shape compatibility fix; the subsequent focused compatibility
run passes all **10 tests**, no skips (`managed-boundary-compat.xml`), covering the
unchanged legacy left-handed assertion and managed gear/deletion route/service
behavior. Exact clean application source
`0727e9b63cdaaeb7ac994f5729d82b8114456d7d` passes R12 and R09 again, 13 checks each,
cleanup PASS (`recovery-r12-managed-boundary`, `recovery-r09-managed-boundary`).
Ruff and diff checks pass. The final exact-head full CI/review result is linked on
PR 125; all 18 prior review threads have concrete fixes and are resolved.
Operational data, package publication, numeric policy and merge/activation gates
remain unchanged by these host-only fixes.

### Published artifacts and independent indexed verification

Publication workflow [38884204894](https://github.com/SquirmyWormy275/STRATHMARK/actions/runs/38884204894)
passes all six jobs, including wheel/sdist installs and every declared extra.
Both immutable annotated tags `v3.0.0rc11` and `pypi-v3.0.0rc11` dereference to
`f184e9f2e110bd01d5fec2abd2ef1bfe6f22e214`. GitHub's release is a prerelease.
The downloaded release and PyPI metadata agree with the exact locally reviewed
artifacts; `published-artifacts/SHA256SUMS` verifies both files:

| File | SHA-256 |
| --- | --- |
| `strathmark-3.0.0rc11-py3-none-any.whl` | `7151e43e6126d704fd09b634b82c20e08cc32b74580b2feb7613e380be45fdcd` |
| `strathmark-3.0.0rc11.tar.gz` | `2f9fecc63c287ce0e69c88059c449bfd8fe0915a71a8fde27ee68bee209bcbfe` |

`pypi-publication-local-verification.log` independently verifies metadata hashes,
a fresh indexed install, dependency consistency and real demo. `pypi-two-show/report.json`
passes both full lifecycles from a new indexed core-only environment, without source
checkout imports. `pypi-core-optional-absence.json` confirms absent Flask/MNEMEX/STRATHEX;
runtime Python 3.13.15, NumPy 2.5.4, pandas 3.0.6. `evaluation-published/report.json`
and readable report reproduce all eight generated cases and matched-baseline limits.
Initial index propagation returned no rc11; the retained failed installation
`pypi-core-install.log` was retried with the same exact pin and succeeds in
`pypi-core-install-retry.log`. No version substitution occurred.

Published-package R11 and R12 pass with cleanup PASS in `recovery-r11-published`
and `recovery-r12-published`, source d672d53 clean. R11 includes both final
corrections/voids and 16 revisions versus eight summary entries. The first R11
launch refused a relative output path before creating resources; the absolute-path
retry is retained separately. The first latest-profile R12 run, `recovery-r12-profile-published`, FAILS one
assertion (cleanup PASS): its old request-level commit hook stopped after the
new export-refresh commit, before acceptance. The receipt-transaction observer
now binds the fault to the actual acceptance INSERT transaction, preserving the
original expected independent counts of zero before commit and two after commit.
`recovery-r12-receipt-transaction` passes at clean 0ae607e. The hook also avoids
importing new model modules into historical rollback code. Final clean source
`71faad29478b483ecb9b837504c84eb779c4dbc2` passes R12's 13 checks and R09's 13 checks,
cleanup PASS, in `recovery-r12-closure` and `recovery-r09-closure`. No failure
threshold was relaxed. All these are synthetic Linux evidence, not hosted/provider
recovery qualification.

### Commands and exact pins

Use the documented isolated variables **before imports/collection**. Representative
commands from the worktree (each run used a unique evidence/database/temp path):

```text
STRATHMARK_TEST_DB=1 STRATHMARK_DB_PATH=<unique-v2> STRATHMARK_V3_DB_PATH=<unique-v3> STRATHMARK_REQUIRE_FORMULA_ENGINE_VERIFICATION=0 python -m pytest tests -q --basetemp <unique> -p no:cacheprovider
python scripts/smoke_installed_distribution.py --kind wheel --dist-dir ../producer-dist
python scripts/smoke_installed_distribution.py --kind sdist --dist-dir ../producer-dist
python -I -m strathmark.events.verification --output <new-directory>
python -I scripts/verify_managed_browser.py <new-directory>
strathmark evaluate synthetic --output <new-directory>
PROAM_MANAGED_TEST_PYTHON=<installed-producer-python> PROAM_MNEMEX_TEST_PYTHON=<installed-mnemex-python> python -m pytest tests/test_managed_events.py
python scripts/recovery_drills.py run --suite all --pg-bin <owned-tools> --pg-library <owned-libraries> --tools-bin <age-tools> --managed-python <installed-producer-python> --output <new-owned-directory>
DJANGO_SETTINGS_MODULE=mnemex.web.settings.test MNEMEX_TEST_SUPABASE=0 MNEMEX_VISION_PROVIDER_KEY= python -m pytest -q -p no:cacheprovider
python scripts/verify_managed_client.py --wheel dist/strathex-7.4.3-py3-none-any.whl --managed-python <installed-producer-python>
```

Missoula CI's new managed lane installs separate host/producer/records environments,
pins `strathmark[workspace]==3.0.0rc11` and MNEMEX merge
`7a315946c92e24a125fc213c7d139eef6278f12c`, and runs actual HTTP contracts and
R11/R12. It never opts into the existing production audit dispatch. Existing
production requirements and all compatibility pins remain intact.

### Evaluation carry-forward

Existing V3 historical benchmark, accuracy audit, frozen prospective and separate
accuracy-preview implementations were retained. Replay/frozen-contract tests and
CI preview checks reproduce supported synthetic checks. The new installed
synthetic evaluator emits readable and JSON reports, including counts/reasons,
availability, abstentions, MAE/median/p90/p99/max, interval coverage **and width**,
matched-baseline cohorts and event/material/diameter/history-depth counts.
`evaluation-final/report.json` retains all eight generated cases, including four
without exact-context baseline history, with four genuinely matched baselines.
Synthetic numerical execution establishes no real-world accuracy/fairness claim.

Private historical replay/training: NOT_RUN, no additional authorization.
Prospective: BLOCKED pending at least 100 genuinely later unexamined results
from ten new competitions and exact original frozen components. Original rc6
protocol/component identities remain unchanged; modular code does not inherit
its qualification. Windows CNG/designated Formula production verification remains
BLOCKED; the retained executable evidence is source-bound and stale for changed
code (`v3-production-gate.log`), not rebound to obtain a pass.

### Operational status and blocked merge gate

No new service DEPLOYED and no new OFFICIAL-USE ENABLED state. MNEMEX's receiver
defaults off and accepts sandbox synthetic sources only. Missoula's managed
profile is hard-off in ProductionConfig, independent of environment flags.
STRATHMARK rc11 is PUBLISHED; publication is not official activation. No new
service was deployed by this effort.

STRATHMARK's separately published wiki is verified at
`bea618007ef72b465bfe9a8c0a487a5ed847e414`, sourced from exact merged main f184e9f.
`producer-merged-wiki-{publish,check}.log` records remote verification of every
versioned page. STRATHEX's separately published wiki is verified at
`43197c198b7282fad551c3a6da2a76ece1c76dd3`, sourced from exact merged main bcfe4e5;
`strathex-merged-wiki-{publish,check}.log` records every remote page check.

**Missoula merge BLOCKED:** its additive schema requires changing the daily-backup
expected head from `a6b7c8d9e0f1` to `a6d6dda4e480`. The current
[release checklist](RELEASE_CHECKLIST.md#hosted-backup-restore-proof) explicitly
requires a hosted backup run on the exact branch head and separately held
recovery-identity decryption/restore rehearsal before merging a backup workflow
change. Static tests and synthetic age keys do not satisfy either gate.
No hosted production backup/data audit was dispatched: the handover forbids using
operational data merely for tests and does not authorize a new live-data run or
claim recovery-key custody. Keep this PR unmerged until the designated operators
provide the exact-head hosted run and independently held key rehearsal, then
rerun CI if the branch changes and merge through the permitted process. Main
would trigger Railway migrations/deployment; do not use merge as activation.

After any later authorized merge, execute the existing commit/version/Alembic
public smoke, Railway log review, authenticated judge page and tournament-bound
spectator checks. Those deployment/qualification gates are NOT_RUN here.
Independent remaining gates are provider restore, real keys/models, operator
rehearsal and production approval. Existing official/shadow and retro workflows
remain available. No test threshold, release requirement or branch protection
was removed to make delivery appear complete.

## Request recovery, lock order and real show profiles — 2026-10-16

Application `97769be6c1adc323d511ed2fa5dd6d851c8b6539` fixes the three findings raised at documentation
96951ed: typed canonical host references before parsing, shared host/source lock
ordering, and concurrent durable command insertion. Creation and acceptance first
acquire the same outer order gate before either inner host/source lock domain.
The empty canonical-path `.host-order.sqlite3` mutex stays held through host
commit/rollback; helper death cannot release it. It contains no business state,
never replaces source recovery data, and must not be replaced while hosts run.
Identical concurrent request IDs reload the winning intent and recover through
normal payload/audit checks; changed payloads retain their conflict.

Related concrete regression checks also fixed two pre-intent configuration races:
registration validation now reads without creating result placeholders, which
projection creates under host locks. Known stale uncommitted retries fail before
host preparation. Malformed held-export packets fail within the managed boundary
and release coordination resources. The real show-profile matrix found Standing
Block was looking up `block_standing_block_*` instead of Woodboss's existing
`block_standing_*`; the adapter now reuses the existing canonical key helper.
Underhand, gender precedence and the general-key fallback remain aligned with
show configuration. No numerical engine, policy, schema or production setting
changed.

Retained RED receipts cover six failures plus three already-refused reference
cases, the actual lock-domain interleave, one stale retry, two preparation races
and four Standing Block lifecycle failures (four Underhand cases already passed).
An intermediate broad run exposed one bulk test fixture repeating the same wrong
Standing Block key; only that fixture key changed, with its assertions preserved.
Final focused verification passes **220 SQLite / 157 PostgreSQL tests, zero skips**,
with PostgreSQL cleanup PASS. The eight SB/UH × pro/college × M/F cases use the real
installed engine, actual gender-specific WoodConfig and inch/mm conversion through
source review/issue/outcomes/acceptance/finalization and host awards. Four concurrent
handover/configuration exact/conflicting request cases pass. All **146 legacy heat
regressions** pass, plus ruff and diff checks.

Fresh R12 and R09 each pass all 13 checks with cleanup PASS at clean committed
source 97769be. Evidence: `../evidence/managed-final-complete.xml`,
`managed-final-complete-postgres/report.json`, `managed-legacy-heat-regression.xml`,
`managed-command-races-final.xml`, `managed-real-profiles-final.xml`,
`recovery-coordination-final-managed-acceptance-crashes/report.json` and
`recovery-coordination-final-code-schema-rollback/report.json`.
The three findings and their thread resolutions are tracked on PR 125; current
CI/review state is exact-head and remains separate from earlier checkpoints.
The previous 96951ed passed all eight applicable jobs in CI 38987534485, with the
production audit skipped, before review raised these three defects. That green
checkpoint does not establish their absence. Missoula remains draft under the
unchanged hosted backup/key proof gate; no operational workflow was dispatched.
