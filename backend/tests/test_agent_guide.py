"""The plain-language agent guide must agree with the code it describes.

The console tells non-technical operators what each agent needs and what
will happen to its changes. Both answers are derived here, so these tests pin
them to the sources of truth: each agent's ``AgentSpec`` and
``policy.decide``.
"""
from __future__ import annotations

from app.agents.base.contracts import AgentAction
from app.agents.base.plain import GUIDES
from app.agents.base.policy import Decision, Guardrail, decide
from app.agents.base.registry import all_agents, get_agent
from app.connectors.base import registry as connector_registry
from app.services import agent_requirements as req


def test_every_agent_has_a_guide_and_no_guide_is_orphaned():
    from app.agents.base.plain import guide_for

    slugs = {agent.slug for agent in all_agents()}
    assert set(GUIDES) <= slugs
    assert all(guide_for(slug) is not None for slug in slugs)


def test_dependencies_name_real_agents():
    for guide in GUIDES.values():
        if guide.uses_data_from:
            assert get_agent(guide.uses_data_from) is not None


def test_needs_cover_every_declared_capability():
    for agent in all_agents():
        keys = {need.key for need in req.needs_for(agent, usable=frozenset())}
        for capability in agent.spec.required_capabilities:
            assert capability.value in keys
        if agent.spec.requires_llm:
            assert "ai_model" in keys


def test_an_agent_is_ready_exactly_when_preflight_would_find_its_connectors():
    every = frozenset(spec.slug for spec in connector_registry.all_specs())
    for agent in all_agents():
        assert all(need.satisfied for need in req.needs_for(agent, usable=every))
        needs = req.needs_for(agent, usable=frozenset())
        assert not needs or not any(need.satisfied for need in needs)


def test_a_read_only_crawler_does_not_make_the_rewrite_agent_ready():
    agent = get_agent("on_page_seo_sync")
    needs = req.needs_for(agent, usable=frozenset({"site_crawler", "openai"}))
    assert not all(need.satisfied for need in needs)


def test_options_only_list_integrations_that_have_the_capability():
    for agent in all_agents():
        for capability in agent.spec.required_capabilities:
            for option in req.options_for(capability, usable=frozenset(), enabled=None):
                assert capability in connector_registry.get_spec(option.slug).capabilities


def test_the_approval_sentence_matches_the_policy():
    """'Waits for approval' exactly when decide() queues the agent's change.

    Every agent proposes a single kind of action at its declared max_impact.
    """
    for agent in all_agents():
        action = AgentAction(kind="probe", title="probe", impact=agent.spec.max_impact)
        for guardrail in Guardrail:
            for autonomous in (True, False):
                decision, _ = decide(
                    action, guardrail=guardrail, agent_autonomous=autonomous, remaining_actions=5
                )
                sentence = req.approval_behaviour(
                    agent, guardrail=guardrail, autonomous=autonomous
                )
                waits = "waits for your approval" in sentence
                assert waits == (decision is Decision.QUEUE), (agent.slug, guardrail, autonomous)


def test_integrations_list_the_agents_they_serve():
    assert ("on_page_seo_sync", "On-Page SEO Sync") in req.agents_using("wordpress")
    # The competitor monitor sends its draft counter-pitches to the team
    # through the same outreach sender, so Slack serves it too.
    assert [slug for slug, _ in req.agents_using("slack")] == [
        "digital_pr_outreach",
        "competitor_link_monitor",
    ]
    # Optional use counts too: the auditor measures speed when it can.
    assert [slug for slug, _ in req.agents_using("pagespeed_insights")] == ["technical_seo_auditor"]
    assert req.connector_abilities("site_crawler") == [
        "Find the pages on your website",
        "Read your page content",
    ]
