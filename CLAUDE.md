# CLAUDE.md — Missoula Pro-Am Manager

This file is read by Claude Code at the start of every session. It documents the project's identity, architecture, domain logic, data model, current state, development rules, and relationship to the broader STRATHEX ecosystem. Read it in full before making any changes.

---

## 1. PROJECT IDENTITY

The Missoula Pro-Am Manager is a purpose-built tournament management web application for the Missoula Pro Am timbersports competition. It handles the full operational lifecycle of the event: team registration, competitor registration, event configuration, heat generation, flight scheduling, score entry, standings, and payout tracking. It is client-specific software, not a generic tournament tool, though the long-term goal is to generalize it.

This application exists within the **STRATHEX ecosystem**, built by Alex Kaper. STRATHEX is the parent company and flagship Tournament Management System platform. This app is STRATHEX's first real-world pilot deployment, demonstrating the platform's capabilities against a live annual competition.

**Ecosystem components:**

- **Missoula Pro-Am Manager** (this repo): Tournament logistics — registration, heats, flights, results, payouts.
- **STRATHMARK** (separate STRATHEX repo): Handicap Calculator add-on. Python CLI, uses XGBoost + Ollama LLM. Live integration as of V2.3.0 — competitor enrollment, SB/UH result push, college result resolution, sync status page, handicap scoring math in `_metric()`, and mark assignment route wired. Full end-to-end pipeline requires `strathmark` package installed + env vars. See Section 7.
- **KYTHEREX**: Predictive Engine add-on. Planned but not yet detailed in this codebase.

STRATHMARK has active live-data integration (enrollment + result push to Supabase global DB). Handicap scoring math and mark assignment route are wired (V2.3.0). Full end-to-end pipeline requires the `strathmark` package installed + env vars. See Section 7.

---

## 2. ARCHITECTURE OVERVIEW

### Tech Stack

| Component | Technology |
|-----------|------------|
| Backend | Python 3.10+ / Flask 3.0 |
| Database | SQLite via Flask-SQLAlchemy 3.1 / SQLAlchemy 2.0 |
| Migrations | Flask-Migrate 4.0.7 (Alembic) |
| Forms / CSRF | Flask-WTF 1.2.2 |
| Frontend | Jinja2 templates, Bootstrap 5 |
| Excel I/O | pandas 2.1, openpyxl 3.1 |
| Utilities | Werkzeug 3.0 |

The database file is `instance/proam.db`. Schema is managed exclusively by Flask-Migrate — run `flask db upgrade` to initialize or evolve the database. Never use `db.create_all()` for schema changes.

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
    competitor.py       # CollegeCompetitor, ProCompetitor (+ portal_pin_hash)
    event.py            # Event, EventResult (+ version_id optimistic lock)
    heat.py             # Heat, HeatAssignment, Flight (+ version_id optimistic lock)
    user.py             # User — role-based auth (7 roles: admin/judge/scorer/registrar/competitor/spectator/viewer)
    audit_log.py        # AuditLog — immutable audit trail for sensitive actions
    school_captain.py   # SchoolCaptain — one PIN account per school per tournament
    wood_config.py      # WoodConfig — per-tournament wood species/size config for block prep
    pro_event_rank.py   # ProEventRank — per-tournament ability ranking for pro competitors (7 event categories)
    payout_template.py  # PayoutTemplate — reusable payout structure templates (tournament-independent)

routes/
    __init__.py
    main.py             # Dashboard, tournament CRUD, college/pro dashboards; tournament_setup multi-tab GET; save_tournament_settings POST
    registration.py     # Excel upload for college; manual entry for pro; gear sharing manager routes (15+ routes)
    scheduling/         # Event setup, heat generation, flight building (Blueprint package)
        __init__.py       # Blueprint + shared helpers; imports sub-modules
        events.py         # event_list, setup_events, day_schedule, apply_saturday_priority
        heats.py          # event_heats, generate_heats, generate_college_heats, swap, sync
        flights.py        # flight_list, build_flights, reorder, start_flight, complete_flight
        heat_sheets.py    # heat_sheets, day_schedule_print
        friday_feature.py # friday_feature
        show_day.py       # show_day
        ability_rankings.py # ability_rankings
        preflight.py      # preflight_check, preflight_json, generate_async, job_status
        assign_marks.py   # assign_marks (STRATHMARK handicap mark assignment)
    scoring.py          # Heat result entry, position calculation, payouts
    reporting.py        # Standings, event results, payout summary (with settlement), ALA report + email, fee_tracker
    proam_relay.py      # Pro-Am Relay lottery, results, and manual team builder
    partnered_axe.py    # Partnered Axe Throw prelims/finals flow
    validation.py       # Data integrity checks (teams, competitors, heats)
    import_routes.py    # Pro entry form Excel import (parse → review → confirm)
    auth.py             # Login, logout, bootstrap, user management (/auth prefix)
    portal.py           # Spectator and competitor portals (/portal prefix)
    api.py              # Public read-only REST API (/api/public prefix)
    woodboss.py         # Virtual Woodboss — material planning (/woodboss prefix)
                        #   woodboss_bp (protected) + woodboss_public_bp (HMAC share link)
    strathmark.py       # STRATHMARK sync status page (/strathmark prefix); no auth required

services/
    __init__.py
    excel_io.py         # College entry form import; results export
    heat_generator.py   # Snake-draft heat generation with stand constraints
    flight_builder.py   # Optimized flight scheduling with competitor spacing
    point_calculator.py # College placement points and team aggregation
    birling_bracket.py  # Double-elimination bracket generation
    proam_relay.py      # ProAmRelay class — lottery logic, team management, manual team builder
    partnered_axe.py    # PartneredAxeThrow class — prelims/finals state machine
    validation.py       # ValidationResult, TeamValidator, CompetitorValidator, HeatValidator
    pro_entry_importer.py  # parse_pro_entries() + compute_review_flags() for xlsx import
    registration_import.py # Enhanced import pipeline: dirty-file handling, fuzzy matching, cross-validation, report gen
    audit.py            # log_action() helper — writes AuditLog records best-effort
    background_jobs.py  # Thread-pool executor for async tasks (Excel export)
    report_cache.py     # In-memory TTL cache for report payloads
    cache_invalidation.py  # invalidate_tournament_caches() — clears report/portal/API caches on mutation
    upload_security.py  # Magic-byte Excel validation, UUID safe filenames, scan hook
    logging_setup.py    # JSON structured log formatter; optional Sentry SDK init
    sms_notify.py       # Twilio SMS for flight start/complete; graceful no-op if not configured
    backup.py           # S3 or local SQLite backup; triggered from reporting route
    woodboss.py         # Virtual Woodboss: block/saw calculations, lottery view, history, share token, wood presets
    handicap_export.py  # Chopping-event Excel export helpers (underhand, springboard, standing)
    partner_matching.py # Auto-partner matching for pro partnered events (bidirectional validation)
    preflight.py        # Pre-scheduling validation: heat/table sync, odd partner pools, Saturday overflow
    gear_sharing.py     # Comprehensive gear-sharing service: parse/match/audit, bidirectional sync, group gear,
                        #   free-text parser, parse review, heat conflict detection + auto-fix, batch operations
    schedule_builder.py # Day schedule assembly: Friday day/feature blocks, Saturday show block from flights
    scoring_engine.py   # Centralized scoring engine: position calculation, tiebreak logic, tie/throwoff management,
                        #   outlier flagging, individual/team standings, payout template CRUD, bulk CSV import
    strathmark_sync.py  # STRATHMARK integration layer: competitor enrollment, result push (pro SB/UH, college Speed),
                        #   ID generation, WoodConfig lookup, name-match resolution, local sync cache + skipped log
    mark_assignment.py  # Handicap mark assignment: is_mark_assignment_eligible(), assign_handicap_marks();
                        #   queries STRATHMARK HandicapCalculator; stores start marks on EventResult.handicap_factor

