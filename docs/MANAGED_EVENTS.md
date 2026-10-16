# Delegated handicap rehearsal

The [ecosystem agreement](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/MODULAR_ECOSYSTEM.md)
is the current cross-repository ownership map. Existing host-owned official events
and scoring-inert shadow events retain their contracts. New synthetic individual
SB/UH handicap events may deliberately link to STRATHMARK managed v1. STRATHMARK
then owns fields, review, issue, outcomes, correction and progression. Missoula
owns registration, whole-show scheduling/resources, accepted result projection,
points, payouts and outward career publication. No saved event migrates implicitly.

## Separate runtime and operator controls

Keep Missoula's Python 3.10 and existing STRATHMARK 2.x requirements unchanged.
Create a separate environment from `requirements-managed-runtime.txt`. Configure
absolute `PROAM_MANAGED_EVENTS_PYTHON` and `PROAM_MANAGED_EVENTS_DATABASE` paths,
then opt in with `PROAM_MANAGED_EVENTS_ENABLED=1` in an isolated rehearsal.
`ProductionConfig.MANAGED_EVENTS_ENABLED` is hard false even when that variable
is set. Merge, installation and qualification do not activate this profile.

Judges/admins open **Managed handicap events** in the management sidebar. Link
only a fresh event with its complete active-entry mapping. The JSON configuration
uses stable host references (`pro:id` or `college:id`), source event/tournament
identities shown on the page, actual configured wood, physical stand capacity,
gear groups, an explicit selection reason and authorized prior history. The
example 275 mm configuration never overrides this show's WoodConfig.
Unsupported families, partial/mismatched rosters, absent wood and conflicting
gear/stand assignments fail explicitly. Future history is refused.
Existing host-generated heats also prevent binding; do not mix two field layouts.
Tournament setup cannot alter delegated discipline/capacity/handicap configuration.
Preparation rechecks the current active roster, divisions, gear declarations and
wood. Changed host facts block new preparation until reconciled with a reviewed
source configuration. Physical stand identifiers must be canonical distinct
integers; Missoula retains its dual-timer rule. Already accepted requests still
return their original receipts. Recovery of an ambiguous command first checks
verified source audit, so a later host edit cannot hide a committed outcome.
An unissued roster can be reconciled through the shared `configure` operation;
the host validates its entries and updates their mapping with the new source
revision. The generic workspace serves several show profiles, so Missoula refuses
to import a source configuration selecting a different discipline or timer rule.
Export refreshes authoritative state and requires the same source revision; a
concurrent change asks the operator to refresh and retry.
New acceptance also reloads registrations under the tournament parent lock and
checks the active entries, divisions, gear, wood and physical resources against
the source configuration. An unchanged source roster cannot conceal a host-side
entry addition/removal. Exact retries of an already accepted export retain their
original receipt after later registration edits.

Acceptance and award application retain a host-owned SQLite writer fence through
the outer host transaction. The private adapter first asks exact rc11 to export
and verify the snapshot, then hands off to a standard SQLite mutex in the host
process. It compares opaque state-byte digests again under that mutex, refusing
an intervening source change. It adds no calculation/progression logic and never
imports the component into Python 3.10. Helper exit cannot release the host's
lock during DBAPI commit. Commit, rollback and session close release it; savepoint
completion keeps the outer fence. Bulk payouts acquire one fence for all selected
managed events before host scheduling/row locks. The source schema/adapter is
bound to rc11 and must be reverified before selecting a different runtime.

Initial binding shares SQLite's schedule-writer guard and PostgreSQL's tournament
parent lock with heat generation. It reloads the event/link and checks freshness
under that lock before committing delegation. Authority-presence reads also avoid
a relationship's stale cached absence after another request delegates the event.

A pending creation keeps legacy writes fenced while its outcome is uncertain.
Recover its exact request first. If its supplied configuration was refused, the
same workspace accepts corrected configuration under a new request, retaining
the original intent. Source creation is insert-only: an ambiguous earlier success
cannot be replaced. Its creating actor/request and exact configuration determine
the accepted host mapping. Once confirmed, other initial requests become retained
conflicts rather than permanent pending work. Never delete a pending link to guess
that the remote creation failed.

