"""An agent's saved AI model that is no longer connected falls back to a connected one."""
from __future__ import annotations

from app.agents.base import registry
from app.llm.from_connector import effective_llm
from app.services.agent_requirements import needs_for


def test_chosen_model_is_used_when_connected() -> None:
    assert effective_llm("openai", {"openai", "anthropic_claude"}) == ("openai", False)


def test_stale_choice_falls_back_to_a_connected_model() -> None:
    # The Alambrix case: agents saved to OpenAI, only Claude connected.
    assert effective_llm("openai", {"anthropic_claude", "site_crawler"}) == ("anthropic_claude", True)


def test_no_choice_uses_first_connected_model() -> None:
    assert effective_llm("", {"google_gemini"}) == ("google_gemini", False)


def test_nothing_connected() -> None:
    assert effective_llm("openai", {"site_crawler"}) == ("", True)
    assert effective_llm("", set()) == ("", False)


def test_non_llm_connectors_are_never_picked() -> None:
    assert effective_llm("wordpress", {"wordpress"}) == ("", True)


def test_setup_screen_counts_claude_and_names_only_the_model_in_use() -> None:
    agent = registry.get_agent("on_page_seo_sync")
    needs = needs_for(agent, usable=frozenset({"site_crawler", "anthropic_claude"}), llm_choice="openai")
    ai = next(n for n in needs if n.key == "ai_model")
    assert ai.satisfied
    assert "anthropic_claude" in {o.slug for o in ai.options}
    # Every model is equal: only the one in use is named, never the old choice.
    assert ai.note == "Uses Anthropic Claude."
    assert "OpenAI" not in ai.note


def test_setup_screen_lists_anthropic_when_nothing_is_connected() -> None:
    agent = registry.get_agent("on_page_seo_sync")
    ai = next(n for n in needs_for(agent, usable=frozenset(), llm_choice="") if n.key == "ai_model")
    assert not ai.satisfied and ai.note == ""
    assert {"openai", "anthropic_claude", "google_gemini"} <= {o.slug for o in ai.options}
