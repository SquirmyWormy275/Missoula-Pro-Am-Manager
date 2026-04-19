# Gear Sharing Audit

Read-only audit of the gear-sharing pipeline: parse → store → consume → conflict-resolve.
Date: 2026-04-18. Scope: source-of-truth review for the bug "OP Saw and Cookie Stack
SHARING entries from the entry form are not appearing in the gear-sharing module," plus
USING/SHARING vocabulary handling, name matching, Q27 interaction, and degenerate input.

Domain context already documented in
`docs/Alex's Docs/GEAR_SHARING_DOMAIN.md` — that file enumerates per-event sharing
constraints. This audit does not duplicate it; it documents the code that implements
(or fails to implement) those constraints.

---

## 1. File inventory

### Parsers / normalizers

| Path | Lines | Role |
|------|-------|------|
| `services/gear_sharing.py` | 1611 | Primary parser, normalizer, conflict checker, audit-builder, batch ops, heat auto-fix |
| `services/registration_import.py` | 1073 | Enhanced import pipeline. Wraps `pro_entry_importer.parse_pro_entries()` with dirty-text gear parser (`_parse_dirty_gear_text`), name fuzzy-resolve, gender/event cross-validation, gear flag reconciliation |
| `services/pro_entry_importer.py` | 307 | Basic Google Forms xlsx parser. Reads `Are you sharing gear?` (Q27) and the free-text "If yes, provide..." column |

### Storage models

| Path | Lines | Role |
|------|-------|------|
| `models/competitor.py` | 377 | `CollegeCompetitor.gear_sharing` (TEXT JSON), `ProCompetitor.gear_sharing` (TEXT JSON), `ProCompetitor.gear_sharing_details` (TEXT raw form value) |

### Consumers (heat / flight / validation)

| Path | Lines | Role |
|------|-------|------|
| `services/heat_generator.py` | 812 | Calls `competitors_share_gear_for_event` during snake-draft fallback (`_generate_standard_heats`, `_generate_springboard_heats`); records violations to `_last_gear_violations` |
| `services/flight_builder.py` | 1034 | Calls `build_gear_conflict_pairs(tournament)` once; uses pair set as adjacency penalty in `_score_ordering` (-200) and `_calculate_heat_score` (-200) |
| `services/validation.py` | 416 | `HeatValidator.validate_gear_sharing(heat)` runs the same pair-wise check via `HeatAssignment` rows |

### Routes / templates

| Path | Lines | Role |
|------|-------|------|
| `routes/registration.py` | 1412 | All gear-manager endpoints (`pro_gear_manager`, `pro_gear_parse`, `pro_gear_parse_review`, `pro_gear_parse_confirm`, `pro_gear_update`, `pro_gear_update_ajax`, `pro_gear_remove`, `pro_gear_complete_pairs`, `pro_gear_cleanup_scratched`, `pro_gear_auto_partners`, `pro_gear_sync_heats`, `pro_gear_group_create`, `pro_gear_group_remove`, `college_gear_update`, `college_gear_update_ajax`, `auto_assign_pro_partners_route`, `pro_gear_print`); `update_pro_events` writes per-event `gear_{eid}` form fields |
| `routes/import_routes.py` | (full) | Pro-entry-form xlsx upload → review → confirm. Line 315 only calls `parse_gear_sharing_details` when `entry.get('gear_sharing')` (Q27) is truthy |
| `templates/pro/gear_sharing.html` | 934 | Manager UI: stat cards, conflicts alert, group-size warnings, unresolved table, group table, named groups table, add/update form, college table, edit modal |
| `templates/pro/gear_sharing_print.html` | 186 | Printable report (pro pairs, college constraints) |
| `templates/pro/gear_parse_review.html` | 127 | Per-competitor parse-proposal review with checkbox commit |

### Tests

| Path | Lines |
|------|-------|
| `tests/test_gear_sharing.py` | 711 |
| `tests/test_gear_sharing_advanced.py` | 310 |
| `tests/test_gear_sharing_parse_realistic.py` | 376 |

### Config

- `config.py` lines 231–306 define `HANDICAP_ELIGIBLE_STAND_TYPES`, `GEAR_FAMILIES`, `NO_CONSTRAINT_STAND_TYPES`, `STAND_CONFIGS`. Stand type → gear family taxonomy lives in `GEAR_FAMILIES`.

---

## 2. Parser anatomy

### 2.1 `services/gear_sharing.py::parse_gear_sharing_details`

`services/gear_sharing.py:388`

```
def parse_gear_sharing_details(
    details_text: str,
    event_pool: list,
    name_index: dict[str, str],
    self_name: str = '',
    entered_event_names: list[str] | None = None,
) -> tuple[dict[str, str], list[str]]:
```

**Input:** raw form free-text (`details_text`), the tournament's pro events
(`event_pool`), a normalized-name → canonical-name lookup (`name_index`), the
posting competitor's own name, and the events that competitor entered (used
to disambiguate "SB").

**Output:** `(gear_map, warnings)` where `gear_map` is `dict[event_key, partner_name]`.
`event_key` is either `str(event.id)` (numeric event id) or `f'category:{cat}'`
(category fallback). `warnings` is a list of string codes:
`'missing_details'`, `'partner_not_resolved'`, `'events_not_resolved'`.

**Downstream consumers of the output dict:**

- `services/gear_sharing.py:856` `auto_parse_and_warn` — writes to
  `competitor.gear_sharing` JSON column on registration
- `services/gear_sharing.py:1387` `parse_all_gear_details` — bulk parser called
  from `pro_gear_parse` route
- `services/gear_sharing.py:807` `build_parse_review` — proposed-rows for the
  review UI
- `routes/import_routes.py:316` — invoked during xlsx confirm

**Vocabulary (verbatim):**

Equipment categories — `services/gear_sharing.py:292`:

```python
def infer_equipment_categories(text: str) -> set[str]:
    """Infer broad equipment categories from free-text detail strings."""
    normalized = str(text or '').strip().lower()
    categories = set()
    if any(token in normalized for token in ['single buck', 'double buck', 'crosscut', 'jack & jill', 'jack and jill', 'handsaw', 'hand saw']):
        categories.add('crosscut')
    if any(token in normalized for token in ['hot saw', 'chainsaw', 'power saw', 'powersaw']):
        categories.add('chainsaw')
    if any(token in normalized for token in ['springboard', 'board']):
        categories.add('springboard')
    return categories
```

Only **three** categories are emitted: `crosscut`, `chainsaw`, `springboard`.
Cookie Stack, Obstacle Pole / OP Saw, Speed Climb, and Underhand / Standing Block
**have no category branch and cannot be inferred from text alone.**

Event aliases — `services/gear_sharing.py:305`:

