# Missoula Pro Am Manager - Developer Documentation

## Overview

The Missoula Pro Am Manager is a Flask-based web application for managing the annual Missoula Pro Am timbersports competition. The competition runs over two days:

- **Friday**: College Competition (team-based with individual scoring)
- **Saturday**: Professional Competition (flight-based show format with payouts)

---

## Architecture

### Technology Stack

| Component | Technology |
|-----------|------------|
| Backend | Python 3.x / Flask 3.0 |
| Database | SQLite (via SQLAlchemy 2.0) |
| Migrations | Flask-Migrate 4.0.7 (Alembic) |
| Forms / CSRF | Flask-WTF 1.2.2 |
| Frontend | Jinja2 templates, Bootstrap 5 |
| Excel I/O | pandas, openpyxl |

### Project Structure

```
Missoula-Pro-Am-Manager/
├── app.py                 # Flask application entry point
├── config.py              # Configuration constants & event definitions
├── database.py            # Database initialization
├── requirements.txt       # Python dependencies
├── models/                # SQLAlchemy models
│   ├── tournament.py      # Tournament model
│   ├── team.py            # Team model (college)
│   ├── competitor.py      # CollegeCompetitor & ProCompetitor models
│   ├── event.py           # Event & EventResult models
│   ├── heat.py            # Heat & Flight models
│   ├── user.py            # User — 7-role auth model
│   ├── audit_log.py       # AuditLog — immutable audit trail
│   ├── school_captain.py  # SchoolCaptain — one PIN account per school per tournament
│   ├── wood_config.py     # WoodConfig — Virtual Woodboss per-tournament species/size config
│   ├── pro_event_rank.py  # ProEventRank — per-tournament ability rankings (9 categories)
│   └── payout_template.py # PayoutTemplate — reusable tournament-independent payout configs
├── routes/                # Flask blueprints
│   ├── main.py            # Dashboard & navigation
│   ├── registration.py    # Competitor/team registration; headshot upload
│   ├── scheduling/        # Blueprint package: heat/flight gen, heat swap, heat sheets, Friday feature, assign marks
│   │   ├── __init__.py    # scheduling_bp + shared helpers (defined before sub-module imports)
│   │   ├── events.py      # Event CRUD (setup, upsert, handicap toggle)
│   │   ├── heats.py       # Heat generation, swap, locking
│   │   ├── flights.py     # Flight scheduling
│   │   ├── heat_sheets.py # Heat sheet display & PDF
│   │   ├── friday_feature.py  # Friday Night Feature config
│   │   ├── show_day.py    # Flight status dashboard (60s auto-refresh)
│   │   ├── ability_rankings.py # ProEventRank UI
│   │   ├── preflight.py   # Pre-scheduling validation
│   │   └── assign_marks.py # Handicap mark assignment UI
│   ├── scoring.py         # Result entry & calculation; outlier flagging
│   ├── reporting.py       # Standings, reports, payout summary (with settlement), ALA report + email, cloud backup
│   ├── proam_relay.py     # Pro-Am Relay lottery system
│   ├── partnered_axe.py   # Partnered Axe Throw prelims/finals
│   ├── validation.py      # Data validation endpoints
│   ├── import_routes.py   # Pro entry xlsx importer (parse → review → confirm)
│   ├── auth.py            # Login, logout, bootstrap, user management, audit log viewer
│   ├── portal.py          # Spectator, competitor, school captain portals; user guide
│   ├── api.py             # Public read-only REST API (/api/public)
│   ├── strathmark.py      # STRATHMARK sync status page (/strathmark/status)
│   └── woodboss.py        # Virtual Woodboss — material planning (/woodboss); dual-blueprint (protected + public share)
├── services/              # Business logic
│   ├── excel_io.py        # Excel import/export; structured team validation errors
│   ├── heat_generator.py  # Heat generation algorithm
│   ├── flight_builder.py  # Flight scheduling with competitor spacing & stand conflict enforcement
│   ├── birling_bracket.py # Double-elimination bracket generator (fully functional)
│   ├── point_calculator.py# Point calculation utilities
│   ├── proam_relay.py     # Pro-Am Relay team building + manual team builder
│   ├── partnered_axe.py   # Axe throw scoring logic
│   ├── pro_entry_importer.py  # Google Forms xlsx import with duplicate detection
│   ├── registration_import.py # Enhanced import pipeline: dirty-file handling, fuzzy matching, cross-validation, report
│   ├── audit.py           # log_action() helper
│   ├── background_jobs.py # Thread-pool executor for async Excel export
│   ├── report_cache.py    # In-memory TTL cache for report payloads
│   ├── upload_security.py # Magic-byte validation, UUID filenames, scan hook
│   ├── logging_setup.py   # JSON structured logging; optional Sentry SDK
│   ├── sms_notify.py      # Twilio SMS; graceful no-op if not configured
│   ├── backup.py          # S3 or local SQLite backup
│   ├── woodboss.py        # Block/saw calculations, lottery view, history, HMAC share token, wood presets
│   ├── handicap_export.py # Chopping-event Excel export helpers
│   ├── partner_matching.py# Auto-partner matching for pro partnered events
│   ├── preflight.py       # Pre-scheduling validation checks
│   ├── gear_sharing.py    # Comprehensive gear-sharing: parse, audit, sync, conflict-fix, groups, batch ops
│   ├── schedule_builder.py# Day schedule assembly (Friday/Saturday blocks)
│   ├── scoring_engine.py  # Centralized scoring: positions, tiebreaks, throwoffs, payouts, outlier flagging
│   ├── strathmark_sync.py # STRATHMARK enrollment, result push, prediction residuals
│   ├── mark_assignment.py # Handicap mark calculation via STRATHMARK HandicapCalculator
│   └── cache_invalidation.py # Tournament cache invalidation helpers
├── static/                # Static assets
│   ├── js/onboarding.js   # First-time onboarding modal engine
│   ├── sw.js              # Service worker (offline cache + Background Sync)
│   └── offline_queue.js   # Offline queue UI (banner + manual replay)
└── templates/             # Jinja2 HTML templates
    ├── base.html          # Base layout (includes onboarding.js, Help nav button)
    ├── dashboard.html     # Main dashboard
    ├── role_entry.html    # Landing: Judge / Competitor / Spectator
    ├── auth/              # login, bootstrap, users, audit_log
    ├── portal/            # spectator, competitor, school_captain, user_guide
    ├── college/           # College competition templates
    ├── pro/               # Pro competition templates (+ import flow, gear_sharing, gear_parse_review, gear_sharing_print)
    ├── scoring/           # Score entry templates
    ├── scheduling/        # Heat/flight management; heat sheets print; friday feature; preflight
    ├── reports/           # Report templates (screen & print); payout settlement
    ├── reporting/         # fee_tracker, payout_settlement
    ├── proam_relay/       # Pro-Am Relay templates
    ├── partnered_axe/     # Partnered Axe templates
    ├── tournament_setup.html  # Consolidated setup: events + wood specs + settings tabs
    └── woodboss/          # Virtual Woodboss (dashboard, config, report, report_print, lottery, history)
```

---

## Data Models

### Tournament

The root entity representing a single year's competition.

```python
Tournament:
    id: Integer (PK)
    name: String           # "Missoula Pro Am"
    year: Integer          # 2026
    status: String         # 'setup' | 'college_active' | 'pro_active' | 'completed'
    college_date: Date     # Friday
    pro_date: Date         # Saturday
```

**Relationships:**
- `teams` → Team (one-to-many)
- `college_competitors` → CollegeCompetitor (one-to-many)
- `pro_competitors` → ProCompetitor (one-to-many)
- `events` → Event (one-to-many)

### Team (College Only)

```python
Team:
    id: Integer (PK)
    tournament_id: Integer (FK)
    team_code: String      # "UM-A", "MSU-B"
    school_name: String    # "University of Montana"
    school_abbreviation: String
    total_points: Integer  # Computed from competitor results
```

### Competitors

**CollegeCompetitor:**
```python
CollegeCompetitor:
    id: Integer (PK)
    tournament_id: Integer (FK)
    team_id: Integer (FK)
    name: String
    gender: String         # 'M' | 'F'
    events_entered: JSON   # List of event names
    partners: JSON         # Dict: event_name → partner_name
    gear_sharing: JSON     # Dict: event_id → partner sharing gear
    individual_points: Integer
    status: String         # 'active' | 'scratched'
```

**ProCompetitor:**
```python
ProCompetitor:
    id: Integer (PK)
    tournament_id: Integer (FK)
    name: String
    gender: String
    address: String
    phone: String
    email: String
    shirt_size: String
    is_ala_member: Boolean
    pro_am_lottery_opt_in: Boolean
    is_left_handed_springboard: Boolean
    events_entered: JSON
    partners: JSON
    gear_sharing: JSON     # Dict: event → partner sharing gear
    total_fees_owed: Float
    total_fees_paid: Float
    total_earnings: Float
    status: String
```

### Event & Results

```python
Event:
    id: Integer (PK)
    tournament_id: Integer (FK)
    name: String           # "Single Buck", "Obstacle Pole"
    event_type: String     # 'college' | 'pro'
    gender: String         # 'M' | 'F' | None (mixed)
    scoring_type: String   # 'time' | 'score' | 'distance' | 'hits' | 'bracket'
    scoring_order: String  # 'lowest_wins' | 'highest_wins'
    is_open: Boolean       # College: OPEN vs CLOSED event
    is_handicap: Boolean   # Championship (False) vs Handicap (True); underhand/standing_block/springboard Speed only
    is_partnered: Boolean
    partner_gender_requirement: String  # 'same' | 'mixed' | 'any'
    requires_dual_runs: Boolean  # True for Speed Climb, Chokerman's Race
    stand_type: String     # For heat generation
    max_stands: Integer
    has_prelims: Boolean   # True for Partnered Axe Throw
    payouts: JSON          # Pro only: {position: amount}
    status: String         # 'pending' | 'in_progress' | 'completed'

EventResult:
    id: Integer (PK)
    event_id: Integer (FK)
    competitor_id: Integer
    competitor_type: String
    competitor_name: String
    partner_name: String   # For partnered events
    result_value: Float    # Time/score/distance
    run1_value: Float      # For dual-run events
    run2_value: Float
    best_run: Float
    final_position: Integer
    points_awarded: Integer  # College
    payout_amount: Float     # Pro
    status: String
```

### Heat & Flight

```python
Heat:
    id: Integer (PK)
    event_id: Integer (FK)
    heat_number: Integer
    run_number: Integer    # 1 or 2 for dual-run events
    competitors: JSON      # List of competitor IDs
    stand_assignments: JSON  # {competitor_id: stand_number}
    flight_id: Integer (FK)  # Pro only
    status: String

Flight:
    id: Integer (PK)
    tournament_id: Integer (FK)
    flight_number: Integer
    name: String           # Optional custom name
    status: String
    notes: Text
```

---

## Key Services

### Flight Builder (`services/flight_builder.py`)

The flight builder creates an optimized schedule for pro competition that:

1. **Maximizes event variety** - Mixes different events within each flight
2. **Ensures competitor rest** - Maintains 5+ heats (minimum 4) between a competitor's appearances

**Algorithm:**
```
1. Collect all heats with their competitor lists
2. Use greedy selection:
   - For each position, score all remaining heats
   - Score based on minimum spacing since competitor's last appearance
   - Select heat with highest score
3. Group ordered heats into flights (default 8 heats/flight)
```

**Configuration:**
```python
MIN_HEAT_SPACING = 4   # Minimum heats between appearances
TARGET_HEAT_SPACING = 5  # Preferred spacing
```

### Heat Generator (`services/heat_generator.py`)

Generates heats for events using:
- Stand capacity constraints
- Snake draft for balanced heats
- Partner/gear-sharing conflict avoidance (same heat = conflict)

### Birling Bracket (`services/birling_bracket.py`)

Generates double-elimination brackets for birling events:
- Seeding based on previous results or random
- Winner's and loser's brackets
- Automatic advancement

---

## Event Configuration

Events are defined in `config.py`:

### College Events

**OPEN Events** (can compete across teams):
- Axe Throw
- Peavey Log Roll (partnered, mixed gender)
- Caber Toss
- Pulp Toss (partnered, mixed gender)

**CLOSED Events** (max 6 per athlete):
- Underhand Hard Hit / Speed
- Standing Block Hard Hit / Speed
- Single Buck / Double Buck
- Jack & Jill Sawing (partnered, mixed)
- Stock Saw
- Speed Climb (dual runs)
- Obstacle Pole (single run)
- Chokerman's Race (dual runs)
- Birling (bracket)
- 1-Board Springboard

### Pro Events

- Springboard / Pro 1-Board / 3-Board Jigger
- Underhand (M/F)
- Standing Block (M/F)
- Stock Saw (M/F)
- Hot Saw (M/F)
- Single Buck (M/F)
- Double Buck (partnered, M/F)
- Jack & Jill Sawing (partnered, mixed)
- Partnered Axe Throw (prelims → finals)
- Obstacle Pole
- Pole Climb (M/F, single run)
- Cookie Stack

---

## Scoring System

### College Points

Position-based points from `config.PLACEMENT_POINTS`:

| Position | Points |
|----------|--------|
| 1st | 10 |
| 2nd | 7 |
| 3rd | 5 |
| 4th | 3 |
| 5th | 2 |
| 6th | 1 |

Points contribute to:
- **Individual Total** → Bull/Belle of the Woods
- **Team Total** → Team Standings

### Pro Payouts

Configurable per-event payouts stored as JSON:
```python
event.payouts = {"1": 500, "2": 300, "3": 200, "4": 100}
```

---

## URL Routes

### Main
| Route | Description |
|-------|-------------|
| `/` | Dashboard |
| `/tournament/new` | Create tournament |
| `/tournament/<id>` | Tournament detail |
| `/tournament/<id>/setup` | Consolidated setup page (events/wood/settings tabs) |
| `/tournament/<id>/setup/settings` | Save tournament name/year/dates |
| `/tournament/<id>/college` | College dashboard |
| `/tournament/<id>/pro` | Pro dashboard |

