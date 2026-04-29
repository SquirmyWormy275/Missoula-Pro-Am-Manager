# Registration / Partner / Gear Recon — 2026-04-27

Read-only audit. No code changes, no commits, no branches.
Source tree: `Missoula-Pro-Am-Manager/` (current main, post-V2.14.14, 2026-04-27).

> **Update (later same day, post-V2.14.16):** companion recon `docs/recon/dual_path_recon_2026_04_27.md` covers the dual-path (pro xlsx + college xlsx) and Pro-Am Relay merge in detail. New findings discovered while writing the companion are appended at the end of each section below as "Amendment (dual-path pass)". The original prose is unchanged.

Note on `scheduling.py` (referenced at 2,018 lines in the request): that monolith no longer exists. Per `CLAUDE.md` Section 5 ("Scheduling blueprint decomposed into package"), `routes/scheduling.py` was split into the `routes/scheduling/` package (14 files, 6,571 LOC). Section 6 below treats the closest analogs as the scheduling boundary: the route package plus `services/heat_generator.py`, `services/flight_builder.py`, `services/schedule_builder.py`, `services/schedule_status.py`, `services/schedule_generation.py`. Combined they are ~10,431 LOC — five times the size of the old monolith.

---

## SECTION 1 — REPO MAP

### Two-level directory listing (excluding `__pycache__/`, `instance/`, `.git/`, `.venv/`, `node_modules/`, `.qa_tmp/`, `.gstack/`, `.pytest_cache/`, `.ruff_cache/`)

```
.
./.claude
./.claude/skills
./.compound-engineering
./.context/retros
./.github/workflows
./.vscode
./docs
./docs/Alex's Docs
./docs/archived
./docs/brainstorms
./docs/designs
./docs/plans
./docs/solutions
./migrations
./migrations/versions
./models
./routes
./routes/scheduling
./scripts
./services
./static
./static/{audio,css,img,js,video}
./templates
./templates/{admin,auth,college,errors,partnered_axe,portal,pro,proam_relay,
              reporting,reports,scheduling,scoring,strathmark,validation,woodboss}
./tests
./tests/fixtures
./uploads
```

Top-level files of note:
`app.py` (563 lines), `config.py` (562), `database.py` (35), `strings.py` (713), `pyproject.toml`, `railway.toml`, `Procfile`, `requirements.txt`, `pytest.ini`, `pyrightconfig.json`, `CLAUDE.md` (99 KB), `DESIGN.md` (37 KB), `DEVELOPMENT.md` (185 KB), `FlightLogic.md` (28 KB), `README.md`, `USER_GUIDE.md`.

### Python files over 200 lines (top — sorted by line count)

```
1892  services/gear_sharing.py
1747  routes/scoring.py
1572  services/woodboss.py
1564  routes/registration.py
1525  services/flight_builder.py
1357  tests/test_strathmark_sync.py
1284  routes/portal.py
1238  services/heat_generator.py
1129  tests/test_models_full.py
1122  services/excel_io.py
1106  tests/test_route_smoke_qa.py
1090  tests/test_scratch_cascade.py
1073  services/registration_import.py
1072  tests/test_woodboss.py
1039  tests/test_gear_sharing.py
1016  services/birling_bracket.py
1014  services/scoring_engine.py
 989  tests/test_mark_assignment.py
 981  routes/scheduling/heats.py
 980  routes/main.py
 953  services/print_catalog.py
 900  routes/reporting.py
 878  tests/test_flight_builder_integration.py
 867  routes/scheduling/flights.py
 845  tests/test_heat_gen_integration.py
 841  routes/scheduling/events.py
 837  tests/test_integration_qa.py
 815  tests/fixtures/synthetic_data.py
 803  services/strathmark_sync.py
 777  tests/test_migration_integrity.py
 ... (full list ≥200 LOC: 175 files; truncated. See run output for tail.)
 563  app.py
 562  config.py
 557  services/schedule_builder.py
 555  services/preflight.py
 500  services/schedule_status.py
 485  routes/scheduling/assign_marks.py
 481  routes/import_routes.py
 412  scripts/qa_solo_heat_placement.py
 396  models/competitor.py
 395  routes/auth.py
 394  services/partnered_axe.py
 391  routes/scheduling/__init__.py
 379  routes/api.py
 370  services/video_judge_export.py
 361  routes/scheduling/friday_feature.py
 347  services/pro_entry_importer.py
 322  services/email_delivery.py
 319  models/event.py
 314  routes/proam_relay.py
 307  services/partner_matching.py
 296  routes/scheduling/ability_rankings.py
 291  routes/partnered_axe.py
 268  models/tournament.py
 253  services/backup.py
 220  models/heat.py
 214  services/background_jobs.py
 209  services/saw_block_assignment.py
 202  services/ala_report.py
 201  services/partner_resolver.py
```

### Flask app entrypoint, models, blueprints

- **App entrypoint:** [`app.py`](../../app.py). `create_app()` is the factory. Models are imported via `init_db()` in [`database.py`](../../database.py).
- **Models module:** [`models/__init__.py`](../../models/__init__.py) re-exports: `Tournament`, `Team`, `CollegeCompetitor`, `ProCompetitor`, `Event`, `EventResult`, `Heat`, `HeatAssignment`, `Flight`, `User`, `AuditLog`, `BackgroundJob`, `SchoolCaptain`, `WoodConfig`, `ProEventRank`, `PayoutTemplate`, `PrintTracker`, `PrintEmailLog`.
- **Blueprints registered in `app.py:346-371`:** `main_bp`, `registration_bp` (`/registration`), `scheduling_bp` (`/scheduling`), `scoring_bp` (`/scoring`), `reporting_bp` (`/reporting`), `proam_relay_bp`, `partnered_axe_bp`, `validation_bp`, `import_pro_bp` (`/import`), `woodboss_bp` (`/woodboss`), `woodboss_public_bp` (`/woodboss`), `strathmark_bp` (`/strathmark`), `demo_bp` (`/demo`), `domain_conflicts_bp`, `auth_bp` (`/auth`), `portal_bp` (`/portal`), `api_bp` (`/api` and `/api/v1`).

### Files matching the keyword set (parse / import / ingest / register / assign / partner / gear / share / schedul)

```
migrations/versions/109d1ac298e1_add_gear_sharing_to_college_competitors.py
migrations/versions/41b9a6cbcfd4_add_import_fields_to_pro_competitors.py
migrations/versions/h5i6j7k8l9m0_add_schedule_config_to_tournaments.py
routes/import_routes.py
routes/partnered_axe.py
routes/scheduling/__init__.py
routes/scheduling/ability_rankings.py
routes/scheduling/assign_marks.py
routes/scheduling/birling.py
routes/scheduling/events.py
routes/scheduling/flights.py
routes/scheduling/friday_feature.py
routes/scheduling/heat_sheets.py
routes/scheduling/heats.py
routes/scheduling/partners.py
routes/scheduling/preflight.py
routes/scheduling/print_hub.py
routes/scheduling/pro_checkout_roster.py
routes/scheduling/show_day.py
services/gear_sharing.py
services/mark_assignment.py
services/partner_matching.py
services/partner_resolver.py
services/partnered_axe.py
services/pro_entry_importer.py
services/registration_import.py
services/saw_block_assignment.py
services/schedule_builder.py
services/schedule_generation.py
services/schedule_status.py
+ matching test files (16 of them, including test_gear_sharing*, test_partner*,
  test_pro_entry_importer*, test_registration_import.py, test_schedule_*).
```

### Findings (Section 1)

1. **Five files >1,000 lines do the heavy domain work.** `gear_sharing.py` (1,892), `flight_builder.py` (1,525), `heat_generator.py` (1,238), `registration_import.py` (1,073), `excel_io.py` (1,122). Any rebuild of registration / partner / gear will touch all five. Race-day risk: each is single-author, integration-test-heavy, and brittle to JSON-shape changes.
2. **Two parallel ingestion paths exist.** Pro entries: `pro_entry_importer.py` (basic) → `registration_import.py` (enhanced). Each parses the same xlsx but produces overlapping outputs. The confirm route (`routes/import_routes.py`) runs the enhanced pipeline first, falls back to the basic on exception, then runs `compute_review_flags()` again on top. Skew between the two parsers is a live source of subtle field drift.
3. **The `routes/scheduling/` package consumed the old 2,018-line monolith and grew 5x.** Total scheduling surface today is ~10,431 LOC across 19 files. Any "rewrite scheduling because it's small" framing is stale by a year.

### Amendment (dual-path pass)

- **The pro and college xlsx paths are completely separate modules.** Pro xlsx → `services/pro_entry_importer.py` + `services/registration_import.py`. College xlsx → `services/excel_io.py`. They share NOTHING beyond `services/gear_sharing.py` (the shared parser library) and the underlying JSON model fields. A unified pipeline rebuild that touches both will need to reconcile two completely different parsing philosophies (header-key dict vs heuristic header detection).
- **Zero TODO/FIXME/HACK tags exist in `services/`, `routes/`, or `models/`.** Verified by `grep -rn 'TODO|FIXME|HACK|XXX'` — empty result project-wide. The decision narrative is in long inline comments and commit-body retros — there is no in-code marker that future work tracking could grep. Any rebuild adding tagged debt markers will be the first to do so.

---

## SECTION 2 — REGISTRATION INGESTION PATH

### Ingestion entry points

There are FOUR distinct ingestion paths to the database:

| # | Entry route | Service module | Target |
|---|---|---|---|
| 1 | `POST /import/<tid>/pro-entries` (upload) → `POST /import/<tid>/pro-entries/confirm` | `services/pro_entry_importer.parse_pro_entries()` + `services/registration_import.run_import_pipeline()` | `ProCompetitor`, `EventResult` |
| 2 | `POST /registration/<tid>/college/upload` | `services/excel_io.process_college_entry_form()` | `Team`, `CollegeCompetitor` |
| 3 | `GET/POST /registration/<tid>/pro/new` (manual single-pro entry) | inline form handler in `routes/registration.py:538-607` | `ProCompetitor` |
| 4 | Various per-route partner / gear edit POSTs in `routes/registration.py` (see Section 5) | various | `partners` / `gear_sharing` JSON columns |

Path #1 is the highest-volume race-week path (Google Form → xlsx → import). #2 is the college roster path. #3 is rarely used in production. #4 is post-import cleanup.

### Path #1 — Pro xlsx import

