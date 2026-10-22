"""One plain-English name for the approval setting, everywhere.

The setting is "Review before publishing", with three choices: "Always ask
me", "Ask me for big changes" and "Publish automatically". The stored values
(``human`` / ``hybrid`` / ``full``) do not change, so these tests pin the
words a person sees to the values the runner reads, and keep the old jargon
("guardrail", "Hybrid", "Human in the loop", "Full autonomy") out of copy.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.agents.base.policy import (
    GUARDRAIL_DETAIL,
    GUARDRAIL_LABELS,
    GUARDRAIL_NAMES,
    REVIEW_SETTING_NAME,
    Guardrail,
    resolve_guardrail,
)
from app.agents.base.registry import all_agents
from app.services import agent_requirements as req
from app.services.views import guardrail_options

EXPECTED_NAMES = {
    "human": "Always ask me",
    "hybrid": "Ask me for big changes",
    "full": "Publish automatically",
}

#: Words a non-technical marketer should never have to decode.
JARGON = re.compile(
    r"guardrail|hybrid|human[- ]in[- ]the[- ]loop|human-in-loop|full autonomy|autonomous",
    re.IGNORECASE,
)

PLAIN_TS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "plain.ts"


def test_the_setting_has_one_name():
    assert REVIEW_SETTING_NAME == "Review before publishing"


def test_api_values_are_unchanged():
    """Only words changed: the stored values the runner reads are the same."""
    assert {g.value for g in Guardrail} == {"full", "hybrid", "human"}
    assert set(GUARDRAIL_LABELS) == set(GUARDRAIL_NAMES) == set(GUARDRAIL_DETAIL)
    assert set(GUARDRAIL_NAMES) == set(EXPECTED_NAMES)


def test_each_choice_has_its_plain_name():
    assert GUARDRAIL_NAMES == EXPECTED_NAMES


def test_labels_lead_with_the_name_and_still_differ_from_it():
    for value, name in GUARDRAIL_NAMES.items():
        assert GUARDRAIL_LABELS[value].startswith(name)
        assert GUARDRAIL_LABELS[value] != name


@pytest.mark.parametrize("value", sorted(EXPECTED_NAMES))
def test_customer_facing_copy_has_no_jargon(value: str):
    for text in (GUARDRAIL_NAMES[value], GUARDRAIL_LABELS[value], GUARDRAIL_DETAIL[value]):
        assert not JARGON.search(text), text


def test_onboarding_options_carry_the_plain_names():
    options = {option["value"]: option for option in guardrail_options()}
    assert set(options) == set(EXPECTED_NAMES)
    for value, name in EXPECTED_NAMES.items():
        assert options[value]["name"] == name
        assert options[value]["detail"]


def test_approval_sentences_use_the_setting_name_not_jargon():
    """What the agent setup screen says about each agent's changes."""
    seen_setting = False
    for agent in all_agents():
        for guardrail in Guardrail:
            for autonomous in (True, False):
                sentence = req.approval_behaviour(
                    agent, guardrail=guardrail, autonomous=autonomous
                )
                assert not JARGON.search(sentence), (agent.slug, guardrail, sentence)
                if REVIEW_SETTING_NAME in sentence:
                    seen_setting = True
                    assert GUARDRAIL_NAMES[guardrail.value] in sentence
    assert seen_setting, "no sentence named the workspace setting"


def test_a_display_name_is_never_accepted_as_a_value():
    """Abuse case: posting the label instead of the value must not widen access.

    "Publish automatically" is not a stored value, so it falls back to the
    usual choice rather than turning on automatic publishing.
    """
    for name in EXPECTED_NAMES.values():
        assert resolve_guardrail(name) is Guardrail.HYBRID
    assert resolve_guardrail("FULL") is Guardrail.HYBRID
    assert resolve_guardrail("full ") is Guardrail.HYBRID
    assert resolve_guardrail("") is Guardrail.HYBRID


@pytest.mark.skipif(not PLAIN_TS.exists(), reason="frontend sources not present")
def test_frontend_names_match_the_server():
    """The console's own copy of the names must agree with the server's."""
    text = PLAIN_TS.read_text(encoding="utf-8")
    block = re.search(r"export const GUARDRAIL_NAME\b[^=]*=\s*\{(.*?)\}", text, re.DOTALL)
    assert block, "GUARDRAIL_NAME not found in plain.ts"
    pairs = dict(re.findall(r"(\w+):\s*'([^']*)'", block.group(1)))
    assert pairs == EXPECTED_NAMES
    assert f"'{REVIEW_SETTING_NAME}'" in text