### Registration (`/registration`)
| Route | Description |
|-------|-------------|
| `/<id>/college` | College registration |
| `/<id>/college/upload` | Upload Excel entry form |
| `/<id>/pro` | Pro registration |
| `/<id>/pro/new` | Add pro competitor |
| `/<id>/pro/gear` | Gear Sharing Manager (audit dashboard) |
| `/<id>/pro/gear/update` | Add/update a pro gear-sharing entry (POST) |
| `/<id>/pro/gear/remove` | Remove a pro gear-sharing entry (POST) |
| `/<id>/pro/gear/complete-pairs` | Write reciprocals for all one-sided pairs (POST) |
| `/<id>/pro/gear/auto-partners` | Copy gear entries into partner fields (POST) |
| `/<id>/pro/gear/cleanup-scratched` | Remove entries referencing scratched competitors (POST) |
| `/<id>/pro/gear/sync-heats` | Auto-fix gear conflicts in heats (POST) |
| `/<id>/pro/gear/parse-review` | Review proposed free-text parse results (GET) |
| `/<id>/pro/gear/parse-confirm` | Commit selected parse results (POST) |
| `/<id>/pro/gear/group/create` | Create a gear group (POST) |
| `/<id>/pro/gear/group/remove` | Remove a gear group (POST) |
| `/<id>/pro/gear/auto-assign-partners` | Auto-assign partners for partnered events (POST) |
| `/<id>/pro/gear/print` | Printable gear sharing report (GET) |
| `/<id>/college/gear/update` | Add/update a college gear-sharing entry (POST) |
| `/<id>/college/gear/remove` | Remove a college gear-sharing entry (POST) |

### Scheduling (`/scheduling`)
| Route | Description |
|-------|-------------|
| `/<id>/events` | Event list |
| `/<id>/events/setup` | Configure events |
| `/<id>/event/<eid>/heats` | View/manage heats |
| `/<id>/flights` | View flights |
| `/<id>/flights/build` | Build flights |

### Scoring (`/scoring`)
| Route | Description |
|-------|-------------|
| `/<id>/event/<eid>/results` | View/enter results |
| `/<id>/heat/<hid>/enter` | Enter heat results |
| `/<id>/event/<eid>/finalize` | Calculate positions |
| `/<id>/event/<eid>/payouts` | Configure payouts |

### Reporting (`/reporting`)
| Route | Description |
|-------|-------------|
| `/<id>/college/standings` | College standings |
| `/<id>/pro/payouts` | Payout summary |
| `/<id>/all-results` | All event results |
| `/<id>/export-results/async` | Background Excel export job |
| `/<id>/backup` | SQLite backup download (admin) |
| `/<id>/restore` | SQLite restore upload (admin) |
| `/<id>/pro/fee-tracker` | Pro entry fee collection tracker |
| `*/print` | Printable versions |

### Public API (`/api/public`)
| Route | Description |
|-------|-------------|
| `/tournaments/<id>/standings` | JSON standings payload |
| `/tournaments/<id>/schedule` | JSON schedule/heats payload |
| `/tournaments/<id>/results` | JSON completed event results |

---

## Development Setup

### Prerequisites

- Python 3.10+
- pip

### Installation

```bash
# Clone repository
git clone <repo-url>
cd Missoula-Pro-Am-Manager

# Install dependencies
pip install -r requirements.txt

# Run development server
python app.py
```

Server runs at `http://localhost:5000`

If your terminal is opened from the parent workspace folder instead of the repo
root, run `python Missoula-Pro-Am-Manager/app.py`. Local filesystem defaults
must remain repo-rooted regardless of the shell working directory.

### Database

The application uses Flask-Migrate (Alembic) for schema management.

To initialize a fresh database:
```bash
flask db upgrade
```

To apply new migrations after a model change:
```bash
flask db migrate -m "description of change"
flask db upgrade
```

To reset in development:
```bash
rm -rf instance/proam.db
flask db upgrade
```

The database file is created at the absolute repo path `instance/proam.db` for
SQLite (dev) or the URL from the `DATABASE_URL` environment variable
(production/Railway).

---

## Future Development Considerations

### Planned Features

1. **Live Results Display**
   - WebSocket-based real-time updates
   - Public-facing results page for spectators

2. **Mobile Score Entry**
   - Responsive design for tablet/phone entry
   - Offline capability with sync

3. **Historical Data**
   - Multi-year competitor tracking
   - Performance trends and records

4. **Entry Form Generator**
   - Generate blank Excel entry forms
   - Pre-populate with event lists

### Technical Debt

1. **Input Validation** - Continue hardening server-side validation and rejection messaging.
2. **Error Handling** - Expand recovery UX for failed background jobs and restore flows.
3. **Testing** - Add unit and integration tests for optimistic locking and transaction rollback (none exist).
4. **API expansion** - Public GET endpoints exist; authenticated write endpoints are future work.
5. **Friday Night Feature** - Route and config UI exist; heat generation and flight integration do not.
6. **Saturday overflow integration** - Priority flagging exists; pro flight integration does not.
7. **Pro event fee configuration UI** - No route/template for setting per-event fee amounts.
8. **Auto-partner assignment** - No logic to auto-match unpartnered pro competitors.

### Performance Optimization

- For large competitions (100+ competitors), consider:
  - Database indexing
  - Query optimization
  - Caching for standings calculations

---

## Configuration Reference

### `config.py` Key Settings

```python
# Application
SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-key-change-in-production')
SQLALCHEMY_DATABASE_URI = os.environ.get(
    'DATABASE_URL',
    'sqlite:///<repo>/instance/proam.db',
)
UPLOAD_FOLDER = '<repo>/uploads'
MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB

# Flask-WTF CSRF (enabled by default; uses SECRET_KEY)
WTF_CSRF_ENABLED = True

# Scoring
PLACEMENT_POINTS = {1: 10, 2: 7, 3: 5, 4: 3, 5: 2, 6: 1}

# Team constraints
MIN_TEAM_SIZE = 2
MAX_TEAM_SIZE = 8
MAX_CLOSED_EVENTS_PER_ATHLETE = 6
```

### Path Stability Rule

Any local-path default that is needed for development startup must resolve from
the project directory, not from the current working directory. This specifically
applies to:

- SQLite dev database path
- Alembic migrations directory
- Upload folder
- Local backup directory
- Instance config JSON files

Regression coverage lives in `tests/test_flask_reliability.py`.

### Stand Configurations

Each event type has defined stand configurations:

```python
STAND_CONFIGS = {
    'springboard': {'total': 4, 'supports_handedness': True},
    'underhand': {'total': 5},
    'saw_hand': {'total': 8, 'groups': [[1,2,3,4], [5,6,7,8]]},
    'obstacle_pole': {'total': 2},
    'speed_climb': {'total': 2},
    # ... etc
}
```

---

## Changelog

### 2026-04-21 (V2.12.1)

**Patch — clickable seed links in birling "not seeded" flash warning**

When a judge clicked "Print Birling Brackets" on the tournament overview before any bracket was seeded, the flash warning listed which events still needed seeding but the names were plain text — the judge had to hunt through the sidebar and event list to find each event's seeding page. This patch turns each event name in the warning into a direct `<a>` link to its `scheduling.birling_manage` page.

**`routes/scheduling/birling.py::birling_print_all`:**
- Imports `markupsafe.Markup` + `escape` (first use of Markup in this codebase — no new dependency; markupsafe is already a Flask transitive dep).
- `skipped` list now holds `Event` objects instead of pre-formatted display-name strings so we have access to `event.id` when building links. A `skipped_names` list is still assembled at the end solely for the `log_action` audit payload, preserving the existing contract.
- Nested helper `_seed_links(evts)` builds `<a href="/scheduling/<tid>/event/<eid>/birling" class="text-white fw-semibold text-decoration-underline">{escaped name}</a>` fragments. All user-controllable values (`display_name`) pass through `markupsafe.escape` *before* being concatenated into the `Markup` wrapper — no XSS surface introduced.
- Both branches emit a `Markup(...)` flash body: the "all ungenerated → 302" warning and the "some skipped alongside rendered" info flash.
- `text-white` + underline inline classes ensure the link is visible against the amber warning-toast background rendered by `templates/base.html` (Bootstrap's `.alert-link` is scoped to `.alert` elements and wouldn't apply to toasts).

**Tests — `tests/test_routes_birling_print.py::TestPrintAll` (+2):**
- `test_all_ungenerated_flash_contains_seed_links`: both unseeded events' `href` attributes appear in the warning-category flash body.
- `test_mixed_skip_flash_contains_seed_links`: the info-category skip flash (when at least one bracket IS seeded) also links the unseeded event's seeding page.
- Existing 9 route tests in the file remain unchanged — none asserted on flash body content, so the Markup switch didn't perturb them.

**Data model:** No schema changes.
**Tests:** 2942 passed, 9 skipped, 1 xpassed (+2 new regression tests for clickable flash).

---

### 2026-04-21 (V2.12.0)

**Minor — even flight distribution + between-flights drag-drop + print polish**

Saturday show building had two separate problems. The flight builder would stack all of one event's heats into one flight when their competitors didn't overlap with other events (reported as "all underhand in flight 1"), and there was no way to manually move a heat from one flight to another without rebuilding from scratch. Also, the printed heat-sheet borders were too faint to survive photocopy and the day schedule print leaked ink on decorative gray fills. This release fixes all three in one pass.

**Flight builder — per-event per-flight cap (`services/flight_builder.py`):**
- New `event_per_flight_cap = ceil(N_e / target_flights)` computed in `_optimize_heat_order`. Threaded through `_single_pass_optimize` (tracks `(block, event_id) → count` as heats are placed) and into `_calculate_heat_score` (applies `EVENT_FLIGHT_CAP_PENALTY = 2000` per heat over cap).
- Penalty is large enough to dominate the +1000 "first appearance" bonus and +500 springboard opener bonus combined, so crowd-variety distribution wins over local spacing optimization when a heat's competitors never appear in any other event. Before: greedy front-loaded same-event-heats in a row whenever all their competitors were new, because +1000 first-appearance crushed the +30 recency bonus. After: each event's heats spread evenly across flights.
- Mathematically `F × ceil(N_e / F) ≥ N_e` always, so a feasible distribution exists. Sequential heat-number order within each event is preserved (per-event queues remain FIFO).
- `_score_ordering` gets a matching `EVENT_FLIGHT_CAP_SCORE_PENALTY = 500` per over-cap heat so the multi-pass best-of-N comparison favors the better-distributed ordering.
- Verified on a 53-heat / 3-flight show mirroring the reported incident: every event at or under its cap, 18/18/17 flight sizes preserved, Women's UH distribution went from 4-0-0 to 1-1-2.

**Between-flights drag-drop (`routes/scheduling/flights.py`, `templates/pro/flights.html`):**
- New `POST /scheduling/<tid>/flights/bulk-reorder` endpoint. Accepts `{flights: [{flight_id, heat_ids}]}` — the full DOM snapshot. Server rewrites `flight_id` + `flight_position` atomically based on what the client sent. Refuses with 400 if the heat set in the payload doesn't exactly match the heats currently assigned to those flights (guards against a half-loaded DOM dropping heats).
- Frontend: each heat tile gets a grip handle (`.heat-drag-handle`) via `<i class="bi-grip-vertical">`. All three flight grids share `group: 'flight-heats'` so SortableJS allows drag between flights, not just within. On drop, the JS snapshots every visible flight's current state and POSTs to the bulk endpoint. Failed responses trigger a page reload so server state always wins. CSP nonce auto-injected by `_inject_csp_nonce` (inline `<script>` block requires no manual nonce attribute).
- Saw-block recompute hook fires automatically after the move (same pattern as the existing `reorder_flight_heats`), so saw heats rebalance their Block A/B assignments to the new sequence.
- 2 new integration tests in `tests/test_saw_block_integration.py`: happy-path cross-flight move verifies `flight_id` + `flight_position` update correctly for all three affected heats; mismatched-heat-set test verifies the 400 guard rejects incomplete payloads.

**Heat sheet print (`templates/scheduling/heat_sheets_print.html`):**
- `@media print` block: `.heat-card` border bumped from `1.5px solid #444` to `2.5px solid #000`. `.heat-card-head` bottom border from `1.5px solid #555` to `2.5px solid #000`. Pro/college left color bands from 4px to 6px (still navy/mauve). Each heat is now unambiguous on paper and survives photocopy + fax.

**Day schedule print (`templates/scheduling/day_schedule_print.html`):**
- Removed gray fill on `.slot-header` (was `#f1f1f1`) and `.heat-table thead th` (was `#fafafa`) — pure ink savings on every printed page.
- Section titles (Friday Day Show / Friday Showcase / Saturday Show) now use `3px solid #000` above + `1px solid #000` below + uppercase tracking. Unmistakable day break without filled banners.
- `.slot` card gets `1px solid #000` + `2px solid #000` below slot-title. Table rows use bottom-only `1px solid #000` hairlines (no top rules, no colored fills). `Heat N` label only prints on the first competitor row per heat (uses `{% if loop.first %}`) so scanning a column of 5 competitors in the same heat doesn't repeat the label 5 times.
- Bracket rosters bumped from 2 columns to 3 for tighter vertical use. Page-break rules: `.slot` avoids splitting, `.section-title` avoids orphaning.

