"""Code-defined playbook catalogue and paused agent pack installation."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base.contracts import Impact
from app.agents.base.registry import get_agent
from app.db.base import utcnow
from app.models.agent import AgentRecord, AgentStatus
from app.models.playbooks import PlaybookInstallation
from app.models.workspace import Organization
from app.services.encryption import Ctx, OrgCipher


@dataclass(frozen=True, slots=True)
class PlaybookSpec:
    slug: str
    name: str
    description: str
    agent_slugs: tuple[str, ...]
    #: Soft daily caps applied on install (never raises above agent defaults).
    max_actions_per_day: dict[str, int]
    default_schedules: dict[str, str]


#: Catalogue is code-defined first — not a DB table.
PLAYBOOKS: dict[str, PlaybookSpec] = {
    "launch_seo": PlaybookSpec(
        slug="launch_seo",
        name="Launch SEO",
        description="Core on-page, technical, schema, and topic cluster agents.",
        agent_slugs=(
            "on_page_seo_sync",
            "technical_seo_auditor",
            "knowledge_graph_schema",
            "topic_cluster_planner",
        ),
        max_actions_per_day={
            "on_page_seo_sync": 10,
            "technical_seo_auditor": 10,
            "knowledge_graph_schema": 10,
            "topic_cluster_planner": 5,
        },
        default_schedules={},
    ),
    "local_pack": PlaybookSpec(
        slug="local_pack",
        name="Local pack",
        description="Multi-location rankings, review replies, and social listening.",
        agent_slugs=(
            "local_search_tracker",
            "multi_location_local_seo",
            "review_responder",
            "social_listening",
        ),
        max_actions_per_day={
            "review_responder": 10,
            "social_listening": 10,
        },
        default_schedules={},
    ),
}


def get_playbook(slug: str) -> PlaybookSpec | None:
    return PLAYBOOKS.get((slug or "").strip().lower())


def list_playbooks() -> list[PlaybookSpec]:
    return list(PLAYBOOKS.values())


def install_playbook(
    db: Session,
    org: Organization,
    slug: str,
    *,
    cipher: OrgCipher,
    settings: dict | None = None,
) -> PlaybookInstallation:
    """Ensure pack agents exist as PAUSED records; never auto-starts HIGH impact."""
    spec = get_playbook(slug)
    if spec is None:
        raise LookupError(f"Unknown playbook: {slug}")

    existing_agents = {
        row.slug: row
        for row in db.execute(
            select(AgentRecord).where(AgentRecord.tenant_id == org.id)
        ).scalars()
    }
    installed_slugs: list[str] = []
    for agent_slug in spec.agent_slugs:
        agent = get_agent(agent_slug)
        if agent is None:
            continue
        record = existing_agents.get(agent_slug)
        if record is None:
            record = AgentRecord(
                tenant_id=org.id,
                slug=agent.spec.slug,
                name=agent.spec.name,
                category=agent.spec.category.value,
                description=agent.spec.description,
                # Always paused — a human starts each agent after configuration.
                status=AgentStatus.PAUSED.value,
                autonomy=False
                if agent.spec.max_impact is Impact.HIGH
                else bool(getattr(org, "global_autonomy", True)),
                schedule=spec.default_schedules.get(
                    agent_slug, agent.spec.default_schedule
                ),
                max_actions_per_day=min(
                    agent.spec.default_max_actions_per_day,
                    spec.max_actions_per_day.get(
                        agent_slug, agent.spec.default_max_actions_per_day
                    ),
                ),
                metric_label="Playbook installed — configure to start",
                next_run_at=None,
                actions_today_date=utcnow().date().isoformat(),
                settings={},
            )
            db.add(record)
        else:
            # Refresh caps downward only; never unpause.
            cap = spec.max_actions_per_day.get(agent_slug)
            if cap is not None:
                record.max_actions_per_day = min(record.max_actions_per_day, cap)
            if agent.spec.max_impact is Impact.HIGH:
                # Installing a pack must not flip HIGH agents into autonomous mode.
                record.autonomy = False
            if record.status != AgentStatus.PAUSED.value and record.next_run_at is None:
                pass
        installed_slugs.append(agent_slug)

    row = db.execute(
        select(PlaybookInstallation).where(
            PlaybookInstallation.tenant_id == org.id,
            PlaybookInstallation.playbook_slug == spec.slug,
        )
    ).scalar_one_or_none()
    if row is None:
        row = PlaybookInstallation(
            tenant_id=org.id,
            playbook_slug=spec.slug,
            installed_at=utcnow(),
            status="installed",
        )
        db.add(row)
    row.settings_encrypted = cipher.encrypt_json(
        settings or {}, context=Ctx.PLAYBOOK_SETTINGS
    )
    row.agent_slugs = installed_slugs
    row.status = "installed"
    row.installed_at = utcnow()
    db.flush()
    return row
