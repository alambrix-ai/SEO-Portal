"""RBAC matrix and autonomy-policy tests.

Both are pure decision tables, and both decide something consequential — who
can see what, and whether an agent publishes or waits for a person. Worth
pinning down precisely.
"""
from __future__ import annotations

import pytest

from app.agents.base.contracts import AgentAction, Impact
from app.agents.base.policy import Decision, Guardrail, decide, resolve_guardrail
from app.core.rbac import Access, Module, Role, access_map, can_view, can_write


# ── RBAC ───────────────────────────────────────────────────────────────────
def test_every_role_has_every_module():
    """A missing cell would raise a KeyError at request time."""
    for role in Role:
        mapped = access_map(role)
        assert set(mapped) == {m.value for m in Module}


def test_admin_has_full_access_everywhere():
    for module in Module:
        assert can_write(Role.ADMIN, module)


def test_client_viewer_is_confined_to_dashboard_and_reports():
    for module in Module:
        if module in (Module.DASHBOARD, Module.REPORTS):
            assert can_view(Role.CLIENT, module)
        else:
            assert not can_view(Role.CLIENT, module)
    # And can write nothing at all.
    assert not any(can_write(Role.CLIENT, m) for m in Module)


def test_specialists_are_scoped_to_their_own_area():
    # An SEO specialist owns SEO and off-page, and cannot touch ads.
    assert can_write(Role.SEO, Module.SEO)
    assert can_write(Role.SEO, Module.OFFPAGE)
    assert not can_view(Role.SEO, Module.ADS)

    # An ads specialist is the mirror image.
    assert can_write(Role.ADS, Module.ADS)
    assert not can_view(Role.ADS, Module.SEO)
    assert not can_view(Role.ADS, Module.OFFPAGE)


def test_approver_can_decide_but_not_author():
    assert can_write(Role.APPROVER, Module.APPROVALS)
    # Sees the work to judge it, cannot change it directly.
    assert can_view(Role.APPROVER, Module.SEO)
    assert not can_write(Role.APPROVER, Module.SEO)
    assert not can_write(Role.APPROVER, Module.ADS)


def test_only_admin_manages_connectors_and_admin():
    for role in (Role.MANAGER, Role.SEO, Role.ADS, Role.APPROVER):
        assert not can_write(role, Module.CONNECTORS)
    assert not can_write(Role.MANAGER, Module.ADMIN)
    assert can_write(Role.ADMIN, Module.ADMIN)


def test_no_role_can_write_what_it_cannot_view():
    """Write access without view access would be incoherent."""
    for role in Role:
        for module in Module:
            if can_write(role, module):
                assert can_view(role, module), f"{role}/{module}"


# ── Autonomy policy ────────────────────────────────────────────────────────
def _action(impact: Impact) -> AgentAction:
    return AgentAction(kind="test", title="Test action", impact=impact)


def test_full_autonomy_applies_both_impacts():
    for impact in (Impact.LOW, Impact.HIGH):
        decision, _ = decide(
            _action(impact),
            guardrail=Guardrail.FULL,
            agent_autonomous=True,
            remaining_actions=10,
        )
        assert decision is Decision.APPLY


def test_hybrid_is_the_line_between_low_and_high_impact():
    """The hybrid guardrail's entire meaning: draft freely, publish carefully."""
    low, _ = decide(
        _action(Impact.LOW),
        guardrail=Guardrail.HYBRID,
        agent_autonomous=True,
        remaining_actions=10,
    )
    high, reason = decide(
        _action(Impact.HIGH),
        guardrail=Guardrail.HYBRID,
        agent_autonomous=True,
        remaining_actions=10,
    )
    assert low is Decision.APPLY
    assert high is Decision.QUEUE
    assert "high-impact" in reason


def test_human_guardrail_queues_everything():
    for impact in (Impact.LOW, Impact.HIGH):
        decision, _ = decide(
            _action(impact),
            guardrail=Guardrail.HUMAN,
            agent_autonomous=True,
            remaining_actions=10,
        )
        assert decision is Decision.QUEUE


def test_per_agent_switch_overrides_a_permissive_guardrail():
    """Turning one agent to human review must not be undone by the default."""
    decision, reason = decide(
        _action(Impact.LOW),
        guardrail=Guardrail.FULL,
        agent_autonomous=False,
        remaining_actions=10,
    )
    assert decision is Decision.QUEUE
    assert "agent is set to human-in-the-loop" in reason


def test_daily_cap_blocks_rather_than_queues():
    """The cap is about rate, so work is re-proposed later, not piled up."""
    decision, reason = decide(
        _action(Impact.LOW),
        guardrail=Guardrail.FULL,
        agent_autonomous=True,
        remaining_actions=0,
    )
    assert decision is Decision.BLOCK
    assert "daily autonomous action limit" in reason


def test_cap_does_not_apply_to_queued_actions():
    """A full queue is a human's problem, not a rate-limit one.

    An agent under review should keep surfacing work even after the daily
    autonomous cap is spent, because nothing is being executed.
    """
    decision, _ = decide(
        _action(Impact.HIGH),
        guardrail=Guardrail.HYBRID,
        agent_autonomous=True,
        remaining_actions=0,
    )
    assert decision is Decision.QUEUE


def test_unknown_guardrail_falls_back_to_hybrid():
    """A bad stored value must not silently become full autonomy."""
    assert resolve_guardrail("nonsense") is Guardrail.HYBRID
    assert resolve_guardrail(None) is Guardrail.HYBRID
    assert resolve_guardrail("full") is Guardrail.FULL
