"""Provisioning a new organisation.

Called once at sign-up. It installs the fleet and the connector catalogue for
the workspace and nothing else: a real customer starts with an empty console
that fills as they connect their systems and the agents run. No invented pages,
backlinks or spend.

Because the agent and connector rows are generated from the registries, an
integration added to the codebase is available to every organisation created
afterwards, and :func:`sync_catalog` back-fills the ones created before.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base.registry import all_agents
from app.connectors.base import registry as connector_registry
from app.core.logging import get_logger
from app.db.base import utcnow
from app.models.ads import AdChannel, BudgetAllocation
from app.models.agent import AgentRecord, AgentStatus
from app.models.connector import ConnectorRecord
from app.models.workspace import OnboardingState, Organization
from app.services import audit

log = get_logger(__name__)

# An even split until the predictive engine has CAC data to reallocate from.
_INITIAL_SHARE = {
    AdChannel.GOOGLE.value: 20,
    AdChannel.META.value: 20,
    AdChannel.LINKEDIN.value: 20,
    AdChannel.TIKTOK.value: 20,
    AdChannel.DSP.value: 20,
}


def provision_organization(db: Session, org: Organization) -> None:
    """Install agents, connectors, budget rows and the onboarding wizard."""
    install_agents(db, org)
    install_connectors(db, org)
    install_budgets(db, org)
    install_onboarding(db, org)
    audit.record_system_action(
        db,
        tenant_id=org.id,
        action="provisioned the workspace with its agent fleet and connector catalogue",
        module="admin",
    )
    log.info("Provisioned organisation %s", org.slug)


# ── Agents ─────────────────────────────────────────────────────────────────
def install_agents(db: Session, org: Organization) -> int:
    existing = {
        slug
        for (slug,) in db.execute(
            select(AgentRecord.slug).where(AgentRecord.tenant_id == org.id)
        )
    }
    added = 0
    now = utcnow()

    for agent in all_agents():
        if agent.spec.slug in existing:
            continue
        from app.models.portal import FeatureKind
        from app.services import portal_features

        if not portal_features.is_enabled(db, FeatureKind.AGENT, agent.spec.slug):
            continue
        spec = agent.spec
        db.add(
            AgentRecord(
                tenant_id=org.id,
                slug=spec.slug,
                name=spec.name,
                category=spec.category.value,
                description=spec.description,
                # Installed paused, and deliberately so. These agents rewrite
                # live pages, mail strangers and move ad spend; none of that
                # should begin because an account was created. A person starts
                # the fleet — from "Launch workspace" at the end of onboarding,
                # or one agent at a time from its card — and that act is what
                # makes the workspace live.
                status=AgentStatus.PAUSED.value,
                autonomy=org.global_autonomy,
                schedule=spec.default_schedule,
                max_actions_per_day=spec.default_max_actions_per_day,
                metric_label="Not started yet",
                # No schedule until it is started, so the claim query in the
                # scheduler never sees it.
                next_run_at=None,
                actions_today_date=now.date().isoformat(),
            )
        )
        added += 1

    if added:
        db.flush()
    return added


# ── Connectors ─────────────────────────────────────────────────────────────
def install_connectors(db: Session, org: Organization) -> int:
    existing = {
        slug
        for (slug,) in db.execute(
            select(ConnectorRecord.slug).where(ConnectorRecord.tenant_id == org.id)
        )
    }
    added = 0

    for spec in connector_registry.all_specs():
        if spec.slug in existing:
            continue
        from app.models.portal import FeatureKind
        from app.services import portal_features

        if not portal_features.is_enabled(db, FeatureKind.CONNECTOR, spec.slug):
            continue
        db.add(
            ConnectorRecord(
                tenant_id=org.id,
                slug=spec.slug,
                name=spec.name,
                category=spec.category,
                connected=False,
            )
        )
        added += 1

    if added:
        db.flush()
    return added


# ── Budgets ────────────────────────────────────────────────────────────────
def install_budgets(db: Session, org: Organization) -> int:
    existing = {
        channel
        for (channel,) in db.execute(
            select(BudgetAllocation.channel).where(BudgetAllocation.tenant_id == org.id)
        )
    }
    added = 0
    for channel, share in _INITIAL_SHARE.items():
        if channel in existing:
            continue
        db.add(
            BudgetAllocation(
                tenant_id=org.id, channel=channel, percent=share, cac=0.0, spend=0.0
            )
        )
        added += 1
    if added:
        db.flush()
    return added


# ── Onboarding ─────────────────────────────────────────────────────────────
def install_onboarding(db: Session, org: Organization) -> OnboardingState:
    state = db.execute(
        select(OnboardingState).where(OnboardingState.tenant_id == org.id)
    ).scalar_one_or_none()
    if state is not None:
        return state
    state = OnboardingState(
        tenant_id=org.id,
        step=0,
        domain=org.primary_domain,
        # Unset, so the wizard's content-system question is actually
        # asked. Defaulting to WordPress meant a workspace that never
        # answered it was recorded as WordPress.
        cms="",
        # Left empty. This used to seed google/meta/linkedin as unticked
        # boxes for a wizard step that wrote back into it and nothing read;
        # the step queries the connector records now. The column is kept
        # rather than migrated away — an empty JSONB costs nothing and a
        # migration for it buys nothing.
        ad_accounts={},
        guardrail="hybrid",
        completed=False,
    )
    db.add(state)
    db.flush()
    return state


# ── Back-fill ──────────────────────────────────────────────────────────────
def sync_catalog(db: Session, org: Organization) -> dict[str, int]:
    """Add agents and connectors registered since this organisation was created.

    Safe to run on every deploy; it only inserts what is missing.
    """
    result = {
        "agents_added": install_agents(db, org),
        "connectors_added": install_connectors(db, org),
        "budgets_added": install_budgets(db, org),
    }
    if any(result.values()):
        audit.record_system_action(
            db,
            tenant_id=org.id,
            action=(
                f"added {result['agents_added']} agents and "
                f"{result['connectors_added']} connectors from the catalogue"
            ),
            module="admin",
        )
    return result


def sync_all_organizations(db: Session) -> dict[str, int]:
    """Back-fill the catalogue into every workspace.

    Goes through :func:`run_per_organization` because these are inserts into
    row-level-security tables: without that organisation pinned, the policy's
    ``WITH CHECK`` rejects the row outright, and a single commit at the end
    would attribute every workspace's inserts to whichever pin was set last.
    """
    from app.db.session import run_per_organization

    totals = {"agents_added": 0, "connectors_added": 0, "budgets_added": 0}

    def sync_one(session: Session, tenant_id: str) -> dict[str, int]:
        org = session.get(Organization, tenant_id)
        if org is None:
            return {"agents_added": 0, "connectors_added": 0, "budgets_added": 0}
        return sync_catalog(session, org)

    for result in run_per_organization(db, sync_one, label="Catalogue sync"):
        for key, value in result.items():
            totals[key] += value
    return totals
