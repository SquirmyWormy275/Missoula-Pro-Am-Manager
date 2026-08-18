# Handicap Marks and STRATHMARK Shadow Recommendations

**Audience:** Show director, head judge, scoring lead, and release operator

**Authority:** `Event.handicap_authority_mode` is the deciding switch

**Last reviewed:** 2026-08-14

Missoula now has two deliberately separate handicap workflows. They must never
be blended.

## Official legacy path

Events whose authority mode is not `shadow` continue to use the existing
**Assign Marks** page. That workflow may write `EventResult.handicap_factor`,
`predicted_time`, and `mark_assigned_at`. Existing manual entry and CSV import
remain available for those events.

The legacy path is retained for backward compatibility. Its old model-selection
description is not a statement about STRATHMARK V2 and is not the procedure for
a shadow event.

## STRATHMARK V2 shadow path

A shadow event produces a **shadow recommendation only**. It never writes the
official mark fields, never changes championship placement, and never becomes
official merely because a judge reviewed or issued it.

Open **Scheduling → Event → Shadow marks** and complete the whole-field workflow:

1. **Prepare.** Select one deliberate **exclusive UTC** prediction cutoff. The
   app freezes the ordered entrants, stable external identities, wood, event,
   target, schedule, and context fingerprint into one immutable request.
2. **Approve preflight.** Confirm the exact field revision. A changed entrant,
   wood specification, run order, cutoff, or identity requires a new linked run.
3. **Calculate or recover.** Missoula performs **receipt lookup** before any
   calculation. After a timeout or restart it looks up the same request rather
   than blindly calculating again.
4. **Review.** A judge or admin explicitly accepts every recommendation,
   including a zero-second recommendation. Individual rows cannot be
   cherry-picked or edited into a different V2 sheet.
5. **Issue.** Freeze the **whole-field** recommendation and download its
   checksummed, pseudonymous, non-importable export.
6. **Record results.** Normal scoring remains official. Missoula separately
   appends operational outcomes and sends only eligible positive raw elapsed
   time as **numeric settlement** evidence. DNF, DQ, scratch, and timing failure
   remain context-only unless a previous numeric finish must be voided.
7. **Correct evidence.** A finish-to-DQ correction appends a void; a corrected
   time appends a new numeric revision. History is not overwritten.
8. **Export prospective context.** Deferred factors are structured,
   pseudonymous, explicit-known or explicit-unknown, and numerically inactive.

## Blocking versus advisory status

The local STRATHMARK receipt is race-day authority for the shadow sheet.

Blocking:

- missing or unresolved stable identity;
- unsupported multi-run target;
- missing wood, invalid diameter, or changed frozen input;
- no trusted receipt, stale receipt, or incomplete whole-field review;
- local ledger write failure or invalid integrity evidence.

Advisory:

- cloud mirror pending or retryable-failed after the local receipt is durable;
- model drift/calibration monitoring that does not invalidate the saved receipt.

Cloud mirroring is not a restore source and is not required to review or issue a
durable local receipt.

## Configuration

The V2 client uses a dedicated local/service boundary, not the legacy Supabase
variables:

| Variable | Purpose |
|---|---|
| `STRATHMARK_SHADOW_URL` | Absolute URL of the trusted STRATHMARK service |
| `STRATHMARK_SHADOW_CONSUMER_ID` | Namespaced consumer identity; default `missoula:service:shadow` |
| `STRATHMARK_SHADOW_SERVICE_TOKEN` | Service bearer credential |
| `STRATHMARK_SHADOW_ATTESTATION_KEY` | Separate HMAC key for actor/request attestations |

The two secrets must be distinct. Missing or partial configuration is a visible
not-configured/invalid state; it must not fall back to another numeric engine.

## Race-day recovery

| Symptom | Safe action |
|---|---|
| Calculate timed out | Use the same run; receipt lookup resolves an ambiguous commit |
| App restarted after calculation | Open the saved run; the immutable local receipt is replayed |
| Roster, wood, cutoff, or schedule changed | Prepare a new run that supersedes the old run |
| Mirror is retryable-failed | Continue locally; replay the durable outbox later |
| Receipt is stale or fails integrity | Do not issue; investigate and supersede if needed |
| Incorrect valid finish was recorded | Reconcile with reason and append a void/correction |
| STRATHMARK unavailable before any receipt exists | Do not invent a shadow sheet; use the separately authorized official workflow if the show director chooses it |

Numeric outcomes are committed to Missoula's durable outbox in the same local
transaction as the operational result. Scoring and judge requests never wait
for remote delivery. Run `python scripts/deliver_shadow_settlements.py --limit 25`
as the separately supervised delivery command. Failed rows use a bounded
exponential cooldown; exact STRATHMARK duplicate responses close the local row
as recorded.

## Non-negotiable authority rule

The shadow recommendation export is for evaluation and operational learning.
It is deliberately non-importable. Applying shadow recommendations to official
marks is a future product decision and requires a separately reviewed authority
change; this workflow does not provide that capability.

See [STRATHMARK Shadow Operations](STRATHMARK_SHADOW_OPERATIONS.md) for the
deployment, recovery, and rehearsal runbook.
