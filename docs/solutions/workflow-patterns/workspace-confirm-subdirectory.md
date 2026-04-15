---
type: knowledge
problem_type: workflow-pattern
severity: medium
tags:
  - "workspace"
  - "multi-project"
  - "strathex"
confidence: high
created: 2026-04-15
source: "knowledge-seed from CLAUDE.md and git history"
---

# Confirm target project before making changes in the STRATHEX workspace

## Context
The parent workspace contains multiple related projects: `Missoula-Pro-Am-Manager/`, `STRATHEX/`, `STRATHMARK/`, `KYTHEREX/`. The user often references features or names that could belong to any of them. Making changes in the wrong subdirectory wastes cycles and can corrupt unrelated repos.

## Pattern
Before editing:
1. Confirm which project the user means — ask if ambiguous.
2. Check for separate GitHub repos if code isn't found locally (STRATHMARK lives in a separate repo; STRATHMARK references from here point to `services/strathmark_sync.py` which calls the installed package, not source in this repo).
3. When the user references a prior decision, feature, or business name (e.g., 'Pyramid Lumber', 'MT/PNW pivot'), search DESIGN.md, README, recent git history BEFORE claiming ignorance.

## Rationale
- STRATHMARK integration is cross-repo. A bug report about "handicap marks" could mean this app's `services/mark_assignment.py`, the STRATHMARK package's `HandicapCalculator`, or the Supabase schema.
- Each project has its own CLAUDE.md, migrations, and conventions.

## Examples
- STRATHMARK handicap math lives in the STRATHMARK repo; this app calls it via `from strathmark.calculator import ...`.
- "Virtual Woodboss" is a feature inside this repo (routes/woodboss.py), not a separate project.
