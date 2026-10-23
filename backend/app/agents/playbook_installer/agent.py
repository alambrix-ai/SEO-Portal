"""Propose installing a playbook pack through the approval gate."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.agents.base.context import AgentContext
from app.agents.base.contracts import (
    AgentAction,
    AgentCategory,
    AgentResult,
    AgentSpec,
    BaseAgent,
    Impact,
)
from app.models.approval import ApprovalItem, ApprovalType
from app.models.playbooks import PlaybookInstallation
from app.models.workspace import Organization
from app.services import playbooks as playbook_svc
from app.services.approvals import register_applier, register_rejecter
from app.services.encryption import OrgCipher

TARGET_KIND = "playbook_installation"
SLUG = "playbook_installer"


class PlaybookInstallerAgent(BaseAgent):
    spec = AgentSpec(
        slug=SLUG,
        name="Playbook Installer",
        category=AgentCategory.CONTENT,
        description=(
            "Installs one-click agent packs (Launch SEO, Local pack). Agents are "
            "created paused; HIGH-impact agents stay non-autonomous until configured."
        ),
        default_interval=timedelta(days=30),
        default_schedule="Manual",
        required_capabilities=(),
        max_impact=Impact.HIGH,
        default_max_actions_per_day=3,
        scope_placeholder="Playbook slug: launch_seo or local_pack",
        requires_llm=False,
        on_demand=True,
    )

    def run(self, ctx: AgentContext) -> AgentResult:
        slug = (ctx.scope or "").strip().split(",")[0].strip().lower()
        spec = playbook_svc.get_playbook(slug)
        if spec is None:
            known = ", ".join(p.slug for p in playbook_svc.list_playbooks())
            return AgentResult.skip(f"Unknown playbook — choose one of: {known}")

        action = AgentAction(
            kind="install_playbook",
            title=f'Install playbook: "{spec.name}"',
            impact=Impact.HIGH,
            approval_type=ApprovalType.PLAYBOOK_INSTALL.value,
            target_kind=TARGET_KIND,
            target_id=spec.slug,
            payload={
                "playbook_slug": spec.slug,
                "agent_slugs": list(spec.agent_slugs),
                "name": spec.name,
            },
            apply=_make_installer(spec.slug),
            audit=f"installed playbook {spec.slug}",
            metrics={"playbooks_installed": 1},
        )
        return AgentResult(
            summary=f"Proposed installing {spec.name} ({len(spec.agent_slugs)} agents, paused)",
            actions=[action],
            metric_label="Playbook install proposed",
            metrics={"playbook_agents": len(spec.agent_slugs)},
        )


def _make_installer(slug: str):  # noqa: ANN202
    def apply(ctx: AgentContext) -> None:
        playbook_svc.install_playbook(ctx.db, ctx.org, slug, cipher=ctx.cipher)

    return apply


@register_applier(TARGET_KIND)
def apply_playbook(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    slug = str((payload or {}).get("playbook_slug") or item.target_id or "")
    if not slug:
        raise ValueError("Missing playbook slug")
    cipher = OrgCipher.for_org(org)
    row = playbook_svc.install_playbook(db, org, slug, cipher=cipher)
    row.approval_item_id = item.id
    db.flush()


@register_rejecter(TARGET_KIND)
def reject_playbook(
    db: Session, org: Organization, item: ApprovalItem, payload: dict
) -> None:
    # Nothing to roll back — install only happens on approve.
    return


AGENT = PlaybookInstallerAgent()
