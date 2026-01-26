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
│   └── heat.py            # Heat & Flight models
├── routes/                # Flask blueprints
│   ├── main.py            # Dashboard & navigation
│   ├── registration.py    # Competitor/team registration
│   ├── scheduling.py      # Heat & flight generation
│   ├── scoring.py         # Result entry & calculation
│   ├── reporting.py       # Standings & reports
│   ├── proam_relay.py     # Pro-Am Relay lottery system
│   ├── partnered_axe.py   # Partnered Axe Throw prelims/finals
│   └── validation.py      # Data validation endpoints
├── services/              # Business logic
│   ├── excel_io.py        # Excel import/export
│   ├── heat_generator.py  # Heat generation algorithm
│   ├── flight_builder.py  # Flight scheduling with competitor spacing
│   ├── birling_bracket.py # Double-elimination bracket generator
│   ├── point_calculator.py# Point calculation utilities
│   ├── proam_relay.py     # Pro-Am Relay team building
│   └── partnered_axe.py   # Axe throw scoring logic
└── templates/             # Jinja2 HTML templates
    ├── base.html          # Base layout
    ├── dashboard.html     # Main dashboard
    ├── college/           # College competition templates
    ├── pro/               # Pro competition templates
    ├── scoring/           # Score entry templates
    ├── scheduling/        # Heat/flight management templates
    ├── reports/           # Report templates (screen & print)
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
| `*/print` | Printable versions |

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

SQLite database is created automatically at `proam.db` on first run.

To reset:
```bash
rm proam.db
python app.py  # Creates fresh database
```

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

1. **Authentication** - Currently no user authentication
2. **Input Validation** - Add comprehensive server-side validation
3. **Error Handling** - Improve error messages and recovery
4. **Testing** - Add unit and integration tests
5. **API** - RESTful API for external integrations

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
SQLALCHEMY_DATABASE_URI = 'sqlite:///proam.db'
UPLOAD_FOLDER = 'uploads'
MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB

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

### 2026-01-25
- Removed Pro Birling event
- Added Pro Pole Climb (single run, gendered)
- Renamed Pro "Jack & Jill" to "Jack & Jill Sawing"
- Changed College Obstacle Pole to single run (was dual runs)
- Updated flight builder with competitor spacing algorithm (target: 5 heats, minimum: 4)
