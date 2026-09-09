"""House style for everything the agents write, and the cleanup behind it.

Six agents produce prose that ends up on a customer's website or in an email
sent under their name. This is the one place that says how it should read, so
the rule cannot drift between them.

On the reason for it: Google does not block AI-generated content. Its spam
policy targets *scaled content abuse*, meaning content mass-produced to
manipulate rankings whether a person or a model wrote it, and its published
guidance rewards quality however it was produced. So the case for this style
is not that a filter is hunting for machines. It is simpler, and it holds
regardless:

* copy that reads as generated reads as unedited, and a reviewer who can
  hear the model in it stops trusting the rest of it;
* formulaic copy produced at volume is exactly what the scaled-abuse policy
  is about, so the risk is real even where the stated reason is wrong;
* the tells cost nothing to avoid.

``humanise`` exists because a prompt is a request, not a guarantee. The
punctuation rules are mechanical and safe to enforce after the fact.
Vocabulary is not: substituting words changes what the copy claims, so the
tells listed below are asked for in the prompt and never rewritten
automatically.
"""
from __future__ import annotations

import re

#: Appended to every content-producing agent's system prompt.
HOUSE_STYLE = """
How the writing must read:

- Like a person who knows the subject wrote it, not like a model completing
  a brief. Plain, specific, varied sentence length. Short sentences are
  allowed to be short.
- Never use an em dash (the long one) or an en dash (the medium one). Use a
  comma, a full stop, a colon or brackets. This is absolute: not one,
  anywhere, in any field.
- No stock openings or transitions: "In today's fast-paced world", "In the
  ever-evolving landscape", "It's worth noting", "Moreover", "Furthermore",
  "In conclusion", "That said", "Let's dive in", "delve into".
- No filler superlatives: seamless, robust, cutting-edge, game-changing,
  unlock, elevate, supercharge, revolutionise, testament to, navigate the
  complexities. If a claim needs an adjective to sound impressive, the claim
  is the problem.
- No "not only... but also", no rhetorical questions used as headings, no
  three-item lists where two items would do.
- Contractions are fine. Write "you'll" if that is how the page speaks.
- Say the specific thing. "Cuts invoice processing from three days to four
  hours" beats "dramatically improves efficiency".
- Do not open by restating the question or the heading.
"""

# The dash characters, held in constants so the rest of this module can name
# them without embedding one. The instruction above deliberately describes
# them in words for the same reason: an earlier version used em dashes to
# say "never use em dashes", and a model follows the example it is shown
# over the rule it is told.
_EM = "—"
_EN = "–"

#: Dashes a model reaches for and a person rarely types.
_SPACED_DASH = re.compile(rf"\s+[{_EM}{_EN}]\s+")
_TIGHT_DASH_WORDS = re.compile(rf"(?<=[A-Za-z])[{_EM}{_EN}](?=[A-Za-z])")
_TIGHT_DASH_DIGITS = re.compile(rf"(?<=\d)\s*[{_EM}{_EN}]\s*(?=\d)")
_LEADING_DASH = re.compile(rf"(?m)^\s*[{_EM}{_EN}]\s*")
#: Two hyphens, the same habit typed differently.
_DOUBLE_HYPHEN = re.compile(r"\s*--\s*")

#: Hyphen runs that are structure rather than punctuation, and have to
#: survive untouched.
#:
#: Learned the hard way. An earlier version of this turned
#: ``<!-- automarket:aeo:start -->`` into ``<!, automarket:aeo:start, >``,
#: breaking the marker that lets the Q&A injector replace its own section
#: instead of appending a second copy on every run. A rewritten page body can
#: legitimately contain an HTML comment, a Markdown rule or front-matter
#: fences, and none of them is a writer reaching for a dash.
_STRUCTURE = re.compile(
    # An HTML comment, including this platform's own section markers.
    r"<!--.*?-->"
    # A Markdown horizontal rule, or the fences around front matter.
    r"|(?:(?<=\n)|^)[ \t]*-{3,}[ \t]*(?=\n|$)",
    re.DOTALL,
)
#: Stands in for stashed structure while the substitutions run. A private-use
#: code point, so it cannot occur in real copy and no rule above touches it.
_HOLE = ""


def humanise(text: str) -> str:
    """Remove the punctuation tells from generated copy.

    Only punctuation. Rewriting word choices would change what the copy
    claims, and this runs after a model has already been told not to use
    them: it is the backstop, not the instruction.

    The substitutions are what an editor would type:

    * a spaced dash becomes a comma, because that is what it is standing in
      for nine times out of ten;
    * an en dash between digits becomes a hyphen, a numeric range;
    * a tight dash between words becomes a hyphen, a compound;
    * a double hyphen becomes a comma.

    Structure survives byte for byte: HTML comments, Markdown rules and
    front-matter fences are lifted out first and put back afterwards.
    """
    if not text:
        return text

    kept: list[str] = []

    def stash(match: re.Match[str]) -> str:
        kept.append(match.group(0))
        return f"{_HOLE}{len(kept) - 1}{_HOLE}"

    cleaned = _STRUCTURE.sub(stash, text)

    cleaned = _TIGHT_DASH_DIGITS.sub("-", cleaned)
    cleaned = _SPACED_DASH.sub(", ", cleaned)
    cleaned = _DOUBLE_HYPHEN.sub(", ", cleaned)
    cleaned = _TIGHT_DASH_WORDS.sub("-", cleaned)
    # A dash opening a line is a bullet, not prose.
    cleaned = _LEADING_DASH.sub("- ", cleaned)
    # Any survivor, inside brackets or doubled up, so the guarantee is
    # absolute rather than nearly.
    cleaned = cleaned.replace(_EM, ",").replace(_EN, "-")
    # The substitutions can leave ", ," or " ,".
    cleaned = re.sub(r",\s*,+", ",", cleaned)
    cleaned = re.sub(r"\s+,", ",", cleaned)

    return re.sub(rf"{_HOLE}(\d+){_HOLE}", lambda m: kept[int(m.group(1))], cleaned)


def has_dash(text: str) -> bool:
    """Whether any dash tell survived, ignoring structure.

    For the tests. The agents call :func:`humanise` and do not need to ask.
    """
    body = re.sub(r"<!--.*?-->", "", text or "", flags=re.DOTALL)
    body = re.sub(r"(?m)^[ \t]*-{3,}[ \t]*$", "", body)
    return any(mark in body for mark in (_EM, _EN, "--"))


def humanise_json(value: object) -> object:
    """Apply :func:`humanise` to every string inside a nested structure.

    Structured data carries prose in its values, a ``description``, a
    ``name``, an FAQ ``text``, and that prose publishes to the customer's
    page inside the graph. Cleaning the wrapper and not the values would
    leave the dashes exactly where a reader sees them.
    """
    if isinstance(value, str):
        return humanise(value)
    if isinstance(value, dict):
        return {key: humanise_json(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [humanise_json(inner) for inner in value]
    return value
