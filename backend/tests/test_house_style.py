"""How the agents write, and the backstop that enforces the punctuation.

The premise this came from was that Google blocks AI-generated content. It
does not: its spam policy targets *scaled content abuse*, mass-produced
content aimed at rankings whether a person or a model wrote it, and its
guidance rewards quality however it was produced.

The instruction is still right, for reasons that hold either way. Copy that
reads as generated reads as unedited, and a reviewer who can hear the model
in it stops trusting the rest of it. Formulaic copy at volume is what the
scaled-abuse policy is actually about. And the tells cost nothing to avoid.

Punctuation is enforced mechanically because a prompt is a request.
Vocabulary is not: substituting a word changes what the copy claims, so the
banned phrases are asked for and never rewritten.
"""
from __future__ import annotations

import pytest

from app.agents.base.style import HOUSE_STYLE, has_dash, humanise, humanise_json

#: Every agent whose output publishes or gets sent.
WRITERS = (
    "on_page_seo_sync",
    "aeo_qa_injector",
    "knowledge_graph_schema",
    "dynamic_creative_optimizer",
    "digital_pr_outreach",
    "competitor_link_monitor",
)


@pytest.mark.parametrize("slug", WRITERS)
def test_every_writing_agent_carries_the_house_style(slug: str):
    module = __import__(f"app.agents.{slug}.prompts", fromlist=["SYSTEM"])
    assert HOUSE_STYLE in module.SYSTEM, slug
    # The instruction that was asked for, stated absolutely.
    assert "Never use an em dash" in module.SYSTEM
    assert "not one," in module.SYSTEM


@pytest.mark.parametrize("slug", WRITERS)
def test_no_prompt_uses_the_punctuation_it_bans(slug: str):
    """The bug this test found on its first run.

    The AEO prompt opened "You prepare web content so answer engines, em
    dash, Perplexity, ChatGPT, Google's AI answers, Claude, em dash, can
    retrieve it" while also instructing the model never to use one. A model
    follows the example it is shown over the rule it is told, so the
    instruction was working against itself in five of the six files.

    The style block names the characters in words for the same reason.
    """
    module = __import__(f"app.agents.{slug}.prompts", fromlist=["SYSTEM"])
    assert not has_dash(module.SYSTEM), slug


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # A spaced dash is standing in for a comma.
        ("We cut costs — by a lot — last year.", "We cut costs, by a lot, last year."),
        # A numeric range is a hyphen.
        ("Processing takes 2–3 days.", "Processing takes 2-3 days."),
        # A tight dash between words is a compound.
        ("A well—known problem.", "A well-known problem."),
        # Two hyphens are the same habit, typed differently.
        ("Fast delivery -- usually next day.", "Fast delivery, usually next day."),
        # A dash opening a line is a bullet.
        ("— A bullet line", "- A bullet line"),
        # Nothing to do.
        ("Nothing to change here.", "Nothing to change here."),
        # And it must not mangle a real hyphen.
        ("A first-party audience.", "A first-party audience."),
    ],
)
def test_the_punctuation_tells_are_removed(raw: str, expected: str):
    assert humanise(raw) == expected
    assert not has_dash(humanise(raw))


def test_no_dash_survives_whatever_shape_it_arrives_in():
    """The guarantee has to be absolute, not nearly: one dash on a published
    page is the thing the instruction exists to prevent."""
    nasty = "A—B – C —— D--E  —  F (— G)"
    assert not has_dash(humanise(nasty))


def test_empty_and_none_are_safe():
    assert humanise("") == ""
    assert humanise(None) is None  # type: ignore[arg-type]


def test_prose_inside_structured_data_is_cleaned_too():
    """The graph publishes to the page, so its values are where a reader
    actually sees the dash."""
    graph = {
        "@type": "FAQPage",
        "mainEntity": [
            {
                "name": "How fast — really?",
                "acceptedAnswer": {"text": "Two to three days — usually."},
            }
        ],
        "count": 3,
        "tags": ["one — two"],
    }
    cleaned = humanise_json(graph)
    assert cleaned["mainEntity"][0]["name"] == "How fast, really?"
    assert cleaned["mainEntity"][0]["acceptedAnswer"]["text"] == (
        "Two to three days, usually."
    )
    assert cleaned["tags"] == ["one, two"]
    # Non-strings pass through untouched.
    assert cleaned["count"] == 3


@pytest.mark.parametrize(
    "phrase",
    [
        "in today's fast-paced",
        "ever-evolving landscape",
        "it's worth noting",
        "delve into",
        "seamless",
        "game-changing",
        "testament to",
    ],
)
def test_the_style_names_the_phrases_it_bans(phrase: str):
    """Named in the prompt rather than stripped afterwards: replacing a word
    changes what the copy claims, which is not a mechanical edit."""
    assert phrase in HOUSE_STYLE.lower()


def test_the_sanitiser_runs_where_the_copy_is_captured():
    """At the parse site, not the publish site. What is stored is what a
    reviewer reads and what eventually publishes, so cleaning on the way in
    means the two cannot disagree."""
    import inspect

    from app.agents.aeo_qa_injector import agent as aeo
    from app.agents.competitor_link_monitor import agent as clm
    from app.agents.digital_pr_outreach import agent as pr
    from app.agents.dynamic_creative_optimizer import specs
    from app.agents.knowledge_graph_schema import agent as kgs
    from app.agents.on_page_seo_sync import agent as onpage

    for module in (aeo, clm, pr, specs, onpage):
        assert "humanise(" in inspect.getsource(module), module.__name__
    assert "humanise_json(" in inspect.getsource(kgs)


def test_structure_is_never_rewritten_as_punctuation():
    """The regression this cost. An earlier version turned
    "<!-- automarket:aeo:start -->" into "<!, automarket:aeo:start, >" and
    broke the marker that lets the Q&A injector replace its own section
    instead of appending a second copy on every run.

    A rewritten page body can legitimately contain an HTML comment, a
    Markdown rule or front-matter fences.
    """
    from app.agents.aeo_qa_injector import prompts as aeo

    em = chr(0x2014)
    page = "\n".join([aeo.SECTION_START, f"Body {em} here.", aeo.SECTION_END])
    cleaned = humanise(page)
    assert aeo.SECTION_START in cleaned
    assert aeo.SECTION_END in cleaned
    assert "Body, here." in cleaned

    assert humanise("a\n\n----\n\nb").count("----") == 1
    assert humanise("---\ntitle: x\n---\n\nbody").startswith("---")


def test_a_real_hyphen_is_left_alone():
    """Over-correcting would mangle ordinary English."""
    for text in ("A first-party audience.", "state-of-the-art", "e-commerce"):
        assert humanise(text) == text
