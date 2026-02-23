# CLAUDE.md — Missoula Pro-Am Manager

This file is read by Claude Code at the start of every session. It documents the project's identity, architecture, domain logic, data model, current state, development rules, and relationship to the broader STRATHEX ecosystem. Read it in full before making any changes.

---

## 1. PROJECT IDENTITY

The Missoula Pro-Am Manager is a purpose-built tournament management web application for the Missoula Pro Am timbersports competition. It handles the full operational lifecycle of the event: team registration, competitor registration, event configuration, heat generation, flight scheduling, score entry, standings, and payout tracking. It is client-specific software, not a generic tournament tool, though the long-term goal is to generalize it.

This application exists within the **STRATHEX ecosystem**, built by Alex Kaper. STRATHEX is the parent company and flagship Tournament Management System platform. This app is STRATHEX's first real-world pilot deployment, demonstrating the platform's capabilities against a live annual competition.

**Ecosystem components:**

- **Missoula Pro-Am Manager** (this repo): Tournament logistics — registration, heats, flights, results, payouts.
- **STRATHMARK** (separate STRATHEX repo): Handicap Calculator add-on. Python CLI, uses XGBoost + Ollama LLM. Not yet connected to this app.
- **KYTHEREX**: Predictive Engine add-on. Planned but not yet detailed in this codebase.

STRATHMARK and KYTHEREX are not integrated into this app. See Section 7 for planned integration points.

---

## 2. ARCHITECTURE OVERVIEW

### Tech Stack

| Component | Technology |
|-----------|------------|
| Backend | Python 3.10+ / Flask 3.0 |
| Database | SQLite via Flask-SQLAlchemy 3.1 / SQLAlchemy 2.0 |
| Frontend | Jinja2 templates, Bootstrap 5 |
| Excel I/O | pandas 2.1, openpyxl 3.1 |
| Utilities | Werkzeug 3.0 |

The database file is `instance/proam.db`, created automatically on first run via `db.create_all()` in `database.py`.

### Project Structure

```
app.py                  # Application factory only — no routes, no DB logic
config.py               # All configuration constants and event definitions
database.py             # SQLAlchemy db object and init_db()
strings.py              # Centralized UI text labels (NAV, COMPETITION, FLASH)
requirements.txt

models/
    __init__.py         # Re-exports all models
    tournament.py       # Tournament
    team.py             # Team (college only)
    competitor.py       # CollegeCompetitor, ProCompetitor
    event.py            # Event, EventResult
    heat.py             # Heat, HeatAssignment, Flight

routes/
    __init__.py
    main.py             # Dashboard, tournament CRUD, college/pro dashboards
    registration.py     # Excel upload for college; manual entry for pro
    scheduling.py       # Event setup, heat generation, flight building
    scoring.py          # Heat result entry, position calculation, payouts
    reporting.py        # Standings, event results, payout summary (screen + print)
    proam_relay.py      # Pro-Am Relay lottery and results
    partnered_axe.py    # Partnered Axe Throw prelims/finals flow
    validation.py       # Data integrity checks (teams, competitors, heats)

services/
    __init__.py
    excel_io.py         # College entry form import; results export
    heat_generator.py   # Snake-draft heat generation with stand constraints
    flight_builder.py   # Optimized flight scheduling with competitor spacing
    point_calculator.py # College placement points and team aggregation
    birling_bracket.py  # Double-elimination bracket generation
    proam_relay.py      # ProAmRelay class — lottery logic and team management
    partnered_axe.py    # PartneredAxeThrow class — prelims/finals state machine
    validation.py       # ValidationResult, TeamValidator, CompetitorValidator, HeatValidator

templates/
    base.html
    dashboard.html
    tournament_detail.html / tournament_new.html
    college/            dashboard, registration, team_detail
    pro/                dashboard, registration, new_competitor, competitor_detail, flights, build_flights
    scheduling/         events, setup_events, heats
    scoring/            event_results, enter_heat, configure_payouts
    reports/            all_results, college_standings, event_results, payout_summary (+ _print variants)
    proam_relay/        dashboard, teams, results, standings
    partnered_axe/      dashboard, prelims, finals, results
    validation/         dashboard, college, pro

uploads/                # Uploaded Excel entry forms
instance/proam.db       # SQLite database (auto-created)
```

