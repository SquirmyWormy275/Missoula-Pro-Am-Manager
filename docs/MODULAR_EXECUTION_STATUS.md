# Modular tournament execution status

Owner authorization: `CODEX_Modular_Tournament_Execution_Handover.md`, 2026-10-15.
Canonical [architecture agreement](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/MODULAR_ECOSYSTEM.md).

## Repository record

All remotes fetched 2026-10-15; no open PRs at reconnaissance. All work uses
`feature/modular-events-20261015` in isolated worktrees under
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
| Missoula seeded route-smoke DB, isolated SQLite full suite excluding migration-integrity | Reached 100%, no failure output; process exit receipt lost during session interruption. Rerun required before verification claim. |

Existing reusable work: real V2 calculations/receipts, full separate V3 Linux
lifecycle and backup, retro terminal, Missoula R01–R10 process recovery lab,
MNEMEX reviewed SB/UH export and Django identity/consent/Results Desk, historical
benchmark/accuracy/prospective/preview tools. Missing: shared higher-level
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
| STRATHMARK | `de878a477f1d9ecae1c5190cb04318c8e957a78f`, documentation clarification `3bf1a5c0a55ef55eaa8c76b9c84135e1eac0c791`; [PR 35](https://github.com/SquirmyWormy275/STRATHMARK/pull/35), Required CI running; rc11 publication pending main merge/CI |
| Missoula | nullable rollback-compatible model fix `8c0ae904d19cf618d7b8b2460e540cb3414a9d96`; managed integration `e16893e`; branch `feature/modular-events-20261015`; PR pending producer publication; merge BLOCKED by hosted backup/recovery proof below |
| STRATHEX | `382561b`; [PR 34](https://github.com/SquirmyWormy275/STRATHEX/pull/34), draft pending exact producer publication; version 7.4.3 |
| MNEMEX | implementation `1d2bd2942d051403b3406979a1e567b4475acd7c`; [PR 5](https://github.com/SquirmyWormy275/mnemex/pull/5) MERGED at `7f96f798317ac3b1afd7e0a37eb021c6099389a4`; Linux/Windows PR and main CI passed; no deployment |

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
invalidated and issued progression needs director review. Every accepted attempt
has source/revision/issue/field/clock evidence; the final summary is separate.
The optional GUI has scoped authenticated sessions, revocation, CSRF/origin
checks, native forms, responsive layout, sync status and independent print sheets.
CLI/GUI handover changes event revision without changing physical field identity.

Missoula deliberately links only fresh supported individual SB/UH events,
validates all active entries, division/wood, declared gear and physical stands,
and projects module fields for host scheduling. Retained host and shadow modes
remain independent. Legacy managed scoring/placing mutations refuse competing
authority. Host acceptance commits attempts, delivery intents, projection and
receipt together. Points/payouts use accepted source positions through an
explicit host action; payout edits preserve that boundary. MNEMEX delivery is a
bounded supervised batch, never part of scoring/finalization. ProductionConfig
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
source revisions, durable conflicts and audit/idempotency atomicity. All received
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
| STRATHMARK full isolated portable suite | 3242 passed, 47 skipped; `producer-full-final/{pytest.log,junit.xml}`; final authority-location change additionally verified by the 20 focused managed tests; exact final revision reruns in Required CI |
| STRATHMARK focused final authority/UI/process/evaluation | 20 passed; `producer-authority-final/{pytest.log,junit.xml}` |
| Installed producer wheel and sdist, outside checkout | PASS supplied-data CLI, real two-show qualifying/semi/final lifecycle through separate processes, correction/export and synthetic evaluation; `producer-smoke-final-{wheel,sdist}2.log` |
| Core-only installation | PASS without Flask, MNEMEX or STRATHEX; `core-optional-absence.log` |
| Real installed browser | PASS login, CLI handover/native fields, real engine/review/issue, raw timers, both clocks, stale edit, normal CSRF, wrong-show access, print identity and mobile no-overflow; `browser-workspace-7/report.json` plus synthetic screenshots/HTML |
| Installed two-show CLI | PASS; `installed-core-final/report.json`; Missoula and second configuration run unchanged component with different wood/capacity/heat structure |
| V3 replay / frozen contract | PASS; `producer-replay-final.log`, `producer-contract-final.log`; no frozen hashes rewritten |
| Missoula SQLite full fast lane | 4492 passed, 43 skipped (4535 total); `host-full-final.xml`; migration-integrity checks run separately |
| Missoula full PostgreSQL | 4521 passed, 33 skipped (4554 total), exit 0; `postgres-full-final/{junit.xml,pytest.log,report.json}`; owned cluster cleanup PASS |
| Current payout changes | 94 passed, 3 skipped; `host-payout-broad.xml`; actual producer/MNEMEX contract and direct payout subset 7 passed in `host-payout-final2.xml` |
| Release/migration/recovery guards | 49 passed, 1 skipped; `host-release-current.xml`; PostgreSQL migration repair/concurrent delivery also executed in full PG lane |
| MNEMEX full isolated SQLite | 629 passed, 53 optional skipped; `mnemex-full-final.xml` |
| MNEMEX narrow inbox on PostgreSQL | 10 passed; `postgres-full-final/{mnemex-junit.xml,mnemex.log}` |
| MNEMEX distribution | wheel/sdist built; installed wheel imported from site-packages outside checkout; Django check/migration drift PASS; `mnemex-installed-wheel-check2.log` |
| STRATHEX full suite | 380 passed; `strathex-final.xml` |
| STRATHEX installed retro + managed clients | PASS wheel/sdist inspection and disposable-workbook startup/exit; supplied-data creation/resume and wrong-show refusal without legacy startup; `strathex-installed-final.log`, `strathex-managed-installed.log` |
| Ruff/docs/format/diff | PASS applicable repository checks; hosted CI repeats exact revisions |

Recovery receipts preserve failures rather than overwriting evidence:
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

R11 runs installed real-engine two-show CLI portability. R12 uses actual
PostgreSQL/Gunicorn worker death before acceptance commit and after commit before
response. Retries produce one acceptance and exact append-only attempt/delivery
counts with no award side effect. The independent three-repository test launches
the installed Django HTTP receiver, including downtime and an actually committed
lost response. R08 remains explicitly a simulated **shadow** peer.

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
`7f96f798317ac3b1afd7e0a37eb021c6099389a4`, and runs actual HTTP contracts and
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
STRATHMARK rc11 publication and separate wiki synchronization are tracked at
closeout; publication is not official activation.

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
