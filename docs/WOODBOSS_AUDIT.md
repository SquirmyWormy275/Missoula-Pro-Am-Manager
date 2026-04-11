# Virtual Woodboss — Audit Report

**Date:** 2026-04-10
**Version at audit:** V2.8.1 → V2.8.3 (all HIGH + MEDIUM + LOW resolved except M7/M8)
**Scope:** [models/wood_config.py](../models/wood_config.py), [routes/woodboss.py](../routes/woodboss.py), [services/woodboss.py](../services/woodboss.py), [templates/woodboss/](../templates/woodboss/), [tests/test_woodboss.py](../tests/test_woodboss.py), [models/competitor.py](../models/competitor.py)

Severity legend: **HIGH** = data-loss or functional bug users hit today · **MEDIUM** = latent bug, broken edge case, or missing UI for an existing route · **LOW** = polish / robustness

---

## HIGH

### H0. [RESOLVED V2.8.2] College blocks were entirely missing from the Wood Count Report
**Files:** [services/woodboss.py:256-285](../services/woodboss.py#L256) `_count_competitors`, :327-352 `_list_competitors`, [models/competitor.py:143-162](../models/competitor.py#L143) `closed_event_count`

**Symptom:** The Block Turning report's "By Species" view showed only pro + relay blocks — zero college blocks — even though college saw logs rendered normally. Hit the day before block-turning day.

**Root cause:** `events_entered` stores event **names** as strings on both `CollegeCompetitor` and `ProCompetitor` (what college registration and the Excel importer actually write, e.g. `"Underhand Hard Hit"`). `_count_competitors` and `_list_competitors` built only an ID-keyed lookup (`{str(e.id): e}`) for college events and silently skipped every non-matching entry via `if not event: continue`. Counts stayed at 0 for every college block key. `_group_by_species` then filtered the zero-count rows out of the by-species view. Saw logs still appeared because `calculate_saw_wood` emits zero-count rows unconditionally and the logs view doesn't filter them.

The pro path was unaffected because `_get_pro_event_map` already built a name fallback (`{name.lower(): e, display_name.lower(): e}`) for Excel-imported entries.

The same ID-vs-name mismatch also silently broke `CollegeCompetitor.closed_event_count` — it built a set of event IDs and then did `eid in closed_ids` against names, always returning 0, so the 6-CLOSED-events-per-athlete enforcement wasn't running at all.

**Fix:** Both woodboss helpers now build `college_id_map` + `college_name_map` (by `.name` and `.display_name`) and try ID → name → skip, mirroring the pro pattern. `closed_event_count` switched from ID-set to name-set intersection.

**Verified:** against the live `(WOOD TEST) 2026` tournament — 32 college count keys now populated, 4 new college block groups in the by-species view (Western White Pine 12", Hybrid Poplar 10", Hybrid Poplar 9", plus the Men's Underhand+Standing Block combined row).

**Lesson:** the audit below focused on preset code paths and missed this because the audit-doc's description in CLAUDE.md / MEMORY.md said `events_entered` stored IDs — which was wrong for college. The documentation drift masked the bug. CLAUDE.md Section 4 has been updated to state explicitly that both competitor types store names.

---

### H1. [RESOLVED V2.8.3] Applying a preset can WIPE existing size_value
**Files:** [services/woodboss.py:1150-1152](../services/woodboss.py#L1150-L1152), :1174-1176
```python
if 'size_value' in block_spec:
    existing.size_value = block_spec['size_value']
```
`apply_preset` writes whatever key is **present** in the preset dict. `build_preset_from_form` and `build_preset_from_config` both construct specs with `size_value` set to `None` when the user left the diameter blank. Result: saving a preset with species-only (no diameter) and then applying it **nulls out** the tournament's existing diameters for every block row and log row the preset touches.

Repro: Save preset where one row has species but blank diameter → reload another tournament → Apply that preset → diameters on the target tournament are gone.

**Fix applied:** `apply_preset` now uses `_apply_spec_to_row()` which skips any field whose value is `None`. Also gates on `spec.get('species') is None` to avoid inserting half-empty rows for categories the preset doesn't cover. Regression test: `TestPresetRoundtrip::test_apply_preset_does_not_wipe_existing_diameter` in [tests/test_woodboss.py](../tests/test_woodboss.py).

### H2. [RESOLVED V2.8.3] `save_config` cannot clear a previously-set field
**File:** [routes/woodboss.py:114-116](../routes/woodboss.py#L114-L116)
```python
if species is None and size_value is None and notes is None and count_override is None:
    continue
```
If a user blanks every field on a row to clear it, the whole row is skipped and the old DB values persist — there's no way to remove species/size from the UI once set. The only escape is `/config/copy-from` (which replaces everything) or direct SQL.

**Fix applied:** `save_config` now writes `None` values through when the row already exists (user blanked on purpose), and still skips inserts for new-and-empty rows. Flashes a "row(s) cleared" count so it's visible. Regression test: `TestSaveConfigClear::test_blanking_existing_row_clears_it`.

### H3. [RESOLVED V2.8.3] `delete_preset` route has no UI
**Route exists:** [routes/woodboss.py:245-262](../routes/woodboss.py#L245)
**No matching template form** — grepped all templates for `delete-preset`/`deletePreset`, zero hits. Custom presets are append-only from the UI. Users can only clean up via shell (`instance/wood_presets.json`).

**Fix applied:** `config_form` now passes `custom_preset_names` to the template; [templates/woodboss/config.html](../templates/woodboss/config.html) renders a "Manage custom presets" row of delete buttons (built-ins not shown). Each button posts to the existing `delete_preset` route with CSRF + `data-confirm`.

---

## MEDIUM

### M1. [RESOLVED V2.8.3] Preset applies to ALL block rows with one species
**Files:** [services/woodboss.py:1140-1163](../services/woodboss.py#L1140-L1163), :1238-1253
Block presets store a **single** `block_spec` that's stamped onto every non-relay block row (College Men, College Women, Pro Men, …, Pro 3-Board Jigger). If a tournament uses different species/sizes per division — e.g. different wood for pro 3-board jigger vs. underhand — a preset can't represent that, and `build_preset_from_config` only captures the first row it sees with species set, silently discarding the rest.

**Fix applied:** Preset format now has `blocks_by_key` (V2, per-category) alongside `blocks` (V1 broadcast). `build_preset_from_form` and `build_preset_from_config` emit both; `_resolve_block_spec_for_key` in `apply_preset` reads V2 first, falls back to V1. Old preset files still load. Regression test: `TestPresetRoundtrip::test_apply_preset_per_cfg_key_support`.

### M2. [RESOLVED V2.8.3] `log_relay_doublebuck` is never in presets
**File:** [services/woodboss.py:1133-1138, 1256](../services/woodboss.py#L1133-L1138)
The preset system covers `log_general/stock/op/cookie` but omits `log_relay_doublebuck`. Presets round-trip through DB minus this row.

**Fix applied:** New constant `_LOG_PRESET_KEYS = (LOG_GENERAL_KEY, LOG_STOCK_KEY, LOG_OP_KEY, LOG_COOKIE_KEY, LOG_RELAY_DOUBLEBUCK_KEY)` drives both apply and build. Regression test: `TestPresetRoundtrip::test_apply_preset_includes_log_relay_doublebuck`.

### M3. [RESOLVED V2.8.3] `calculate_saw_wood` relay size_unit has wrong condition
**File:** [services/woodboss.py:576-577](../services/woodboss.py#L576-L577)
```python
rel_size_unit = (relay_db_cfg.size_unit if relay_db_cfg and relay_db_cfg.size_value is not None else
                 (general_cfg.size_unit if general_cfg else 'in'))
```
Reads `size_unit` but gates on `size_value is not None`. A relay row with an explicitly chosen unit but no diameter yet falls back to general. Not a crash, just wrong display once both conditions diverge.

**Fix applied:** Now gates on `relay_db_cfg.size_unit in ('in', 'mm')` instead of `size_value is not None`.

### M4. [RESOLVED V2.8.3] `calculate_blocks` inconsistent `count_override` handling
**File:** [services/woodboss.py:406, 412](../services/woodboss.py#L406)
Relay path uses truthy test (`if cfg.count_override`), non-relay uses `is not None`. So non-relay `count_override = 0` means "override to zero blocks" while relay `count_override = 0` means "not set" (falls through to 0 anyway, but the semantic mismatch will bite if someone later changes the enrollment-fallback path).

**Fix applied:** Relay branch in `calculate_blocks` now uses `is not None`. Matches the non-relay branch.

### M5. [RESOLVED V2.8.3] `WoodConfig.size_unit` declared without `server_default`
**File:** [models/wood_config.py:37](../models/wood_config.py#L37)
```python
size_unit = db.Column(db.String(4), nullable=False, default='in')
```
Violates CLAUDE.md model column rules — `default=` is Python-side only, so raw SQL inserts and PostgreSQL will trip over missing defaults. Not hit today because all writes go through SQLAlchemy, but it's a landmine for future migrations that backfill rows.

**Fix applied:** `size_unit` now declares `server_default=sa.text("'in'")`. Column already appears in `KNOWN_SERVER_DEFAULT_DRIFT` — no new migration needed, drift allowlist entry retires on the next rebuild.

### M6. [RESOLVED V2.8.3] Preset file write is not atomic
**File:** [services/woodboss.py:1098-1099, 1109-1110](../services/woodboss.py#L1098-L1099)
`save_custom_preset` / `delete_custom_preset` open `wood_presets.json` in `'w'` mode and json.dump directly. A crash between open and dump yields a truncated/empty JSON file, and `get_all_presets` swallows the `JSONDecodeError` silently — so all custom presets vanish without flash warning.

**Fix applied:** New `_write_preset_file()` writes to `wood_presets.json.tmp` then `os.replace()`. New `_load_preset_file()` logs a warning via `logging.getLogger(__name__)` on `JSONDecodeError`. Regression test: `TestPresetRoundtrip::test_atomic_write_survives_simulated_crash`.

### M7. [DEFERRED] Preset routes don't revalidate tournament ownership
**File:** [routes/woodboss.py:206, 224, 248](../routes/woodboss.py#L206)
```python
Tournament.query.get_or_404(tid)
```
The return value is discarded. In a multi-tenant future this is a stub waiting to bite — no scope check beyond the management-blueprint login hook. Today it's fine because any judge can touch any tournament, but worth noting.

**Fix:** Keep as-is for now; revisit when tenants exist.

### M8. [DEFERRED] No rate-limit on public share route
**File:** [routes/woodboss.py:327-340](../routes/woodboss.py#L327)
`woodboss_public_bp.share` has valid HMAC verification but no rate limiting. A leaked token is valid for 7 days, and report generation runs full enrollment queries per request. Scraping a token is cheap.

**Fix:** Add `write_limit`-style reader throttling or cache `get_wood_report` output per (tid, day).

---

## LOW

### L1. [RESOLVED V2.8.3] `save_config` swallows negative count_override silently
Save now collects negatives into `negative_overrides` and flashes a warning listing the affected keys. The value is still coerced to `None` so the save succeeds.

### L2. [DEFERRED] `is_gendered` list omits `jack & jill`
Intentional — J&J is mixed-gender-partnered and collapses to `open`. Leaving untouched; documented in the audit lesson section.

### L3. [RESOLVED V2.8.3] `apply_preset` creates WoodConfig rows for zero-enrollment categories
`apply_preset` already gates on `_active_block_keys(tournament_id)` (added during the H0 fix pass), so ghost rows are no longer planted. `prune_stale_block_configs` runs after save/copy/apply to clean up any historical ghosts.

### L4. [RESOLVED V2.8.3] Preset save silently overwrites built-in preset names
`save_custom_preset` now raises `ValueError` on a built-in collision; `routes/woodboss.save_preset` catches and flashes. Regression test: `TestPresetRoundtrip::test_save_rejects_builtin_name_collision`.

### L5. [RESOLVED V2.8.3] `calculate_springboard_dummies` friday-feature JSON is read on every report call
Extracted into `_detect_friday_feature_springboard(tournament_id)` helper. The math function is now pure(r) — pass `None` for `tournament_id` in unit tests to skip the file IO entirely.

### L6. [RESOLVED V2.8.3] Dead global `_PRESET_FILE`
Replaced with a plain function that computes the path each call. No module-level state to leak across pytest workers.

---

## Test coverage added V2.8.3

[tests/test_woodboss.py](../tests/test_woodboss.py) grew by three test classes (40 tests total, all passing):

- `TestCollegeEnrollmentByName` — H0 regression. Asserts that `_count_competitors` and `get_wood_report` correctly resolve college event NAMES (not IDs), and that `closed_event_count` counts CLOSED entries under the dual ID+name resolver.
- `TestPresetRoundtrip` — H1, M1, M2, M6, L4, save/delete roundtrip. Uses a `tmp_preset_file` fixture that monkeypatches `_preset_path` to a `tmp_path` file so tests never touch `instance/wood_presets.json`.
- `TestSaveConfigClear` — H2 regression. Logs in a unique judge user, POSTs a fully-blanked form through the real `save_config` route, and asserts the existing `WoodConfig` row is actually cleared.

Still uncovered (next pass):
- `calculate_saw_wood` direct unit tests (only exercised via report integration)
- `calculate_springboard_dummies` friday-feature branch — now easier to test since `_detect_friday_feature_springboard` can be monkeypatched
- `get_lottery_view`
- `generate_share_token` / `verify_share_token` round-trip

---

## Status summary

| Finding | Severity | Status |
|---|---|---|
| H0 — college enrollment missing from report | HIGH | RESOLVED V2.8.2 |
| H1 — apply_preset wipes existing size_value | HIGH | RESOLVED V2.8.3 |
| H2 — save_config can't clear a row | HIGH | RESOLVED V2.8.3 |
| H3 — delete_preset has no UI | HIGH | RESOLVED V2.8.3 |
| M1 — single-species block preset | MEDIUM | RESOLVED V2.8.3 |
| M2 — log_relay_doublebuck missing from presets | MEDIUM | RESOLVED V2.8.3 |
| M3 — relay size_unit wrong condition | MEDIUM | RESOLVED V2.8.3 |
| M4 — count_override truthy vs is-not-None | MEDIUM | RESOLVED V2.8.3 |
| M5 — size_unit missing server_default | MEDIUM | RESOLVED V2.8.3 |
| M6 — preset file write not atomic | MEDIUM | RESOLVED V2.8.3 |
| M7 — tournament ownership check | MEDIUM | DEFERRED (no multi-tenant yet) |
| M8 — share route rate limit | MEDIUM | DEFERRED |
| L1 — negative count_override silent | LOW | RESOLVED V2.8.3 |
| L2 — J&J not in is_gendered | LOW | DEFERRED (intentional) |
| L3 — ghost rows for zero-enrollment | LOW | RESOLVED (done in H0 pass) |
| L4 — built-in preset name collision | LOW | RESOLVED V2.8.3 |
| L5 — file IO inside springboard math | LOW | RESOLVED V2.8.3 |
| L6 — dead _PRESET_FILE global | LOW | RESOLVED V2.8.3 |