The host's advanced command panel accepts the versioned operations documented in
[STRATHMARK's operator guide](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/MANAGED_EVENTS.md).
Use a stable request ID, expected revision and `editor: missoula`. Pending local
intents survive an unavailable component; **Recover exact request** resubmits
that same identity and actor. Refresh reads authoritative state. A stale retry
cannot replace a newer accepted projection. No runtime or engine fallback exists.

For native field/timer forms, configure `PROAM_MANAGED_WORKSPACE_ORIGIN` to an
explicit HTTPS or loopback origin and launch the optional shared workspace
against **the same authority database**. The deep link carries no credential.
Hand control from Missoula to `gui` deliberately. CLI and GUI resume the same
issued records. Hand control back from the owning interface, then refresh.
The separate scoped workspace login uses normal CSRF. It is not a public
service relying on actor headers. A development server is not a hosted deployment.

## Accepted projections, awards and career records

The module appends next-round readiness; Missoula places those heats into its
existing program. It does not reorder the whole show. Host legacy mark, outcome
and placing mutations refuse linked events. A physical roster/wood/stand change
cannot reuse an old unissued preparation. Editor handover does not change fields.

**Accept current module result revision** stores every accepted heat attempt,
immutable source revision, exact acceptance receipt and durable career delivery
intent in one host transaction. EventResult is a separate latest-round summary;
times across rebased heats are not compared to invent placings. Acceptance never
settles points/payouts. **Apply host points and payouts** explicitly uses accepted
module positions, preserves tie behavior, and clears changed payout settlements.
Corrections append history and require a fresh acceptance/award decision.
Accepting a source revision that is no longer finalized reopens the host event
and clears the accepted-summary eligibility for awards. Earlier award/settlement
facts remain for explicit reconciliation. After source review and re-finalization,
accept the new summary and apply host awards deliberately.
Accepting an editor/metadata revision with the same summary keeps existing host
award finalization. Competitor-wide scratch cascades refuse linked managed
entries, preserving their source positions and history; use the managed outcome
or roster workflow and event-scoped operations for unrelated events.
Registration's gear heat synchronization leaves managed fields unchanged,
including pending and issued fields, and identifies the skipped events. It can
still repair conflicts in independent host-owned heats in the same show.
College competitor/team deletion also protects every mapped managed entrant,
including a pending creation or unissued event. Reconcile a removable entry in
the managed configuration first; deleting registration cannot remove its authority.
College awards rebuild both individual and affected team totals, including after
corrections. Pro ties retain Missoula's existing shared-position payout: each tied
entrant receives the configured purse for that shared place. Direct entry and
saved payout templates use those accepted positions without recomputing placing.

**Deliver next five pending career revisions** is a supervised bounded batch.
Configure `MNEMEX_SHOW_RESULTS_URL` as an HTTPS or loopback
`/partners/results/ingest` endpoint and a server-only scoped
`MNEMEX_SHOW_RESULTS_TOKEN`. No call occurs during scoring/finalization.
Downtime or an invalid receipt leaves the original intent pending; exact retries
recover the receiver receipt. Conflicts stay visible. Acknowledged **unlinked**
results preserve source history without silently resolving a person or granting
public career/export eligibility. The shadow settlement outbox is unrelated.
MNEMEX is optional on race day.

## Migration, rollback and recovery

A scheduled unissued field cannot be silently deleted or physically replaced.
In the managed workspace, confirm **Unschedule unissued field** while its flight
is still pending, then recover the retained source request and schedule the new
field deliberately. The confirmation binds the current placement digest. Issued
fields and started/completed flight history cannot be released this way.

Additive chain: `a6b7c8d9e0f1 -> f22576d0ea7d -> 66a549582a97 ->
a6d6dda4e480`. Existing events remain host-owned unless a link is created. New
managed tables retain receipts, projections, attempts and deliveries. The earlier
nullable delivery-actor expansion stays nullable for the documented rollback
window; runtime still requires its frozen authenticated principal.

Rollback code with managed functionality disabled first. Old code ignores the
new tables; preserve them and the private authority DB/head pair. Do not
schema-downgrade an installation containing accepted managed records without an
independent verified backup and explicit data migration. Full drop/downgrade
checks use disposable synthetic databases only. The backup expected-head pin
must track this chain and carries the release checklist's hosted proof gate.

Retain an independently accessible issued sheet before every heat. Issued heats
may record manual outcomes against exact issue/field identities during an outage;
unissued fields pause. Reconcile into the original authority after recovery.
No browser cache or copied old DB grants permission for a parallel authority.
STRATHMARK's fenced local transfer is explicit; lost-machine recovery requires
independent current-head and external fencing proof. See [recovery drills](RECOVERY_DRILLS.md)
and the [execution record](MODULAR_EXECUTION_STATUS.md) for actual evidence.