templates/
    base.html
    _tournament_tabs.html  # Shared 3-tab nav (Overview / College Day / Pro Day)
    _sidebar.html          # Collapsible tournament sidebar: 5 sections, localStorage state, unscored badge; Fee Tracker link
    dashboard.html
    role_entry.html        # Landing page: Judge / Competitor / Spectator selection
    tournament_detail.html     # Phase-based 3-panel layout (Before/Game Day/After); workflow stepper; contextual banners
    tournament_new.html
    tournament_setup.html      # Consolidated setup: events + wood specs + settings tabs in one page
    auth/               login, bootstrap, users, audit_log
    portal/             landing, spectator_dashboard, spectator_college_standings,
                        spectator_pro_standings, spectator_event_results,
                        spectator_relay_results, competitor_access, competitor_dashboard,
                        competitor_pin_gate, competitor_my_results,
                        school_access, school_claim, school_dashboard, user_guide
    college/            dashboard, registration, team_detail
    pro/                dashboard, registration, new_competitor, competitor_detail,
                        flights, build_flights, import_upload, import_review,
                        gear_sharing, gear_sharing_print, gear_parse_review
    scheduling/         events, setup_events, heats, day_schedule (+ _print),
                        heat_sheets_print, friday_feature, preflight,
                        ability_rankings, show_day, assign_marks
    scoring/            event_results, enter_heat, configure_payouts, offline_ops,
                        heat_sheet_print, tournament_payouts
    reports/            all_results, college_standings, event_results,
                        payout_summary (+ _print variants), export_status
    reporting/          fee_tracker, ala_membership_report
    proam_relay/        dashboard, teams, results, standings, manual_teams
    partnered_axe/      dashboard, prelims, finals, results
    validation/         dashboard, college, pro
    woodboss/           dashboard, config, report, report_print (standalone), lottery, history
    strathmark/         status

static/
    css/theme.css       # STRATHEX design system: dark theme tokens, fire palette, component overrides
    js/onboarding.js    # First-time onboarding modal engine (ProAmOnboarding.show/reopen)
    sw.js               # Service worker: offline cache + IDB queue + Background Sync
    offline_queue.js    # Offline queue UI: banner + manual replay
    img/favicon.svg     # Browser tab icon (red P)
    img/                # Brand logos (STRATHEX, Pro-Am)

FlightLogic.md          # Source-of-truth for all flight builder rules, heat gen rules, stand configs,
                        # algorithm details, constants, and known gaps. Update when rules change.

tests/
    conftest.py         # Shared pytest fixtures (app, client, db, seeded tournament)
    fixtures/
        synthetic_data.py  # Synthetic tournament/competitor/event data generators

pytest.ini              # Pytest config: markers (smoke/integration/slow), warning filters

uploads/                # Uploaded Excel entry forms (gitignored)
instance/proam.db       # SQLite database (auto-created; gitignored)
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

**Dual-run day-split events:** Chokerman's Race and Speed Climb are the two dual-run events that split across days. The rule is:

- **Run 1: Friday.** Both events have their first run on Friday.
- **Run 2: Saturday.** The second run is ALWAYS on Saturday. Non-negotiable.
- **Stand/course swap:** Stands are reversed between Run 1 and Run 2 (the heat generator already does this). Chokerman swaps Course 1 / Course 2. Speed Climb swaps Pole 2 / Pole 4.
- **Chokerman's Race Run 1 placement:** End of the Friday schedule, but BEFORE Birling. Birling is always the last college event on Friday.
- **Speed Climb Run 1 placement:** Normal position in the Friday schedule (no special end-of-day requirement).
- **Saturday placement:** Both Chokerman Run 2 and Speed Climb Run 2 go to Saturday. Chokerman Run 2 is placed at the end of the last flight per the existing rule. Speed Climb Run 2 is placed via the college overflow integration (round-robin across flights, or judge-selected position).

Obstacle Pole is single-run in both college and pro divisions for 2026.

**Caber Toss:** Caber Toss has `requires_dual_runs=True` in config.py but does NOT split across days -- both runs occur on Friday. The day-split rule applies only to Chokerman's Race and Speed Climb.

**Other two-run events:** The heat generator creates run 1 and run 2 heats automatically and swaps stand assignments between runs for all events with `requires_dual_runs=True`. The best (lowest) time counts.

**Friday event order:**
1. OPEN events first (come-and-go format): Axe Throw, Peavey Log Roll, Caber Toss, Pulp Toss
2. CLOSED events in configured order
3. Chokerman's Race Run 1 (end of day, before Birling)
4. Birling (always last -- double-elimination bracket, runs until complete)

Chokerman's Race Run 2 and Speed Climb Run 2 do NOT appear on the Friday schedule.

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

Two teams are randomly drawn prior to the start of the show. Each team consists of 8 competitors: 2 pro men + 2 pro women + 2 college men + 2 college women. Teams arrange themselves in any order they choose to complete four events: partnered sawing (any gender combination), Standing Butcher Block, Underhand Butcher Block, and team axe throw. There is no fixed competitor order or role assignment — the teams decide internally. The relay is fit into one flight. Pro competitors opt in via their entry form (`pro_am_lottery_opt_in` on `ProCompetitor`). College competitors are all eligible. Results do not count toward college team or individual scores. This is the only event where college competitors can win money.

The lottery implementation in `services/proam_relay.py` enforces strict gender balance when drawing teams: exactly 2 male + 2 female from each division per team.

### Partnered Axe Throw

Pre-show prelims: all pairs throw, scores recorded as hits. Top 4 pairs advance. Finals run during the show with one pair per flight. Full standings combine: finals results for positions 1-4, prelim standings for positions 5+. The `PartneredAxeThrow` class manages this as a three-stage state machine: `prelims → finals → completed`.

### Saturday College Overflow

The second run of Chokerman's Race must always be conducted on Saturday — this is non-negotiable and must be accounted for when building the Saturday schedule. Additional college events may roll to Saturday only when Friday scheduling requires it. The following priority order governs which events are candidates for Saturday overflow (in order of preference):

1. Men's Standing Block Speed
2. Men's Standing Block Hard Hit
3. Women's Standing Block Speed
4. Women's Standing Block Hard Hit
5. Men's Obstacle Pole

Saturday overflow college events are judged and scored as normal college events — points count toward individual and team college totals. College overflow integration is handled by `integrate_college_spillover_into_flights()` in the flight builder. Manual event ordering within the day schedule and manual heat ordering within flights is supported via drag-and-drop UI on the Events & Schedule page and the Flight Builder page.

### Friday Night Feature

The Friday Night Feature is an optional overflow or special events session run after the college day concludes on Friday. It is built separately from both the main college and pro schedules. Typical events are Collegiate 1-Board, Pro 1-Board, and Pro 3-Board Jigger. The `Tournament` model has a `friday_feature_date` field to record when this session is held, but no dedicated scheduling UI, heat generation logic, or flight structure exists for it. Flag as a known gap (see Section 5).

---

## 4. DATA MODEL

### Tournament

Root entity for one year's competition. Fields: `id`, `name`, `year`, `college_date`, `pro_date`, `friday_feature_date`, `status` (setup/college_active/pro_active/completed), `providing_shirts` (Boolean — whether the show provides shirts; controls shirt_size collection on pro entry forms), `schedule_config` (TEXT — JSON schedule config persisted to DB; helpers `get_schedule_config()` / `set_schedule_config()`), `created_at`, `updated_at`. Relationships: teams, college_competitors, pro_competitors, events (all cascade delete).

### Team

College only. Fields: `id`, `tournament_id`, `team_code` (e.g., UM-A), `school_name`, `school_abbreviation`, `total_points`, `status` (active/invalid), `validation_errors` (JSON TEXT). Unique constraint on `(tournament_id, team_code)`. Methods: `recalculate_points()` sums member individual points; `get_validation_errors()` returns list of structured error dicts; `set_validation_errors(errors)` stores JSON and sets status to 'invalid'. Properties: `member_count`, `male_count`, `female_count`, `is_valid` (checks min 2 per gender, max 8 total).

### CollegeCompetitor

Fields: `id`, `tournament_id`, `team_id`, `name`, `gender` (M/F), `individual_points`, `events_entered` (JSON list — stores event **names** as strings, e.g. `"Underhand Hard Hit"`, for both college registration and the Excel importer; the service layer must fall back from ID-lookup to name-lookup to resolve them), `partners` (JSON dict event_id → partner_name), `gear_sharing` (JSON dict event_id → partner_name), `strathmark_id` (String(50), nullable, indexed — populated by `strathmark_sync.push_college_event_results()` when a name match is found in the global STRATHMARK DB; college-only competitors without a global profile remain NULL), `status` (active/scratched). Methods: `add_points()` updates individual total and triggers team recalculation; `get_gear_sharing()` returns the dict; `set_gear_sharing(event_id, partner_name)` updates it. Both `CollegeCompetitor` and `ProCompetitor` store gear-sharing identically as a dedicated `gear_sharing` TEXT column (JSON dict event_id → partner_name).

Note: `closed_event_count` property filters by CLOSED event classification using a name-set intersection (fixed 2026-04-10 V2.8.2 — prior code compared names against an ID set and always returned 0).

### ProCompetitor

