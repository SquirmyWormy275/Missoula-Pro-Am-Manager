---
title: "Preflight gear-sharing false positives from USING prefix drift"
date: 2026-04-21
category: data-integrity
module: missoula-pro-am-manager
problem_type: logic_error
component: service_object
symptoms:
  - "Preflight reported 30 unresolved gear-sharing partner names (all false positives)"
  - "Preflight reported 32 gear-vs-partner mismatches (23 false positives, 9 SHARING-concept false positives, 4 real drift)"
  - "Preflight reported 33 stale gear entries for unenrolled events with no cleanup UI"
  - "Judges unable to trust preflight signal on live tournament four days before race day"
root_cause: logic_error
resolution_type: code_fix
severity: high
related_components:
  - service_object
  - database
  - frontend_stimulus
tags:
  - preflight
  - gear-sharing
  - using-vs-sharing
  - json-field-drift
  - v2.9.1-followup
  - false-positive
  - consumer-sweep
---

# Preflight gear-sharing false positives from USING prefix drift

## Problem

V2.9.1 (commit `9a4f1fb`, 2026-04-19) introduced a `using:` value prefix on the `gear_sharing` JSON dict to distinguish *partnered-event confirmation* from *cross-competitor gear dependency*, but only taught the heat-building services about the new format. The preflight scanner kept reading raw values through `normalize_person_name`, so every USING entry surfaced as a false-positive "unknown partner" warning on the judge-facing preflight page. A separate conceptual bug in the mismatch check was also flagging correct SHARING data as a disagreement, and a third warning about stale gear-for-unenrolled-events had no cleanup UI at all.

On live tournament 2, four days before race day, the preflight page showed 6 high-severity + 4 medium-severity issues, most of which were noise. Judges could not tell real problems from artifacts.

## Symptoms

The preflight page at `/registration/<tid>/preflight-check` on `tournament_id=2` reported three warnings that the V2.9.1 changelog claimed were solved:

- **`gear_unknown_partner_names`: 30 entries** listing Kate Page, Brianna Kvinge, Chrissy Marcellus, Emma Macon, Grace Shelton, + 17 more. Direct SQL against `instance/proam.db` showed all 30 were USING entries whose underlying names DO resolve against the roster — the `using:` prefix was short-circuiting the normalization lookup.
- **`gear_partner_mismatch`: 32 entries.** SQL breakdown: 23 using-prefix false positives (same root cause as Warning 1, but via string comparison) + 9 SHARING-conceptual false positives on Double Buck / Jack & Jill pairs where a saw-sharer legitimately differed from the event partner (e.g., Ripley Orr partnered with Cody Labahn, shares saw with May Brown).
- **`gear_non_enrolled_event`: 33 entries.** Stale `gear_sharing` keys pointing at events the competitor no longer entered (e.g., Chrissy Marcellus had 10 entries for events she wasn't in). Genuine data hygiene, but no cleanup route existed.

## What Didn't Work

- **Hypothesis: competitors had missing roster data.** First instinct was that Warning 1 was catching genuine typos or unregistered partner names. A standalone SQL probe against `instance/proam.db` counted genuine unknowns vs. prefix artifacts — 0 genuine, 30 prefix. Killed the hypothesis before touching code.
- **False-alarm duplicate endpoint.** Misread a grep hit and initially claimed `move_competitor_between_heats` was registered in both `routes/scheduling/flights.py` and `routes/scheduling/heats.py`, blocking the test run. On re-inspection, `flights.py:346` was a different function named `drag_move_competitor`. The pytest collision was transient, likely a race with the `SessionStart: Cleared __pycache__` hook during test discovery; all 26 preflight tests passed on the next run. Correcting this required re-reading the grep output column-by-column rather than trusting the initial skim.
- **V2.9.1's consumer sweep missed `services/preflight.py` entirely** (session history). The 828-line `docs/GEAR_SHARING_AUDIT.md` written during V2.9.1 contains zero mentions of preflight. A subagent saw the file once in a directory listing but never opened it. The explicit consumer list used by the V2.9.1 implementation was `competitors_share_gear_for_event`, `build_gear_conflict_pairs`, `_aggregate_gear_groups`, plus the manager-page display template — four hot-path consumers, no display-facing scanners.
- **A later Codex audit also missed it** (session history). The 2026-04-19 Codex audit touched `routes/scheduling/preflight.py` (the HTTP route layer) and refactored background-job app-context, but never opened `services/preflight.py` (the report builder). Same failure-mode trap in a different file: two modules share a name but do entirely different things, and a dedicated second-opinion audit landed on the route-layer one without cross-checking the service-layer one.

## Solution

### 1) `services/preflight.py` — import the USING helpers

```python
# Before
from services.gear_sharing import event_matches_gear_key, normalize_person_name

# After
from services.gear_sharing import (
    event_matches_gear_key,
    is_using_value,
    normalize_person_name,
    strip_using_prefix,
)
```

### 2) Warning 1 (`gear_unknown_partner_names`) — strip the prefix before normalization

```python
# Before
partner_text = str(partner or '').strip()
partner_norm = normalize_person_name(partner_text)
if not partner_text:
    unknown_partner_rows += 1
    ...

# After
partner_text = str(partner or '').strip()
# USING entries carry a "using:" prefix to flag partnered-event
# confirmation (see services/gear_sharing._USING_VALUE_PREFIX).
# The underlying name must still resolve to a real competitor,
# but the prefix itself is not part of the person's name.
partner_name_only = strip_using_prefix(partner_text)
partner_norm = normalize_person_name(partner_name_only)
if not partner_name_only:
    unknown_partner_rows += 1
    ...
```

### 3) Warning 3 (`gear_partner_mismatch`) — only compare USING entries, strip prefix first

```python
# Before
for key, gear_partner in gear.items():
    gp = normalize_person_name(str(gear_partner or '').strip())
    pp = normalize_person_name(str(partners.get(key, '') or '').strip())
    if gp and pp and gp != pp:
        partner_mismatch_rows += 1

# After
# Only USING entries claim to confirm the event partner — a mismatch there
# is a genuine data bug (stale confirmation vs. new partner assignment).
# SHARING entries (no "using:" prefix) are defined as cross-competitor gear
# dependency OUTSIDE the event partnership, so gear_partner != event_partner
# is the expected, correct shape — flagging it produced noise on every
# Double Buck / Jack & Jill pair with a saw-sharer.
for key, gear_partner in gear.items():
    gear_text = str(gear_partner or '').strip()
    if not is_using_value(gear_text):
        continue
    gp = normalize_person_name(strip_using_prefix(gear_text))
    pp = normalize_person_name(str(partners.get(key, '') or '').strip())
    if gp and pp and gp != pp:
        partner_mismatch_rows += 1
```

### 4) `services/gear_sharing.py` — new `cleanup_non_enrolled_gear_entries()` service

Walks every active pro + college competitor's `gear_sharing` dict and removes keys the competitor isn't enrolled in. Handles direct event-id keys (`"82"`) and category keys (`"category:crosscut"`) — category keys are kept when the competitor is enrolled in any event of that category (matching the resolution logic in `event_matches_gear_key`). Returns `{cleaned, affected: [names], pro_cleaned, college_cleaned}`; caller commits.

### 5) `routes/registration.py` — new POST endpoint

`POST /pro/gear-sharing/cleanup-non-enrolled` wired to `cleanup_non_enrolled_gear_entries()`, invalidates the competitor cache via `invalidate_tournament_caches`, audit-logs the row counts and affected names via `log_action('gear_cleanup_non_enrolled', ...)`, flashes the result.

### 6) `templates/pro/gear_sharing.html` — "Cleanup Non-Enrolled" button

Renders next to the existing "Cleanup Scratched" action with the standard `data-confirm` modal attribute (no inline `onsubmit` — CSP-compliant, matches the V2.11.0 FNF one-click-generate pattern).

### 7) `tests/test_preflight.py` — 6 regression tests

| Test | Asserts |
|---|---|
| `test_using_prefix_resolves_to_known_partner` | USING entry whose stripped name matches roster → no warning |
| `test_sharing_entry_different_from_partner_is_not_flagged` | Ripley/Cody/May case: SHARING != event partner → no warning |
| `test_using_entry_still_flagged_when_name_unknown` | `using:Ghost Competitor` → still flags `gear_unknown_partner_names` |
| `test_using_mismatch_with_partners_still_flagged` | USING name drifted from `partners[key]` → still flags `gear_partner_mismatch` |
| `test_removes_entries_for_non_enrolled_events` | Cleanup removes stale direct-id keys, keeps enrolled ones |
| `test_keeps_category_entries_when_enrolled_in_matching_event` | `category:crosscut` preserved when competitor is in any crosscut event |

### Before/after counts on live tournament 2

| Warning | Before | After code fix | After clicking "Cleanup Non-Enrolled" |
|---|---|---|---|
| `gear_unknown_partner_names` | 30 | **0** | 0 |
| `gear_partner_mismatch` | 32 | **4** (real USING drift) | 4 |
| `gear_non_enrolled_event` | 33 | 33 | **0** |

The 4 remaining `gear_partner_mismatch` entries are genuine data bugs the warning is supposed to catch — e.g., Iliana Castro's gear says `using:Karson Wilson` for Jack & Jill but her `partners` dict says `Jack Love`. Those stay flagged for human review in the Gear Sharing Manager.

Test suite: 172/172 pass (146 gear-sharing unit tests + 26 preflight tests including the 6 new regressions).

## Why This Works

The `gear_sharing` JSON field carries two semantically different domain meanings that V2.9.1 distinguished with a value prefix:

- **USING** (`"using:<name>"`) — *partnered-event confirmation.* "I confirm `<name>` is my registered partner for this event." Redundant with the `partners` dict; NOT a heat constraint. A mismatch between `strip_using_prefix(gear[key])` and `partners[key]` is a genuine data bug — the confirmation was entered against a now-stale partner assignment.
- **SHARING** (`"<name>"`, no prefix) — *cross-competitor gear dependency.* "I share my cookie-stack saw with `<name>`, who competes separately from me." IS a heat constraint — the two competitors cannot be in the same heat. By definition, `gear[key] != partners[key]` because the whole point is a different person from the event partner.

V2.9.1 correctly taught `competitors_share_gear_for_event` and `build_gear_conflict_pairs` to skip USING entries when building heat constraints — so the scheduling side was right. But `services/preflight.py` is a *display-facing scanner*, not a heat builder, and it still:

1. Ran `normalize_person_name("using:Eric hoberg")` → `"usingerichoberg"` → never a roster hit → false Warning 1.
2. Compared every gear key against `partners` regardless of prefix → SHARING entries are defined to differ → false Warning 3.

Stripping the prefix before normalization makes Warning 1 ask the right question ("is the underlying name in the roster?"). Restricting the mismatch check to USING-only makes Warning 3 ask the right question ("is this confirmation stale relative to the current partner assignment?"). SHARING entries are now silent in both warnings — correct, because they're expressed through heat-building constraints, not preflight display.

The cleanup service closes the orthogonal Warning 2 loop: stale keys accumulate as competitors drop events, and there was no UI to purge them. Category-key awareness prevents clobbering `"category:crosscut"` entries when a competitor is still enrolled in any crosscut event.

## Prevention

**Concrete rules for the next value-format change on a shared JSON field:**

1. **Grep every consumer before declaring rollout complete.** When adding `_USING_VALUE_PREFIX` to `services/gear_sharing.py`, V2.9.1 updated the hot-path call sites and shipped. The mandatory follow-up is a repo-wide grep for the field name across `services/` AND `routes/` AND `templates/`, with an explicit audit note per hit: "does this consumer need to know about the new prefix?" Preflight would have been caught in 30 seconds. The useful grep for `gear_sharing` readers is `rg -l "get_gear_sharing|\.gear_sharing\b"` (session history).

2. **Name the "fourth consumer" category** (session history). V2.9.1 handled three hot-path consumers plus the display template. Any file that reads `gear_sharing` dict values without going through `strip_using_prefix` / `is_using_value` is a latent false-positive source. The category worth naming in CLAUDE.md: *display-facing scanners and report builders* — the quiet consumers that don't affect scheduling but shape what judges see.

3. **Prefer read-through helpers over raw dict access.** Any consumer that touches `gear_sharing[key]` should call a centralized accessor — e.g., `resolve_gear_partner(gear, key) -> (kind, name)` returning `('using', name)`, `('sharing', name)`, or `(None, '')`. The prefix becomes an implementation detail of the helper, not of every caller. This would have made the preflight fix a one-line call-site swap instead of two separate stanzas with copy-pasted prefix-stripping logic.

4. **Add a lint/grep guard.** A cheap CI check: `rg "normalize_person_name" services/ routes/` and require every hit to also reference either `strip_using_prefix` or `is_using_value` (or be explicitly allowlisted). Any bare `normalize_person_name(gear[key])` is now a suspected prefix-unaware read.

5. **Test the display path, not just the scheduling path.** V2.9.1 added regression tests for heat building (correct) but not for preflight (missed). When a data format changes, every *interpreter* of that format — UI warnings, reports, exports, audit logs — needs a test exercising the new shape. The 6 tests in `tests/test_preflight.py` are the template: USING-match, SHARING-mismatch, USING-unknown, USING-stale, plus the cleanup cases.

6. **Two files can share a name and do entirely different things** (session history). `routes/scheduling/preflight.py` (HTTP route + async worker) and `services/preflight.py` (report builder) are a trap. The Codex audit walked the route-layer one and missed the service-layer one. Mitigation: CLAUDE.md should call out this kind of name-twin explicitly in the architecture section, and auditors should always cross-check for `services/<name>.py` when they find `routes/.../<name>.py` (and vice versa).

7. **When a changelog claims "fixed X", verify X on live data before closing the loop.** The V2.9.1 entry said gear-sharing was fixed; the preflight page on tournament 2 said otherwise. A 5-line SQL probe against `instance/proam.db` distinguished "fix didn't ship" from "fix is incomplete" in under a minute. This should be the default first step when a user says "this should have been solved."

## Related Issues

- **PR #25** (merged 2026-04-19, commit `9a4f1fb`) — V2.9.1 race-day UI hardening. Introduced the `using:` prefix. Direct ancestor of this fix.
- **PR #9** (merged 2026-04-08) — earlier gear-sharing hardening pass (G1-G8); context only.
- **`docs/GEAR_SHARING_AUDIT.md`** — V2.9.1 audit artifact (828 lines, 25 numbered gaps). Finding #1 ("USING vs SHARING semantically conflated") is now fully resolved across V2.9.1 + this fix. Audit did not enumerate `services/preflight.py` as a consumer — the audit itself had the blind spot that produced this bug.
- [`docs/solutions/data-integrity/events-entered-stores-names-not-ids.md`](events-entered-stores-names-not-ids.md) — sibling: same failure shape (one producer, multiple consumers, silent mis-resolution across services) on a different JSON field.
- [`docs/solutions/architecture-decisions/json-fields-over-join-tables.md`](../architecture-decisions/json-fields-over-join-tables.md) — the tradeoff doc that warns about JSON-field consumer drift. This bug is a concrete instance of that warning.
- [`docs/solutions/data-integrity/json-decode-errors-from-corrupt-fields.md`](json-decode-errors-from-corrupt-fields.md) — related only in that both touch `gear_sharing` integrity; different failure mode.

## Auto memory cross-references

(auto memory [claude]) MEMORY.md `V2.9.1 (2026-04-19)` patch note listed USING/SHARING fix as a "BLOCKER fix" because partnered pairs were being split across heats; the note did not mention preflight, consistent with V2.9.1's scope being the heat builders only.

(auto memory [claude]) MEMORY.md "Never stash/restore/revert without approval" applied correctly in this session — uncommitted files from other in-progress work (flights.py additions, birling.py, ability_rankings.html) were observed and left untouched.
