# 2027 Full-Dress Rehearsal Receipt

Date: 2026-08-19

Branch: `rehearsal/2027-full-dress`

Deployed-code baseline: `7544ad421eae31b1084c9e3c149ff7bf2ace1734`

## Scope And Safety Boundary

This was a local, synthetic, non-production rehearsal. It exercised the
deployed code baseline plus the fixes described below. It did not access a
production database, Railway, production PII, or the optional STRATHMARK
network integration.

SQLite tests used migration-built temporary databases. PostgreSQL tests used
unique disposable clones of `proam_unit_template` and ran serially. The
rehearsal did not delete databases by prefix. The unit-test database namespace
was the same before and after the lanes: `proam_unit_44728_1` and
`proam_unit_template`. Other pre-existing local databases were outside this
rehearsal and untouched. The worktree default `instance/proam.db` did not exist
before or after the rehearsal.

The retained browser fixture is a synthetic database outside the Git worktree:
`.rehearsal-tmp/browser-rehearsal.db`.

## Rehearsal Fixture

The deterministic fixture contained:

- one `[REHEARSAL]` 2027 tournament;
- 8 college teams, 25 college competitors, and 25 pro competitors;
- 10 synthetic judges;
- completed events and results for spectator and reporting surfaces; and
- one live timed event with two heats for scoring, concurrency, and offline
  recovery.

The load harness now migrates and seeds its own database, refuses
`instance/proam.db`, refuses every pre-existing database path, prevents the
report output from aliasing either database, and requires an explicit flag to
replace an existing report. Existing aliases are detected by file identity and
authorized replacement is an atomic file swap, so the report writer never
truncates an existing target. It releases database, process-fence, worker, and
server-log resources on failure and returns a failing process status when any
aggregate, role, endpoint, activation, authentication, error-rate,
sample-count, or zero-server-error gate fails. Judge users establish
independent authenticated sessions with the real CSRF-backed login flow before
reading scoring pages. Every measured request rejects an unexpected redirect,
and every timed response includes the full body transfer. Workload ordering and
per-worker random streams use the recorded seed `2027`.

## Load Gate

The inherited zero-ramp stress shape exposed the failure mode before any
optimization: 260 users started together, producing 615 requests, 436
successful responses, and 179 request timeouts. Error rate was 29.11%, p95 was
5,314.03 ms, and the gate failed. Every response that completed returned HTTP
200, so the failure was saturation and timeout behavior rather than an
application-error response.

Profiling found two avoidable burst costs: anonymous public pages performed a
judge-only pending-heat query, and concurrent cold public-cache misses rebuilt
the same payload independently. The fixes skip the judge query for anonymous
traffic and coalesce cache fills per key without serializing unrelated keys.

The first run with exact activation and authentication accounting exposed a
third defect rather than passing around it: concurrent judge GETs attempted
optimistic ORM updates to acquire the same heat lock, producing three HTTP 500
responses and a poisoned-session failure while rendering the error page. Heat
lock acquisition is now one conditional database update; losing requests
reload the winner and render a blocked form instead of racing the heat version.
The global 500 handler also rolls back before consulting users or templates.

A subsequent zero-error run exposed a harness timing defect: its shared ramp
clock started before all 260 client threads were ready, so host scheduling
delays inflated both the observed activation spread and response latency. A
ready barrier now excludes client-launch overhead and releases every worker
against one shared ramp epoch. Neither failed run is represented as a pass.

The final operator-paced gate used the repository defaults intended to model
real polling and entry cadence:

| Metric | Result | Gate |
|---|---:|---:|
| Virtual users | 260 (200 spectator, 50 competitor, 10 judge) | n/a |
| Worker activation spread | 14.999 s actual | 15 s ramp configured |
| Steady state | 15 s | 15 s configured |
| Requests | 342 | n/a |
| Successful requests | 342 | n/a |
| Errors | 0 (0.00%) | no more than 0.50% |
| HTTP 5xx responses | 0 | 0 |
| Throughput | 11.35 requests/s | informational |
| Mean latency | 32.51 ms | informational |
| p50 latency | 7.73 ms | informational |
| p95 latency | 154.50 ms | no more than 800 ms |
| p99 latency | 361.72 ms | informational |
| Maximum latency | 563.75 ms | informational |