### Key Design Patterns

**Application factory:** `app.py` contains only `create_app()`. It loads config, initializes the DB, registers blueprints, and injects `strings.NAV` and `strings.COMPETITION` into all templates via a context processor. No route logic lives in `app.py`.

**Blueprints:** Each concern has its own blueprint in `routes/`. The `proam_relay`, `partnered_axe`, and `validation` blueprints define their `url_prefix` at registration time; the others (`registration`, `scheduling`, `scoring`, `reporting`) receive their prefix from `app.py`.

**Service classes for complex state:** The Pro-Am Relay and Partnered Axe Throw are managed by `ProAmRelay` and `PartneredAxeThrow` service classes respectively. Both serialize their state into an `Event.payouts` JSON field, repurposing the field rather than adding new tables. The `BirlingBracket` class uses the same pattern.

**JSON fields for lists and dicts:** `competitors`, `stand_assignments`, `events_entered`, `partners`, `entry_fees`, `fees_paid`, `gear_sharing`, and `payouts` are all stored as JSON text columns. This is a deliberate simplicity tradeoff — no join tables for these relationships.

**Flexible competitor references:** `EventResult.competitor_id` and `Heat.competitors` do not use SQLAlchemy foreign keys to competitor tables. Instead, `competitor_type` (`'college'` or `'pro'`) tells the code which model to query. This allows one Event/Heat/Result system to serve both college and pro without polymorphic inheritance.

**Centralized text:** All user-visible strings live in `strings.py` as `NAV`, `COMPETITION`, and `FLASH` dicts. Templates access `NAV` and `COMPETITION` via the context processor. Flash messages use `.format(**kwargs)` for dynamic values.

**Two-day competition format:** Friday is college competition, Saturday is pro. The `Tournament.status` field tracks `setup → college_active → pro_active → completed`. Tournament dates are stored separately as `college_date`, `pro_date`, and `friday_feature_date`.

---

## 3. DOMAIN KNOWLEDGE

### Timbersports Context

The Missoula Pro Am is an annual timbersports competition featuring events derived from logging trades: axe chopping (underhand, standing block, springboard), hand sawing (single buck, double buck, Jack & Jill), power sawing (stock saw, hot saw), climbing (speed climb, obstacle pole), log rolling (birling), and novelty events (axe throw, cookie stack, caber toss, pulp toss, peavey log roll). College and professional divisions compete on separate days under different formats.

### College Division (Friday)

Teams are organized by school. A school can enter multiple teams (e.g., UM-A, UM-B, UM-C). Each team must have at least 2 male and 2 female competitors, with a maximum of 8 total. Scoring is both individual and team-based. Individual placement points are: 1st=10, 2nd=7, 3rd=5, 4th=3, 5th=2, 6th=1. Team points are the sum of all member individual points. Top individual male is "Bull of the Woods," top female is "Belle of the Woods."

**OPEN vs CLOSED events:** OPEN events (Axe Throw, Peavey Log Roll, Caber Toss, Pulp Toss) have no competitor count restriction and traditionally run at the start of the day with a come-and-go format. CLOSED events (all others) limit each athlete to a maximum of 6 entries. The app allows the traditionally OPEN events to be configured as CLOSED when setting up a tournament, because the Missoula Pro Am sometimes runs them as CLOSED to save time.

**Two-run events:** Chokerman's Race and Speed Climb give each competitor two runs on different courses. The best (lowest) time counts. The heat generator creates run 1 and run 2 heats automatically and swaps stand assignments between runs. Obstacle Pole is single-run in both college and pro divisions.

**Birling:** College Birling is gender segregated — separate men's and women's brackets are run. It runs at the end of the college day as a double-elimination bracket, pre-seeded, top 6 determined. Pro Birling is not gender segregated and has been removed from the pro events list entirely (removed per the 2026-01-25 changelog; `config.py` PRO_EVENTS does not include birling).

