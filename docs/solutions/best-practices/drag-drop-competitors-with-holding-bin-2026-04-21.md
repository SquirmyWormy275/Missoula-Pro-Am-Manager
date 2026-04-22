---
title: Drag-drop competitors between heats with a client-side holding bin
date: 2026-04-21
category: best-practices
module: flight_builder
problem_type: best_practice
component: service_object
severity: medium
applies_when:
  - "Building drag-drop UI that moves entities between same-kind containers with capacity limits"
  - "Entities have mandatory pairing (partnered events, teams, etc.) that must travel together"
  - "Users need an escape-hatch holding area for complex rearrangements when target containers are full"
related_components:
  - frontend_stimulus
  - hotwire_turbo
tags:
  - drag-drop
  - sortablejs
  - holding-bin
  - partnered-events
  - cross-scope-validation
  - localstorage
---

# Drag-drop competitors between heats with a client-side holding bin

## Context

The Pro Flights page shows heat tiles from all pro events interleaved in each flight. Judges needed to rearrange individual competitors (or partnered pairs) between heats to fix gaps, but only within heats of the SAME event the competitor is signed up for. Moving a Men's Underhand competitor into a Cookie Stack heat would be nonsensical — they're not enrolled in Cookie Stack.

Naive SortableJS across all heat tiles would allow any drop. The user also needed an escape hatch: what happens when the target heat is full? A holding bin was chosen over swap-on-drop because it gives the judge freedom to rearrange multiple competitors without forcing immediate resolution of every constraint.

## Guidance

### 1. Scope SortableJS groups to a validation axis — not just "all rows draggable"

The key insight: SortableJS's `group: { name: X, put: [...] }` option restricts where items can be dropped. Use it to encode the domain validation constraint.

For competitor rearrangement, the axis is **event_id** — a Men's UH competitor can only drop into a Men's UH heat. Every heat table's Sortable is scoped to a group named after its event:

```javascript
document.querySelectorAll('.comp-sortable').forEach(function (table) {
    var eventId = table.dataset.eventId;
    new Sortable(table, {
        group: { name: 'event-' + eventId, put: ['event-' + eventId, 'bin'] },
        animation: 120,
        handle: '.comp-row-drag-handle',
        draggable: 'tr.comp-row',
        // ...
    });
});
```

The `put: ['event-' + eventId, 'bin']` clause means this table accepts drops from same-event heats OR the holding bin. Cross-event drops are refused by SortableJS at the client layer before the server is ever called.

### 2. Defense in depth: server also validates

Never trust the client. The `POST /scheduling/<tid>/heats/<source_heat_id>/drag-move` endpoint re-validates:

```python
if source.event_id != target.event_id:
    return jsonify({
        'ok': False,
        'error': 'Competitor can only be moved into a heat of the same event.',
    }), 400
```

Two-layer validation: client-side for UX (invalid drops never try), server-side for correctness (browser compromise or API abuse).

### 3. Visual feedback during drag: green/red outlines

When drag starts, mark every OTHER table as a valid or invalid target based on BOTH the group constraint and capacity:

```javascript
onStart: function (evt) {
    document.querySelectorAll('.comp-sortable').forEach(function (t) {
        if (t === evt.from) return;
        if (t.dataset.eventId === eventId) {
            var max = parseInt(t.dataset.maxStands, 10) || 4;
            var current = t.querySelectorAll('tr.comp-row').length;
            if (current < max) t.classList.add('drag-valid-target');
            else t.classList.add('drag-invalid-target');
        } else {
            t.classList.add('drag-invalid-target');
        }
    });
}
```

The CSS shows green dashed outlines on valid drops, red on invalid. Users learn the rule visually instead of by trial-and-error-toast.

### 4. Partnered-pair atomicity: move both or neither

For Jack & Jill, Double Buck, Partnered Axe Throw — the pair IS the entry. Moving one partner without the other corrupts the event. Encode the pair relationship as a data attribute and collect both IDs before the server call:

```javascript
var competitorIds = [info.competitor_id];
if (info.is_partnered && info.partner_id && !fromBin) {
    var sourceTable = document.querySelector('.comp-sortable[data-heat-id="' + sourceHeatId + '"]');
    if (sourceTable) {
        var partnerRow = sourceTable.querySelector(
            'tr.comp-row[data-competitor-id="' + info.partner_id + '"]'
        );
        if (partnerRow) { competitorIds.push(info.partner_id); }
    }
}
```

