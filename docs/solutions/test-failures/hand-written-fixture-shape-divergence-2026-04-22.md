---
module: testing
date: 2026-04-22
problem_type: test_failure
component: testing_framework
severity: high
root_cause: fixture_shape_drift
resolution_type: test_fix
symptoms:
  - "Unit tests pass. Template rendering tests pass against the fixture."
  - "In production, the rendered output is empty / malformed because the real service emits a different JSON shape than the fixture."
  - "Codex review spots it immediately by reading the real service class."
tags:
  - "pytest"
  - "fixtures"
  - "json-over-service"
  - "template-rendering"
  - "codex-review"
  - "sibling: mock-signature-matches-buggy-call-site"
---

# Hand-written fixture shape diverges from the real service emitter

## Problem

In V2.14.0 Phase 4 the Pro-Am Relay pseudo-heat work added a printable teams sheet route at `/scheduling/<tid>/relay-teams-sheet`. The new template looped over each team's member list:

```jinja
{% for team in teams %}
  {% for member in team.get('members', []) %}
    <li>{{ member.get('name') }}</li>
  {% endfor %}
{% endfor %}
```

The fixture I wrote to test the route used the same key:

```python
relay_data = {
    'status': 'drawn',
    'teams': [
        {'team_number': 1, 'members': [{'id': 1, 'name': 'Pro 1', 'gender': 'M', 'division': 'pro'}, ...]},
        ...
    ],
}
relay.event_state = json.dumps(relay_data)
```

**9 unit tests passed.** The route rendered 200. The teams sheet visually showed team names. Everything looked fine.

But `ProAmRelay.run_lottery()` in production does NOT emit a combined `members` list. It emits two separate keys per team:

```python
# services/proam_relay.py (real shape)
teams.append({
    'team_number': idx + 1,
    'pro_members': [],
    'college_members': [],
})
...
team['pro_members'].append(pro_male.pop(0))
team['college_members'].append(college_male.pop(0))
```

So against a real drawn relay, the template's `team.get('members', [])` returned `[]` for every team. Every row on the printed sheet was empty. Ten lines of output per team stripped to a blank block.

**Codex review caught it in one pass**, reading `services/proam_relay.py:185-217` against the template. The fixture had invented a key that only existed in the test. The test was self-consistent — it proved the template rendered the fixture shape, not that the template rendered production data.

## Why it passed CI

Tests only verify the relationship between inputs and outputs the test itself defines. If the fixture and the template agree on a made-up key, the test proves they agree — nothing more.

The three layers that would have caught it:

1. **Round-tripping the real emitter** — if the fixture called `ProAmRelay(tournament).run_lottery()` to generate its JSON, the template would have hit the real keys.
2. **Content-assertion on real production state** — asserting "every member name from the drawn roster appears in the rendered HTML" rather than "the route returns 200".
3. **Independent review reading both sides** — codex read the service class and the template and spotted the key mismatch.

Only layer 3 happened, post-merge.

## This is the SECOND time this pattern has bit us

The sibling `mock-signature-matches-buggy-call-site-2026-04-22.md` documents the identical pattern from V2.13.0: the test mock of `background_jobs.submit` had the SAME wrong signature as the buggy production caller. Both sides agreed on the bug. Mock and caller had never touched the real function.

In the V2.13.0 case, the divergence was in function signature. In V2.14.0, the divergence was in JSON shape. Both are instances of the same rule:

**If the test fixture and the production caller both describe the same external contract (signature, JSON shape, protocol message), one of them must round-trip through the real thing, or the contract drifts silently.**

## Resolution (V2.14.0 PR #73)

