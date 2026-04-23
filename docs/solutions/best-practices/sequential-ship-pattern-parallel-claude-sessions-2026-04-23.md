---
module: workflow/multi-session
date: 2026-04-23
problem_type: best_practice
component: development_workflow
severity: high
applies_when:
  - "Two or more Claude Code sessions are active against the same repo worktree simultaneously"
  - "A parallel session has uncommitted changes that include an exclusive resource claim (version bump in pyproject.toml, hardcoded /health literals, CHANGELOG entry, or test docstrings naming the slot)"
  - "Working tree has modifications you did not make, surfaced by git status"
  - "Options on the table are (A) overwrite peer's uncommitted bump to claim a later slot, (B) bundle peer's bump into your own commit, or (C) pause, let peer ship, rebase and bump one patch further"
related_components:
  - tooling
  - testing_framework
  - documentation
tags:
  - claude-code
  - multi-session
  - parallel-sessions
  - sequential-ship
  - version-bump
  - git-workflow
  - worktree-hygiene
  - release-management
---

# Sequential-Ship Pattern for Parallel Claude Code Sessions Competing for an Exclusive Version Slot

## Context

During the 2026-04-22 → 2026-04-23 race-weekend patch arc for Missoula Pro-Am Manager (Flask/PostgreSQL tournament app), three Claude Code sessions were running in parallel against the same repo worktree. Between 03:30 and 05:10 on 2026-04-23, they shipped **V2.14.1**, **V2.14.2**, and **V2.14.3** sequentially without collision — despite each session independently editing the same version-bearing files.

This document codifies the **sequential-ship pattern** that made that outcome possible, so future multi-session weekends don't repeat the mistake that preceded it.

The compounded learning is *not* "avoid parallel sessions" — Claude Code is token-hungry and context-segregated, which makes parallel work attractive for throughput. The learning is how to safely navigate **exclusive-resource collisions** (most commonly a version slot) when sessions cannot see each other's in-progress state directly.

### Ambient constraints the pattern respects

- **Race-weekend predictability > wall-clock speed.** Sequential ships each squash-merge cleanly and prod `/health` advances in lockstep with the audit trail. Parallel version-slot fights produce force-pushes, rewrites, and the V2.13.0-class bug where prod `migration_head` advances but `/health` reports a stale version because someone missed a hardcoded literal. (session history)
- **Memory-rule stack**: `feedback_never_stash_unknown_changes.md` (never touch unknown working-tree state), `feedback_reset_hard_wipes_unstaged.md` (sister rule, written mid-session after a near-miss — see "What didn't work" below), `feedback_one_block_commands.md` (atomic command delivery).
- **No cross-session handoff exists.** The user types prose relays between sessions. Neither session sees the other's conversation or TODOs. (session history)

---

## Guidance

### The A/B/C decision framework

When a relay from the user or your own `git status` surfaces uncommitted version-slot work from a sibling session, you have three options. Only one is correct.

| Option | Mechanic | Verdict |
|---|---|---|
| **A — Steamroll** | Ship your work first. Overwrite peer's uncommitted bump. Your commit takes the slot. | **Forbidden.** Violates `feedback_never_stash_unknown_changes.md`. Destroys peer's work. |
| **B — Bundle** | Fold peer's uncommitted bump into your commit so both version-lines and your code land together. | **Forbidden.** Same rule. You cannot author or assume intent for changes you did not make. Muddles the audit trail. |
| **C — Pause and ship sequentially** | Pause your ship. Let peer ship their slot. Rebase your branch. Bump your version one patch past theirs. | **Correct.** |

### Pre-action safety check (run before ANY ref-moving operation)

This is the guardrail that prevents the antipattern below from being reached.

```bash
git diff --quiet && git diff --cached --quiet && [ -z "$(git ls-files --others --exclude-standard)" ]
```

If that exits non-zero, the tree has unknown state. Stop. Do not `git reset --hard`, do not `git stash`, do not `git checkout .`, do not `git clean`. Investigate first — read the diff, identify the sibling session's footprint, relay to the user if unclear.

### Moving refs without touching the working tree

Prefer `git branch -f <branch> <target>` executed from a *different* branch instead of `git reset --hard`. This updates the ref pointer without discarding any working-tree state:

```bash
git switch <other-branch>
git branch -f <branch-to-move> origin/main
git switch <branch-to-move>
```

`git reset --hard` atomically moves the ref AND discards working-tree changes. When the tree has sibling-session state, the discard is unrecoverable — the wiped content does **not** pass through git's object DB, so `git fsck --lost-found` cannot retrieve it. VSCode Local History (Timeline panel) is the only recovery path, and only if the sibling session saved the file in VSCode. (One partial recovery observed: if the sibling session that authored the edits is still running, it may still have the diff in its own conversation context and can re-apply — but do not rely on this, sibling may have closed.)

