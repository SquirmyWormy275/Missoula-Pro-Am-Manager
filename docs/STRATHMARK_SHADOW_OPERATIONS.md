# STRATHMARK Shadow Operations Runbook

This runbook covers the scoring-inert 2027 shadow integration between Missoula
Pro-Am Manager and STRATHMARK V2. It does not authorize production deployment,
database migration, or official mark application.

## System boundary

- Missoula owns tournament setup, entrants, scheduling, authenticated operators,
  official results, and the operational shadow lifecycle.
- STRATHMARK owns the immutable prediction receipt, model/calibration identity,
  numeric settlement revisions, and its local append-only ledger.
- The cloud mirror is asynchronous evidence delivery. It is not race-day
  authority and is not a local-ledger restore source.
- Deferred context is prospective-only and cannot affect current V2 numbers.
- Tournament setup offers shadow authority only for Underhand and Standing
  Block. Springboard may still be configured as an official handicap event,
  but it is not a supported STRATHMARK V2 shadow target.
- After a whole-field sheet reaches review, its event authority and handicap
  format are locked. Correct the field by preparing a superseding shadow run;
  do not switch the reviewed event back to the official mark path.

## Supported topology

Trusted writes require one durable STRATHMARK writer: either a single service
process with a persistent volume or an explicitly offline single-writer laptop.
An ephemeral or multi-writer SQLite deployment is not a supported trusted
topology. Health output is an observed local state plus operator topology
attestation, not proof that the underlying volume survives infrastructure loss.

## Pre-event preparation

1. Apply Missoula migrations to an isolated rehearsal database first.
   The c8 forward repair preserves each legacy outbox scorer as its frozen
   delivery actor and converts every legacy receipt request identity to the
   deterministic STRATHMARK UUID. Its new delivery column is intentionally
   nullable during the expand phase so a b7 code rollback can still write;
   current delivery code rejects a missing frozen actor rather than guessing.
2. Apply STRATHMARK migrations `005`, `006`, then `007` separately, using the
   disposable PostgreSQL rehearsal before any production authorization.
3. Refresh and verify the offline evidence snapshot for the chosen exclusive
   UTC cutoff. Missing, stale, or corrupt evidence blocks new calculation.
4. Configure distinct service bearer and actor-attestation secrets.
5. Confirm every entrant has one reviewed namespaced STRATHMARK identity.
6. Confirm wood species/diameter and single-elapsed target support.
7. Run the full dress rehearsal and save the checksummed issue/context exports.

## Operator rehearsal

Perform this sequence against isolated SQLite state:

1. prepare exact field;
2. approve preflight;
3. calculate, then restart and recover the same receipt;
4. review all recommendations, including zero;
5. issue and verify the non-importable whole-field export;
6. record one valid finish and one nonfinish;
7. deliver settlement after a simulated retry;
8. change the finish to DQ and deliver the void;
9. record known and unknown deferred context;
10. export every factor with provenance;
11. prove official mark fields are byte-for-byte unchanged.

The executable regression is `tests/test_shadow_release_readiness.py`.
The installed-artifact gate is
`python scripts/verify_strathmark_shadow_contract.py`. It migrates a disposable
Missoula SQLite database to the current Alembic head, creates a synthetic
event through real Missoula models, and sends the exact calculate and numeric
outcome payloads produced by Missoula's request builders to the pinned
in-process STRATHMARK service. It also proves PEP 610 commit provenance,
restart replay, environment restoration, an empty working directory, no
network transport, and no production data use.

## Monitoring

Treat these dimensions independently:

- operational lifecycle;
- local receipt trust and freshness;
- mirror state and pending count;
- outcome completeness and numeric revision state;
- offline evidence snapshot integrity and age.

Mirror failure does not move the operational lifecycle backward. An outcome
correction appends a new revision. A refreshed evidence snapshot may make live
status stale without changing the previously issued receipt bytes.

## Failure and recovery

### Ambiguous calculation timeout

Look up `(consumer_id, request_id, run_revision)` first. If a receipt exists,
hydrate it exactly. Calculate only after an authoritative not-found response.

### Local write failure

The draft is untrusted and cannot be issued. Do not label it as a trusted sheet
and do not reconstruct it from the cloud mirror.

### Mirror outage

Retain the durable local outbox. Retry oldest-first with bounded work after the
service returns. Scoring, reconciliation, and other web requests only commit
the local intent; they never drain the remote outbox inline. Use the supervised
`python scripts/deliver_shadow_settlements.py --limit 25` command for bounded
delivery. Never discard or edit the original payload to make it deliver. The
command reports the remaining eligible backlog. Exit `0` means that eligible
work is complete, exit `1` means an attempt failed and entered backoff, and
exit `2` means the batch limit was reached while eligible work remains.

Each delivery intent freezes one active admin or judge identity when the
outbox row is created. The scorer remains the local operational author, but
delivery never silently changes to another operator. If that frozen identity
is later disabled or loses its admin/judge role, delivery fails closed and the
row remains retryable. Restore that exact account only after an authorized
review; if the principal must be replaced, use an audited data-repair release
rather than editing the payload or switching operators at runtime.

Official scoring still commits if no active admin or judge exists when a new
intent is captured. The outbox freezes the scoring actor as an explicitly
blocked principal so the exact intent remains auditable and repairable. The
worker rejects that row before attestation or transport; it never searches for
a replacement principal during retry.

Within one shadow run, delivery is revision-ordered. A newer batch cannot pass
an earlier unrecorded batch, including one held by another worker or waiting in
backoff. This preserves STRATHMARK's expected numeric revision sequence.

PostgreSQL workers claim one row at a time with `FOR UPDATE SKIP LOCKED` and
hold the claim through the bounded remote call. SQLite operation remains
single-writer: run only one supervised delivery command at a time. There is no
built-in scheduler yet, and deterministic failures continue to retry with a
bounded backoff until an operator corrects the cause. Those are deliberate
operational limits, not evidence that settlement delivery is automatic.
Remote validation and authorization failures can therefore remain retryable
forever; local payload-integrity failures halt the command instead of sending
altered evidence. The shadow migration stores database-generated timestamps as
naive UTC (including an explicit UTC expression on PostgreSQL).

### Bad result evidence

Use append-only reconciliation with the current outcome state token and a
bounded reason code. A finish-to-DQ/scratch/nonfinish correction produces a
numeric void when a prior numeric revision is active.

## Release gates

- STRATHMARK contract commit is published and deliberately pinned.
- Missoula fresh SQLite migration chain passes.
- Missoula disposable PostgreSQL migration suite passes.
- STRATHMARK disposable PostgreSQL mirror rehearsal passes.
- installed STRATHMARK artifact exposes the frozen seven-route contract.
- installed-artifact traffic comes from real Missoula builders on a migrated
  disposable Missoula database, not OpenAPI example payloads.
- focused and full Missoula suites pass without production dependencies.
- browser/operator rehearsal passes at keyboard and narrow viewport.
- release and rollback checklists name the shadow workflow.

Until all gates pass, keep shadow authority disabled in production.
