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
│   ├── pro_event_rank.py  # ProEventRank — per-tournament ability rankings (7 categories)
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
│   ├── reporting.py       # Standings, reports, payout settlement, cloud backup
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
│   ├── proam_relay.py     # Pro-Am Relay team building
│   ├── partnered_axe.py   # Axe throw scoring logic
│   ├── pro_entry_importer.py  # Google Forms xlsx import with duplicate detection
│   ├── audit.py           # log_action() helper
│   ├── background_jobs.py # Thread-pool executor for async Excel export
│   ├── report_cache.py    # In-memory TTL cache for report payloads
│   ├── upload_security.py # Magic-byte validation, UUID filenames, scan hook
│   ├── logging_setup.py   # JSON structured logging; optional Sentry SDK
│   ├── sms_notify.py      # Twilio SMS; graceful no-op if not configured
│   ├── backup.py          # S3 or local SQLite backup
│   ├── woodboss.py        # Block/saw calculations, lottery view, history, HMAC share token
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

The database file is created at `instance/proam.db` for SQLite (dev) or the URL from the `DATABASE_URL` environment variable (production/Railway).

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
SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', 'sqlite:///proam.db')
UPLOAD_FOLDER = 'uploads'
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
