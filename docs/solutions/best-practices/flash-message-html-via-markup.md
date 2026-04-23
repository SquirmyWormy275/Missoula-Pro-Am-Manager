---
module: routes
date: 2026-04-21
last_updated: 2026-04-23
problem_type: best_practice
component: flash_messages
severity: low
tags:
  - "flask"
  - "flash"
  - "markupsafe"
  - "xss"
  - "templates"
---

# Emit HTML (links, emphasis) in a Flask flash message safely

## Context

The `templates/base.html` toast renderer pipes flash bodies through `{{ message }}`, which Jinja auto-escapes. A plain-string `flash('Click <a href="/x">here</a>', 'warning')` renders the literal angle brackets to the user. Historically in this codebase, flashes were always plain text — fine when the message only points the user at a single page. It falls short when the flash enumerates N items the user needs to navigate to individually (e.g., "seed these 2 birling events: Women's Birling, Men's Birling" — each should be a direct link).

Wrapping the flash body in `|safe` inside the template breaks the auto-escape for EVERY flash and invites XSS via any message built from user-controlled data.

## Pattern

Use `markupsafe.Markup` at the call site — `flash()` accepts any string-like value, Jinja recognizes `Markup` as already-safe and skips auto-escape for just that one message. Always pass user-controllable substrings through `markupsafe.escape` BEFORE concatenating them into the Markup wrapper.

```python
from flask import flash, url_for
from markupsafe import Markup, escape

def _seed_links(events, tournament_id):
    parts = []
    for evt in events:
        href = url_for('scheduling.birling_manage',
                       tournament_id=tournament_id, event_id=evt.id)
        parts.append(
            '<a href="{href}" class="text-white fw-semibold text-decoration-underline">{name}</a>'.format(
                href=escape(href),              # escape user-controllable data
                name=escape(evt.display_name),  # before concatenation
            )
        )
    return Markup(', '.join(parts))             # Markup only wraps the final safe string

flash(
    Markup('No birling brackets have been seeded yet: {}. Seed at least one to print.'
           .format(_seed_links(skipped, tournament_id))),
    'warning',
)
```

Template side stays unchanged — no `|safe` filter in `base.html`. A plain-string flash from elsewhere in the app still auto-escapes correctly.

## Rationale

- **Targeted opt-out.** Only the specific flash that needs HTML opts out of auto-escape, via `Markup`. Every other flash in the app stays safe-by-default.
- **Escape-before-wrap is mandatory.** Wrapping the concatenated string in `Markup` bypasses escaping — so each user-sourced substring (display names, URLs) must be individually escaped with `markupsafe.escape` BEFORE it enters the concatenation. Skipping the per-substring escape reintroduces XSS.
- **No new dependency.** `markupsafe` is a Flask transitive dep; no `requirements.txt` change.
- **Works with Bootstrap toasts.** The base toast uses `bg-warning` / `bg-danger` (white text). `.alert-link` is scoped to `.alert` elements, not toasts — use inline Bootstrap utility classes (`text-white fw-semibold text-decoration-underline`) to make the link visible on the colored background.

## Reference implementations

- `routes/scheduling/birling.py::birling_print_all` — first Markup flash in the codebase (V2.12.1, commit `017eebc`). Both the "all ungenerated → 302 warning" flash and the "mixed skip info" flash use this pattern.
- `routes/scheduling/__init__.py::_generate_all_heats` — second use (V2.14.6, commit `bbe59a5`). When `Generate All Heats + Build Flights` skips events because no competitors entered them, the wrapper now emits two distinct `Markup`-flashed warnings (one per division) that name every skipped event and link to the matching registration page (`registration.pro_registration` / `registration.college_registration`). Replaced the previous "Skipped N without entrants" silent flash. The split-by-division pattern is the right shape when an operation can fail across two distinct user-facing surfaces — emit one Markup flash per surface, each carrying its own targeted link, instead of one combined message.
- Tests — `tests/test_routes_birling_print.py::TestPrintAll::test_all_ungenerated_flash_contains_seed_links` and `test_mixed_skip_flash_contains_seed_links` assert on the presence of `href="..."` in the flash body read back from `session['_flashes']`. The V2.14.6 ship adds inline end-to-end verification through the Flask test client (GET → POST → 302 → follow-up GET asserts skipped event name + registration link both present in flash body).

## Promote-to-helper threshold

Two use sites is "document the pattern, keep inline." If a third call site appears, promote to a helper — likely `services/flash_helpers.py::flash_with_links(message_template, links_dict, category)` — so the `Markup` + `escape` boilerplate doesn't get copy-pasted into every new caller. Don't pre-build the helper now; the two existing call sites have meaningfully different shapes (birling enumerates events into one inline message; `_generate_all_heats` splits into two separate flashes) and the abstraction would cost more than it saves at N=2.

## Reading flash bodies in tests

Because the message stored in `session['_flashes']` is the stringified Markup (already-escaped HTML), tests can string-match the rendered `<a href="...">` exactly:

```python
resp = client.get(f"/scheduling/{t.id}/birling/print-all")
assert resp.status_code in (302, 303)

with client.session_transaction() as sess:
    flashes = sess.get("_flashes", [])
warning_bodies = [msg for cat, msg in flashes if cat == "warning"]
assert f'href="/scheduling/{t.id}/event/{event_id}/birling"' in warning_bodies[-1]
```

## When NOT to use

- The flash contains ONLY plain text. Stick with `flash('plain text', 'info')` — `Markup` adds zero value and one more thing to get wrong.
- The flash wraps a string that is itself ALREADY HTML from another source. Don't double-wrap; don't Markup-wrap an `.format()` of a string that contains literal angle brackets the user typed (XSS). If in doubt, escape the input first.
- The HTML is complex enough to need templating logic (loops, conditionals, nested structure). That belongs in a template, not a flash. Flashes should fit on one toast line.
