---
module: templates/jinja
date: 2026-04-23
problem_type: best_practice
component: jinja_template
severity: high
applies_when:
  - "A Jinja2 template needs to sort a dict's items by a custom key transformation (e.g., numeric sort on string keys like '1', '2', '10')"
  - "A developer reaches for |sort(..., key=X) in a template because Python's sorted() accepts key="
  - "A template uses |sort(attribute='0', key=int), |sort(key=...), or any |sort() kwarg other than reverse, case_sensitive, attribute"
related_components:
  - testing_framework
  - rails_view
tags:
  - jinja2
  - template-filters
  - python-vs-jinja
  - latent-crash
  - pre-commit-grep
  - sort-filter
---

# Jinja2's `sort` Filter Has No `key=` Kwarg — Use a Model Method Instead

## Context

In Python, `sorted(iterable, key=callable)` is the reflex. You want integer-ordered sort on string keys `"1", "2", ..., "10", "11"`? `sorted(items, key=lambda kv: int(kv[0]))`. Done.

In Jinja2, the filter named `sort` **looks** the same but accepts a different set of kwargs. Its full signature is:

```python
# jinja2/filters.py
def do_sort(value, reverse=False, case_sensitive=False, attribute=None):
    ...
```

Three kwargs. No `key`. Anything else raises `TypeError: do_sort() got an unexpected keyword argument 'X'` **at render time** (not startup, not import, not template-load — only when the filter actually runs on real data).

This bit us in V2.14.5 on 2026-04-23. Three templates used:

```jinja
{% for pos, amt in tpl.get_payouts().items()|sort(attribute='0', key=int) %}
```

Every Payout Manager page view 500'd with `TypeError: do_sort() got an unexpected keyword argument 'key'` as soon as any `PayoutTemplate` row existed. The page was fine with zero templates — the outer `{% if templates %}` short-circuited — so empty-DB smoke tests never hit the bomb. Prod traceback request IDs `9bb650a4`, `f7931c78`, `d9893c09` on 2026-04-23 06:24-06:25 UTC.

## Guidance

### Rule 1 — Never try custom-key sort in Jinja

Jinja's `sort` can sort by:
- The value itself (default): `items|sort`
- An attribute or nested path: `items|sort(attribute='name')`
- An integer index (dict-item tuples): `items|sort(attribute=0)`
- Reverse: `items|sort(reverse=true)`
- Case-insensitive (strings only): `items|sort(case_sensitive=false)`

That's it. If you need `int()` conversion, `.lower()` normalization, or any other transformation before comparison, **Jinja cannot do it**. Don't invent syntax that Python's `sorted()` would accept — the filter will silently parse to an identical-looking call and then explode at render.

### Rule 2 — Put custom-key sort in a model method

Move the sort to Python where the real `sorted()` lives:

```python
# models/payout_template.py
class PayoutTemplate(db.Model):
    ...
    def sorted_payouts(self) -> list:
        """Return [(pos, amt), ...] sorted by position as int.

        Templates call this instead of |sort(attribute='0', key=int) — Jinja2's
        sort filter has no key= kwarg, and a string sort puts '10' before '2'.
        """
        return sorted(self.get_payouts().items(), key=lambda kv: int(kv[0]))
```

Then:

```jinja
{% for pos, amt in tpl.sorted_payouts() %}
```

The method name reads as "this returns payouts in sorted order" — clearer than the filter chain anyway. Free readability win.

### Rule 3 — Grep for the bomb at commit time

Two grep recipes to find existing and future instances:

```bash
# Specific bug family — |sort() with any kwarg Jinja doesn't accept
grep -rn '|sort(' templates/ | grep -v 'sort()' \
  | grep -vE 'sort\([^)]*(reverse=|case_sensitive=|attribute=)[^)]*\)$'
```

The stricter catch-22 (any `|sort(...key=...)` at all — the exact V2.14.5 family):

```bash
grep -rn '|sort([^)]*key=' templates/
```