Role p95 latency was 56.65 ms for spectators, 47.21 ms for competitors, and
282.90 ms for authenticated judges. All 260 configured users activated, all 10
distinct judges authenticated, and all 11 expected endpoints received samples
and passed their independent 800 ms p95 and 0.50% error-rate gates. The slowest
endpoint p95 was 345.92 ms for the first scoring heat. The final gate passed.
The external report `load-report-auth-final-v8.json` has SHA-256
`df2d5209c206fdbfd7f0ff4257510e2782034438963adc9f8dca3f55ee6a01e0`.

This passing paced run does not erase the zero-ramp result. The zero-ramp mode
remains available as an explicit saturation test; it is not represented as the
expected human traffic shape.

## Automated Evidence

- Broad focused regression lane: 249 passed across rehearsal safety,
  infrastructure/cache, Flask reliability, spectator portals, API endpoints,
  and migration/listener hygiene.
- Rehearsal safety module: 33 passed, including database and report-path
  refusal, generated-database disposal, aggregate/role/endpoint gate status,
  hard-link/path-swap refusal, atomic report replacement, exact user activation
  and judge authentication counts, minimum judge login samples,
  zero-server-error enforcement, public/authenticated redirect rejection,
  full-body timing, CSRF parsing, deterministic load streams, bounded thread
  cleanup, and PostgreSQL template isolation.
- Scoring and day-of-operations regression lane: 66 passed across concurrent
  integration QA, scoring workflow, dual-timer entry, operator actions, and
  stale/invalid edge cases. This includes an eight-session same-account heat
  GET race that produced one lock/version update and eight HTTP 200 responses.
- SQLite operations lane: 160 passed across daily backup, offline assets,
  offline scoring, reporting backup/export, restore, service-worker contract,
  shadow release readiness, tournament lifecycle portals, and end-to-end
  workflow modules.
- JavaScript offline lanes: `service_worker_offline.test.js` and
  `offline_queue_shared.test.js` passed.
- Disposable PostgreSQL adversarial lane: 10 passed, comprising eight
  race-day concurrency tests, populated shadow-migration repair, and the
  locked-delivery worker.
- Disposable PostgreSQL runtime smoke: 4 passed for PostgreSQL URI selection,
  health and migration state, ORM round trip, and real `FOR UPDATE NOWAIT`.
- Migration-safety lane: 26 passed and 2 expected skips. The listener-hygiene
  tests used their own temporary SQLite database and verified the default
  database fingerprint was unchanged.

## Browser Evidence

All browser data and credentials were synthetic.

- Desktop scoring at 1440x900 rendered without overlap, hidden primary
  commands, document overflow, page errors, or console warnings/errors.
- Phone results and spectator views at 390x844 had document width equal to
  viewport width. Tables remained inside explicit horizontal-scroll
  containers, and no console warnings/errors were recorded.
- Heat 1 persisted 11.00/11.20 as 11.10 and 12.00/12.20 as 12.10.
- Two authenticated judge sessions loaded Heat 2 at different versions. The
  newer session persisted 13.00/13.20 as 13.10 and 14.00/14.20 as 14.10. The
  stale session attempted different values and received the `Score Entry
  Conflict` dialog. A separate results view confirmed that 13.10 and 14.10
  remained authoritative; no silent overwrite occurred.
- Offline preparation byte-verified 15 of 15 package items. After the local
  application origin was stopped, a cold reload of the prepared scoring URL
  still rendered the complete authenticated heat form, competitors, persisted
  averages, offline/reconciliation state, and Save Results command from the
  service-worker package.

The origin-stop drill proves service-worker survival of an unavailable local
server. It is not represented as a physical phone restart or an operating
system radio-disable test.

## Defects Corrected

