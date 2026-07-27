# docs/recon

Pre-implementation reconnaissance documents. These are scratch artifacts from the planning phase of past ships — they describe the *current state* of a code area at the time of writing, before any change was made, so the implementer (or a parallel session) could understand what was already there.

Each recon doc here corresponds to work that has shipped. They are kept for historical context — useful when re-investigating an area or onboarding a new contributor — but should not be treated as live state. The shipped behavior is captured in [`docs/solutions/`](../solutions/) and the per-version changelog in [`DEVELOPMENT.md`](../../DEVELOPMENT.md).

## Index

| Recon doc | Shipped as |
|-----------|-----------|
| `BIRLING_RECON.md` | V2.14.14 — bracket compact-field rewrite (PR #95) |
| `BLOCK_LOTTERY_RECON.md` | V2.11.3 — Pro 1-Board block-count fix (PR #66) |
| `FLIGHT_FIXES_RECON.md` | V2.14.0 — 5-phase flight overhaul (PR #74) |
| `SAW_STAND_ALTERNATION_RECON.md` | V2.14.13 — Stock Saw 7/8 alternation (PR #94) |
| `SHOWPREP_WORKFLOW_RECON.md` | various Print Hub + Run Show ships |
| `VIDEO_JUDGE_BRACKET_RECON.md` | various scoring ships |
| `dual_path_recon_2026_04_27.md` | V2.14.16 — partner pairing rewrite (PR #97) |
| `registration_assignment_recon_2026_04_27.md` | V2.14.16 — partner pairing rewrite (PR #97) |

## When to add to this folder

Write a recon doc when:
- Multiple sessions or agents will work on the same area
- The area is large enough that a quick survey saves later sessions hours
- The user explicitly asks for a read-only audit before implementing

Otherwise, prefer working from conversation context. A recon doc is a planning artifact, not a deliverable.

## When NOT to keep them

If a recon doc covers an area that has been substantially refactored since it was written, the recon claims may be stale. Either update the recon doc with a `last_verified:` date or delete it — leaving stale recon docs around misleads future readers.