**Regression coverage — `tests/test_flight_builder_integration.py::test_even_event_distribution_across_flights`:**
- 3 events × 4 heats × disjoint competitor pools × 3 flights. Asserts no flight exceeds `ceil(4/3) = 2` heats of any event. Would fail against the pre-fix greedy (Women's UH 4-0-0). Passes on the new algorithm (spread 1-1-2 or 2-1-1).

**Docs:**
- `FlightLogic.md` §3.4 rewritten: removed the "variety emerges naturally from competitor spacing" claim (only true when competitors overlap across events, which is exactly the case that broke), documented the new cap mechanic with constants and penalties.

**Data model:** No schema changes.
**Tests:** 2940 passed (+3 new regression guards). 98 flight-specific tests all green. 7 saw-block integration tests all green (2 new).

---

### 2026-04-21 (V2.11.3)

**Patch — design review fixes: touch targets, root font-family, print-mode brand fonts**

Three findings from a `/design-review` pass against V2.11.2. No functional changes; CSS + one template edit.

**F-001 — Touch targets below WCAG 2.2 SC 2.5.8 on secondary navbar + table buttons (HIGH):**
- `static/css/theme.css`: new `min-height: 44px` + `inline-flex` centering on `.navbar-proam .btn` (covers Help / Language / Users / Logout rendered as `.btn-outline-light.btn-sm` at ~31px previously). Also `.table .btn.btn-sm, table .btn.btn-sm` for inline row actions (View buttons, etc.).
- DESIGN.md §17.2 previously enforced 44px on `.btn-proam` and `.navbar-proam .nav-link` only. Product context §0 explicitly targets field-side tablets — the secondary toolbar buttons were outside that rule and failing on tablet taps. Six of seven flagged targets now at 44px; the seventh (Load Demo Data in dashboard card header) is an admin-only convenience, deferred.

**F-003 — Print stylesheets fall back to browser default serif (MEDIUM):**
- `static/css/theme.css` `@media print`: `body` font-family forced to Inter, `h1-h6` forced to Fraunces. DESIGN.md §18 reset the dark theme for print but left fonts unspecified, so printable outputs (heat sheets, schedules) rendered Times New Roman on judge/spectator handouts. Brand identity now carries into printed artifacts.
- `templates/scheduling/friday_feature_print.html`: standalone template (not using base.html), gets its own `<link>` to Google Fonts for Fraunces + Inter. Arial kept as fallback when offline.

**F-004 — `<html>` element inherited browser default serif (LOW):**
- `static/css/theme.css`: `html { font-family: "Inter", ... }` added alongside the existing `body` rule. No rendered elements affected today, but closes a design-system purity gap and protects against future edge cases where text lands outside `<body>`.

**Deferred to future work:** F-002 heading hierarchy sweep (H5 used for section headers across ~20 templates); F-005 `.data-label` bump in hero context; F-006 tournament detail stepper-vs-stats visual ambiguity.

**Data model:** No schema changes.
**Tests:** No tests added — CSS-only changes are caught by `/design-review` reruns per skill convention. Regression guard in `tests/test_css_modal_safety.py` continues to apply.

---

### 2026-04-21 (V2.11.2)

**Patch — Pro event fee configuration surfaced + tested**

`GET/POST /reporting/<tid>/pro/event-fees` and its template `reporting/event_fee_config.html` were already implemented (bulk-apply default fees to enrolled pro competitors, `overwrite` flag, per-event enrollment count + suggested fee), but the route had no sidebar link and no real tests — just one smoke hit in `test_route_smoke_qa.py`. CLAUDE.md still listed it as a gap. Patch makes the existing feature discoverable and verified.

**Sidebar — `templates/_sidebar.html`:**
- New "Event Fees" entry under Pro Day section, positioned immediately above the existing "Fee Tracker" link. `bi-cash-stack` icon. Active-route highlighting via `request.endpoint == 'reporting.pro_event_fees'`.

**Tests — 6 new in `tests/test_pro_event_fees.py`:**
- GET renders with event names + enrollment UI visible
- POST applies the fee to competitors enrolled in that event AND skips competitors not enrolled (even when their row would have received the form field)
- Blank fee field is a no-op (doesn't wipe or zero-out existing fees)
- Default behavior skips competitors who already have a non-zero fee for that event
- `overwrite=on` replaces existing non-zero fees
- Invalid fee input ("twenty bucks") flashes an error but returns 302 — no 500

**Docs — CLAUDE.md:**
- Moved Pro Event Fee Configuration from Section 5 "Known Gaps" to "Features Functionally Complete"
- Dropped from Section 8 remaining-gaps list

**Data model:** No schema changes.

---

### 2026-04-21 (V2.11.1)

**Patch — Friday Night Feature PDF export**

Adds a PDF download alongside the existing HTML-print view for the FNF schedule. Uses the shared `services/print_response.py::weasyprint_or_html` helper so the route returns a real PDF when WeasyPrint is installed, or falls back to HTML (with `Content-Type: text/html`) on environments without cairo/pango — which includes Railway.

**New route — `routes/scheduling/friday_feature.py`:**
- `GET /scheduling/<tid>/friday-night/pdf` renders the same `scheduling/friday_feature_print.html` template as `/friday-night/print` and pipes it through `weasyprint_or_html()`. Same schedule data, same template, just different response wrapping — print and PDF stay in sync automatically.
- Download filename derived from tournament name + year: `{name}_{year}_friday_night_feature.pdf`.

**Template — `templates/scheduling/friday_feature.html`:**
- New "PDF" button next to the existing "Print FNF Schedule" button on the Friday Showcase page, visible only when `fnf_schedule` has content. Uses `bi-file-earmark-pdf` icon.

**Tests — 2 new in `tests/test_one_click_and_fnf.py::TestFridayFeaturePdfRoute`:**
- `test_pdf_route_renders_200` — asserts 200 with Content-Type either `application/pdf` or `text/html` (matches both the WeasyPrint branch and the fallback).
- `test_pdf_route_pdf_branch_sets_download_header` — `monkeypatch`es a fake `weasyprint` module to force the PDF branch, then asserts `Content-Type: application/pdf`, `Content-Disposition: attachment; filename="..._friday_night_feature.pdf"`, and that the response body is the fake PDF bytes. This guarantees the PDF branch is actually exercised by CI even without WeasyPrint installed.

**Data model:** No schema changes.

---

### 2026-04-21 (V2.11.0)

**Minor — one-click Saturday show build + Friday Night Feature schedule**

Single button on the Pro Flights page that builds the entire Saturday show, plus a complete Friday Night Feature schedule view and printable layout.

**New — `POST /scheduling/<tid>/flights/one-click-generate`:**
- Runs `_generate_all_heats` → `build_pro_flights` → `integrate_college_spillover_into_flights` in one transaction.
- Flashes per-step progress, redirects back to the Flights page.
- Button uses the app's `data-confirm` / `data-confirm-danger` attribute pattern (Bootstrap modal via `base.html` delegated handler) — NOT inline `onsubmit`, which the app's strict CSP blocks.

**New — Friday Night Feature schedule view:**
- Friday Showcase page (`/scheduling/<tid>/friday-night`) now renders a heat-by-heat schedule table below the config form. Each row: slot, event, heat + run, competitors with stand assignments, inline Score link. FNF runs as a straight schedule like college day, not flights.
- Events ordered Springboard → Pro 1-Board → 3-Board Jigger via `_fnf_event_order()`.
- `_build_fnf_schedule(tournament, eligible_events, fnf_config)` builds the data; shared between the page and print view.

**New — printable FNF schedule:**
- `GET /scheduling/<tid>/friday-night/print` renders `templates/scheduling/friday_feature_print.html`: event blocks, rowspan-merged heat cells, competitor-by-stand rows, optional notes banner, header/footer with generation timestamp.
- Print button uses `id="fnf-print-btn"` + inline `<script>` block (auto-nonced by `app.py:_inject_csp_nonce`) with `addEventListener('click', window.print)` — CSP-compliant.

**`services/flight_builder.py` changes:**
- `build_pro_flights()` reads `tournament.get_schedule_config()['friday_pro_event_ids']` and excludes those event IDs from Saturday flight building. Their heats stay in the DB with `flight_id=NULL` so the FNF schedule view can find them. Handles malformed config gracefully.
- `MIN_HEATS_PER_FLIGHT = 2` clamp: if caller-supplied `num_flights` would produce fewer than 2 heats per flight, `target_flights` is reduced to `ceil(total_heats / 2)`.
- `contains_lh` flag scoped to springboard heats only. Prior code flagged ANY heat containing a left-handed-springboard competitor (including Obstacle Pole, Cookie Stack, etc.) as "LH-containing," producing false-positive `LH DUMMY CONTENTION` warnings. The LH dummy is a physical springboard stand; only springboard heats contend for it.

**Regression tests — `tests/test_one_click_and_fnf.py` (12 tests):**
- `TestFNFExclusion` — 3 tests
- `TestMinHeatsPerFlightClamp` — 2 tests
- `TestOneClickGenerateRoute` — 2 tests
- `TestBuildFnfSchedule` — 4 tests
- `TestFridayFeaturePrintRoute` — 1 test (includes CSP regression guard — asserts no `onclick=` or `onsubmit=` in rendered body)

All 95 flight-builder + FNF tests pass.

**Data model:** No schema changes. Uses existing `schedule_config` JSON column on `Tournament`.

---

### 2026-04-21 (V2.10.1)

**Patch — friendly redirect on expired CSRF tokens**

Fixes a silent-failure bug where long-open forms (CSRF time limit defaults to 1 hour) submitted a stale token, got a raw `400 Bad Request` page, and left the user thinking the button was broken. The "Integrate Spillover" button on the scheduling page was the first reported case, but the bug affected every CSRF-protected form in the app.

**New error handler — app.py:**
- `@app.errorhandler(CSRFError)` catches missing/expired CSRF tokens from Flask-WTF.
- HTML routes: flash `"Your session expired for security. Please try that action again."` and 302-redirect to `request.referrer` (or `request.path` as fallback). The next GET picks up a fresh token, so one more click works.
- `/api/` routes: return `{"error":"CSRF token missing or expired","status":400}` JSON for programmatic clients.

**Regression tests — tests/test_csrf_error_handler.py (2 new):**
- Verifies HTML route redirects to referrer with the correct warning flash.
- Verifies the fallback when the browser omits `Referer` (redirects to `request.path`).
- Uses a fixture that re-enables CSRF protection for the test app (the global `conftest.py` disables it for every other test).

**Data model:** No schema changes.

---

### 2026-04-21 (V2.10.0)

**Minor — hand-saw stand block alternation**

Alternates the two physical saw-stand blocks (A = stands 1-4, B = stands 5-8) across consecutive hand-saw heats in each day's run order. The next team can set in on the opposite block while the current team runs. Applies to Single Buck, Double Buck, and Jack & Jill Sawing — all pro and college, all genders. Friday and Saturday each start fresh on Block A.

**New service — services/saw_block_assignment.py:**
- `assign_saw_blocks(tournament)` iterates Friday then Saturday in authoritative run order, flips Block A↔B on every heat with `stand_type=='saw_hand'`, skips non-saw heats while preserving alternation state. Idempotent — commits on success, rolls back and re-raises on failure.
- `remap_heat_to_block(heat, target_block)` remaps a heat's `stand_assignments` JSON onto the 4 target-block stand numbers, preserving pair-sharing structure for partnered events (both partners keep the same stand number). Never changes heat composition.
- `trigger_saw_block_recompute(tournament)` shared hook wrapper — calls `assign_saw_blocks`, logs result, flashes a warning on exception. Hook failures never roll back primary commits.

**Authoritative run-order helpers — services/schedule_builder.py:**
- `get_friday_ordered_heats(tournament)` returns all Friday college heats (run_number=1) in authoritative order: respects `schedule_config['friday_event_order']` when set, else falls back to `_college_friday_sort_key`. Excludes Saturday spillover events and Friday-Feature events.
- `get_saturday_ordered_heats(tournament)` returns Saturday heats: iterates `Flight.flight_number` ascending then `flight.get_heats_ordered()` when flights exist, else falls back to pro events by event_id + heat_number. Includes day-split Run 2 heats (Chokerman, Speed Climb).
- Both are pure reads with no side effects.

**Route hooks — routes/scheduling/{heats,events,flights}.py:**
- Wired into 9 mutation endpoints: `generate_heats` (single event), `generate_college_heats` (bulk), `event_list` POST actions (`generate_all` / `rebuild_flights` / `integrate_spillover`), `build_flights` POST, `reorder_flight_heats`, `reorder_friday_events`, `reset_event_order`. Hooks run AFTER the route's primary commit; exceptions inside the hook log a warning and flash but never roll back the primary mutation.

**Admin safety valve + status page — routes/scheduling/heats.py, templates/scheduling/saw_blocks_status.html:**
- `POST /scheduling/<tid>/heats/recompute-saw-blocks` manually re-runs `assign_saw_blocks` with flash feedback (`N heats updated (X Friday, Y Saturday)`). Redirects to `request.referrer` or the events page.
- `GET /scheduling/<tid>/saw-blocks-status` renders two tables (Friday + Saturday) showing every hand-saw heat in run order with Block A/B badge, stands used, and competitor names. Empty-state message per day when no saw heats.

**Sidebar — templates/_sidebar.html:**
- New "Saw Block Status" entry under Run Show section, visible only when the tournament has at least one event with `stand_type=='saw_hand'`.

**Tests — 29 new tests, zero regressions:**
- `tests/test_schedule_builder_ordering.py` (10): custom + default Friday order, heat_number within event, Saturday spillover exclusion, Run 2 exclusion on Friday, Saturday flights-mode + fallback + custom-order + dual-run Run 2 inclusion, pure-reads guarantee.
- `tests/test_saw_block_assignment.py` (10): within-event alternation, cross-event Friday continuity, non-saw gap preservation, day-boundary reset, partnered pair preservation, Jack & Jill mixed-gender, idempotency, flight-reshuffle recompute, Stock Saw untouched, HeatAssignment sync after remap.
- `tests/test_saw_block_integration.py` (5): generate_heats triggers recompute, build_flights triggers recompute, reorder_flight_heats triggers recompute, reorder_friday_events triggers recompute, hook failure does not break primary mutation.
- `tests/test_saw_blocks_admin.py` (4): recompute POST route, status page render, empty-state rendering, sidebar conditional visibility.

**Data model:**
- Zero schema changes. `Heat.stand_assignments` JSON remains authoritative; `HeatAssignment` rows synced via `heat.sync_assignments()` after every remap. `STAND_CONFIGS['saw_hand']['labels']` unchanged — heat sheets, judge sheets, and the kiosk continue to render raw stand numbers.

**Design docs — docs/SAW_STAND_ALTERNATION_RECON.md, docs/SHOWPREP_WORKFLOW_RECON.md:**
- Full recon on existing stand-assignment mutation points, the show-prep workflow, and authoritative run-order sources. Documents the 9 hook locations and open design questions answered before implementation.

---

### 2026-04-19 (V2.9.1)

**Patch — gear-sharing parser overhaul + modal stacking fix + race-day UI hardening**

Closes the reported bug ("OP Saw and Cookie Stack SHARING entries don't appear in the gear-sharing module") and a BLOCKER-class domain bug (USING vs SHARING semantically conflated, splitting partnered pairs across heats).

**Reported bug fix — Cookie Stack / OP Saw populate (services/gear_sharing.py):**
- Partner-segment scrub regex (`parse_gear_sharing_details`) now strips USING/SHARING and the previously-missing Cookie Stack / Obstacle Pole / Speed Climb / climbing-gear vocabulary. Dirty form input like `SHARING OP Saw with Cody Labahn` now reduces to `Cody Labahn` instead of leaving `OP Cody Labahn` (3 tokens that no fuzzy fallback could resolve).
- `_event_name_aliases` gains stand_type branches for `cookie_stack`, `obstacle_pole`, and `speed_climb`, matching the existing pattern for `saw_hand`/`hot_saw`/`springboard`.
- `infer_equipment_categories` emits new categories `op_saw`, `cookie_stack`, `climbing` alongside crosscut/chainsaw/springboard. `_CATEGORY_KEYS` and `event_matches_gear_key` updated coherently.

**USING vs SHARING distinction (services/gear_sharing.py):**
- New form keyword `USING` (e.g. `USING Jack and Jill saw with Karson Wilson`) is now recognized as partnered-event confirmation, NOT a heat constraint. Stored with `using:` prefix on the partner-name value (`{"20": "using:Karson Wilson"}`); fully backward-compatible with existing entries (no DB migration needed).
- `competitors_share_gear_for_event`, `build_gear_conflict_pairs`, and `sync_all_gear_for_competitor` all skip USING-prefixed values so the flight builder no longer splits Jack & Jill partners across heats they should be in together.
- Misuse like `USING Hot Saw with X` falls back to SHARING because Hot Saw is non-partnered.
- Two new helpers: `is_using_value(v)` and `strip_using_prefix(v)`.

**Name resolver fallbacks for dirty form data (services/gear_sharing.py::resolve_partner_name):**
- Single-token fallback now combines last-name and first-name candidates into one ambiguity check. `Cody` with one Cody on the roster resolves; `Cody` with two Codys (or `Cody` + `Tom Cody`) returns raw. First-name path requires 4+ chars to avoid resolving short English-word collisions like `she` → `Shea Warren`.
- New 3+ token sliding-window fallback. Dirty leftovers like `OP Cody Labahn` (where the partner-segment scrub missed a word) now slide a 2-token window across the input and re-run the resolver. Accepts only when every successful slice resolves to the same canonical.
- `parse_gear_sharing_details` now iterates EVERY comma/semicolon segment instead of only the first, closing `SHARING Op saw, single saw with Cody` (partner in second segment).

**Q27 reconciliation + parser visibility (routes/import_routes.py, routes/registration.py, services/gear_sharing.py):**
- The xlsx import now always parses `gear_sharing_details` when text is present, regardless of Q27 ("Are you sharing gear?"). Q27/text mismatches (Yes-but-empty AND No-but-text) get tracked separately and surfaced in the import summary flash and audit log.
- `auto_parse_and_warn` exception handler in `new_pro_competitor` no longer swallows crashes silently; logs at WARNING with `exc_info=True` and writes a `gear_parse_error` audit log entry. Gear parsing remains non-blocking.
- `parse_gear_sharing_details` now logs at INFO when emitting `partner_not_resolved` or `events_not_resolved` warnings. Source text truncated to 200 chars.

**Modal stacking bug — every modal in the app was unclickable (static/css/theme.css):**
- The page-fade-in animation on `#main-content` kept the element on its own composited GPU layer in Chrome (the compositor promotes animated elements). That implicit layer acted as a stacking context, trapping Bootstrap modals at z-index 1055 BELOW the modal-backdrop at z-index 1050 attached to body. The X / Cancel / Save buttons on EVERY modal in the app were unclickable for real users (only Escape worked).
- Hit-test before: `elementFromPoint(X-button center)` → `DIV.modal-backdrop`. Hit-test after: `BUTTON.btn-close`. Verified live on the gear sharing edit modal, the delete tournament modal, and the confirm modal.
- Fix: removed the `@keyframes page-fade-in` block AND both `#main-content { animation: page-fade-in ... }` rules entirely. The 0.18-0.2s fade was cosmetic polish; clickable modals are not.
- Initial fix in `c50ceb2` was incomplete (only removed `transform` from keyframes); QA caught the residual issue and the final fix landed in `488d912`.

**Static asset cache-busting (templates/base.html, app.py):**
- `theme.css` link now appends `?v={mtime}` cache-bust token via new `_static_version_token(app)` helper exposed as `STATIC_VERSION` to all templates. Without this, every CSS-only fix on this app would have the same trap for real users (browser keeps cached CSS, fix appears to work for the developer who hard-refreshed during testing, production users stay broken).

**Regression guards (tests/test_css_modal_safety.py, CLAUDE.md):**
- New `tests/test_css_modal_safety.py` parses `theme.css` and asserts no rule on a Bootstrap-modal-ancestor selector (`#main-content`, `main`, `html`, `body`, `.d-flex`) declares any compositor-promoting property (animation, transform, filter, opacity, etc.) with a non-safe value. Verified the test catches the regression by injecting both the original transform and the keyframes-with-transform pattern.
- CLAUDE.md gains a `### CSS Verification Protocol (MANDATORY)` section that names the failure mode in plain language, gives a 6-step verification procedure, and explicitly bans `link.href = '...?v='+Date.now()` reloads as final verification.

**Test suite fixes (tests/test_fuzz_scoring.py, tests/test_registration_import.py):**
- Three pre-existing tests asserted the old (broken) behaviour; updated to reflect the fix:
  - `TestInferEquipmentCategoriesFuzz::test_no_match` now uses `axe throw` (no equipment vocab) for the empty-categories case; `obstacle pole` correctly emits `op_saw` now.
  - `TestFuzzyMatching::test_first_name_only` accepts either `FIRST-NAME` or `FUZZY MATCH` log entry — the upstream `gear_sharing.resolve_partner_name` now resolves "Henry" earlier via fuzzy match.
  - `TestFuzzyMatching::test_short_name_no_match` continues to pass after bumping the single-token first-name threshold from 3 to 4 chars.

**Gear-sharing audit document (docs/GEAR_SHARING_AUDIT.md):**
- New 828-line read-only audit covering parser anatomy, event/gear mapping, name-matching algorithms, Q27 interaction, degenerate input handling, and a numbered list of 25 bugs/gaps. Items #9 and #17 withdrawn after re-verification (audit was wrong); item #20 downgraded after the fixes shipped on this branch made the alternate parser largely redundant.

**Test coverage:** 2730 passed, 9 skipped, 1 xpassed. 235 gear-sharing tests including 13 new USING/SHARING cases, 11 vocabulary-fix cases, plus the 3 new modal-safety guards.



**Minor release — race-day integrity, birling rebuild, judge sheet, day-schedule audit**

Rolls up everything committed since V2.8.3 on 2026-04-11. Individual PRs carry the full rationale; this entry is a release-level index.

**Features:**
- **Race-Day Integrity (PR #22, commit `5be5e04`).** Scratch-cascade service (`services/scratch_cascade.py`), relay payout finalization, judge-facing ops dashboard (`templates/ops_dashboard.html`). Scratches now propagate through heats, flights, and standings in one transaction.
- **Birling double-elimination rebuild (commit `5efd74f`).** Losers-bracket fix, fall recording, per-match undo, placement points. Full rebuild of `services/birling_bracket.py` plus bracket state on heat-sheet print.
- **Judge Sheet feature (PR #7 via merge `bed2bc3` + commit `e9762fa`).** DQ status + `status_reason` TEXT column on `event_results` (migration `a1b2c3d4e5f8`), blank scoring-sheet PDF download, judge-facing entry UI. Full service module at `services/judge_sheet.py`.
- **Day-schedule audit + manual event ordering (PR #23, commit `5820039`).** Drag-and-drop event reordering on the Events & Schedule page; schedule audit exposes per-event sequencing problems before heat generation.

**Fixes:**
- **Woodboss 1-board / 2-board / 3-board springboard split (commit `a13c62c`).** Separates the three springboard dummy classes in the Wood Count Report so each gets its own row and count. Fixes double-counting across board heights.
- **Tournament-scope audit + QA harness integration (commit `1365da4`).** Scheduling actions now log tournament-scoped audit entries; QA harness hooks validate integrity after each mutation.

**Docs:**
- CLAUDE.md Section 5 + 8 — retired stale STRATHMARK gap bullets (commit `304cd17`). The `_get_handicap_calculator()` / `_fetch_start_mark()` bugs described as open were actually fixed by PR #6 (V2.7.0) months ago; main's code correctly uses `ollama_url` / `wood_df` / `results_df` and calls `calculator.calculate()`. Branch `feat/strathmark-v2.7-deploy-wiring` deleted local + remote (was the pre-squash source of PR #6).

### 2026-04-11 (V2.8.3)

**Virtual Woodboss audit sweep — all HIGH/MEDIUM/LOW findings resolved except M7/M8 (deferred)**

**HIGH resolved:**
- **H1 — `apply_preset` wiped existing `size_value`.** `apply_preset` used to write any field present in the preset dict; `build_preset_from_*` stored `None` when the user left diameter blank, so applying a species-only preset nulled out the target tournament's diameters. New `_apply_spec_to_row()` helper skips any field whose value is `None`. Regression test: `TestPresetRoundtrip::test_apply_preset_does_not_wipe_existing_diameter`.
- **H2 — `save_config` couldn't clear a row.** Blanking every field on an existing row silently skipped the update, stranding old values. `save_config` now writes `None` through when the DB row already exists, and still skips inserts for new-and-empty rows. Flashes a "cleared" count separate from the "updated" count. Regression test: `TestSaveConfigClear::test_blanking_existing_row_clears_it` (uses a unique test judge + test client session cookie to hit the real view).
- **H3 — `delete_preset` route had no UI.** [config_form](routes/woodboss.py) now passes `custom_preset_names` to the template; [templates/woodboss/config.html](templates/woodboss/config.html) renders a "Manage custom presets" row of delete buttons wired to the existing `delete_preset` route. Built-ins aren't shown.

**MEDIUM resolved:**
- **M1 — block presets stamped a single species on every category.** Preset format is now V2-aware: new `blocks_by_key` dict (`{cfg_key: spec}`) lets one preset cover different species for College M / College F / Pro / Pro 3-Board Jigger / etc. The V1 `blocks` single-dict form is kept as a backwards-compatible broadcast fallback. `build_preset_from_form` and `build_preset_from_config` emit both. Regression test: `TestPresetRoundtrip::test_apply_preset_per_cfg_key_support`.
- **M2 — `log_relay_doublebuck` wasn't in presets.** New constant `_LOG_PRESET_KEYS = (LOG_GENERAL_KEY, LOG_STOCK_KEY, LOG_OP_KEY, LOG_COOKIE_KEY, LOG_RELAY_DOUBLEBUCK_KEY)` drives both apply and build. Regression test: `TestPresetRoundtrip::test_apply_preset_includes_log_relay_doublebuck`.
- **M3 — relay `size_unit` fallback gated on `size_value`.** `calculate_saw_wood` now gates on `relay_db_cfg.size_unit in ('in', 'mm')` instead.
- **M4 — `count_override` inconsistent handling.** Relay branch in `calculate_blocks` now uses `is not None` matching the non-relay branch.
- **M5 — `WoodConfig.size_unit` missing `server_default`.** Now declares `server_default=sa.text("'in'")`. Already in `KNOWN_SERVER_DEFAULT_DRIFT` allowlist so no new migration needed.
- **M6 — preset file write was not atomic.** New `_write_preset_file()` writes to `wood_presets.json.tmp` then `os.replace()`. New `_load_preset_file()` logs a warning via `logging.getLogger(__name__)` on `JSONDecodeError` rather than silently swallowing. Regression test: `TestPresetRoundtrip::test_atomic_write_survives_simulated_crash`.

**MEDIUM deferred (no user-facing impact today):**
- **M7** — preset routes don't revalidate tournament ownership. Kept as-is until multi-tenant lands.
- **M8** — no rate-limit on public share route. Separate concern from the audit; file under general API rate-limiting work.

**LOW resolved:**
- **L1** — negative `count_override` silently swallowed → now flashes a warning listing affected cfg_keys.
- **L3** — `apply_preset` created ghost rows for zero-enrollment categories → already gated on `_active_block_keys(tournament_id)` and followed by `prune_stale_block_configs` (added in the H0 pass).
- **L4** — `save_custom_preset` now raises `ValueError` on built-in name collision; route catches and flashes. Regression test: `TestPresetRoundtrip::test_save_rejects_builtin_name_collision`.
- **L5** — Friday Feature JSON IO lifted out of `calculate_springboard_dummies` into `_detect_friday_feature_springboard(tournament_id)` helper. Math function is now pure(r).
- **L6** — Removed `_PRESET_FILE` module global; `_preset_path()` is a plain function that computes the path each call. Safer under pytest parallelism.

**LOW deferred:**
- **L2** — `is_gendered` list omits `jack & jill`. Intentional — J&J is mixed-gender-partnered and collapses to `'open'`.

**Collateral fixes:**
- `CollegeCompetitor.closed_event_count` ([models/competitor.py](models/competitor.py)) is now a dual ID+name resolver. The V2.8.2 fix broke the `test_counts_closed_events_only` test which had encoded the original bug (stored IDs and the model compared against names). The resolver now builds both sets and matches either form, so test fixtures using IDs and real-world competitors using names both work. Previously this property silently returned 0 for every real competitor, so the 6-CLOSED-events enforcement had never run.
- [tests/test_route_smoke.py](tests/test_route_smoke.py) fixture: added `None` guards on `first_heat`, `first_event`, `first_user` so the fixture doesn't `AttributeError` on partially-seeded test DBs. Woodboss smoke tests (12) now run clean.

**Test coverage added:** `tests/test_woodboss.py` grew three classes — `TestCollegeEnrollmentByName` (H0 regression), `TestPresetRoundtrip` (H1/M1/M2/M6/L4 + roundtrip), `TestSaveConfigClear` (H2). 40 woodboss tests, 218 tests across the woodboss-adjacent suites, all passing.

Audit doc with per-finding status table: [docs/WOODBOSS_AUDIT.md](docs/WOODBOSS_AUDIT.md).

### 2026-04-10 (V2.8.2)

**Patch — Woodboss college enrollment lookup + save-preset form-data capture**

**Virtual Woodboss (`services/woodboss.py`, `models/competitor.py`):**
- **Fixed: college blocks were entirely missing from the Wood Count Report (By Species view).** Discovered the day before block turning. Root cause: `events_entered` stores event **names** as strings on both `CollegeCompetitor` and `ProCompetitor` (what college registration and the Excel importer actually write — e.g. `"Underhand Hard Hit"`), but `_count_competitors` and `_list_competitors` built only an ID-keyed lookup for college events. Every lookup hit `if not event: continue`, so college block counts stayed at 0, and `_group_by_species` filtered the zero rows out of the by-species view. Saw logs still appeared because `calculate_saw_wood` emits zero-count rows unconditionally and the logs view doesn't filter them — masking the bug for weeks. The pro path was unaffected because `_get_pro_event_map` already had a name fallback.
- Both woodboss helpers now build `{college_id_map, college_name_map}` (by `.name` and `.display_name`) and try ID → name → skip, mirroring the pro lookup pattern.
- Same ID-vs-name mismatch fixed in `CollegeCompetitor.closed_event_count` — it built a set of event IDs and compared against names, always returning 0, so the 6-CLOSED-events-per-athlete enforcement had been silently off.
- Verified against the live `(WOOD TEST) 2026` tournament — 32 college count keys populated, 4 new college block groups appear in the by-species view.
- Save Preset form (`templates/woodboss/config.html`, `routes/woodboss.py`, `services/woodboss.py`): now reads the current wood config form data via HTML5 `form="wood-form"` + `build_preset_from_form()`, so unsaved species/sizes are captured. Prior version read only committed DB state, so pressing Save Preset after typing into the form but before pressing Save Configuration produced a preset with blank values.
- Audit report added: [docs/WOODBOSS_AUDIT.md](docs/WOODBOSS_AUDIT.md) — 3 HIGH, 8 MEDIUM, 6 LOW findings across routes/service/model/templates/tests. H0 and the Save Preset issue resolved in this patch; H1 (preset apply wipes existing sizes when value is None), H2 (save_config can't clear rows), H3 (delete_preset route has no UI) pending.
- CLAUDE.md Section 4 updated to state explicitly that both competitor types store event names in `events_entered`. Prior documentation said "list of event IDs" — the doc drift is what let the woodboss bug live.

### 2026-04-10 (V2.8.1)

**Patch — Name parser correctness & Virtual Woodboss pro springboard exclusivity**

**Gear-sharing / registration name parser (`services/gear_sharing.py`):**
- Fixed silent merge of generational-suffix variants: "David Moses" and "David Moses Jr." are now always treated as different people. The fuzzy matcher (SequenceMatcher ratio 0.91 > 0.86 cutoff) and the last-name-only fallback were both collapsing them into a single record.
- Fixed same-last-name first-name collisions: "Eric Lavoie" no longer silently resolves to "Erin Lavoie", and "Eric Hoberg" / "Erin Lavoie" stay distinct. The initials fallback was truncating full first names to a single character; the fuzzy matcher scored Eric/Erin at 0.90 and accepted it.
- `parse_gear_sharing_details` mention detection rewritten from normalized-substring to token-sequence matching: "...David Moses Jr..." in free text no longer matches a plain "David Moses" entry at the same position because the next token `jr` rejects the shorter canonical.
- New helpers: `_NAME_SUFFIXES` (Jr/Sr/II/III/IV/V), `_name_tokens`, `_strip_name_suffix_tokens`, `_name_stem`, `_suffix_mismatch`, `_names_token_compatible`.
- `resolve_partner_name` now:
  - Rejects fuzzy candidates that differ only by a generational suffix.
  - Rejects fuzzy candidates with identical last names and divergent first names unless the first names are either prefix-compatible (`Bri`/`Brianna`) or tight typo-fuzzy (first-name SequenceMatcher ratio ≥ 0.80 — `Imortol`/`Imortal` passes, `Eric`/`Erin` fails).
  - Returns the raw input when multiple distinct close matches tie, instead of silently picking the first.
  - Restricts the last-name-only fallback to single-token inputs.
  - Two-token fallback requires either a real 1–2 char initial or a prefix/typo-fuzzy relationship between first names.
- 6 regression tests added to `tests/test_gear_sharing.py` covering Moses Jr. (both directions, both variants present) and Lavoie/Hoberg scenarios.
- 271 gear-sharing + registration-import tests pass.

**Virtual Woodboss pro springboard exclusivity (`services/woodboss.py`):**
- Pro springboard now splits into three distinct wood categories instead of lumping everything into one `block_springboard_pro` bucket:
  - `block_springboard_pro` — Pro Springboard (2-board) only
  - `block_1board_pro` — Pro 1-Board
  - `block_3board_pro` — 3-Board Jigger
- `calculate_blocks()` now enforces pro 1-board / 3-board / 2-board exclusivity: an event whose name explicitly contains `1-board` / `one board` / `3-board` / `three board` / `jigger` no longer also matches the generic `springboard` fragment. Previously a single Pro 1-Board competitor was counted twice (once against 2-board, once against 1-board), either shorting real 2-board inventory on block-turning day or ghosting extra blocks for a category that wasn't running.
- `BLOCK_CONFIG_LABELS` updated to expose the three distinct keys in setup UIs.
- Dummy-math docstring clarified — `calculate_springboard_dummies()` still walks real event counts as the authoritative source to avoid any double-count regression.

---

### 2026-04-10 (V2.8.0)

**Registration Import Pipeline & Documentation Consolidation**

**Enhanced registration import pipeline (`services/registration_import.py`):**
- New `run_import_pipeline(filepath)` wraps existing `parse_pro_entries()` and adds comprehensive validation, cross-validation, and dirty-file handling
- Dirty partner field detection: 10 garbage patterns auto-resolved ("?", "idk", "Lookin", "Whoever", "no oarnter", "N/A", "TBD", "Have saw need partner", "spare", "put me down") mapped to NEEDS_PARTNER
- Fuzzy name matching: 4-tier resolution (exact normalized, difflib fuzzy, last-name/initial, first-name-only with 4-char minimum to prevent false positives)
- Dirty gear sharing text parsing: handles `Name-equipment`, `equipment: name1 name2`, conversational ("Me and X sharing a Y"), parenthetical grouping, comma-separated, conditional language detection
- Duplicate detection: keep latest entry by timestamp per email; warn on duplicate names with different emails
- Gender-event cross-validation: warns on gender-mismatched event signups
- Partner reciprocity validation: detects non-reciprocal partnerships (A lists B, but B lists C or is absent)
- Gear sharing inference from partner assignments: auto-infers shared equipment for Jack & Jill, Double Buck, Partnered Axe Throw
- Gear flag reconciliation: overrides sharing_gear=No to Yes when sharing data exists
- Unregistered reference detection: catches references to non-competitors (event organizers, typos, non-entrants)
- Structured 11-section plain-text import report (replaces 4 hours of manual review)
- CLI entry point: `python -m services.registration_import <file.xlsx>`
- `to_entry_dicts()` converts enhanced results back to entry dicts for existing DB commit flow

**Import route integration (`routes/import_routes.py`):**
- Upload route now runs enhanced pipeline alongside existing parser; stores report as temp file
- Review page shows collapsible "Import Analysis Report" section above the review table
- Confirm route cleans up report file alongside parsed data temp file

**Test suite (`tests/test_registration_import.py` — 85 tests):**
- Partner classification (garbage patterns, real names, empty/null)
- Fuzzy name matching (exact, case-normalized, prefix, first-name-only, Levenshtein, unresolvable)
- Equipment detection and gear text parsing (dirty patterns)
- Gender-event cross-validation
- Full integration against real dirty xlsx (47 competitors, dedup, auto-resolve, fuzzy match, reciprocity, inference)
- Import report generation

**Documentation consolidation:**
- Moved SCORING_AUDIT.md and PLAN_REVIEW.md to docs/ (audit artifacts, not living root docs)
- Archived stale PRODUCTION_AUDIT.md (V2.2.0) to docs/archived/
- Cleaned GEAR_SHARING_DOMAIN.md template header in docs/Alex's docs/
- Updated USER_GUIDE.md to V2.8.0 (added 10+ missing features from V2.1-V2.8)
- Fixed dangling EntryFormReqs.md references in CLAUDE.md (file never existed)
- Updated CLAUDE.md project structure and feature lists

---

### 2026-04-09 (V2.8.0)

**Race-Day Hardening & Feature Completion**

**Competitor `display_name` property (pervasive refactor):**
- Added `display_name` property to `CollegeCompetitor` (returns `"Name (TeamCode)"`) and `ProCompetitor` (returns plain `name`)
- Replaced `.name` with `.display_name` across ~30 locations: routes, services, templates (heat sheets, flights, standings, gear sharing, birling, portals, API)
- Eliminates redundant `{{ c.name }} ({{ c.team.team_code }})` patterns in templates

**Handicap factor sentinel fix (1.0 → 0.0 = scratch):**
- Changed `EventResult.handicap_factor` default from `1.0` to `0.0`; `0.0` now means scratch directly
- Updated `_metric()` in scoring engine: `1.0` is now a real 1-second mark, not a sentinel
- Updated all tests and `_SCRATCH_PLACEHOLDER` in assign_marks

**Springboard category split (ability rankings):**
- Added `pro_1board` and `3board_jigger` ranking categories to `config.py`
- `event_rank_category()` now differentiates 1-board, 3-board/jigger, and generic springboard
- Added Jack & Jill Sawing to `COLLEGE_SATURDAY_PRIORITY_DEFAULT`

**Ability rankings UI overhaul:**
- Replaced text-input rank fields with drag-and-drop SortableJS lists (ranked/unranked zones)
- POST handler rewritten to parse position-based ordering from hidden inputs
- Added gender-based competitor grouping
- Added College Birling Seedings section: per-school drag-and-drop ordering, generates global seed numbers, stores as `pre_seedings` in `Event.payouts` JSON

**Payout settlement merged into payout summary:**
- Merged settlement UI (Paid/Pending badges, Mark Paid toggle) into the existing Pro Payout Summary page
- Deleted `templates/reporting/payout_settlement.html`; old route now 301-redirects
- Removed sidebar "Payout Settlement" link; renamed button to "Pro Payouts"

**Virtual Woodboss enhancements:**
- Wood presets: `COMMON_WOOD_SPECIES` + `WOOD_PRESETS` in config; `get_all_presets()`, `save_custom_preset()`, `delete_custom_preset()`, `apply_preset()` in service; 3 new routes; preset UI card with species autocomplete
- Saw wood calculation overhaul: tracks by `(comp_type, gender)` instead of just gender; separate college/pro rows; fixed zero-division bugs
- Springboard dummy calculation overhaul: accepts `tournament_id`; separates 1/2/3-board heights; Friday/Saturday day split; reuse logic for sequential boards
- Report template: per-day/per-height breakdown, reuse annotations, college/pro division headers

**Pro-Am Relay manual team builder (new feature):**
- Added `set_teams_manually()` to `ProAmRelay` service with duplicate-assignment validation
- New routes: `GET /manual-teams` (drag-and-drop builder) + `POST /manual-teams/save`
- New template `templates/proam_relay/manual_teams.html` (~260 lines): SortableJS pools, gender count validation, add/remove teams
- Dashboard: added "Manual Team Builder" button, improved capacity display with bottleneck indicator

**Partnered Axe Throw scoring integration:**
- Added `_sync_prelim_to_event_results()`: prelim scores now create/update `EventResult` records for visibility in regular scoring view
- Inline prelim scoring form on `event_results.html` when event has prelims
- `return_to=event_results` redirect support in `record_prelim` route

**Payout state protection for special events:**
- Added `Event.uses_payouts_for_state` property (Pro-Am Relay, Partnered Axe, Birling bracket)
- `configure_payouts` route blocks state-events; bulk template skips them; "Special Event" badge in payout manager

**Finalization validation warnings:**
- Added `validate_finalization()` in scoring engine: checks missing payouts, unassigned handicap marks, pending throwoffs
- Warnings surfaced in `finalize_preview` JSON and flashed in `finalize_event`

**Post-finalize payout recalculation:**
- Saving payouts on an already-finalized event re-runs `calculate_positions()` to propagate new amounts
- Same auto-recalculation when applying a payout template to a finalized event

**College Excel import improvements:**
- School name extraction from filename via `_school_name_from_filename()`
- Team column auto-detection via `_detect_team_column_by_values()`
- Team code generation uses `school_abbr-letter` format (e.g., "UM-A")
- 14 new school abbreviations; expanded event marker keywords
- Roster validation: min 2M, min 2F, max 8 per team
- First-name fallback + fuzzy matching (Levenshtein edit distance ≤ 2) for partner matching

**Admin team validation override:**
- New route `POST /<tid>/college/team/<team_id>/override-validation`: forces team to "active" status

**Gear sharing group aggregation:**
- Union-find connected-component grouping replaces pair-based display
- Groups of 3+ show transitive sharing chains
- Templates overhauled: group-based table columns (Members / Size / Equipment / Status / Heat Status)

**Birling bracket on heat sheets:**
- Birling bracket events rendered with winners/losers bracket, grand finals, and placement table on heat sheet print pages
- `birling_generate` falls back to `pre_seedings` from ability rankings

**ALA report email feature:**
- New `ala_email_report` POST route: generates PDF + sends via SMTP to `americanlumberjacks@gmail.com`
- ALA Report link added to sidebar navigation

**Scoring engine throwoff fix:**
- `record_throwoff_result()` now uses canonical `PLACEMENT_POINTS_DECIMAL` lookup instead of stale `config.PLACEMENT_POINTS`

**Print button JavaScript fix:**
- Added `[data-print]` event listener across 9 print templates that previously had non-functional print buttons

Files changed: 71 files, ~2544 insertions, ~705 deletions. 1 new file (`templates/proam_relay/manual_teams.html`), 1 deleted (`templates/reporting/payout_settlement.html`).

### 2026-03-23 (V2.6.0)

**Design Review — Accessibility, Polish & Empty States**

- Added `color-scheme: dark` to `<html>` element (CSS + inline) for native dark mode form controls, scrollbars, and date pickers
- Raised touch targets to 44px minimum on `.navbar-proam .nav-link` and `.btn-proam` (from 31px/38px)
- Added `@media (prefers-reduced-motion: reduce)` to `theme.css` — zeroes all animation/transition durations
- Added staggered `card-enter` fade-up animation on role entry cards (0.3s ease-out, 80ms stagger)
- Added hover lift transition on `.card.shadow-sm` (spectator portal cards)
- Fixed disabled button cursor: `cursor: not-allowed` on `.btn:disabled` / `.btn.disabled`
- Changed hero title from `<div>` to `<h1>` on role entry page for semantic heading hierarchy
- Replaced 9 bare empty state strings ("No rankings yet.", "No standings yet.", "No completed events yet.") across `spectator_college.html`, `spectator_pro.html`, `kiosk.html` with warm messages + Bootstrap icons

Files changed: `static/css/theme.css`, `templates/base.html`, `templates/role_entry.html`, `templates/portal/spectator_college.html`, `templates/portal/spectator_pro.html`, `templates/portal/kiosk.html`

### 2026-03-23 (V2.5.0)

**STRATHEX Design System & Comprehensive Test Suite**

**Design system extraction (`static/css/theme.css`):**
- Extracted STRATHEX dark theme tokens, fire palette (--sx-fire, --sx-amber, --sx-gold), and component overrides into standalone CSS file
- Button styles (.btn-proam with gradient/glow), badge variants (gold/silver/bronze), dark surface system (9 shades)
- Typography: Fraunces serif headings, Orbitron numeric, Inter body

**UI/UX overhaul:**
- Redesigned login page: dual-logo lockup (Missoula Pro-Am + STRATHEX), gradient card header, fire border accent
- Redesigned dashboard: Command Centre hero, live aggregate stats, quick launch sidebar, onboarding checklist, tournament table with status badges
- Redesigned role entry page: 3 role cards with per-role styling, active tournament beacon
- Enhanced tournament detail: compact stats bar, phase action panels, status-specific alerts
- Enhanced sidebar: mobile drawer with backdrop, expanded section groups, localStorage v2 persistence
- Enhanced base template: mobile sidebar toggle, Arapaho lock countdown banner, ownership bar
- New tournament payouts hub (`templates/scoring/tournament_payouts.html`): bulk template application to events, distribution visualization bars, event summary table, new template builder with presets
- Enhanced configure payouts: quick preset buttons, percentage mode, saved templates panel with inline breakdowns
- Enhanced scoring entry (`enter_heat.html`): tablet mode toggle, mobile numpad overlay, run-2 context chips, tie indicator badge, heat lock banner
- Enhanced heats page: dual-run side-by-side display, competitor spacing timeline, sync check modal
- Enhanced setup events: sticky save bar via IntersectionObserver
- Enhanced gear sharing page: summary stat cards, heat conflict alert table, action buttons
- STRATHMARK status moved to Jinja2 template (`templates/strathmark/status.html`): Bootstrap 5 styled config/push/skipped cards

**Comprehensive test suite (37 new test files):**
- Added `tests/conftest.py`: session-scoped Flask app, per-test transactional rollback, auth helpers, seed functions
- Added `tests/fixtures/synthetic_data.py`: 25 pro competitors, 55 college competitors, event scores, gear sharing details
- Added `pytest.ini`: markers (smoke/integration/slow), warning filters, short traceback
- New test files: test_api_endpoints, test_axe_throw_qualifiers, test_birling_bracket_12, test_college_import_e2e, test_db_constraints, test_flask_reliability, test_flight_builder_25_pros, test_flight_builder_integration, test_fuzz_scoring, test_gear_sharing_advanced, test_gear_sharing_parse_realistic, test_heat_gen_integration, test_infrastructure, test_mark_assignment, test_model_json_safety, test_models_full, test_partnered_axe_state, test_partnered_events_realistic, test_point_calculator, test_postgres_compat, test_preflight, test_pro_entry_importer, test_pro_import_e2e, test_relay_lottery_realistic, test_routes_post, test_schedule_builder, test_scoring_college_points, test_scoring_engine_integration, test_scoring_full_event, test_scoring_integration, test_sms_backup, test_strathmark_sync, test_template_rendering, test_upload_security, test_woodboss, test_workflow_e2e
- Updated test_gear_sharing.py, test_models.py, test_routes_smoke.py

**Expanded i18n (`strings.py`):**
- Additional Arapaho and Russian translations for new UI elements

**Developer tooling:**
- Added `pyrightconfig.json`: Pyright/Pylance static type checking (basic mode, Python 3.13)
- Added `.vscode/launch.json`: Flask debugger configuration
- Added `static/img/favicon.svg`: red P browser tab icon
- Added `docs/GEAR_SHARING_DOMAIN.md`: equipment inventory and sharing constraints reference

**Service enhancements:**
- Enhanced `services/gear_sharing.py`: additional batch operations, name normalization, event family helpers
- Enhanced `services/mark_assignment.py`: batch mark calculation, wood profile builder
- Enhanced `services/scoring_engine.py`: additional scoring logic
- Enhanced `services/flight_builder.py`: event spacing tiers, competing stand conflict gap
- Enhanced `services/heat_generator.py`: ability sorting improvements

### 2026-03-09 (V2.4.0)

**STRATHMARK Phase 5 — Prediction residuals, predicted_time persistence, import fix, logo:**

- **`EventResult.predicted_time` column (new):** Added `predicted_time = db.Column(db.Float, nullable=True, default=None)` to `EventResult` in `models/event.py`. Stores the HandicapCalculator's predicted completion time (seconds) for a competitor in a handicap event. Used by `_record_prediction_residuals_for_pro_event()` to compute residuals (actual − predicted) after finalization. NULL = mark assignment not run or competitor scratched. Updated `handicap_factor` inline comment to remove stale "DEPRECATED" language.
- **Migration `d8d4aa7bdb45_add_predicted_time_to_event_result.py`** (down_revision: `k8l9m0n1o2p3`): adds `predicted_time FLOAT NULL` to `event_results`.
- **`_record_prediction_residuals_for_pro_event()` (new helper in `services/strathmark_sync.py`):** Private function called at the end of `push_pro_event_results()`. Iterates completed results for the event; reads `result.predicted_time` and `result.result_value`; builds `predicted` and `actual` dicts keyed by `strathmark_id`; calls `record_prediction_residuals(predicted, actual, show_name, event_code, result_date)` from the `strathmark` package to update the Supabase bias-learning table. Fully non-blocking — any failure is caught, logged at ERROR level, and silently dropped. If `predicted_time` is NULL (mark assignment not run before finalization), logs a WARNING and skips that competitor.
- **`services/mark_assignment.py` — bug fix (wrong import):** Fixed `from strathmark.handicap import HandicapCalculator` → `from strathmark.calculator import HandicapCalculator`. Module `strathmark.handicap` does not exist; `strathmark.calculator` is the correct path confirmed by `strathmark/__init__.py` and `calculator.py`.
- **`services/mark_assignment.py` — predicted_time wiring:** Added `result.predicted_time = None` in the `assign_handicap_marks()` for loop alongside `result.handicap_factor = mark`. Comment documents the forward-compatibility design: replace `None` with `mark_result.predicted_time` once `_get_handicap_calculator()` and `_fetch_start_mark()` are updated to call `HandicapCalculator.calculate()` and return a `MarkResult`.
- **`services/mark_assignment.py` — logging:** Added `logger.info("HandicapCalculator produced %d marks", assigned)` after the result iteration loop.
- **`templates/scheduling/assign_marks.html` — STRATHMARK logo:** Added centered STRATHMARK logo (`static/strathmark_logo.png`) at the top of the Assign Marks page, wrapped in a black container div (`background: #000; padding: 16px; display: inline-block`) to preserve the logo's intentional black background against the light page. Logo is `max-width: 400px`.
- **`static/strathmark_logo.png`** (new): STRATHMARK brand logo copied from `Logos/sTRATHMARK.png`.
- **Scrubbed** 8 `__pycache__` directories.

**Model changes (migration `d8d4aa7bdb45`):**

| Table | Column | Type | Notes |
|-------|--------|------|-------|
| `event_results` | `predicted_time` | FLOAT NULL | HandicapCalculator predicted time in seconds; NULL = no prediction |

### 2026-03-09 (V2.3.0)

**Production Audit Sweep — scheduling decomposition, Postgres migration guide, handicap scoring, mark assignment pipeline:**

- **Scheduling blueprint package (Phase 3A):** Decomposed monolithic `routes/scheduling.py` (2025 lines) into a proper Flask Blueprint package at `routes/scheduling/`. Sub-modules: `events.py`, `heats.py`, `flights.py`, `heat_sheets.py`, `friday_feature.py`, `show_day.py`, `ability_rankings.py`, `preflight.py`, `assign_marks.py`. All 24 routes preserved at identical URL paths. Blueprint name `'scheduling'` unchanged. Shared helpers (`_normalize_name`, `_load_competitor_lookup`, `_generate_all_heats`, etc.) defined in `__init__.py` before sub-module imports to avoid circular imports.

- **Route smoke tests (Phase 3B):** Created `tests/test_routes_smoke.py` — pytest-based smoke tests using Flask test client with in-memory SQLite; CSRF disabled; seeds one admin user + one tournament; covers all blueprints: public, main, registration, scheduling, scoring, reporting, auth, portal, validation, woodboss, proam-relay, partnered-axe, import. Asserts no route returns 500/502/503.

- **PostgreSQL migration guide (Phase 3C):** Created `docs/POSTGRES_MIGRATION.md` — full migration procedure from SQLite to PostgreSQL for Railway deployment: env var checklist, `flask db upgrade` schema migration, `data_migrate.py` inline Python script for data transfer, `postgres://` → `postgresql://` prefix fix, sequence reset after bulk import, JSON TEXT column compatibility notes, Railway deployment checklist, rollback plan.

- **Handicap scoring math (Phase 4A):** Modified `_metric()` in `services/scoring_engine.py` to subtract `handicap_factor` (start mark in seconds) from raw time when `event.is_handicap is True` and `event.scoring_type == 'time'`. A `handicap_factor` of `None` or `1.0` (DB default placeholder) is treated as `0.0` (scratch). Net time clamped to `max(0.0, raw - start_mark)`. Added `TestHandicapScoring` class (11 tests) to `tests/test_scoring.py`.

- **Mark assignment service (Phase 4B):** Created `services/mark_assignment.py`:
  - `is_mark_assignment_eligible(event)` — checks `is_handicap`, `scoring_type == 'time'`, `stand_type` in `HANDICAP_ELIGIBLE_STAND_TYPES`, event not completed
  - `assign_handicap_marks(event)` — queries STRATHMARK HandicapCalculator for each competitor's start mark; stores result on `EventResult.handicap_factor`; returns `{status, assigned, skipped, errors}` dict; non-blocking (catches all exceptions); graceful no-op if STRATHMARK not configured or `strathmark` package not installed
  - `_build_strathmark_id_lookup()` — batch query for strathmark_id from either competitor model
  - `_get_handicap_calculator()` — lazy import of `strathmark.calculator.HandicapCalculator`; returns None if unavailable
  - `_fetch_start_mark()` — single competitor mark fetch; clamps negative marks to 0.0

- **Mark assignment route + template (Phase 4C):** Added `routes/scheduling/assign_marks.py` — `GET/POST /scheduling/<tid>/events/<eid>/assign-marks`:
  - GET: displays current mark state per competitor (has_mark flag, mark display), STRATHMARK config status, eligible status
  - POST: calls `assign_handicap_marks(event)`, commits, logs audit action, flashes result summary
  - Created `templates/scheduling/assign_marks.html` — Bootstrap 5 status-card layout: 3 info cards (eligibility, STRATHMARK connection, competitor count), action button (disabled if not eligible/configured), competitor mark table.

### 2026-03-09 (V2.2.0)

**STRATHMARK Phase 2 — Live competitor enrollment and result push:**

- Added `strathmark_id` (String(50), nullable, indexed) to both `ProCompetitor` and `CollegeCompetitor` in `models/competitor.py`
- Added migration `k8l9m0n1o2p3_add_strathmark_id.py` (down_revision: `j7k8l9m0n1o2`): `strathmark_id` column + index on both `pro_competitors` and `college_competitors` tables
- Created `services/strathmark_sync.py` — non-blocking STRATHMARK integration layer:
  - `is_configured()` — checks env vars (never raises)
  - `make_strathmark_id(name, gender, existing_ids)` — deterministic ID `{FirstInitial}{LastName}{GenderCode}` (e.g. `AKAPERM`); numeric suffix on collision
  - `enroll_pro_competitor(competitor)` — generates ID, calls `push_competitors()`, stores locally; non-blocking on any failure
  - `push_pro_event_results(event, year)` — pushes finalized pro SB/UH results; looks up wood species + diameter from `WoodConfig`; logs rows on failure for manual retry
  - `is_college_sb_uh_speed(event)` — case-insensitive match: Standing Block Speed, Underhand Speed, SB Speed, UH Speed
  - `push_college_event_results(event, year)` — resolves college competitor `strathmark_id` by name match via `pull_competitors()`; stores resolved ID locally; skips and logs unmatched competitors
  - Local cache files (gitignored): `instance/strathmark_sync_cache.json`, `instance/strathmark_skipped.json`
- Modified `routes/registration.py` `new_pro_competitor`: 3 lines after commit call `enroll_pro_competitor(competitor)`
- Modified `routes/scoring.py` `finalize_event`: added `_push_strathmark_results()` helper + 1-line call after `invalidate_tournament_caches()`
- Created `routes/strathmark.py` — `GET /strathmark/status`: director sync status page (env config, last push, global result count, skipped names); no auth; registered at `/strathmark`
- Modified `app.py`: added `from routes.strathmark import strathmark_bp` and `app.register_blueprint(strathmark_bp, url_prefix='/strathmark')`

**Model changes (migration `k8l9m0n1o2p3`):**

| Table | Column | Type | Notes |
|-------|--------|------|-------|
| `pro_competitors` | `strathmark_id` | VARCHAR(50) NULL | Populated at registration |
| `college_competitors` | `strathmark_id` | VARCHAR(50) NULL | Populated by name-match resolution |

---

### 2026-03-09 (V2.1.0)

**STRATHMARK Phase 1 — Championship vs. Handicap format selection:**

- Added `Event.is_handicap` (Boolean, default False) to `models/event.py`: stores Championship vs. Handicap format choice per event; applies only to underhand, standing block, and springboard Speed events
- Added migration `j7k8l9m0n1o2_add_is_handicap_to_events.py` (down_revision: `i6j7k8l9m0n1`): `is_handicap` column with `server_default='0'`
- Added `HANDICAP_ELIGIBLE_STAND_TYPES = {'underhand', 'standing_block', 'springboard'}` to `config.py`: single gating constant used across routes and templates
- Updated `routes/scheduling.py` (`_upsert_event`, `_create_college_events`, `_create_pro_events`, `_get_existing_event_config`): reads `handicap_format_{field_key}` form field for eligible events (excluding `scoring_type == 'hits'`); saves to `event.is_handicap`; `_get_existing_event_config` returns `college_handicap` and `pro_handicap` state dicts
- Updated `templates/scheduling/setup_events.html` and `templates/tournament_setup.html` (Events tab): Championship / Handicap radio toggle rendered beneath each eligible college CLOSED event and within each eligible pro event card; toggle hidden for Hard Hit events (`scoring_type == 'hits'`)

**Eligible events with Championship/Handicap toggle:**
- College CLOSED: Underhand Speed (M/F), Standing Block Speed (M/F), 1-Board Springboard (M/F)
- Pro: Springboard, Pro 1-Board, 3-Board Jigger, Underhand (M/F), Standing Block (M/F)

**Not eligible (no toggle):**
- Underhand Hard Hit, Standing Block Hard Hit — hits-based scoring, handicap not applicable

---

### 2026-03-09 (V2.0.0)

**20-improvement code quality and feature release:**

**Performance:**
- Fixed N+1 query in `routes/api.py` (`public_schedule`, `public_results`): replaced per-event lazy loads with single `Heat.query.filter(Heat.event_id.in_(event_ids))` batch query + `defaultdict` grouping
- Fixed N+1 query in `services/flight_builder.py`: replaced per-event `.filter_by(run_number=1).all()` loop with single batch heat query for all non-axe events

**Reliability & Error Handling:**
- Split `StaleDataError` and `IntegrityError` handlers in `routes/scoring.py`: StaleDataError now shows "another judge edited this — reload" warning; IntegrityError shows constraint violation error
- Added transactional rollback to scheduling: each `generate_all` / `rebuild_flights` / `integrate_spillover` action in `routes/scheduling.py` wrapped in `try/except db.session.rollback()`
- Fixed error leakage in `routes/registration.py`: unexpected exceptions no longer flash `str(e)` to users; generic admin-contact message shown instead
- Added `JSONDecodeError` guards to all model `.get_*()` methods — `CollegeCompetitor`, `ProCompetitor`, `Event.get_payouts()`, `Heat.get_competitors()`, `Heat.get_stand_assignments()` — return empty list/dict rather than propagating decode error

**Security:**
- Added `write_limit()` rate-limiting decorator to `routes/api.py`; `_init_write_limiter(app)` registered in `create_app()`
- Applied `@write_limit('60 per minute')` to `enter_heat_results` and `@write_limit('10 per minute')` to `finalize_event` in `routes/scoring.py`

**Validation:**
- Added `@validates('name')` on `CollegeCompetitor` and `ProCompetitor` (SQLAlchemy event); truncates at `MAX_NAME_LENGTH = 100`

**New Features:**
- Competitor self-service portal: `competitor_my_results` route in `routes/portal.py` at `/portal/competitor/<tid>/<type>/<id>/my-results`; PIN gate template `templates/portal/competitor_pin_gate.html`; results dashboard `templates/portal/competitor_my_results.html` showing events entered, heat assignments (with stand/flight/run), personal results with position and payout, gear-sharing partners; mobile/full view toggle
- Heat sheet PDF: `heat_sheet_pdf` route in `routes/scoring.py` at `/scoring/<tid>/heat/<hid>/pdf`; uses WeasyPrint if installed, falls back to print-styled HTML; standalone `templates/scoring/heat_sheet_print.html` with `@page` CSS, run-aware columns, "Print / Save as PDF" button; "Print Heat Sheet" link added to `enter_heat.html`
- Async heat/flight generation: `generate_async` POST and `generation_job_status` GET routes in `routes/scheduling.py`; wraps `background_jobs.submit()`; returns 202 + `job_id`; poll endpoint returns status/progress JSON
- API v1 prefix: `api_bp` now registered at both `/api/` and `/api/v1/` (name='api_v1') in `app.py`

**Code Organization:**
- Added `FlightBuilder` class to `services/flight_builder.py`: OO wrapper with `build()`, `integrate_spillover()`, and `spacing()` convenience methods
- Added `logging.getLogger(__name__)` + `logger.info(...)` at entry of `calculate_positions()` in `services/scoring_engine.py`
- Added `logging.getLogger(__name__)` + `logger.info(...)` at entry of `generate_event_heats()` in `services/heat_generator.py`
- Extracted `_handle_event_list_post()` helper in `routes/scheduling.py` to separate POST action dispatch from GET rendering

**UI:**
- Added Bootstrap 5 conflict modal to `templates/scoring/enter_heat.html`: 409 conflict response shows modal with server message and reload link

**Tests:**
- Added 5 new test classes to `tests/test_scoring.py`: `TestFlagOutliersEdgeCases`, `TestDetectAxeTiesEdgeCases`, `TestSortKeyTiebreaks`, `TestPendingThrowoffs`, `TestImportResultsFromCSV`

---

### 2026-03-07 (V1.9.0)

**Scoring Engine Overhaul — centralized, comprehensive scoring service:**
- Added `services/scoring_engine.py` (566 lines): centralized position calculation with per-type tiebreak strategies (hard-hit, axe throw, default/time); tie detection and throwoff workflow; triple-run cumulative scoring; score outlier flagging; individual/team standings; payout template CRUD; bulk CSV result import; live spectator poll data
- Added `models/payout_template.py` (`PayoutTemplate`): reusable payout configuration templates; tournament-independent; `get_payouts()`, `set_payouts()`, `total_purse()` helpers
- Added `payout_templates` table via migration `i6j7k8l9m0n1` (down_revision: `h5i6j7k8l9m0`)
- Added `Event.is_finalized` (Boolean): locks scoring when finalized; prevents further edits; triggers payout distribution
- Added `Event.requires_triple_runs` (Boolean): enables 3-run cumulative scoring format
- Added `EventResult.run3_value` (Float): third run for triple-run events
- Added `EventResult.tiebreak_value` (Float): result from a throwoff run
- Added `EventResult.throwoff_pending` (Boolean): two+ tied competitors waiting on throwoff before positions finalize
- Added `EventResult.handicap_factor` (Float, default 1.0): placeholder column for STRATHMARK-adjusted scoring (currently unused)
- Added `Heat.locked_by_user_id` (FK→users) + `Heat.locked_at` (DateTime): exclusive locking for concurrent score entry; `acquire_lock()`, `release_lock()`, `is_locked()` methods on Heat model

**Pro Ability Rankings:**
- Added `models/pro_event_rank.py` (`ProEventRank`): per-tournament ability ranking for pro competitors per event category; 7 categories: springboard, underhand, standing_block, obstacle_pole, singlebuck, doublebuck, jack_jill; rank 1 = best; unranked placed after ranked
- Added `templates/scheduling/ability_rankings.html`: judge UI to assign ability ranks before heat generation
- Added `/scheduling/<tid>/ability-rankings` route in `routes/scheduling.py`

**Show Day Dashboard:**
- Added `templates/scheduling/show_day.html` and `show_day` route in `routes/scheduling.py`: flight status cards (live/completed/pending), current heat CTA, upcoming heats, college event progress bars; 60s auto-refresh; linked from the 4-step scheduling wizard

---

### 2026-03-07 (V1.8.0)

**Heat Sheet & Flight Generation UI/UX Enhancements (14 features):**
- Added 4-step unified scheduling wizard indicator in `events.html` (Configure → Heats → Generate & Build → Show Day)
- Added `Tournament.schedule_config` TEXT column (migration `h5i6j7k8l9m0`, down_revision: `e8f9a0b1c2d3`); `get_schedule_config()` / `set_schedule_config()` helpers; `event_list` and `friday_feature` routes persist config to DB on save and load from DB on open
- Added inline preflight panel to `events.html`: `preflight_json` GET route returns JSON; JS auto-loads on page open; panel shows severity, issue list with type badges
- Added per-flight heat sheet filter pill tabs in `heat_sheets_print.html`: filter visible flight blocks; "Print This Flight" button when a single flight is selected
- Added competitor spacing heatmap to `heats.html`: server-computed `spacing_data`; collapsible section with inline CSS bars; red if gap < 4, blue otherwise
- Added stand-assignment color coding: 8 distinct colors for stands 1-8; applied in both `heats.html` and `heat_sheets_print.html`; print-safe via `print-color-adjust: exact`
- Rewrote `heats.html` to display dual-run heats side-by-side: `{% set ns = namespace(shown_heat_numbers=[]) %}` tracks rendered heat numbers; Run 1 left / Run 2 right two-column Bootstrap grid
- Added flight build progress bar overlay: full-screen loading overlay with animated step labels; triggered by `[data-loading]` attribute on build forms in `events.html`
- Added flight build diff summary modal: snapshot taken before/after build; stored in session, popped on next GET; Bootstrap modal auto-opens with flight counts and total heats
- Added drag-and-drop heat reordering: SortableJS CDN in `heat_sheets_print.html`; drag handles on heat card headers; `reorder_flight_heats` POST route updates `Heat.flight_position`; CSRF via `X-CSRFToken` header
- Rewrote `heat_sheets_print.html` with two-column print layout: `.heat-card-head.head-pro` (blue band), `.head-college` (mauve band); `print-color-adjust: exact`
- Added judge annotation mode: notes-lines toggle checkbox in `heat_sheets_print.html`; `.notes-mode .heat-card::after` adds ruled dashed lines; hidden in print when not checked
- Added Cookie Stack / Standing Block conflict warning badges: server-side detection via `_STAND_CONFLICT_GAP`; `stand_conflicts` per flight dict; warning badges in `heat_sheets_print.html`

---

### 2026-03-06 (V1.7.0)

**Gear Sharing Manager — complete pro gear-sharing management system:**
- Expanded `services/gear_sharing.py`: `build_gear_report()` (audit), `parse_gear_sharing_details()` (free-text NLP), `build_parse_review()` (pre-commit review), `fix_heat_gear_conflicts()` (auto-fix heats), `sync_gear_bidirectional()`, `sync_all_gear_for_competitor()`, `create_gear_group()`, `get_gear_groups()`, `complete_one_sided_pairs()`, `cleanup_scratched_gear_entries()`, `auto_populate_partners_from_gear()`, `build_gear_completeness_check()`, `parse_all_gear_details()`
- Added ~15 routes in `routes/registration.py`: `pro_gear_manager`, `pro_gear_update`, `pro_gear_remove`, `pro_gear_complete_pairs`, `pro_gear_auto_partners`, `pro_gear_cleanup_scratched`, `pro_gear_sync_heats`, `pro_gear_parse_review`, `pro_gear_parse_confirm`, `pro_gear_group_create`, `pro_gear_group_remove`, `auto_assign_pro_partners_route`, `college_gear_update`, `college_gear_remove`, `pro_gear_print`
- New `templates/pro/gear_sharing.html`: full manager UI — verified pairs table, unresolved entries with edit/remove, gear groups, add/update form, college constraints section; all with inline Bootstrap modals
- New `templates/pro/gear_parse_review.html`: review proposed gear maps from free-text details before committing; select/deselect per competitor
- New `templates/pro/gear_sharing_print.html`: standalone print report grouped by equipment category (springboard/crosscut/chainsaw)

**Tournament Setup consolidated page:**
- New `GET /tournament/<tid>/setup` route in `routes/main.py` with tabs for Events, Wood Specs, and Settings
- New `POST /tournament/<tid>/setup/settings` route: save tournament name, year, college/pro/Friday dates
- New `templates/tournament_setup.html`: unified setup UI; wood specs tab reuses woodboss config form inline; `return_to=setup` redirect from woodboss save_config and copy_from routes
- Tournament detail workflow stepper now links to `/tournament/<tid>/setup`

**Fee Tracker:**
- New `GET/POST /reporting/<tid>/pro/fee-tracker` route
- New `templates/reporting/fee_tracker.html`: per-competitor fee table with expandable per-event breakdown; mark all paid / unmark; outstanding-only filter toggle; balance summary cards
- Fee Tracker link added to sidebar Competitors section

**Tournament Detail redesign:**
- `templates/tournament_detail.html` fully rewritten: 3 phase-panels (Before Show / Game Day / After Show); 6-step workflow stepper with clickable links; context-sensitive next-step guidance banner; compact stats bar with alert badges; async validation status banner
- All Phase 1 setup links now point to `tournament_setup`

**Migration:**
- `migrations/versions/b5f1e9cd7c50_merge_springboard_slow_heat_branch.py`: merge migration resolving `a9b8c7d6e5f4` + `c6d7e8f9a0b1` branch heads (no-op schema change)

**Various template updates:**
- Updated `pro/dashboard.html`, `pro/competitor_detail.html`, `pro/flights.html`, `pro/build_flights.html`, `scheduling/events.html`, `scheduling/heats.html`, `scheduling/day_schedule.html`, `scheduling/friday_feature.html`, `woodboss/config.html`, `woodboss/dashboard.html`, `woodboss/report.html`, `woodboss/report_print.html`, `college/team_detail.html`, `dashboard.html`

### 2026-03-04 (V1.6.0)

**Collapsible tournament management sidebar:**
- Added `templates/_sidebar.html`: sticky collapsible sidebar component (220px expanded / 44px icon-only); 5 independently collapsible sections (Show Entries, Show Configuration, Scoring, Results, Admin); localStorage-persisted collapse state; active-link auto-expand; unscored-heats badge on "Events & Schedule" link
- Updated `base.html` with full STRATHEX dark-theme redesign; sidebar rendered when tournament context is available; `unscored_heats` count injected via `app.py` context processor

**Unified Events & Schedule page (`/scheduling/<tid>/events`):**
- `event_list` route now accepts POST with actions: `generate_all`, `rebuild_flights`, `integrate_spillover`; session-stored schedule options (Friday Night Feature pro events, Saturday college overflow events); schedule preview via `build_day_schedule()`
- Redesigned `templates/scheduling/events.html`: heat status per event, one-click "Generate All Heats" + "Build Flights" + "Integrate Spillover", schedule preview accordion, event progress badges
- `/scheduling/<tid>/day-schedule` now redirects to `event_list` (301) — legacy route preserved
- New `generate_college_heats` bulk route: generates heats for all closed college events in one click; skips OPEN/list-only and completed events; flashes per-event error if generation fails
- Stand count overrides: `setup_events` form now includes per-stand-type count inputs; `_parse_stand_overrides()` helper; `_upsert_event()` uses override when provided; `_get_existing_event_config()` returns `stand_counts`; `_next_open_stand()` uses `event.max_stands` as authoritative

**Flight builder algorithm improvements:**
- Per-event sequential queue (`_optimize_heat_order`): only the NEXT unplaced heat from each event is eligible at each step, guaranteeing heats within any event appear in ascending `heat_number` order across the show schedule
- Springboard flight opener (`_promote_springboard_to_flight_start`): after greedy ordering, the first pro springboard heat in each flight block is moved to position 0 of that block
- Stand conflict deadlock fallback: if all candidates score `-1.0`, re-score ignoring stand conflicts and take the best-spacing choice

**College Saturday overflow integration (closes known gap):**
- `integrate_college_spillover_into_flights()` now handles Chokerman's Race Run 2 separately: all Run 2 heats are placed together at the end of the last flight in heat-number order
- Other overflow events distributed round-robin across all flights (unchanged behavior, now wired to `event_list` POST)
- `event_list` calls integration automatically after `generate_all` and `rebuild_flights` actions if Saturday college event IDs are selected

**HeatAssignment sync (`Heat.sync_assignments`):**
- New `Heat.sync_assignments(competitor_type)` method: deletes and rebuilds `HeatAssignment` rows from the authoritative `Heat.competitors` JSON — closes the consistency gap between the two representations
- Called automatically after heat generation (`heat_generator.py`), after flight rebuilds (`flight_builder.py`), and after competitor moves (`scheduling.py` `move_competitor_between_heats`)

**Score entry: "Save & Next Heat" button:**
- `routes/scoring.py` now queries the next pending heat in the same event; passes `next_heat_url` to `enter_heat.html`
- `templates/scoring/enter_heat.html` shows a "Save & Next Heat" button when one exists

**Woodboss fixes:**
- OP lag formula corrected: 5" lag every **7** competitors (was 10)
- `log_op` and `log_cookie` config keys are now fully independent (no fallback to `log_general`); `LOG_OP_KEY` and `LOG_COOKIE_KEY` constants exported
- Pro competitor event lookup fixed: `_count_competitors()` and `_list_competitors()` now resolve event IDs to event names via the Event table (pro competitors store IDs in `events_entered`, not names)
- `routes/woodboss.py` passes `op_cfg` and `cookie_cfg` to config form template

**New documentation:**
- Added `FlightLogic.md`: 480-line source-of-truth document for all flight builder rules, heat generation rules, stand configurations, algorithm details, edge cases, and build sequence — referenced by `services/flight_builder.py` and `services/heat_generator.py`

**Print template cleanup:**
- Reformatted `all_results_print.html`, `college_standings_print.html`, `event_results_print.html`, `payout_summary_print.html`, `day_schedule_print.html`, `heat_sheets_print.html` — consistent no-print classes, page-break handling, layout fixes

### 2026-03-02 (V1.5.0)

**Virtual Woodboss — complete material planning system for block prep days:**
- Added `models/wood_config.py` (`WoodConfig`): per-tournament species/size config; UniqueConstraint on (tournament_id, config_key); `count_override` for relay/manual entries; `display_size()` helper
- Migrations: `a4b5c6d7e8f9` (team validation_errors), `b5c6d7e8f9a0` (wood_configs table), `c6d7e8f9a0b1` (count_override column)
- Added `services/woodboss.py`: BLOCK_EVENT_GROUPS enrollment matching, relay block handling, SAW_EVENTS formulas (crosscut 2"/cut, stocksaw 5", hotsaw 6.5", OP formula, cookie logs), relay double buck fix (log_relay_doublebuck key), lottery view, ordering summary, history report, HMAC share token
- Added `routes/woodboss.py`: dual blueprint — `woodboss_bp` (judge/admin protected) + `woodboss_public_bp` (HMAC-token share link, no auth); routes: dashboard, config_form, save_config, copy_from, report, report_print, lottery, history, share
- Added 6 templates in `templates/woodboss/`: dashboard, config (with relay rows + relay double buck row), report (3-tab: by event/by species/ordering), report_print (standalone HTML), lottery, history
- Added `woodboss` to `MANAGEMENT_BLUEPRINTS` + `BLUEPRINT_PERMISSIONS['woodboss'] = 'is_judge'` in app.py
- Added `wood_configs` relationship to `Tournament` model (cascade delete-orphan)
- Added Virtual Woodboss card to tournament_detail.html Tools section
- **Pro-Am Relay double buck fix:** relay participants are not enrolled in a standard "Double Buck" event — added `log_relay_doublebuck` config key so judge enters team count manually; service appends relay double buck to saw_wood output (teams × 2" per cut)

**Team Validation Framework:**
- Added `validation_errors` TEXT column to `Team` model (JSON list of structured error dicts); `get_validation_errors()` / `set_validation_errors()` methods; status now supports 'invalid'
- Refactored `services/excel_io.py` `_validate_college_entry_constraints()` to return `dict[team_id, list[dict]]` with 8 typed error types instead of raising; partial success import — valid teams commit, invalid teams tracked
- Updated `routes/registration.py` to split `valid_teams` / `invalid_teams` and display warning flash
- Updated `templates/college/registration.html` to show Invalid Teams count; `team_detail.html` to show error accordion with targeted fix forms per error type

**New services (not yet wired to UI routes):**
- `services/partner_matching.py`: auto-partner assignment for pro events; bidirectional validation; gender/mixed-gender matching
- `services/preflight.py`: pre-scheduling checks (heat/table sync, odd partner pools, Saturday overflow); generates severity-ranked issue list with autofixable flags; `templates/scheduling/preflight.html` template added
- `services/handicap_export.py`: chopping-event Excel export helpers (underhand, springboard, standing)

**Infrastructure:**
- Added `templates/_tournament_tabs.html` shared 3-tab nav component
- `.gitignore` updated to exclude `static/video/` (large local media files)
- All `__pycache__/` and `.pyc` bytecode artifacts scrubbed from repo

### 2026-02-28 (V1.4.0)
- Added school captain portal: one PIN-protected account per school covers all teams; `models/school_captain.py`, migration `f3a4b5c6d7e8`
- Added school captain dashboard: 4-tab Bootstrap UI (overview, teams/members, schedule, Bull & Belle); browser PDF export via `@media print`
- Added school captain claim/access flow: school name search → PIN creation/verification → session auth
- Updated `templates/portal/competitor_access.html` to surface school captain login alongside pro competitor lookup
- Added in-app user guide (`/portal/guide`): 6-role guide with sticky sidebar and mobile nav pills
- Added `static/js/onboarding.js`: `ProAmOnboarding.show/reopen` engine; localStorage-tracked first-time modal; Bootstrap 5 multi-step with dots, skip, and "Got it!"
- Added first-time onboarding popups to spectator dashboard, pro competitor dashboard, and school captain dashboard
- Added Help `?` nav button to `base.html` linking to user guide

### 2026-02-27 (V1.3.0)
- Fixed `CollegeCompetitor.closed_event_count` to compare against `COLLEGE_CLOSED_EVENTS` names (#20)
- Added heat sync check (GET JSON) + sync fix (POST reconcile) in scheduling.py; modal + JS in heats.html (#19)
- Added heat sheets print page (`/scheduling/<tid>/heat-sheets` + `heat_sheets_print.html`) (#7)
- Added Saturday priority route (`POST /scheduling/<tid>/college/saturday-priority`) (#15)
- Added audit log viewer (`/auth/audit`, admin-only, paginated, filterable) + `audit_log.html` (#9)
- Added payout settlement checklist (`/reporting/<tid>/pro/payout-settlement`) + `payout_settlement.html`; migration `d1e2f3a4b5c6` adds `payout_settled BOOLEAN` to pro_competitors (#21)
- Added score outlier flagging `_flag_score_outliers()` in scoring.py; ⚠ badge in event_results.html; migration adds `is_flagged BOOLEAN` to event_results (#8)
- Added birling bracket route + `birling_bracket.html` (flex bracket tree); completed `_advance_winner`, `_drop_to_losers`, `_advance_loser_winner` advance logic (#13)
- Added kiosk TV display (`/portal/kiosk/<tid>`): standalone HTML, 4-panel 15s rotation (#12)
- Added tournament clone route (`POST /tournament/<tid>/clone`) — copies teams, competitors, events (no heats/results) (#10)
- Added difflib duplicate detection (cutoff 0.85) and `existing_names` param to `compute_review_flags()` in pro_entry_importer.py (#18)
- Expanded `_EVENT_MAP` in pro_entry_importer.py with 15+ aliases (#6)
- Added `standings_poll` endpoint in api.py; live 30s polling JS in spectator_college.html (#11)
- Added headshot upload routes in registration.py; magic-byte JPEG/PNG/WebP validation; migration `e2f3a4b5c6d7` adds `headshot_filename` + `phone_opted_in` to both competitor tables (#14)
- Added `services/sms_notify.py` (conditional Twilio, graceful no-op); `start_flight`/`complete_flight` routes; SMS opt-in toggle in portal.py; Start/Complete buttons in flights.html (#2)
- Added `static/sw.js` (service worker: cache + IDB queue + Background Sync); `static/offline_queue.js` (banner, manual replay); registered in base.html; `/sw.js` route in app.py (#24)
- Added `services/backup.py` (S3 via boto3 + local fallback); cloud backup route in reporting.py; Cloud Backup button in tournament_detail.html (#25)
- Added `_CONFLICTING_STANDS` + `_STAND_CONFLICT_GAP` constants to flight_builder.py; enforces Cookie Stack / Standing Block mutual exclusion

### 2026-02-27 (V1.2.0)
- Added Flask-Login authentication: User model (admin/judge/competitor/spectator), login/logout, bootstrap, user management UI
- Added `require_judge_for_management_routes` before_request hook; portal and auth routes are public
- Added portal blueprints (`/portal`): spectator dashboard, competitor dashboard (schedule/results/payouts), mobile/desktop view toggle
- Added public REST API (`/api/public`): standings, schedule, results endpoints
- Added `models/audit_log.py` + `services/audit.py` for immutable audit trail
- Added `services/background_jobs.py`: thread-pool executor for async Excel export
- Added `services/report_cache.py`: in-memory TTL cache for reporting routes
- Added `services/upload_security.py`: magic-byte Excel validation, UUID filenames, optional malware scan
- Added `services/logging_setup.py`: JSON structured logging, optional Sentry SDK init
- Added pro entry xlsx importer (`services/pro_entry_importer.py`, `routes/import_routes.py`)
- Added migrations: users table, portal_pin_hash on competitors, audit_logs + indexes + version_id + unique constraints
- Added brand logo static assets
- Added `requirements.txt` entries: Flask-Login, sentry-sdk, pinned psycopg2-binary, gunicorn
- Created `.gitignore`; untracked `__pycache__/`, `instance/`, `uploads/` from git index
- Added `templates/role_entry.html` landing page (judge/competitor/spectator selection)
- **Known issues:** migration branch conflict (run `flask db merge heads` before deploying); api_bp not registered in app.py; several portal/auth routes referenced in templates not yet implemented

### 2026-02-25
- Added Flask-WTF CSRF protection; all POST form templates now include `{{ csrf_token() }}`
- Added `gear_sharing` dedicated TEXT column to `CollegeCompetitor` (matches `ProCompetitor`); removed nested `__gear_sharing__` approach
- Migrated database schema management to Flask-Migrate exclusively; removed `db.create_all()` from `init_db()`
- Added `railway.toml` with `releaseCommand = "flask db upgrade"` for Railway deployments
- Added input hardening (try/except) around all `int()`/`float()` calls on POST form data in scoring, scheduling, relay, and partnered axe routes
- Fixed open redirect in `set_language()` by removing `request.referrer` fallback

### 2026-01-25
- Removed Pro Birling event
- Added Pro Pole Climb (single run, gendered)
- Renamed Pro "Jack & Jill" to "Jack & Jill Sawing"
- Changed College Obstacle Pole to single run (was dual runs)
- Updated flight builder with competitor spacing algorithm (target: 5 heats, minimum: 4)
