"""Playbook catalogue and paused-agent installation."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from types import SimpleNamespace

from app.db.base import Base
from app.models.agent import AgentRecord, AgentStatus
from app.models.approval import ApprovalItem
from app.models.playbooks import PlaybookInstallation
from app.services import playbooks as playbook_svc
from app.services.encryption import Ctx
from tests.test_seo_pipeline_fixes import TENANT, PlainCipher, make_ctx


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "CHAR(36)"


def test_catalogue_has_launch_seo() -> None:
    spec = playbook_svc.get_playbook("launch_seo")
    assert spec is not None
    assert "on_page_seo_sync" in spec.agent_slugs


def test_agent_registered_and_rls() -> None:
    from app.agents.base.registry import get_agent
    from app.db.rls import TENANT_SCOPED_TABLES

    assert get_agent("playbook_installer") is not None
    assert "playbook_installations" in TENANT_SCOPED_TABLES


@pytest.fixture
def playbook_db():  # noqa: ANN201
    engine = create_engine("sqlite://")
    tables = [AgentRecord.__table__, PlaybookInstallation.__table__, ApprovalItem.__table__]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_install_keeps_agents_paused(playbook_db: Session) -> None:
    org = SimpleNamespace(id=TENANT, global_autonomy=True, name="Acme")
    cipher = PlainCipher()
    row = playbook_svc.install_playbook(
        playbook_db, org, "launch_seo", cipher=cipher, settings={"note": "test"}
    )
    playbook_db.flush()
    assert row.status == "installed"
    agents = list(
        playbook_db.execute(
            select(AgentRecord).where(AgentRecord.tenant_id == TENANT)
        ).scalars()
    )
    assert agents
    assert all(a.status == AgentStatus.PAUSED.value for a in agents)
    # HIGH-impact agents in the pack must not start autonomous.
    high = [a for a in agents if a.slug in {"on_page_seo_sync", "knowledge_graph_schema"}]
    assert high
    assert all(a.autonomy is False for a in high)


def test_agent_proposes_high_install(playbook_db: Session) -> None:
    from app.agents.playbook_installer.agent import AGENT

    ctx = make_ctx(
        playbook_db, slug="playbook_installer", domain="example.com", scope="local_pack"
    )
    result = AGENT.run(ctx)
    assert result.skipped is False
    assert len(result.actions) == 1
    assert result.actions[0].impact.value == "high"
    assert result.actions[0].payload["playbook_slug"] == "local_pack"