**Partnered events:** Partners are pre-assigned during team registration, not randomly drawn. Mixed-gender partnered events (Pulp Toss, Peavey Log Roll, Jack & Jill Sawing) are exceptions to gender segregation and field one man + one woman per entry. Double Buck is partnered but gender-segregated (same gender pairs only).

### Professional Division (Saturday)

Pro competitors register individually with contact info, shirt size, ALA membership status, gear-sharing partners, and Pro-Am lottery opt-in. There are no teams. Payouts are tracked per event per competitor, configurable up to 10th place.

**Flight format:** Pro competition runs on a flight system. Heats from different events are interleaved into flights to maintain crowd variety. The flight builder uses a greedy algorithm to maximize event variety and ensure competitors have at least 4 heats between their own appearances (target: 5). Default is 8 heats per flight.

**Ability grouping:** Especially important for springboard. Slow cutters (4+ minute times) should be grouped together to avoid diluting heats. The `optimize_flight_for_ability` function in `flight_builder.py` is a stub — currently a no-op — that is the designated integration point for STRATHMARK predictions.

**Gear sharing:** Competitors who share expensive equipment (springboards, hotsaws, single saws) with a partner cannot be placed in the same heat. The heat generator and validation service check for gear-sharing conflicts.

**Left-handed springboard cutters:** Require assignment to the same dummy. The `is_left_handed_springboard` flag on `ProCompetitor` and corresponding logic in `_generate_springboard_heats()` handle this.

**Pro Birling:** Pro Birling has been removed from the pro events list entirely (per the 2026-01-25 changelog). It does not appear in `config.py` PRO_EVENTS. Unlike college Birling — which is gender segregated and runs as a double-elimination bracket — Pro Birling would not be gender segregated, but it is not hosted at this event. `services/birling_bracket.py` remains in the codebase for college Birling use.

**Stand constraints (from `config.STAND_CONFIGS`):**
- Springboard: 4 dummies, 3 uses each, supports handedness
- Underhand: 5 stands
- Standing Block and Cookie Stack: 5 stands, shared between the two events. These two events are mutually exclusive — they cannot have heats running simultaneously. Neither `heat_generator.py` nor `flight_builder.py` currently enforces this constraint; it is a known gap (see Section 5)
- Hand sawing (Single Buck, Double Buck, Jack & Jill): 8 stands in two groups of 4; heats of 4 go while the other group sets up
- Stock Saw: stands 1-2 only
- Hot Saw: stands 1-4 only
- Obstacle Pole: 2 sides (Pole 1/Pole 2)
- Speed Climb: Pole 2 and Pole 4
- Birling: 1 pond

### Pro-Am Relay

Two teams are randomly drawn prior to the start of the show. Each team consists of 6 competitors: 3 college and 3 professional. Teams arrange themselves in any order they choose to complete three events: partnered sawing (any gender combination), Standing Butcher Block, and Underhand Butcher Block. There is no fixed competitor order or role assignment — the teams decide internally. The relay is fit into one flight. Pro competitors opt in via their entry form (`pro_am_lottery_opt_in` on `ProCompetitor`). College competitors are all eligible. Results do not count toward college team or individual scores. This is the only event where college competitors can win money.

The lottery implementation in `services/proam_relay.py` attempts gender balance when drawing teams: the first slot on each side draws from the opposite gender pool, then fills from whatever pool remains.

### Partnered Axe Throw

Pre-show prelims: all pairs throw, scores recorded as hits. Top 4 pairs advance. Finals run during the show with one pair per flight. Full standings combine: finals results for positions 1-4, prelim standings for positions 5+. The `PartneredAxeThrow` class manages this as a three-stage state machine: `prelims → finals → completed`.

### Saturday College Overflow

The second run of Chokerman's Race must always be conducted on Saturday — this is non-negotiable and must be accounted for when building the Saturday schedule. Additional college events may roll to Saturday only when Friday scheduling requires it. The following priority order governs which events are candidates for Saturday overflow (in order of preference):