```python
def _event_name_aliases(event) -> set[str]:
    aliases = {
        normalize_event_text(getattr(event, 'name', '')),
        normalize_event_text(getattr(event, 'display_name', '')),
    }
    event_name = normalize_event_text(getattr(event, 'name', ''))
    stand_type = str(getattr(event, 'stand_type', '') or '').strip().lower()

    if event_name == 'springboard':
        aliases.update({'springboardl', 'springboardr'})
    elif event_name in {'pro1board', '1boardspringboard'}:
        aliases.update({'intermediate1boardspringboard', 'pro1board', '1boardspringboard'})
    elif event_name == 'jackjillsawing':
        aliases.update({'jackjill', 'jackandjill'})
    elif event_name in {'poleclimb', 'speedclimb'}:
        aliases.update({'poleclimb', 'speedclimb'})
    elif event_name == 'partneredaxethrow':
        aliases.update({'partneredaxethrow', 'axethrow'})

    if stand_type == 'saw_hand':
        aliases.update({'singlebuck', 'doublebuck', 'jackjill', 'jackandjill', 'crosscut'})
    elif stand_type == 'hot_saw':
        aliases.update({'hotsaw', 'chainsaw', 'powersaw'})
    elif stand_type == 'springboard':
        aliases.update({'springboard', '1boardspringboard', 'pro1board'})

    return {a for a in aliases if a}
```

There are **no `elif stand_type == 'cookie_stack'`, `elif stand_type == 'obstacle_pole'`,
or `elif stand_type == 'speed_climb'` branches.** Cookie Stack and Obstacle Pole
events get only their own normalized name (`cookiestack`, `obstaclepole`) plus the
display_name normalization. The strings `cookiestacksaw`, `opsaw`, `op`, `cookie saw`
are **not** aliases.

Short codes — `services/gear_sharing.py:335`:

```python
def _short_event_codes(event) -> set[str]:
    name = normalize_event_text(getattr(event, 'name', ''))
    display = normalize_event_text(getattr(event, 'display_name', ''))
    combined = f'{name} {display}'
    codes = set()
    if 'underhand' in combined:
        codes.add('uh')
    if 'obstaclepole' in combined:
        codes.add('op')
    if 'hotsaw' in combined:
        codes.add('hs')
    if 'springboard' in combined:
        codes.add('sb')
    if 'singlebuck' in combined:
        codes.update({'sbu', 'singlebuck'})
    if 'doublebuck' in combined:
        codes.add('db')
    if 'poleclimb' in combined or 'speedclimb' in combined:
        codes.update({'pc', 'sc'})
    if 'stocksaw' in combined:
        codes.add('ss')
    if 'cookiestack' in combined:
        codes.add('cs')
    return codes
```

OP Saw events have short code `op`; Cookie Stack has `cs`. These match against
`raw_tokens = set(re.findall(r'[a-z0-9]+', lowered))` — any standalone token in
the text that equals `op` or `cs` triggers a match (`gear_sharing.py:464,481`).

**SB ambiguity guard** (`gear_sharing.py:484`): when more than one springboard event
matches and the user did not enter any springboard event, the `sb` short-code is
suppressed. There is no analogous guard for `op` or `cs`.

Partner-segment scrub regex — `services/gear_sharing.py:446`:

```python
first_segment = re.sub(
    r'\b(sharing|with|gear|events?|springboard|crosscut|underhand|standing\s*block|'
    r'single\s*buck|double\s*buck|jack\s*(?:&|and)\s*jill|hot\s*saw|stock\s*saw|'
    r'chainsaw|power\s*saw|hand\s*saw|board|axe|saw|speed|hard\s*hit)\b',
    '', first_segment, flags=re.IGNORECASE
).strip()
```

**Cookie Stack and Obstacle Pole / OP Saw / Cookie Stack saw / Speed Climb / Pole
Climb are not in this scrub list**, so when no comma/colon/dash separator exists
and no canonical name is detected via `text_tokens`, the leftover words bleed
into the partner-name candidate.

Token-sequence partner detection — `services/gear_sharing.py:417`:

```python
text_tokens = _name_tokens(text)
mentioned = []
for norm_name, canonical_name in name_index.items():
    if not norm_name or norm_name == self_norm:
        continue
    canon_tokens = _name_tokens(canonical_name)
    if not canon_tokens:
        continue
    n = len(canon_tokens)
    canon_has_suffix = canon_tokens[-1] in _NAME_SUFFIXES
    for i in range(len(text_tokens) - n + 1):
        if text_tokens[i:i + n] != canon_tokens:
            continue
        next_tok = text_tokens[i + n] if i + n < len(text_tokens) else None
        if next_tok in _NAME_SUFFIXES and not canon_has_suffix:
            continue
        mentioned.append((len(norm_name), canonical_name))
        break
```

Looks for an exact tokenized name sequence in the message text. **Strictly
exact tokens**: a misspelling such as `Cody Lebahn` for `Cody Labahn` will not
match here. Falls through to the first-segment fuzzy fallback.

**USING vs SHARING handling — there is no distinction.** The strings
`USING`, `SHARING`, `using`, `sharing`, `Sharing` appear nowhere as keywords
or branches in any parser. The word `sharing` is **only** a strip-target inside
the partner-segment regex above. The parser treats every detail string the same
way regardless of which keyword (or no keyword) the competitor used. Both keywords
end up writing the same `event_id → partner_name` entry into `gear_sharing` JSON,
which `build_gear_conflict_pairs` then converts into a heat-conflict constraint.
This is the root domain mismatch surfaced by the prompt: "USING" rows from the
form (which mean "we are confirmed partners for a partnered event") get treated
as cross-competitor sharing constraints exactly like "SHARING" rows.

### 2.2 `services/gear_sharing.py::resolve_partner_name`

`services/gear_sharing.py:170`

Resolution order:
1. Exact normalized match (`normalize_person_name`, strips non-alphanumeric) → 1.0 score
2. `difflib.get_close_matches(..., n=3, cutoff=0.86)` with two filters:
   - `_suffix_mismatch` rejects David Moses ↔ David Moses Jr.
   - `_names_token_compatible` rejects Eric ↔ Erin Lavoie (same last, divergent first)
3. Bare last-name fallback (single-token input, ≥3 chars, exactly one match)
4. Two-token fallback for "A. Smith" / "Bri Kvinge" → "Alice Smith" / "Brianna Kvinge"

Ambiguity guard — `gear_sharing.py:215`: if multiple fuzzy candidates above the
cutoff resolve to different canonicals, returns the raw input rather than guessing.

### 2.3 `services/gear_sharing.py::competitors_share_gear_for_event`

`services/gear_sharing.py:502`