Fields: `id`, `tournament_id`, `name`, `gender`, `address`, `phone`, `email`, `shirt_size`, `is_ala_member`, `pro_am_lottery_opt_in`, `is_left_handed_springboard`, `springboard_slow_heat` (Boolean — competitor should be grouped in the dedicated slow-cutter heat), `events_entered` (JSON — stores event **names** as strings, same as college; resolvers must try ID first then name via `Event.name`/`Event.display_name`), `entry_fees` (JSON dict event_id → amount), `fees_paid` (JSON dict event_id → bool), `gear_sharing` (JSON dict event_id → partner_name), `partners` (JSON dict event_id → partner_name), `total_earnings`, `payout_settled` (Boolean), `strathmark_id` (String(50), nullable, indexed — deterministic portable ID generated at registration; format: `{FirstInitial}{LastName}{GenderCode}` e.g. `AKAPERM`; populated by `strathmark_sync.enroll_pro_competitor()`; used by result push functions to reference the global STRATHMARK Supabase DB), `status`. Import tracking fields: `submission_timestamp`, `gear_sharing_details`, `waiver_accepted`, `waiver_signature`, `notes`, `total_fees`, `import_timestamp`. Properties: `total_fees_owed`, `total_fees_paid`, `fees_balance`.

### Event

Fields: `id`, `tournament_id`, `name`, `event_type` (college/pro), `gender` (M/F/None), `scoring_type` (time/score/distance/hits/bracket), `scoring_order` (lowest_wins/highest_wins), `is_open` (college OPEN/CLOSED flag), `is_handicap` (Boolean, default False — Championship vs. Handicap format; applies to underhand/standing_block/springboard Speed events only; Hard Hit events always Championship; controlled via `config.HANDICAP_ELIGIBLE_STAND_TYPES`), `is_partnered`, `partner_gender_requirement` (same/mixed/any), `requires_dual_runs`, `requires_triple_runs` (Boolean — event uses 3-run cumulative scoring format), `stand_type`, `max_stands`, `has_prelims`, `payouts` (JSON dict position → amount), `status` (pending/in_progress/completed), `is_finalized` (Boolean — scoring locked; payouts distributed; prevents further edits). Relationships: heats, results.

The `payouts` JSON field is repurposed by `ProAmRelay` and `PartneredAxeThrow` to store their state, and by `BirlingBracket` to store bracket data. This is a deliberate design decision to avoid extra tables.

### EventResult

Fields: `id`, `event_id`, `competitor_id` (integer, not FK), `competitor_type` (college/pro), `competitor_name`, `partner_name`, `result_value`, `result_unit`, `run1_value`, `run2_value`, `best_run`, `run3_value` (Float, nullable — third run for triple-run events), `tiebreak_value` (Float, nullable — result from a throwoff run to break a tie), `throwoff_pending` (Boolean — two or more tied competitors need a throwoff before positions can be finalized), `handicap_factor` (Float, default 0.0 — STRATHMARK start mark in seconds; `_metric()` subtracts this from raw time when `event.is_handicap is True` and `scoring_type == "time"`; 0.0 = scratch/no mark; 1.0 = real 1-second start mark), `predicted_time` (Float, nullable — HandicapCalculator predicted completion time in seconds; stored by `mark_assignment.assign_handicap_marks()` for use by `_record_prediction_residuals_for_pro_event()` after finalization; NULL = mark assignment not run), `final_position`, `points_awarded`, `payout_amount`, `is_flagged` (Boolean — score flagged as statistical outlier by `_flag_score_outliers()`), `status` (pending/completed/scratched/dnf), `version_id` (Integer — optimistic locking). Methods: `calculate_best_run()` sets `best_run` and `result_value` to the lower of run1/run2; `calculate_cumulative_score()` sums all run values for triple-run events.

### Heat

Fields: `id`, `event_id`, `heat_number`, `run_number` (1 or 2), `competitors` (JSON list of competitor IDs), `stand_assignments` (JSON dict competitor_id → stand_number), `status`, `version_id` (Integer — optimistic locking), `locked_by_user_id` (FK→users, nullable — exclusive lock held by a scorer), `locked_at` (DateTime, nullable — timestamp when lock was acquired), `flight_id` (nullable FK to flights), `flight_position` (nullable Integer — 1-based display order within a flight). Methods: `get_competitors()`, `set_competitors()`, `add_competitor()`, `remove_competitor()`, `get_stand_assignments()`, `set_stand_assignment()`, `is_locked()`, `acquire_lock(user_id)`, `release_lock()`.

### HeatAssignment

Separate table for heat assignments: `id`, `heat_id`, `competitor_id`, `competitor_type`, `stand_number`. Note: both `Heat.competitors` (JSON) and `HeatAssignment` rows exist in the codebase. The JSON field on Heat is the primary mechanism used by the heat generator; HeatAssignment is used by the validation service. These can diverge — this is a consistency gap to watch.

### Flight

Fields: `id`, `tournament_id`, `flight_number`, `name`, `status`, `notes`. Relationship: heats (via `Heat.flight_id`). Properties: `heat_count`, `event_variety`. The flight builder works by setting `heat.flight_id` directly.

### SchoolCaptain

One PIN-protected account per school per tournament. Fields: `id`, `tournament_id`, `school_name`, `pin_hash`, `created_at`. Unique constraint on `(tournament_id, school_name)`. Methods: `has_pin` (property), `set_pin(pin)`, `check_pin(pin)`. One SchoolCaptain account covers all teams from that school (e.g., UM-A, UM-B, UM-C) automatically by matching `Team.school_name`. Session auth stored in `session['school_portal_auth']` keyed as `'{tournament_id}:school:{school_name.lower()}'`.

### WoodConfig

Per-tournament wood species and size configuration for Virtual Woodboss. Fields: `id`, `tournament_id` (FK), `config_key` (TEXT), `species` (TEXT), `size_value` (FLOAT), `size_unit` ('in'|'mm', default 'in'), `notes` (TEXT), `count_override` (INTEGER, nullable). UniqueConstraint on `(tournament_id, config_key)` named `uq_wood_config_tournament_key`. Methods: `display_size()` returns formatted string (e.g., "12 in" or "300 mm").

Config key conventions:
- Block events: `block_{category}_{type}_{gender}` — e.g. `block_underhand_college_M`
- Relay blocks (manual count): `block_relay_underhand`, `block_relay_standing`
- Saw logs: `log_general`, `log_stock`, `log_relay_doublebuck`
- `count_override` on relay keys = judge-entered team/cut count (lottery-determined)
- Stock Saw falls back to general log species/size if `log_stock` is not configured
- `log_relay_doublebuck` falls back to `log_general` species/size if not set explicitly

### ProEventRank

Per-tournament ability ranking for pro competitors. Used by heat generation to group competitors by predicted performance. Fields: `id`, `tournament_id`, `competitor_id` (FK to pro_competitors), `event_category` (TEXT), `rank` (Integer, 1 = best). Unique constraint on `(tournament_id, competitor_id, event_category)`. Nine ranked categories: `springboard`, `pro_1board`, `3board_jigger`, `underhand`, `standing_block`, `obstacle_pole`, `singlebuck`, `doublebuck`, `jack_jill`. Unranked competitors are placed after ranked ones during heat generation. Managed via `/scheduling/<tid>/ability-rankings` (drag-and-drop SortableJS UI). Also supports College Birling Seedings — per-school ordering stored as `pre_seedings` in `Event.payouts` JSON.

### PayoutTemplate

Reusable payout configuration templates for quick event setup. Tournament-independent — one template can be applied to events across any tournament. Fields: `id`, `name` (TEXT, unique), `payouts` (JSON TEXT — dict mapping position int → dollar amount), `created_at`. Methods: `get_payouts()` returns the dict; `set_payouts(d)` stores it; `total_purse()` returns sum of all payout values.

### Key Relationships Summary

```
Tournament
  ├── Team (1:many, cascade delete)
  │     └── CollegeCompetitor (1:many, via team_id)
  ├── CollegeCompetitor (1:many, cascade delete)
  ├── ProCompetitor (1:many, cascade delete)
  │     └── ProEventRank (1:many, via competitor_id + tournament_id)
  ├── SchoolCaptain (1:many, cascade delete via tournament_id)
  ├── WoodConfig (1:many, cascade delete-orphan via wood_configs relationship)
  └── Event (1:many, cascade delete)
        ├── Heat (1:many, cascade delete)
        │     └── HeatAssignment (1:many)
        └── EventResult (1:many, cascade delete)

Flight
  ├── tournament_id (FK to tournaments)
  └── Heat.flight_id (FK, optional)

PayoutTemplate  (tournament-independent, standalone)
```

### College Excel Import

