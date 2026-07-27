# Dual-Path Recon (Pro xlsx + College xlsx) — 2026-04-27

Read-only. No code changes, no commits, no branches.
Source tree: `Missoula-Pro-Am-Manager/` (current main, post-V2.14.16 — head `5937d3a`).
Companion to `docs/recon/registration_assignment_recon_2026_04_27.md`.

The 2026 Pro-Am ran April 24-25, 2026. There are **zero commits to main between April 23 21:18 and April 27 12:18** — the show ran on V2.14.14, code-frozen during the live event. All post-event activity (V2.14.15 + V2.14.16, plus the new `scripts/diagnose_unplaced_competitors.py` and the Domain Conflict Review Board) landed April 27 in two release commits.

---

## SECTION 1 — PRO INGESTION PATH (GOOGLE FORM)

### Module location

The April 10 mega-prompt registration import module is at `services/registration_import.py`. It is wrapped by the basic xlsx parser at `services/pro_entry_importer.py` and surfaced through routes at `routes/import_routes.py`.

```
services/
  registration_import.py    1073 lines  ← April-10 enhanced pipeline
  pro_entry_importer.py      347 lines  ← original Google-Forms xlsx parser
  gear_sharing.py           1892 lines  ← parser + matcher + heat-conflict + audit
  partner_matching.py        307 lines  ← V2.14.16 rewrite (3-bucket summary)
  partner_resolver.py        201 lines  ← heat-sheet render resolver
  name_match.py              175 lines  ← shared 4-tier matching ladder
routes/
  import_routes.py           481 lines  ← upload → review → confirm flow
  registration.py           1564 lines  ← also hosts /pro/new (manual entry) + 18 gear-mgr routes
```

### Top-level entry point — `run_import_pipeline`

`services/registration_import.py:655-716`:

```python
def run_import_pipeline(filepath: str) -> ImportResult:
    """Run the full import pipeline on an xlsx file.

    This wraps parse_pro_entries() and adds all validation/cross-validation.
    """
    result = ImportResult()
    try:
        from services.pro_entry_importer import parse_pro_entries
        raw_entries = parse_pro_entries(filepath)
    except Exception as exc:
        result.errors.append(f"Failed to parse xlsx: {exc}")
        return result
    if not raw_entries:
        result.errors.append("No entries found in file.")
        return result
    entries = _deduplicate(raw_entries, result)
    all_names = [str(e.get("name", "")).strip() for e in entries
                 if str(e.get("name", "")).strip()]
    name_index = _build_name_index(all_names)
    for row_num, entry in enumerate(entries, start=1):
        try:
            comp = _process_entry(entry, row_num, name_index, all_names, result)
            result.competitors.append(comp)
            for event_name in comp.events:
                result.event_signups.append((comp.full_name, event_name))
                warning = _check_gender_event(comp.gender, event_name)
                if warning:
                    result.warnings.append(f"{comp.full_name}: {warning}")
            for event_name, partner_name in comp.partners.items():
                result.partner_assignments.append(
                    (comp.full_name, event_name, partner_name)
                )
        except Exception as exc:
            result.errors.append(f"Row {row_num}: failed to process entry: {exc}")
    _validate_partner_reciprocity(result)
    _validate_gear_sharing(result, name_index)
    _infer_gear_from_partnerships(result)
    _reconcile_gear_flags(result)
    _check_unregistered_references(result)
    return result
```

### Sub-item presence check

#### a) 31-column Google Forms column mapper (substring matching) — PRESENT

The substring/prefix matcher lives in the basic parser (`services/pro_entry_importer.py:59-70`):

```python
def _find_column_index(stripped_headers: list[str], candidates: list[str]) -> int | None:
    """Find a header index by exact or contains-match against normalized candidates."""
    lowered = [str(h or '').strip().lower() for h in stripped_headers]
    normalized_candidates = [c.strip().lower() for c in candidates if c and c.strip()]

    for candidate in normalized_candidates:
        if candidate in lowered:
            return lowered.index(candidate)
    for idx, header in enumerate(lowered):
        if any(candidate in header for candidate in normalized_candidates):
            return idx
    return None
```

The actual column dictionary `_EVENT_MAP` (`pro_entry_importer.py:17-47`) has **27 entries**, not 31 — it covers the event checkboxes. The full Google Form has additional name/contact/waiver/relay/notes/gear columns probed by `_find_column_index` and direct `hmap.get(...)` lookups. Total distinct columns probed across `parse_pro_entries`: roughly 30-33 depending on how you count alias collapses (see Section 2 of the prior recon for the full table).

#### b) `ImportResult` dataclass — PRESENT

`services/registration_import.py:65-83`:

```python
@dataclass
class ImportResult:
    """Full result of the import pipeline."""
    competitors: list[CompetitorRecord] = field(default_factory=list)
    event_signups: list[tuple[str, str]] = field(default_factory=list)
    partner_assignments: list[tuple[str, str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    auto_resolved: list[str] = field(default_factory=list)
    inferred: list[str] = field(default_factory=list)
    fuzzy_matches: list[str] = field(default_factory=list)
    duplicates_removed: list[str] = field(default_factory=list)
    flag_overrides: list[str] = field(default_factory=list)
    unregistered_references: list[str] = field(default_factory=list)
```

Plus a sibling `CompetitorRecord` dataclass at lines 35-62. The `report_text()` method on `ImportResult` (lines 85-193) emits an 11-section plain-text report consumed by `routes/import_routes.py` and shown on the review page.

#### c) Gender-event cross-validation (Jack & Jill mixed-gender exception) — PARTIAL

The check itself exists at `services/registration_import.py:268-289`:

```python
_MALE_ONLY_EVENTS = {
    "Men's Underhand",
    "Men's Single Buck",
    "Men's Double Buck",
}
_FEMALE_ONLY_EVENTS = {
    "Women's Underhand",
    "Women's Standing Block",
    "Women's Single Buck",
}
# All other events are gender-neutral (springboard, hot saw, obstacle pole,
# speed climb, cookie stack, partnered axe throw, intermediate springboard,
# jack & jill, pro-am lottery)


def _check_gender_event(gender: str, event_name: str) -> str | None:
    if gender == "F" and event_name in _MALE_ONLY_EVENTS:
        return f"GENDER MISMATCH: Female competitor signed up for {event_name}"
    if gender == "M" and event_name in _FEMALE_ONLY_EVENTS:
        return f"GENDER MISMATCH: Male competitor signed up for {event_name}"
    return None
```

The "Jack & Jill mixed-gender exception" is implicit — Jack & Jill is in the comment as a gender-neutral event, so it never trips the check. There is **no positive validation that a Jack & Jill PARTNERSHIP is actually mixed-gender** at import time. The mixed-gender requirement is enforced only when reassigning partners after a scratch (`routes/scheduling/partners.validate_reassignment` lines 92-107) — not on the original import or auto-pair.

Also missing: `Women's Standing Block Speed`, `Women's Standing Block Hard Hit` are not in `_FEMALE_ONLY_EVENTS` (the set carries plain `"Women's Standing Block"` — but the canonical event names from `_EVENT_MAP` include both Speed and Hard Hit variants on some forms). Edge case worth checking against real form output.

#### d) Partner reciprocity check across all four partner columns — PRESENT