Pair-wise gear-conflict check used by all consumers. Walks both competitors'
`gear_sharing` dicts; for each entry whose key matches the event (via
`event_matches_gear_key`), checks if the partner name (normalized) equals the
other competitor. When `all_events` is supplied and the event belongs to a
cascade family (`config.GEAR_FAMILIES['chopping'].cascade=True`,
`['crosscut_saw'].cascade=True`), checks sibling events too — sharing an axe for
Springboard means conflict in Underhand and Standing Block as well.

Group-key handling: a value `'group:{name}'` matches if the other competitor has
the **same exact group string** stored under the same event key.

### 2.4 `services/gear_sharing.py::build_gear_conflict_pairs`

`services/gear_sharing.py:905`

Returns `dict[int, set[int]]` — competitor id → set of competitor ids they share
gear with for **any** event. Used by `flight_builder._score_ordering`
(`flight_builder.py:475`) and `_calculate_heat_score`
(`flight_builder.py:586`). The penalty is event-blind — once a pair is recorded
they will be penalized for being in adjacent heats of any event. This is the
core data structure that conflates USING (partnered confirmation) and SHARING
(conflict): if the parser writes any entry pointing competitor A at competitor B,
this dict pairs them and the flight builder will spread their heats apart.

Cascade-pass at `gear_sharing.py:947` extends pairs that declared a chopping or
crosscut sharing on any single event in the family to all events in that family.

### 2.5 `services/gear_sharing.py::auto_parse_and_warn`

`services/gear_sharing.py:822`

Called from `routes/registration.py:511` immediately after a manual pro-competitor
registration commit. Reads `competitor.gear_sharing_details` (the textbox from
the new-competitor form), runs `parse_gear_sharing_details`, writes the result
into `competitor.gear_sharing` JSON (overwriting). Returns flash-friendly warning
strings. No USING/SHARING distinction.

### 2.6 `services/registration_import.py::_parse_dirty_gear_text`

`services/registration_import.py:390`

Alternate parser used by the "enhanced" pipeline (only invoked through
`routes/import_routes.py` when the user uploads the xlsx via the enhanced flow,
which is currently optional). Returns a list of records:
`{equipment, event_hint, partners[], conditional}`.

Equipment alias map — `services/registration_import.py:360`:

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

This map **does** include `cookie stack` and `op`. However the output of this
parser populates `CompetitorRecord.gear_sharing_records` (an in-memory data
class), which is consumed only by `to_entry_dicts()` for an alternate confirm
path; the records are not written to `ProCompetitor.gear_sharing` JSON. The
`gear_sharing_records` field is also only read by `_check_unregistered_references`
and the report-text renderer — never by heat or flight code.

Conditional detection — `services/registration_import.py:407`:

```python
is_conditional = bool(
    re.search(
        r"\b(?:sometimes|possibly|may\s+be|tbd|likely|unsure|if\s+ours|currently)\b",
        text,
        re.IGNORECASE,
    )
)
```

USING / SHARING are not in this list either. There is no token-class distinction
between confirmation language and dependency language in either parser.

Filler regex — `services/registration_import.py:384`:

```python
_FILLER_RE = re.compile(
    r"\b(?:me\s+and|sharing\s+a?|sharing\s+with|borrowing|will\s+need\s+to\s+share\s+a?)\b",
    re.IGNORECASE,
)
```

Strips `sharing`, `sharing with`, `me and`, `borrowing`, `will need to share` —
treats `sharing` as a noise word, not a typed marker.

### 2.7 `services/pro_entry_importer.py::parse_pro_entries`

`services/pro_entry_importer.py:85`

Reads the Google Forms xlsx. Q27 mapping at line 191:

```python
gear_sharing = _yes(_get(row, hmap.get('Are you sharing gear?')))
```

This is the boolean. The free-text column is identified at line 113:

```python
gear_detail_col = next((i for i, h in enumerate(stripped) if h.lower().startswith('if yes, provide')), None)
```

Returned dict carries `'gear_sharing': bool` (Q27) and
`'gear_sharing_details': str | None` (free text). **No further parsing of the
text happens at this layer.**

`compute_review_flags` at line 238 uses `infer_equipment_categories` to flag
`GEAR SHARING DETAILS MAY BE AMBIGUOUS` for entries whose text yields no
category signal. Since `infer_equipment_categories` returns `set()` for "OP Saw"
and "Cookie Stack" (no chainsaw/crosscut/springboard tokens unless the user
also wrote "saw"), most OP/Cookie Stack-only entries are flagged ambiguous in
the review UI but are still committed if the reviewer clicks confirm.

---

## 3. Event → gear mapping

### Stand type per event (`config.py` 380–429)

| Event | Where defined | `stand_type` | Gear family | Cascade |
|-------|---------------|--------------|-------------|---------|
| Hot Saw (pro) | `config.py:419` | `hot_saw` | `hot_saw` | False |
| Single Buck (pro & college) | `config.py:400,420` | `saw_hand` | `crosscut_saw` | True |
| Double Buck (pro & college) | `config.py:401,421` | `saw_hand` | `crosscut_saw` | True |
| Jack & Jill Sawing | `config.py:402,422` | `saw_hand` | `crosscut_saw` | True |
| Springboard / 2-Board (pro) | `config.py:413` | `springboard` | `chopping` | True |
| Pro 1-Board | `config.py:414` | `springboard` | `chopping` | True |
| 3-Board Jigger | `config.py:415` | `springboard` | `chopping` | True |
| 1-Board Springboard (college) | `config.py:408` | `springboard` | `chopping` | True |
| Underhand (pro/college Hard Hit + Speed) | `config.py:396,397,416` | `underhand` | `chopping` | True |
| Standing Block (pro/college Hard Hit + Speed) | `config.py:398,399,417` | `standing_block` | `chopping` | True |
| Obstacle Pole (pro) | `config.py:426` | `obstacle_pole` | `op_saw` (pro_only) | False |
| Obstacle Pole (college) | `config.py:405` | `obstacle_pole` | NONE — `is_no_constraint_event` returns True for college (`gear_sharing.py:140`) |
| Speed Climb / Pole Climb | `config.py:404,427` | `speed_climb` | `climbing` | False |
| Cookie Stack | `config.py:428` | `cookie_stack` | `cookie_stack` | False |
| Stock Saw | `config.py:403,418` | `stock_saw` | NONE — `NO_CONSTRAINT_STAND_TYPES` |
| Birling | `config.py:407` | `birling` | NONE — `NO_CONSTRAINT_STAND_TYPES` |
| Axe Throw / Peavey / Caber / Pulp / Chokerman | various | various | NONE — `NO_CONSTRAINT_STAND_TYPES` |

`config.GEAR_FAMILIES` (`config.py:243-269`):