1. Men's Standing Block Speed
2. Men's Standing Block Hard Hit
3. Women's Standing Block Speed
4. Women's Standing Block Hard Hit
5. Men's Obstacle Pole

Saturday overflow college events are judged and scored as normal college events — points count toward individual and team college totals. No dedicated scheduling mechanism currently exists to designate college events as Saturday overflow or integrate them into the pro flight schedule. Flag as a known gap (see Section 5).

### Friday Night Feature

The Friday Night Feature is an optional overflow or special events session run after the college day concludes on Friday. It is built separately from both the main college and pro schedules. Typical events are Collegiate 1-Board, Pro 1-Board, and Pro 3-Board Jigger. The `Tournament` model has a `friday_feature_date` field to record when this session is held, but no dedicated scheduling UI, heat generation logic, or flight structure exists for it. Flag as a known gap (see Section 5).

---

## 4. DATA MODEL

### Tournament

Root entity for one year's competition. Fields: `id`, `name`, `year`, `college_date`, `pro_date`, `friday_feature_date`, `status` (setup/college_active/pro_active/completed), `created_at`, `updated_at`. Relationships: teams, college_competitors, pro_competitors, events (all cascade delete).

Missing field: the Tournament model has no `providing_shirts` boolean. Whether the show provides shirts is determined before entry forms are sent out and should control whether shirt size is collected during pro competitor registration. Currently, `ProCompetitor.shirt_size` is always collected regardless. This is a known gap (see Section 5).

### Team

College only. Fields: `id`, `tournament_id`, `team_code` (e.g., UM-A), `school_name`, `school_abbreviation`, `total_points`, `status`. Unique constraint on `(tournament_id, team_code)`. Methods: `recalculate_points()` sums member individual points. Properties: `member_count`, `male_count`, `female_count`, `is_valid` (checks min 2 per gender, max 8 total).

### CollegeCompetitor

Fields: `id`, `tournament_id`, `team_id`, `name`, `gender` (M/F), `individual_points`, `events_entered` (JSON list of event IDs), `partners` (JSON dict event_id → partner_name), `status` (active/scratched). Method `add_points()` updates individual total and triggers team recalculation.

Note: `closed_event_count` property currently counts all events entered, not just CLOSED ones. This is a known imprecision — it counts by list length rather than filtering by event classification.

### ProCompetitor

Fields: `id`, `tournament_id`, `name`, `gender`, `address`, `phone`, `email`, `shirt_size`, `is_ala_member`, `pro_am_lottery_opt_in`, `is_left_handed_springboard`, `events_entered` (JSON), `entry_fees` (JSON dict event_id → amount), `fees_paid` (JSON dict event_id → bool), `gear_sharing` (JSON dict event_id → partner_name), `partners` (JSON dict event_id → partner_name), `total_earnings`, `status`. Properties: `total_fees_owed`, `total_fees_paid`, `fees_balance`.

### Event

Fields: `id`, `tournament_id`, `name`, `event_type` (college/pro), `gender` (M/F/None), `scoring_type` (time/score/distance/hits/bracket), `scoring_order` (lowest_wins/highest_wins), `is_open` (college OPEN/CLOSED flag), `is_partnered`, `partner_gender_requirement` (same/mixed/any), `requires_dual_runs`, `stand_type`, `max_stands`, `has_prelims`, `payouts` (JSON dict position → amount), `status` (pending/in_progress/completed). Relationships: heats, results.

The `payouts` JSON field is repurposed by `ProAmRelay` and `PartneredAxeThrow` to store their state, and by `BirlingBracket` to store bracket data. This is a deliberate design decision to avoid extra tables.

### EventResult

Fields: `id`, `event_id`, `competitor_id` (integer, not FK), `competitor_type` (college/pro), `competitor_name`, `partner_name`, `result_value`, `result_unit`, `run1_value`, `run2_value`, `best_run`, `final_position`, `points_awarded`, `payout_amount`, `status` (pending/completed/scratched/dnf). Method `calculate_best_run()` sets `best_run` and `result_value` to the lower of run1/run2.

