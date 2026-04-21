# Recon: Video Judge Excel Workbook + Birling Blank Bracket Print + Birling Nav Surfacing

Read-only audit of existing infrastructure relevant to three April 24–25 show-prep
deliverables. No code changes made.

Deliverables under recon:
1. **Video Judge Excel Workbook** — one .xlsx, tab per event, long format (row per
   competitor per run, row per partnered pair per run), two video-judge-time columns
   per row. Generated in show-prep.
2. **Birling Blank Bracket Print (seeded)** — printable PDF/HTML of the seeded
   double-elimination bracket with round-1 matchups populated and all advancement
   slots blank. Generated in show-prep.
3. **Birling Nav Surfacing** — a discoverable sidebar/nav path to the birling
   management page so a judge-in-training can find it without coaching.

---

## TASK 1 — Show-prep route surface

### Primary show-prep entry points

- **Tournament detail page** (`main.tournament_detail`) — route surfaces a
  "Ready for Game Day" action bar at
  [templates/tournament_detail.html:338-359](../templates/tournament_detail.html#L338-L359)
  with three pre-show export buttons:
  - `scheduling.heat_sheets` → Heat Sheets (print-friendly HTML)
  - `scoring.judge_sheets_all` → Print All Judge Sheets (PDF if WeasyPrint, else HTML)
  - `scheduling.preflight_check` → Preflight Check
- **"Before the Show" phase panel** at
  [templates/tournament_detail.html:430-501](../templates/tournament_detail.html#L430-L501)
  lists eight phase-link cards. Existing last card is "Print Heat Sheets"
  ([line 492](../templates/tournament_detail.html#L492)). A new "Video Judge
  Workbook" and "Birling Blank Bracket" card fit naturally in the same list.
- **Sidebar** ([templates/_sidebar.html](../templates/_sidebar.html)) has four
  show-prep-relevant rows under the "Setup" and "Run Show" groups. Heat Sheets
  is a `sidebar-child` under Run Show at
  [line 190](../templates/_sidebar.html#L190). No judge-sheet shortcut in the
  sidebar; nav for judge sheet lives only on tournament_detail and on the
  reports/all_results page banner.

### URL patterns (existing show-prep exports)

| Route | URL | File | Notes |
|---|---|---|---|
| `scheduling.heat_sheets` | `/scheduling/<tid>/heat-sheets` | [routes/scheduling/heat_sheets.py:193](../routes/scheduling/heat_sheets.py#L193) | Print-friendly HTML; renders `scheduling/heat_sheets_print.html` |
| `scoring.judge_sheet_for_event` | `/scoring/<tid>/event/<eid>/judge-sheet` | [routes/scoring.py:1540](../routes/scoring.py#L1540) | WeasyPrint PDF, HTML fallback |
| `scoring.judge_sheets_all` | `/scoring/<tid>/judge-sheets/all` | [routes/scoring.py:1556](../routes/scoring.py#L1556) | One doc, all events w/ heats |
| `scoring.heat_sheet_pdf` | `/scoring/<tid>/heat/<hid>/pdf` | (per CLAUDE.md) | Single-heat print PDF |
| `reporting.export_results` | `/reporting/<tid>/export-results` | [routes/reporting.py:273](../routes/reporting.py#L273) | **Direct sync .xlsx download** via `send_file()` — template for new xlsx routes |
| `reporting.export_results_async` | `/reporting/<tid>/export-results/async` | [routes/reporting.py:340](../routes/reporting.py#L340) | Background job + status page |
| `reporting.export_chopping_results` | `/reporting/<tid>/export-chopping` | [routes/reporting.py:299](../routes/reporting.py#L299) | Scoped .xlsx, json alt |
| `reporting.ala_membership_report` | sidebar group "Show Entries" | [templates/_sidebar.html:112](../templates/_sidebar.html#L112) | |

### Best fit for new buttons
- **Video Judge Workbook**: most natural next to "Print All Judge Sheets" on the
  tournament_detail action bar ([line 343](../templates/tournament_detail.html#L343))
  and as a `phase-link` card in the Before-the-Show panel. Also add a sidebar
  entry under "Run Show" alongside Heat Sheets.
- **Birling Blank Bracket**: two places. (a) Action button on the per-event
  bracket management page (`scheduling.birling_manage`) — natural location
  because it's specific to a single birling event (men's or women's).
  (b) A phase-link card in Before-the-Show on tournament_detail.

### No dedicated "Reports/Exports" section exists

No single "Reports" dashboard page. Exports are scattered across
tournament_detail buttons, the sidebar "Show Entries" and "Results" groups,
and the bottom of individual report screens (all_results, payout_summary,
fee_tracker). If cleanup is desired long term, a new Reports hub could unify
these — out of scope for this recon.

---

## TASK 2 — Existing judge sheet infrastructure (mirror target)

### `get_event_heats_for_judging(event_id: int) -> JudgeSheetData | None`

File: [services/judge_sheet.py:64](../services/judge_sheet.py#L64)

TypedDict shape:
```python
JudgeSheetData = {
    "event_name": str,              # event.display_name
    "event_type": str,              # stand_type or event name
    "num_runs": int,                # 3 if requires_triple_runs, 2 if dual, else 1
    "scoring_type": str,            # 'timed' or 'scored' (flattened)
    "heats": [
        {
            "heat_number": int,
            "competitors": [
                {"name": str, "team_code": str | None},
                ...
            ],
        },
        ...
    ],
}
```

- **Returns None** if event not found; empty `heats` list is legal.
- **Filters out run 2** — only `Heat.run_number != 2` is returned.
  ([line 86-91](../services/judge_sheet.py#L86-L91)) The judge sheet uses
  multi-column layout per heat (two timer cols × num_runs), not separate sheets
  per run.
- **Batched competitor lookup** — single `.in_()` per type, no N+1.
- **College team_code** populated via `comp.team.team_code` if available.
- **`num_runs` derivation** — `_num_runs_for_event()` at
  [line 49](../services/judge_sheet.py#L49): `triple_runs → 3`,
  `dual_runs → 2`, else `1`.
- **`scoring_type` flattening** — `_sheet_scoring_type()` at
  [line 57](../services/judge_sheet.py#L57): `'time'` or `'distance'` → `'timed'`;
  everything else → `'scored'`.

### Judge sheet routes

| Route | URL | Function |
|---|---|---|
| Single event | `/scoring/<tid>/event/<eid>/judge-sheet` | `judge_sheet_for_event()` at [routes/scoring.py:1540](../routes/scoring.py#L1540) |
| All events | `/scoring/<tid>/judge-sheets/all` | `judge_sheets_all()` at [routes/scoring.py:1556](../routes/scoring.py#L1556) |

Response built via shared helpers
[`_render_judge_sheet_html()` / `_judge_sheet_response()`](../routes/scoring.py#L1517-L1537):
WeasyPrint PDF if installed, plain HTML fallback; `Content-Disposition`
filename built via `_safe_filename_part()` which strips non-alphanumeric
characters. Filename pattern: `judge_sheet_<Event_Display_Name>.pdf`
or `judge_sheets_tournament_<tid>.pdf`.

### Judge sheet template

File: [templates/scoring/judge_sheet.html](../templates/scoring/judge_sheet.html)

- **Standalone HTML** (does NOT extend base.html) — WeasyPrint requires inline CSS.
- `@page` declaration at [line 8](../templates/scoring/judge_sheet.html#L8):
  `Letter landscape if num_runs > 1 else Letter portrait`, 0.5in/0.4in margins,
  bottom-center page counter + "STRATHEX Tournament Management" footer.
- **Column layout** for N runs: one name col + (N × 2 timer/judge cols) + status
  col + reason col. Dual-run events use 2×2 = 4 timer/judge columns.
- **Header row structure** at [lines 131-151](../templates/scoring/judge_sheet.html#L131-L151):
  `<th rowspan="2">Competitor</th>` then per-run `<th colspan="2">Run N</th>`
  over `Timer 1 / Timer 2` (if timed) or `Judge 1 / Judge 2` (if scored).
- **Data rows** at [lines 153-164](../templates/scoring/judge_sheet.html#L153-L164):
  competitor name + team_code, then blank `.blank-cell` (0.62in tall) for each
  timer/judge column, then status + reason blanks.

### Partner handling gap

`get_event_heats_for_judging()` does NOT resolve partners. Each competitor in a
partnered event heat shows up as a separate row with just their own name. For
Jack & Jill / Double Buck / Peavey / Pulp / Partnered Axe, the judge sheet
today shows each competitor separately — the PDF does not render
"Smith / Jones" pairs. This is a pre-existing gap, not created by the VJ spec.

The **partner resolution pattern** for name-pair rendering already exists in
four places, all using the same helper chain:
- [routes/scheduling/__init__.py:130](../routes/scheduling/__init__.py#L130) —
  `_resolve_partner_name(competitor, event)` returns partner name string
  from `competitor.get_partners()` dict, keyed by event id / name / display_name.
- [routes/scheduling/__init__.py:148](../routes/scheduling/__init__.py#L148) —
  `_build_signup_rows(event)` pairs competitors using `_resolve_partner_name()`
  + a by-name index; renders as `"{comp.name} + {partner.name}"`.
- [routes/scheduling/heat_sheets.py:147](../routes/scheduling/heat_sheets.py#L147) —
  `_serialize_heat_detail()` renders `"{name} & {partner_label}"` and `consumed`-
  tracks IDs to avoid duplicating partnered pairs across rows.
- [routes/scheduling/heat_sheets.py:218](../routes/scheduling/heat_sheets.py#L218) —
  same consumed-set / `_lookup_partner_cid()` pattern inside
  `heat_sheets()` route, with first-name fuzzy fallback.

**Partner storage** (CLAUDE.md section 4): `ProCompetitor.partners` and
`CollegeCompetitor.partners` are JSON dict columns keyed `event_id → partner_name`
(strings, not FKs). There is NO `Partnership` / `Pair` model and no `partner_id`
FK. Partner resolution is always name-matching across the active-competitor pool.
This means the VJ workbook builder needs to mirror the `_lookup_partner_cid()`
+ `consumed` pattern from heat_sheets.py to emit one row per pair.

### Data adequacy for the VJ workbook

`get_event_heats_for_judging()` returns enough for the **simple** (non-partnered)
case: heat_number, competitor name, team_code, num_runs. For partnered events
the builder must either (a) extend `get_event_heats_for_judging()` with partner
resolution or (b) bypass the helper and walk heats directly, reusing
`_resolve_partner_name()` + `_lookup_partner_cid()` from the heat-sheets module.
Option (b) is closer to the established pattern and does not modify the
existing judge sheet output.

Also missing from the helper for the VJ spec: **division flag** (college vs pro)
for the sheet name / tab title, and **Birling exclusion** (see Task 4).

---

## TASK 3 — Excel library status

### Already installed

[requirements.txt](../requirements.txt):
- `pandas==2.1.3`
- `openpyxl==3.1.2`

CLAUDE.md Tech Stack table (section 2): "Excel I/O — pandas 2.1, openpyxl 3.1".

### Existing .xlsx emission paths (all via openpyxl through pandas)

- [services/excel_io.py:1054](../services/excel_io.py#L1054) —
  `export_results_to_excel(tournament, filepath)`: uses
  `pd.ExcelWriter(filepath, engine='openpyxl')` and writes per-event sheets via
  `pd.DataFrame(...).to_excel(writer, sheet_name=..., index=False)`. Sheets:
  Team Standings, Bull of Woods, Belle of Woods, one sheet per event, Overview.
- [services/handicap_export.py:62](../services/handicap_export.py#L62) —
  `export_chopping_results_to_excel(tournament, filepath)`: single sheet
  "Chopping Results", same pattern.

Both route handlers use the same `tempfile.mkstemp(..., suffix='.xlsx')` +
`send_file(path, as_attachment=True, download_name=...)` +
`@after_this_request` cleanup pattern ([routes/reporting.py:277-296](../routes/reporting.py#L277-L296)).

### Nothing to install

Both pandas+openpyxl and the tempfile/send_file download pattern are in place.
The VJ workbook can be assembled with the same libraries and the same route
response pattern as `export_results`. If deeper openpyxl control is needed
(column widths, freeze panes, protected cells, header formatting) that's
available directly on openpyxl Workbook objects — pandas to_excel writes
through openpyxl, and the writer's `book`/`sheets` attributes expose the
underlying openpyxl objects.

---

## TASK 4 — Configured events (VJ workbook tab list)

Source of truth: [config.py:380-429](../config.py#L380-L429). Events are
defined in three dicts and instantiated as `Event` rows per tournament.

### COLLEGE_OPEN_EVENTS (come-and-go Friday, `is_open=True`)

| Name | scoring_type | runs | partnered? | Birling/bracket? |
|---|---|---|---|---|
| Axe Throw | score (triple) | 3 | no | no |
| Peavey Log Roll | time | 1 | yes (mixed) | no |
| Caber Toss | distance (dual) | 2 | no | no |
| Pulp Toss | time | 1 | yes (mixed) | no |

### COLLEGE_CLOSED_EVENTS

| Name | scoring_type | runs | partnered? | Gendered? | Birling/bracket? |
|---|---|---|---|---|---|
| Underhand Hard Hit | hits | 1 | no | M/F | no |
| Underhand Speed | time | 1 | no | M/F | no |
| Standing Block Hard Hit | hits | 1 | no | M/F | no |
| Standing Block Speed | time | 1 | no | M/F | no |
| Single Buck | time | 1 | no | M/F | no |
| Double Buck | time | 1 | **yes (same)** | M/F | no |
| Jack & Jill Sawing | time | 1 | **yes (mixed)** | no | no |
| Stock Saw | time | 1 | no | M/F | no |
| Speed Climb | time (dual, day-split) | 2 | no | M/F | no |
| Obstacle Pole | time | 1 | no | M/F | no |
| Chokerman's Race | time (dual, day-split) | 2 | no | M/F | no |
| **Birling** | **bracket** | n/a | no | M/F | **YES** |
| 1-Board Springboard | time | 1 | no | M/F | no |

### PRO_EVENTS

| Name | scoring_type | runs | partnered? | Gendered? | Birling? |
|---|---|---|---|---|---|
| Springboard | time | 1 | no | no | no |
| Pro 1-Board | time | 1 | no | no | no |
| 3-Board Jigger | time | 1 | no | no | no |
| Underhand | time | 1 | no | M/F | no |
| Standing Block | time | 1 | no | M/F | no |
| Stock Saw | time | 1 | no | M/F | no |
| Hot Saw | time | 1 | no | no | no |
| Single Buck | time | 1 | no | M/F | no |
| Double Buck | time | 1 | **yes** | M/F | no |
| Jack & Jill Sawing | time | 1 | **yes (mixed)** | no | no |
| Partnered Axe Throw | score (triple) | 3 | **yes** | no | no |
| Obstacle Pole | time | 1 | no | no | no |
| Pole Climb | time | 1 | no | no | no |
| Cookie Stack | time | 1 | no | no | no |

### Birling flag for VJ export

**Primary skip flag: `event.scoring_type == 'bracket'`**.

- Only Birling has `scoring_type='bracket'` ([config.py:407](../config.py#L407)).
- No pro birling exists in `PRO_EVENTS` (confirmed — CLAUDE.md section 3
  explicitly notes pro birling removed per 2026-01-25 changelog).
- The same predicate is used in existing code:
  [routes/scheduling/heat_sheets.py:313](../routes/scheduling/heat_sheets.py#L313)
  branches on `event.scoring_type == 'bracket'` to render birling bracket
  separately from heat cards; and the `birling_bp` routes gate on the same
  check ([routes/scheduling/birling.py:23](../routes/scheduling/birling.py#L23)).

**Partnered skip logic for the VJ spec**: the VJ workbook spec requires a
row per pair (not a row per competitor) for partnered events. Partnered events
in the list above:
- College: Peavey Log Roll, Pulp Toss, Double Buck, Jack & Jill Sawing
- Pro: Double Buck, Jack & Jill Sawing, Partnered Axe Throw

Also note: **LIST_ONLY_EVENT_NAMES** at [config.py:464](../config.py#L464) —
`axethrow, peaveylogroll, cabertoss, pulptoss` are tracked as sign-up lists
only; **no heats are generated** for them. So their heats list will be empty
and the VJ export either needs to skip them or emit a "No heats — sign-up
only" tab. Existing `get_event_heats_for_judging()` returns an empty heats
array for these ([line 93](../services/judge_sheet.py#L93) — "No heats"
message flagged in template).

### Event list for a specific tournament

Tournaments instantiate from these lists via `_create_college_events()` and
`_create_pro_events()` in `routes/scheduling` (CLAUDE.md section 2). Actual
events for a given tournament come from `Event.query.filter_by(tournament_id=tid)`
— the judge-sheets-all route already does this at
[routes/scoring.py:1565-1570](../routes/scoring.py#L1565-L1570). Mirror that
query for the VJ workbook.

---

## TASK 5 — Birling bracket: data and templates

### State storage

**The entire bracket lives in `Event.payouts` (JSON TEXT)** —
[services/birling_bracket.py:20-46](../services/birling_bracket.py#L20-L46).
This is the "repurposing payouts field for state" pattern used by ProAmRelay,
PartneredAxeThrow, and BirlingBracket (CLAUDE.md section 2: "Service classes for
complex state").

Default shape returned by `_load_bracket_data()`:
```python
{
    "bracket": {
        "winners": [[match, ...], ...],    # list of rounds
        "losers":  [[match, ...], ...],    # list of rounds
        "finals": {match},
        "true_finals": {match, "needed": bool}
    },
    "competitors": [{"id": int, "name": str}, ...],
    "seeding": [comp_id, comp_id, ...],    # 1st seed first
    "current_round": "winners_1",
    "placements": {comp_id_str: position_int}
}
```

Match dict shape (winners + losers):
```python
{
    "match_id": "W1_1" | "L2_3" | "F1" | "F2",
    "round": "winners_1" | "losers_2" | "finals" | "true_finals",
    "competitor1": int | None,   # None = TBD or BYE
    "competitor2": int | None,
    "winner": int | None,
    "loser": int | None,
    "falls": [{"fall_number": int, "winner": int, "recorded_at": iso}, ...],
    "is_bye": bool,                              # winners only
    "eliminated_position": int | None,           # losers only
    "needed": bool,                              # true_finals only
}
```

### Bracket structure generation

`BirlingBracket.generate_bracket(competitors, seeding=None)` at
[services/birling_bracket.py:48](../services/birling_bracket.py#L48):

1. `bracket_size = 2 ** ceil(log2(N))` — next power of 2 ≥ competitor count.
2. Number of byes = `bracket_size - N`.
3. **Round 1 pairings**: standard tournament bracket (1 vs N, 2 vs N-1, etc.) —
   `seed1 = i`, `seed2 = bracket_size - 1 - i`. Missing seeds yield `None`
   (BYE slots), which auto-advance the present competitor via lines 97-100.
4. Subsequent winners rounds created with `None` competitors (TBD).
5. Losers bracket via `_generate_losers_bracket()` — `2 * (log2(B) - 1)` rounds
   alternating consolidation (odd) and drop-down (even).
6. Finals (`F1`) and true finals (`F2`) placeholders.
7. `_propagate_byes()` walks Round 1 BYE matches and advances the present
   competitor into their next winners match.

**Result**: after `generate_bracket()` runs, every Round-1 match has both
`competitor1` and `competitor2` filled (or one + `is_bye=True`). All later
rounds have `None` competitors because they await advancement.

**Perfect for the blank-bracket print**: generate → save → print. No match
results recorded, but round-1 matchups populated.

### Seeding source & timing

In `scheduling.birling_generate()` at
[routes/scheduling/birling.py:85](../routes/scheduling/birling.py#L85):
- If manual `seed_{comp_id}` form fields are filled, use those.
- Otherwise fall back to `pre_seedings` from ability rankings
  ([line 123-138](../routes/scheduling/birling.py#L123-L138)).
- Unseeded competitors sort alphabetically after seeded ones.
- `event.payouts` dict has a `pre_seedings` key populated from the
  ability-rankings page (CLAUDE.md section 4: "Also supports College Birling
  Seedings — per-school ordering stored as `pre_seedings` in `Event.payouts` JSON").

**Seeding IS intended to happen in show-prep**. The flow is:
`scheduling.ability_rankings` (per-school birling seedings drag-drop) →
`scheduling.birling_manage` (confirm/override seeds and press Generate Bracket).
Once Generate Bracket runs, the bracket has round-1 populated — that's the
target state for the blank print.

### Templates

- [templates/scheduling/birling_manage.html](../templates/scheduling/birling_manage.html)
  — the main bracket view. 512 lines. Sections:
  - Seeding form (table with `seed_{comp.id}` input per competitor).
  - Active matches card (playable matches with fall-tracker UI).
  - Bracket visualization (winners / losers / finals) using `render_match` +
    `render_slot` Jinja macros at [lines 323-379](../templates/scheduling/birling_manage.html#L323-L379).
  - Placements table.
  - Actions (Finalize / Reset).
- [templates/scheduling/heat_sheets_print.html:443-](../templates/scheduling/heat_sheets_print.html#L443)
  — already renders **live** birling brackets inline on the heat sheets print
  page. It has its own `hs-bracket-*` CSS and `bracket_slot` / `bracket_match`
  macros at [line 486-516](../templates/scheduling/heat_sheets_print.html#L486-L516).
  This rendering already handles an in-progress or blank bracket, because the
  slot macro gracefully renders `TBD` for `None` competitors
  ([line 487-499](../templates/scheduling/heat_sheets_print.html#L487-L499)).
- [templates/scoring/birling_bracket.html](../templates/scoring/birling_bracket.html)
  — 133 lines. `scoring.birling_bracket` route at
  [routes/scoring.py:1498](../routes/scoring.py#L1498) is a legacy redirect to
  `scheduling.birling_manage`. The template may still be used elsewhere; not
  recon-critical.

### Can the bracket render with empty competitor slots?

**Yes, by design.** All three bracket templates (`birling_manage.html`,
`heat_sheets_print.html`, and the render_slot macro) already handle
`comp_id=None` → "TBD" rendering for rounds 2+. So a freshly-generated bracket
with only round-1 populated will render cleanly with all later slots as "TBD".
No template changes needed for the blank-bracket scenario; only a new route +
standalone print template.

**Caveat**: the existing render assumes the bracket was generated. If the user
hits the print-blank-bracket button before `generate_bracket()` has run, the
page will have no structure to render. The workflow needs to either (a)
auto-generate on first print, or (b) flash "Generate the bracket first" and
redirect.

---

## TASK 6 — Print/PDF infrastructure

### WeasyPrint

**NOT in requirements.txt** — absent from [requirements.txt](../requirements.txt).
Both existing PDF routes import it lazily inside a try/except:

```python
try:
    from weasyprint import HTML as WP_HTML
    pdf_bytes = WP_HTML(string=html).write_pdf()
    return pdf_bytes, 200, {'Content-Type': 'application/pdf', ...}
except ImportError:
    return html, 200, {'Content-Type': 'text/html'}
```

See [routes/scoring.py:1527-1537](../routes/scoring.py#L1527-L1537)
(`_judge_sheet_response`) and the heat-sheet PDF route around
[routes/scoring.py:1490-1495](../routes/scoring.py#L1490-L1495).

**Implication**: on Railway (production) WeasyPrint is probably not installed
(system deps are heavy: cairo, pango, gdk-pixbuf). Users download HTML
instead and press Ctrl-P to save as PDF. The blank-bracket deliverable
should follow the same pattern — HTML is the reliable output, PDF is a
bonus if WeasyPrint happens to be available.

### Print CSS patterns in use

Two distinct patterns:

1. **Standalone inline-CSS templates for WeasyPrint** (preferred pattern for
   PDF-first exports):
   - [templates/scoring/judge_sheet.html:8](../templates/scoring/judge_sheet.html#L8) —
     `@page { size: ...; margin: ...; @bottom-center { content: ...; } }`
   - [templates/scoring/heat_sheet_print.html:8](../templates/scoring/heat_sheet_print.html#L8) —
     `@page { size: letter portrait; margin: 1.5cm; }`
   - Does NOT extend base.html. No Bootstrap, no theme.css — inline CSS only.
   - This is the template the blank-bracket print should mimic.

2. **In-app print screen with `@media print` overrides** (the heat sheets page
   for browser Ctrl-P use):
   - [templates/scheduling/heat_sheets_print.html:124](../templates/scheduling/heat_sheets_print.html#L124) —
     `@media print { ... }` block; extends base.html with Bootstrap sidebar
     hidden via `.no-print`.

For the blank birling bracket, pattern #1 is the right match: a standalone
template modeled on `judge_sheet.html` / `heat_sheet_print.html` with inline
CSS and `@page` declarations.

### Recommended reuse

- **Page header / footer idiom**: mirror `judge_sheet.html` lines 26-38
  (header) + `@page @bottom-center` page counter.
- **Bracket slot / match macros**: copy the `hs-bracket-*` CSS and macros
  from [templates/scheduling/heat_sheets_print.html:453-516](../templates/scheduling/heat_sheets_print.html#L453-L516) —
  already inlined, already renders `TBD` for blank slots, already sized for
  print.

---

## TASK 7 — Field-size scaling in bracket rendering

### Current bracket rendering is dynamic, not fixed-layout

- **Winners bracket**: `log2(bracket_size)` rounds, each with half the matches
  of the previous. Rendered as one `.bracket-round` flex column per round.
  ([templates/scheduling/birling_manage.html:388-398](../templates/scheduling/birling_manage.html#L388-L398))
- **Losers bracket**: `2 * (log2(B) - 1)` rounds, same column-per-round layout.
  ([line 410-417](../templates/scheduling/birling_manage.html#L410-L417))
- Each `.bracket-round` is `min-width: 180px; flex-shrink: 0`, so horizontal
  space grows with round count.
- `.bracket-container { overflow-x: auto; }` — wide brackets scroll
  horizontally in-browser.

### Field size implications

`bracket_size = 2 ** ceil(log2(N))`, so rounds scale predictably:

| N competitors | bracket_size | winners rounds | losers rounds | total round columns |
|---|---|---|---|---|
| 2-4 | 4 | 2 | 2 | 4 |
| 5-8 | 8 | 3 | 4 | 7 |
| 9-16 | 16 | 4 | 6 | 10 |

At N=16 with 10 round columns × 180px minimum = 1800px horizontal. This
overflows both `Letter portrait` (~2100px at 300dpi ÷ ~200dpi actual = ~1100px)
AND `Letter landscape` without scaling. The birling_manage page works around
this with `overflow-x: auto` and horizontal scroll, but a **print** layout
cannot scroll.

**Options for the blank print**:
- (a) `@page { size: A3 landscape; }` or `size: 17in 11in;` for a tabloid sheet.
- (b) Scale down font + min-width at larger N.
- (c) Split losers bracket onto a second page — winners + finals on page 1,
  losers on page 2.
- (d) For small fields (N ≤ 8, which is the typical Missoula field per
  CLAUDE.md section 3 — "top 6 determined", implying single-digit entries),
  Letter landscape at current CSS is roughly adequate.

Field size for Missoula Pro Am birling: college birling is gender-segregated;
each school typically sends 1-2 birlers per gender; ballpark total field per
gender is 8-16. So N=8 is the realistic low-end, N=16 is the upper end.
Letter landscape with minor font tweaks works up to ~8-10; beyond that, A3 or
multi-page layout is required.

### Recommendation boundary (taste call, not implementation)

Anything beyond recon, but the existing `hs-bracket-*` CSS in heat_sheets_print
([line 453-480](../templates/scheduling/heat_sheets_print.html#L453-L480))
is already tighter than the interactive CSS — 170px columns, 0.82rem font — and
would likely fit a 16-field bracket in A3 landscape or Letter landscape with
one more font shrink. Clean baseline to start from.

---

## TASK 8 — Birling nav surfacing (current state)

### Existing entry points to `scheduling.birling_manage`

| Location | File | Context |
|---|---|---|
| Event results page "Bracket" button | [templates/scoring/event_results.html:86](../templates/scoring/event_results.html#L86) | Only shown when `event.scoring_type == 'bracket'` |
| Ability rankings page | [templates/scheduling/ability_rankings.html:174](../templates/scheduling/ability_rankings.html#L174) | "College Birling Seedings" section; drag-drop writes `pre_seedings` into `event.payouts` but does not link to bracket page |
| Legacy redirect | [routes/scoring.py:1498](../routes/scoring.py#L1498) | `scoring.birling_bracket` → redirects to `scheduling.birling_manage` |

### What's missing

- **No sidebar link** — [templates/_sidebar.html](../templates/_sidebar.html)
  has zero matches for `birling` (grep returned nothing). The "Run Show" sidebar
  group has `Build Schedule / Friday / Saturday Operations / Heat Sheets / Kiosk`
  but no Birling / Brackets entry.
- **No tournament_detail card** — [templates/tournament_detail.html](../templates/tournament_detail.html)
  has no link to birling_manage in any of the phase panels.
- **No events.html bracket button** — the events list at
  [templates/scheduling/events.html:627-636](../templates/scheduling/events.html#L627-L636)
  shows an "Always Last" badge for birling but **does not link to birling_manage**.
  A judge sees the event in the list but has no obvious click-through.
- **Only discoverable via Event Results screen** — which requires the judge to
  navigate to Results → pick the birling event → see the Bracket button. That's
  the deepest possible path and explains why judges-in-training can't find it.

### Lowest-effort surfacing candidates

1. **Event list row → "Bracket" action button** on the events.html event list
   when `event.scoring_type == 'bracket'`. Mirrors the event_results.html
   pattern. Single-template change.
2. **Sidebar "Run Show" group → "Birling Bracket(s)"** entry. Slightly more
   work because there are two events (men's + women's) for college; either
   link to a landing page listing both, or surface each directly.
3. **Tournament_detail "Ready for Game Day" action bar** → Birling Bracket
   button next to Heat Sheets / Judge Sheets. Only visible in setup phase, but
   that's the exact phase where show-prep happens.

---

## Open Questions for Alex

1. **VJ workbook — partnered event row format**: confirmed requirement is
   "one row per pair per run". Should the partner column render as
   `"Smith / Jones"` (single cell) or as a separate second column? The
   existing judge_sheet.html and heat sheet templates both use single-cell
   `"{a} & {b}"` or `"{a} / {b}"` — is that the right model?

2. **VJ workbook — run 2 inclusion**: `get_event_heats_for_judging()` filters
   out run 2 for the paper judge sheet because it uses multi-column layout.
   The VJ workbook spec says "one row per competitor per run". Should run 1
   and run 2 appear on the same tab (stacked rows) or on separate tabs per
   run? Chokerman's Race and Speed Climb are the day-split cases — run 1 is
   Friday, run 2 is Saturday. Keeping them on one tab may confuse the
   video-judging workflow.

3. **VJ workbook — Birling / LIST_ONLY events**: confirmed Birling must be
   skipped (scoring_type='bracket' is not video-timed). Should the
   sign-up-only events (Axe Throw, Peavey Log Roll, Caber Toss, Pulp Toss —
   `LIST_ONLY_EVENT_NAMES` at [config.py:464](../config.py#L464)) also be
   skipped? They have no heats to populate rows from — the workbook would
   emit empty tabs. Recommend skip; confirm.

4. **VJ workbook — scoring_type='hits' / 'score' events**: Underhand Hard Hit,
   Standing Block Hard Hit, Axe Throw (if closed), Partnered Axe Throw all
   score by hits/points, not time. These are judged in-person, not on video.
   Should they be excluded from the VJ workbook, or included with "VJ Score 1 /
   VJ Score 2" columns for documentation only? Existing judge sheet includes
   them all.

5. **Birling blank bracket — multiple brackets per tournament**: men's and
   women's college brackets are separate Event rows (both with
   `scoring_type='bracket'`). Does the blank-bracket deliverable produce
   (a) one PDF per event (two separate buttons on birling_manage), or
   (b) one combined PDF (two brackets, one document) invoked from a higher
   surface? Existing judge-sheets-all already follows pattern (b) with the
   single tournament-wide aggregator.

6. **Birling blank bracket — bracket-not-yet-generated UX**: the bracket has
   to exist in `event.payouts` before round-1 slots are populated. If the
   judge clicks "Print Blank Bracket" before `generate_bracket()` has been
   run via the seeding form, what should happen? Auto-generate on the fly
   (from `pre_seedings` or alpha order)? Or flash "seed first" error and
   redirect?

7. **Nav surfacing — scope**: which of the three surfacing options (events
   list row button / sidebar Run-Show entry / tournament_detail action bar)
   is in scope? All three? One? If sidebar, how should the two college
   brackets (men's + women's) be addressed — landing page listing all
   bracket events in the tournament, or one link per event?

8. **Show-prep home for the new buttons**: should the VJ workbook and
   blank-bracket buttons live under a new "Reports" or "Exports" section of
   the sidebar (which does not yet exist — see Task 1), or be appended into
   the existing scattered locations (tournament_detail + sidebar "Run Show")?
   A dedicated Exports hub would make all ~6 existing exports discoverable
   in one place, but is a bigger-scope change than the two deliverables need.

9. **Filename convention for VJ workbook**: existing xlsx filenames use
   `{tournament.name}_{tournament.year}_results.xlsx` (spaces → `_`). VJ
   workbook suggested as `{tournament.name}_{year}_video_judge_sheets.xlsx`?

10. **Handedness / left-handed springboard**: pro springboard heats group
    left-handed cutters together on specific dummies. Does the VJ workbook
    need to surface this (e.g., a flag column) or is stand assignment
    sufficient? Current judge_sheet.html does not show stand assignments at
    all — rows are `name / team_code / blank timers / status / reason`.