`services/excel_io.py` imports college entry forms via `process_college_entry_form()`. It reads sheet 0, normalizes column names to lowercase, and tries to detect format by looking for `school` or `team` columns. It supports flexible column name matching (e.g., `school`, `university`, `college`, `institution`). It creates Team and CollegeCompetitor records, deduplicating by name within a team. It generates team codes in the pattern `ABBREV-A`, `ABBREV-B`, etc. A hardcoded `_abbreviate_school()` dict covers common schools (UM, MSU, CSU, UI, OSU, UW, HSU, CP); others get generated from initials.

---

## 5. CURRENT STATE & KNOWN GAPS

### Features Functionally Complete

- Tournament creation and lifecycle management (setup → college_active → pro_active); tournament clone route
- College team import from Excel (flexible column detection)
- Pro competitor manual registration; pro entry xlsx importer (Google Forms round-trip, duplicate detection, alias map); enhanced registration import pipeline (`services/registration_import.py`) with dirty-file support (garbage partner detection, first-name fuzzy matching, gear text parsing, dedup by timestamp, gender-event cross-validation, partner reciprocity, gear sharing inference from partnerships, structured import report)
- Event configuration from `config.py` event lists, with OPEN/CLOSED designation choice
- Heat generation: snake-draft distribution, stand capacity, springboard left-hand grouping, dual-run heat creation
- Heat swap/edit: move competitors between heats (`/scheduling/<tid>/heats/swap`)
- Heat sync check (GET JSON) + sync fix (POST reconcile) to keep `Heat.competitors` and `HeatAssignment` rows consistent
- Heat sheets print page (`/scheduling/<tid>/heat-sheets`)
- `Heat.sync_assignments(competitor_type)` method: syncs `HeatAssignment` rows from the authoritative `Heat.competitors` JSON; called after heat generation, flight rebuild, and competitor moves
- Flight builder: greedy competitor-spacing (min 4 heats between appearances, target 5); per-event sequential queue (heats within any event appear in ascending heat_number order); Cookie Stack / Standing Block mutual exclusion enforced via `_CONFLICTING_STANDS` in `flight_builder.py`; springboard flight opener (`_promote_springboard_to_flight_start`) places a springboard heat at position 0 of each flight block
- Unified Events & Schedule page (`/scheduling/<tid>/events`): single-page heat generation, flight build, and spillover integration; stand count override inputs; session-stored schedule options; schedule preview; one-click "Generate All College Heats" bulk action
- Collapsible tournament sidebar (`templates/_sidebar.html`): sticky 220px/44px sidebar with 5 sections (Show Entries, Show Configuration, Scoring, Results, Admin); localStorage state; unscored-heats badge
- Score entry: standard events (single-value and dual-run); score outlier flagging (`_flag_score_outliers()` in scoring.py, ⚠ badge in results)
- Position calculation and point/payout award on finalization
- College standings: Bull/Belle of the Woods, team standings; live 30s polling in spectator portal
- Pro payout summary with integrated settlement (Paid/Pending toggle, settlement stats)
- All-results report (screen and printable); event-level result reports (screen and printable)
- Pro-Am Relay lottery (opt-in, gender-balanced draw, result entry, standings)
- Partnered Axe Throw state machine (prelim registration, prelim scoring, top-4 advance, finals, full standings)
- Birling bracket: full double-elimination bracket generation with advance logic (`_advance_winner`, `_drop_to_losers`, `_advance_loser_winner`); bracket viewer route + `birling_bracket.html`
- Validation service for teams, college competitors, pro competitors, heat constraints; validation API (JSON) endpoints
- Saturday priority route (`/scheduling/<tid>/college/saturday-priority`) for college overflow event flagging
- College Saturday overflow flight integration: `integrate_college_spillover_into_flights()` places Chokerman's Race Run 2 at the end of the last flight in heat-number order; other overflow events distributed round-robin; wired to `event_list` POST actions
- Friday Night Feature: route, config (JSON in `instance/`), and template (`/scheduling/<tid>/friday-feature`) — UI exists, no heat generation or flight integration
- Flask-Login authentication: 7 roles (admin, judge, scorer, registrar, competitor, spectator, viewer); login/logout/bootstrap/user-management; audit log viewer (`/auth/audit`, admin-only, paginated, filterable)
- `require_judge_for_management_routes` before_request hook; portal and auth routes are public
- Portal — spectator: college standings (live), pro standings, event results, relay results; mobile/desktop view toggle; kiosk TV display (`/portal/kiosk/<tid>`, 4-panel 15s rotation)
- Portal — pro competitor: individual access via name search + PIN; dashboard with schedule, results, and SMS opt-in
- Portal — school captain: one account per school covers all teams; 4-tab dashboard (overview, teams/members, schedule, Bull & Belle); PDF export via browser print
- In-app user guide (`/portal/guide`): 6-role guide with sticky sidebar; first-time onboarding popups for spectators, pro competitors, and school captains (localStorage-tracked, skippable)
- Public REST API (`/api/public`): standings, schedule, results, standings-poll endpoints
- AuditLog model + `services/audit.py` for immutable audit trail
- Headshot upload: JPEG/PNG/WebP magic-byte validation; stored with UUID filename
- SMS notifications (`services/sms_notify.py`): Twilio flight start/complete alerts; graceful no-op if not configured
- Service worker (`static/sw.js`): offline cache + IDB queue + Background Sync; offline queue UI (`static/offline_queue.js`)
- Cloud backup (`services/backup.py`): S3 if env vars set, local `instance/backups/` fallback; triggered from tournament detail
- Team validation framework: `Team.validation_errors` JSON column; `_validate_college_entry_constraints()` in excel_io returns structured errors; partial success import (valid teams commit, invalid teams tracked); fix forms in `team_detail.html` per error type
- Virtual Woodboss (`routes/woodboss.py`, `services/woodboss.py`, `models/wood_config.py`): complete material planning — block counts, saw log linear footage, Pro-Am Relay blocks + double buck, lottery view, cross-tournament history, HMAC share link
- Preflight checks service (`services/preflight.py`, `templates/scheduling/preflight.html`): pre-scheduling validation — heat/table sync, odd partner pools, Saturday overflow
- Partner matching service (`services/partner_matching.py`): auto-partner assignment logic for pro partnered events; bidirectional validation and gender matching (service exists; no UI route yet)
- Handicap export helpers (`services/handicap_export.py`): chopping-event Excel export utilities
- Gear Sharing Manager (`routes/registration.py`, `services/gear_sharing.py`, `templates/pro/gear_sharing.html`): comprehensive pro gear-sharing audit — verified pairs, unresolved entries, heat conflicts; free-text parse with review workflow; gear groups (multiple pairs sharing one saw); bidirectional sync; heat conflict auto-fix; auto-populate partners; cleanup scratched; college gear constraints view/edit; printable report
- Fee Tracker (`routes/reporting.py`, `templates/reporting/fee_tracker.html`): entry fee collection checklist per pro competitor; per-event breakdown expandable rows; mark/unmark paid; outstanding-only filter; summary cards
- Tournament Setup consolidated page (`routes/main.py`, `templates/tournament_setup.html`): single `/tournament/<tid>/setup` page with tabs for Events, Wood Specs, and Settings (name/year/dates); wood specs and copy-from now redirect back to setup when called from this page
- Tournament Detail redesigned (`templates/tournament_detail.html`): 3-phase action panels (Before Show / Game Day / After Show); 6-step workflow stepper; contextual next-step banner per status; stats bar with actionable alerts; async validation status banner
- Show Day Dashboard (`/scheduling/<tid>/show-day`, `templates/scheduling/show_day.html`): flight status cards (live/completed/pending), current heat CTA, upcoming heats, college event progress bars; 60s auto-refresh
- Scoring Engine (`services/scoring_engine.py`): centralized position calculation with per-type tiebreak strategies (hard-hit, axe throw, default); tie detection; throwoff workflow (`throwoff_pending` flag, `record_throwoff_result()`); triple-run cumulative scoring (`Event.requires_triple_runs`, `EventResult.run3_value`); `Event.is_finalized` lock; `EventResult.handicap_factor` placeholder for STRATHMARK; payout template CRUD (`models/payout_template.py`); bulk CSV result import; live spectator poll data
- Pro Ability Rankings (`/scheduling/<tid>/ability-rankings`, `templates/scheduling/ability_rankings.html`): assign rank 1-N per competitor per event category (7 categories: springboard, underhand, standing_block, obstacle_pole, singlebuck, doublebuck, jack_jill); rank-ordered heat grouping for improved competitive balance
- Heat locking for concurrent score entry: `Heat.locked_by_user_id` + `Heat.locked_at`; `acquire_lock()` / `release_lock()` / `is_locked()` methods prevent simultaneous edits by multiple scorers
- Heat sheet enhancements (V1.8.0): per-flight pill filter tabs; two-column print layout with event color bands (pro=blue, college=mauve); drag-and-drop heat reordering (SortableJS); judge annotation notes-lines toggle; Cookie Stack / Standing Block conflict warning badges; competitor spacing heatmap; stand-assignment color coding (8 distinct colors); dual-run side-by-side layout; flight build progress overlay; flight build diff summary modal; 4-step scheduling wizard indicator
- Competitor self-service portal (`/portal/competitor/<tid>/<type>/<id>/my-results`): PIN gate (`competitor_pin_gate.html`) + personal results dashboard (`competitor_my_results.html`); shows events entered, heat assignments with stand/flight/run, personal results with position and payout, gear-sharing partners; mobile/full view toggle
- Heat sheet PDF export (`/scoring/<tid>/heat/<hid>/pdf`): WeasyPrint PDF if installed, graceful fallback to print-styled HTML; standalone `heat_sheet_print.html` template with `@page` CSS; "Print / Save as PDF" button; "Print Heat Sheet" link in `enter_heat.html`
- Async heat/flight generation (`/scheduling/<tid>/events/generate-async`, `/scheduling/<tid>/events/job-status/<job_id>`): wraps `background_jobs.submit()`; returns 202 + `job_id`; poll endpoint returns status/progress; app context created inside async worker via `current_app._get_current_object()`
- Rate limiting: `write_limit()` decorator in `routes/api.py`; `_init_write_limiter(app)` called in `create_app()`; `enter_heat_results` capped at 60/min, `finalize_event` at 10/min
- STRATHMARK live integration (`services/strathmark_sync.py`, `routes/strathmark.py`, migration `k8l9m0n1o2p3`): pro competitor enrollment at registration; pro SB/UH result push on finalization; college SB Speed / UH Speed result push with name-match ID resolution; `GET /strathmark/status` sync status page; all calls non-blocking; `strathmark_id` column on both competitor models; local cache files in `instance/` track push timestamps and skipped college competitors
- N+1 query fix in `routes/api.py` (`public_schedule`, `public_results`): batch `.filter(Heat.event_id.in_(event_ids)).all()` + `defaultdict` grouping replaces per-event lazy loads
- N+1 query fix in `services/flight_builder.py`: single batch query for all non-axe event heats (run_number==1) replaces per-event `.filter_by(run_number=1).all()` loop
- `FlightBuilder` class in `services/flight_builder.py`: OO wrapper with `build(num_flights=None)`, `integrate_spillover(saturday_college_event_ids)`, and `spacing(event)` methods
- JSON resilience: `JSONDecodeError` guards on all model `.get_*()` methods — `CollegeCompetitor.get_events_entered/get_partners/get_gear_sharing`, `ProCompetitor` equivalents, `Event.get_payouts()`, `Heat.get_competitors/get_stand_assignments()`; return empty list/dict instead of propagating decode error
- Name validation: `@validates('name')` on both `CollegeCompetitor` and `ProCompetitor`; truncates at `MAX_NAME_LENGTH = 100` characters silently
- `StaleDataError` / `IntegrityError` split in scoring: `StaleDataError` shows user-friendly "another judge edited this — reload" warning; `IntegrityError` shows database constraint violation error; each rolls back independently
- Error leakage fix in `routes/registration.py`: `except Exception` no longer exposes `str(e)` to users; unexpected errors show a generic admin-contact message
- Transactional rollback in scheduling: each `generate_all` / `rebuild_flights` / `integrate_spillover` action wrapped in `try/except db.session.rollback()` to prevent half-committed state
- Logging: `logger.info(...)` added at entry of `calculate_positions()` in `scoring_engine.py` and `generate_event_heats()` in `heat_generator.py`
- API v1 prefix: `api_bp` registered at `/api/v1/` (name='api_v1') in addition to `/api/` for forward-compatible clients
- Bootstrap 5 conflict modal in `enter_heat.html`: 409 conflict response shows modal with server message and reload link instead of inline alert
- Test coverage: 5 new test classes in `tests/test_scoring.py` — `TestFlagOutliersEdgeCases`, `TestDetectAxeTiesEdgeCases`, `TestSortKeyTiebreaks`, `TestPendingThrowoffs`, `TestImportResultsFromCSV`
- Scheduling blueprint decomposed into package (`routes/scheduling/`): 9 sub-modules; all 24 URL paths identical; shared helpers in `__init__.py`; original monolithic `routes/scheduling.py` removed
- Route smoke tests (`tests/test_routes_smoke.py`): Flask test client + in-memory SQLite; covers all blueprints; asserts no 500/502/503
- Handicap scoring math in `scoring_engine._metric()` (V2.3.0): subtracts `handicap_factor` start mark from raw time when `event.is_handicap` + `scoring_type=="time"`; 1.0/None treated as scratch; 11 unit tests in `TestHandicapScoring`
- STRATHMARK mark assignment service (`services/mark_assignment.py`, V2.3.0): `assign_handicap_marks()` queries HandicapCalculator; `is_mark_assignment_eligible()` guard; non-blocking + graceful no-op if package missing; import path fixed to `strathmark.calculator` (V2.4.0)
- Mark assignment route + template (`/scheduling/<tid>/events/<eid>/assign-marks`, V2.3.0): GET status page, POST trigger; audit log; `templates/scheduling/assign_marks.html`; STRATHMARK logo added at top (V2.4.0)
- PostgreSQL migration guide (`docs/POSTGRES_MIGRATION.md`, V2.3.0): full SQLite-to-PG data migration procedure with inline script; Railway deployment checklist
- `EventResult.predicted_time` (Float, nullable, V2.4.0): stores HandicapCalculator predicted completion time; wired in `assign_handicap_marks()` for loop; used by `_record_prediction_residuals_for_pro_event()` after finalization; migration `d8d4aa7bdb45`
- `_record_prediction_residuals_for_pro_event()` in `strathmark_sync.py` (V2.4.0): records per-competitor prediction residuals (actual − predicted) to STRATHMARK Supabase bias-learning table after pro SB/UH finalization; called at end of `push_pro_event_results()`; non-blocking
- Design review accessibility fixes (V2.6.0): `color-scheme: dark` on `html` element for native dark mode control rendering; 44px minimum touch targets on `.navbar-proam .nav-link` and `.btn-proam`; `prefers-reduced-motion` media query zeroes all animation/transition durations; disabled button `cursor: not-allowed`
- Design review visual polish (V2.6.0): staggered `card-enter` fade-up animation on role cards (80ms stagger); hover lift on `.card.shadow-sm` (spectator portal cards); semantic `<h1>` for hero title on role entry page
- Design review empty states (V2.6.0): warm empty states with icons + descriptive messages across `spectator_college.html`, `spectator_pro.html`, `kiosk.html` — replaces bare "No rankings yet." / "No completed events yet." text
- STRATHEX design system (`static/css/theme.css`, V2.5.0): extracted dark theme tokens, fire palette, component overrides from `base.html`; standalone CSS file for maintainability
- Comprehensive test suite (V2.5.0): 37 new test files + shared `conftest.py` + synthetic data fixtures (`tests/fixtures/synthetic_data.py`); covers models, routes, services, scoring, gear sharing, flight builder, heat gen, templates, infrastructure, security, PostgreSQL compat; `pytest.ini` with smoke/integration/slow markers
- Tournament payouts hub (`templates/scoring/tournament_payouts.html`, V2.5.0): bulk template application, distribution visualization bars, event summary table, new template builder
- STRATHMARK status template (`templates/strathmark/status.html`, V2.5.0): moved from inline HTML in route to Jinja2 template; Bootstrap 5 styled status cards
- Enhanced scoring UI (V2.5.0): tablet mode toggle, mobile numpad overlay, conflict warning modal, run-2 context chips
- Enhanced gear sharing (V2.5.0): advanced batch operations, domain knowledge docs (`docs/GEAR_SHARING_DOMAIN.md`)
- Enhanced dashboard (V2.5.0): command centre hero, live aggregate stats, quick launch sidebar, onboarding checklist
- Enhanced login page (V2.5.0): dual-logo lockup, redesigned card styling
- Developer tooling (V2.5.0): `pyrightconfig.json` (Pyright/Pylance), `.vscode/launch.json` (debugger), `favicon.svg`
- Competitor `display_name` property (V2.8.0): `CollegeCompetitor.display_name` returns `"Name (TeamCode)"`; `ProCompetitor.display_name` returns plain name; used across all UI surfaces instead of raw `.name`
- Handicap factor sentinel fix (V2.8.0): `EventResult.handicap_factor` default changed from `1.0` to `0.0`; `0.0` = scratch, `1.0` = real 1-second mark
- Springboard category split (V2.8.0): 9 ability ranking categories (added `pro_1board`, `3board_jigger`); `event_rank_category()` differentiates by event name
- Ability rankings drag-and-drop UI (V2.8.0): SortableJS ranked/unranked zones replace text inputs; College Birling Seedings with per-school ordering stored as `pre_seedings`
- Payout settlement merged into payout summary (V2.8.0): settlement status + Mark Paid toggle on Pro Payout Summary; old route 301-redirects; `payout_settlement.html` deleted
- Virtual Woodboss wood presets (V2.8.0): species presets (built-in + custom); preset CRUD routes; saw wood tracks `(comp_type, gender)`; springboard dummies split by board height + day
- Pro-Am Relay manual team builder (V2.8.0): `set_teams_manually()` service method; drag-and-drop team builder page; gender count validation
- Partnered Axe Throw scoring integration (V2.8.0): prelim scores sync to `EventResult` records; inline prelim scoring on event results page
- Payout state protection (V2.8.0): `Event.uses_payouts_for_state` property blocks payout config for state-events (relay, axe throw, birling)
- Finalization validation warnings (V2.8.0): `validate_finalization()` checks missing payouts, unassigned marks, pending throwoffs
- Post-finalize payout recalculation (V2.8.0): saving payouts on finalized event re-runs `calculate_positions()`
- College Excel import improvements (V2.8.0): school name from filename, team column auto-detection, `school_abbr-letter` team codes, fuzzy partner matching (Levenshtein ≤ 2), roster validation (min 2M/2F, max 8)
- Gear sharing group aggregation (V2.8.0): union-find connected-component grouping; group-based display replaces pair-based
- Birling bracket on heat sheets (V2.8.0): winners/losers bracket, grand finals, placement table rendered on heat sheet print pages
- ALA report email (V2.8.0): PDF generation + SMTP delivery to ALA; sidebar link
- Print button fix (V2.8.0): `[data-print]` JS handler added to 9 print templates
- Scoring engine throwoff fix (V2.8.0): `record_throwoff_result()` uses canonical `PLACEMENT_POINTS_DECIMAL` lookup