### Heat

Fields: `id`, `event_id`, `heat_number`, `run_number` (1 or 2), `competitors` (JSON list of competitor IDs), `stand_assignments` (JSON dict competitor_id → stand_number), `status`, `flight_id` (nullable FK to flights). Methods: `get_competitors()`, `set_competitors()`, `add_competitor()`, `remove_competitor()`, `get_stand_assignments()`, `set_stand_assignment()`.

### HeatAssignment

Separate table for heat assignments: `id`, `heat_id`, `competitor_id`, `competitor_type`, `stand_number`. Note: both `Heat.competitors` (JSON) and `HeatAssignment` rows exist in the codebase. The JSON field on Heat is the primary mechanism used by the heat generator; HeatAssignment is used by the validation service. These can diverge — this is a consistency gap to watch.

### Flight

Fields: `id`, `tournament_id`, `flight_number`, `name`, `status`, `notes`. Relationship: heats (via `Heat.flight_id`). Properties: `heat_count`, `event_variety`. The flight builder works by setting `heat.flight_id` directly.

### Key Relationships Summary

```
Tournament
  ├── Team (1:many, cascade delete)
  │     └── CollegeCompetitor (1:many, via team_id)
  ├── CollegeCompetitor (1:many, cascade delete)
  ├── ProCompetitor (1:many, cascade delete)
  └── Event (1:many, cascade delete)
        ├── Heat (1:many, cascade delete)
        │     └── HeatAssignment (1:many)
        └── EventResult (1:many, cascade delete)

Flight
  ├── tournament_id (FK to tournaments)
  └── Heat.flight_id (FK, optional)
```

### College Excel Import

`services/excel_io.py` imports college entry forms via `process_college_entry_form()`. It reads sheet 0, normalizes column names to lowercase, and tries to detect format by looking for `school` or `team` columns. It supports flexible column name matching (e.g., `school`, `university`, `college`, `institution`). It creates Team and CollegeCompetitor records, deduplicating by name within a team. It generates team codes in the pattern `ABBREV-A`, `ABBREV-B`, etc. A hardcoded `_abbreviate_school()` dict covers common schools (UM, MSU, CSU, UI, OSU, UW, HSU, CP); others get generated from initials.

---

## 5. CURRENT STATE & KNOWN GAPS

### Features Functionally Complete

- Tournament creation and lifecycle management (setup → college_active → pro_active)
- College team import from Excel (flexible column detection)
- Pro competitor manual registration with all required fields
- Event configuration from `config.py` event lists, with OPEN/CLOSED designation choice
- Heat generation using snake-draft distribution, with stand capacity enforcement, springboard left-hand grouping, and dual-run heat creation
- Flight builder with greedy competitor-spacing algorithm (min 4 heats between appearances, target 5)
- Score entry for standard events (single-value and dual-run)
- Position calculation and point/payout award on finalization
- College standings: Bull/Belle of the Woods, team standings
- Pro payout summary
- All-results report (screen and printable)
- Event-level result reports (screen and printable)
- Pro-Am Relay lottery (opt-in, gender-balanced draw, result entry, standings)
- Partnered Axe Throw state machine (prelim registration, prelim scoring, top-4 advance, finals, full standings)
- Validation service for teams, college competitors, pro competitors, heat constraints
- Validation API endpoints (JSON) for full, college-only, and pro-only checks
- Excel results export (`export_results_to_excel` in `excel_io.py`) — function exists but no route triggers it from the UI

### Known Gaps and Incomplete Features

**Birling bracket (`services/birling_bracket.py`):** The `BirlingBracket` class has full bracket generation (double-elimination structure, seeding, byes) and match recording, but three critical methods are stubs with `pass`: `_advance_winner()`, `_drop_to_losers()`, and `_advance_loser_winner()`. The bracket structure is built but winners cannot be advanced between rounds automatically. There is also no route in `routes/` for the birling bracket UI. The bracket is non-functional end-to-end.