#### `parse_pro_entries(filepath: str) -> list`
**File:** [`services/pro_entry_importer.py:85-267`](../../services/pro_entry_importer.py#L85-L267)
**Docstring:** "Parse a Google Forms xlsx export (first sheet) and return a list of dicts. Each dict contains all form data needed for review and DB import. Datetime objects are converted to ISO strings for JSON serialisability. Rows where 'Full Name' is blank are silently skipped."

Key parsing logic (excerpt — lines 96-168):

```python
wb = openpyxl.load_workbook(filepath, data_only=True)
ws = wb.worksheets[0]

raw_headers = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
stripped = [h.strip() if isinstance(h, str) else (h or '') for h in raw_headers]

hmap = {}
for i, h in enumerate(stripped):
    if not h:
        continue
    hmap[h] = i
    first_line = h.splitlines()[0].strip()
    if first_line:
        hmap.setdefault(first_line, i)

# Prefix-matched special columns
waiver_col      = next((i for i, h in enumerate(stripped) if h.startswith(_WAIVER_HEADER_START)), None)
gear_detail_col = next((i for i, h in enumerate(stripped) if h.lower().startswith('if yes, provide')), None)
notes_col       = next((i for i, h in enumerate(stripped) if h.lower().startswith('anything else we should know')), None)
slow_heat_col   = _find_column_index(stripped, [
    'springboard slow heat',
    'slow heat springboard',
    'relegated to slow heat',
    'springboard slow',
])

for row in ws.iter_rows(min_row=2, values_only=True):
    name_val = _get(row, hmap.get('Full Name'))
    if not name_val or not str(name_val).strip():
        continue

    ts = _get(row, hmap.get('Timestamp'))
    timestamp_str = ts.isoformat() if isinstance(ts, datetime) else (str(ts) if ts else None)

    email = str(_get(row, hmap.get('Email Address')) or '').strip() or None
    name  = str(name_val).strip()

    gender_raw = str(_get(row, hmap.get('Gender')) or '').strip()
    if gender_raw == 'Male':
        gender = 'M'
    elif gender_raw == 'Female':
        gender = 'F'
    else:
        gender = gender_raw[:1].upper() if gender_raw else ''
    ...
```

The full event mapping is a static dict `_EVENT_MAP` with 27 entries (`pro_entry_importer.py:17-47`) mapping form-header strings (e.g. `'Springboard (L)'`, `'Jack & Jill'`, `'Hot Saw'`) to canonical event names. Aliases collapse to the same canonical (e.g. `'Springboard (L)'` and `'Springboard (R)'` both → `'Springboard'`). Three columns drive partner extraction, hard-coded by lowercased stripped header: `'men's double buck partner name'`, `'jack & jill partner name'`, `'partnered axe throw 2'` (`pro_entry_importer.py:50-54`).

#### `run_import_pipeline(filepath: str) -> ImportResult`
**File:** [`services/registration_import.py:655-716`](../../services/registration_import.py#L655-L716)
**Docstring:** "Run the full import pipeline on an xlsx file. This wraps parse_pro_entries() and adds all validation/cross-validation."

Calls `_deduplicate()` → `_build_name_index()` → `_process_entry()` per row → `_validate_partner_reciprocity()` → `_validate_gear_sharing()` → `_infer_gear_from_partnerships()` → `_reconcile_gear_flags()` → `_check_unregistered_references()`.

Key dirty-string parser (excerpt from `_classify_partner_value`, lines 200-262):

```python
_NEEDS_PARTNER_PATTERNS = [
    (re.compile(r"^\s*\?\s*$"), '"?" -> Needs Partner'),
    (re.compile(r"^\s*(?:idk|IDK|Idk)\s*$", re.IGNORECASE), '"idk" -> Needs Partner'),
    (re.compile(r"^\s*(?:lookin|looking)\b", re.IGNORECASE), '"Looking" -> Needs Partner'),
    (re.compile(r"^\s*(?:whoever|anyone\s*available|anyone)\s*$", re.IGNORECASE), ...),
    (re.compile(r"^\s*(?:no\s*(?:o?a?r?n?t?e?r?|partner)|none)\s*$", re.IGNORECASE), ...),
    (re.compile(r"^\s*(?:need\s*partner|needs\s*partner)\s*$", re.IGNORECASE), ...),
    (re.compile(r"^\s*(?:tbd|TBD)\s*$", re.IGNORECASE), '"TBD" -> Needs Partner'),
    (re.compile(r"^\s*N/?A\s*$", re.IGNORECASE), '"N/A" -> Needs Partner'),
]

_HAS_SAW_PATTERN = re.compile(r"have\s+saw", re.IGNORECASE)
_SPARE_PATTERN = re.compile(r"spare", re.IGNORECASE)

def _classify_partner_value(raw: str) -> tuple[str, str | None]:
    val = str(raw or "").strip()
    if not val:
        return "empty", None
    for pattern, label in _NEEDS_PARTNER_PATTERNS:
        if pattern.match(val):
            return "needs_partner", f"AUTO-RESOLVED: {label}"
    if _HAS_SAW_PATTERN.search(val):
        return ("needs_partner", 'AUTO-RESOLVED: "Have saw, need partner" -> Needs Partner ...')
    ...
    if re.search(r"put\s+me\s+down", val, re.IGNORECASE):
        return ("needs_partner", f'AUTO-RESOLVED: "{val[:40]}..." -> Needs Partner (spare request)')
    return "name", None
```

The dirty-text gear parser (`_parse_dirty_gear_text`, `registration_import.py:390-530`) handles parenthetical groupings, `event:names` patterns, semicolon-split segments, dash-separated equipment-vs-name pairs, and conversational filler. It uses a 17-entry equipment alias dict and three regex passes.

#### `confirm_pro_entries` (commit handler)
**File:** [`routes/import_routes.py:215-480`](../../routes/import_routes.py#L215-L480)

This is where the parsed dicts hit the database. Uses `email` as the dedup key (`ProCompetitor.query.filter_by(email=...).first()`) — name collisions with different emails create separate rows. `entry_fees` is rebuilt from scratch each import (line 308-309: `competitor.entry_fees = '{}'; competitor.partners = '{}'; competitor.gear_sharing = '{}'`), then re-populated from parsed data.

### Path #2 — College xlsx import

#### `process_college_entry_form(filepath, tournament, original_filename) -> dict`
**File:** [`services/excel_io.py:13-51`](../../services/excel_io.py#L13-L51)
**Docstring:** "Process a college entry form Excel file and import teams/competitors. Expected format ... Sheet contains team and competitor information; Columns should include: Name, Gender, Events, Partners."

Flow:
1. `pd.read_excel(filepath, sheet_name=0, header=None)` — read raw to detect headers.
2. `_detect_header_row()` — scan first 50 rows for a row containing both a name token (`name`/`competitor`/`athlete`/`participant`) AND a school/team token. Returns `None` on failure (raises `ValueError`).
3. `_school_name_from_filename(original_filename)` — strip noise (`'entry form'`, `'roster'`, `'team'`, `'pro am'`, etc.) from filename to derive the default school.
4. Re-read with detected header row, normalize column names to lowercase.
5. Branch on column presence: `_process_standard_entry_form()` vs `_process_inferred_format()`.

Dispatcher (`excel_io.py:47-51`):
```python
if _find_column(df, ['school', 'university', 'college', 'institution']) or _find_column(df, ['team', 'team code']):
    return _process_standard_entry_form(df, tournament, default_school_name=default_school_name)
else:
    return _process_inferred_format(df, tournament, default_school_name=default_school_name)
```

Standard path uses many `_find_column()` calls with candidate name lists, plus `_find_event_marker_columns()` which keyword-matches headers against a hand-coded equipment vocabulary (`['horiz', 'vert', 'pole', 'climb', 'choker', 'saw', 'birling', 'kaber', 'caber', 'chop', 'buck', 'toss', 'hit', 'speed', 'axe', 'throw', 'pv', 'peavey', 'log roll', 'pulp', 'power', 'obstacle', 'single']`).

`_canonicalize_event_name()` (`excel_io.py:638-677`) does free-text → canonical name via cascading `if 'jack' in normalized and 'jill' in normalized: return 'Jack & Jill Sawing'` style branches — 14 branches total. Anything not matched falls through unmodified.

`_extract_gear_sharing_note()` and `_apply_gear_sharing_note_to_team()` (`excel_io.py:455-503`) scan a "team group" with no valid competitor names for free-text gear notes (looking for `'crosscut'`, `'gear'`, `'share'`) and apply them to the LAST real team — implicit position-based association, not explicit linkage.

### Free-text fields parsed by string operations (across all four ingestion paths)

| Source field | Reader | What it parses | Where the parsed result lands |
|---|---|---|---|
| Google Forms `'Full Name'` column | `pro_entry_importer._get(row, hmap.get('Full Name'))` line 126 | Whole-name string. No first/last split. | `ProCompetitor.name` |
| Google Forms `'Phone Number'` column | `pro_entry_importer.py:149-156` | float → int → str fallback to raw stringification | `ProCompetitor.phone` |
| Google Forms `'Gender'` column | `pro_entry_importer.py:138-144` | `'Male'/'Female'` → `'M'/'F'`, else first char uppercased | `ProCompetitor.gender` |
| Form headers themselves | `pro_entry_importer.py:188-197` `for form_header, (event_name, fee) in _EVENT_MAP.items(): if _yes(_get(row, hmap.get(form_header)))` | Form column names matched against 27-entry static `_EVENT_MAP`. Anything not in the map is silently dropped. | `ProCompetitor.events_entered` |
| Partner-name text columns (`men's double buck partner name`, `jack & jill partner name`, `partnered axe throw 2`) | `pro_entry_importer.py:204-214` then `registration_import._classify_partner_value` (lines 227-262) | Free text classified by regex into `'needs_partner'` / `'name'` / `'empty'`. | `ProCompetitor.partners` JSON dict (event_name → partner_name string) |
| Gear-sharing-details ("If Yes, provide...") column | `pro_entry_importer.py:218-219`, then `services.gear_sharing.parse_gear_sharing_details()` (1,073 LOC module) | Free text → equipment categories + partner names via regex + 17-alias equipment dict + difflib fuzzy match. | `ProCompetitor.gear_sharing` JSON dict (event_id|category → partner_name) |
| Waiver column (text starts with `'I know that logging events'`) | `pro_entry_importer.py:222-227` | `wv == 'Yes' or wv.startswith('I know')` | `ProCompetitor.waiver_accepted` |
| Notes ("Anything else we should know..." column) | `pro_entry_importer.py:233-234` | Stripped string | `ProCompetitor.notes` |
| Slow-heat free-text column (`'springboard slow heat'`/`'slow heat springboard'`/`'relegated to slow heat'`) | `pro_entry_importer.py:115-120, 235-236` | True if value lower-stripped is in `{'yes','y','true','1','x'}` | `ProCompetitor.springboard_slow_heat` |
| Springboard L/R checkbox columns | `pro_entry_importer.py:165-176` | `_yes()` on each. L wins on conflict; flagged separately as `'CONFLICT: BOTH L AND R SPRINGBOARD CHECKED'` in review flags | `ProCompetitor.is_left_handed_springboard` |
| College Excel `Name` cell | `services/excel_io.py:142-152` | `pd.isna()` filter, str-strip | `CollegeCompetitor.name` |
| College Excel `Gender` cell | `services/excel_io._parse_gender()` line 539 | `'F','FEMALE','W','WOMAN','WOMEN'` → `'F'`, else `'M'` (defaults to M on `pd.isna()`) | `CollegeCompetitor.gender` |
| College Excel `Events` cell | `services/excel_io._parse_events()` line 626 | `re.split(r'[,;/\n]', ...)` then `_canonicalize_event_name()` per token | `CollegeCompetitor.events_entered` |
| College Excel marker columns (`x`/`yes`/`1`) | `services/excel_io._parse_event_markers()` line 372 | per-column truthiness | `CollegeCompetitor.events_entered` |
| College Excel `Partner` cell | `services/excel_io._process_partners()` line 1037 | Free text split into name candidates | `CollegeCompetitor.partners` |
| College Excel `team_identifier` group key (`'A Team'`, `'B Team'`, etc.) | `services/excel_io._extract_team_letter()` line 335, `_looks_like_team_code()` line 350 | Regex `^([A-Da-d])\s*[Tt]eam$` and `^[A-Za-z]{2,6}[- ][A-Za-z0-9]{1,3}$` | `Team.team_code` |
| Filename (uploaded xlsx name) | `services/excel_io._school_name_from_filename()` line 322 | strip noise tokens (`'entry form'`, `'roster'`, etc.) | `Team.school_name`, `Team.school_abbreviation` |
| College Excel "team-shaped" rows that have no valid competitor names | `services/excel_io._extract_gear_sharing_note()` line 455 | Free-text scan for `'crosscut'`/`'gear'`/`'share'` keywords | Implicitly attached to the LAST real team via position (NO explicit linkage) |

### Free-text → structured-form-field gaps (places where structure SHOULD exist but parsing does the work)

Every entry in the table above where the source is a free-text cell rather than a discrete checkbox is a free-text-instead-of-structured gap. Specific high-value gaps:

1. **Partner identity is a name string, not an FK.** Form has no "select your partner from the registered list" UX. Result: every partner field is parsed by `_classify_partner_value()` then fuzzy-matched via `services.name_match.find_partner_match()` to recover the actual `ProCompetitor.id`. Mismatch and nickname problems propagate into heat generation.
2. **Event entry is a per-event checkbox column.** No single "events selected" structured field — `_EVENT_MAP` has 27 hand-coded form-header → canonical-event mappings. New events require code changes in `pro_entry_importer.py:_EVENT_MAP`, in `routes/import_routes.py:_EVENT_FEES`, AND in `excel_io._canonicalize_event_name()` for college imports. Three update sites for one concept.
3. **Gear sharing is a single free-text "details" textarea.** Parsed by `gear_sharing.parse_gear_sharing_details()` — a 175-line function (`gear_sharing.py:491-663`) using regex + 17-alias equipment dict + difflib + fuzzy partner resolver. Emits warnings: `'events_not_resolved'`, `'partner_not_resolved'`, etc.
4. **College team identification depends on group-key parsing.** Pandas `groupby` on a column whose values look like `'A Team'`, `'B Team'`. Regex `_extract_team_letter` recovers the letter. School name comes from filename if present, falls back to a preamble row, falls back to the team identifier itself. Three-tier fallback per file.
5. **College school abbreviation has a 28-entry hard-coded dict.** `excel_io._abbreviate_school()` line 582 — adding a new school requires editing the dict.

### TODO/FIXME/HACK/XXX/NOTE comments in the ingestion code path

`grep -rn 'TODO|FIXME|HACK|XXX' services/registration_import.py services/pro_entry_importer.py services/excel_io.py routes/import_routes.py routes/registration.py` returns **zero matches**. The only "NOTE" hits are two report-string labels in `registration_import.py:245,251` (`'note: has equipment'`, `'note: available as spare'`) — they are user-facing labels, not code annotations.

This is suspicious for a 1,073-line dirty-data parser written under race-week pressure. There ARE many narrative comments documenting decisions and gotchas (e.g. `pro_entry_importer.py:160-176` on handedness, `routes/import_routes.py:328-355` on Q27 inconsistencies, `excel_io.py:43-44` on filename precedence) — none are tagged.

### Findings (Section 2)

1. **Two parallel xlsx parsers run on the same file in series.** `parse_pro_entries()` (basic) runs first; `run_import_pipeline()` (enhanced) runs immediately after on the SAME upload, then `compute_review_flags()` runs again on the dicts produced by `to_entry_dicts()`. If the enhanced pipeline raises, the basic results are kept — but the enhanced pipeline ALSO calls `parse_pro_entries()` internally (`registration_import.py:664`). Race-day risk: divergent regex/normalization between the two layers produces fields that disagree about what the operator actually entered.
2. **Event/fee mappings are duplicated across THREE files.** `_EVENT_MAP` in `pro_entry_importer.py:17-47`, `_EVENT_FEES` in `routes/import_routes.py:37-57`, and `_canonicalize_event_name()` cascading-if branches in `excel_io.py:638-677`. They overlap but are not identical (e.g., `_EVENT_FEES` has `'Springboard (L)':10` and `'Springboard (R)':10` — but `parse_pro_entries` collapses both to the canonical `'Springboard'` before fees are recorded; `_EVENT_FEES` uses `entry.get('events', [])` which contains canonical names — so the (L)/(R) keys are dead weight). Adding a new event silently breaks one of the three layers.
3. **Partner identity flows as raw strings, not IDs, all the way through commit.** `routes/import_routes.py:321-326` writes `competitor.set_partner(key, canonical_partner)` where `canonical_partner` is whatever `resolve_partner_name(partner_name, name_index)` returned. If the resolver missed (returned the input unchanged), the literal typo lands in `ProCompetitor.partners` JSON and stays there until preflight or heat generation tries to resolve it again with a different fuzzy ladder. Three different fuzzy ladders run during ingestion alone (`gear_sharing.resolve_partner_name`, `excel_io._fuzzy_match_member`, `name_match.find_partner_match`).

### Amendment (dual-path pass)

- **`_PARTNER_COLS` covers only THREE partner columns** (Men's Double Buck, Jack & Jill, Partnered Axe Throw 2 — see `services/pro_entry_importer.py:50-54`). Women's Double Buck partner is silently dropped at parse time. Any partnership data on a Women's Double Buck partner column never enters `entry['partners']`, never reaches reciprocity validation, never gets gear-inferred.
- **"Needs Partner" is lossy at storage.** Survives only in the `result.auto_resolved` report list; the temp file holding the report is deleted at confirm time (`routes/import_routes.py:444-447`). At the DB level NEEDS_PARTNER is the ABSENCE of a partner JSON entry — Phase 3 auto-pair cannot distinguish operator-marked-needs-partner from form-was-blank.
- **Bidirectional gear inference (J&J partnership → shared J&J saw) writes only to `result.inferred` (a report line), not to `ProCompetitor.gear_sharing` JSON.** The structured gear column is populated from the FREE TEXT details column by `parse_all_gear_details(tournament)`. Operators must manually click `Auto-populate from gear` to push the inverse direction.
- **Gender-event cross-validation has gaps.** `_FEMALE_ONLY_EVENTS` carries plain `"Women's Standing Block"` (not the Speed/Hard Hit variants); `_MALE_ONLY_EVENTS` is missing the `Men's Standing Block` Speed/Hard Hit variants entirely. The Jack & Jill mixed-gender exception is implicit — there is NO positive validation that a J&J partnership IS mixed-gender at import time. Mixed-gender enforcement runs only at partner reassignment after a scratch.
- **College Excel has no documented column schema.** Header detection in `services/excel_io.py:13-51` is fully heuristic via `_find_column()` candidate lists. No schema doc exists in code, in a constants module, or in tests. New captain-form variants silently fail or produce wrong teams.

---

## SECTION 3 — COMPETITOR AND TEAM DATA MODEL

### Tournament — [`models/tournament.py`](../../models/tournament.py)

| Field | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | Integer | False | autoincrement PK | |
| `name` | String(200) | False | — | |
| `year` | Integer | False | — | |
| `college_date` | Date | True | — | Friday |
| `pro_date` | Date | True | — | Saturday |
| `friday_feature_date` | Date | True | — | Friday Night Feature |
| `status` | String(50) | False | `TournamentStatus.SETUP` | setup / college_active / pro_active / completed |
| `providing_shirts` | Boolean | False | False | server_default `false` |
| `schedule_config` | Text | True | — | JSON: friday_pro_event_ids, saturday_college_event_ids, flight_sizing keys |
| `created_at`, `updated_at` | DateTime | False | utcnow | |

Relationships: `teams`, `college_competitors`, `pro_competitors`, `events`, `wood_configs` — all `cascade='all, delete-orphan'`.

Methods of note: `get_schedule_config()` / `set_schedule_config()` (json wrappers), `get_team_standings()`, `get_bull_of_woods()` / `get_belle_of_woods()` (placement-count tiebreak chain), `get_bull_belle_with_tiebreak_data()`.

**Free-text fields that are later parsed:** `schedule_config` (Text/JSON, parsed at every read).

### Team — [`models/team.py`](../../models/team.py)

| Field | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | Integer | False | PK | |
| `tournament_id` | Integer FK→tournaments.id | False | | |
| `team_code` | String(20) | False | — | e.g., "UM-A" |
| `school_name` | String(200) | False | — | |
| `school_abbreviation` | String(20) | False | — | |
| `total_points` | Numeric(8,2) | False | 0 | |
| `status` | String(20) | False | 'active' | active / scratched / invalid |
| `validation_errors` | Text (JSON) | True | — | List of structured error dicts |
| `is_override` | Boolean | False | False | Admin override of validation |

Relationship: `members` → `CollegeCompetitor` via `team_id` (lazy='dynamic').
UniqueConstraint on `(tournament_id, team_code)`.

**Free-text fields parsed:** `validation_errors` (JSON), and indirectly `team_code` (parsed by code that wants the letter suffix).

### CollegeCompetitor — [`models/competitor.py:18-209`](../../models/competitor.py#L18-L209)

| Field | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | Integer | False | PK | |
| `tournament_id` | Integer FK→tournaments.id | False | | |
| `team_id` | Integer FK→teams.id | False | | |
| `name` | String(200) | False | — | `@validates` truncates at 100 chars |
| `gender` | String(1) | False | — | CheckConstraint M/F |
| `individual_points` | Numeric(8,2) | False | 0 | |
| `events_entered` | Text (JSON list) | False | `'[]'` | **Stores event NAMES (strings), not IDs** |
| `partners` | Text (JSON dict) | False | `'{}'` | event_id → partner_name string |
| `gear_sharing` | Text (JSON dict) | False | `'{}'` | event_id → partner_name string |
| `portal_pin_hash` | String(255) | True | — | |
| `headshot_filename` | Text | True | — | |
| `phone_opted_in` | Boolean | False | False | server_default `false` |
| `status` | String(20) | False | 'active' | CheckConstraint active/scratched |
| `strathmark_id` | String(50) | True | — | indexed |

CheckConstraints: gender M/F, status active/scratched, individual_points ≥ 0.

**Free-text fields parsed:** `events_entered`, `partners`, `gear_sharing`. All three are JSON-encoded strings whose contents are interpreted by code at every read. The `partners` dict additionally carries the magic key `__pro_am_lottery_opt_in__` (lines 69 & 127-142) — overloading the partners JSON to store an unrelated boolean preference.

**Relationships using string lookup, not FK:** `partners` and `gear_sharing` reference partners by NAME STRING. There is NO foreign key to `CollegeCompetitor` or `ProCompetitor` for partner identity. Resolution happens at read time via fuzzy matchers in `services/name_match.py` and `services/partner_resolver.py`.

### ProCompetitor — [`models/competitor.py:212-396`](../../models/competitor.py#L212-L396)

| Field | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | Integer | False | PK | |
| `tournament_id` | Integer FK | False | | |
| `name` | String(200) | False | | `@validates` truncates at 100 |
| `gender` | String(1) | False | | CheckConstraint M/F |
| `address` | Text | True | | |
| `phone` | String(50) | True | | |
| `email` | String(200) | True | | (not unique; email IS the import dedup key) |
| `shirt_size` | String(10) | True | | |
| `is_ala_member` | Boolean | False | False | |
| `pro_am_lottery_opt_in` | Boolean | False | False | |
| `is_left_handed_springboard` | Boolean | False | False | |
| `springboard_slow_heat` | Boolean | False | False | server_default `false` |
| `events_entered` | Text (JSON list) | False | `'[]'` | **Stores event NAMES OR IDs (mixed)** |
| `entry_fees` | Text (JSON dict) | False | `'{}'` | event_id-string → number |
| `fees_paid` | Text (JSON dict) | False | `'{}'` | event_id-string → bool |
| `gear_sharing` | Text (JSON dict) | False | `'{}'` | event_id-string-or-category-key → partner_name string |
| `partners` | Text (JSON dict) | False | `'{}'` | event_id-string-or-event-name → partner_name string |
| `portal_pin_hash` | String(255) | True | | |
| `total_earnings` | Float | False | 0.0 | CheckConstraint ≥ 0 |
| `payout_settled` | Boolean | False | False | server_default `false` |
| `headshot_filename` | Text | True | | |
| `phone_opted_in` | Boolean | False | False | |
| `status` | String(20) | False | 'active' | CheckConstraint active/scratched |
| `submission_timestamp` | DateTime | True | | from Google Forms |
| `gear_sharing_details` | Text | True | | **raw free-text from form** |
| `waiver_accepted` | Boolean | False | False | |
| `waiver_signature` | String(200) | True | | |
| `notes` | Text | True | | **raw free-text from form** |
| `total_fees` | Integer | False | 0 | CheckConstraint ≥ 0 |
| `import_timestamp` | DateTime | True | | |
| `strathmark_id` | String(50) | True | | indexed |

**Free-text fields parsed:**
- `events_entered` — JSON list of mixed event-name strings or stringified IDs.
- `entry_fees` / `fees_paid` — JSON dicts keyed by stringified event_id, with at least one magic key (`'relay'` per `routes/import_routes.py:317-318`).
- `partners` — JSON dict keyed by stringified event_id OR event name OR display_name (per `services/partner_matching._read_partner_name()` lines 59-73, which probes 5 key variants).
- `gear_sharing` — JSON dict keyed by stringified event_id OR `'category:<name>'` OR `'group:<name>'`. Values may be a partner name OR `'using:<partner>'` (USING confirmation, per `services/gear_sharing.py:42`) OR `'group:<group_name>'`.
- `gear_sharing_details` — UNPARSED original textarea text. Re-parsed by `parse_all_gear_details()` and on every gear-manager mutation.
- `notes` — never parsed; rendered as-is in templates.

### Event — [`models/event.py:11-149`](../../models/event.py#L11-L149)

| Field | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id`, `tournament_id` | Integer | | | |
| `name` | String(200) | False | | |
| `event_type` | String(20) | False | | college / pro |
| `gender` | String(10) | True | | M / F / None |
| `scoring_type` | String(20) | False | | time/score/distance/hits/bracket |
| `scoring_order` | String(20) | False | 'lowest_wins' | |
| `is_open` | Boolean | False | False | |
| `is_handicap` | Boolean | False | False | |
| `is_partnered` | Boolean | False | False | |
| `partner_gender_requirement` | String(10) | True | | same/mixed/any |
| `requires_dual_runs` | Boolean | False | False | |
| `requires_triple_runs` | Boolean | False | False | |
| `stand_type` | String(50) | True | | |
| `max_stands` | Integer | True | | |
| `has_prelims` | Boolean | False | False | |
| `payouts` | Text (JSON) | False | `'{}'` | dict position→amount, OR overloaded state for relay/axe/birling |
| `event_state` | Text | True | | added 2026-04 to migrate state-machine data out of `payouts` |
| `status` | String(20) | False | 'pending' | |
| `is_finalized` | Boolean | False | False | |

**Free-text fields parsed:** `payouts` and `event_state` are both JSON; `payouts` is overloaded for state-machine storage (see `Event.uses_payouts_for_state` property and CLAUDE.md §4 "JSON fields for lists and dicts").

### EventResult — [`models/event.py:152-319`](../../models/event.py#L152-L319)

29 columns. Highlights:
- `competitor_id` (Integer, NOT FK — flexible polymorphic reference).
- `competitor_type` (String(20), CheckConstraint college/pro).
- `competitor_name` (String(200) — denormalized).
- `partner_name` (String(200), nullable — denormalized partner name string).
- `result_value`, `run1_value`, `run2_value`, `run3_value`, `best_run`, `tiebreak_value`, `t1_run1`, `t2_run1`, `t1_run2`, `t2_run2` — numeric scoring fields.
- `handicap_factor` (Float, default 0.0), `predicted_time` (Float, nullable) — STRATHMARK integration.
- `final_position`, `points_awarded` (Numeric 6,2), `payout_amount`, `payout_settled`.
- `is_flagged`, `throwoff_pending`.
- `status` (String(20), CheckConstraint pending/completed/scratched/dnf/dq/partial), `status_reason` (Text).
- `version_id` for optimistic locking.

UniqueConstraint on `(event_id, competitor_id, competitor_type)`.

**Relationships using string lookup, not FK:** `competitor_id` is `Integer NOT FK` — the `competitor_type` discriminator routes the resolver to either `CollegeCompetitor` or `ProCompetitor`. `competitor_name` and `partner_name` are denormalized name strings, set at heat generation time and used throughout reporting / heat sheet rendering.

### Heat — [`models/heat.py:28-164`](../../models/heat.py#L28-L164)

| Field | Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id`, `event_id` | Integer | | | |
| `heat_number`, `run_number` | Integer | False | | run_number default 1 |
| `competitors` | Text (JSON list) | False | `'[]'` | **Authoritative competitor IDs for this heat** |
| `stand_assignments` | Text (JSON dict) | False | `'{}'` | competitor_id-string → stand_number |
| `status` | String(20) | False | 'pending' | |
| `version_id` | Integer | False | 1 | |
| `locked_by_user_id` | Integer FK→users.id | True | | |
| `locked_at` | DateTime | True | | |
| `flight_id` | Integer FK→flights.id | True | | |
| `flight_position` | Integer | True | | |

UniqueConstraint on `(event_id, heat_number, run_number)`.

**Free-text fields parsed:** `competitors` and `stand_assignments` are JSON; both `Heat.competitors` (canonical) AND `HeatAssignment` rows (validation-only) exist. `Heat.sync_assignments()` rebuilds the `HeatAssignment` rows from the JSON column.

### HeatAssignment — [`models/heat.py:13-25`](../../models/heat.py#L13-L25)

`id`, `heat_id` (FK→heats.id), `competitor_id` (Integer, NOT FK), `competitor_type` (String(20)), `stand_number` (Integer, nullable). Same polymorphic pattern as `EventResult`.

### Flight — [`models/heat.py:167-220`](../../models/heat.py#L167-L220)

`id`, `tournament_id` (FK), `flight_number`, `name` (String(100), nullable), `status` (String(20)), `notes` (Text). Heats relate via `Heat.flight_id`.

### SchoolCaptain — [`models/school_captain.py`](../../models/school_captain.py)

`id`, `tournament_id` (FK), `school_name` (String, indexed), `pin_hash`, `created_at`. UniqueConstraint `(tournament_id, school_name)`. Methods: `set_pin`, `check_pin`, `has_pin`. **Note: This relates to teams by school_name STRING, not FK.**

### Free-text fields summary (anything stored as String/Text whose contents are parsed)

| Model | Field | Parsed by |
|---|---|---|
| Tournament | `schedule_config` | `Tournament.get_schedule_config()` JSON loads |
| Team | `validation_errors` | `Team.get_validation_errors()` JSON loads |
| Team | `team_code` | various string parsers (e.g., `_extract_team_letter`) — pulls letter suffix |
| CollegeCompetitor | `events_entered` | `get_events_entered()` JSON loads; matched by NAME against Event records (no FK) |
| CollegeCompetitor | `partners` | `get_partners()` JSON loads; partner name STRING resolved by fuzzy matchers at heat-gen / preflight time |
| CollegeCompetitor | `gear_sharing` | `get_gear_sharing()` JSON loads; partner name STRING + magic prefixes (`group:`, `category:`, `using:`) |
| ProCompetitor | `events_entered` | same |
| ProCompetitor | `partners` | same |
| ProCompetitor | `entry_fees` / `fees_paid` | JSON loads with magic `'relay'` key |
| ProCompetitor | `gear_sharing` | same as college, plus `'category:'` and `'group:'` keys |
| ProCompetitor | `gear_sharing_details` | unparsed original textarea — re-parsed by `parse_all_gear_details()` and several gear manager routes |
| ProCompetitor | `notes` | never parsed |
| Event | `payouts` | overloaded — sometimes payout dict, sometimes state-machine payload |
| Event | `event_state` | JSON state-machine payload |
| EventResult | `competitor_name`, `partner_name` | denormalized strings, NOT FK; rendered directly + matched against the live competitor table by some renderers |
| Heat | `competitors` | JSON list of integer competitor IDs (canonical heat composition) |
| Heat | `stand_assignments` | JSON dict of stringified ids → stand_number |

### Relationships that use string lookup rather than foreign key

1. `CollegeCompetitor.partners` / `ProCompetitor.partners` — partner is a NAME STRING, not an FK. Multiple resolvers (`services.name_match.find_partner_match`, `services.partner_resolver.lookup_partner_cid`, `services.partner_matching._resolve_partner`) try to recover the actual ID at runtime.
2. `CollegeCompetitor.gear_sharing` / `ProCompetitor.gear_sharing` — same pattern. Partner name strings, plus `group:` / `category:` / `using:` prefixes overlaying additional semantics.
3. `CollegeCompetitor.events_entered` / `ProCompetitor.events_entered` — JSON list of event NAMES or IDs (mixed); resolved by `services.heat_generator._competitor_entered_event()` which tries ID then `Event.name` then `Event.display_name` (lines 1034-1063).
4. `EventResult.competitor_id` — Integer, NOT FK. `competitor_type` discriminator chooses the model. No referential integrity at the DB level.
5. `EventResult.competitor_name` / `EventResult.partner_name` — denormalized name strings. Renderers may re-resolve to ORM rows; if the underlying competitor was renamed after the result was created, the EventResult holds the stale name.
6. `HeatAssignment.competitor_id` — same polymorphic Integer pattern as `EventResult.competitor_id`.
7. `Heat.competitors` JSON list — integer IDs not enforced as FKs.
8. `Heat.stand_assignments` JSON dict — keys are STRINGIFIED competitor IDs (`assignments[str(competitor_id)] = stand_number`). Type-coercion at every read.
9. `SchoolCaptain.school_name` — links to teams by school name STRING, not FK to Team.
10. `Tournament.schedule_config` JSON — keys `friday_pro_event_ids`, `saturday_college_event_ids` are lists of ints with no FK enforcement; orphaned IDs stay in the config after event deletion.

### Findings (Section 3)

1. **Partner identity has no foreign key anywhere.** Every layer — registration, preflight, heat generation, scoring, heat-sheet render, video judge export — re-runs a fuzzy matcher to recover the partner's ID from a name string. Five different matchers exist (`services/name_match.find_partner_match`, `services/partner_resolver.lookup_partner_cid`, `services/partner_matching._resolve_partner`, `services/gear_sharing.resolve_partner_name`, `services/excel_io._fuzzy_match_member`). Subtle disagreement between them is the source of most race-week partner bugs (see V2.14.10 / V2.14.11 / V2.14.16 in CLAUDE.md).
2. **`events_entered` mixes IDs and names without a discriminator.** `CollegeCompetitor.closed_event_count` (`models/competitor.py:165-197`) explicitly handles both forms — reading `entry`, then `entry_str.isdigit()`, then `entry_str.lower() in closed_names`. Every consumer needs the same dual-lookup pattern; some consumers don't have it. The model docstring acknowledges this on line 50: "List of event IDs" — but the actual data is a mix of names and IDs depending on which import path wrote the row.
3. **`Event.payouts` is overloaded for state-machine storage.** `Pro-Am Relay` started using `Event.event_state` in 2026-04 (migration `b1c2d3e4f5a6`), but Partnered Axe Throw and Birling still write into `payouts`. The `Event.uses_payouts_for_state` property gates write access. Anyone reading `Event.payouts` to compute payouts on these events gets state-machine garbage instead. Race-day risk: payout configuration UI guard relies on this property — a single missed check would corrupt event state.

### Amendment (dual-path pass)

- **Pros and college are SEPARATE tables, no inheritance, no discriminator column.** Polymorphism happens on the `EventResult.competitor_type` and `HeatAssignment.competitor_type` discriminator strings only. `Heat.competitors` JSON list does NOT carry a per-id type discriminator — heat type is inferred from `Heat.event.event_type`.
- **No `Partnership` table. No `GearSharing` table. No `ProAmRelayLottery` table. No `ProAmRelayTeam` table.** All four are JSON dicts on `Event` (relay state) or on competitor rows (partners/gear).
- **Pro `pro_am_lottery_opt_in` is a real Boolean column. College `pro_am_lottery_opt_in` is a Python property masquerading as a column** — backed by a magic key `__pro_am_lottery_opt_in__` in `CollegeCompetitor.partners` JSON (`models/competitor.py:69, 127-142`). The two populations require asymmetric SQL to filter on the same logical fact.
- **2+2+2+2 relay gender/population split is enforced procedurally (in `run_lottery`), not structurally.** No DB constraint, FK, check constraint, or property. `set_teams_manually` (`services/proam_relay.py:316-441`) trusts whatever team_assignments the caller passes. A direct write to `Event.event_state` could install a 4-pro-men team with no DB-level objection.
- **Per-pair structure inside a relay team is undefined.** The model holds 4 pro IDs + 4 college IDs and 4 events with one time per (team, event). Who paired with whom for partnered sawing or team axe throw is not represented anywhere.

---

## SECTION 4 — PARTNER ASSIGNMENT LOGIC

### Functions and routes that handle partner pairings

| File | Symbol | Purpose |
|---|---|---|
| `services/partner_matching.py` | `auto_assign_event_partners(event)` | Three-phase resolver: confirm reciprocal, hold one-sided claims, auto-pair the unclaimed pool. Used by the auto-assign POST route. |
| `services/partner_matching.py` | `auto_assign_pro_partners(tournament)` | Loop wrapper over all partnered pro events. |
| `services/partner_resolver.py` | `lookup_partner_cid(partner_str, comps, self_cid)` | Heat-sheet render: resolve a partner string to an ID within the heat's competitor pool. |
| `services/partner_resolver.py` | `pair_competitors_for_heat(event, comp_ids, comp_lookup, roster_lookup)` | Heat-sheet render: collapse a heat's id list into pair rows ("Alice & Bob"). |
| `services/name_match.py` | `find_partner_match(partner_name, pool, name_getter, exclude_key, enable_fuzzy)` | Shared 4-tier matching ladder. Used by all higher-level resolvers. |
| `services/heat_generator.py` | `_find_partner(partner_name, pool, self_comp)` | Snake-draft pair builder — wraps `find_partner_match`. |
| `services/heat_generator.py` | `_get_partner_name_for_event(competitor, event)` | Reads `partners` JSON dict, probes 5 key variants (id, name, display_name, lowercases). |
| `services/heat_generator.py` | `_build_partner_units(...)` (line 675, 87 lines) | Builds the partnered-event "unit" list for snake-draft placement. |
| `services/heat_generator.py` | `_rebuild_pair_units(heat_competitors, event)` | Re-assemble pairs after a heat mutation. |
| `services/preflight.py` | `build_preflight_report(...)` | Validates partner reciprocity at the tournament level; emits codes `unresolved_partner_name`, `self_reference_partner`, `non_reciprocal_partnership` (all in `BLOCKING_CODES`). |
| `services/registration_import.py` | `_validate_partner_reciprocity(result)` (line 841) | Same check at import time. |
| `services/excel_io.py` | `_process_partners(competitor, partners_str)`, `_extract_partner_entries(row, columns)`, `_partnered_event_gender_requirements()`, `_fuzzy_match_member(...)` | College Excel partner extraction and validation. |
| `routes/scheduling/partners.py` | `partner_queue` (GET), `reassign_partner` (POST), `get_orphaned_competitors`, `validate_reassignment`, `set_partner_bidirectional` | Orphaned-partner queue (when a partner gets scratched). |
| `routes/registration.py:494` | `set_competitor_partner` (POST) | Manual partner edit on the college competitor detail page. |
| `routes/registration.py:998` | `auto_assign_pro_partners_route` (POST) | Wraps `services.partner_matching.auto_assign_pro_partners()`. |
| `services/partnered_axe.py` | `PartneredAxeThrow` class | Manages prelim → finals state machine for the partnered axe event. |
| `services/proam_relay.py` | `ProAmRelay` class | Manages the Pro-Am Relay lottery — gender-balanced team assembly, NOT a partnered-event pairing. |

### How partnerships are represented

Per CLAUDE.md §4 and Section 3 above:
- **Storage:** `competitor.partners` JSON dict (`event_id-string-or-event-name → partner_name string`). Keys may be stringified event IDs OR event display names; values are bare name strings (no team-code suffix).
- **Magic key:** `__pro_am_lottery_opt_in__` is overloaded into the same `partners` dict on `CollegeCompetitor` (lines 69, 127-142) — boolean preference smuggled through a partner JSON.
- **Result-row denormalization:** `EventResult.partner_name` carries the partner's display name as a string; written at heat generation time. Reporting reads from this denormalized field, not from the live partner JSON.
- **Heat-level:** Pair members occupy adjacent slots in `Heat.competitors` JSON list; `Heat.stand_assignments` typically gives both partners the same stand number (heat_generator.py line 226 comment).

**Partnerships are NOT first-class database entities.** No `Partnership` table exists. Partnerships are derived at runtime from string fields in `competitor.partners` JSON, cross-validated against `EventResult.partner_name` at finalization time. There is no FK linking the two sides of a pair.

### How partners are matched

Three resolution layers, each with slightly different rules:

**Layer 1 — `services.name_match.find_partner_match` (the shared ladder, 4 tiers):**

```
Tier 1: Exact normalized full-name match (lowercase + alphanumeric only).
Tier 2: First-token match — one match only, refuses on ambiguity.
Tier 3: Levenshtein ≤ 2 on full normalized name — one match only.
Tier 4: Levenshtein ≤ 2 on first-token only — runs ONLY if tier 3 found zero matches
        (catches "Mckinley" → "Mickinley Verhulst" where last name adds 8+ edits).
```

`enable_fuzzy=False` disables tiers 3 and 4. Refusal-on-ambiguity is preserved at every fuzzy tier.

**Layer 2 — `services.partner_matching` (auto-assignment):** Uses Layer 1 as the resolver, plus a three-phase orchestration:
- Phase 1 (CONFIRM): walk pool, fuzzy-resolve each comp's partner. Reciprocal → write canonical names on both sides, mark paired.
- Phase 2 (CLAIM): one-sided claims (A says B but B is blank or B says someone else) → both held in `needs_review`, NOT thrown into the auto-pair pool. Reasons recorded: `self_reference`, `unresolved`, `partner_already_paired`, `one_sided_claim`, `non_reciprocal`.
- Phase 3 (AUTO-PAIR): genuinely unclaimed pool gets paired. Mixed-gender events prefer M↔F pairings; same-gender events pair within gender. Truly unpairable returned as `unmatched`.

**Layer 3 — `services.partner_resolver.pair_competitors_for_heat` (heat sheet render):** Heat-local lookup with optional tournament-roster fallback (added 2026-04-23) for split pairs whose partners landed in different heats. Returns `partner_comp_id=None` when the pair is split across heats; uses the broader roster only for display.

### Conflicts the system does NOT currently detect

(Cross-referencing `services/preflight.py` BLOCKING_CODES list, `services/registration_import.py` warnings list, `services/partner_matching.py` summary keys, and `routes/scheduling/partners.py` validation.)

| Detected today | Where |
|---|---|
| Partner name does not match anyone in the event pool | Preflight (`unresolved_partner_name`) + registration_import (`UNREGISTERED`) + partner_matching (`reason='unresolved'`) |
| Self-reference (A typed A's own name) | Preflight (`self_reference_partner`) + partner_matching (`reason='self_reference'`) |
| Non-reciprocal (A says B, B says C) | Preflight (`non_reciprocal_partnership`) + registration_import (`NON-RECIPROCAL`) + partner_matching (`reason='non_reciprocal'`) |
| Same competitor as partner to two different people | Partner_matching (`reason='partner_already_paired'`) — hold for review |
| Odd-pool count for a partnered event | Preflight (`odd_partner_pool`) |
| Partner exists in the import data but is not enrolled in the same partnered event | Preflight scan finds the partner row; auto-pair won't pair them; surfaces as `non_reciprocal` |
| Mixed-gender requirement violated on reassignment | `routes/scheduling/partners.validate_reassignment` |
| Same-gender requirement violated on reassignment | Same |
| Partner already paired on the new-partner reassignment target | Same — but only checks if the existing partner is NOT scratched |

| **NOT detected** | What goes wrong |
|---|---|
| Partner exists in registration but is NOT registered for the event their listed partner registered for | Auto-assign pool only contains `_is_entered(event, ...)` competitors. A claimed partner who didn't sign up for the event simply doesn't exist in the pool — they get reported as `unresolved` with no hint of "they're in the tournament but didn't enter THIS event." |
| Cross-event partner conflict (A is paired with B for Jack & Jill AND with C for Double Buck where B is also entered in Double Buck without a listed partner) | No cross-event consistency check. Each partnered event resolves independently. |
| A listed partner whose row was scratched AFTER pairing — the pair survives in `Heat.competitors` and `EventResult.partner_name`, the orphaned-queue route only fires for partnered EVENTS the user remembers to visit | `routes/scheduling/partners.partner_queue` exists but is per-event; nothing broadcasts the orphan to the run-show dashboard. |
| Triangle: A↔B reciprocal in Jack & Jill, B↔A reciprocal in Double Buck, but B's gender violates the partnered-event requirement on one side | Gender requirement is checked only at reassignment time, not at the original auto-pair. Phase 3 pairing assumes gender filter is correct based on event-pool filter, not on the partner field. |
| Two competitors who fuzzy-match to the same name in the pool (e.g. "Alex" with two Alexes registered) | `find_partner_match` correctly refuses on ambiguity (returns None). The downstream consumer reports `unresolved`, but there is NO surfaced path that says "ambiguous — pick one" with a click-to-resolve UI. The operator sees `'PARTNER NOT FOUND: Alex'` and has to guess. |
| Mixed-form partner casing where one side wrote "Alex Kaper" and the other wrote "ALEX KAPER" with extra trailing whitespace inside the partners JSON written by an earlier import that stored unnormalized | Tier 1 normalization handles the comparison, but the WRITE path in some routes does not. `routes/registration.py:set_competitor_partner` (line 494) writes whatever the form sent, unnormalized. |
| Stale `partners` dict keys after an event is deleted/renamed | `partners` JSON dict contains stringified IDs of events that may no longer exist. No cleanup pass. Same risk on `gear_sharing`. |
| Partner ID drift on rename: `EventResult.partner_name` is a denormalized string set at heat generation. Renaming a competitor after heats are generated leaves stale partner names on every result row. | No detection. |
| Heat `competitors` JSON containing a competitor ID whose underlying row was deleted (cascade should prevent this, but Heat is per-event with cascade only via `Event` — direct deletion via `routes/registration.py:delete_college_competitor` does call `_remove_college_competitor_from_unfinished_heats` but this is best-effort and per-route) | No DB constraint; depends on every delete path remembering to clean heats. |

### Findings (Section 4)

1. **No first-class Partnership entity. Five different fuzzy resolvers all try to recover the same fact at runtime.** `name_match`, `partner_resolver`, `partner_matching`, `gear_sharing.resolve_partner_name`, `excel_io._fuzzy_match_member`. The fact that V2.14.16 had to introduce a `BLOCKING_CODES` constant in preflight + a Domain Conflict Review Board (`routes/domain_conflicts.py`) admits that this is structurally underspecified. Race-day risk: the same partner bug recurs with slight variations every event because there's no single source of truth for "what does it mean for A and B to be paired."
2. **Cross-event partner consistency is not validated anywhere.** Each partnered event resolves independently. A pro who lists "Jane Doe" as their Jack & Jill partner AND "Jane Doe" as their Double Buck partner — but Jane only signed up for Jack & Jill — fails one event silently and succeeds on the other with whoever Phase 3 auto-paired. Nothing surfaces this asymmetry.
3. **Partner reassignment after a scratch is per-event and per-route.** `routes/scheduling/partners.partner_queue` only shows orphans for ONE event at a time. The Run Show dashboard does not aggregate them. A scratch that orphans 4 competitors across 3 partnered events requires the operator to remember to visit each event's partner-queue page in turn.

### Amendment (dual-path pass)

- **V2.14.16 (Apr 27 18:44) shipped a 283-line rewrite of `services/partner_matching.py`** with the explicit Phase 1 (CONFIRM) / Phase 2 (CLAIM) / Phase 3 (AUTO-PAIR) structure. Decision_note from `docs/domain_conflicts.json` (`partner-unpaired-solo-vs-held-back`): *"BUILD A BETTER PARSER THAT IS ABLE TO FUCKING INTUIT WHO IS SIGNED UP FOR EVENTS ... BE SURE to write something that checks if someone else has already claimed a partner before you throw them into the unpaired pool."* The rewrite implements this. Race-day risk for the next event: this is brand-new code with one session of test coverage; field-test in a low-stakes context first.
- **The `BLOCKING_CODES` set in `services/preflight.py:25-29`** lists `unresolved_partner_name`, `self_reference_partner`, `non_reciprocal_partnership`, plus the heat-sync codes. These are the four conditions under which heat generation should refuse to run; surfaced via `report['blocking']` and a red banner on the preflight template. Anyone designing a portal must respect this set as the hard-blocker contract.

---

## SECTION 5 — GEAR SHARING LOGIC

### Functions and routes that handle gear

`services/gear_sharing.py` (1,892 lines, 35 top-level functions) is the gear core. Selected signatures:

| Symbol | Purpose |
|---|---|
| `parse_gear_sharing_details(details_text, event_pool, name_index, self_name, entered_event_names)` (line 491) | The big free-text parser. Returns `(parsed_dict, warnings_list)`. |
| `competitors_share_gear_for_event(comp1_name, comp1_gear, comp2_name, comp2_gear, event, all_events=None)` (line 666) | Heat-conflict check. Used by heat generator and validation. |
| `build_gear_conflict_pairs(tournament) -> dict[int, set[int]]` (line 1166) | Pre-compute competitor_id → set of conflict competitor_ids. Used by flight builder. |
| `build_gear_report(tournament) -> dict` (line 1378) | Comprehensive audit (verified pairs, unresolved, conflicts, groups). Used by gear-manager UI. |
| `build_gear_conflict_pairs(tournament)` + cascade pass | Penalty matrix for adjacency in the flight builder. |
| `build_gear_completeness_check(event, pro_comps)` (line 1135) | Per-event completeness audit. |
| `infer_equipment_categories(text) -> set[str]` (line 377) | Free-text → equipment category set. |
| `event_matches_gear_key(event, raw_key)` (line 459) | Match a `gear_sharing` JSON key (id, name, display_name, alias) to an Event row. |
| `_event_name_aliases(event)` (line 396), `_short_event_codes(event)` (line 432) | Build an alias set per event for matching. |
| `sync_gear_bidirectional(comp_a, comp_b, event_key)` (line 732) | Write reciprocal entry on both sides. |
| `sync_all_gear_for_competitor(comp, pro_comps_by_norm, old_gear)` (line 764) | Mirror entries to all partners; clear deletions. |
| `parse_all_gear_details(tournament)` (line 1626) | Re-parse `gear_sharing_details` text for every active pro who lacks structured `gear_sharing` data. |
| `fix_heat_gear_conflicts(tournament)` (line 1686) | Detect existing heat conflicts and attempt resolution by moving competitors. |
| `auto_populate_partners_from_gear(tournament)` (line 1004) | Use gear pairs to populate `partners` JSON. |
| `complete_one_sided_pairs(tournament)` (line 852) | Mirror unidirectional gear declarations. |
| `cleanup_scratched_gear_entries(tournament, ...)` (line 963) | Remove gear refs to scratched competitors. |
| `cleanup_non_enrolled_gear_entries(tournament)` (line 885) | Remove gear refs for events the comp is not enrolled in. |
| `create_gear_group(comps, event_key, group_name)` (line 811), `get_gear_groups(tournament)` (line 831) | Multi-pair gear groups. |
| `is_using_value(value)` (line 45), `strip_using_prefix(value)` (line 50), `_USING_VALUE_PREFIX = 'using:'` | Distinguish partnered-event gear CONFIRMATIONS from cross-comp SHARING constraints. |

Routes (all in `routes/registration.py`):

```
GET  /registration/<tid>/pro/gear-sharing                  pro_gear_manager
POST /registration/<tid>/pro/gear-sharing/parse            pro_gear_parse
POST /registration/<tid>/pro/gear-sharing/sync-heats       pro_gear_sync_heats
POST /registration/<tid>/pro/gear-sharing/update           pro_gear_update
POST /registration/<tid>/pro/gear-sharing/update-ajax      pro_gear_update_ajax
POST /registration/<tid>/pro/gear-sharing/remove           pro_gear_remove
POST /registration/<tid>/pro/gear-sharing/complete-pairs   pro_gear_complete_pairs
POST /registration/<tid>/pro/gear-sharing/cleanup-scratched  pro_gear_cleanup_scratched
POST /registration/<tid>/pro/gear-sharing/cleanup-non-enrolled  pro_gear_cleanup_non_enrolled
POST /registration/<tid>/pro/gear-sharing/auto-partners    pro_gear_auto_partners
GET  /registration/<tid>/pro/gear-sharing/parse-review     pro_gear_parse_review
POST /registration/<tid>/pro/gear-sharing/parse-confirm    pro_gear_parse_confirm
POST /registration/<tid>/pro/gear-sharing/group-create     pro_gear_group_create
POST /registration/<tid>/pro/gear-sharing/group-remove     pro_gear_group_remove
POST /registration/<tid>/college/gear-sharing/update       college_gear_update
POST /registration/<tid>/college/gear-sharing/update-ajax  college_gear_update_ajax
POST /registration/<tid>/college/gear-sharing/remove       college_gear_remove
GET  /registration/<tid>/pro/gear-sharing/print            pro_gear_print
```

Plus `auto_assign_pro_partners_route` at `/registration/<tid>/pro/auto-assign-partners` (line 998) which also touches gear via the partner inference step.

### Data model for gear / gear ownership / sharing

There is **no Gear table, no GearOwnership table, no GearGroup table.** Everything lives in:

- `CollegeCompetitor.gear_sharing` (Text JSON dict). Keys are stringified event IDs (or event names — same dual-form problem as `events_entered`). Values are partner name strings, or `'group:<groupname>'`, or `'using:<partner>'`.
- `ProCompetitor.gear_sharing` (Text JSON dict). Same structure. Plus magic keys `'category:crosscut'`, `'category:chainsaw'`, `'category:springboard'`, `'category:op_saw'`, `'category:cookie_stack'`, `'category:climbing'` (`gear_sharing.py:20-27`) when the parser identified a gear category but no specific event.
- `ProCompetitor.gear_sharing_details` (Text). The original raw textarea string from the form. Re-parsed on demand; never deleted.
- Gear "groups" are an emergent concept stored as `'group:<groupname>'` values across multiple competitors' `gear_sharing` JSON. There is no row representing the group itself — it exists only when multiple competitors happen to carry the same `'group:<name>'` value.

So yes — **gear is tracked only as free text on competitor JSON columns.** Quoting the `gear_sharing` field:

```python
# CollegeCompetitor
gear_sharing = db.Column(db.Text, nullable=False, default='{}')
# Dict: event_id -> partner sharing gear

# ProCompetitor
gear_sharing = db.Column(db.Text, nullable=False, default='{}')
# Dict: event_id -> partner name
```

Example values from the parsing/test code (paraphrased from `services/registration_import.py:227-262, 360-388, 405-530` and the gear-sharing test fixtures):

- `{"123": "Karson Wilson"}` — competitor 123 (event id) shared with Karson Wilson
- `{"123": "using:Karson Wilson"}` — partnered-event USING confirmation (NOT a heat constraint)
- `{"category:crosscut": "Cody Labahn"}` — gear category, no specific event
- `{"123": "group:hotsaw_a"}` — three+ comps share one hot saw, all carry the same group key
- The original `gear_sharing_details` example seen in tests: `"Sharing Cookie Stack saw with Cody Labahn"`, `"Me and Henry — springboard"`, `"(Cody & Owen) - Cookie Stack"`, `"Have saw, need partner"`, `"hotsaw, op, jack and jill — Toby"`.

### Does the scheduler consider gear conflicts?

YES — and the implementation exists in three places:

1. **Heat generation** — `services/heat_generator.py`:
   - `_has_gear_sharing_conflict(comp, heat_competitors, event)` line 1181, `_competitors_share_gear_for_event(comp1, comp2, event)` line 1189 — wrappers over `services.gear_sharing.competitors_share_gear_for_event`.
   - Checked in two snake-draft fallback passes (`_generate_standard_heats` line 519+, `_generate_springboard_heats` line 822+). The first pass refuses to place a competitor in a heat with a gear conflict; the second pass falls back and records the violation in a per-event `gear_violations` list. After generation, `routes` call `get_last_gear_violations(event_id)` to surface a flash to the judge.
   - `check_gear_sharing_conflicts(heats)` line 1213 — post-hoc audit returning a list of `{type: 'gear_sharing'}` conflict dicts.

2. **Flight builder** — `services/flight_builder.py:_calculate_heat_score` line 733 and `_score_ordering` line 626. Gear conflict pairs are pre-computed via `build_gear_conflict_pairs(tournament)` and passed to the placement scorer. A `GEAR_CONFLICT_PENALTY` (raised from `-30` per the gear audit 2026-04-07 — see CLAUDE.md V2.14.0 notes) deducts heavily from the score when two heats placed adjacent contain gear-sharing partners. The penalty is intended to OUTWEIGH spacing bonuses, so adjacency is exception, not norm.

3. **Heat-conflict auto-fix** — `services/gear_sharing.fix_heat_gear_conflicts(tournament)` line 1686. Multi-pass mover that walks pending/in-progress heats, finds gear conflicts, scores candidate target heats by capacity-minus-new-conflicts, and moves a competitor to the best target. Records un-fixable conflicts with structured suggestions.

So the answer to the literal question — "if Competitor A and Competitor B share a saw, can the scheduler currently put them in the same heat?" — is: **the scheduler tries hard not to**, but the fallback path WILL place them and emit a violation rather than refuse. The judge gets a flash message. The relevant check function is `services/heat_generator._has_gear_sharing_conflict` and yes, it exists and is called.

The system also has a NEGATIVE check (`is_using_value`) to prevent treating partnered-event gear confirmations (`"using:..."` prefix) as cross-competitor sharing constraints — this is what stops it from refusing to place a Jack & Jill pair in the same heat.

### Findings (Section 5)

1. **Gear is the only domain concept with multiple parallel storage formats.** Same logical fact stored as: (a) `gear_sharing` JSON entries keyed by event_id; (b) `gear_sharing` JSON entries keyed by `'category:<name>'`; (c) `gear_sharing` entries with `'group:<name>'` values pointing to nothing; (d) `gear_sharing_details` raw textarea from the form. Every gear operation has to handle all four forms. The `parse_all_gear_details` function exists to retroactively populate (a)/(b)/(c) from (d) on every import, because the import path can fail.
2. **The USING/SHARING distinction is correct but undocumented at the model level.** Two completely different domain meanings (partnered-event confirmation vs cross-competitor heat constraint) share one column and are distinguished by a string prefix on the value. Multiple call sites must call `is_using_value()` defensively. Missing the prefix check anywhere creates a phantom conflict that blocks Jack & Jill pairs from running together.
3. **Gear conflict resolution emits violations rather than refusing placement.** Race-day risk: the snake-draft fallback path produces a heat with gear-sharing competitors AND a flash message. If the operator misses the flash (race-day chaos), the heat runs and one team has no saw. The `fix_heat_gear_conflicts` route is opt-in via the gear manager — it does not run automatically after heat generation.

### Amendment (dual-path pass)

- **Gear sharing inside a Pro-Am Relay team is NOT tracked.** The relay does not generate Heat rows in the usual sense (a synthetic pseudo-Heat is rendered for display only). The gear-conflict checker is never called for the relay. If two teammates share a saw outside the relay, the heat builder will refuse to place them together in their REGULAR events — but inside the relay team they can be on the same partnered-sawing run with no objection.
- **The college xlsx path also runs gear-note inference at import via `services/excel_io._extract_gear_sharing_note` and `_apply_gear_sharing_note_to_team`** (lines 455-503). It scans "team groups" with no valid competitor names for free-text gear notes (looking for `'crosscut'`, `'gear'`, `'share'`) and applies them to the LAST real team — implicit position-based association, not explicit linkage. A captain who puts a gear note in the wrong row attaches it to the wrong team.

---

## SECTION 6 — SCHEDULING DEPENDENCIES

`scheduling.py` referenced at 2,018 lines no longer exists as a single file. The closest analogs are:

- `routes/scheduling/` package — 14 files, 6,571 LOC.
- `services/heat_generator.py` — 1,238 LOC.
- `services/flight_builder.py` — 1,525 LOC.
- `services/schedule_builder.py` — 557 LOC.
- `services/schedule_status.py` — 500 LOC.
- `services/schedule_generation.py` — 140 LOC.

Combined: ~10,431 LOC across 19 files. Five times the size of the old monolith.

### Imports in the scheduling boundary (consolidated)

```
services/heat_generator.py
  config, config.LIST_ONLY_EVENT_NAMES, config.event_rank_category
  database.db
  models {Event, EventResult, Heat, HeatAssignment}
  models.competitor {CollegeCompetitor, ProCompetitor}
  services.gear_sharing.competitors_share_gear_for_event

services/flight_builder.py
  json, logging, math, collections.defaultdict
  config.DAY_SPLIT_EVENT_NAMES
  database.db
  models {Event, Flight, Heat, HeatAssignment, Tournament}
  (delayed) services.gear_sharing.build_gear_conflict_pairs

services/schedule_builder.py
  config, config.{DAY_SPLIT_EVENT_NAMES, LIST_ONLY_EVENT_NAMES}
  models {Event, Flight, Tournament}

services/schedule_status.py
  config.LIST_ONLY_EVENT_NAMES
  database.db
  models.competitor {CollegeCompetitor, ProCompetitor}
  models.event.Event
  models.heat {Flight, Heat}
  models.tournament.Tournament
  flask.url_for
  (delayed) services.gear_sharing.build_gear_report

services/schedule_generation.py
  database.db
  models {Event, HeatAssignment, Tournament}
  (delayed) services.gear_sharing.{complete_one_sided_pairs, parse_all_gear_details}
  (delayed) services.partner_matching.auto_assign_pro_partners

routes/scheduling/__init__.py
  flask.Blueprint
  config, config.LIST_ONLY_EVENT_NAMES
  database.db
  models {Event, Flight, Heat, HeatAssignment, Tournament}
  models.competitor {CollegeCompetitor, ProCompetitor}
  + sub-module imports at end

routes/scheduling/heats.py
  flask {abort, flash, jsonify, redirect, render_template, request, url_for}
  flask_login.current_user
  config, strings as text
  database.db
  models {Event, EventResult, Heat, HeatAssignment, Tournament}
  models.competitor {CollegeCompetitor, ProCompetitor}
  services.audit.log_action
  services.cache_invalidation.invalidate_tournament_caches

routes/scheduling/flights.py
  flask {flash, jsonify, redirect, render_template, request, url_for}
  strings as text
  database.db
  models {Event, Flight, Heat, Tournament}
  models.competitor {CollegeCompetitor, ProCompetitor}
  services.audit.log_action
  services.background_jobs.submit

routes/scheduling/events.py
  json, os, re
  flask {flash, redirect, render_template, request, session, url_for}
  config, strings as text
  database.db
  models {Event, Flight, Heat, HeatAssignment, Tournament}
  models.competitor {CollegeCompetitor, ProCompetitor}
  services.audit.log_action
  services.cache_invalidation.invalidate_tournament_caches
```

### Functions in the scheduling boundary, with line counts (functions ≥100 lines flagged with `*`)

```
services/heat_generator.py
   295  L 104  *  generate_event_heats(event)
    83  L 519     _generate_standard_heats(...)
    87  L 675     _build_partner_units(...)
    85  L 822     _generate_springboard_heats(...)
    89  L 907     _place_group(...)  [nested helper inside springboard generator]
    80  L 1086    rebalance_stock_saw_solo_stands(event)
    68  L 399     _get_event_competitors(event)
    35  L 762     _sort_units_by_ability(units, event)
    32  L 634     _rebuild_pair_units(heat_competitors, event)
    31  L 1034    _competitor_entered_event(event, entered_events)
    26  L 608     _find_partner(partner_name, pool, self_comp)
    26  L 1213    check_gear_sharing_conflicts(heats)
    22  L 996     _generate_saw_heats(...)
    21  L 801     _get_partner_name_for_event(competitor, event)
    16  L 1189    _competitors_share_gear_for_event(comp1, comp2, event)
    15  L 1166    _get_tournament_events(event)
    14  L 505     _remap_violation_heat_indices(...)
    13  L 1069    _stand_numbers_for_event(event, max_per_heat, stand_config)
    12  L 1018    _advance_snake_index(...)
    9 misc helpers between 4 and 9 lines

services/flight_builder.py
   217  L 121  *  build_pro_flights(tournament, num_flights=None, commit=True, ...)
   150  L 733  *  _calculate_heat_score(competitors, competitor_last_heat, ...)
   148  L 1055 *  build_flight_audit_report(tournament)
   110  L 516  *  _single_pass_optimize(event_queues, event_id_order, ...)
   107  L 626  *  _score_ordering(ordered, heats_per_flight, ...)
   101  L 1391 *  integrate_proam_relay_into_final_flight(tournament, commit=True)
   100  L 1203 *  integrate_college_spillover_into_flights(...)
    88  L 1303    _event_order_key(ev)  [nested]
    76  L 440     _optimize_heat_order(all_heats, heats_per_flight=8, ...)
    73  L 42      get_last_lh_flight_warnings(tournament_id)
    72  L 883     optimize_flight_for_ability(flight, event)
    46  L 338     _prepare_partnered_axe_show_heats(event)
    42  L 1013    validate_competitor_spacing(tournament)
    29  L 955     insert_axe_throw_finals(tournament, top_teams)
    29  L 984     get_flight_summary(tournament)
    28  L 384     _get_partnered_axe_qualifier_pairs(event, count)
    20  L 412     _insert_partnered_axe_heats(flights, axe_heats)
    14  L 1492    class FlightBuilder
     6 helpers <10 lines

services/schedule_builder.py
    67  L 491     get_saturday_ordered_heats(tournament)
    48  L 37      build_day_schedule(...)
    42  L 241     _to_schedule_entries(events, start_slot=1)
    40  L 451     get_friday_ordered_heats(tournament)
    34  L 305     _college_friday_sort_key(event)
    31  L 110     _apply_friday_springboard_ordering(events)
    31  L 163     _build_saturday_from_flights(tournament, allowed_event_ids)
    28  L 213     _build_saturday_from_event_order(...)
    26  L 425     _add_mandatory_day_split_run2(schedule_entries, college_events)
    23  L 355     _college_name_rank(name)
    22  L 141     _build_saturday_show_block(...)
    22  L 15      _load_college_saturday_priority()
    20  L 378     _pro_name_rank(name)
    19  L 194     _append_college_spillover(...)
    13 helpers <20 lines

services/schedule_status.py
   112  L 154  *  _day_status(...)
   107  L 293  *  _build_warnings(...)
    63  L 82      build_schedule_status(tournament)
    30  L 471     _overall(...)
    27  L 444     _count_cookie_standing_simultaneous(tournament_id)
    23  L 270     _flight_stats(tournament_id)
    15 TypedDicts and helpers <15 lines

services/schedule_generation.py
    86  L 55      generate_tournament_schedule_artifacts(tournament_id)
    47  L 8       run_preflight_autofix(tournament, saturday_ids=None)
```

`routes/scheduling/__init__.py` (391 lines) — the package init exposes the `scheduling_bp` blueprint and shared helpers. It re-exports submodule routes via the bottom-of-file `from . import (events, heats, flights, ...)` pattern, then `from .events import _get_existing_event_config, _with_field_key`. Multiple module-level helpers live in `__init__`: `_resolve_partner_name`, `_get_event_payouts`, `_set_event_payouts`, `_build_pro_flights_if_possible`, `_generate_all_heats`, plus the cookie-stack/standing-block conflict detection.

### Which scheduling functions read free-text fields vs structured fields?

Free-text reads (specifically the JSON-encoded fields identified in Section 2):

| Reader | Reads | Source |
|---|---|---|
| `services/heat_generator._get_event_competitors()` | `comp.get_events_entered()`, `comp.get_gear_sharing()`, `comp.get_partners()` (via `_get_partner_name_for_event`) | All three JSON fields per competitor |
| `services/heat_generator._competitor_entered_event(event, entered_events)` | the `events_entered` list, with ID-then-name fallback | `competitor.events_entered` JSON |
| `services/heat_generator._get_partner_name_for_event(competitor, event)` | `competitor.partners` JSON dict, probes 5 key variants | `competitor.partners` |
| `services/heat_generator._has_gear_sharing_conflict(comp, heat_competitors, event)` | `comp['gear_sharing']` from the dict snapshot | `competitor.gear_sharing` |
| `services/flight_builder.build_pro_flights(...)` | indirectly via `build_gear_conflict_pairs(tournament)` which reads `gear_sharing` for every active pro | `competitor.gear_sharing` JSON |
| `services/flight_builder._calculate_heat_score(...)` | `gear_conflict_pairs` precomputed dict | derived from `gear_sharing` |
| `services/schedule_status._day_status` (line 211) | `comp.get_events_entered()` to count entries per event | `competitor.events_entered` JSON |
| `services/schedule_status._build_warnings` (line 361) | `services.gear_sharing.build_gear_report(tournament)` | reads all gear JSON |
| `services/schedule_generation` | `auto_assign_pro_partners(tournament)`, `parse_all_gear_details(tournament)`, `complete_one_sided_pairs(tournament)` | reads `partners`, `gear_sharing`, `gear_sharing_details` |
| `routes/scheduling/__init__._resolve_partner_name` | `competitor.get_partners()` with the 5-key probe | `competitor.partners` JSON |
| `routes/scheduling/heats.py` | event_results read+write, plus `Heat.competitors` JSON via `Heat.get_competitors()/set_competitors()` | `Heat.competitors`, `Heat.stand_assignments` |
| `routes/scheduling/flights.py` | `Heat.flight_id`, `Heat.flight_position`, `Tournament.schedule_config` | structured columns + `schedule_config` JSON |

Structured-only reads (no free-text JSON parsing):

| Reader | Reads |
|---|---|
| `services/schedule_builder.build_day_schedule(...)` | `Event.name`, `Event.gender`, `Event.event_type`, `Event.is_open`, `config.DAY_SPLIT_EVENT_NAMES`, `Tournament.schedule_config` (limited keys) |
| `services/flight_builder._optimize_heat_order(...)` | `Heat.event_id`, `Heat.heat_number`, `Heat.run_number`, `Heat.flight_id`, `Heat.flight_position` |
| `services/flight_builder.integrate_college_spillover_into_flights(...)` | `Event.name`, `Heat.run_number`, `Tournament.schedule_config['saturday_college_event_ids']` |
| `services/flight_builder.integrate_proam_relay_into_final_flight(...)` | `Event.event_state` JSON for relay state, no competitor JSON |
| `services/schedule_status._flight_stats(...)` | `Heat.flight_id`, `Heat.status` |
| `routes/scheduling/show_day.py` | `Heat.status`, `Heat.flight_id`, `Flight.status` (mostly structured) |
| `routes/scheduling/print_hub.py` | `print_trackers` table, `Event.id`, no JSON reads |

So the reads-from-free-text concentration is in `heat_generator`, `flight_builder` (via the gear_conflict pre-pass), `schedule_status` (warnings + entry counts), `schedule_generation` (preflight orchestration), and the `__init__` partner resolver. The schedule BUILDERS (`schedule_builder`, `print_hub`, `show_day`) are mostly structured-only.

### Functions over 100 lines (≥100 LOC) flagged

```
services/heat_generator.py
  generate_event_heats           (295 L)
services/flight_builder.py
  build_pro_flights              (217 L)
  _calculate_heat_score          (150 L)
  build_flight_audit_report      (148 L)
  _single_pass_optimize          (110 L)
  _score_ordering                (107 L)
  integrate_proam_relay_into_final_flight  (101 L)
  integrate_college_spillover_into_flights (100 L)
services/schedule_status.py
  _day_status                    (112 L)
  _build_warnings                (107 L)
```

10 functions ≥100 lines across the scheduling boundary. None in `schedule_builder` or `schedule_generation`. The longest single function is `heat_generator.generate_event_heats` at 295 lines.

### Findings (Section 6)

1. **`services/heat_generator.py` is the gravity well.** It reads ALL THREE free-text competitor JSON fields (`events_entered`, `partners`, `gear_sharing`) plus `Heat.competitors` and `Heat.stand_assignments`. `generate_event_heats` is 295 lines. Any registration/partner/gear data-model change ripples through this file's many helper functions. Race-day risk: it's also the function judges hit when they regenerate heats after a roster change, which is the single highest-stakes mutation operation on race day.
2. **`flight_builder.py` couples to gear via a precomputed conflict dict, but to partners only implicitly via `Heat.competitors`.** The flight builder treats heats as opaque competitor sets and relies on the heat generator to have already produced sane partnered units. So a partner-rebuild does NOT require touching flight builder — but a gear-data-model change DOES, because `build_gear_conflict_pairs` is the contract surface. Keep that contract intact in any rebuild.
3. **`schedule_builder.py` and most of `routes/scheduling/` are structured-only.** This is the cleanest code in the scheduling boundary and depends almost entirely on `Event` + `Heat` + `Flight` + `Tournament.schedule_config` columns. A rebuild of registration / partner / gear can leave these untouched IF the rebuild preserves the `Heat.competitors` JSON shape and `Event` row attributes. The fragile contracts are the JSON column shapes on Competitor and Heat.

### Amendment (dual-path pass)

- **Pro/college distinction is woven through 8 service-level paths** (full list in `docs/recon/dual_path_recon_2026_04_27.md` Section 6). Some are intentional (separate flight builder for pros, separate STRATHMARK push, college Saturday-spillover route), some are accidental (the relay opt-in column/property asymmetry). Any unification of `Competitor` into a single table will need to walk all 8 sites.
- **The single merge surface is `services/proam_relay.py`.** It pulls from two different storage shapes for `pro_am_lottery_opt_in` (column on pro, property over JSON on college). The merge code looks symmetric in the function bodies but the SQL paths are not (`get_eligible_pro_competitors` filters in SQL; `get_eligible_college_competitors` filters in Python). A new opt-in surface in either portal must respect both shapes.
- **V2.14.16 removed the pro/college gate on stock-saw stand mapping.** `_stand_numbers_for_event` and `_is_stock_saw` no longer branch on `event.event_type`. ALL stock saw uses stands 7-8 now (per `docs/domain_conflicts.json` `stock-saw-stands-pro-vs-college` decision). One less divergence point — but `_CONFLICTING_STANDS` in `flight_builder.py` had to add `stock_saw ↔ saw_hand` to compensate.

---

## SUMMARY OF RACE-DAY RISKS (cross-section synthesis)

The three highest-risk areas, ranked by what would break worst on the next race day:

1. **Partner-identity-as-string-with-five-resolvers.** Five different fuzzy ladders (`name_match`, `partner_resolver`, `partner_matching`, `gear_sharing.resolve_partner_name`, `excel_io._fuzzy_match_member`) all try to recover the same fact at runtime. The 2026-04-23 → 2026-04-27 patch series (V2.14.10 / V2.14.11 / V2.14.16) shows this pattern produces a recurring class of race-week bug. Any new pipeline must collapse these resolvers to one and pin partner identity to an ID.
2. **Gear-as-prefixed-strings-in-overloaded-JSON.** Same column stores partner names, `'group:<name>'` references, `'category:<name>'` references, AND `'using:<partner>'` partnered-event confirmations. Six places need to handle the prefix grammar correctly. Missing the `is_using_value` check anywhere produces phantom heat conflicts; missing the `'group:'` check produces incorrect adjacency penalties; missing the `'category:'` check loses cross-event sharing.
3. **Three duplicate event/fee mappings across three files.** `_EVENT_MAP` (pro_entry_importer.py), `_EVENT_FEES` (routes/import_routes.py), and `_canonicalize_event_name()` (excel_io.py) are independent string-to-string mappings that overlap but are not identical. Adding a new event silently breaks one of the three layers. The `Event` table itself is not the source of truth for event identity in the import path.

### Cross-section amendment (dual-path pass)

The dual-path recon surfaced a fourth class of race-day risk worth pinning here:

4. **The 2026 event ran code-frozen on V2.14.14 (Apr 23) and four user-reported bugs landed in V2.14.15 (Apr 27 12:18) — the de-facto post-event retro.** Stock saw solos stuck on stand 8 after cascade scratch (judges had to keep resetting the same stand), ability rankings silently wiped on save (operator re-entered them), birling bracket display corruption, and an unexplained "Placed N/total" panel that the operator had to write+SSH-deploy `scripts/diagnose_unplaced_competitors.py` to investigate. None blocked the show; all were live-event observations. The next deployment must close all four classes BEFORE freeze.

5. **V2.14.16 (Apr 27 18:44) shipped the Domain Conflict Review Board** (`routes/domain_conflicts.py`, `services/domain_conflicts.py`, `docs/domain_conflicts.json`) — the operator had captured 7 architectural conflicts between code, FlightLogic.md, and intuition. The `decision_note` fields are unfiltered post-event language. Six are now `implemented`. The portal rebuild must internalize the contract decisions (especially partner-pairing intuition and the stock-saw stands rule) as first-class invariants, not as another thing to patch later.

End of report.