1. The load harness could touch an implicit local database and returned success
   even when its gate failed. It now owns a guarded synthetic lifecycle and
   returns truthful status, attributes failures to endpoints, retains server
   diagnostics for failed requests, and refuses to pass any HTTP 5xx response.
2. Test setup swept every PostgreSQL database matching `proam_unit_%`, which
   could destroy another concurrent run. Cleanup is now limited to the unique
   database created by the current factory call.
3. Fresh isolated clones did not guarantee Flask's instance directory existed.
   Test factories now establish it explicitly.
4. Anonymous public requests paid for a judge-only pending-heat query. The
   sidebar query now runs only for an authenticated judge with a tournament.
5. Concurrent cold public requests duplicated standings work. Per-key fill
   locking now coalesces the build and releases correctly after builder errors.
6. PostgreSQL runtime-smoke cleanup explicitly deleted both parent and
   delete-orphan child rows, producing duplicate-delete warnings. Cleanup now
   deletes only the owning tournament.
7. Migration listener-hygiene tests could instantiate the default application
   database. They now bind a temporary SQLite database, release all handles and
   process fences, and assert the default database remains unchanged.
8. Report output could alias and overwrite a protected or retained database.
   Output paths are now checked before startup and publication, hard-link and
   symlink aliases are rejected by file identity, existing reports require
   explicit overwrite authorization, and replacement is atomic.
9. Authenticated scoring redirects, partial-body timing, aggregate-only p95,
   unseeded worker streams, and unbounded worker cleanup could produce a
   misleading or stuck load receipt. The harness now rejects every unexpected
   public or authenticated redirect, consumes complete responses, requires
   every configured user to activate and every judge to authenticate, gates
   every role and endpoint with minimum samples, records a deterministic seed,
   and stops started workers in `finally`.
10. Concurrent checkouts shared one mutable PostgreSQL schema template. Test
    templates are now namespaced by migration head, construction is serialized
    with a PostgreSQL advisory lock, and every template drop/create is checked.
11. Per-key cache fill locks accumulated for the process lifetime. Idle locks
    now retire automatically, while active readers retain the shared lock.
12. Concurrent judge page loads used ordinary optimistic ORM writes to acquire
    a heat lock, so simultaneous requests could raise `StaleDataError`. Lock
    acquisition is now an atomic available-or-expired update, repeat ownership
    checks are read-only, and losing judges render the existing blocked state.
13. The 500 handler rendered against a failed SQLAlchemy transaction. It now
    rolls back before API serialization, user lookup, or template rendering.
14. Client-thread creation occurred inside the measured ramp and could delay
    later virtual users. Every client now reaches a ready barrier before one
    shared launch epoch begins the measured ramp.
15. Redirect validation compared only paths after redirects had already been
    followed. A same-origin redirect handler now blocks scheme, host, or port
    changes before network follow, and final scheme/host/port/path must match.
16. First-time report creation could leave a partial final file after a crash
    or serialization error. New and replacement reports are now fully written
    and flushed in a sibling temporary file before atomic publication.
17. A heat deleted between route lookup and lock acquisition could fail during
    ORM refresh. The lock path now re-queries after commit and returns 404 when
    the row no longer exists.

## Uncertified Gates

This receipt is substantial local evidence, not a 2027 certification claim.
The following gates remain open:

- dated owner approval of the 2027 event matrix, Springboard exception policy,
  Pro Birling decision, Relay composition, tie splitting, timer averaging,
  event dates, and recovery objectives;
- a complete approved 2027 show-order rehearsal after those choices are
  recorded in the Domain Contract and configuration;
- physical release-phone/device restart, cross-device queue transfer, and
  radio-disabled scoring rehearsals;
- hosted exact-head GitHub Actions evidence;
- a production-mirror PostgreSQL rehearsal;
- deployment and production smoke evidence; and
- approved backup recipient/key custody, least-privilege dump access, hosted
  encryption, retained-artifact decryption/restore, retention, and audit
  evidence.

Until those gates are satisfied, the correct disposition remains
**implemented and locally rehearsed, not 2027 race-day certified**.
