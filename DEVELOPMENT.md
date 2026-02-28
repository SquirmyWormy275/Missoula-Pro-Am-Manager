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
│   └── school_captain.py  # SchoolCaptain — one PIN account per school per tournament
├── routes/                # Flask blueprints
│   ├── main.py            # Dashboard & navigation
│   ├── registration.py    # Competitor/team registration; headshot upload
│   ├── scheduling.py      # Heat & flight generation; heat swap; heat sheets print; Friday feature
│   ├── scoring.py         # Result entry & calculation; outlier flagging
│   ├── reporting.py       # Standings, reports, payout settlement, cloud backup
│   ├── proam_relay.py     # Pro-Am Relay lottery system
│   ├── partnered_axe.py   # Partnered Axe Throw prelims/finals
│   ├── validation.py      # Data validation endpoints
│   ├── import_routes.py   # Pro entry xlsx importer (parse → review → confirm)
│   ├── auth.py            # Login, logout, bootstrap, user management, audit log viewer
│   ├── portal.py          # Spectator, competitor, school captain portals; user guide
│   └── api.py             # Public read-only REST API (/api/public)
├── services/              # Business logic
│   ├── excel_io.py        # Excel import/export
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
│   └── backup.py          # S3 or local SQLite backup
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
    ├── pro/               # Pro competition templates (+ import flow)
    ├── scoring/           # Score entry templates
    ├── scheduling/        # Heat/flight management; heat sheets print; friday feature
    ├── reports/           # Report templates (screen & print); payout settlement
    ├── proam_relay/       # Pro-Am Relay templates
    └── partnered_axe/     # Partnered Axe templates
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
| `/tournament/<id>/college` | College dashboard |
| `/tournament/<id>/pro` | Pro dashboard |

### Registration (`/registration`)
| Route | Description |
|-------|-------------|
| `/<id>/college` | College registration |
| `/<id>/college/upload` | Upload Excel entry form |
| `/<id>/pro` | Pro registration |
| `/<id>/pro/new` | Add pro competitor |

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