### Known Gaps and Incomplete Features

**Excel results export route (direct download):** `export_results_to_excel()` exists in `services/excel_io.py`. An async background export job route exists at `/reporting/<tid>/export-results/async`, but no route triggers a direct synchronous Excel download. The async job approach is the recommended path forward — completing the status/download endpoint would close this gap.

**Friday Night Feature heat generation and flight integration:** The Friday Night Feature has a route, config storage, and UI template. However, no heat generation logic or flight integration exists for it. Heats and flights for Friday Night Feature events must be managed manually or via the standard event/heat flow.

**Pro event fee configuration UI:** No route or template exists for setting fee amounts per event per tournament. `entry_fees` and `fees_paid` fields exist on `ProCompetitor` but fees must currently be set directly in the database or via the edit competitor form.

**Pro birling references:** `config.py` PRO_EVENTS correctly excludes birling. Verify that no templates, database records, or service code contain hardcoded references to a pro birling event that could create phantom data.

**STRATHMARK integration:** Live data push wired (V2.2.0); handicap scoring math + mark assignment route wired (V2.3.0); prediction residuals infrastructure + `predicted_time` column added (V2.4.0); batched pipeline with full data context + CSV/manual offline paths wired (V2.7.0 via PR #6). Remaining gap: heat ability-weighting via STRATHMARK predictions — `optimize_flight_for_ability()` in `flight_builder.py` and ability-weighted input to `_generate_event_heats()` in `heat_generator.py` are still stubs. See Section 7.

**Pro entry form redesign:** Scope pending. Current import flow handled by `services/pro_entry_importer.py` (basic parsing) and `services/registration_import.py` (enhanced pipeline with dirty-file support, fuzzy matching, cross-validation, and structured report). See `routes/import_routes.py` for the upload → review → confirm workflow.

**Authentication:** Flask-Login is active. Management blueprints (main, registration, scheduling, scoring, reporting, proam_relay, partnered_axe, validation, import_pro) require `is_judge` (admin or judge role) via `require_judge_for_management_routes` in `app.py`. Auth routes (`/auth/*`) and portal routes (`/portal/*`) are public. Bootstrap endpoint (`/auth/bootstrap`) creates the first account when DB has no users — it locks itself afterward. Seven defined roles: admin, judge, scorer, registrar, competitor, spectator, viewer.

**CSRF protection:** Flask-WTF `CSRFProtect` is active. All POST form templates include `{{ csrf_token() }}`. JSON API endpoints (routes/validation.py) that do not submit HTML forms are GET-only and require no exemption. If a new POST endpoint returns JSON rather than HTML, apply `@csrf.exempt` from `app.csrf`.

---

## 6. DEVELOPMENT RULES

Keep all code lean and simple — functionality first, then simplicity and ease of understanding.

Do not create new routes in `app.py` — all routes belong in the `routes/` directory as blueprints.

Do not create new database logic in `app.py` — all DB logic belongs in `models/` or `services/`.

Preserve all existing comments and notes in their original format and style when making changes.

Plain text outputs only in UI — no emojis, no color codes.

Safe error handling: failures print descriptive messages but allow continued operation. Wrap risky operations in try/except and flash a descriptive error rather than raising to the user or silently swallowing the failure.

Database schema changes: do not use `db.create_all()` for schema modifications on an existing database. `db.create_all()` only creates tables that do not yet exist — it does not alter existing tables. Use Flask-Migrate (Alembic) for all schema changes after initial setup. If Flask-Migrate is not yet initialized in this project, flag it and ask before proceeding with any schema change that would modify an existing table.

### Migration Protocol (MANDATORY — read every time you touch models or migrations)

This protocol exists because auto-generated migrations have repeatedly introduced silent schema drift — changing nullable flags, dropping indexes, and altering defaults on columns unrelated to the intended change. These bugs are expensive to diagnose and fix. **Follow every step exactly.**

#### Step 1: Before generating a migration

- **Only modify model columns you intend to change.** Do not "clean up" nullable, defaults, or server_defaults on existing columns while adding a new column. Each concern gets its own migration with a descriptive name.
- **Check for existing drift first.** Run `flask db check` (or `flask db migrate --dry-run` if available). If Alembic detects changes you did NOT make, do NOT proceed. Investigate why the models and DB are already out of sync. Fix the root cause (wrong model definition or wrong migration) before generating anything new.

#### Step 2: Generate the migration

```bash
flask db migrate -m "descriptive_name_of_what_changed"
```

#### Step 3: Review the generated file (NEVER SKIP THIS)

Open the new file in `migrations/versions/` and verify **every line** of the `upgrade()` and `downgrade()` functions:

1. **Only intended columns appear.** If the migration touches tables or columns you did not modify, DELETE those lines. They are Alembic detecting model/DB drift from a prior session — fixing that drift in an unrelated migration is how bugs 90% of the time are introduced.
2. **`nullable` matches the model.** If your model says `db.Column(db.Boolean, default=False)` (no `nullable=True`), the migration must say `nullable=False`. Alembic sometimes infers `nullable=True` for SQLite batch operations — correct it.
3. **`server_default` matches the model.** Boolean columns with `default=False` should have `server_default=sa.text('0')` (or `'false'` for PostgreSQL). If the migration omits `server_default`, add it.
4. **No index drops unless intentional.** Search for `drop_index` in the generated file. If you did not intend to drop an index, remove that line.
5. **No type changes unless intentional.** Search for `alter_column` — if it changes `type_`, `nullable`, or `server_default` on a column you did not touch, remove it.
6. **`down_revision` is correct.** It must point to the current HEAD migration. Check against the Migration Chain in MEMORY.md.

#### Step 4: Apply and verify

```bash
flask db upgrade
```

Then run the migration integrity tests:
```bash
pytest tests/test_migration_integrity.py -v
```

These tests compare the migration-produced schema against the ORM model schema and will catch:
- Missing tables or columns
- Column type mismatches (TEXT vs VARCHAR, etc.)
- Nullable mismatches (model says NOT NULL, migration says nullable)
- Server default mismatches
- Missing or extra indexes

If any test fails, **fix the migration file** (not the model) before committing.

#### Step 5: Update the migration chain

After the migration is verified, update the Migration Chain in MEMORY.md:
```
`new_revision` (description) ← `old_head` ← ...
```

#### Common mistakes this protocol prevents

| Mistake | How this protocol catches it |
|---|---|
| `flask db migrate` silently alters unrelated columns | Step 3.1: review shows unexpected `alter_column` lines |
| Boolean column becomes nullable in migration | Step 3.2: review + `test_nullable_parity` catches it |
| Index dropped in wrong migration | Step 3.4: review shows unexpected `drop_index` |
| Schema parity fix needed later | Step 1: check for drift BEFORE generating |
| Migration applied without review | Step 3 is mandatory — never run `flask db upgrade` on an unreviewed migration |
| Fresh DB missing indexes | `test_no_missing_indexes` catches it |

#### What NOT to do

- **Never commit a migration you haven't read line-by-line.** Auto-generated does not mean correct.
- **Never fix drift from a prior session inside a new feature migration.** Create a separate `fix_xyz_drift` migration.
- **Never use `_add_column_if_missing()` or similar idempotent hacks.** If a column is missing, the correct migration was wrong — fix it at the source.
- **Never alter `nullable` or `server_default` on an existing column unless that is the explicit purpose of the migration.**

#### PostgreSQL Compatibility Rules (MANDATORY — production runs on PostgreSQL)

This project deploys to Railway PostgreSQL. SQLite is dev-only. Every migration MUST work on both. These rules exist because SQLite-specific migrations caused a multi-hour production outage (Session 24, 2026-03-24). Tests in `tests/test_pg_migration_safety.py` enforce these rules automatically.

**NEVER use in upgrade() functions:**

| Banned Pattern | Why | Use Instead |
|---|---|---|
| `batch_alter_table` | Reconstructs tables; fails on PG with FKs/indexes | `op.add_column()`, `op.create_index()`, `op.drop_index()` directly |
| `server_default='0'` on Boolean | PG rejects `0` as boolean; needs `'false'` | `server_default='false'` or `server_default=sa.false()` |
| `server_default=sa.text('0')` on Boolean | Same — `sa.text('0')` emits literal `0` | `server_default=sa.text('false')` |
| `PRAGMA table_info(...)` | SQLite-only introspection | `information_schema.columns` query (see `e9f0a1b2c3d4` for dual-dialect pattern) |
| `SET col = 0` in `op.execute()` on Boolean | PG rejects integer for boolean | `SET col = false` |
| `ALTER TABLE ... RENAME` via batch | PG doesn't need it; batch can corrupt | `op.alter_column()` directly |

**Before committing any migration:**
```bash
pytest tests/test_pg_migration_safety.py -v
```
If any test fails, fix the migration before committing. These tests scan all migration files for the patterns above — they catch problems in seconds, not after a 2-hour deploy debugging session.

### Model Column Declaration Rules

Every `db.Column()` call in `models/*.py` must explicitly declare `nullable`. Omitting it causes SQLAlchemy to default to `nullable=True`, which Alembic then bakes into auto-generated migrations — even when the column clearly should be NOT NULL (e.g., Boolean with `default=False`). This ambiguity is the upstream source of most migration drift.

**Required for every new or modified column:**

```python
# CORRECT — explicit nullable, server_default for Alembic visibility
is_active = db.Column(db.Boolean, nullable=False, default=False, server_default=sa.text('0'))
status = db.Column(db.String(20), nullable=False, default='pending', server_default='pending')
notes = db.Column(db.Text, nullable=True)  # intentionally nullable

# WRONG — implicit nullable, Alembic can't see Python-side default
is_active = db.Column(db.Boolean, default=False)  # nullable=True by default!
status = db.Column(db.String(20), default='pending')  # same problem
```

**Rules:**

1. **Always declare `nullable=True` or `nullable=False`** — never rely on the implicit default.
2. **Add `server_default` alongside `default`** for any column with a non-NULL default value. `default=` is Python-side only — raw SQL inserts, migrations, and PostgreSQL bypass it entirely. `server_default=` is what Alembic sees and what the database enforces.
3. **Foreign key columns** may be `nullable=True` (optional relationships) — but declare it explicitly.
4. **The `TestModelColumnDeclarations` test** in `test_migration_integrity.py` flags columns that have a default but are still nullable. When adding new models, run this test to verify your declarations are correct.

HeatAssignment vs Heat.competitors: `Heat.competitors` (JSON field) is the authoritative source for heat composition and is what the heat generator reads and writes. `HeatAssignment` rows are used only by the validation service. All new code reading or writing heat composition must use `Heat.competitors`. After modifying `Heat.competitors`, call `heat.sync_assignments(event.event_type)` (after `db.session.flush()` so `heat.id` is assigned) to keep the two representations consistent.

Input conversion: All `int()` and `float()` calls on POST form data must be wrapped in `try/except (TypeError, ValueError)`. Flash a descriptive error message and redirect rather than raising an unhandled exception that produces a 500 response.

Authentication: Flask-Login is active. New management routes must be covered by the `require_judge_for_management_routes` before_request hook — add the blueprint name to `MANAGEMENT_BLUEPRINTS` in `app.py`. Public endpoints (static files, `main.index`, `main.set_language`, all `auth.*`, all `portal.*`, the `/sw.js` service-worker route) are whitelisted. The bootstrap route at `/auth/bootstrap` locks itself once any user exists — never remove that guard. Seven valid User roles: admin, judge, scorer, registrar, competitor, spectator, viewer.

Cookie Stack and Standing Block stand conflict: any code touching heat generation or flight scheduling must enforce mutual exclusivity of these two events. Cookie Stack (`stand_type: cookie_stack`) and Standing Block (`stand_type: standing_block`) share the same 5 physical stands. Never schedule heats from both events at the same time or within the same flight slot without explicit instruction.

### Test Isolation

Tests MUST NEVER write to or pollute the production database. Always use separate test databases, fixtures, or transactions that roll back. Before writing any test, verify the test config uses an isolated DB connection.

### Project Structure (Multi-Project Workspace)

This workspace contains multiple projects in subdirectories (e.g., KYTHEREX/, STRATHEX/, STRATHMARK/). Always confirm which project/subdirectory the user is referring to before making changes. Check for separate GitHub repos if code isn't found locally.

### Stale Cache

When debugging Python import errors or unexpected behavior where source code looks correct, check for stale `__pycache__`/`.pyc` files first. Run `find . -type d -name __pycache__ -exec rm -rf {} +` as an early diagnostic step.

### Git Workflow

Always check the current branch before running release/deploy workflows. Feature branches are required for PRs and /document-release. Never assume we're on a feature branch.

### Context Retention

When the user references a business name, feature, or prior decision (e.g., 'Pyramid Lumber', 'MT/PNW pivot'), search the codebase and docs for context before claiming ignorance. Check DESIGN.md, README, and recent git history.

---

## 7. RELATIONSHIP TO STRATHEX ECOSYSTEM

This app handles tournament logistics only: registration, heats, flights, results, payouts. It does not yet perform full handicap calculation.

**STRATHMARK** (partially integrated) handles handicap calculation and AI-powered predictions. It is a Python CLI tool using XGBoost and an Ollama LLM, living in the separate STRATHEX repository.

**KYTHEREX** (not yet integrated) is described as a Predictive Engine add-on.

### What Is Already Integrated (V2.3.0)

1. **`Event.is_handicap` (Boolean):** Stores the format choice — Championship (False) or Handicap (True) — per event. Added via migration `j7k8l9m0n1o2`. Default False (Championship).

2. **`config.HANDICAP_ELIGIBLE_STAND_TYPES`:** Set `{'underhand', 'standing_block', 'springboard'}`. Used by `_upsert_event()`, `_create_college_events()`, `_create_pro_events()`, `_get_existing_event_config()`, and both setup templates to gate the toggle. Hard Hit events (scoring_type `'hits'`) are excluded even within eligible stand types.

3. **Tournament setup UI (both `tournament_setup.html` and `scheduling/setup_events.html`):** Championship / Handicap radio toggle appears beneath each eligible event during tournament design. Selection is saved to `Event.is_handicap` on form submit.

4. **`ProCompetitor.strathmark_id` and `CollegeCompetitor.strathmark_id` (String(50), nullable):** Added via migration `k8l9m0n1o2p3`. Deterministic portable ID used to reference the global STRATHMARK Supabase database. Pro IDs are generated at registration; college IDs are resolved by name match against the global competitor list.

5. **`services/strathmark_sync.py`:** Non-blocking STRATHMARK integration layer. `enroll_pro_competitor()` generates a deterministic ID (`{FirstInitial}{LastName}{GenderCode}`, collision-safe) and calls `push_competitors()`. `push_pro_event_results()` pushes finalized pro SB/UH results via `push_results()`. `push_college_event_results()` resolves college competitor IDs via `pull_competitors()` name match and pushes SB Speed / UH Speed results. `is_college_sb_uh_speed()` identifies eligible college events by name (case-insensitive; matches Standing Block Speed, Underhand Speed, SB Speed, UH Speed). Wood species and diameter are looked up from `WoodConfig` using the existing key convention; inches are converted to mm. Local state files: `instance/strathmark_sync_cache.json` (last push timestamp/count), `instance/strathmark_skipped.json` (college competitors skipped due to no global profile). All functions: catch all exceptions, log as warning/error, return False/None — STRATHMARK outages never block app operations.

6. **`routes/strathmark.py` — `GET /strathmark/status`:** Director-only status page showing env-var config state, last push timestamp, global result count for `Missoula Pro-Am` (via `pull_results()` filtered by `show_name`), and a table of skipped college competitors. Self-contained minimal HTML; no Jinja template; no auth required (localhost use). Registered at `/strathmark` prefix in `app.py`; not in `MANAGEMENT_BLUEPRINTS`.

7. **Hook points in existing routes:** `routes/registration.py` `new_pro_competitor` calls `enroll_pro_competitor()` after `db.session.commit()`. `routes/scoring.py` `finalize_event` calls `_push_strathmark_results()` after `invalidate_tournament_caches()` on the success path. `_push_strathmark_results()` dispatches to pro or college push based on `event.event_type` and `event.stand_type`/name.

### Remaining Integration Points

8. **`services/flight_builder.py` — `optimize_flight_for_ability()`:** Stub function (lines 196–213) — the designed hook for STRATHMARK predicted completion times to enable ability-grouped heats for springboard and other time-based events.

9. **`services/heat_generator.py` — `_generate_event_heats()`:** Current snake-draft distribution has no ability-weighting. STRATHMARK predictions should feed a pre-sorted competitor list here.

10. **`EventResult.handicap_factor` (Float, default 0.0):** Column active. Scoring engine applies this in `_metric()` when `event.is_handicap` is True — subtracts start mark from raw time before position calculation. Default `0.0` = scratch (no mark).

11. **Mark assignment pipeline:** When `event.is_handicap` is True, STRATHMARK's `HandicapCalculator` must be called at heat sheet print time (or pre-event) to compute each competitor's start mark from historical data and store it on `EventResult.handicap_factor` (or a new `start_mark_seconds` field on EventResult or HeatAssignment).

The broader vision: STRATHMARK calculates start marks and predicted times, feeds them into this app's heat generation step to group competitors by predicted ability, and marks are printed on heat sheets so competitors know their start times.

---

## 8. FUTURE DEVELOPMENT NOTES

The following features remain as planned or implied by the codebase and requirements:

**Remaining gaps (from Section 5):**
- Friday Night Feature heat generation and flight integration
- Excel results export direct download route
- Pro event fee configuration UI
- Pro entry form redesign (scope pending; current import handled by `registration_import.py` enhanced pipeline)

**Technical debt:**
- Comprehensive server-side input validation (continue hardening)
- Unit and integration tests (comprehensive — 37 test files covering models, routes, services, scoring, gear sharing, flight builder, heat gen, templates, infrastructure, security, PostgreSQL compat; shared `conftest.py` + synthetic data fixtures; `pytest.ini` with smoke/integration/slow markers)
- Authenticated write endpoints on the public API (currently GET-only)
- Multi-year competitor tracking and performance history

**STRATHMARK integration (see Section 7 for what's done and what remains):**
- `Event.is_handicap` flag now stored and surfaced in UI (V2.1.0) ✓
- `config.HANDICAP_ELIGIBLE_STAND_TYPES` gating constant added (V2.1.0) ✓
- Pro competitor enrollment at registration (V2.2.0) ✓
- Pro SB/UH result push on finalization (V2.2.0) ✓
- College SB Speed / UH Speed result push with name-match resolution (V2.2.0) ✓
- Sync status page `/strathmark/status` (V2.2.0) ✓
- Handicap scoring math in `_metric()` (V2.3.0) ✓
- Mark assignment route `assign_marks` + service `mark_assignment.py` (V2.3.0) ✓
- `EventResult.predicted_time` column + migration `d8d4aa7bdb45` (V2.4.0) ✓
- `_record_prediction_residuals_for_pro_event()` in `strathmark_sync.py` (V2.4.0) ✓
- Import fix in `mark_assignment.py` (`strathmark.calculator` path) (V2.4.0) ✓
- STRATHMARK logo on assign marks page (V2.4.0) ✓
- Batched `HandicapCalculator.calculate()` pipeline with `wood_df` + `results_df` + per-competitor `CompetitorRecord` history; `predicted_time` now populated end-to-end (V2.7.0, PR #6) ✓
- Offline CSV upload/preview + manual/bulk-paste mark entry paths for Railway deployments without Ollama reach (V2.7.0, PR #6) ✓
- `optimize_flight_for_ability()` stub in `flight_builder.py` is the designed hook for predicted times
- `_generate_event_heats()` in `heat_generator.py` needs ability-weighting input

**Generalization vision:** The current app is hardcoded to the Missoula Pro Am's specific event list and format. The event list lives in `config.py` (`COLLEGE_OPEN_EVENTS`, `COLLEGE_CLOSED_EVENTS`, `PRO_EVENTS`) and the UI strings in `strings.py`. The long-term STRATHEX platform vision is to make these event lists configurable per-tournament rather than hardcoded, enabling this application to serve any timbersports event, not just Missoula. The `is_open` flag on Event, the configurable payouts system, and the per-tournament event setup flow are all steps in this direction.

---

## 9. GSTACK — QA & DEVELOPER TOOLING

[Gstack](https://github.com/garrytan/gstack) is installed as a Claude Code skill at `~/.claude/skills/gstack/`. It provides a fast headless browser (Playwright Chromium) and a suite of slash commands for QA testing, design review, code review, deployment, and more.

### Available Gstack Skills

| Slash Command | Purpose |
|---------------|---------|
| `/gstack` | Headless browser — navigate, interact, screenshot, diff |
| `/qa` | Systematic QA testing + iterative bug fixing |
| `/qa-only` | QA report only (no fixes) |
| `/review` | Pre-landing PR code review |
| `/design-review` | Visual QA — spacing, hierarchy, consistency fixes |
| `/design-consultation` | Create a design system / DESIGN.md |
| `/ship` | Ship workflow — tests, review, version bump, PR |
| `/land-and-deploy` | Merge PR, wait for CI, verify production |
| `/investigate` | Systematic root-cause debugging |
| `/autoplan` | Auto-run CEO + design + eng reviews sequentially |
| `/plan-ceo-review` | CEO/founder scope review |
| `/plan-eng-review` | Engineering architecture review |
| `/plan-design-review` | Designer's eye plan review |
| `/office-hours` | Brainstorming / idea validation |
| `/careful` | Safety guardrails for destructive commands |
| `/freeze` / `/unfreeze` | Restrict/unrestrict editable directories |
| `/guard` | Combined careful + freeze |
| `/retro` | Weekly engineering retrospective |
| `/document-release` | Post-ship documentation sync |
| `/codex` | Second opinion via OpenAI Codex |
| `/browse` | Direct headless browser interaction |
| `/canary` | Post-deploy monitoring |
| `/benchmark` | Performance regression detection |
| `/cso` | Security audit (OWASP, STRIDE, supply chain) |
| `/setup-deploy` | Configure deployment platform |
| `/setup-browser-cookies` | Import real browser cookies for auth testing |
| `/gstack-upgrade` | Update gstack to latest version |

### Prerequisites

- **bun** (installed): `~/.bun/bin/bun` — JavaScript runtime for skill scripts
- **Node.js** (required for browser features on Windows): Install from https://nodejs.org/ — Playwright Chromium needs Node.js on Windows due to a bun pipe bug
- After installing Node.js, re-run: `cd ~/.claude/skills/gstack && ./setup`

## Deploy Configuration (configured by /setup-deploy)
- Platform: Railway
- Production URL: https://missoula-pro-am-manager-production.up.railway.app
- Deploy workflow: auto-deploy on push to main (Railway watches the GitHub repo)
- Deploy status command: HTTP health check at production URL
- Merge method: squash
- Project type: web app (Flask)
- Post-deploy health check: GET / (root URL, expect 200)
- Release command: `flask db upgrade` (configured in railway.toml)

### Custom deploy hooks
- Pre-merge: `pytest` (run tests before merging)
- Deploy trigger: automatic on push to main (Railway auto-deploy)
- Deploy status: poll production URL root for HTTP 200
- Health check: GET https://missoula-pro-am-manager-production.up.railway.app/
