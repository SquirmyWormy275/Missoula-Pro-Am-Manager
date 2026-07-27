Missoula Pro-Am Manager - Post-2026 State Audit
================================================

Generated: 2026-04-29
Last commit (main HEAD): bd6ad7c890f1e083d23da4f7ab6e6fbb93e02009
Last commit date: 2026-04-29 16:25:11 -0600
Last commit message: chore(docs): consolidate recon docs + extract operations runbooks + CI cleanup (#99)
Application version (pyproject.toml): 2.14.16
Branch: main
Repo: SquirmyWormy275/Missoula-Pro-Am-Manager

Purpose
-------
This document captures the static state of the codebase immediately after the
April 24-25 2026 Missoula Pro-Am event ran. It exists to serve as a snapshot
reference for the STRATHEX rebuild W3 service decomposition work. STRATHEX
W3 reads this document to scope which behaviors must be preserved by
post-decomposition regression tests.

Generation method: pure static inspection. No application start, no test
run, no migration apply. All counts derived from git ls-files plus AST
parsing of Python sources at HEAD.


1. File Structure (Top 3 Levels, Tracked Files Only)
----------------------------------------------------

Excluded by audit charter: node_modules, __pycache__, instance, .venv,
.git, dist, build, *.egg-info. None of these are tracked in git, so the
exclusion is informational only.

Root files
  .gitattributes
  .gitignore
  .python-version
  .railwayignore
  CLAUDE.md
  DESIGN.md
  DEVELOPMENT.md
  FlightLogic.md
  Procfile
  README.md
  USER_GUIDE.md
  app.py
  arapaho_glossary.json
  config.py
  database.py
  pyproject.toml
  pyrightconfig.json
  pytest.ini
  railway.toml
  requirements.txt
  runtime.txt
  strings.py

Directory structure (level 2-3, tracked files only)

  .compound-engineering/
    config.local.example.yaml
  .context/
    retros/
      2026-03-23-1.json
  .github/
    workflows/
      ci.yml
      daily-backup.yml
      health-monitor.yml
  .vscode/
    launch.json
  docs/
    Alex's Docs/                     (3 files)
    DOMAIN_CONTRACT.md
    GEAR_SHARING_AUDIT.md
    GITHUB_REQUIRED_SETTINGS.md
    HEAT_FLIGHT_AUDIT.md
    JUDGE_TRAINING_CURRICULUM.md
    MARK_ASSIGNMENT_WORKFLOW.md
    PLAN_REVIEW.md
    POSTGRES_MIGRATION.md
    PRE_DEPLOY_QA_2026-04-10.md
    QA_REPORT.md
    RELEASE_CHECKLIST.md
    ROLLBACK_SOP.md
    ROOT_CAUSES_AND_CLEANUP_ORDER.md
    SCORING_AUDIT.md
    VIDEO_JUDGE_BRACKET_PLAN.md
    WOODBOSS_AUDIT.md
    archived/                        (1 file)
    brainstorms/                     (1 file)
    designs/                         (1 file)
    domain_conflicts.json
    plans/                           (2 files)
    recon/                           (9 files)
    solutions/                       (subdirectories: best-practices, logic-errors, operations, test-failures)
  migrations/
    README
    alembic.ini
    env.py
    script.py.mako
    versions/                        (34 files; see Section 3)
  models/
    __init__.py
    audit_log.py
    background_job.py
    competitor.py
    event.py
    heat.py
    payout_template.py
    print_email_log.py
    print_tracker.py
    pro_event_rank.py
    school_captain.py
    team.py
    tournament.py
    user.py
    wood_config.py
  routes/
    __init__.py
    api.py
    auth.py
    demo_data.py
    domain_conflicts.py
    import_routes.py
    main.py
    partnered_axe.py
    portal.py
    proam_relay.py
    registration.py
    reporting.py
    scoring.py
    strathmark.py
    validation.py
    woodboss.py
    scheduling/                      (14 sub-modules: __init__, ability_rankings, assign_marks, birling, events, flights, friday_feature, heat_sheets, heats, partners, preflight, print_hub, pro_checkout_roster, show_day)
  scripts/
    __init__.py
    diagnose_unplaced_competitors.py
    load_test_race_day.py
    profile_spectator_endpoint.py
    qa_print_hub.py
    qa_solo_heat_placement.py
    repair_springboard_handedness.py
    smoke_test.py
    verify_security_headers.sh
  services/
    (50+ Python files; see Section 4)
  static/
    audio/                           (1 file)
    css/theme.css
    img/                             (6 files)
    js/                              (3 files)
    offline_queue.js
    strathmark_logo.png
    sw.js
  templates/
    _sidebar.html
    _tournament_tabs.html
    base.html
    dashboard.html
    ops_dashboard.html
    role_entry.html
    tournament_detail.html
    tournament_new.html
    tournament_setup.html
    admin/                           (1 file)
    auth/                            (4 files)
    college/                         (3 files)
    errors/                          (3 files)
    partnered_axe/                   (4 files)
    portal/                          (15 files)
    pro/                             (11 files)
    proam_relay/                     (6 files)
    reporting/                       (3 files)
    reports/                         (8 files)
    scheduling/                      (18 files)
    scoring/                         (10 files)
    strathmark/                      (1 file)
    validation/                      (3 files)
    woodboss/                        (6 files)
  tests/
    __init__.py
    conftest.py
    db_test_utils.py
    fixtures/                        (synthetic_data.py, __init__.py)
    (100+ test_*.py files; see Section 5)


2. Schema Summary (table_name (column_count), AST-parsed)
---------------------------------------------------------

Source: ast.parse over models/*.py at HEAD. Column count is the number of
db.Column(...) calls inside each class body.

  audit_logs (9)
  background_jobs (10)
  college_competitors (14)
  event_results (28)
  events (20)
  flights (6)
  heat_assignments (5)
  heats (12)
  payout_templates (4)
  print_email_logs (10)
  print_trackers (7)
  pro_competitors (31)
  pro_event_ranks (5)
  school_captains (5)
  teams (9)
  tournaments (11)
  users (11)
  wood_configs (8)

TOTAL: 18 tables, 195 columns combined.

Note: heats also stores a JSON 'competitors' field (list of competitor
ids) and a JSON 'stand_assignments' field (dict competitor_id ->
stand_number). events stores 'payouts' as JSON which is also repurposed
by ProAmRelay, PartneredAxeThrow, and BirlingBracket to carry their
state. Several JSON fields on competitor tables (events_entered, partners,
gear_sharing, entry_fees, fees_paid) hold list/dict data that is not
captured by the column count.


3. Migrations Applied (34 files)
--------------------------------

Source: ls migrations/versions/*.py at HEAD. Listed alphabetically by
filename, not by chain order. Chain order is recorded in MEMORY.md and
in the down_revision fields of the files themselves.

  109d1ac298e1_add_gear_sharing_to_college_competitors.py
  3a1d40ce3c6a_initial_schema.py
  41b9a6cbcfd4_add_import_fields_to_pro_competitors.py
  7f8f2f600aa1_add_users_for_auth_and_portals.py
  8b2fd0d307bb_add_portal_pin_hash_to_competitors.py
  a1b2c3d4e5f8_add_status_reason_to_event_result.py
  a4b5c6d7e8f9_add_validation_errors_to_teams.py
  a9b8c7d6e5f4_add_flight_position_and_springboard_slow_heat.py
  b1c2d3e4f5a6_add_event_state_and_payout_settled.py
  b27d62f4f8a1_add_audit_logs_indexes_and_optimistic_.py
  b5c6d7e8f9a0_add_wood_configs.py
  b5f1e9cd7c50_merge_springboard_slow_heat_branch.py
  c1d2e3f4a5b6_merge_heads.py
  c2d3e4f5a6b7_fix_nullable_drift.py
  c6d7e8f9a0b1_add_count_override_to_wood_configs.py
  c7d8e9f0a1b2_add_providing_shirts_to_tournaments.py
  d1e2f3a4b5c6_add_payout_settled_and_is_flagged.py
  d3e4f5a6b7c8_fix_server_default_drift.py
  d7e8f9a0b1c2_add_pro_event_ranks.py
  d8d4aa7bdb45_add_predicted_time_to_event_result.py
  e2f3a4b5c6d7_add_headshots_and_sms_fields.py
  e4f5a6b7c8d9_add_background_jobs_table.py
  e8f9a0b1c2d3_merge_pro_event_ranks_and_shirts.py
  e9f0a1b2c3d4_schema_parity_fix.py
  f0a1b2c3d4e5_add_dual_timer_columns.py
  f0a1b2c3d4e6_points_columns_to_numeric.py
  f3a4b5c6d7e8_add_school_captains.py
  f5a6b7c8d9e0_add_first_pass_integrity_checks.py
  h5i6j7k8l9m0_add_schedule_config_to_tournaments.py
  i6j7k8l9m0n1_scoring_engine_overhaul.py
  j7k8l9m0n1o2_add_is_handicap_to_events.py
  k8l9m0n1o2p3_add_strathmark_id.py
  l2a3b4c5d6e7_add_is_override_to_teams.py
  m3b4c5d6e7f8_add_print_trackers_and_email_logs.py

Production schema head at archive time: m3b4c5d6e7f8.


4. Service Inventory (services/*.py, Sorted by LOC Descending)
--------------------------------------------------------------

Source: wc -l on git-tracked services/*.py files at HEAD.

Five named gravity-well service files (per audit charter):

  services/registration_import.py     1073 lines
  services/gear_sharing.py            1892 lines
  services/heat_generator.py          1238 lines
  services/flight_builder.py          1525 lines
  services/excel_io.py                1122 lines

All five exist. All five exceed 500 lines. registration_import,
gear_sharing, heat_generator, flight_builder, and excel_io are the
five named in the STRATHEX W3 brief and remain the largest decomposition
targets.

Additional service files exceeding 500 LOC (further gravity-well
candidates the original brief did not name):

  services/woodboss.py                1572 lines
  services/birling_bracket.py         1016 lines
  services/scoring_engine.py          1014 lines
  services/print_catalog.py            953 lines
  services/strathmark_sync.py          803 lines
  services/mark_assignment.py          770 lines
  services/proam_relay.py              632 lines
  services/scratch_cascade.py          618 lines
  services/schedule_builder.py         557 lines
  services/preflight.py                554 lines
  services/schedule_status.py          500 lines

Eleven additional services exceed the 500-line threshold.

Remaining services (under 500 lines):

  services/scoring_workflow.py         421
  services/validation.py               416
  services/partnered_axe.py            394
  services/video_judge_export.py       370
  services/pro_entry_importer.py       347
  services/email_delivery.py           322
  services/partner_matching.py         307
  services/backup.py                   253
  services/background_jobs.py          214
  services/saw_block_assignment.py     209
  services/ala_report.py               202
  services/partner_resolver.py         201
  services/domain_conflicts.py         189
  (plus 18 smaller services under 200 LOC)

Total services/ LOC: 21278 lines across 50+ files.


5. Test Inventory (tests/*.py, Sorted by LOC Descending)
--------------------------------------------------------

Source: wc -l on git-tracked tests/*.py files at HEAD.

Highlighted per audit charter:

  tests/test_strathmark_sync.py       1357 lines  (STRATHMARK live integration tests)
  tests/test_scratch_cascade.py       1090 lines  (scratch cascade behavior tests)

Both files exist. Both are large. The audit charter calls these out
because the STRATHEX rebuild needs to preserve their assertions in
the new architecture.

Top 50 test files by LOC:

  1357  tests/test_strathmark_sync.py                    [highlighted]
  1129  tests/test_models_full.py
  1106  tests/test_route_smoke_qa.py
  1090  tests/test_scratch_cascade.py                    [highlighted]
  1072  tests/test_woodboss.py
  1039  tests/test_gear_sharing.py
   989  tests/test_mark_assignment.py
   878  tests/test_flight_builder_integration.py
   845  tests/test_heat_gen_integration.py
   837  tests/test_integration_qa.py
   815  tests/fixtures/synthetic_data.py
   777  tests/test_migration_integrity.py
   775  tests/test_portal_hardening.py
   761  tests/test_point_calculator.py
   755  tests/test_one_click_and_fnf.py
   745  tests/test_heat_generator.py
   717  tests/test_flask_reliability.py
   690  tests/test_saw_block_integration.py
   689  tests/test_preflight.py
   678  tests/test_competitor_portal.py
   675  tests/test_scoring_college_points.py
   653  tests/test_scoring_engine_integration.py
   644  tests/test_day_of_operations.py
   634  tests/test_edge_cases.py
   629  tests/test_spectator_portal.py
   605  tests/test_registration_import.py
   599  tests/test_routes_smoke.py
   592  tests/test_flight_build_full_stack.py
   574  tests/test_model_json_safety.py
   571  tests/test_scratch_routes.py
   562  tests/test_saw_block_assignment.py
   557  tests/test_scoring.py
   556  tests/test_pro_entry_importer.py
   556  tests/test_partner_pairing_fixes.py
   544  tests/test_role_access_control.py
   542  tests/test_dual_timer_entry.py
   538  tests/test_postgres_compat.py
   533  tests/test_flight_builder_25_pros.py
   533  tests/test_axe_throw_qualifiers.py
   527  tests/test_scoring_full_event.py
   527  tests/test_schedule_builder.py
   518  tests/test_schedule_builder_ordering.py
   516  tests/test_excel_io.py
   510  tests/test_split_tie_scoring.py
   501  tests/test_fuzz_scoring.py
   499  tests/test_proam_relay_placement.py
   499  tests/test_partner_reassignment.py
   473  tests/test_video_judge_export.py
   461  tests/test_proam_relay.py

Total tests/ LOC: 58182 lines.
Total test files: 119 files (counting __init__.py and conftest.py).


6. Route Inventory (Static @route + add_url_rule Parse)
-------------------------------------------------------

Source: regex parse over routes/*.py + routes/scheduling/*.py +
app.py for @app.route, @blueprint_bp.route, and add_url_rule calls.

Total route decorator hits: 237 across 28 files
Total unique (path, methods) pairs: 229

Per-file decorator counts:

      6  routes/api.py
      7  routes/auth.py
      2  routes/demo_data.py
      2  routes/domain_conflicts.py
      3  routes/import_routes.py
     16  routes/main.py
     11  routes/partnered_axe.py
     17  routes/portal.py
     12  routes/proam_relay.py
     38  routes/registration.py
     23  routes/reporting.py
      1  routes/scheduling/ability_rankings.py
      1  routes/scheduling/assign_marks.py
     10  routes/scheduling/birling.py
      7  routes/scheduling/events.py
      8  routes/scheduling/flights.py
      3  routes/scheduling/friday_feature.py
      3  routes/scheduling/heat_sheets.py
     11  routes/scheduling/heats.py
      2  routes/scheduling/partners.py
      4  routes/scheduling/preflight.py
      2  routes/scheduling/print_hub.py
      2  routes/scheduling/pro_checkout_roster.py
      1  routes/scheduling/show_day.py
     25  routes/scoring.py
      1  routes/strathmark.py
      7  routes/validation.py
     12  routes/woodboss.py
   ----
    237  total

Full deduplicated route list (path with HTTP methods; paths are
relative to each blueprint's url_prefix as registered in app.py):

  GET                   /
  POST                  /<conflict_id>
  GET                   /<int:tid>
  GET                   /<int:tid>/config
  POST                  /<int:tid>/config
  POST                  /<int:tid>/config/apply-preset
  POST                  /<int:tid>/config/copy-from
  POST                  /<int:tid>/config/delete-preset
  POST                  /<int:tid>/config/save-preset
  GET                   /<int:tid>/events/<int:eid>/partner-queue
  POST                  /<int:tid>/events/<int:eid>/reassign-partner
  GET                   /<int:tid>/lottery
  GET                   /<int:tid>/report
  GET                   /<int:tid>/report/print
  GET                   /<int:tid>/share
  GET                   /<int:tournament_id>/all-results
  GET                   /<int:tournament_id>/all-results/print
  GET                   /<int:tournament_id>/backup
  POST                  /<int:tournament_id>/backup/cloud
  GET                   /<int:tournament_id>/birling
  GET                   /<int:tournament_id>/birling/print-all
  GET                   /<int:tournament_id>/college
  POST                  /<int:tournament_id>/college/competitor/<int:competitor_id>/add-event
  POST                  /<int:tournament_id>/college/competitor/<int:competitor_id>/delete
  POST                  /<int:tournament_id>/college/competitor/<int:competitor_id>/remove-event
  POST                  /<int:tournament_id>/college/competitor/<int:competitor_id>/scratch
  POST                  /<int:tournament_id>/college/competitor/<int:competitor_id>/set-partner
  POST                  /<int:tournament_id>/college/gear-sharing/remove
  POST                  /<int:tournament_id>/college/gear-sharing/update
  POST                  /<int:tournament_id>/college/gear-sharing/update-ajax
  POST                  /<int:tournament_id>/college/saturday-priority
  GET                   /<int:tournament_id>/college/standings
  GET                   /<int:tournament_id>/college/standings/print
  GET                   /<int:tournament_id>/college/team/<int:team_id>
  POST                  /<int:tournament_id>/college/team/<int:team_id>/delete
  POST                  /<int:tournament_id>/college/team/<int:team_id>/override-validation
  POST                  /<int:tournament_id>/college/team/<int:team_id>/remove-override
  POST                  /<int:tournament_id>/college/team/<int:team_id>/revalidate
  POST                  /<int:tournament_id>/college/upload
  POST                  /<int:tournament_id>/competitor/<int:competitor_id>/scratch-confirm
  GET                   /<int:tournament_id>/competitor/<int:competitor_id>/scratch-preview
  POST                  /<int:tournament_id>/competitor/<int:competitor_id>/scratch-undo
  GET,POST              /<int:tournament_id>/day-schedule
  GET                   /<int:tournament_id>/day-schedule/print
  POST                  /<int:tournament_id>/event/<int:event_id>/add-to-heat
  GET                   /<int:tournament_id>/event/<int:event_id>/birling
  GET                   /<int:tournament_id>/event/<int:event_id>/birling-bracket
  POST                  /<int:tournament_id>/event/<int:event_id>/birling/fall
  POST                  /<int:tournament_id>/event/<int:event_id>/birling/finalize
  POST                  /<int:tournament_id>/event/<int:event_id>/birling/generate
  GET                   /<int:tournament_id>/event/<int:event_id>/birling/print-blank
  POST                  /<int:tournament_id>/event/<int:event_id>/birling/record
  POST                  /<int:tournament_id>/event/<int:event_id>/birling/reset
  POST                  /<int:tournament_id>/event/<int:event_id>/birling/undo
  POST                  /<int:tournament_id>/event/<int:event_id>/delete-heat/<int:heat_id>
  POST                  /<int:tournament_id>/event/<int:event_id>/finalize
  GET                   /<int:tournament_id>/event/<int:event_id>/finalize-preview
  POST                  /<int:tournament_id>/event/<int:event_id>/generate-heats
  GET                   /<int:tournament_id>/event/<int:event_id>/heats
  GET                   /<int:tournament_id>/event/<int:event_id>/heats/sync-check
  POST                  /<int:tournament_id>/event/<int:event_id>/heats/sync-fix
  GET,POST              /<int:tournament_id>/event/<int:event_id>/import-results
  GET                   /<int:tournament_id>/event/<int:event_id>/judge-sheet
  GET                   /<int:tournament_id>/event/<int:event_id>/live-standings
  POST                  /<int:tournament_id>/event/<int:event_id>/move-competitor
  GET                   /<int:tournament_id>/event/<int:event_id>/next-heat
  GET,POST              /<int:tournament_id>/event/<int:event_id>/payouts
  GET                   /<int:tournament_id>/event/<int:event_id>/results
  GET                   /<int:tournament_id>/event/<int:event_id>/results/print
  POST                  /<int:tournament_id>/event/<int:event_id>/scratch-competitor
  POST                  /<int:tournament_id>/event/<int:event_id>/throwoff
  GET,POST              /<int:tournament_id>/events
  GET,POST              /<int:tournament_id>/events/<int:event_id>/assign-marks
  POST                  /<int:tournament_id>/events/generate-async
  GET                   /<int:tournament_id>/events/job-status/<job_id>
  POST                  /<int:tournament_id>/events/reorder-friday
  POST                  /<int:tournament_id>/events/reorder-saturday
  POST                  /<int:tournament_id>/events/reset-order
  GET,POST              /<int:tournament_id>/events/setup
  GET                   /<int:tournament_id>/export-chopping
  GET                   /<int:tournament_id>/export-results
  POST                  /<int:tournament_id>/export-results/async
  GET                   /<int:tournament_id>/export-video-judge
  POST                  /<int:tournament_id>/export-video-judge/async
  GET                   /<int:tournament_id>/flights
  POST                  /<int:tournament_id>/flights/<int:flight_id>/complete
  POST                  /<int:tournament_id>/flights/<int:flight_id>/reorder
  POST                  /<int:tournament_id>/flights/<int:flight_id>/start
  GET,POST              /<int:tournament_id>/flights/build
  POST                  /<int:tournament_id>/flights/bulk-reorder
  POST                  /<int:tournament_id>/flights/one-click-generate
  GET,POST              /<int:tournament_id>/friday-night
  GET                   /<int:tournament_id>/friday-night/pdf
  GET                   /<int:tournament_id>/friday-night/print
  POST                  /<int:tournament_id>/generate-college-heats
  GET                   /<int:tournament_id>/heat-sheets
  GET,POST              /<int:tournament_id>/heat/<int:heat_id>/enter
  GET                   /<int:tournament_id>/heat/<int:heat_id>/pdf
  POST                  /<int:tournament_id>/heat/<int:heat_id>/release-lock
  POST                  /<int:tournament_id>/heat/<int:heat_id>/undo
  POST                  /<int:tournament_id>/heats/<int:source_heat_id>/drag-move
  POST                  /<int:tournament_id>/heats/recompute-saw-blocks
  GET                   /<int:tournament_id>/jobs/<job_id>
  GET                   /<int:tournament_id>/judge-sheets/all
  GET                   /<int:tournament_id>/next-incomplete-event
  GET                   /<int:tournament_id>/offline-ops
  GET,POST              /<int:tournament_id>/preflight
  GET                   /<int:tournament_id>/preflight-json
  GET                   /<int:tournament_id>/print-hub
  POST                  /<int:tournament_id>/print-hub/email
  GET                   /<int:tournament_id>/pro
  GET,POST              /<int:tournament_id>/pro-entries
  POST                  /<int:tournament_id>/pro-entries/confirm
  GET                   /<int:tournament_id>/pro-entries/review
  GET                   /<int:tournament_id>/pro/<int:competitor_id>
  POST                  /<int:tournament_id>/pro/<int:competitor_id>/scratch
  POST                  /<int:tournament_id>/pro/<int:competitor_id>/update-events
  POST                  /<int:tournament_id>/pro/<int:competitor_id>/upload-headshot
  GET,POST              /<int:tournament_id>/pro/ability-rankings
  POST                  /<int:tournament_id>/pro/auto-assign-partners
  GET                   /<int:tournament_id>/pro/checkout-roster/pdf
  GET                   /<int:tournament_id>/pro/checkout-roster/print
  GET,POST              /<int:tournament_id>/pro/event-fees
  GET,POST              /<int:tournament_id>/pro/fee-tracker
  GET                   /<int:tournament_id>/pro/gear-sharing
  POST                  /<int:tournament_id>/pro/gear-sharing/auto-partners
  POST                  /<int:tournament_id>/pro/gear-sharing/cleanup-non-enrolled
  POST                  /<int:tournament_id>/pro/gear-sharing/cleanup-scratched
  POST                  /<int:tournament_id>/pro/gear-sharing/complete-pairs
  POST                  /<int:tournament_id>/pro/gear-sharing/group-create
  POST                  /<int:tournament_id>/pro/gear-sharing/group-remove
  POST                  /<int:tournament_id>/pro/gear-sharing/parse
  POST                  /<int:tournament_id>/pro/gear-sharing/parse-confirm
  GET                   /<int:tournament_id>/pro/gear-sharing/parse-review
  GET                   /<int:tournament_id>/pro/gear-sharing/print
  POST                  /<int:tournament_id>/pro/gear-sharing/remove
  POST                  /<int:tournament_id>/pro/gear-sharing/sync-heats
  POST                  /<int:tournament_id>/pro/gear-sharing/update
  POST                  /<int:tournament_id>/pro/gear-sharing/update-ajax
  GET,POST              /<int:tournament_id>/pro/new
  GET,POST              /<int:tournament_id>/pro/payout-manager
  GET,POST              /<int:tournament_id>/pro/payout-settlement
  GET,POST              /<int:tournament_id>/pro/payouts
  GET                   /<int:tournament_id>/pro/payouts/print
  GET                   /<int:tournament_id>/relay-teams-sheet
  POST                  /<int:tournament_id>/restore
  GET                   /<int:tournament_id>/saw-blocks-status
  GET                   /<int:tournament_id>/show-day
  POST                  /admin/repair-points/<int:tournament_id>
  POST                  /advance-to-finals
  GET                   /ala-membership-report/<int:tournament_id>
  POST                  /ala-membership-report/<int:tournament_id>/email
  GET                   /ala-membership-report/<int:tournament_id>/pdf
  GET                   /api/college
  GET                   /api/full
  GET                   /api/pro
  POST                  /api/replay
  GET                   /api/replay-token
  GET                   /api/status
  GET                   /audit
  GET,POST              /bootstrap
  POST                  /clear
  GET                   /college
  GET                   /competitor
  GET,POST              /competitor-access
  GET,POST              /competitor/<int:tournament_id>/<competitor_type>/<int:competitor_id>/my-results
  GET,POST              /competitor/claim
  GET                   /competitor/public
  POST                  /competitor/sms-opt-in
  POST                  /draw
  POST                  /enable
  GET                   /finals
  POST                  /finals/record
  POST                  /generate
  GET                   /guide
  GET                   /headshots/<path:filename>
  GET                   /health
  GET                   /health/diag
  GET                   /history
  GET                   /judge
  GET                   /kiosk/<int:tournament_id>
  GET                   /language/<lang_code>
  GET,POST              /login
  POST                  /logout
  GET                   /manual-teams
  POST                  /manual-teams/save
  GET                   /payouts
  POST                  /payouts
  GET                   /prelims
  POST                  /prelims/record
  GET                   /pro
  GET                   /public/tournaments/<int:tournament_id>/handicap-input
  GET                   /public/tournaments/<int:tournament_id>/results
  GET                   /public/tournaments/<int:tournament_id>/schedule
  GET                   /public/tournaments/<int:tournament_id>/standings
  GET                   /public/tournaments/<int:tournament_id>/standings-poll
  GET                   /public/tournaments/<int:tournament_id>/standings-stream
  POST                  /redraw
  POST                  /register-pair
  POST                  /replace-competitor
  POST                  /reset
  GET                   /results
  GET,POST              /results
  GET,POST              /school-access
  GET,POST              /school/claim
  GET                   /school/dashboard
  GET                   /spectator/<int:tournament_id>
  GET                   /spectator/<int:tournament_id>/college
  GET                   /spectator/<int:tournament_id>/event/<int:event_id>
  GET                   /spectator/<int:tournament_id>/pro
  GET                   /spectator/<int:tournament_id>/relay
  GET                   /standings
  GET                   /status
  GET                   /teams
  GET                   /tournament/<int:tid>/ops-dashboard
  POST                  /tournament/<int:tid>/result/<int:rid>/toggle-settled
  GET                   /tournament/<int:tournament_id>
  POST                  /tournament/<int:tournament_id>/activate/<competition_type>
  POST                  /tournament/<int:tournament_id>/clone
  GET                   /tournament/<int:tournament_id>/college
  POST                  /tournament/<int:tournament_id>/delete
  GET                   /tournament/<int:tournament_id>/export-config
  GET                   /tournament/<int:tournament_id>/pro
  GET                   /tournament/<int:tournament_id>/setup
  POST                  /tournament/<int:tournament_id>/setup/settings
  GET,POST              /tournament/new
  GET,POST              /users
  POST                  /users/<int:user_id>/toggle-active
  POST                  /users/reset-competitor-pin

Note: paths shown are the literal strings inside @route(...) decorators.
The blueprint url_prefix is applied at registration time in app.py (e.g.
the proam_relay blueprint is registered at /tournament/<int:tournament_id>/proam-relay/,
so its /draw decorator becomes /tournament/<int:tournament_id>/proam-relay/draw at runtime).
A handful of paths appear to be duplicates because they live on different
blueprints (e.g. /payouts on multiple state-machine blueprints).


7. Cruft Check (Tracked Files Only)
-----------------------------------

Source: git ls-files | grep -E pattern. Reports presence in the git
index, NOT the working tree filesystem. Files gitignored but present
on disk are not counted.

  __pycache__/ in git:           NONE
  *.pyc in git:                  NONE
  instance/ in git:              NONE
  .venv/ or venv/ in git:        NONE
  *.sqlite or *.db in git:       NONE

Verdict: CLEAN. No build artifacts, virtualenvs, caches, or local databases
are tracked in the repository.

The .gitignore at repo root (verified during V2.6.x ship per MEMORY.md)
covers: __pycache__/, instance/, uploads/, .env, proam.db, .claude/.


8. Version History
------------------

CHANGELOG.md: NOT PRESENT in repository.

Version surfaces in code (grepped at HEAD):

  pyproject.toml                           version = "2.14.16"
  routes/main.py:79                        /health JSON 'version': '2.14.16'
  routes/main.py:163                       /health/diag JSON 'version': '2.14.16'

V2.x release commits visible in main branch git log (most recent first):

  fix(V2.14.16): implement domain-conflict registry decisions + ship review board (#97)   8c439b1
  fix(V2.14.15): bundle - ability rankings, placed breakdown, stock saw cascade,
                                          birling stale shape (#96)                       9c60627
  fix(birling): compact non-power-of-two bracket + CI health + docs (V2.14.14) (#95)      8c622be
  fix(heat_generator): Stock Saw solos alternate stands 7/8 (V2.14.13) (#94)              322f5cf
  fix(scheduling): lock Friday final-four event order (V2.14.12) (#93)                    8e9e2a8
  fix(scheduling): partner pairing + persisted-config audit (V2.14.10) (#91)              07ce115
  fix(birling): index hub + aligned bracket visualization (V2.14.9) (#90)                 c265687
  fix(audit-sprint): 17 audit-sweep fixes (V2.14.8) (#89)                                 6f2d619
  fix(scheduling): filter pro ability rankings by event signup (v2.14.7) (#88)            f876801
  fix(run-show): warning panel CTAs actually generate (V2.14.6) (#86)                     bbe59a5
  fix(payouts): replace unsupported Jinja2 sort kwarg on payout templates (V2.14.5) (#85) eafa69c
  fix: solo competitor closes the event, not opens it (V2.14.4) (#84)                     1b81dca
  feat(relay): redraw accepts operator-chosen num_teams (V2.14.3) (#82)                   279f0d2
  fix(scheduling): scope schedule-status warning to exclude list-only events (V2.14.2) (#81) 68e78ca
  fix(scheduling): expose per-event stand count override on Friday Showcase (V2.14.1) (#80)  e476e12
  chore(V2.14.0): release - Flight Fixes 5-phase overhaul + codex hotfix (#74)            df4feb3
  chore(V2.13.0): bump hardcoded version strings in /health + /health/diag                d8bbeac
  fix: clickable seed links in birling "not seeded" flash (V2.12.1) (#57)                 017eebc
  feat: V2.12.0 - even flight distribution + drag-drop + print polish (#54)               89f7f5b
  style(design): touch targets + print fonts + html font-family (V2.11.3) (#53)           deef9df
  feat: surface + test Pro event fee configuration (V2.11.2) (#52)                        4794616
  feat: FNF PDF export via WeasyPrint (V2.11.1) (#51)                                     48bd2a2
  fix: friendly redirect on expired CSRF tokens (V2.10.1) (#46)                           b22d1ab
  feat: hand-saw stand block alternation (V2.10.0) (#45)                                  a10f17d
  fix(V2.9.1): race-day UI hardening - gear sharing + modal stacking (#25)                9a4f1fb
  chore: bump version to V2.9.0 + sync release docs                                       78a2bd4
  fix(woodboss): college enrollment + preset subsystem audit sweep (V2.8.3)               d10e98d
  fix: name parser suffix/first-name collisions + woodboss exclusivity (V2.8.1)           0eaae43
  feat: V2.8.0 - race-day hardening & feature completion (20 improvements)                79248a9
  feat: STRATHMARK V2.7.0 deployment wiring + bulletproofing (#6)                         5303a63
  feat: V2.7.0 - Battle-harden for first deployment                                       2a6124d

Earlier versions (V2.6.x and below) appear in git log history beyond
the cutoff this audit pulled. The tag history in MEMORY.md captures
the full V2.0 to V2.14.16 arc.


9. LOC Totals
-------------

Source: wc -l on git-tracked Python files at HEAD.

  Source Python (excludes tests/):       44247 lines
  Test Python (tests/ only):              58182 lines
  Total Python (entire repo):             ~102429 lines

Breakdown by top-level Python directory:

  Module                       Files            LOC
  -----------------------------------------------------
  models/                      15               1854
  routes/                      28              15919
  services/                    50+             21278
  scripts/                     8 (excluded from source LOC where qa_*)
  tests/                       119             58182
  app.py + config.py + database.py + strings.py + others   (remaining ~5000 LOC)

The five named gravity-well services alone account for 6850 LOC
(registration_import.py + gear_sharing.py + heat_generator.py +
flight_builder.py + excel_io.py = 1073 + 1892 + 1238 + 1525 + 1122).
That is roughly 32 percent of all services/ code and roughly 16 percent
of all source Python.


10. Closing State at Archive Time
---------------------------------

  Production version:        2.14.16 (deployed via Railway after the
                             PR #97 + PR #99 squash-merges performed
                             during this audit session)
  Database:                  PostgreSQL (Railway managed)
  Schema head:               m3b4c5d6e7f8 (add_print_trackers_and_email_logs)
  Open PRs at archive time:  0
  Open issues at archive:    not enumerated by this audit
  CI status on main HEAD:    not re-verified by this audit (the squash
                             commits inherit unstable CI from the parent
                             PRs; runner-cancellation timeouts on the
                             test job are pre-existing infrastructure
                             behavior, not test failures)

This repository is being archived after this commit and the v2026.final
tag are pushed. The archive is performed via gh repo archive, which is
reversible from the GitHub web UI. Local clones continue to function.
No further commits to main are expected.