```python
GEAR_FAMILIES = {
    'chopping': {'stand_types': {'underhand', 'standing_block', 'springboard'}, 'cascade': True},
    'crosscut_saw': {'stand_types': {'saw_hand'}, 'cascade': True},
    'hot_saw': {'stand_types': {'hot_saw'}, 'cascade': False},
    'climbing': {'stand_types': {'speed_climb'}, 'cascade': False},
    'op_saw': {'stand_types': {'obstacle_pole'}, 'cascade': False, 'pro_only': True},
    'cookie_stack': {'stand_types': {'cookie_stack'}, 'cascade': False},
}
```

### How heat / flight generation queries `gear_sharing`

**Heat generator (`services/heat_generator.py`):**

1. `_collect_competitors` (around line 312) snapshots each entrant into a dict
   that includes `'gear_sharing': comp.get_gear_sharing()`.
2. `_generate_standard_heats` (`heat_generator.py:324`) does a snake-draft pass.
   Inside the inner loop (line 359) it skips heats where the candidate has any
   gear-sharing conflict via `_has_gear_sharing_conflict(comp, heats[heat_idx], event)`
   (`heat_generator.py:755`).
3. If no clean heat is found the second pass at `heat_generator.py:374` accepts
   the conflict but appends to `gear_violations` so the route can flash a warning.
4. `_generate_springboard_heats` (`heat_generator.py:562`) repeats the same
   pattern.
5. `_competitors_share_gear_for_event` (`heat_generator.py:763`) calls
   `competitors_share_gear_for_event(...)` with `all_events=_get_tournament_events(event)`,
   enabling cascade checks for chopping/crosscut events.

**Flight builder (`services/flight_builder.py`):**

1. `build_flights_for_tournament` calls `build_gear_conflict_pairs(tournament)`
   once (`flight_builder.py:139`) to produce an event-blind `{cid: {partner_ids}}` map.
2. `_calculate_heat_score` applies a `-200 * len(overlap)` penalty when the
   previous heat contains any conflict partner of any current-heat competitor
   (`flight_builder.py:586`).
3. `_score_ordering` adds the same `-200` per overlap on the full ordering
   (`flight_builder.py:475`).

Behaviour for each event when a SHARING constraint exists between two of its
competitors:

| Event | Heat-gen behaviour | Flight-builder behaviour |
|-------|-------------------|--------------------------|
| Hot Saw | enforced — split across heats; cascade=False so isolated to hot_saw event | adjacency penalty across all events |
| Single Buck (M/W) | enforced — split; cascade includes Double Buck and Jack & Jill | adjacency penalty across all events |
| Double Buck (M, partnered) | enforced at unit level — pairs treated as single stand allocation; cascade across saw_hand events | adjacency penalty |
| Jack & Jill (mixed) | same as Double Buck | adjacency penalty |
| Springboard / 1-Board / 3-Board Jigger | enforced; cascade includes Underhand and Standing Block | adjacency penalty; springboard opener bonus may override |
| Obstacle Pole — pro | enforced; isolated family | adjacency penalty |
| Obstacle Pole — college | **silently dropped** — `is_no_constraint_event` returns True for college (`gear_sharing.py:140-144`); `_event_name_aliases` only emits `obstaclepole`, no `opsaw` alias | no flight penalty |
| Cookie Stack | enforced if the stored entry maps to the cookie_stack event id; **but** the parser writes nothing for it from "OP Saw" / "Cookie Stack saw" text without a long-form alias hit (see Section 4) | adjacency penalty if entry exists |
| Speed Climb / Pole Climb | enforced if entry exists; no special category fallback (Speed Climb is not in `infer_equipment_categories`) | adjacency penalty |
| Jack & Jill (uses crosscut saws) | conflict cascades across `saw_hand` family, so Single/Double/J&J share constraints | adjacency penalty |

---

## 4. Bug hypothesis: OP Saw + Cookie Stack

### 4.1 Why `infer_equipment_categories` cannot save them

`gear_sharing.py:292` — only emits `crosscut`, `chainsaw`, `springboard`. The
strings "OP Saw", "obstacle pole saw", "Cookie Stack saw", "cookie saw" generate
**no category at all** unless the user also writes "chainsaw" or "saw" with a
separate signal. `cookie stack` does not match `chainsaw`. `op saw` does not
match any of the chainsaw-detection tokens (`'hot saw'`, `'chainsaw'`,
`'power saw'`, `'powersaw'`).

Consequence: when the explicit per-event match (Section 4.2) fails, the
category-based fallback at `gear_sharing.py:492` cannot recover the entry
either. The parser returns `('events_not_resolved')` and the resulting
`gear_map` is **empty**.

### 4.2 Why explicit event matching is unreliable for these two

`_event_name_aliases` (`gear_sharing.py:305`) emits **only the normalized event
name and display name** for Cookie Stack and Obstacle Pole. There is no
`elif stand_type == 'cookie_stack'` or `elif stand_type == 'obstacle_pole'`
branch.

For an event named `"Cookie Stack"`, aliases are `{'cookiestack'}` plus the
normalized `display_name`. The matching gate at `gear_sharing.py:480` is:

```python
alias_match = any(alias and alias in normalized_text for alias in aliases if len(alias) >= 4)
```

`'cookiestack' in 'sharingcookiestacksawwithcodylabahn'` → True, so the alias
match **does fire** when the user writes "Cookie Stack" verbatim. The bug
does not surface at the alias step for the literal phrase `Cookie Stack`.

For Obstacle Pole, alias `'obstaclepole'` fires only when the user writes
"obstacle pole" (any spacing/casing). Writing **only "OP Saw"** does not
contain `obstaclepole` after normalization (`opsaw`). The parser then falls
back to short codes (`gear_sharing.py:481`):

```python
short_match = any(code in raw_tokens for code in short_codes)
```

`raw_tokens` from "SHARING OP Saw with Cody Labahn" is
`{'sharing', 'op', 'saw', 'with', 'cody', 'labahn'}`. Short code `'op'` is
present (`_short_event_codes` line 343-344). The match should fire.

So **OP Saw via short code should match Obstacle Pole** — at the matching
layer alone. The actual failure must come from one of:

1. **Partner not resolved (`gear_sharing.py:457`):** if `partner_name` is empty
   the function returns `({}, ['partner_not_resolved'])` BEFORE event matching
   ever runs. This is the most likely silent drop. For "SHARING OP Saw with
   Cody Labahn":
   - Token-sequence detection fails if `Cody Labahn` is misspelled or absent
     from `name_index`.
   - First-segment fallback (`gear_sharing.py:443`) splits on `[-—:;,]` —
     no separator in this string, so the whole text is the segment. The
     scrub regex (`gear_sharing.py:446`) **does not list** `cookie`,
     `cookie stack`, `obstacle`, `obstacle pole`, `op`, `op saw`, `pole climb`,
     `speed climb`, `cookiesaw`. After stripping `sharing`, `with`, `saw`
     the leftover is `OP Cody Labahn` (or `Cookie Stack Cody Labahn`). With
     `len(...split()) >= 2`, `resolve_partner_name` runs on `'OP Cody Labahn'`
     — `Cody Labahn` is the canonical, but the input has three tokens. The
     normalized form is `opcodylabahn`. No exact match; `difflib` cutoff 0.86
     against canonical `'codylabahn'` scores ≈ 0.71–0.78 → rejected. Last-name
     fallback requires single-token input (`gear_sharing.py:233`). Two-token
     fallback requires len==2 (`gear_sharing.py:254`). With three tokens,
     **all fallbacks fail**. `resolve_partner_name` returns `'OP Cody Labahn'`
     unchanged. `partner_name` is set to that garbage string. Subsequent
     matching writes the entry with partner = `'OP Cody Labahn'`, which never
     resolves to a roster competitor in `pro_by_norm` — surfacing as
     `unknown_partner` in `build_gear_report` and producing **no heat
     constraint**.

2. **Event name actually stored under a different name in DB:** Only events
   whose `event.name` normalized contains `obstaclepole` get the `op` short
   code. The `Event.name` for the Obstacle Pole pro event is the literal
   string `'Obstacle Pole'` (`config.py:426`), so this is fine. But if a
   tournament was created with a different event name (e.g. `OP`, `OP Race`,
   `Pole Race`), `obstaclepole` is not in `combined`, and **no `op` short
   code is emitted**, so the parser cannot match. There is no integration
   test asserting `op` short-code emission across tournaments.

3. **College Obstacle Pole is silently dropped** (`gear_sharing.py:140-144`):

   ```python
   def is_no_constraint_event(event):
       st = str(getattr(event, 'stand_type', '') or '').strip().lower()
       if st in config.NO_CONSTRAINT_STAND_TYPES:
           return True
       event_type = str(getattr(event, 'event_type', '') or '').strip().lower()
       for fam in config.GEAR_FAMILIES.values():
           if st in fam['stand_types'] and fam.get('pro_only') and event_type == 'college':
               return True
       return False
   ```

   `config.GEAR_FAMILIES['op_saw'].pro_only=True` (`config.py:263`). For any
   college Obstacle Pole event, `is_no_constraint_event` returns True. The
   gear-completeness check (`gear_sharing.py:880`) skips the event entirely.
   `competitors_share_gear_for_event` would still match if the partner pair
   has an entry stored, **but** `build_gear_completeness_check` and the
   audit reports treat college OP as having no constraints. If a college
   competitor writes "SHARING OP Saw with X" the entry is parsed (assuming
   partner resolves) but the manager UI/printable report reflects "no
   constraint" for the event. The college user's intent is therefore not
   surfaced to the judge.

4. **Cookie Stack: the parser path works for the literal phrase but not for
   abbreviations.** `_short_event_codes` adds `cs` for cookie_stack; "SHARING
   Cookie Stack saw with Cody Labahn" has token `cs`? No — tokens are
   `{'sharing', 'cookie', 'stack', 'saw', 'with', 'cody', 'labahn'}` — `cs`
   is NOT a token. The literal alias `cookiestack` saves it. But the entry
   "SHARING Cookie Stack saw with Cody Labahn" still requires `Cody Labahn`
   to be detected. If misspelled (`Cody Lebahn`, `Cody Lebanon`, `Cody L.`)
   the same partner-resolution failure as OP Saw kills the entry.

### 4.3 Exact lines that drop these silently

- `services/gear_sharing.py:457` —
  `if not partner_name: warnings.append('partner_not_resolved'); return {}, warnings`
  The function exits BEFORE event matching when the partner cannot be inferred
  from the text. No warning is surfaced to the user about the unmatched
  equipment phrase.
- `services/gear_sharing.py:496` —
  `if not matched_any_event and not categories: warnings.append('events_not_resolved')`
  When the partner is detected but the event is not, the warning is emitted
  but the gear_map is empty — written to `competitor.gear_sharing` as `{}`,
  effectively dropping the entry.
- `routes/import_routes.py:315` —
  `if entry.get('gear_sharing'):` — when Q27 was answered No, the
  free-text is **never parsed** even when present. Saved as raw text in
  `gear_sharing_details` only.
- `services/gear_sharing.py:142` (and 140-144) — College Obstacle Pole entries
  are treated as no-constraint regardless of what the competitor wrote.
- `routes/registration.py:516-517` — `auto_parse_and_warn` failures swallow
  with bare `except Exception: pass`, no log:

  ```python
  except Exception:
      pass  # Gear parse failure should never block registration
  ```

### 4.4 Why the prompt's specific examples both fail

| Form text | Outcome (current code) |
|-----------|------------------------|
| `USING Jack and Jill saw with Karson Wilson` | If `Karson Wilson` is on roster → token-sequence partner detected → alias `jackandjill` (because event `Jack & Jill Sawing` has stand_type `saw_hand`, and `_event_name_aliases` emits `jackjill`, `jackandjill`, `crosscut`, etc.) matches → entry written. **Treated as a heat conflict between Karson and the writer**, even though it is partner confirmation — wrong domain semantics. |
| `SHARING Cookie Stack saw with Cody Labahn` | If `Cody Labahn` exact tokens match → partner resolves → alias `cookiestack` matches → entry written. Correct. If `Cody Labahn` is mis-spelled in the text or absent from roster → partner detection fails → first-segment fallback returns `OP Cody Labahn`-like garbage → `resolve_partner_name` cannot match three tokens → `partner_name` becomes garbage → entry stored with `unknown_partner` status, no heat constraint enforced. |
| `SHARING OP Saw with Cody Labahn` | Same partner-resolution problem when name is dirty. When partner DOES resolve, short code `op` matches, entry is written for the Obstacle Pole event id. So the bug for OP Saw is **partner resolution dependent on dirty form data**, not a vocabulary gap on `op` itself. |
| `SHARING Op saw, single saw with Cody` | "Cody" alone is a first-name only candidate. `resolve_partner_name` two-token fallback needs two tokens; bare `Cody` requires the single-token last-name fallback, which only matches if a competitor's normalized full name ENDS with `cody` (it doesn't here — `Cody` is the first name). → returns raw `Cody`. Token-sequence loop also fails because `Cody Labahn` requires both tokens to appear. The **first-name fallback in `services/registration_import.py:_fuzzy_resolve` lines 332-352 does exist** but is in the alternate import pipeline only — it is NOT reached by the manager-page parser or by the auto-parse-on-registration call. So `Cody` is dropped silently. |

---

## 5. Name matching audit

### Algorithm summary

`services/gear_sharing.py:147-289` covers the entire matching stack used by the
gear parser, the gear update routes, and the inline-edit AJAX endpoint:

- **Normalization:** `re.sub(r'[^a-z0-9]+', '', value.strip().lower())` — strips
  spaces, punctuation, accents (only ASCII letters/digits survive). Yields
  e.g. `"Jean-Luc O'Neill"` → `jeanlucone ill` → `jeanluconeill`.
