---
type: knowledge
problem_type: workflow-pattern
severity: low
tags:
  - "python"
  - "debugging"
  - "windows"
confidence: high
created: 2026-04-15
source: "knowledge-seed from CLAUDE.md and git history"
---

# Clear `__pycache__` as first diagnostic step

## Context
Windows + frequent branch switching + file-renames produce stale `.pyc` files that mask source changes. Symptom: source looks correct, behavior is wrong, no error traces to your code.

## Pattern
When debugging Python import errors or unexpected behavior where source code looks correct, clear bytecode caches FIRST, before deeper investigation:

```bash
find . -type d -name __pycache__ -exec rm -rf {} +
```

Also part of the "PREPARE FOR COMMIT" and "COMMIT PATCH" standing orders — scrub `__pycache__`, `.pyc`, and temp files before any commit.

## Rationale
Cost of running this command: seconds. Cost of chasing a phantom bug that's just stale bytecode: hours. This is always worth doing before you start theorizing.

## Examples
Enshrined in global `~/.claude/CLAUDE.md` under "Stale Cache" and re-stated in project CLAUDE.md Section 6. Session-start hook already clears `__pycache__` in this project.
