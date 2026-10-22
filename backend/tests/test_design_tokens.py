"""Every design token the console references has to exist.

An undefined custom property fails silently and completely: `var(--space-5)`
where no `--space-5` is defined makes the whole declaration invalid, the
browser drops it, and nothing anywhere reports it. No console warning, no
build error, no failing test.

That is what happened. The Industry scale is 1, 2, 3, 4, 6, 8 — there is no
`--space-5` — so `.card-section { margin-bottom: var(--space-5) }` gave every
section zero bottom margin, and the next section's heading rendered on top of
the cards above it. It looked like a layout bug and was a typo in a token
name.

Checked from the test suite because this is the only place in the project
that runs anything, and a rule that only exists in somebody's head is the
rule that let it through.
"""
from __future__ import annotations

import pathlib
import re

import pytest

STYLES = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src" / "styles"

#: `var(--name` — the reference. The fallback form `var(--name, 12px)` is
#: deliberately excluded from the requirement: a declared fallback means the
#: author knew the token might be absent, which is a different decision.
_REFERENCE = re.compile(r"var\(\s*(--[\w-]+)\s*\)")
#: `--name:` at the start of a declaration — the definition.
_DEFINITION = re.compile(r"(--[\w-]+)\s*:")


#: Comments are stripped before scanning. A token named in a comment is not
#: a reference — and the comment explaining this very bug quotes the broken
#: token, which the first version of this test then flagged as the bug.
_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _sheets() -> list[pathlib.Path]:
    return sorted(STYLES.glob("*.css"))


def _code(path: pathlib.Path) -> str:
    """A stylesheet with its comments removed."""
    return _COMMENT.sub(" ", path.read_text(encoding="utf-8"))


@pytest.mark.skipif(not STYLES.exists(), reason="frontend styles not present")
def test_every_referenced_token_is_defined():
    defined: set[str] = set()
    referenced: dict[str, set[str]] = {}

    for sheet in _sheets():
        text = _code(sheet)
        defined.update(_DEFINITION.findall(text))
        for name in _REFERENCE.findall(text):
            referenced.setdefault(name, set()).add(sheet.name)

    assert defined, "no tokens found — the check would be vacuous"

    missing = sorted(
        (name, sorted(sheets)) for name, sheets in referenced.items() if name not in defined
    )
    assert missing == [], missing


@pytest.mark.skipif(not STYLES.exists(), reason="frontend styles not present")
def test_the_spacing_scale_is_what_the_console_thinks_it_is():
    """The specific gap that caused it. If the scale ever gains a 5, this
    stops being a trap and the test should be updated to say so."""
    text = "".join(_code(sheet) for sheet in _sheets())
    steps = {
        int(match)
        for match in re.findall(r"--space-(\d+)\s*:", text)
    }
    assert steps == {1, 2, 3, 4, 6, 8}, sorted(steps)


@pytest.mark.skipif(not STYLES.exists(), reason="frontend styles not present")
def test_inline_styles_do_not_reference_tokens_without_a_fallback():
    """A token used from a JSX `style` prop is just as silent when it is
    wrong, and harder to spot than one in a stylesheet."""
    src = STYLES.parent
    defined: set[str] = set()
    for sheet in _sheets():
        defined.update(_DEFINITION.findall(_code(sheet)))

    missing: list[tuple[str, str]] = []
    for path in sorted(src.rglob("*.tsx")):
        for name in _REFERENCE.findall(path.read_text(encoding="utf-8")):
            if name not in defined:
                missing.append((path.name, name))
    assert missing == [], missing


# ── Text contrast ────────────────────────────────────────────────────────────

_ROOT_TOKEN = r"^\s*{name}\s*:\s*(#[0-9a-fA-F]{{6}})\s*;"


def _hex_token(name: str) -> str:
    """The `#rrggbb` value a `:root` token is set to in industry.css."""
    text = _code(STYLES / "industry.css")
    match = re.search(_ROOT_TOKEN.format(name=re.escape(name)), text, re.M)
    assert match, f"{name} is not a #rrggbb literal in industry.css"
    return match.group(1)


def _luminance(hex_colour: str) -> float:
    """WCAG 2.x relative luminance."""
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_contrast_helper_matches_known_wcag_values():
    """Guards the maths: black on white is 21:1, and the old subtle grey
    (#9ca3af) is the 2.5:1 failure this test exists to stop."""
    assert round(_contrast("#000000", "#ffffff"), 1) == 21.0
    assert _contrast("#9ca3af", "#ffffff") < 4.5


@pytest.mark.skipif(not STYLES.exists(), reason="frontend styles not present")
@pytest.mark.parametrize("ground", ["--color-surface", "--color-bg"])
def test_subtle_text_meets_wcag_aa(ground: str):
    """Subtle text is still body-size text, so it needs 4.5:1 (WCAG 1.4.3)
    on both white cards and the page background."""
    ratio = _contrast(_hex_token("--color-text-subtle"), _hex_token(ground))
    assert ratio >= 4.5, f"--color-text-subtle on {ground} is {ratio:.2f}:1"


@pytest.mark.skipif(not STYLES.exists(), reason="frontend styles not present")
@pytest.mark.parametrize("ground", ["--color-surface", "--color-bg"])
def test_muted_text_meets_wcag_aa(ground: str):
    """Muted text carries hints, captions and table metadata at body size, so
    it needs 4.5:1 too. The old #6b7280 was 4.47:1 on the page background."""
    ratio = _contrast(_hex_token("--color-text-muted"), _hex_token(ground))
    assert ratio >= 4.5, f"--color-text-muted on {ground} is {ratio:.2f}:1"


def test_the_old_muted_grey_is_the_failure_the_muted_test_catches():
    assert _contrast("#6b7280", "#f5f6f8") < 4.5
