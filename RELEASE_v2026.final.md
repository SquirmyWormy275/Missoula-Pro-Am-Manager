Release v2026.final - Final Post-2026-Event State
==================================================

Date: 2026-04-29

Summary
-------
This tag captures the state of the Missoula Pro-Am Manager codebase
immediately after the April 24-25 2026 event ran. Application version at
tag time is 2.14.16, deployed on Railway, against PostgreSQL with schema
head m3b4c5d6e7f8. Future STRATHEX rebuild work treats this state as the
snapshot reference for service decomposition regression tests. The
companion POST_2026_STATE.md at repo root contains the full static
inventory the rebuild work will scope against, including the five named
gravity-well services, the 18-table schema, the 34 applied migrations,
the 229-route inventory, and LOC totals (44,247 source / 58,182 test).

Known Issues Carried Forward
----------------------------

  * Eleven additional service files exceed 500 LOC beyond the five named
    gravity-wells (woodboss.py, birling_bracket.py, scoring_engine.py,
    print_catalog.py, strathmark_sync.py, mark_assignment.py,
    proam_relay.py, scratch_cascade.py, schedule_builder.py,
    preflight.py, schedule_status.py). These were not in the original
    decomposition charter and may need to be reconsidered when STRATHEX
    W3 plans the rebuild scope.

  * No CHANGELOG.md exists in the repository. Version history is only
    discoverable through git log commit messages and through MEMORY.md
    (which lives outside the repo at
    ~/.claude/projects/.../memory/MEMORY.md). Anyone reading this archive
    cold will have no in-repo narrative for the V2.0 to V2.14.16 arc.

  * The application version literal is duplicated in three places:
    pyproject.toml plus two hardcoded strings in routes/main.py
    (one in /health, one in /health/diag). The PREPARE FOR COMMIT
    standing order in MEMORY.md exists specifically because earlier
    ships missed the routes/main.py copies. Future rebuild should
    consolidate to a single source of truth.

  * heat_assignments and heats both store competitor membership, with
    heats.competitors (JSON list) being authoritative and heat_assignments
    rows used only by the validation service. CLAUDE.md flags this as a
    known consistency gap; sync_assignments() must be called after every
    Heat.competitors mutation. STRATHEX rebuild should pick one
    representation, not both.

  * Several Event-related state machines (ProAmRelay, PartneredAxeThrow,
    BirlingBracket) repurpose the events.payouts JSON column to carry
    their own state rather than introducing dedicated tables. Static
    schema parsing does not surface this. The rebuild may want to
    promote each state machine to its own table.

  * Cookie Stack and Standing Block share the same five physical stands
    and cannot run simultaneously. CLAUDE.md flags that the heat
    generator and flight builder do not enforce this constraint
    everywhere, only at flight conflict resolution. This is a documented
    gap that survives this archive.

  * Pro Birling: removed from PRO_EVENTS in config.py, but per CLAUDE.md
    Section 5 "verify that no templates, database records, or service
    code contain hardcoded references to a pro birling event that could
    create phantom data" remains a never-fully-audited cleanup item.

  * STRATHMARK integration: live data push and handicap math are wired
    end-to-end (V2.7.0). The remaining stub is heat ability-weighting
    via STRATHMARK predictions. services/flight_builder.py
    optimize_flight_for_ability() is a no-op; services/heat_generator.py
    _generate_event_heats() has no ability input. These are designed
    integration points, not bugs.

  * CI on main is intermittently red due to two infrastructure-level
    issues: a runner-cancellation timeout that fires on the full pytest
    suite around the 29-minute mark, and a known-flaky stochastic
    adjacency test (test_flight_builder_25_pros.py
    ::test_no_competitor_in_consecutive_heats). Neither indicates a
    real test failure. Multiple V2.14.x ships have merged through these
    red signals after operator review.

  * Production schema parity: the postgres-parity domain conflict in
    docs/domain_conflicts.json remains in accepted_contract status (an
    ongoing CI discipline contract rather than a one-shot fix).

Reference
---------
See POST_2026_STATE.md at repo root for the full static state audit
including file structure, schema column counts per table, complete
migration list, full route inventory, service and test LOC tables, and
a complete cruft check.

Archive Notice
--------------
This repository is being archived on GitHub immediately after this
release tag is pushed. No new commits are expected to main after this
point. The archive is read-only on GitHub but local clones continue to
function. The archive can be reversed via the GitHub web UI if a future
need arises (e.g., a critical patch must ship to a deployed instance
that has not yet migrated to STRATHEX).

The Railway deployment may continue to serve the deployed V2.14.16
build until it is decommissioned, even after the source repo is
archived. Archive of the source repository does not stop a running
container.

Tag: v2026.final
Last main commit at tag time: bd6ad7c890f1e083d23da4f7ab6e6fbb93e02009
