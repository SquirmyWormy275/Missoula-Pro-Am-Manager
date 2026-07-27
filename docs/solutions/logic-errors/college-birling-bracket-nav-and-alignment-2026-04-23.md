---
title: "College Birling: sidebar nav dead-ends at first-gender PDF + bracket rounds visually unaligned"
date: 2026-04-23
category: logic-errors
module: routes/scheduling/birling, templates/scheduling
problem_type: logic_error
component: flask_route
symptoms:
  - "Sidebar 'Birling Brackets' link opens combined print PDF that silently skips unseeded gender events, making the second gender appear unseedable"
  - "User reports: 'the other gender CANNOT BE SEEDED' and 'when I click on the birling menu it takes me to the first gender's printable bracket'"
  - "On the Birling management page, bracket rounds render with misaligned match boxes — child matches don't sit at the geometric midpoint of their parents; no connector lines between rounds"
  - "User describes the on-screen bracket view as 'garbled unintelligible slop'"
root_cause: logic_error
resolution_type: code_fix
severity: high
related_components:
  - jinja_template
  - css
tags:
  - flask
  - jinja
  - birling
  - bracket
  - navigation
  - sidebar
  - discoverability
  - css
  - flex
  - ui-alignment
  - run-show
---

# College Birling — Navigation dead-end + bracket visualization unaligned (V2.14.9)

## Problem

Two discrete bugs on the College Birling feature shipped together in PR #90 (V2.14.9, main commit `c265687`, Railway deploy 2026-04-23): the sidebar "Birling Brackets" link routed to a filtered print endpoint that made the second gender's seeding page unreachable from the primary nav, and the on-screen bracket visualization rendered as misaligned boxes because round containers had no shared row height.

## Symptoms

**Bug A — Navigation dead-end.** User report:

> when I try and seed the birling brackets, whatever gender I see first seeds and generates the bracket, and the other gender CANNOT BE SEEDED. When I click on the birling menu it takes me to the first gender's printable bracket.

