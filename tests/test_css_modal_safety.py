"""
Static check that theme.css cannot re-introduce the modal-stacking bug
that shipped twice on fix/race-day-ui-fixes (commits c50ceb2 and 488d912).

The bug: a CSS animation or transform on #main-content (or any common
ancestor of Bootstrap modals) creates a composited layer that traps the
modal at z-index 1055 BELOW the modal-backdrop at z-index 1050. The
result is every modal in the app being unclickable, with no console error
and no visible JS failure. The original developer-side test (live link
reload in DevTools) does NOT reproduce the bug because hot-swapping the
stylesheet destroys the compositor state.

This test enforces the static rule that prevents the regression: no rule
in theme.css that targets #main-content (or other named modal-ancestor
selectors) may declare a property known to create a stacking context or
promote the element to its own composited layer.

The list of "promoting" properties below is from the CSS Containment
spec (https://www.w3.org/TR/css-contain-1/) plus Chrome compositor
heuristics: any non-trivial animation on opacity/transform/filter is
treated as compositor-promoting even after the animation finishes.
"""

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
THEME_CSS = PROJECT_ROOT / "static" / "css" / "theme.css"

# Selectors that are ANCESTORS of Bootstrap modals when the modal is
# placed inside the page wrapper (not appended to <body> directly). A
# stacking context on any of these traps the modal.
MODAL_ANCESTOR_SELECTORS = [
    "#main-content",
    "main",
    "html",
    "body",
    ".d-flex",  # the wrapper between body and main in this app
]

# Properties that create a stacking context (per spec) OR get promoted to
# their own composited layer by Chrome (which behaves like a stacking
# context for hit-testing). Any non-`none`/non-`auto` value is unsafe on a
# modal ancestor.
PROMOTING_PROPERTIES = (
    "animation",
    "transform",
    "filter",
    "backdrop-filter",
    "perspective",
    "clip-path",
    "mask",
    "mix-blend-mode",
    "isolation",
    "will-change",
    "contain",
)

# Values that are explicitly safe for the properties above.
SAFE_VALUES = {
    "none",
    "auto",
    "normal",
    "initial",
    "unset",
    "0",
    "0px",
    "0s",
    "1",
}


def _parse_rules(css_text: str):
    """Yield (selector, body_text) for every top-level rule in the CSS.

    Strips block comments first to avoid matching commented-out rules.
    Naive brace-matching — assumes no @-rules with nested blocks at the
    selectors we care about (none of MODAL_ANCESTOR_SELECTORS appear
    inside @media or @supports blocks today; this test will need updating
    if that changes).
    """
    text = re.sub(r"/\*.*?\*/", "", css_text, flags=re.DOTALL)
    # Match `selector { body }` at the top level. Body must not contain `{` or `}`.
    pattern = re.compile(r"([^{}]+?)\s*\{\s*([^{}]*?)\s*\}", re.DOTALL)
    for match in pattern.finditer(text):
        selector = match.group(1).strip()
        body = match.group(2).strip()
        if selector and body:
            yield selector, body


def _value_for(body: str, prop: str) -> str | None:
    """Return the last declared value for `prop` in a CSS rule body, or None."""
    # Match `prop: value;` (last wins per CSS cascade within a single rule).
    pattern = re.compile(
        rf"\b{re.escape(prop)}\s*:\s*([^;]+?)\s*(?:;|$)", re.IGNORECASE
    )
    matches = pattern.findall(body)
    if not matches:
        return None
    return matches[-1].strip().lower().rstrip("!important").strip()


@pytest.fixture(scope="module")
def theme_css_text() -> str:
    assert THEME_CSS.is_file(), f"theme.css not found at {THEME_CSS}"
    return THEME_CSS.read_text(encoding="utf-8")


def test_theme_css_loads(theme_css_text):
    assert len(theme_css_text) > 100, "theme.css is suspiciously small"


def test_no_compositor_promoting_styles_on_modal_ancestors(theme_css_text):
    """Catch the modal-stacking regression class.

    A rule like `#main-content { animation: page-fade-in 0.2s ... }` looks
    harmless and passes every developer-side smoke test (open the page,
    look at it, run /qa with hot-swapped CSS), but ships a broken modal
    to every real user with no console error.

    If this test fails, do NOT add a rule-level disable. Either:
      1. Remove the property from the modal-ancestor rule, OR
      2. Move the modal target out of the wrapper (append to <body>), OR
      3. Add the new selector to a documented exemption list with the
         specific reason it cannot trap a modal (e.g. only applies under
         a media query that's off when modals are open).
    """
    violations = []
    for selector, body in _parse_rules(theme_css_text):
        # Only consider rules whose selector list contains one of the
        # named ancestors. A rule like `body.modal-open #foo` is not a
        # rule on body itself — it's a rule on `#foo` inside body.modal-open.
        # We split on commas (selector list) and check each piece.
        for piece in selector.split(","):
            piece = piece.strip()
            # Strip pseudo-classes for matching; keep base selector.
            base = re.split(r"[:\[]", piece, maxsplit=1)[0].strip()
            if base not in MODAL_ANCESTOR_SELECTORS:
                continue
            for prop in PROMOTING_PROPERTIES:
                value = _value_for(body, prop)
                if value is None:
                    continue
                # Normalize: any of the safe-value markers passes.
                if any(value == s or value.startswith(s + " ") for s in SAFE_VALUES):
                    continue
                # Special case: `transform: translateZ(0)` and friends are
                # explicitly used for compositor promotion. Flag them too.
                violations.append(
                    f"{selector!r} sets {prop}: {value!r} — this creates a "
                    f"stacking context on a modal ancestor and traps Bootstrap "
                    f"modals below the modal-backdrop. See "
                    f"docs/GEAR_SHARING_AUDIT.md commits c50ceb2 / 488d912."
                )
            break  # don't double-report the same selector

    assert not violations, "\n  - " + "\n  - ".join(violations)


def test_page_fade_in_keyframes_have_no_transform(theme_css_text):
    """Defensive: if a future PR re-introduces a `page-fade-in` animation,
    make sure it does not include `transform`. The original modal bug came
    from this exact keyframe block.
    """
    # Find any @keyframes named page-fade-in (and other fade-* names that
    # might be applied to modal ancestors in the future).
    pattern = re.compile(
        r"@keyframes\s+(page-fade-in|fade-in)\s*\{([^@]*?)\n\s*\}", re.DOTALL
    )
    for match in pattern.finditer(theme_css_text):
        name = match.group(1)
        body = match.group(2)
        if "transform" in body.lower():
            pytest.fail(
                f"@keyframes {name} contains `transform`. If applied to "
                f"#main-content (or any modal ancestor), this creates a "
                f"composited layer that blocks Bootstrap modals. Use "
                f"opacity-only fades, OR — better — remove the animation "
                f"entirely. See commits c50ceb2 / 488d912 for the bug history."
            )
