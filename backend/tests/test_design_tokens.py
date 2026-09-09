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
