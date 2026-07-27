---
title: Flask dev server serves pre-pull code until restarted — pycache clear doesn't help
date: 2026-04-21
category: developer-experience
module: development_workflow
problem_type: developer_experience
component: development_workflow
severity: high
applies_when:
  - "You pulled new code on a branch but the running Flask app still behaves like the old version"
  - "Tests pass locally but the browser shows pre-fix behavior"
  - "A SessionStart hook clears __pycache__ but the bug persists"
  - "Verifying a deploy that you know is merged + live on production"
tags:
  - flask
  - dev-server
  - python-import
  - debugging
  - stale-bytecode
  - workflow-trap
---

# Flask dev server serves pre-pull code until restarted — pycache clear doesn't help

## Context

A judge reported that fixes shipped to production still weren't working in their browser. Hours went into the debugging loop below before we realized the culprit wasn't the code or the algorithm — it was a **running Python process that had imported the OLD source at startup and was still serving that in-memory bytecode**. Pulling new code on disk did nothing. Clearing `__pycache__` did nothing. The process was frozen at the moment it started.

This is a well-known Python/Flask behavior, but easy to miss when:

- A SessionStart hook already cleared `__pycache__` for you, creating the illusion that stale bytecode can't be the problem
- The user isn't sure whether their dev server is even running
- Production is correctly updated, so it feels like a "user machine" problem rather than a process-lifetime problem
- The browser might be caching an old response, adding a second possible stale layer

## Guidance

**When any user reports "the fix isn't working locally" after a merge/pull, the FIRST diagnostic is never about the code.** It's about whether the running dev server has the new code loaded. Ask one question before anything else:

> "Hit `http://localhost:5000/health` and tell me what version it reports."

If the response shows the old version, or the endpoint doesn't respond at all, you've found it — no more algorithm speculation needed. Restart the process.

### The full workflow

```bash
# 1. Confirm the dev server is reachable
curl http://localhost:5000/health
# → Returns {"version": "X.Y.Z"}

# 2. Compare the version to what's on disk
cd <project-root>
grep -n '"version"' routes/main.py
# or
cat VERSION

# 3. If the disk version is newer than the served version,
#    the process has stale bytecode in memory. Restart:
# (Ctrl+C in whichever terminal has python app.py running)
python app.py
```

### Why `__pycache__` clearing is a red herring

Python loads source once at import time, compiles to in-memory bytecode, and caches `.pyc` files alongside the `.py` on disk. Clearing `__pycache__`:

- Forces a fresh compile on the NEXT import
- Does NOT affect modules already imported into the running process

A Flask dev server that started an hour ago imported everything at boot. The in-memory compiled code is what answers requests. `__pycache__` is only the ON-DISK cache — irrelevant until the next process start.

If `python app.py` is running when you pull new code:
- The `.py` files on disk are updated ✅
- The `.pyc` files in `__pycache__` are stale (but ignored by the running process) ⚠️
- The in-memory bytecode in the running process is from the OLD code ❌

Only a process restart picks up the new code.

### Flask's reloader would fix this — but it's off by default

Flask's dev server supports `use_reloader=True` (or `app.run(debug=True)` which enables it). With the reloader on, the server watches source files and restarts on change. Most production/staging-adjacent configurations disable it:

```python
# app.py
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app = create_app()
    app.run(host='0.0.0.0', port=port, debug=False)  # <-- reloader off
```

That's intentional — reloader crashes can mask real issues, and the app state gets destroyed on every save. But it means the workflow obligation is on the developer: remember to restart after pulling.

### The SessionStart hook illusion

This project has a SessionStart hook that clears `__pycache__` at the start of every Claude session. It runs once, on session start. The hook message says "Cleared `__pycache__`." This can create the false impression that "stale bytecode" has been ruled out — but the hook doesn't touch running processes. A Flask dev server that was already running continues serving its pre-clear in-memory bytecode.

**Rule:** SessionStart pycache clearing rules out ONE class of stale-code problem (stale `.pyc` from a previous Python run). It does NOT rule out running-process staleness. Always also check whether a long-running Python process (Flask, Celery, Django runserver, FastAPI uvicorn, etc.) needs a restart.

## Why This Matters

One instance of this costs 30-60 minutes of wasted investigation — speculating about the algorithm, re-reading diffs, questioning whether the merge happened, doubting the test coverage. All while the actual cause is a one-second `Ctrl+C` + `python app.py` away.

At scale across a team, this pattern costs hours per week. The "code doesn't match behavior" feeling creates genuine mistrust of the codebase. Naming the pattern explicitly, and putting a single diagnostic step first (`curl /health`), eliminates the wasted hours.

## When to Apply

- User reports a shipped fix doesn't work in their local browser
- Local tests pass but live app behavior doesn't match
- A feature you know is merged + deployed doesn't appear in the browser
- The branch diff on disk includes the fix but the running process is older
- Debugging inside a long-running Python process (Flask dev server, Celery worker, Django runserver, uvicorn, etc.)

## Examples

### Before (what not to do)

> User: "Your fix didn't work, I still see the broken behavior."
> Assistant: "Let me re-read the algorithm and check the commits again..."
>
> [45 minutes of re-checking code, running tests, verifying the PR merged, speculating about a regression I missed...]

### After (what to do)

> User: "Your fix didn't work, I still see the broken behavior."
> Assistant: "First check — what does `curl http://localhost:5000/health` return? I want to confirm your running dev server has the new code loaded before we look at anything else."
>
> [User reports version 2.11.3 when disk has 2.12.0. Instantly resolved: restart dev server. Bug fixed.]

### Concrete commands

```bash
# Diagnostic (2 seconds)
curl -sS http://localhost:5000/health | python -m json.tool

# Fix (5 seconds)
# Ctrl+C in the terminal running the dev server, then:
cd "<project-root>"
python app.py

# Verify
curl -sS http://localhost:5000/health | grep version
```

### Building the habit

Put `/health` version checks in:

- CI smoke tests (so version drift from a bad deploy is caught automatically)
- Deploy verification steps (post-deploy canary check already does this)
- Incident response playbooks (first question: "what version is the dev/staging/prod server reporting?")
- README "debugging 101" section — this is the #1 gotcha for anyone new to the project

## Related

- `app.py` — Flask entry point; `use_reloader=False` by default
- `routes/main.py::/health` — the version-reporting endpoint that makes this diagnostic possible
- SessionStart hook in `.claude/` configs — clears `__pycache__` but not running processes
- PR #54 (V2.12.0) + PR #55 — the fixes that took hours to verify because the dev server was stale. This doc is the lesson.
- Related memory entry: [feedback_flight_even_distribution.md](../../../C:/Users/Alex%20Kaper/.claude/projects/c--Users-Alex-Kaper-Desktop-John-Ruffato-Startup-Challenge-Python-Missoula-Pro-Am/memory/feedback_flight_even_distribution.md) — the flight fix that was invisible because of this stale-server issue.