1. Rewrote the fixture in `tests/test_proam_relay_placement.py::_seed_with_flights` to emit the real `pro_members` / `college_members` shape.
2. Rewrote the template to render both lists explicitly with matching PRO/COLLEGE badges.
3. Added content-assertion tests: `TestRelayTeamsSheetRendersRealShape::test_sheet_renders_pro_member_names` asserts every seeded pro name appears in the rendered HTML.
4. Added status-transition tests (`TestRelayStatusTransitions`) that rebuild mid-show — codex also caught that `if status != 'drawn'` orphaned the relay once scoring started. That's a separate bug but lives in the same class of "fixture covered the happy path, production exposed a wider state machine."

## Prevention — future features that synthesize JSON over an existing service

When a new feature reads/writes JSON produced by an existing service (e.g. the Pro-Am Relay teams blob, Partnered Axe state, Birling bracket data):

1. **Fixture derivation:** the fixture should call the real service to produce its JSON, not hand-write the blob.
   ```python
   # Good
   relay = ProAmRelay(tournament)
   relay.run_lottery(num_teams=2)
   # Now tournament's relay_data is real — use it in the test

   # Bad
   relay.event_state = json.dumps({'status': 'drawn', 'teams': [{'members': [...]}]})
   ```

2. **Content assertions over route assertions:** `resp.status_code == 200` proves the template rendered. It does NOT prove the data was there. Assert that specific string data from the fixture appears in the rendered body.

3. **Read the service's emitter when adding a new consumer:** whenever writing a template/route that reads JSON from an existing service, open the service file and read the exact shape its methods emit. `grep` for `append`, `['key']=`, or `dumps` calls that build the JSON. Copy the shape from reality, not from imagination.

4. **Outside review for new consumers of existing state:** routes/templates that add new consumers of existing service state are a high-leverage codex-review target. They touch a wide data contract with minimal local code.

## Pattern signature

```
Divergence type      | Caught by                | V-tag
---------------------+--------------------------+-------
Function signature   | codex outside review     | V2.13.0
JSON shape           | codex outside review     | V2.14.0
Empty-collection     | prod 500 (Railway logs)  | V2.14.5
smoke test
```

**Three instances is a trilogy.** See [test-shape-matches-bug-shape-trilogy-2026-04-23.md](test-shape-matches-bug-shape-trilogy-2026-04-23.md) for the generalized meta-pattern and the three rules extracted from all three instances: mock fidelity, fixture round-trip, and non-empty seed.

## Related docs

- [test-shape-matches-bug-shape-trilogy-2026-04-23.md](test-shape-matches-bug-shape-trilogy-2026-04-23.md) — **the generalized meta-pattern.** This doc is Instance 2 of three; the trilogy doc extracts the class-level rule.
- [mock-signature-matches-buggy-call-site-2026-04-22.md](mock-signature-matches-buggy-call-site-2026-04-22.md) — sibling pattern on function signatures (Instance 1)
- [../best-practices/jinja-sort-filter-has-no-key-kwarg-2026-04-23.md](../best-practices/jinja-sort-filter-has-no-key-kwarg-2026-04-23.md) — the specific surface mechanism of Instance 3 (Jinja `|sort(key=int)` landmine)
- [../best-practices/traceback-before-repro-2026-04-23.md](../best-practices/traceback-before-repro-2026-04-23.md) — process rule from Instance 3: pull prod logs before writing local repros
- [docs/FLIGHT_FIXES_RECON.md](../../FLIGHT_FIXES_RECON.md) — the recon that scoped V2.14.0 Phase 4
- [services/proam_relay.py](../../../services/proam_relay.py) — the real emitter (run_lottery + set_teams_manually)
- `tests/test_proam_relay_placement.py::TestRelayTeamsSheetRendersRealShape` — the content-assertion guard added after codex caught it
- [../best-practices/sequential-ship-pattern-parallel-claude-sessions-2026-04-23.md](../best-practices/sequential-ship-pattern-parallel-claude-sessions-2026-04-23.md) — same meta-principle at the workflow layer: **verify against the real artifact, not its description.** That doc applies the rule to version-slot collision detection (`git status` the worktree, don't trust the relay message). This doc applies it to fixture/service shape (read the emitter, don't imagine the key).