- **Exact:** `name_index[normalized]` lookup.
- **Fuzzy:** `difflib.SequenceMatcher` ratio with `cutoff=0.86`. Two reject
  filters (suffix mismatch, divergent first names with same last name).
- **Bare last-name:** only when input is exactly one token and matches exactly
  one canonical via `endswith(last_norm)`.
- **Initial + last name:** when input is exactly two tokens, last names match,
  and first tokens compatible (1-2 chars matched by first letter, or ≥3 chars
  with prefix relationship in either direction).
- **Ambiguity guard** (`gear_sharing.py:215`): if multiple fuzzy candidates
  resolve to different canonicals, returns raw input rather than guessing.

`services/registration_import.py:_fuzzy_resolve` (line 304) adds a
**first-name-only** fallback (single token ≥4 chars matched by `startswith`)
that is **not** present in the gear_sharing.py matcher. Only invoked from the
enhanced-import pipeline.

### Predicted behaviour for the prompt's specific misspellings

| Form value | Roster has | gear_sharing.py result | Notes |
|------------|-----------|------------------------|-------|
| `Gillain Shannon` | `Jillian Shannon` (assumed) | Two tokens. `_names_token_compatible` checks: same last name (`shannon`); first names `gillain` vs `jillian` — neither prefix of the other; SequenceMatcher ratio ≈ 0.71. **Filter rejects** (`gear_sharing.py:72` requires ≥0.80). Returns raw. → DROPPED. |
| `Illiana Castro` | `Iliana Castro` (assumed) | Two tokens, same last name. First names `illiana` vs `iliana` — `iliana` is a prefix of `illiana` → `_names_token_compatible` returns True. `difflib` ratio ≈ 0.93 ≥ 0.86 cutoff → **resolves**. |
| `Quentin Lawrence` | `Quinn Lawrence` | Two tokens, same last name. First names `quentin` vs `quinn` — neither prefix; ratio ≈ 0.46 (q-u common, then divergent). **Filter rejects**. Returns raw → DROPPED. |
| `Eyeler Adams` | `Eyler Adams` | Two tokens, same last name. First names `eyeler` vs `eyler` — `eyler` is a prefix of `eyeler` (`eyler` ⊂ `eyeler`) — wait, actually neither is exactly a prefix; `eyler` (5 chars) starts `eyel`, `eyeler` (6 chars) starts `eyele`. `eyler.startswith('eyeler')` → False. `'eyeler'.startswith('eyler')` → True. So prefix passes. Then full ratio ≈ 0.91 ≥ 0.86 → **resolves**. |
| `Owen Vrendenburg` | `Owen Vredenburg` | Same first name `owen`, last names normalized `vrendenburg` vs `vredenburg`. **Last names must match exactly** at `gear_sharing.py:62` — `if a_last != b_last: return True` allows the pair to keep going, but the `difflib` step on the full name then runs. Full normalized strings: `owenvrendenburg` vs `owenvredenburg` (15 vs 14 chars, 1 char insertion). Ratio ≈ 0.97 ≥ 0.86 → **resolves**. |

The audit's verdict: typos with the same last name and a 1-character difference
are caught; typos with differing first names but identical last names are
caught when one is a prefix of the other; everything else falls through to raw,
producing an `unknown_partner` row in the manager.

### What happens to an unmatched name

Silent drop of the *intent*. The text is preserved on
`competitor.gear_sharing_details` but the structured `gear_sharing` JSON either
never gets the entry (when `partner_not_resolved`) or gets an entry whose
partner string never resolves to an active competitor. In `build_gear_report`
(`gear_sharing.py:1162-1224`) such rows surface in the **Unresolved Gear
Entries** table with status `unknown_partner` or `missing_partner`. They do
NOT surface in the Parse Review page (`build_parse_review`,
`gear_sharing.py:782`) unless the competitor still has unstructured
`gear_sharing_details` AND no entries at all in `gear_sharing` JSON. Once any
entry is written (even a garbage one), the parse-review row is hidden.

Logging — `gear_sharing.py:187,219,224,242,284`: every successful resolution
logs at INFO. Failures do not log — the function returns the raw input
silently.

---

## 6. Q27 ("Are you SHARING gear?" yes/no) handling

Q27 is sourced from the spreadsheet column `Are you sharing gear?`
(`pro_entry_importer.py:191`). Stored on the parsed entry dict as the boolean
key `gear_sharing`. The free-text column header starts with `'If yes, provide'`
(`pro_entry_importer.py:113`); stored as `gear_sharing_details`.

**The boolean is not persisted to the DB.** No column on `ProCompetitor` stores
Q27. The boolean is consulted in three places:

1. `routes/import_routes.py:315` — `if entry.get('gear_sharing'):` gates
   parsing during xlsx confirm. **Field present + Q27=No → free text saved
   to `gear_sharing_details` but NEVER parsed by `parse_gear_sharing_details`.**
   The judge has no UI prompt that this happened.
2. `services/pro_entry_importer.py:273-284` (`compute_review_flags`):
   - Q27=Yes + details blank → flag `'GEAR SHARING DETAILS MISSING'`.
   - Q27=Yes + details present + no category signal/likely no partner →
     flag `'GEAR SHARING DETAILS MAY BE AMBIGUOUS'`.
   - Q27=No + details present → no flag, no warning. **Inconsistency goes
     undetected at the import-review stage.**
3. `services/registration_import.py:880` (`_validate_gear_sharing` in the
   enhanced pipeline):
   - Q27=Yes + no records + no details → warning `gear sharing flag is Yes
     but no details provided`.
   - Q27=No + details present → warning `gear sharing flag is No but details
     text present`.
   - Q27=Yes + records OR Q27=No + records → `_reconcile_gear_flags`
     (`registration_import.py:943`) overrides Q27 to True.

   These warnings are surfaced **only** if the user runs the optional
   enhanced pipeline, which is not the default upload path.

The manual new-competitor route (`routes/registration.py:480`) does not have a
Q27 form field at all; the textbox is the only signal. The auto-parser at
`registration.py:511` runs unconditionally on the textbox.

Inconsistency surfacing: only the enhanced pipeline (`registration_import.py`)
produces the explicit warning. The standard xlsx import path silently keeps the
text and skips parsing. The manager UI does not display Q27 anywhere.

---

## 7. Degenerate input handling