Server-side endpoint accepts `competitor_ids: [int, ...]` and moves them as a single transaction. Capacity check is on the combined count: `len(target_comps) + len(competitor_ids) <= max_stands`. Either both fit or the move is refused.

### 5. Holding bin as an escape hatch — client-side only (localStorage)

When a target heat is full, hard-refusing the drop is frustrating. Offering a temporary parking area lets users rearrange in stages: pull a competitor OUT of Heat A (freeing a slot), park them in the bin, then drop someone from the bin INTO Heat A's now-open slot.

Design choice: **the bin is client-side only**. DB is NOT updated when a competitor enters the bin. Implications:

- localStorage key per tournament: `flight-holding-bin-<tid>`
- On drop-to-bin: add to localStorage, remove row from DOM, but leave DB record untouched.
- On page reload: bin items survive (localStorage), but the server re-renders competitors in their ORIGINAL heats. Filter against localStorage to hide those rows in the DOM on load.
- On drop-from-bin-to-heat: single atomic server call moves competitor from `original_heat_id` → `target_heat_id`. Remove from localStorage on success.

```javascript
function hideBinCompetitorsInHeats() {
    var items = loadBin();
    var binIds = new Set(items.map(function (i) { return String(i.competitor_id); }));
    document.querySelectorAll('tr.comp-row').forEach(function (tr) {
        if (binIds.has(tr.dataset.competitorId)) { tr.remove(); }
    });
}
```

**Tradeoff:** If the user closes the browser with bin items in it, those competitors are still in their original heats on the server. LocalStorage is per-browser, so another machine won't see the bin. That's acceptable for this use case (single-director rearrangement session) but would be wrong for multi-user editing. For a collaborative version, the bin would need to be server-persisted.

## Why This Matters

- **Wrong scoping = data corruption.** Without event-scoped groups, a judge accidentally drops a Men's UH competitor into a Cookie Stack heat, the server saves it, now that competitor has heat assignments for an event they're not enrolled in. Score entry breaks.
- **Client-only validation = security hole.** Attackers can always bypass client JS. Every drag-drop API must re-validate on the server.
- **Pair atomicity = event correctness.** Partnered events run as pairs. A split pair from a failed atomic move makes the event unrunable.
- **Holding bin = user agency.** Without it, full-heat drops force users to either refuse or cascade-swap. Bin lets users shelve-and-reshuffle without commitment.

## When to Apply

- New drag-drop UI where the source/target containers have a validation constraint (same-event, same-type, same-parent)
- Drag-drop that touches persistent data AND has capacity limits on targets
- Any rearrangement UX where users may need to temporarily "hold" items while freeing target space
- Partnered/paired entities where one element's move MUST move the other

## Examples

### Minimal event-scoped drag-drop

```html
<!-- Each heat table is its own Sortable scoped by event_id -->
<table class="comp-sortable"
       data-heat-id="42"
       data-event-id="7"
       data-max-stands="5">
    <tr class="comp-row" data-competitor-id="101">
        <td class="td-name">
            <span class="comp-row-drag-handle">⋮</span>
            Competitor A
        </td>
    </tr>
</table>
```

```javascript
new Sortable(document.querySelector('.comp-sortable'), {
    group: { name: 'event-7', put: ['event-7', 'bin'] },
    draggable: 'tr.comp-row',
    handle: '.comp-row-drag-handle',
});
```

Only same-event tables (group `event-7`) and the bin can accept drops here.

### Server validation + partner-pair move

```python
# POST body: {"competitor_ids": [101, 102], "target_heat_id": 43}
if source.event_id != target.event_id:
    return jsonify({'ok': False, 'error': 'Same event required'}), 400

max_stands = event.max_stands or 4
if len(target.get_competitors()) + len(competitor_ids) > max_stands:
    return jsonify({'ok': False, 'code': 'target_full', 'error': 'Heat full'}), 409

for cid in competitor_ids:
    source.remove_competitor(cid)
    target.add_competitor(cid)
    target.set_stand_assignment(cid, next_free_stand())
```

## Related

- [flight-builder-cross-event-same-stand-adjacency-2026-04-21.md](../logic-errors/flight-builder-cross-event-same-stand-adjacency-2026-04-21.md): the scheduling algorithm counterpart. This doc covers manual rearrangement; that one covers automatic placement.
- SortableJS docs on `group` option: https://github.com/SortableJS/Sortable#group-option
- PR #55 (`e4e45a0`): implementation reference. See `templates/pro/flights.html`, `routes/scheduling/flights.py::drag_move_competitor`, and tests in `tests/test_saw_block_integration.py`.