### Identifying version-slot claims across this repo (the hidden resources)

A single version bump touches more than `pyproject.toml`. In this repo, every one of the following must be checked:

- **`pyproject.toml`** — canonical version
- **`routes/main.py`** — TWO hardcoded `'version': 'X.Y.Z'` literals backing `/health` and `/health/diag` JSON payloads (missed once during V2.13.0 ship; led to baking `grep -rn "OLD.VERSION"` into PREPARE FOR COMMIT step 5) (session history)
- **`DEVELOPMENT.md`** — changelog header `### YYYY-MM-DD (VX.Y.Z)`
- **`CLAUDE.md`** — "Current version" line
- **`tests/test_*.py`** — docstring and comment markers like `"""VX.Y.Z regression: ..."""` are **public commitments** to a slot, even when `pyproject.toml` itself hasn't been bumped yet. A test-suite comment naming V2.14.2 is a claim on V2.14.2.
- Release recon docs like `docs/FLIGHT_FIXES_RECON.md` — changelog-adjacent content

### Detecting a sibling session's uncommitted slot claim

```bash
git status --short
git log --oneline origin/main..HEAD
grep -rn "V2\.14\." routes/ services/ templates/ tests/ pyproject.toml DEVELOPMENT.md CLAUDE.md
```

If `git status` shows files you didn't modify, or the grep returns a version string you didn't write, a sibling session has work staged in the worktree. Relay to the user with an A/B/C framing. Do not touch those files.

### The sequential-ship protocol (full loop)

When collision is confirmed and Option C is chosen:

1. **Verify the relay** — don't act on the description alone. Run `git status`, `git log origin/main..HEAD`, and grep for the claimed version. The sibling session's claim may be partial or misremembered. (Learned in session `4855294e`: "Let me verify the actual state before picking — the relay's description could be wrong.") (session history)
2. **Leave peer's work alone.** Your feature branch already has your commits — nothing is at risk. Do not stash, commit, or otherwise touch peer's worktree state.
3. **Wait for peer's PR to merge.** Typical Railway deploy cycle is 2–5 minutes; peer's ship path (feature branch → PR → squash merge → auto-deploy) usually completes in under 15 minutes.
4. **Sync local main** (only after peer's squash lands and the tree is clean): `git switch main && git pull --ff-only origin main`.
5. **Rebase your feature branch** onto the new main: `git switch <your-branch> && git rebase main`.
6. **Bump your version to one patch past peer's.** Grep the old version across the repo FIRST (canonical doc files + `routes/main.py` `/health` literals + test docstrings + CHANGELOG). Post-bump, re-grep to confirm zero stale literals remain.
7. **Run the full verification chain** — tests, `flask db current` at HEAD, `git status` clean — then open your PR.
8. **Confirm prod `/health` reports your new version** after the deploy cycle (canonical post-ship check).

---

## Why This Matters

### 1. Unrecoverable data loss via `git reset --hard`

Wiping unstaged working-tree changes through `git reset --hard` is permanent from git's perspective. The object DB never sees the content; `git fsck --lost-found` finds nothing. VSCode Local History may save you if the file was saved in that editor; otherwise the work is gone.

This session burned that lesson directly: `routes/proam_relay.py` and `templates/proam_relay/dashboard.html` were wiped at 03:34:00 UTC on 2026-04-23 during a post-merge local-main sync. Only partial recovery was possible — and only because the sibling session that authored the files (`8fa4f1a6`) was still running with the edits in its conversation context, and re-applied them into V2.14.3. (session history)

### 2. Public commitment markers are promises

A test docstring that says `"""V2.14.2 regression: ..."""` is a claim on the V2.14.2 slot even before `pyproject.toml` is bumped. The docstring is committed-worthy intent, visible to the sibling session as a concrete signal the slot is spoken for. Overwriting it forces the sibling into a rebase-and-rewrite cycle that rewrites their committed test history.

In this session's collision, `tests/test_schedule_status.py:108` and `:137` had `"""V2.14.2 regression: ..."""` docstrings. The sibling session's explicit naming of the slot in test code was the decisive signal that Option C (let them ship V2.14.2, I take V2.14.3) was the right call.

### 3. Waiting is free; three-way merges are not

Your commits live on your feature branch. A 10-minute pause to let a sibling ship costs nothing. A three-way merge across conflicting version bumps in the same files (`pyproject.toml`, `routes/main.py`, `DEVELOPMENT.md`) costs 30+ minutes AND risks losing provenance of either session's changelog entries.

### 4. Hidden resources compound the risk

Because a version bump touches 4–6 files, two sessions each making partial bumps is strictly worse than one session making the complete bump. Sequential ensures the canonical version, the `/health` literals, the changelog, and the public commitment markers all advance together. The V2.13.0 ship (2026-04-22) missed both `/health` literals after bumping `pyproject.toml` + docs; prod `/health` reported `2.12.1` while the migration head advanced correctly. The `d8bbeac` follow-up fixed it. That incident is why PREPARE FOR COMMIT step 5 in MEMORY.md explicitly requires `grep -rn "OLD.VERSION"` across the entire repo. (session history)

### 5. Sibling sessions can't see each other; the user is the bus

No cross-session handoff happens automatically in Claude Code. When session A needs to coordinate with session B, the user types a prose relay from one to the other. Assume silence between your session and any sibling. Assume every edit the sibling made is load-bearing until proven otherwise — and ask the user, not the filesystem, when in doubt. (session history)

---

## When to Apply

### Apply when ANY of the following is true

- You're running Claude Code in a multi-session configuration on a shared repo
- Working tree has uncommitted modifications you didn't make
- A version bump, changelog entry, or other exclusive-slot resource is uncommitted for the slot you'd naturally take next
- `git status` is surprising — files modified that you don't remember touching
- User or a relay message uses trigger phrases: `parallel session`, `version collision`, `sibling session`, `uncommitted worktree bump`, `the other Claude`
- You're about to run `git reset --hard`, `git stash`, `git checkout .`, or `git clean` — stop and run the pre-action safety check first

### Do NOT apply (normal solo-session workflow sufficient)

- Single-session, clean working tree, you own all the changes
- Conflict is between your branch and `origin/main` only (normal rebase territory)
- The "collision" is your own earlier commit on the same branch (normal amend / fixup territory)

### Distinguishing this pattern from `worktree-isolated-ship.md`

The sibling doc [`docs/solutions/workflow-patterns/worktree-isolated-ship.md`](../workflow-patterns/worktree-isolated-ship.md) covers a related but different case. Quick disambiguation:

| | `worktree-isolated-ship.md` | This doc |
|---|---|---|
| **Scenario** | ONE session needs to ship a focused change while its own main tree is full of unrelated in-progress work | TWO+ sessions collide on an exclusive resource in the shared main tree |
| **Tool** | `git worktree add ../branch -b <name> origin/main` — escape hatch into a clean adjacent worktree | Sequential ship protocol — peer goes first, you rebase past them |
| **Risk avoided** | Accidentally shipping unrelated local work alongside the focused change | Destroying peer session's uncommitted work via `git reset --hard` or claiming their version slot |
| **Ownership** | Dirty tree belongs to YOU (your own earlier work) | Dirty tree belongs to a SIBLING session, you can't touch it |

They're orthogonal. Combine them when appropriate: if you need to ship a focused change AND the main tree has sibling-session state, use `git worktree add` to get a clean adjacent tree, do your ship there, and still apply the A/B/C framework for the version slot.

---

## Examples

### The trigger (actual relay, 2026-04-23 04:30)

> "Relay from other session: Stopping. Version collision.
>
> The state: Your parallel scheduling session has already bumped pyproject.toml and routes/main.py (both /health literals) to 2.14.2 in the working tree. Uncommitted. The slot is claimed for their in-progress scheduling fix. tests/test_schedule_status.py has 'V2.14.2 regression' comments referencing it. That's their changelog entry in test form.
>
> My relay fix (2 commits already landed on this branch) needs a version bump too. I can't also claim 2.14.2 without trampling their work, and I can't bump routes/main.py literals to 2.14.3 without overwriting their 2.14.2 edit (which is uncommitted, but you said leave it alone).
>
> Three resolutions: A, B, C. My pick: C."

### State verification that confirmed the relay

```
$ git log --oneline origin/main..HEAD
3e2e341 docs(solutions): Railway SSH base64-pipe-to-Python pattern for remote prod ops
955a9dd feat(relay): redraw accepts operator-chosen num_teams

$ git status --short
 M CLAUDE.md
 M DEVELOPMENT.md
 M docs/solutions/integration-issues/rebuild-flights-orphans-saturday-spillover-2026-04-21.md
 M pyproject.toml
 M routes/main.py
 M routes/scheduling/__init__.py
 M routes/scheduling/events.py
 M services/schedule_status.py
 M templates/scheduling/events.html
 M tests/test_schedule_status.py
?? docs/solutions/logic-errors/schedule-status-warning-false-positive-list-only-events-2026-04-22.md

$ grep -n "V2\\.14\\.2" tests/test_schedule_status.py
108:        """V2.14.2 regression: is_open=False + name in LIST_ONLY_EVENT_NAMES
137:        """V2.14.2 regression: Partnered Axe Throw and Pro-Am Relay run on
```

Two committed relay commits safely on the feature branch. Ten sibling-session files modified (8 committed-worthy + 1 new untracked doc). Two public V2.14.2 commitment markers in test docstrings.

### The decision, stated back to the user

Option C. Five reasons:

1. `feedback_never_stash_unknown_changes.md` rules out A and B — both require unauthorized touches on sibling session's in-flight work.
2. Scope asymmetry — sibling's diff is 8 files / +314/-16 with an already-written test suite naming V2.14.2 publicly. Relay is narrow (2 commits). Larger committed work ships first.
3. No data loss either way — relay's 2 commits are parked on `fix/proam-relay-team-count`. Waiting costs nothing.
4. Three-way-merge avoidance — shipping V2.14.3 before V2.14.2 lands would guarantee conflict on sibling session's next commit.
5. Race weekend. Sequential = predictable. Parallel version-slot fights = unpredictable.

### The resulting ship sequence

| Version | PR | Squash SHA | Session | Content |
|---|---|---|---|---|
| V2.14.1 | #80 | `e476e12` | `d08c7656` | FNF per-event stand count override |
| V2.14.2 | #81 | `68e78ca` | `4855294e` (sibling) | Schedule-status warning scope fix (list-only events) |
| V2.14.3 | #82 | `279f0d2` | `8fa4f1a6` (rebased) | Pro-Am Relay redraw accepts operator-chosen num_teams |

Prod went `2.14.1 → 2.14.2 → 2.14.3` cleanly. No force-push. No three-way merge. No data loss.

### The antipattern that preceded and motivated this pattern

```bash
# WRONG — what session d08c7656 did at 03:34 UTC on 2026-04-23, before the relay arrived
git reset --hard origin/main
```

This wiped unstaged `routes/proam_relay.py` and `templates/proam_relay/dashboard.html` belonging to sibling session `8fa4f1a6`. `git fsck --lost-found` returned only two unrelated dangling blobs. Partial recovery only, via VSCode Timeline and the sibling session re-applying from its own conversation context. (session history)

### The correct replacement pattern

```bash
# Pre-action safety check FIRST
git diff --quiet && git diff --cached --quiet && [ -z "$(git ls-files --others --exclude-standard)" ] \
  || { echo "TREE HAS UNKNOWN STATE — STOP"; git status --short; exit 1; }

# If safe: move ref without touching the tree
git switch main
git branch -f <stale-branch> origin/main
git switch <stale-branch>
```

If the pre-action check fails, the loop is:

1. Read `git status --short` output
2. Grep for version strings and recent ticket identifiers to identify the sibling session's footprint
3. Relay to the user with the A/B/C framework and an Option-C recommendation
4. Only proceed once the sibling has shipped and your rebase target is clean

---

## Related

### Prior art in this repo

- **[`docs/solutions/workflow-patterns/worktree-isolated-ship.md`](../workflow-patterns/worktree-isolated-ship.md)** — one-session version of "don't overwrite dirty tree." Different scenario (same-author-multiple-concerns), complementary pattern. See distinction table above.
- **[`docs/solutions/best-practices/railway-postgres-operational-playbook-2026-04-21.md`](./railway-postgres-operational-playbook-2026-04-21.md)** — deploy coordination precedent; same race-weekend operational mode.
- **[`docs/solutions/integration-issues/github-actions-pg-backup-four-bug-chain-2026-04-22.md`](../integration-issues/github-actions-pg-backup-four-bug-chain-2026-04-22.md)** — adjacent "careful-sequencing" case (bugs chained one-at-a-time until each was cleared).

### Memory rules this pattern depends on

- `feedback_never_stash_unknown_changes.md` — load-bearing guardrail; rules out Options A and B. (auto memory [claude])
- `feedback_reset_hard_wipes_unstaged.md` — sister rule written mid-session after the `git reset --hard` incident that motivated this pattern. (auto memory [claude])
- `feedback_memory_describes_intent.md` — MEMORY.md patch notes for a sibling's unfinished work describe *intent*, not shipped state. Don't treat a sibling's uncommitted bump as authoritative until its PR lands. (auto memory [claude])
- `feedback_one_block_commands.md` — deliver terminal commands as ONE paste-ready block. (auto memory [claude])

### Precedent — fixture-shape-diverging-from-reality pattern (V2.14.0 codex hotfix, PR #73)

The same root shape caused the V2.14.0 Pro-Am Relay teams-sheet empty-rows bug: a test fixture invented a `members` key that matched the template read but not the real `ProAmRelay.run_lottery()` emitter shape (which uses `pro_members` + `college_members`). Both problems get solved the same way: **verify against the real artifact, don't trust the description of it.** For the teams sheet, that meant round-tripping through the real service emitter; for version-slot collisions, that means `git status` + grep the real worktree, not just trust the relay message. (session history)

### Trigger phrases for discovery

`parallel session`, `sibling session`, `multi-session`, `version collision`, `uncommitted worktree bump`, `the other Claude`, `relay from other session`, `pyproject.toml collision`, `/health literal race`