| Input | Behaviour | Code reference |
|-------|-----------|----------------|
| `"SHARING SHARING Jack & Jill Saw with..."` | Both `SHARING` tokens are stripped by the partner-segment scrub regex (`gear_sharing.py:447`). Token-sequence partner detection ignores the duplicate. Event match for `jackandjill` succeeds. Entry written. |
| `"SHARING Double Buck Ben Lee"` (no "with") | Token-sequence partner detection: if `Ben Lee` is on roster, exact-token match succeeds and partner resolves correctly — `with` is not required. If `Ben Lee` is misspelled, first-segment fallback runs the entire text through the scrub regex; `double buck` is in the strip list, leaving `Ben Lee` (2 tokens) → `resolve_partner_name` runs and may match. Without `with`, parsing is still possible. |
| `"SHARING Double Buck wit Ben Lee"` (typo for "with") | Token-sequence detection on `Ben Lee` succeeds if on roster — typo on `wit` doesn't matter because partner detection scans for canonical-name token sequences anywhere in the text, not split around `with`. If `Ben Lee` not on roster, first-segment fallback strips `double buck`; `wit` is NOT in the strip list, so leftover is `wit Ben Lee` (3 tokens) → all fuzzy fallbacks require ≤2 tokens → returns raw → DROPPED. |
| Leading/trailing whitespace on tokens | `_name_tokens` (`gear_sharing.py:28`) and `normalize_person_name` (`gear_sharing.py:147`) both `.strip()` before processing; whitespace tolerated. |
| Mixed case (`sharing`, `Sharing`, `SHARING`) | `lowered = text.lower()` (`gear_sharing.py:408`) before any regex/match. `normalize_event_text` and `normalize_person_name` lowercase first. **No case sensitivity anywhere in the parser.** |
| Empty string | `gear_sharing.py:402-404`: `text = str(...).strip(); if not text: return {}, ['missing_details']`. |
| Only whitespace | Same as empty after `.strip()`. |
| Unicode names (e.g. `Müller`) | `normalize_person_name` regex `[^a-z0-9]+` strips non-ASCII letters. `Müller` → `mller`. Predictably mismatched against `Müller` on roster. **Silent drop.** |
| Nicknames (e.g. `Bri Kvinge` for `Brianna Kvinge`) | Two-token fallback (`gear_sharing.py:254-287`) handles this when `Bri` is a prefix of `Brianna`. |
| Group share text "I'm sharing with Tom and Jerry" | `text_tokens` finds first canonical that matches; only ONE `partner_name` is recorded (`gear_sharing.py:438-440`: `mentioned.sort(reverse=True); partner_name = mentioned[0][1]` — picks longest-name match). The second partner is silently dropped. No structured output preserves multi-partner sharing. |

---

## 8. Numbered bug / gap list

1. **USING vs SHARING semantically conflated** — `services/gear_sharing.py:388`. The parser has zero handling of either keyword; `sharing` is only a strip target. USING entries (partnered-event confirmation) get written into `gear_sharing` JSON the same way SHARING entries do, then `build_gear_conflict_pairs` (`gear_sharing.py:905`) treats them as cross-competitor heat constraints, spreading legitimately partnered competitors across heats. Severity: HIGH. Confidence: 9.

2. **`infer_equipment_categories` missing Cookie Stack, Obstacle Pole/OP, Speed Climb categories** — `services/gear_sharing.py:292`. Only emits `crosscut`, `chainsaw`, `springboard`. When the explicit per-event match fails, no category fallback exists for these events, so partner-detected entries with vague event language drop on the floor. Severity: HIGH. Confidence: 10.

3. **`_event_name_aliases` has no `cookie_stack` / `obstacle_pole` / `speed_climb` stand_type branch** — `services/gear_sharing.py:325-330`. `saw_hand`, `hot_saw`, `springboard` have explicit alias-expansion branches, but the other three do not. Cookie Stack and Obstacle Pole are matched only by their literal normalized name string; Speed Climb relies on event_name special-case alone. Common abbreviations (`OP`, `OP Saw`, `cookie saw`, `cookie chain`) only work via short codes (`op`, `cs`) which require standalone tokens. Severity: HIGH. Confidence: 9.

4. **Partner-segment scrub regex omits Cookie Stack, Obstacle Pole, OP, Speed Climb, Pole Climb, Cookie** — `services/gear_sharing.py:446-451`. When no separator triggers token-sequence detection, the fallback strips equipment words from the first segment, but these specific words remain. They bleed into the partner candidate, producing 3+ token strings that no fuzzy fallback can resolve, so partners silently default to garbage strings. Severity: HIGH. Confidence: 9.