Drop the first one into `scripts/lint_templates.sh` or a `pre-commit` hook. Output is empty when clean, noisy when broken. CI-grade catch cost: ~10 ms per run.

## Why This Matters

Three reasons this class of bug deserves its own doc:

1. **The failure is render-time, not load-time.** Unit tests that don't exercise the specific branch never see it. Compile-phase linting doesn't catch it. The bomb waits for real data.
2. **The fix looks obviously correct.** `|sort(attribute='0', key=int)` reads as fluent Python-flavored Jinja to every Python dev. Code review without running the page misses it every time.
3. **Templates compose silently with the test-shape-matches-bug-shape meta-pattern.** The V2.14.5 instance was invisible because the smoke test seeded zero rows, which short-circuited the outer `{% if templates %}` before the broken filter ran. A correct pre-commit grep catches the bomb without requiring the test harness to exercise it. See the [trilogy doc](../test-failures/test-shape-matches-bug-shape-trilogy-2026-04-23.md) for the broader meta-pattern.

## When to Apply

- Any time you're tempted to write `|sort(key=...)`, `|sort(reverse=true, key=...)`, or `|dictsort(key=...)` in a template — stop and add a model method.
- Any time you need non-alphabetical ordering of string-keyed dict items — model method.
- Any time you see `|sort(attribute='0', ...)` — this is the specific footgun shape; prefer a model method returning `.items()` pre-sorted.
- As a periodic codebase audit — run the grep recipes quarterly to catch drift.

## Examples

### Before (crashes at render time)

```jinja
{# templates/scoring/tournament_payouts.html #}
<tbody>
  {% for pos, amt in tpl.get_payouts().items()|sort(attribute='0', key=int) %}
  <tr>
    <td>{{ pos }}th</td>
    <td>${{ "%.2f"|format(amt) }}</td>
  </tr>
  {% endfor %}
</tbody>
```

### After (renders correctly, sort is intentional and testable)

```python
# models/payout_template.py
def sorted_payouts(self) -> list:
    return sorted(self.get_payouts().items(), key=lambda kv: int(kv[0]))
```

```jinja
{# templates/scoring/tournament_payouts.html #}
<tbody>
  {% for pos, amt in tpl.sorted_payouts() %}
  <tr>
    <td>{{ pos }}th</td>
    <td>${{ "%.2f"|format(amt) }}</td>
  </tr>
  {% endfor %}
</tbody>
```

Bonus: the model method is directly unit-testable without a template render. V2.14.5 added:

```python
def test_sorted_payouts_integer_order():
    tpl = PayoutTemplate(name="sort check")
    tpl.set_payouts({"10": 10.0, "2": 20.0, "1": 30.0, "11": 5.0})
    positions = [pos for pos, _amt in tpl.sorted_payouts()]
    assert positions == ["1", "2", "10", "11"]  # int, not lexicographic
```

That test would fail immediately on the old buggy filter (which couldn't run at all) — shorter feedback loop than waiting for the route smoke to 500.

## Related

- [../test-failures/test-shape-matches-bug-shape-trilogy-2026-04-23.md](../test-failures/test-shape-matches-bug-shape-trilogy-2026-04-23.md) — The meta-pattern this bug is an instance of. Empty-DB smokes hid this exact filter bomb for the template's entire lifetime.
- [traceback-before-repro-2026-04-23.md](traceback-before-repro-2026-04-23.md) — The investigation-process lesson from the same V2.14.5 session: ask for the Railway traceback before synthesizing local repro tests.
- [flash-message-html-via-markup.md](flash-message-html-via-markup.md) — Adjacent template gotcha: Jinja's `Markup` handling around `|safe` vs autoescape.

## Files Touched in V2.14.5

The grep that found every instance at fix-time:

```
templates/scoring/tournament_payouts.html:330
templates/scoring/configure_payouts.html:177
templates/tournament_setup.html:366
```

All three replaced with `tpl.sorted_payouts()`. Zero Jinja `|sort(..., key=...)` instances remain in the repo as of commit `eafa69c`.
