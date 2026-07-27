---
module: dev-workflow
date: 2026-04-21
problem_type: workflow
component: development_workflow
severity: medium
tags:
  - "git"
  - "worktree"
  - "ship"
  - "isolation"
  - "ce-ship"
---

# Ship one focused change when the working tree is full of prior-session work

## Context

A common failure mode of `/ce-ship` (or any branch-and-PR workflow) is reaching Phase 0 with a messy working tree: the current branch already has unrelated committed work (often an open PR), and the working tree has N uncommitted modifications from earlier sessions on unrelated features. The session's focused change is mixed in. The session rule "never stash/restore/revert unknown changes without approval" makes the obvious moves unsafe:

- `git stash` — forbidden; shuffling prior-session work is a revert.
- `git checkout main -b new-branch` with dirty files — carries every uncommitted change onto the new branch, polluting the focused PR.
- Committing everything together — ships unrelated concerns in one PR, drifts the branch's scope.
- Reverting unrelated files — destructive without approval.

## Pattern

Use `git worktree` to ship the focused change from an isolated second working directory. The original working tree is never touched.

```bash
# 1. Save the focused diff as a patch (only the session's own file).
git diff -- routes/scheduling/birling.py > /tmp/focus.patch

# 2. Create an isolated worktree off the correct base branch.
#    -b creates the new feature branch; origin/main is the starting point.
git worktree add ../mprom-focus-ship -b fix/focus-change origin/main

# 3. Work in the worktree from here on.
cd ../mprom-focus-ship
git apply /tmp/focus.patch

# 4. Run the ce-ship phases (tests, review, version bump, commit, push, PR)
#    entirely inside the worktree. The main working tree in the original
#    directory remains bit-identical.

# 5. After the PR is merged, clean up.
cd ../Missoula-Pro-Am-Manager        # back to original
git worktree remove ../mprom-focus-ship     # or -f on Windows if locked
git worktree prune                           # reclaim stale metadata
rm -f /tmp/focus.patch
```

## Rationale

- **Zero risk to prior-session work.** The user's in-progress modifications (11+ files in the 2026-04-21 birling-ship session: `app.py` i18n generalization, `services/preflight.py` USING-prefix fix, `tests/test_russian_translation.py`, etc.) are never moved, stashed, reverted, or committed by the ship pipeline. They stay exactly where they were.
- **Clean PR scope.** The new branch starts from `origin/main` and contains only the focused diff + version bump + changelog — the PR reviewer sees a 6-file, ~100-line change instead of 15 files.
- **Already-open-PR safe.** If the current branch has an open PR (the birling-ship ran while PR #55 was open on `fix/rebuild-flights-chains-spillover`), we don't add commits to that PR's scope.
- **Reversible.** If anything goes wrong inside the worktree, `git worktree remove -f` and restart. Nothing in the main tree moved.

## Verification

After the ship completes:

```bash
# Confirm the original working tree is untouched.
git status --short      # should match what it was before the ship

# Confirm the merge landed on main independently.
git fetch origin main
git log -1 --oneline origin/main

# Production health-check once Railway deploy settles.
curl -s https://missoula-pro-am-manager-production.up.railway.app/health | jq .version
```

## When NOT to use this pattern

- Working tree is clean. Use a normal feature branch — worktrees are overhead you don't need.
- The focused change is inside a file that ALSO has uncommitted prior-session edits. The patch would carry both. Either ship the whole set with the user's approval, or talk to the user about splitting the file's diff.
- The change spans multiple files interdependent with the prior-session work. If `services/preflight.py` needs `services/gear_sharing.py` helpers that aren't on main yet, the ship must wait — the worktree can't conjure dependencies that only exist in the dirty working tree.

## Gotchas

- **Windows file locks.** If pytest or another process inside the worktree leaves file handles open, `git worktree remove` fails with "Permission denied." Retry after the processes exit, or use `rm -rf` + `git worktree prune`. Observed on the 2026-04-21 birling-ship cleanup.
- **`gh pr checks` exit code 8** when queuing auto-merge just means "not all checks green yet" — the command still printed the status table; treat it as informational, not an error.
- **Worktree branch tracking.** `git worktree add -b name origin/main` sets the new branch to track `origin/main` by default. Confirm with `git rev-parse --abbrev-ref --symbolic-full-name @{u}` if you'll be pushing with `-u`.

## Related

- `/ce-ship` skill — Phase 0 branch policy fails when the current branch is correct but the working tree is dirty with unrelated work. This pattern resolves that stall.
- Session memory `feedback_never_stash_unknown_changes.md` — the rule this pattern exists to honor.
- 2026-04-21 birling-seed-links ship (PR #57 → `017eebc`) — first documented use. The judge-facing "Print Birling Brackets → clickable event links" fix shipped cleanly while 11 files of unrelated prior-session work sat in the main working tree.
- [`docs/solutions/best-practices/sequential-ship-pattern-parallel-claude-sessions-2026-04-23.md`](../best-practices/sequential-ship-pattern-parallel-claude-sessions-2026-04-23.md) — complementary pattern for when the dirty tree belongs to a *sibling* Claude Code session rather than your own earlier work. This doc handles "one session, many concerns"; that doc handles "two sessions, one version slot."
