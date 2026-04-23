---
title: Warning panel CTA links to its own page = silent no-op
date: 2026-04-23
category: logic-errors
module: services/schedule_status, templates/scheduling/events
problem_type: logic_error
component: service_object
symptoms:
  - "Operator clicks 'Generate pro heats' button in Schedule Status warning panel; nothing visible happens"
  - "Page reloads with the same warning still visible; no flash, no generation, no error"
  - "Operator concludes the button is broken and keeps clicking it"
  - "13 pro events stay 'has no heats yet' across many click attempts"
root_cause: logic_error
resolution_type: code_fix
severity: high
related_components:
  - rails_view
  - testing_framework
tags:
  - flask
  - jinja
  - flash
  - run-show
  - schedule-status
  - cta
  - warning-panel
---

# Warning panel CTA links to its own page = silent no-op

## Problem

The Schedule Status warning panel on Run Show > Build Schedule (V2.14.0+) renders one or more "action needed" warnings. Each warning has a call-to-action button labeled with what the operator should do (`Generate pro heats`, `Build flights`, `Rebuild flights`). The CTA was rendered as `<a href="{{ url_for('scheduling.event_list', tournament_id=tid) }}">{{ link_label }}</a>`. But `scheduling.event_list` IS the Run Show > Build Schedule page itself. Clicking the button reloaded the same page with no operation triggered. Race-week operator (V2.14.6 trigger) clicked it many times expecting generation; nothing happened; warning stayed put.

## Symptoms

- "13 pro event(s) have no heats yet" warning persistent across reloads
- Click on warning's "Generate pro heats" button reloads the page; no flash message; no DB change
- The actual orange `Generate All Heats + Build Flights` form button on the same page works correctly — but operator is clicking the warning button, not the form button, because the warning is what's complaining
- Route smoke tests pass — they `GET` the warning's link target, see 200, conclude success. The "click does nothing" failure is invisible to GET-only smoke tests.

## What Didn't Work

- **Re-clicking the warning button.** Operator hit it many times — each click is a fresh `GET /scheduling/<tid>/events`, which never invokes any handler beyond the page render itself.
- **Trying to suppress the warning at the source.** The warning is correct that no heats exist. The problem is the CTA, not the warning condition.
- **A `<a href="#generate-form">` anchor jump.** Considered but rejected — still requires a second click on the form button, still doesn't address the labeling lie.

## Solution

Two-part fix:

1. **`services/schedule_status.py`** — add `submit_action: str | None` field to the `Warning_` TypedDict. Set on each actionable warning to the form `action` value the warning advertises:
   - `college_missing` → `submit_action="generate_all"`
   - `pro_missing` → `submit_action="generate_all"`
   - `pro_heats_without_flights` → `submit_action="rebuild_flights"`
   - `cookie_block_simultaneous` → `submit_action="rebuild_flights"`

2. **`templates/scheduling/events.html`** — render the warning CTA as a POST `<form>` button when `submit_action` is set. Non-actionable warnings keep the `<a href>` fallback:

```jinja
{% if w.submit_action %}
<form method="POST" action="{{ url_for('scheduling.event_list', tournament_id=tournament.id) }}"
      class="flex-shrink-0 m-0" data-loading>
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <button type="submit" name="action" value="{{ w.submit_action }}"
            class="btn btn-sm btn-{{ w.severity }}"
            data-confirm="{{ w.link_label or 'Run this action?' }} — proceed?">
        <i class="bi bi-lightning-charge me-1"></i>{{ w.link_label or 'Run' }}
    </button>
</form>
{% elif w.link %}
<a href="{{ w.link }}" class="btn btn-sm btn-outline-{{ w.severity }} flex-shrink-0">
    {{ w.link_label or 'Open' }}
</a>
{% endif %}
```

Existing form elsewhere on the page (the `Generate All Heats + Build Flights` button) already POSTs to the same endpoint with the same `action` field, so the warning button reuses the existing handler chain — no new route, no new handler logic.

Reference implementation: V2.14.6, PR #86, commit `bbe59a5`.

## Why This Works

The handler chain `event_list POST → _handle_event_list_post → _generate_all_heats / _build_pro_flights_if_possible` is the existing, tested code path that ran when the operator clicked the orange form button. The warning button now POSTs through the same chain instead of GETting a useless page reload. The label `Generate pro heats` finally matches what the click actually does.

The reason the bug existed at all: `services/schedule_status.py` was authored under the assumption that the warning panel would be rendered on a different page than the one its CTA targets — with `link = url_for("scheduling.event_list", ...)` meaning "go to the events page to do this." When the panel was added directly to `events.html` (V2.14.0), the link target became the rendering page, but the link wasn't updated. Static `url_for()` warnings have no awareness of which page is rendering them.

## Prevention

- **Grep for warnings whose link target is the page that renders them.** When adding a warning panel, audit:

```bash
# Find all warning generators
grep -rn "url_for(" services/*_status.py services/preflight.py 2>/dev/null

# For each warning's link target, confirm which template renders the warning
# and whether the link target is that template's own route
```

If the link target IS the rendering page, the warning needs a `submit_action` (POST form button), an anchor jump (`#section-id`), or a different target (sibling page with the actual form).

- **Route smoke tests are insufficient.** A test that just `GET`s the warning's link target will pass even when the click does nothing visible. Add a regression test that asserts the rendered HTML contains `name="action" value="<expected>"` for actionable warnings:

```python
def test_template_renders_form_button_when_submit_action_set(self, app, client):
    # ... seed an event that triggers the warning ...
    r = client.get(f"/scheduling/{tid}/events")
    html = r.get_data(as_text=True)
    assert 'name="action" value="generate_all"' in html, (
        "actionable warning must render as a POST submit button"
    )
```

See `tests/test_schedule_status.py::TestWarningsCarrySubmitAction` for the full pattern (4 tests covering pro_missing, college_missing, pro_heats_without_flights, and the template render check).

- **End-to-end click simulation in QA.** When adding a new warning to the panel, the verification protocol is: (1) trigger the warning condition in a dev tournament; (2) click the warning's CTA in a real browser; (3) confirm a flash appears AND a DB change happens. Don't ship a warning whose CTA hasn't been clicked end-to-end.

- **Operator-facing labels are claims.** A button labeled `Generate pro heats` is a public claim that clicking it will generate pro heats. Ship the operation, or rename the button to match what it actually does.

## Related Issues

- [`docs/solutions/logic-errors/schedule-status-warning-false-positive-list-only-events-2026-04-22.md`](./schedule-status-warning-false-positive-list-only-events-2026-04-22.md) — sibling fix in the same warning aggregator (V2.14.2). That one fixed *which* events generate warnings; this one fixes *what happens when you click them*.
- [`docs/solutions/best-practices/flash-message-html-via-markup.md`](../best-practices/flash-message-html-via-markup.md) — pattern used by the `_generate_all_heats` flash improvements that ship alongside this fix (named-skipped-event flashes with clickable registration links).
- PR #86 (V2.14.6, commit `bbe59a5`) — full implementation including 4-test regression suite.