**Partner assignment for pro events without explicit partner:** The requirements note "if a partner is not explicitly listed on the form, I want a way for the system to auto-assign a partner." No auto-assignment logic exists. Partners must be manually assigned.

**Heat editing:** The USER_GUIDE describes an "Edit Heat" flow for moving competitors between heats, but no `/scheduling/.../heat/<id>/edit` route exists. Heat composition can only be reset by regenerating all heats for an event.

**Pro competitor event sign-up UI:** The UI flow for checking events and entering gear-sharing/partner info on a pro competitor detail page is referenced in the USER_GUIDE but the `templates/pro/competitor_detail.html` template exists without a corresponding POST route to save event entries. Event sign-ups are stored on `ProCompetitor.events_entered` (JSON) but no route processes that form submission.

**Excel results export route:** `export_results_to_excel()` exists in `services/excel_io.py` and the USER_GUIDE describes clicking "Export Results," but no route in `routes/reporting.py` or elsewhere wires this to an HTTP response.

**Friday Night Feature scheduling:** The `Tournament.friday_feature_date` field exists and the requirements document describes the feature, but there is no dedicated UI or scheduling logic for it.

**College events on Saturday:** The requirements describe a defined list of college events that can spill to Saturday. No scheduling mechanism distinguishes these from regular college events.

**Authentication:** None exists. Anyone with network access to the running server can modify all data. Intentionally deferred for the 2026 prototype deployment.

**STRATHMARK integration:** Completely absent. See Section 7.

**EntryFormReqs.md:** Contains only a stub note ("Entry forms need to be redone for the pros"). Pro entry form redesign is pending.

**Cookie Stack / Standing Block stand conflict:** No enforcement exists in `heat_generator.py` or `flight_builder.py` preventing these two events from being scheduled simultaneously. They share 5 stands and must be treated as mutually exclusive — heats from these two events cannot overlap in the same flight slot. The `shared_with` key is present in `config.STAND_CONFIGS` for both event types but nothing reads it during scheduling.

**Saturday overflow scheduling:** No mechanism exists to designate college events as Saturday overflow or integrate them into the pro flight schedule. The second run of Chokerman's Race is a mandatory Saturday event and must be accounted for when building flights. No current code handles this distinction.

**Tournament model missing `providing_shirts` boolean:** The show decides before entry forms go out whether it is providing shirts. `ProCompetitor.shirt_size` is always collected regardless. Adding this field requires a schema migration — see Development Rules for the Flask-Migrate requirement.

**Pro event fee configuration:** No route or template exists for setting fee amounts per event per tournament. `entry_fees` and `fees_paid` fields exist on `ProCompetitor` but there is no UI to set or update them. Fees must currently be set directly in the database.

**Auto-partner assignment for pro events:** When a pro competitor registers for a partnered event without naming a partner, no auto-assignment logic exists. Desired behavior: match with another unpartnered registrant in the same event.

**Pro birling references:** `config.py` PRO_EVENTS correctly excludes birling. Verify that no existing templates, database records, or service code contain hardcoded references to a pro birling event that would create phantom data.

---

## 6. DEVELOPMENT RULES

Keep all code lean and simple — functionality first, then simplicity and ease of understanding.

Do not create new routes in `app.py` — all routes belong in the `routes/` directory as blueprints.

Do not create new database logic in `app.py` — all DB logic belongs in `models/` or `services/`.

Preserve all existing comments and notes in their original format and style when making changes.

Plain text outputs only in UI — no emojis, no color codes.

Safe error handling: failures print descriptive messages but allow continued operation. Wrap risky operations in try/except and flash a descriptive error rather than raising to the user or silently swallowing the failure.

Database schema changes: do not use `db.create_all()` for schema modifications on an existing database. `db.create_all()` only creates tables that do not yet exist — it does not alter existing tables. Use Flask-Migrate (Alembic) for all schema changes after initial setup. If Flask-Migrate is not yet initialized in this project, flag it and ask before proceeding with any schema change that would modify an existing table.