5. **`resolve_partner_name` has no 3+ token fallback** — `services/gear_sharing.py:170-289`. Last-name fallback requires exactly one token; initial+last requires exactly two. Three-or-more tokens (common after the scrub regex bleed described in #4) skip every fallback and return raw, producing `unknown_partner` rows. Severity: MEDIUM. Confidence: 9.

6. **First-name-only fallback exists in `registration_import.py` but not in `gear_sharing.py`** — `services/registration_import.py:332-352` vs `services/gear_sharing.py:289`. Single first names like `Cody` are resolved by the enhanced pipeline but not by the auto-parse-on-registration call (`registration.py:511`), the manager batch parser (`gear_sharing.py:1387`), or the import path (`import_routes.py:316`). Inconsistent matching across pipelines. Severity: MEDIUM. Confidence: 9.

7. **Q27 (`gear_sharing` boolean) gates parsing in xlsx confirm only** — `routes/import_routes.py:315`. When Q27=No and the textbox has data, the text is saved to `gear_sharing_details` but `parse_gear_sharing_details` is never called. No flash, no audit, no warning in the manager UI. Competitors who answered No but typed sharing details are silently invisible. Severity: HIGH. Confidence: 10.

8. **Q27 boolean is not persisted to the DB** — `models/competitor.py:235-252` (`ProCompetitor`). There is no column for the Q27 answer; the only signal post-import is `gear_sharing_details`. Reconciliation flag overrides in the enhanced pipeline (`registration_import.py:_reconcile_gear_flags`) never reach the DB; they exist only in the in-memory `CompetitorRecord`. Severity: MEDIUM. Confidence: 9.

9. ~~**College Obstacle Pole gear constraints silently ignored**~~ — **WITHDRAWN 2026-04-19.** Re-reading `docs/Alex's Docs/GEAR_SHARING_DOMAIN.md:122` shows the COLLEGE Obstacle Pole entry says "OP saws are provided by the show ... No constraints on gear sharing." The current `is_no_constraint_event` behaviour (returning True for college obstacle_pole via `GEAR_FAMILIES['op_saw'].pro_only=True`) is therefore correct. Pro Obstacle Pole (line 248: "professional OP saws are procured by the competitor") still enforces constraints. Original audit conflated the pro and college entries. No fix required.

10. **First mentioned partner wins; multi-partner sharing dropped** — `services/gear_sharing.py:417-440`. `mentioned.sort(reverse=True); partner_name = mentioned[0][1]` picks the canonical with the longest normalized name and discards the rest. "Sharing Hot Saw with Tom and Jerry" stores only Tom; Jerry is invisible. Severity: MEDIUM. Confidence: 10.

11. **`auto_parse_and_warn` swallows all exceptions** — `routes/registration.py:516-517`. Bare `except Exception: pass`. Crashes inside the parser during registration leave the gear field unparsed with no log line and no flash. Severity: MEDIUM. Confidence: 9.

12. **No log line on `partner_not_resolved` or `events_not_resolved` warnings** — `services/gear_sharing.py:457,496`. Successful resolutions log at INFO; failures do not. The judge has no audit trail of which competitors had unparseable details. Severity: LOW. Confidence: 9.

13. **`parse_gear_sharing_details` exits before event matching when partner is empty** — `services/gear_sharing.py:457`. Even if the event match would have succeeded (e.g. clear `cookie stack` text but partner not yet on roster), the function returns early without recording the equipment intent. The text is preserved in `gear_sharing_details`, but the structured row never carries even a placeholder. Severity: MEDIUM. Confidence: 8.

14. **`_short_event_codes` `op` is dangerous on lowercased text** — `services/gear_sharing.py:343-344`. `op` is a two-letter token. Any text containing the standalone token `op` (e.g. someone using "op" as shorthand for "operation" or "opp" being mis-tokenized) silently matches Obstacle Pole. There is no entered-events disambiguation guard analogous to the springboard `sb` guard at `gear_sharing.py:484`. Severity: LOW. Confidence: 7.

15. **Cascade conflict pairs are event-blind** — `services/gear_sharing.py:905-996`. `build_gear_conflict_pairs` returns one set per competitor regardless of which event the share was declared for, so a chopping share creates a flight-builder adjacency penalty in chopping AND non-chopping events. Combined with the USING/SHARING conflation (#1), this propagates partner-confirmation rows into adjacency penalties everywhere. Severity: MEDIUM. Confidence: 8.

16. **Group-key conflict matching is exact-string** — `services/gear_sharing.py:539-542`. `'group:saw1'` matches only `'group:saw1'` — case-sensitive, whitespace-sensitive. Two pairs typed slightly differently (`'group:Saw 1'` vs `'group:saw1'`) produce two separate groups with no warning. Severity: LOW. Confidence: 8.

17. ~~**`build_parse_review` skips competitors who already have any `gear_sharing` entry**~~ — **WITHDRAWN 2026-04-19.** Re-reading `services/gear_sharing.py:980-994`: the function's only filter is `if not details: continue`. Competitors with `already_structured=True` ARE included in the review with a "Has existing entries — merge only" badge (`templates/pro/gear_parse_review.html:55`), and `pro_gear_parse_confirm` (`routes/registration.py:1042-1044`) merges the proposed map into the existing one rather than overwriting. Original audit mis-described the `already_structured` flag's effect. No fix required.

18. **`_FILLER_RE` (registration_import.py:384) treats `sharing` as noise, not a marker** — `services/registration_import.py:384`. Same conflation as #1, in a separate file. If/when the dirty parser becomes the canonical path, this regex must be replaced rather than extended. Severity: LOW. Confidence: 9.

19. **Gear flag reconciliation never reaches the DB from the standard import path** — `services/registration_import.py:943` (`_reconcile_gear_flags`). The override toggles `comp.gear_sharing_flag`, but `to_entry_dicts()` (`registration_import.py:1016`) writes `'gear_sharing': comp.gear_sharing_flag` into the entry dict, and `routes/import_routes.py:271` then writes `gear_sharing_details` only — the flag itself is consumed only by Q27 gating at line 315 of import_routes. So a dirty-file flag override only matters during the same import run; nothing persists. Severity: MEDIUM. Confidence: 8.

20. **`_parse_dirty_gear_text` output is never written to `ProCompetitor.gear_sharing` JSON** — `services/registration_import.py:390`. `CompetitorRecord.gear_sharing_records` is populated, surfaces in the import report, then discarded — the `to_entry_dicts()` round-trip drops it (it carries `gear_sharing_details` only). **Status 2026-04-19**: largely redundant after the gear-sharing fixes shipped on `fix/race-day-ui-fixes`: the standard `parse_gear_sharing_details` now handles dirty data via vocab-gap fixes (commit 8762fd0), USING/SHARING distinction (39b7ff8), and name-resolver fallbacks (770ccf0). The dirty parser still exists but its outputs are not load-bearing. Severity downgraded to LOW. Confidence: 8.

21. **Confirm step in `pro_gear_parse_confirm` cannot reject individual gear-map entries** — `routes/registration.py:1018-1027`. The checkbox is per competitor, not per `(event_key, partner)` tuple in the proposed map. A row with one good event match and one wrong event match must be accepted or rejected wholesale. Severity: LOW. Confidence: 9.

22. **`_classify_partner_value` doesn't recognize "USING" as a marker** — `services/registration_import.py:200-261`. Only the `_NEEDS_PARTNER_PATTERNS` list (TBD, IDK, ?, etc.) gets special handling. Any text starting with `USING` is treated as a partner name candidate, then resolved (likely to garbage). Severity: MEDIUM. Confidence: 9.

23. **Event name lookup in alias map does not include partnered axe events for axe-throw shares** — `services/gear_sharing.py:322-323`. Only `partneredaxethrow` and `axethrow` are added when event_name is `partneredaxethrow`. If display name uses different wording (e.g. `Pro Axe Throw`), aliases do not include it. Severity: LOW. Confidence: 7.

24. **Conditional sharing detection is parser-only, never persisted** — `services/registration_import.py:407-412`. `is_conditional` is recorded on the in-memory `CompetitorRecord`, surfaces as a warning in the import report, then is dropped. The `gear_sharing` JSON has no `conditional` flag. Heat builder cannot distinguish conditional from confirmed sharing. Severity: LOW. Confidence: 9.

25. **`is_no_constraint_event` walks `GEAR_FAMILIES.values()` on every call** — `services/gear_sharing.py:130-144`. Performance gap — invoked O(events × competitors × heats) during conflict checks. Not correctness. Severity: LOW. Confidence: 6.

---

## Summary of where the code drops user intent

- Section 4 confirms the OP Saw / Cookie Stack failure is **not** a missing
  short-code or alias for `op` — those exist. The actual silent-drop path is
  partner-name resolution failing on dirty form data, plus the Q27 gating in
  `import_routes.py:315` which never parses anything when the user answered No
  to "Are you sharing gear?"
- Section 2 confirms the USING/SHARING distinction does not exist anywhere in
  code; both keywords end up writing identical entries that the flight builder
  later interprets as cross-competitor sharing constraints.
- Section 7 confirms three-or-more-token fallbacks in name resolution are
  absent from `gear_sharing.py`. Only the alternate import pipeline has them,
  and that pipeline's structured records are never written back to the DB.