The four partner columns are mapped in `services/pro_entry_importer.py:50-54` (note: only THREE columns — Women's Double Buck partner is NOT in `_PARTNER_COLS`):

```python
_PARTNER_COLS = {
    "men's double buck partner name": "Men's Double Buck",
    "jack & jill partner name":       "Jack & Jill Sawing",
    "partnered axe throw 2":          "Partnered Axe Throw",
}
```

**Women's Double Buck partner column is absent.** The basic parser does not extract a `Women's Double Buck` partner from any form column — the canonical event name `Women's Double Buck` does exist in `_EVENT_MAP`, but no partner column maps to it. If the form has a Women's Double Buck partner column, its data is silently dropped at parse time.

The reciprocity check itself runs over whatever `entry['partners']` contains (`services/registration_import.py:841-877`):

```python
def _validate_partner_reciprocity(result: ImportResult):
    """Check that partnerships are reciprocal."""
    by_name: dict[str, CompetitorRecord] = {}
    for comp in result.competitors:
        by_name[comp.full_name.strip().lower()] = comp

    for comp in result.competitors:
        for event_name, partner_name in comp.partners.items():
            if partner_name == "NEEDS_PARTNER":
                continue
            partner_key = partner_name.strip().lower()
            partner_comp = by_name.get(partner_key)
            if partner_comp is None:
                result.warnings.append(
                    f"NON-RECIPROCAL: {comp.full_name} lists {partner_name} as "
                    f"{event_name} partner, but {partner_name} is not in the import data"
                )
                continue
            partner_partner = partner_comp.partners.get(event_name, "")
            if partner_partner == "NEEDS_PARTNER":
                continue
            if partner_partner:
                partner_partner_key = partner_partner.strip().lower()
                if partner_partner_key != comp.full_name.strip().lower():
                    result.warnings.append(
                        f"NON-RECIPROCAL: {comp.full_name} lists {partner_name} for "
                        f"{event_name}, but {partner_name} lists {partner_partner} instead"
                    )
```

So the check covers all events whose partner data made it through `_PARTNER_COLS`. Coverage gap: Women's Double Buck never enters this loop.

#### e) Equipment alias table — PRESENT

`services/registration_import.py:360-381`:

```python
_EQUIPMENT_ALIASES = {
    "crosscut": ("crosscut saw", None),
    "xcut": ("crosscut saw", None),
    "op": ("obstacle pole chainsaw", "Obstacle Pole"),
    "j&j": ("jack and jill saw", "Jack & Jill"),
    "hotsaw": ("hot saw", "Hot Saw"),
    "hot saw": ("hot saw", "Hot Saw"),
    "spring board": ("springboard", "Springboard"),
    "springboard": ("springboard", "Springboard"),
    "singlebuck": ("single buck saw", None),
    "single buck": ("single buck saw", None),
    "double buck": ("double buck saw", "Men's Double Buck"),
    "pole climb": ("spurs and rope", "Speed Climb"),
    "speed climb": ("spurs and rope", "Speed Climb"),
    "caulks": ("caulk boots", None),
    "corks": ("caulk boots", None),
    "spurs": ("climbing spurs", "Speed Climb"),
    "rope": ("climbing rope", "Speed Climb"),
    "cookie stack": ("cookie stack saw", "Cookie Stack"),
    "chainsaw": ("chainsaw", None),
    "saw": ("saw", None),
}
```

Independent equipment-category dictionary lives in `services/gear_sharing.py:20-27` (`_CATEGORY_KEYS`), which is the authoritative set of `category:*` keys written into the `gear_sharing` JSON column. The two are not unified — `_EQUIPMENT_ALIASES` is parser-internal; `_CATEGORY_KEYS` is storage-format-internal.

#### f) Bidirectional gear sharing inference (J&J partnership implies shared J&J saw) — PRESENT

`services/registration_import.py:905-940`:

```python
def _infer_gear_from_partnerships(result: ImportResult):
    """Infer gear sharing from partner assignments for paired events."""
    _PAIRED_GEAR = {
        "Jack & Jill Sawing": "Jack & Jill saw",
        "Men's Double Buck": "Double Buck saw",
        "Partnered Axe Throw": "axes",
    }
    by_name: dict[str, CompetitorRecord] = {}
    for comp in result.competitors:
        by_name[comp.full_name.strip().lower()] = comp

    for comp in result.competitors:
        for event_name, partner_name in comp.partners.items():
            if partner_name == "NEEDS_PARTNER":
                continue
            equipment = _PAIRED_GEAR.get(event_name)
            if not equipment:
                continue
            already_recorded = False
            for rec in comp.gear_sharing_records:
                partner_list = [p.strip().lower() for p in rec.get("partners", [])]
                if partner_name.strip().lower() in partner_list:
                    already_recorded = True
                    break
            if not already_recorded:
                result.inferred.append(
                    f"INFERRED: {comp.full_name} shares {equipment} with {partner_name} "
                    f"(inferred from {event_name} partner assignment)"
                )
```

Note: this only ADDS to `result.inferred` (a report line). It does not write to `comp.gear_sharing_records`. The actual write-to-DB inference happens in `_reconcile_gear_flags` (lines 943-979) which only flips the boolean `gear_sharing_flag` from No to Yes. The structured gear entries are NOT auto-written from partnerships at import time. They are written later by `services.gear_sharing.parse_all_gear_details(tournament)` and the gear-manager routes — but those run on the FREE TEXT field, not the inferred-from-partnership list.

So: Phase 3E inference exists at the REPORT level. The actual gear-sharing JSON column is not auto-populated from a J&J partnership unless the operator clicks `Auto-populate partners from gear` in the gear manager (`routes/registration.py:998 auto_assign_pro_partners_route` runs the inverse direction; the gear→partner direction is `services.gear_sharing.auto_populate_partners_from_gear` which the user invokes via `/registration/<tid>/pro/gear-sharing/auto-partners`).

#### g) Email-keyed idempotency — PRESENT (with caveats)

The COMMIT side keys on email (`routes/import_routes.py:259-264`):

```python
competitor = None
if entry.get('email'):
    competitor = ProCompetitor.query.filter_by(
        tournament_id=tournament_id,
        email=entry['email']
    ).first()
is_new = competitor is None
```

The DEDUP side (`services/registration_import.py:719-768`) also keys on email:

```python
def _deduplicate(entries: list[dict], result: ImportResult) -> list[dict]:
    """Remove duplicate entries, keeping the latest by timestamp per email."""
    by_email: dict[str, list[dict]] = {}
    no_email: list[dict] = []
    for entry in entries:
        email = str(entry.get("email") or "").strip().lower()
        if email:
            by_email.setdefault(email, []).append(entry)
        else:
            no_email.append(entry)
    deduped = list(no_email)
    for email, group in by_email.items():
        if len(group) == 1:
            deduped.append(group[0])
        else:
            def _ts_key(e):
                ts = e.get("submission_timestamp", "")
                if not ts:
                    return ""
                return str(ts)
            group.sort(key=_ts_key)
            kept = group[-1]
            deduped.append(kept)
            for dropped in group[:-1]:
                result.duplicates_removed.append(
                    f'DUPLICATE: {dropped.get("name")} ({email}) submitted ...'
                )
```

**Caveats:**

- Entries with NO email pass through `no_email` without dedup. Two manual-entry rows with no email would both create rows.
- `email` has no UNIQUE constraint at the DB level (`models/competitor.py:233`: `email = db.Column(db.String(200), nullable=True)` — no unique index). Two distinct tournaments share the same `email` namespace but the route filters by `tournament_id` so this is intentional. However, two entries within the same tournament with email mismatch (e.g., one row email `alex.kaper@gmail.com`, second row `alex.j.kaper@gmail.com`) WILL produce two competitors. Same-name detection emits a WARNING (`registration_import.py:760-766`) but does not deduplicate.

#### h) "Needs Partner" handling as flag, not name — PARTIAL

In the parsed `CompetitorRecord.partners` dict, the value is the literal string `"NEEDS_PARTNER"` (`registration_import.py:798-799`):

```python
if action == "needs_partner":
    partners[event_name] = "NEEDS_PARTNER"
    if label:
        result.auto_resolved.append(f"{name} ({event_name}): {label}")
```

But `to_entry_dicts()` (`registration_import.py:1024-1028`) drops the marker before commit:

```python
clean_partners = {}
for event_name, partner in comp.partners.items():
    if partner != "NEEDS_PARTNER":
        clean_partners[event_name] = partner
```

So at the database level, "Needs Partner" is represented as **the absence of an entry in the partners JSON**, not as a flag. There is no boolean column or sentinel string in `ProCompetitor.partners` distinguishing "we know this person needs a partner" from "we have no partner info yet." A downstream consumer reading the partner JSON sees the same shape for both cases.

**Implication:** Phase 3 of `services.partner_matching.auto_assign_event_partners` cannot tell the difference between "operator typed a real garbage value that meant Needs Partner" and "form was blank and the competitor never specified anyone." Both result in `_read_partner_name(comp, event)` returning `''`. Auto-pair treats both identically as part of the unclaimed pool. The `auto_resolved` log line on the import report is the only persistent record that the original was a NEEDS_PARTNER signal — and that report is written to a temp file deleted at confirm time (`routes/import_routes.py:444-447`).

#### i) Garbage pattern detection (?, Idk, Whoever, no oarnter, etc.) — PRESENT

`services/registration_import.py:200-262`:

```python
_NEEDS_PARTNER_PATTERNS = [
    (re.compile(r"^\s*\?\s*$"), '"?" -> Needs Partner'),
    (re.compile(r"^\s*(?:idk|IDK|Idk)\s*$", re.IGNORECASE), '"idk" -> Needs Partner'),
    (re.compile(r"^\s*(?:lookin|looking)\b", re.IGNORECASE), '"Looking" -> Needs Partner'),
    (re.compile(r"^\s*(?:whoever|anyone\s*available|anyone)\s*$", re.IGNORECASE),
     '"whoever/anyone" -> Needs Partner'),
    (re.compile(r"^\s*(?:no\s*(?:o?a?r?n?t?e?r?|partner)|none)\s*$", re.IGNORECASE),
     '"no partner/none" -> Needs Partner'),
    (re.compile(r"^\s*(?:need\s*partner|needs\s*partner)\s*$", re.IGNORECASE),
     '"Needs Partner" (normalized)'),
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
        return ("needs_partner",
                'AUTO-RESOLVED: "Have saw, need partner" -> Needs Partner (note: has equipment)')
    if _SPARE_PATTERN.search(val):
        return ("needs_partner",
                "AUTO-RESOLVED: spare request -> Needs Partner (note: available as spare)")
    if re.search(r"put\s+me\s+down", val, re.IGNORECASE):
        return ("needs_partner",
                f'AUTO-RESOLVED: "{val[:40]}..." -> Needs Partner (spare request)')
    return "name", None
```

The "no oarnter" typo from the requirements is matched by the deliberately-loose regex `(?:no\s*(?:o?a?r?n?t?e?r?|partner)|none)` — every letter of `partner` is optional, so `noo`, `noar`, `noart`, `noarter`, etc. all match. That's a dirty-but-deliberate compromise that may also accept real names that happen to start with `No`.

### Findings (Section 1) — ranked by impact on next deployment

1. **Women's Double Buck partner column is silently dropped.** `_PARTNER_COLS` (`pro_entry_importer.py:50-54`) lists only Men's Double Buck, Jack & Jill, and Partnered Axe Throw 2. If the form has a Women's Double Buck partner column, no row carries it forward. The reciprocity check, gear inference, and auto-pair phases never see Women's Double Buck partner data. Pure data-loss bug — discoverable only by inspecting the form output.
2. **"Needs Partner" is lossy at the storage layer.** Survives in `result.auto_resolved` (deleted with the temp file) but not in `ProCompetitor.partners`. Phase 3 auto-pair cannot distinguish "operator marked this as needs-partner" from "form was blank." Anyone marked NEEDS_PARTNER ends up paired with a random unclaimed competitor with no breadcrumb back to the original signal.
3. **Bidirectional gear inference (J&J → shared saw) only writes to a report, not to the DB.** The structured gear-sharing JSON is not populated from partnership inference at import. Operators must manually click `Auto-populate from gear` in the manager UI — gating real gear data on whether the operator remembered to push that button. Race-day risk: heat builder treats J&J pair as no-gear-conflict and may schedule them adjacent to another gear-sharing partnership with the same physical saw.

---

## SECTION 2 — COLLEGE INGESTION PATH (EXCEL SHEET)

### Module location

College ingestion lives in `services/excel_io.py` (1,122 lines, 35 functions). It is not a separate module — it shares the file with the (now-stub) pro Excel export. The pro xlsx pipeline does NOT touch this file.

Entry route: `POST /registration/<tid>/college/upload` → `routes/registration.py:104-168 upload_college_entry` → `services.excel_io.process_college_entry_form(filepath, tournament, original_filename)`.

### Entry point signature and docstring

`services/excel_io.py:13-27`:

```python
def process_college_entry_form(filepath: str, tournament: Tournament,
                               original_filename: str = None) -> dict:
    """
    Process a college entry form Excel file and import teams/competitors.

    Expected format (based on existing college entry form):
    - Sheet contains team and competitor information
    - Columns should include: Name, Gender, Events, Partners

    Args:
        filepath: Path to the Excel file
        tournament: Tournament to add teams/competitors to
        original_filename: Original uploaded filename (e.g., "University of Montana.xlsx")

    Returns:
        dict with counts: {'teams': int, 'competitors': int}
    """
```

### Expected Excel column structure

There is **no documented column schema** in code, in a constants file, or in tests. The expected structure is inferred at runtime by `_find_column()` (line 234) probing for one of several name candidates per logical column:

| Logical column | Candidate header strings probed |
|---|---|
| School | `school`, `university`, `college`, `institution` |
| Team | `team`, `team_code`, `team_id`, `team name` (fallback: column whose values match `^[A-Da-d]\s*[Tt]eam$`) |
| Name | `name`, `first and last name`, `competitor`, `athlete`, `full name`, `competitor name` |
| Gender | `gender`, `sex`, `m/f`, `male/female`, `male female`, `mf` |
| Events | `events`, `event`, `entered` |
| Pro-Am Relay Lottery | `pro-am relay lottery`, `pro am relay lottery`, `pro-am lottery`, `relay lottery` |
| Partner | `partner`, `partners`, `partner name` |
| Event-marker columns | any header containing one of: `horiz, vert, pole, climb, choker, saw, birling, kaber, caber, chop, buck, toss, hit, speed, axe, throw, pv, peavey, log roll, pulp, power, obstacle, single` |

There is no schema document; the parser is fully heuristic. The college entry form template that captains fill out is not stored in this repo.

### How college competitors link to school/team

`Team` (table `teams`) has `(tournament_id, team_code)` as the unique constraint. `CollegeCompetitor.team_id` is a foreign key to `teams.id`. Schools are NOT a separate table — `Team.school_name` (String 200) and `Team.school_abbreviation` (String 20) are columns on Team. `SchoolCaptain` (table `school_captains`) joins to teams **by school_name STRING, not FK**.

So the data path is:

```
Excel filename -> _school_name_from_filename() -> Team.school_name (string)
Excel "School" col -> overrides if present
Excel "A Team"/"B Team" identifier -> _extract_team_letter() -> Team.team_code = "{abbr}-{letter}"
                                                                where abbr = _abbreviate_school(school_name)
                                                                  via 28-entry hardcoded dict
Excel competitor row -> CollegeCompetitor with team_id = Team.id
```

A captain with the wrong filename or a typo in the School column produces a NEW Team row with a NEW team_code rather than attaching to the existing team. There is no fuzzy match on school name.

### How "Pro-Am Relay Eligible (Y/N)" is stored

Excel side: `relay_lottery_col = _find_column(df, ['pro-am relay lottery', 'pro am relay lottery', 'pro-am lottery', 'relay lottery'])` (`services/excel_io.py:69`). Per-row parse: `_parse_relay_opt_in(row.get(relay_lottery_col))` (line 155, helper at line 550) — returns True if the value lower-stripped is in `{'x', 'y', 'yes', '1', 'true', 't'}`.

Storage: written to the property `competitor.pro_am_lottery_opt_in = relay_opt_in` (lines 186 and 191).

But the `pro_am_lottery_opt_in` PROPERTY on `CollegeCompetitor` (`models/competitor.py:127-142`) is **not a real column**. It is a magic-key entry inside the `partners` JSON dict:

```python
_PRO_AM_LOTTERY_META_KEY = '__pro_am_lottery_opt_in__'

@property
def pro_am_lottery_opt_in(self) -> bool:
    """Return whether this college competitor opted into the Pro-Am relay lottery."""
    partners = self.get_partners()
    value = str(partners.get(self._PRO_AM_LOTTERY_META_KEY, '')).strip().lower()
    return value in {'true', '1', 'yes', 'y', 'x'}

@pro_am_lottery_opt_in.setter
def pro_am_lottery_opt_in(self, opted_in: bool):
    """Persist Pro-Am relay lottery preference in the partners metadata payload."""
    partners = self.get_partners()
    if opted_in:
        partners[self._PRO_AM_LOTTERY_META_KEY] = 'true'
    else:
        partners.pop(self._PRO_AM_LOTTERY_META_KEY, None)
    self.partners = json.dumps(partners)
```

So a college competitor's relay opt-in is smuggled into the `partners` JSON dict under a sentinel key. **`ProCompetitor.pro_am_lottery_opt_in` IS a real boolean column** (line 238), so the two populations have asymmetric storage for the same logical bit. Anyone querying `partners` JSON who doesn't filter the sentinel key will treat it as a fake event-id with a partner named `'true'`.

The relay service at `services/proam_relay.py:101-109` correctly uses the property (`college = [c for c in college if c.pro_am_lottery_opt_in]`), so the sentinel works at runtime — but it makes the data model harder to migrate, harder to query directly, and harder to expose through the (planned) self-service portals.

### Manual / one-off college scripts

`scripts/` contents:

```
__init__.py                          247 bytes  Apr 21 (init only)
diagnose_unplaced_competitors.py    6889 bytes  Apr 27 (read-only)
load_test_race_day.py             16613 bytes  Apr  6 (synthetic load gen)
profile_spectator_endpoint.py      5487 bytes  Apr  6 (perf profiler)
qa_print_hub.py                   15185 bytes  Apr 22 (read-only QA)
qa_solo_heat_placement.py         13954 bytes  Apr 23 (read-only QA)
repair_springboard_handedness.py   7275 bytes  Apr 21 (DATA WRITE)
smoke_test.py                      3192 bytes  Apr  6 (HTTP smoke)
```

The only **write** script in the last 60 days is `repair_springboard_handedness.py` (Apr 21) — it bulk-fixes `is_left_handed_springboard` flags lost by the importer, see `docs/solutions/logic-errors/...lh-springboard-bugs...`.

The Apr 27 `diagnose_unplaced_competitors.py` is read-only but its existence is a smoking gun — see Section 5.

There are no scripts dedicated to college imports. No college-specific seed loaders, no captain-roster syncs, no relay-eligibility patchers.

### Findings (Section 2) — ranked by impact on next deployment

1. **No formal college Excel schema. Header detection is fully heuristic.** A captain who renames the "School" column to "Institution" or shifts the "A Team" group key to "Team A" works only because the parser already covers those exact variants. New variants silently fail or produce wrong teams. There is no validation that the file actually came from "the Excel sheet maintained by team captains" — any xlsx with a name column is accepted.
2. **"Pro-Am Relay Eligible (Y/N)" for college lives as a magic key in the `partners` JSON.** `ProCompetitor.pro_am_lottery_opt_in` is a real Boolean column; `CollegeCompetitor.pro_am_lottery_opt_in` is a property masquerading as a column. The two populations require asymmetric SQL to filter on the same logical fact. The relay service handles it via the property, but any direct DB report or admin SQL query against `college_competitors` will need to JSON-decode `partners` and probe the sentinel.
3. **School identity is a string, not an entity.** `Team.school_name` is a free-text column; `SchoolCaptain.school_name` joins by string match. A typo in the captain's Excel filename creates a duplicate school. There is no canonical School table, no `_abbreviate_school` reverse map, and no roster-merge tool for the case where two captains accidentally use slightly different school names.

---

## SECTION 3 — DATA MODELS ACROSS BOTH POPULATIONS

(Detailed model definitions are in the prior recon `docs/recon/registration_assignment_recon_2026_04_27.md` Section 3. This section answers the new questions.)

### Pros and college: separate tables

`CollegeCompetitor` (table `college_competitors`) and `ProCompetitor` (table `pro_competitors`) are entirely separate ORM classes with separate tables. They share no inheritance relationship. There is **no discriminator column unifying them.**

The polymorphism happens downstream:
- `EventResult.competitor_id` is `Integer NOT FK`, with `EventResult.competitor_type` (`'college'` or `'pro'`) acting as the discriminator routing the resolver to the correct table.
- `HeatAssignment.competitor_id` follows the same pattern.
- `Heat.competitors` (JSON list of integer ids) does NOT carry a per-id type discriminator — heat type is inferred from `Heat.event.event_type`.

### Partnerships and gear sharing: NOT first-class entities

There is **no `Partnership` table.** There is **no `GearSharing` table.** Both are JSON dicts on the competitor row:

- `CollegeCompetitor.partners` (Text JSON dict, event_id → partner_name STRING).
- `CollegeCompetitor.gear_sharing` (Text JSON dict, event_id → partner_name STRING with magic prefixes).
- `ProCompetitor.partners` and `ProCompetitor.gear_sharing` — same shape.
- `ProCompetitor.gear_sharing_details` (Text — original raw form text, never deleted).

There are no foreign keys on either side of a partnership. Reciprocity is verified at runtime by walking both rows. Partner identity is recovered by 5 different fuzzy resolvers (see prior recon Section 4).

### Pro-Am Relay schema

There is **no `ProAmRelayLottery` table. There is no `ProAmRelayTeam` table.** The entire relay state is serialized as JSON into a single column on a special `Event` row.

Storage path:
- A row in `events` with `name='Pro-Am Relay'`, `event_type='pro'`, `is_partnered=True` is created lazily by `services/proam_relay.py:_save_relay_data` when the operator first uses the relay UI.
- Relay state is written to `Event.event_state` (Text, JSON) via `services/proam_relay.py:63-89`. Older code wrote to `Event.payouts` instead; the loader (`_load_relay_data`, lines 32-61) reads `event_state` first and falls back to `payouts`.
- The JSON shape:

```python
{
  'status': 'not_drawn'|'drawn'|'in_progress'|'completed',
  'teams': [
    {
      'team_number': int,
      'name': str,
      'pro_members': [{'id': int, 'name': str, 'gender': 'M'|'F'}, ...],
      'college_members': [{'id': int, 'name': str, 'gender': 'M'|'F', 'team': str}, ...],
      'events': {
        'partnered_sawing':       {'result': float|None, 'status': 'pending'|'completed'},
        'standing_butcher_block': {...},
        'underhand_butcher_block':{...},
        'team_axe_throw':         {...},
      },
      'total_time': float|None,
    },
    ...
  ],
  'eligible_pro':     [{'id', 'name', 'gender'}, ...],
  'eligible_college': [{'id', 'name', 'gender', 'team'}, ...],
  'drawn_pro':        [...flattened from teams...],
  'drawn_college':    [...flattened from teams...],
}
```

### How the 2+2+2+2 gender/population split is enforced

The constraint is enforced **only at draw time** in `services/proam_relay.py:130-225` (`run_lottery`). It is NOT enforced by any DB constraint, FK, check constraint, or model property. A direct write to `Event.event_state` could install a 4-pro-men, 4-college-women team and nothing would notice. The lottery code raises `ValueError` if the eligible pools are too small (`len(pro_male) < required_per_bucket`, etc.) and pops 2 from each of the 4 gender-population buckets per team.

`replace_competitor` (`services/proam_relay.py:443-512`) re-validates gender match (`if member.get('gender') != new_comp_data['gender']: raise ValueError`) and opt-in (`pro_am_lottery_opt_in=True` in the query filter) — but only for the single replaced slot. Bulk team modification via `set_teams_manually` (lines 316-441) does NOT enforce 2+2+2+2; the operator can post any team_assignments list and as long as IDs are valid the manual builder accepts it.

### How the lottery draw is recorded

The lottery itself is a single function: `services/proam_relay.py:130-225 run_lottery(num_teams=2)`. The draw mechanism:

```python
random.shuffle(pro_male)
random.shuffle(pro_female)
random.shuffle(college_male)
random.shuffle(college_female)

teams = []
for team_num in range(1, num_teams + 1):
    team = {...}
    team['pro_members'].append(pro_male.pop(0))
    team['pro_members'].append(pro_male.pop(0))
    team['pro_members'].append(pro_female.pop(0))
    team['pro_members'].append(pro_female.pop(0))
    team['college_members'].append(college_male.pop(0))
    team['college_members'].append(college_male.pop(0))
    team['college_members'].append(college_female.pop(0))
    team['college_members'].append(college_female.pop(0))
    random.shuffle(team['pro_members'])
    random.shuffle(team['college_members'])
    teams.append(team)

self.relay_data['status'] = 'drawn'
self.relay_data['teams'] = teams
...
self._save_relay_data()
```

**Random.** Not seeded. Not deterministic. Not auditable beyond the snapshot in `event_state`. Re-running produces a different draw. There is no draw history — `redraw_lottery` (line 227) overwrites the prior teams without preserving them.

### How partner pairings inside a Pro-Am Relay team are determined

For partnered sawing inside a relay team: there is **no per-event partner assignment.** The team has 4 pro members and 4 college members. Who pairs with whom for partnered sawing, who throws which axe at the team axe throw, etc., is decided by the team itself on the day. The data model holds only the bag of 8 IDs; nothing structures who-with-whom inside the team.

`team_axe_throw` and `partnered_sawing` are recorded as a single `result: float` per team per event in `team['events']`. There is no per-pair sub-result.

### Gear sharing inside a relay team — NOT TRACKED

There is no gear-sharing model for relay teams. The `gear_sharing` JSON on individual `ProCompetitor` and `CollegeCompetitor` rows is unaffected by relay assignment — competitors who share a saw outside the relay still share a saw, and the heat builder's gear-conflict check has no concept of relay teams. The relay event has no Heat rows in production usage either (it is rendered as a synthesized pseudo-Heat by `services/flight_builder.integrate_proam_relay_into_final_flight()` for display only — see CLAUDE.md V2.14.0 Phase 4 notes), so gear conflicts inside the relay never reach the heat-conflict checker.

### Findings (Section 3) — ranked by impact on next deployment

1. **2+2+2+2 enforcement is procedural, not structural.** The lottery happens to enforce it; `set_teams_manually` does not; a direct DB write could violate it. Any new portal that lets operators edit teams must re-implement the same procedural check or risk corrupting `event_state`.
2. **College `pro_am_lottery_opt_in` is a property hiding inside a JSON dict.** Pro is a column. The two paths look symmetric in service code but are structurally divergent. Migrating relay opt-in to a real column on `CollegeCompetitor` (or moving both to a unified table) is a prerequisite for any portal that exposes opt-in editing.
3. **Per-pair structure inside the relay team is undefined.** The model says "8 people, run 4 events." Nothing represents "Alex and Cody sawed double buck for Team 1, Bri and Jordan threw axes." A real-time scoring portal that needs per-pair results would have to invent the data model from scratch.

---

## SECTION 4 — PRO-AM RELAY MERGE LOGIC

### Function and signature

`services/proam_relay.py:130-225`:

```python
def run_lottery(self, num_teams: int = 2) -> dict:
    """
    Run the Pro-Am Relay lottery to create teams.

    Each team needs:
    - 2 pro men
    - 2 pro women
    - 2 college men
    - 2 college women

    Args:
        num_teams: Number of teams to create (default 2)

    Returns:
        Dict with lottery results
    """
```

Invoked from:
- `routes/proam_relay.py` (POST `/tournament/<tid>/proam-relay/draw`).
- `routes/proam_relay.py` (POST `/tournament/<tid>/proam-relay/redraw`) via `redraw_lottery`.

Eligible pool definitions (lines 91-109):

```python
def get_eligible_pro_competitors(self) -> list:
    """Get pro competitors who opted into the lottery."""
    pros = ProCompetitor.query.filter_by(
        tournament_id=self.tournament.id,
        status='active',
        pro_am_lottery_opt_in=True
    ).all()
    return [{'id': p.id, 'name': p.name, 'gender': p.gender} for p in pros]


def get_eligible_college_competitors(self) -> list:
    """Get active college competitors who opted into the relay lottery."""
    college = CollegeCompetitor.query.filter_by(
        tournament_id=self.tournament.id,
        status='active',
    ).all()
    college = [c for c in college if c.pro_am_lottery_opt_in]
    return [{'id': c.id, 'name': c.name, 'gender': c.gender,
             'team': c.team.team_code if c.team else 'N/A'} for c in college]
```

Note the pro filter happens in SQL (real column); the college filter happens in Python (property over JSON).

### Determinism

Random. `random.shuffle(...)` four times (one per gender-population bucket), then `random.shuffle(...)` again twice per team to randomize order within `pro_members` and `college_members` lists. Not seeded. The same eligible pool produces a different draw each call. No history table preserves prior draws — `redraw_lottery` (line 227-238) wipes `relay_data` and re-runs.

### 2+2+2+2 enforcement

Hard-enforced inside `run_lottery` (lines 156-204). The function pops exactly 2 from each of the 4 gender-population buckets per team. If any bucket is too small, raises `ValueError(f"Not enough pro men opted in. Need {required_per_bucket}, have {len(pro_male)}")` and the draw aborts.

NOT enforced by `set_teams_manually` (lines 316-441) — that function trusts whatever team_assignments list the caller passes, validating only that each ID is active + opted-in. An operator could post a 4-pro-men team and the function would accept it.

NOT enforced by `replace_competitor` (line 443) at the team-balance level — it only checks gender match for the single swap and that the new competitor is opted-in.

### Inside-team partner pairing for partnered sawing / team axe throw

Not determined by the system. There is no per-pair sub-structure inside a relay team. The team races as a unit; results are recorded per (team, event) as a single time. Who saws with whom is operator/team discretion on the day.

### Gear sharing inside a relay team

Not tracked. The relay does not generate Heat rows in the usual sense (a synthetic pseudo-Heat is rendered for the schedule display only — see CLAUDE.md §5 V2.14.0 Phase 4). The heat-conflict check `services/gear_sharing.competitors_share_gear_for_event` is not invoked for the relay. Gear conflicts within the bag of 8 are invisible to scheduling.

### Findings (Section 4) — ranked by impact on next deployment

1. **No draw audit trail.** A redraw silently overwrites the prior teams. If an operator redraws after a competitor objects, there is no way to reconstruct what the original draw was. For a prize-money event, this is risky.
2. **`set_teams_manually` does not enforce the 2+2+2+2 constraint.** The lottery is the only enforcement point. Any UI that lets the operator edit teams (or an attacker who POSTs to that endpoint with crafted IDs) can break the gender-population balance.
3. **No within-team structure for partnered sawing.** The model says "4 pro + 4 college" — it does not say "this pro-pro pair handles double buck, this college-pair handles team axe." A scoring portal that wants per-pair times would have to extend the schema.

---

## SECTION 5 — POST-EVENT REALITY CHECK

### Git log April 20-27, 2026

66 commits in the window. The full chronological list (most recent first):

```
5937d3a 2026-04-27 18:44 fix(V2.14.16): implement domain-conflict registry decisions + ship review board
9c60627 2026-04-27 12:18 fix(V2.14.15): bundle — ability rankings, placed breakdown, stock saw cascade,
                                          birling stale shape (#96)
8c622be 2026-04-23 13:16 fix(birling): compact non-power-of-two bracket + CI health + docs (V2.14.14) (#95)
322f5cf 2026-04-23 12:04 fix(heat_generator): Stock Saw solos alternate stands 7/8 (V2.14.13) (#94)
8e9e2a8 2026-04-23 11:42 fix(scheduling): lock Friday final-four event order (V2.14.12) (#93)
d54617b 2026-04-23 11:01 hotfix: pre-PEP-701 f-string compat for unpaired flash (#92)
07ce115 2026-04-23 10:44 fix(scheduling): partner pairing + persisted-config audit (V2.14.10) (#91)
c265687 2026-04-23 10:25 fix(birling): index hub + aligned bracket visualization (V2.14.9) (#90)
6f2d619 2026-04-23 08:31 fix(audit-sprint): 17 audit-sweep fixes (V2.14.8) (#89)
f876801 2026-04-23 08:20 fix(scheduling): filter pro ability rankings by event signup (v2.14.7) (#88)
86c6c17 2026-04-23 01:03 docs(solutions): compound V2.14.5 + V2.14.6 ship learnings (#87)
bbe59a5 2026-04-23 00:52 fix(run-show): warning panel CTAs actually generate (V2.14.6) (#86)
eafa69c 2026-04-23 00:43 fix(payouts): replace unsupported Jinja2 sort kwarg on payout templates (V2.14.5) (#85)
1b81dca 2026-04-23 00:20 fix: solo competitor closes the event, not opens it (V2.14.4) (#84)
970d172 2026-04-22 23:39 docs(solutions): compound sequential-ship pattern for parallel Claude sessions (#83)
d86c6ef 2026-04-22 23:15 docs(compound): 4-bug chain in GHA daily pg_dump backup workflow (#67)
279f0d2 2026-04-22 23:03 feat(relay): redraw accepts operator-chosen num_teams (V2.14.3) (#82)
68e78ca 2026-04-22 22:50 fix(scheduling): scope schedule-status warning to exclude list-only events (V2.14.2) (#81)
e476e12 2026-04-22 21:33 fix(scheduling): expose per-event stand count override on Friday Showcase page (V2.14.1) (#80)
922a9e4 2026-04-22 11:08 docs(solutions): ability-sort before resource-constraint spread pattern (#79)
7890b03 2026-04-22 10:54 feat(springboard): order LH cutters by ability before spread/overflow split (#78)
e3086a1 2026-04-22 10:45 test(flights): cross-phase integration — V2.14.0 Phases 1-5 on one tournament (#77)
ec88373 2026-04-22 10:36 docs(solutions): two test-tooling footguns from V2.14.0 ship (#76)
467985d 2026-04-22 10:29 docs(claude): add V2.14.0 flight-fixes features to §5.1 (#75)
df4feb3 2026-04-22 10:15 chore(V2.14.0): release — Flight Fixes 5-phase overhaul + codex hotfix (#74)
c554d48 2026-04-22 10:01 fix(relay): survive status transitions + render real team shape (#73)
23bf03c 2026-04-22 09:40 feat(springboard): LH cutter always on Stand 4 + flight contention flash (#72)
10149e5 2026-04-22 09:29 feat(relay): place Pro-Am Relay at end of final flight + teams sheet (#71)
425e7ea 2026-04-22 09:12 feat(flights): minutes-per-flight OR num_flights sizing toggle (#70)
c78875a 2026-04-22 08:57 fix(flights): DAY_SPLIT Run 2 routing + placement-mode toggle (#69)
14e888e 2026-04-22 08:39 fix(flights): async build chains spillover integration atomically (#68)
3cd2021 2026-04-22 08:17 docs(solutions): tests pass when mock signature matches buggy call site
d8bbeac 2026-04-22 01:29 chore(V2.13.0): bump hardcoded version strings in /health + /health/diag
b3a45c4 2026-04-22 01:27 docs: FLIGHT_FIXES_RECON
de89e00 2026-04-22 01:26 docs(solutions): flash-message Markup pattern + worktree-isolated ship workflow
6ce556e 2026-04-22 01:26 chore(gitignore): ignore SESSION_HANDOFF_*.md paste buffers
5cc23bd 2026-04-22 01:21 feat: print hub + pro checkout roster + per-event results + email delivery (#66)
fc1b483 2026-04-22 00:21 ops(backup): harden workflow + race-weekend hourly schedule (#65)
299b0f6 2026-04-21 23:39 feat(scheduling): Current Schedule status panel on events.html (#64)
825cfc3 2026-04-21 19:48 fix(ops): health-monitor workflow gh CLI needs GH_REPO without checkout (#63)
87f81cc 2026-04-21 19:37 ops: add daily pg_dump backup workflow (#62)
2a90737 2026-04-21 19:36 ops: add production /health monitor (5-min GitHub Actions cron) (#61)
8f6d623 2026-04-21 19:36 docs: compound learnings on test-to-prod isolation + Railway ops playbook (#60)
1fdda61 2026-04-21 19:14 docs: birling bracket wiring learning + recon refresh + CLAUDE.md update (#59)
49bfc9d 2026-04-21 19:05 feat: bundle — Russian i18n + ability-rankings dark-mode + gear-sharing USING-prefix fix (#58)
4f83632 2026-04-21 19:05 ship: gear-sharing preflight fix + i18n + birling UX + 6 solution docs (#56)
017eebc 2026-04-21 18:37 fix: clickable seed links in birling "not seeded" flash (V2.12.1) (#57)
e4e45a0 2026-04-21 18:27 fix+feat: flight stand-usage, rebuild chains spillover, competitor drag-drop (#55)
89f7f5b 2026-04-21 16:58 feat: V2.12.0 — even flight distribution + drag-drop + print polish (#54)
deef9df 2026-04-21 15:48 style(design): touch targets + print fonts + html font-family (V2.11.3) (#53)
4794616 2026-04-21 15:21 feat: surface + test Pro event fee configuration (V2.11.2) (#52)
48bd2a2 2026-04-21 13:41 feat: FNF PDF export via WeasyPrint (V2.11.1) (#51)
8245226 2026-04-21 13:36 docs: retire stale Excel-export gap note in CLAUDE.md (#50)
2e44498 2026-04-21 13:19 fix: null-handling tests flushed through NOT NULL columns (#49)
e4e5083 2026-04-21 13:08 fix: bump /health version string to 2.11.0 (#48)
41d25c7 2026-04-21 12:50 Saturday one-click Generate + Friday Night Feature schedule & print (#47)
b22d1ab 2026-04-21 12:13 fix: friendly redirect on expired CSRF tokens (V2.10.1) (#46)
a10f17d 2026-04-21 08:42 feat: hand-saw stand block alternation (V2.10.0) (#45)
8c8c164 2026-04-21 00:39 Add Video Judge Excel workbook export (#43)
cb9316a 2026-04-21 00:25 Add blank-bracket printable for birling + surface birling nav (#44)
372884d 2026-04-21 00:25 Extract partner_resolver service; dedupe three inline copies (#42)
5c5de2b 2026-04-21 00:21 Fix L/R springboard bugs: importer loses handedness, heat gen clusters,
                            flight builder unaware (#41)
f17306d 2026-04-20 23:22 Fix UTC helper on Python 3.10 (#40)
7e68b59 2026-04-20 22:51 Use timezone-safe UTC helpers (#38)
9fbc873 2026-04-20 22:29 Extract reporting backup workflows (#37)
2880d9f 2026-04-20 21:59 Extract reporting export workflows (#36)
44b5d71 2026-04-20 21:46 Extract database restore validation service (#35)
9fcd171 2026-04-20 21:23 Extract scoring workflows into a service (#34)
```

**Critical observation:** there are NO commits during the live event window April 24-25. Last pre-event commit was Apr 23 21:18 (V2.14.14). First post-event commit was Apr 27 12:18 (V2.14.15). The show ran on V2.14.14 and was code-frozen the entire weekend.

### Files modified or added in the window touching the rebuild domains

(De-duplicated from `git log --since="2026-04-20" --until="2026-04-27 23:59" --name-only`, filtered to registration / partner / gear / schedule / college / captain / relay / lottery.)

```
docs/GEAR_SHARING_AUDIT.md
docs/solutions/data-integrity/preflight-gear-sharing-using-prefix-false-positives-2026-04-21.md
docs/solutions/integration-issues/rebuild-flights-orphans-saturday-spillover-2026-04-21.md
docs/solutions/logic-errors/college-birling-bracket-nav-and-alignment-2026-04-23.md
docs/solutions/logic-errors/schedule-status-warning-false-positive-list-only-events-2026-04-22.md

routes/partnered_axe.py
routes/proam_relay.py
routes/registration.py
routes/scheduling/__init__.py
routes/scheduling/ability_rankings.py
routes/scheduling/birling.py
routes/scheduling/events.py
routes/scheduling/flights.py
routes/scheduling/friday_feature.py
routes/scheduling/heat_sheets.py
routes/scheduling/heats.py
routes/scheduling/preflight.py
routes/scheduling/print_hub.py
routes/scheduling/pro_checkout_roster.py

services/gear_sharing.py
services/partner_resolver.py
services/partner_matching.py            (V2.14.16 — full 283-line rewrite)
services/partnered_axe.py
services/proam_relay.py
services/schedule_builder.py
services/schedule_generation.py
services/schedule_status.py

templates/college/registration.html
templates/college/team_detail.html
templates/pro/gear_sharing.html
templates/proam_relay/dashboard.html
templates/scheduling/_email_modal.html
templates/scheduling/ability_rankings.html
templates/scheduling/birling_index.html
templates/scheduling/birling_manage.html
templates/scheduling/day_schedule_print.html
templates/scheduling/events.html
templates/scheduling/friday_feature.html
templates/scheduling/friday_feature_print.html
templates/scheduling/heat_sheets_print.html
templates/scheduling/preflight.html
templates/scheduling/print_hub.html
templates/scheduling/pro_checkout_roster_print.html
templates/scheduling/relay_teams_sheet_print.html

tests/test_partner_pairing_fixes.py
tests/test_partner_resolver.py
tests/test_proam_relay_placement.py
tests/test_proam_relay_redraw_route.py
tests/test_relay_lottery_realistic.py
tests/test_schedule_builder_ordering.py
tests/test_schedule_generation.py
tests/test_schedule_status.py
tests/test_schedule_status_breakdown.py
tests/test_partner_auto_assign_v2.py    (V2.14.16 — new file)
tests/test_heat_sync_invariants.py      (V2.14.16 — new file)
tests/test_stock_saw_solo_alternation_bug.py
tests/test_stock_saw_stand_rebalance.py
```

### Post-event documents

There is **no `post_event_*`, `race_day_*`, `2026-04-24*`, `2026-04-25*` document.** No retro doc was written for the live event. The `docs/PRE_DEPLOY_QA_2026-04-10.md` is the most recent named race-day doc — it predates the event by two weeks.

What DOES exist as race-week documentation:

| File | First 30 lines (quoted excerpt) |
|---|---|
| `docs/PRE_DEPLOY_QA_2026-04-10.md` | "Pre-Deployment QA Report — Date: 2026-04-10. Testers: Claude Code (automated) + adversarial route review (cross-model). Event: Missoula Pro-Am, April 24-25, 2026 (14 days out). Database state: 115 competitors (47 pro + 68 college), 45 events, 131 heats, 287 results, 10 teams, 1 tournament. ... The app is in solid shape for April 24. ..." |
| `docs/brainstorms/2026-04-12-race-day-integrity-requirements.md` | "Missoula Pro-Am Manager (V2.9.0) handles happy-path tournament operations but fails under race-day chaos. A 5-area recon audit found two systemic gaps: 1. Scratch cascade is missing. ... 2. Relay prize money has zero code." |
| `docs/plans/2026-04-12-001-feat-race-day-integrity-plan.md` | "Add a unified scratch cascade service, relay payout infrastructure, payout settlement tracking, and a race-day operations dashboard. Separates the overloaded Event.payouts field into dedicated event_state + payouts columns." |
| `docs/plans/2026-04-21-002-feat-print-hub-and-pro-checkout-roster.md` | (V2.13.0 plan — print hub + checkout roster) |
| `docs/GEAR_SHARING_AUDIT.md` | "Read-only audit of the gear-sharing pipeline ... Date: 2026-04-18. Scope: source-of-truth review for the bug 'OP Saw and Cookie Stack SHARING entries from the entry form are not appearing in the gear-sharing module' ..." |
| `docs/solutions/integration-issues/rebuild-flights-orphans-saturday-spillover-2026-04-21.md` | "Rebuild Flights Only silently orphans Saturday college spillover heats ... build_pro_flights() clears every Heat.flight_id to NULL as its first step (including heats previously integrated via integrate_college_spillover_into_flights()). The POST /flights/build route called build only — it never re-integrated spillover. Clicking 'Rebuild Flights Only' in the UI therefore silently orphaned every college spillover heat that had been integrated before." |
| `docs/solutions/data-integrity/preflight-gear-sharing-using-prefix-false-positives-2026-04-21.md` | "On live tournament 2, four days before race day, the preflight page showed 6 high-severity + 4 medium-severity issues, most of which were noise. Judges could not tell real problems from artifacts." |

The closest things to a post-event retro are the V2.14.15 commit body (Apr 27, 12:18 — explains four bugs THE OPERATOR SAW DURING THE EVENT) and V2.14.16 (Apr 27, 18:44 — implements the seven decisions in `docs/domain_conflicts.json`).

### V2.14.15 commit body — the de-facto post-event retro

(`git show 9c60627` summary, paraphrased from the commit message):

Four user-reported bugs:

1. **Ability rankings revert to alphabetical after save.** Form submission with empty `order_<cat>_<gender>` hidden inputs (the ordinary case where a user opens the page and saves) emitted `WHERE TRUE` and silently DELETED every rank in the category.
2. **"Placed: 37 / 64 competitors" panel without explanation.** The opaque "Placed: N / total" metric on `events.html` had no breakdown. Judges saw "27 missing" with no path to find them. Replaced with categorised buckets and `scripts/diagnose_unplaced_competitors.py` for ad-hoc DB introspection.
3. **Stock saw solos all stuck on stand 8 after cascade scratch.** V2.14.13 wired `rebalance_stock_saw_solo_stands` into 5 routes/scheduling sites but missed `services/scratch_cascade.py::execute_cascade` — the authoritative scratch path. After cascade, surviving solos stayed on whatever stand the scratched partner left them on, producing the **race-day printout of six straight heats on stand 8**.
4. **Birling bracket W1_8 stacking for 9 entrants.** V2.14.14 fixed the generator to produce compact non-power-of-two brackets (5 round-1 matches for N=9). But existing brackets persisted with the OLD shape; reading them back produced phantom W1_8 nodes the operator couldn't dismiss.

### V2.14.16 commit body — the second post-event retro

(`git show 5937d3a`, `routes/domain_conflicts.py`, `services/domain_conflicts.py`, `docs/domain_conflicts.json`).

The post-event retro was structured AS CODE — the operator's exhausted decision_notes are persisted verbatim in `docs/domain_conflicts.json`. Direct quotes from the live registry:

```
"BUILD A BETTER PARSER THAT IS ABLE TO FUCKING INTUIT WHO IS SIGNED UP FOR EVENTS.
 The majority of the issues I have seen though are shitty parsers (not recognizing
 first names or minor mis spellings) or weak pairings (ie partner A lists partner
 B but partner B doesnt list partner A and is still signed up for the event and
 not listed 'needs partner'."
   — partner-unpaired-solo-vs-held-back, severity:critical, status:implemented

"I dont know how many fucking times I have to say this. ALL stock saw will be done
 on stands 7 and 8"
   — stock-saw-stands-pro-vs-college, severity:high, status:implemented

"Yes we need to decide which ones [are] hard blockers and provide a solid way to
 rectify them with an easy click path. The majority of the issues I have seen
 though are shitty parsers (not recognizing first names or minor mis spellings) or
 weak pairings ... Most of the gear sharing issues come from weak issues on your
 end and not reading and fully understanding the GearSHaring markdown file."
   — preflight-warning-vs-generation-enforcement, severity:critical, status:implemented
```

Six of the seven conflicts were marked `implemented` on Apr 27. The seventh (`local-sqlite-vs-production-postgres`) is `accepted_contract` — ongoing.

### One-off scripts in the last 60 days

`scripts/` directory contents with mtimes:

```
diagnose_unplaced_competitors.py    Apr 27 — created in V2.14.15. Read-only DB introspection
                                            for "Placed: N / total" investigation. Direct
                                            SQL against pro_competitors / college_competitors /
                                            events / heats. Operator deployed it via Railway SSH
                                            during the post-event diagnosis.
qa_solo_heat_placement.py           Apr 23 — created during the immediate pre-event hardening
                                            window. QA harness for stock-saw solo placement.
qa_print_hub.py                     Apr 22 — created with the V2.13.0 print-hub release.
                                            Read-only QA scan.
repair_springboard_handedness.py    Apr 21 — DATA WRITE script. Bulk-fixes
                                            is_left_handed_springboard flags lost by the importer.
                                            See docs/solutions/logic-errors/...lh-springboard...
load_test_race_day.py               Apr  6 — synthetic load gen (older).
profile_spectator_endpoint.py       Apr  6 — perf profiler (older).
smoke_test.py                       Apr  6 — HTTP smoke (older).
```

The Apr 21 `repair_springboard_handedness.py` is the smoking gun for the importer dropping handedness data. The Apr 27 `diagnose_unplaced_competitors.py` is the smoking gun for "competitors who registered but never made it into a heat."

### TODO/FIXME/HACK in registration / scheduling / college code paths in the last 60 days

`grep -rn 'TODO\|FIXME\|HACK\|XXX' services/ routes/ models/` returns **zero matches** project-wide. Even narrative-heavy modules like `registration_import.py` (1,073 lines), `gear_sharing.py` (1,892 lines), and `partner_matching.py` (post-V2.14.16 rewrite) carry no tagged comments. The narrative IS in long inline comments and commit-body retros — there is no in-code marker that future work tracking could grep.

### Manual intervention summary

Every place a human had to step in to fix data the pipelines could not handle:

| Trigger | Manual fix surface | Evidence |
|---|---|---|
| Importer dropped Springboard L/R handedness on legacy form rows | `scripts/repair_springboard_handedness.py` (DB-write script run via Railway SSH) | Script body + `docs/solutions/logic-errors/...lh-springboard...` |
| Operator clicked "Rebuild Flights Only" → orphaned every Saturday college spillover heat | Re-clicked separate "Integrate Spillover" button (manual UI workaround). Code fix landed Apr 21 in V2.12.x | `docs/solutions/integration-issues/rebuild-flights-orphans-saturday-spillover-2026-04-21.md` |
| Preflight page reported 6 high + 4 medium false-positive gear warnings | None during the event — operator had to mentally filter true vs false. Code fix landed in V2.12.x | `docs/solutions/data-integrity/preflight-gear-sharing-using-prefix-false-positives-2026-04-21.md` |
| Race-day stock saw printout: six straight heats stuck on stand 8 after a cascade scratch | Operator continued the show with the wrong layout. Code fix landed Apr 27 (V2.14.15) | V2.14.15 commit body, item 3 |
| Ability rankings page silently wiped all ranks on save when user didn't drag-reorder | Operator had to re-enter rankings. Code fix landed Apr 27 (V2.14.15) | V2.14.15 commit body, item 1 |
| Birling bracket displayed phantom W1_8 nodes for 9-entrant brackets | Operator continued the show with the broken render. Code fix landed Apr 27 (V2.14.15) | V2.14.15 commit body, item 4 |
| "Placed: 37 / 64" panel — operator could not identify which 27 competitors were missing | Operator had to write+deploy `scripts/diagnose_unplaced_competitors.py` via Railway SSH to query the prod DB directly | Script body |
| Partner pairing typos (Mckinley/Mickinley, Elise/Eloise) failing reciprocity | Operator manually reassigned partners via the per-event partner queue. Code fix landed Apr 23 (V2.14.10) and again Apr 27 (V2.14.16, full 283-line rewrite) | V2.14.10 + V2.14.16 commit bodies |
| One-sided partner claims (A says B, B says nothing) | Operator manually accepted/rejected each pair via the partner manager. Phase 2 CLAIMS holding logic landed Apr 27 (V2.14.16) | `docs/domain_conflicts.json` — `partner-unpaired-solo-vs-held-back` decision_note |
| 7 architectural conflicts between code, FlightLogic.md, and operator preference | Operator captured each in a Domain Conflict Review Board and manually decided. 6 of 7 implemented Apr 27 | `docs/domain_conflicts.json`, `routes/domain_conflicts.py`, `templates/admin/domain_conflicts.html` |
| Pro entry form Q27 mismatch (Yes/No flag disagrees with text presence) | Surfaced as flash on import; operator manually reconciled per row in gear manager. Code parses both regardless | `routes/import_routes.py:328-360` Q27 inconsistency tracking |
| Stale `gear_sharing` keys pointing at events the competitor no longer entered | `cleanup_non_enrolled_gear_entries(tournament)` route. Operator clicks button | `services/gear_sharing.py:885-961` |
| Scratched competitor leaves dangling references in partners JSON / EventResult.partner_name | `cleanup_scratched_gear_entries`, `routes/scheduling/partners.partner_queue` per-event reassignment route. Operator visits per event | `services/gear_sharing.py:963-1003`, `routes/scheduling/partners.py` |
| College team imported with wrong filename → duplicate Team row created | Operator deletes the duplicate via `routes/registration.py:delete_college_team` | `routes/registration.py:277-310` |
| Partner field had unparseable garbage (`?`, `idk`, `whoever`, `no oarnter`) | Pipeline auto-resolves to `NEEDS_PARTNER` (which is then dropped at commit). Operator must remember to look at the import report (deleted post-confirm) to know who needs follow-up | `services/registration_import.py:200-262`, `routes/import_routes.py:444-447` |

### Findings (Section 5) — ranked by impact on next deployment

1. **Code-freeze during the live show worked, but immediate post-event deploys were a 4-bug bundle.** V2.14.15 (Apr 27 12:18) shipped four user-reported bugs from the event itself: stock saw solos stuck on one stand, ability rankings silently wiped on save, birling bracket display corruption, and the unexplained "Placed N/total" panel. None blocked the show; all were observed during it. The next deployment must close all four classes BEFORE freeze.
2. **Architectural disagreements between operator intuition, code, and `FlightLogic.md` accumulated until the operator built `routes/domain_conflicts.py` to capture them.** The decision_notes in `docs/domain_conflicts.json` are unfiltered post-event frustration ("BUILD A BETTER PARSER THAT IS ABLE TO FUCKING INTUIT WHO IS SIGNED UP FOR EVENTS"). The portal rebuild must internalize these as first-class invariants, not as another thing to patch later.
3. **Manual intervention surface is wide and entirely undocumented in code.** No TODO/FIXME tags exist anywhere. The list of fixes in the table above was reconstructed from commit messages, solution docs, and one-off scripts. Anything not committed (the operator's mental rollback of a corrupt birling display, the manual partner reassignments) leaves no trail. The next deployment needs a structured operator log.

### Manual intervention summary (recap)

| Category | Count of manual surfaces |
|---|---|
| One-off DB-write scripts run via SSH | 1 (springboard handedness) |
| Read-only diagnostic scripts run via SSH | 3 (diagnose_unplaced, qa_solo_heat, qa_print_hub) |
| Per-event manual reassignment routes (operator visits per partnered event) | 1 family (partner_queue) |
| Per-tournament cleanup routes triggered by button click | 4 (complete-pairs, cleanup-scratched, cleanup-non-enrolled, auto-partners) |
| Per-row import-report follow-ups (review report, then manually fix in gear manager) | unbounded — every row with a `NEEDS_PARTNER`, an `events_not_resolved`, a Q27 mismatch, or an unregistered reference |
| Domain decisions captured in JSON registry instead of code | 7 (six implemented, one accepted-contract) |
| Architectural rebuilds triggered by event observations | 1 (`partner_matching.py` 283-line rewrite in V2.14.16) |

The dual-portal rebuild must close every row in this table — every manual fix is a place where the operator was the integration layer.

---

## SECTION 6 — SCHEDULING DEPENDENCIES

(Detailed per-function line counts, imports, and structured-vs-free-text reads are in the prior recon `docs/recon/registration_assignment_recon_2026_04_27.md` Section 6. This section answers the new gear-conflict and pro/college-distinction questions.)

### Does the heat generator currently prevent two competitors who share a saw from being placed in the same heat?

**Yes — but with a fallback path that places them anyway and emits a warning rather than refusing.**

Primary check function: `services/heat_generator._has_gear_sharing_conflict` line 1181:

```python
def _has_gear_sharing_conflict(comp: dict, heat_competitors: list, event: Event) -> bool:
    return any(
        _competitors_share_gear_for_event(comp, other, event)
        for other in heat_competitors
    )
```

Wrapper at line 1189:

```python
def _competitors_share_gear_for_event(comp1: dict, comp2: dict, event: Event) -> bool:
    return competitors_share_gear_for_event(
        comp1.get('name', ''),
        comp1.get('gear_sharing', {}) or {},
        comp2.get('name', ''),
        comp2.get('gear_sharing', {}) or {},
        event,
    )
```

Called from `_generate_standard_heats` (line 519+) and `_generate_springboard_heats` (line 822+). Two passes:

1. **First pass:** refuses to place a competitor in a heat that already contains a gear-sharing partner (line 563): `not any(_has_gear_sharing_conflict(comp, heats[heat_idx], event) for comp in unit)`.
2. **Fallback pass:** if no clean placement found, places anyway and records the violation:

```python
if gear_violations is not None:
    for comp in unit:
        if _has_gear_sharing_conflict(comp, heats[heat_idx], event):
            gear_violations.append({...})
```

After generation the route flashes `get_last_gear_violations(event_id)` to the operator. The heat IS produced with the conflict — the operator gets a flash but the schedule is generated.

Flight-level gear adjacency check is `services/flight_builder._calculate_heat_score` (line 733) using a precomputed dict from `services/gear_sharing.build_gear_conflict_pairs(tournament)`. Penalty is severe enough to push gear-sharing pairs into different flight slots.

### Where the missing-check question would matter — checked

The `is_using_value` filter (`services/gear_sharing.py:45`) ensures partnered-event GEAR CONFIRMATIONS (`'using:'` prefix) are NOT treated as cross-competitor heat constraints. Without this filter, every Jack & Jill pair would be treated as a gear conflict and refused placement together — exactly the wrong outcome. So the system has BOTH a positive check (refuse cross-comp gear partners in the same heat) AND a negative override (don't refuse partnered-event gear-shared pairs).

### Does scheduling treat pros and college differently?

**Yes — in eight identifiable places:**

1. **Competitor pool query.** `services/heat_generator._get_event_competitors` line 414-423: `if event.event_type == 'college': all_comps = CollegeCompetitor.query.filter_by(...)` else `ProCompetitor.query.filter_by(...)`. Two SQL paths.

2. **Stock saw stand mapping (post-V2.14.16).** `services/heat_generator._stand_numbers_for_event` line 1069 and `_is_stock_saw` line 1082 used to gate on `event.event_type == 'college'`. V2.14.16 removed that gate — all stock saw now uses stands 7-8 regardless of population (per `docs/domain_conflicts.json` `stock-saw-stands-pro-vs-college`). The condition was VISIBLE in the code 24 hours ago.

3. **Flight builder.** `services/flight_builder.build_pro_flights` line 121 — pro-only. College has no flight system. College day runs as straight heat-by-heat (`services/schedule_builder.get_friday_ordered_heats` line 451).

4. **Saturday college spillover.** `services/flight_builder.integrate_college_spillover_into_flights` line 1203 — places select college Run 2 heats into the pro flight schedule. Pro/college boundary crossing only happens here.

5. **Schedule builder day blocks.** `services/schedule_builder.build_day_schedule` reads `Tournament.schedule_config` keys `friday_pro_event_ids` and `saturday_college_event_ids` to decide which events cross the day boundary.

6. **Schedule status warnings.** `services/schedule_status._build_warnings` line 293 separately scans pro and college events. List-only college events (Axe Throw, Caber Toss, Peavey Log Roll, Pulp Toss) and state-machine pro events (Partnered Axe Throw, Pro-Am Relay) are excluded via `LIST_ONLY_EVENT_NAMES` and `_STATE_MACHINE_PRO_NAMES`.

7. **STRATHMARK integration.** `services/strathmark_sync.push_pro_event_results` vs `push_college_event_results` — different result-push functions per population, different ID-resolution paths. Pro is enrolled at registration; college is name-matched against the global Supabase DB only when results are finalized.

8. **Pro-Am Relay merge point.** `services/proam_relay.py:91-109` — `get_eligible_pro_competitors` filters `ProCompetitor.pro_am_lottery_opt_in=True` in SQL; `get_eligible_college_competitors` filters `c.pro_am_lottery_opt_in` in Python (because it's a property over JSON). The single merge surface where the two paths cross.

### Findings (Section 6) — ranked by impact on next deployment

1. **Heat generator's gear-conflict fallback emits a violation rather than refusing placement.** Race-day risk: the operator misses the flash, the heat runs, one team has no saw. The `fix_heat_gear_conflicts` route is opt-in via the gear manager — it does not run automatically after heat generation. A portal-driven rebuild should make conflict refusal hard (block heat generation until resolved) or surface unresolved conflicts on the run-show dashboard, not in a flash.
2. **Pro/college distinction is woven through 8 service-level paths.** Some are intentional (separate flight builder for pros, separate result push for STRATHMARK) and some are accidental (the relay opt-in property/column asymmetry). Any unification of `Competitor` into a single table will need to walk all 8 sites and decide which to preserve as discriminator-based and which to collapse.
3. **The single merge point — `services/proam_relay.py` — pulls from two different storage shapes for the same logical fact (`pro_am_lottery_opt_in`).** Pro is a column; college is a property over a JSON dict. The merge code looks symmetric but is not. A new opt-in surface in either portal must respect both.

End of report.