HeatAssignment vs Heat.competitors: `Heat.competitors` (JSON field) is the authoritative source for heat composition and is what the heat generator reads and writes. `HeatAssignment` rows are used only by the validation service. All new code reading or writing heat composition must use `Heat.competitors`. Whenever `Heat.competitors` is modified, keep `HeatAssignment` rows in sync to avoid validation false positives.

Authentication: intentionally deferred for the 2026 prototype deployment. Do not build authentication unless explicitly instructed.

Cookie Stack and Standing Block stand conflict: any code touching heat generation or flight scheduling must enforce mutual exclusivity of these two events. Cookie Stack (`stand_type: cookie_stack`) and Standing Block (`stand_type: standing_block`) share the same 5 physical stands. Never schedule heats from both events at the same time or within the same flight slot without explicit instruction.

---

## 7. RELATIONSHIP TO STRATHEX ECOSYSTEM

This app handles tournament logistics only: registration, heats, flights, results, payouts. It does not perform any predictive or handicap calculation.

**STRATHMARK** (not yet integrated) handles handicap calculation and AI-powered predictions. It is a Python CLI tool using XGBoost and an Ollama LLM, living in the separate STRATHEX repository.

**KYTHEREX** (not yet integrated) is described as a Predictive Engine add-on.

### Natural Integration Points in the Current Codebase

Two specific locations are where STRATHMARK predictions would plug in:

1. **`services/flight_builder.py` — `optimize_flight_for_ability()`:** This function (lines 196–213) is explicitly a stub with a comment reading "Future: Could reorder based on predicted times." This is the designed hook for STRATHMARK to provide predicted completion times per competitor per event, enabling ability-grouped heats in the springboard and other time-based events.

2. **`services/heat_generator.py` — `_generate_event_heats()`:** The current snake-draft distribution has no ability-weighting. Competitors are distributed without regard to predicted performance. STRATHMARK predictions should feed a pre-sorted competitor list or a weighting scheme here to produce ability-grouped heats rather than purely round-robin distribution.

3. **`models/competitor.py` — `ProCompetitor`:** Has no `handicap` or `predicted_time` fields. These would need to be added (or provided externally at heat generation time) for STRATHMARK integration.

4. **`models/event.py` — `Event`:** Has no `start_mark` field. Pro springboard start marks are determined by handicap — this would be a natural STRATHMARK output field on the Event or EventResult.

The broader vision: STRATHMARK calculates start marks and predicted times from historical data, feeds them into this app's heat generation step to group competitors by predicted ability, and its predictions are surfaced to the TD during the pre-show planning phase.

---

## 8. FUTURE DEVELOPMENT NOTES

The following features are documented as planned or implied by the codebase and requirements:

**From DEVELOPMENT.md "Planned Features":**
- Live results display via WebSocket for spectators
- Mobile score entry with offline sync capability
- Multi-year competitor tracking and performance history
- Blank entry form generator (Excel)

**From DEVELOPMENT.md "Technical Debt":**
- Authentication system (currently none)
- Comprehensive server-side input validation
- Unit and integration tests (none exist)
- RESTful API for external integrations

**From requirements and code gaps:**
- Birling bracket completion (advance logic stubs need implementation)
- Pro competitor event sign-up POST route
- Excel results export route
- Friday Night Feature scheduling
- College Saturday overflow scheduling
- Auto-partner assignment for pro events
- Heat editing UI
- Pro entry form redesign (noted in EntryFormReqs.md)

**Generalization vision:** The current app is hardcoded to the Missoula Pro Am's specific event list and format. The event list lives in `config.py` (`COLLEGE_OPEN_EVENTS`, `COLLEGE_CLOSED_EVENTS`, `PRO_EVENTS`) and the UI strings in `strings.py`. The long-term STRATHEX platform vision is to make these event lists configurable per-tournament rather than hardcoded, enabling this application to serve any timbersports event, not just Missoula. The `is_open` flag on Event, the configurable payouts system, and the per-tournament event setup flow are all steps in this direction.
