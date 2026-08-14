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

## Supported topology

Trusted writes require one durable STRATHMARK writer: either a single service
process with a persistent volume or an explicitly offline single-writer laptop.
An ephemeral or multi-writer SQLite deployment is not a supported trusted
topology. Health output is an observed local state plus operator topology
attestation, not proof that the underlying volume survives infrastructure loss.

## Pre-event preparation

1. Apply Missoula migrations to an isolated rehearsal database first.
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
service returns. Never discard or edit the original payload to make it deliver.

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
- focused and full Missoula suites pass without production dependencies.
- browser/operator rehearsal passes at keyboard and narrow viewport.
- release and rollback checklists name the shadow workflow.

Until all gates pass, keep shadow authority disabled in production.