After seeding one gender (e.g. Men's), clicking the sidebar produced a combined PDF showing only the already-seeded bracket. The unseeded gender's Event still existed at `/scheduling/<tid>/event/<eid>/birling` but was only reachable via a deeply-nested card inside the Events page's Friday tab. Operators had no visible path from the sidebar to the second gender's seeding form.

**Bug B — Bracket visualization unaligned.** User report:

> the "bracket" on the main page (not the printable bracket) looks like shit. Please make it an actual bracket instead of garbled unintelligible slop

Round-N matches drifted off the midpoint between their round-N-1 parents. No connector lines between rounds. Losers bracket stacking was unreadable.

## What Didn't Work (Bug A — eliminated hypotheses)

Three plausible data-layer causes for "second gender cannot be seeded" were ruled out before the fix was scoped as pure UX:

- **Gender cross-contamination in signup filtering.** `_signed_up_competitors(event)` in [routes/scheduling/__init__.py](../../../routes/scheduling/__init__.py#L91) filters by `event.gender` correctly — each gendered Event sees only its own competitor pool.
- **Cross-event bracket data leak.** `BirlingBracket._save_bracket_data()` in [services/birling_bracket.py](../../../services/birling_bracket.py#L43-L46) writes only to the specific event's `payouts` JSON; seeding one Event cannot overwrite another.
- **Cross-event seed overwrite during ability rankings.** Pre-seedings save in [routes/scheduling/ability_rankings.py](../../../routes/scheduling/ability_rankings.py#L107-L140) iterates per-event and only processes events whose form key is present in the POST body.

Each gendered Event's bracket state lives in its own `event.payouts` JSON. The data model was correct the whole time; the sidebar just pointed at the wrong URL.

## Solution

### Bug A — Birling index hub route

New handler in [routes/scheduling/birling.py](../../../routes/scheduling/birling.py) serving `GET /scheduling/<tid>/birling`:

```python
@scheduling_bp.route('/<int:tournament_id>/birling', methods=['GET'])
def birling_index(tournament_id):
    """Landing page listing every college birling event in the tournament."""
    tournament = Tournament.query.get_or_404(tournament_id)
    events = (Event.query
              .filter_by(tournament_id=tournament_id)
              .filter(Event.scoring_type == 'bracket')
              .order_by(Event.event_type, Event.name, Event.gender)
              .all())
    rows = []
    for event in events:
        payload = json.loads(event.payouts or '{}')
        bracket = payload.get('bracket') or {}
        has_bracket = bool(bracket.get('winners'))
        placements = payload.get('placements') or {}
        competitors = payload.get('competitors') or []
        total = len(competitors) if has_bracket else len(_signed_up_competitors(event))
        completed = has_bracket and total > 0 and len(placements) >= total
        status = 'completed' if completed else ('in_progress' if has_bracket else 'not_seeded')
        rows.append({'event': event, 'has_bracket': has_bracket, 'status': status, ...})
    return render_template('scheduling/birling_index.html', tournament=tournament, events=rows)
```

New template `templates/scheduling/birling_index.html` — card grid, one card per birling event with status badge (`Not seeded` / `Seeded` / `Complete`), Seed/Manage button → `birling_manage`, Print Blank → `birling_print_blank`, plus a single "Print All Brackets" action at top → `birling_print_all`.

Sidebar flip in [templates/_sidebar.html:226](../../../templates/_sidebar.html#L226):

```jinja
{# BEFORE #}
<a href="{{ url_for('scheduling.birling_print_all', tournament_id=tournament.id) }}"
   class="nav-link sidebar-child" target="_blank" rel="noopener"
   title="Birling Brackets (seeded, printable)">
    <i class="bi bi-diagram-3 sb-icon"></i>
    <span class="sb-text ms-2">Birling Brackets</span>
</a>

{# AFTER #}
<a href="{{ url_for('scheduling.birling_index', tournament_id=tournament.id) }}"
   class="nav-link sidebar-child {{ 'active' if request.endpoint == 'scheduling.birling_index' else '' }}"
   {% if request.endpoint == 'scheduling.birling_index' %}aria-current="page"{% endif %}
   title="Birling Brackets — seed, manage, and print each gender's bracket.">
    <i class="bi bi-diagram-3 sb-icon"></i>
    <span class="sb-text ms-2">Birling Brackets</span>
</a>
```

`target="_blank"` was removed — the link now navigates in-app rather than opening a PDF. The `birling_print_all` endpoint is preserved for the "Print All Brackets" action on the new hub.

### Bug B — Shared row height drives geometric midpoint placement

CSS custom properties on `.bracket-container` in [templates/scheduling/birling_manage.html](../../../templates/scheduling/birling_manage.html):

```css
.bracket-container {
    --match-h: 62px;
    --match-gap: 14px;
    --round-gap: 32px;
    --connector-w: 16px;
}
```

Inline `--row-match-count` per row, set from winners round-1 match count:

```jinja
{% set wb_round1_count = bracket.winners[0]|length if bracket.winners[0] else 1 %}
<div class="bracket-row" style="--row-match-count: {{ wb_round1_count }};">
```

Deterministic row min-height shared across all rounds in the row:

```css
.bracket-row {
    --row-height: calc(
        var(--row-match-count, 1) * var(--match-h)
        + max(0, var(--row-match-count, 1) - 1) * var(--match-gap)
    );
    min-height: var(--row-height);
    align-items: stretch;
}
.bracket-round {
    display: flex;
    flex-direction: column;
    justify-content: space-around;
}
```

Horizontal connector stubs via pseudo-elements on each `.bracket-match`:

```css
.bracket-round:not(:last-child) .bracket-match::after {
    content: "";
    position: absolute;
    top: 50%;
    right: calc(var(--connector-w) * -1);
    width: var(--connector-w);
    height: 1px;
    background: var(--connector-color);
}
.bracket-round:not(:first-child) .bracket-match::before { /* mirror left */ }
```

Losers bracket override — drops `space-around` because W-loser drop-down rounds interleave with LB consolidation rounds and the geometric parent-midpoint model does not apply to that alternation:

```jinja
<div class="bracket-row losers-row">
```

```css
.bracket-row.losers-row { align-items: flex-start; }
.bracket-row.losers-row .bracket-round {
    justify-content: flex-start;
    gap: var(--match-gap);
}
```

## Why This Works

**Bug A.** The previous sidebar target (`birling_print_all`) is a PDF export endpoint that silently filters out events whose bracket hasn't been generated ([routes/scheduling/birling.py:487-490](../../../routes/scheduling/birling.py#L487-L490): `if ctx is None: skipped.append(event); continue`). That filter is correct behavior for a combined PDF — no operator wants blank pages stitched in — but it is the wrong semantics for a **navigation entry point**. Navigation hubs must list every entity in the collection, regardless of configuration state, so each one is reachable. Men's and Women's birling are separate `Event` rows (`is_gendered=True` in `config.py`); each needs its own seeding session, and the hub now surfaces both side-by-side with per-event status badges.

**Bug B.** In a single-elimination winners bracket, round-N has half as many matches as round-N-1, and each round-N match sits at the midpoint between its two parents. When every round container has the same height H, `justify-content: space-around` on a flex column places N children at y-positions `H/(2N), 3H/(2N), ..., (2N-1)H/(2N)` — which are exactly the midpoints between the `2N` parent positions in the previous round. **Shared row height is the only precondition.** Without it, each round's `space-around` distributes relative to its own (variable) intrinsic content height, and alignment drifts. The `--row-match-count` custom property gives all rounds in a row the same computed min-height, and the bracket geometry falls out for free from the flexbox layout algorithm.

## Prevention

- **Flex-bracket alignment requires shared container height.** Any bracket/tree visualization using `justify-content: space-around` to place children at parent midpoints must set explicit equal heights across all round containers via a CSS custom property driven by the widest round. Without shared height, alignment is non-deterministic and rounds drift vertically.

- **Distinguish navigation targets from export endpoints.** When a sidebar or primary-nav link points at a print/export endpoint that applies a visibility filter (skip-if-not-configured, skip-if-empty, etc.), that is a navigation anti-pattern. Hub/index pages list every entity; filtering belongs on the export endpoint itself, not on the operator's entry point into the feature. If an entity exists in the DB, the operator must be able to reach it from the sidebar without a 3-click detour.

- **For "feature X is broken" reports, verify the user's navigation path actually reaches the feature.** The feature at `/scheduling/<tid>/event/<eid>/birling` worked perfectly the whole time — the sidebar just pointed elsewhere. Before digging into the feature's internals, confirm the click path from the reported entry point lands on the feature.

- **Regression test every sidebar endpoint change.** For any sidebar link modification, add a test that fetches a page rendering the sidebar and asserts the new URL appears in the HTML. Example from [tests/test_birling_index_route.py](../../../tests/test_birling_index_route.py):

  ```python
  def test_sidebar_links_to_birling_index(self, bi_auth_client, db_session):
      t = make_tournament(db_session)
      _make_birling(db_session, t, gender="M")
      db_session.flush()
      resp = bi_auth_client.get(f"/tournament/{t.id}")
      assert resp.status_code == 200
      assert f"/scheduling/{t.id}/birling\"".encode() in resp.data \
          or f"/scheduling/{t.id}/birling'".encode() in resp.data
  ```

  Consider strengthening this pattern project-wide with a `pytest` fixture that loads the sidebar once and scans for every `url_for` call, asserting none of them hit known print/export endpoints unless explicitly whitelisted.

## Related

- [docs/solutions/best-practices/bracket-scoring-category-wiring-2026-04-21.md](../best-practices/bracket-scoring-category-wiring-2026-04-21.md) — Canonical birling bracket wiring pattern (moderate overlap: shares routes/scheduling/birling.py and templates/scheduling/birling_manage.html references; different angle — this doc documents two discovered bugs post-wiring). As of V2.14.9 the sidebar-canonical birling entry is `birling_index`; `birling_print_all` remains only for direct-download usage and the hub's "Print All" action.
- [docs/solutions/logic-errors/warning-panel-cta-links-to-own-page-silent-noop-2026-04-23.md](warning-panel-cta-links-to-own-page-silent-noop-2026-04-23.md) — Same-date sibling, same class of bug (operator-facing UI element that appears to offer a path forward but silently no-ops). Different mechanism (GET-of-own-page vs filtered-PDF-without-hub).
- [docs/solutions/logic-errors/schedule-status-warning-false-positive-list-only-events-2026-04-22.md](schedule-status-warning-false-positive-list-only-events-2026-04-22.md) — Adjacent class: warning/nav surfaces that misrepresent feature state to operators.
- [docs/BIRLING_RECON.md](../../BIRLING_RECON.md) — Historical birling status audit (pre-V2.14.9; stale on navigation and bracket CSS).
