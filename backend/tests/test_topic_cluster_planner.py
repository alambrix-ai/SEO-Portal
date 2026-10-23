"""Topic clustering determinism, brief validation, and agent wiring."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.connectors.base.connector import Capability
from app.db.base import Base, new_id
from app.models.approval import ApprovalItem
from app.models.seo import PageStatus, SeoPage
from app.models.topic_cluster import ContentBrief, TopicCluster, TopicClusterPage
from app.services import topic_clusters as cluster_svc
from tests.test_seo_pipeline_fixes import TENANT, make_ctx


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "CHAR(36)"


def test_clustering_is_deterministic() -> None:
    queries = [
        "industrial widget pricing",
        "buy industrial widgets",
        "widget bulk orders",
        "careers in finance london",
        "finance jobs london",
    ]
    pages = [
        cluster_svc.PageRef(
            id="p1",
            url="https://acme.test/widgets",
            title="Industrial widgets",
            keywords=("industrial widgets",),
        ),
        cluster_svc.PageRef(
            id="p2",
            url="https://acme.test/careers",
            title="Careers",
            keywords=("jobs",),
        ),
    ]
    a = cluster_svc.cluster_queries(queries, pages)
    b = cluster_svc.cluster_queries(queries, pages)
    assert [(c.name, tuple(c.seed_terms), c.gap_score) for c in a] == [
        (c.name, tuple(c.seed_terms), c.gap_score) for c in b
    ]
    assert len(a) >= 2


def test_brief_outline_schema() -> None:
    outline = cluster_svc.brief_outline(
        cluster_name="Widget pricing",
        seed_terms=["widget pricing", "bulk widgets"],
        existing_titles=["About us"],
    )
    assert outline["title"]
    assert "widget pricing" in outline["target_keywords"]
    data = cluster_svc.validate_brief_payload(
        {
            "brief_id": "b1",
            "cluster_id": "c1",
            "title": outline["title"],
            "body": outline["body"],
            "target_keywords": outline["target_keywords"],
        }
    )
    assert data["brief_id"] == "b1"


def test_validate_brief_rejects_empty() -> None:
    with pytest.raises(ValueError):
        cluster_svc.validate_brief_payload({"title": "", "body": "", "brief_id": ""})


def test_agent_registered_and_rls() -> None:
    from app.agents.base.registry import get_agent
    from app.db.rls import TENANT_SCOPED_TABLES

    agent = get_agent("topic_cluster_planner")
    assert agent is not None
    assert "topic_clusters" in TENANT_SCOPED_TABLES
    assert "content_briefs" in TENANT_SCOPED_TABLES


@pytest.fixture
def cluster_db():  # noqa: ANN201
    engine = create_engine("sqlite://")
    tables = [
        SeoPage.__table__,
        TopicCluster.__table__,
        TopicClusterPage.__table__,
        ContentBrief.__table__,
        ApprovalItem.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_agent_proposes_brief_for_gapped_cluster(cluster_db: Session) -> None:
    from app.agents.topic_cluster_planner.agent import AGENT

    # Only a loosely related careers page — widget queries should gap high.
    page = SeoPage(
        id=new_id(),
        tenant_id=TENANT,
        url="https://acme.test/careers",
        title="Careers",
        cms_id="1",
        cms_slug="wordpress",
        body_encrypted="Join our team.",
        status=PageStatus.LIVE.value,
        schema_types=[],
        missing_topics=[],
        target_keywords=["jobs"],
    )
    cluster_db.add(page)
    cluster_db.flush()

    class FakeGsc:
        slug = "search_console"
        name = "Search Console"

        def supports(self, capability: Capability) -> bool:
            return capability is Capability.READ_SEARCH_PERFORMANCE

        def read_search_performance(self, *, days: int = 28) -> dict:
            return {
                "top_queries": [
                    {"query": "industrial widget pricing"},
                    {"query": "buy industrial widgets"},
                    {"query": "widget bulk orders"},
                ]
            }

    ctx = make_ctx(
        cluster_db,
        slug="topic_cluster_planner",
        answer={},  # polish returns empty → keep deterministic outline
        connectors={"search_console": FakeGsc()},
        domain="acme.test",
    )
    result = AGENT.run(ctx)
    cluster_db.flush()

    assert result.skipped is False
    clusters = list(cluster_db.execute(select(TopicCluster)).scalars())
    assert clusters
    assert any(c.gap_score >= 40 for c in clusters)
    assert len(result.actions) >= 1
    action = result.actions[0]
    assert action.impact.value == "high"
    assert action.payload["body"]
    briefs = list(cluster_db.execute(select(ContentBrief)).scalars())
    assert len(briefs) >= 1
    assert briefs[0].status == "proposed"
