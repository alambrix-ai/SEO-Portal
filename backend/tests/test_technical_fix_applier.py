"""Technical fix transforms, agent wiring, and approval-gated apply."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.connectors.base.connector import Capability
from app.db.base import Base, new_id, utcnow
from app.models.approval import ApprovalItem, ApprovalStatus
from app.models.seo import SeoIssue, SeoPage, PageStatus
from app.models.seo_fix import SeoIssueFixProposal
from app.services import technical_fixes as fixes
from tests.test_seo_pipeline_fixes import TENANT, FakeCms, PlainCipher, make_ctx


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "CHAR(36)"


def test_shorten_title_respects_word_boundary() -> None:
    long = "How to choose the best widget platform for enterprise teams in 2026"
    assert len(long) > fixes.TITLE_MAX_CHARS
    short = fixes.shorten_title(long)
    assert len(short) <= fixes.TITLE_MAX_CHARS
    assert not short.endswith("for")


def test_unlink_broken_keeps_anchor_text() -> None:
    body = 'See <a href="/gone">our guide</a> and [more](/gone) please.'
    out = fixes.unlink_broken(body, target="/gone")
    assert 'href="/gone"' not in out
    assert "our guide" in out
    assert "more" in out
    assert "](/gone)" not in out


def test_ensure_meta_and_canonical_on_html() -> None:
    body = "<html><head></head><body><p>Hello world about widgets shipping fast today.</p></body></html>"
    with_meta = fixes.ensure_meta_description(body, "Widgets that ship fast for teams.")
    assert 'name="description"' in with_meta
    with_canon = fixes.ensure_canonical(with_meta, "https://acme.test/guide")
    assert 'rel="canonical"' in with_canon


def test_ensure_h1_on_markdown() -> None:
    body = "Intro paragraph about widgets.\n"
    out = fixes.ensure_h1(body, "Widget Guide")
    assert out.startswith("# Widget Guide")


def test_validate_payload_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        fixes.validate_payload(
            {"fix_kind": "thin_content", "page_id": "a", "issue_id": "b"}
        )


def test_agent_registered_and_table_under_rls() -> None:
    from app.agents.base.registry import get_agent
    from app.db.rls import TENANT_SCOPED_TABLES

    agent = get_agent("technical_fix_applier")
    assert agent is not None
    assert agent.spec.max_impact.value == "high"
    assert "seo_issue_fix_proposals" in TENANT_SCOPED_TABLES


@pytest.fixture
def fix_db():  # noqa: ANN201
    engine = create_engine("sqlite://")
    tables = [
        SeoPage.__table__,
        SeoIssue.__table__,
        SeoIssueFixProposal.__table__,
        ApprovalItem.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _page(db: Session, *, body: str, title: str = "Guide") -> SeoPage:
    page = SeoPage(
        id=new_id(),
        tenant_id=TENANT,
        url="https://acme.test/guide",
        title=title,
        cms_id="1",
        cms_slug="wordpress",
        body_encrypted=body,
        status=PageStatus.LIVE.value,
        schema_types=[],
        missing_topics=[],
        target_keywords=[],
    )
    db.add(page)
    db.flush()
    return page


def _issue(db: Session, page: SeoPage, *, kind: str, target: str = "") -> SeoIssue:
    issue = SeoIssue(
        id=new_id(),
        tenant_id=TENANT,
        page_id=page.id,
        url=page.url,
        kind=kind,
        severity="high" if kind == "broken_internal_link" else "medium",
        target=target,
        summary=kind,
        recommendation="fix it",
        evidence={"target": target} if target else {},
        status="open",
        first_seen_at=utcnow(),
        last_seen_at=utcnow(),
    )
    db.add(issue)
    db.flush()
    return issue


def test_agent_proposes_high_impact_title_fix(fix_db: Session) -> None:
    from app.agents.technical_fix_applier.agent import AGENT

    title = "A very long title that definitely exceeds sixty characters for SEO"
    page = _page(fix_db, body="<p>Enough body copy about widgets for a real page.</p>", title=title)
    _issue(fix_db, page, kind="title_too_long")

    class Cms(FakeCms):
        def supports(self, capability: Capability) -> bool:
            return capability in {
                Capability.LIST_PAGES,
                Capability.READ_PAGE,
                Capability.WRITE_PAGE,
            }

    ctx = make_ctx(
        fix_db,
        slug="technical_fix_applier",
        connectors={"wordpress": Cms()},
        domain="acme.test",
    )
    result = AGENT.run(ctx)
    fix_db.flush()

    assert result.skipped is False
    assert len(result.actions) == 1
    action = result.actions[0]
    assert action.impact.value == "high"
    assert action.target_kind == "seo_issue_fix"
    assert action.payload["fix_kind"] == "title_too_long"
    assert len(action.payload["proposed_title"]) <= fixes.TITLE_MAX_CHARS
    proposals = list(fix_db.execute(select(SeoIssueFixProposal)).scalars())
    assert len(proposals) == 1
    assert proposals[0].status == "proposed"


def test_applier_is_idempotent_for_broken_link(fix_db: Session) -> None:
    from types import SimpleNamespace

    from app.agents.base.context import AgentContext
    from app.agents.technical_fix_applier import agent as fix_mod
    from app.models.agent import AgentRecord

    body = 'Read <a href="/missing">this</a> next.'
    page = _page(fix_db, body=body)
    issue = _issue(fix_db, page, kind="broken_internal_link", target="/missing")

    writes: list[tuple[str, str]] = []

    class TrackingCms(FakeCms):
        def write_page(
            self,
            remote_id: str,
            *,
            body: str,
            title: str | None = None,
            meta_description: str | None = None,
        ) -> bool:
            writes.append((remote_id, body))
            self.live[remote_id] = body
            return True

        def supports(self, capability: Capability) -> bool:
            return capability is Capability.WRITE_PAGE

    cms = TrackingCms(live={"1": body})

    record = AgentRecord(
        slug="technical_fix_applier",
        name="Technical Fix Applier",
        category="seo",
        scope="",
        settings={},
        max_actions_per_day=20,
        actions_today=0,
        actions_today_date="",
    )
    org = SimpleNamespace(id=TENANT, primary_domain="acme.test", name="Acme", settings={})
    ctx = AgentContext(
        db=fix_db,
        org=org,  # type: ignore[arg-type]
        record=record,
        cipher=PlainCipher(),  # type: ignore[arg-type]
        llm=SimpleNamespace(),  # type: ignore[arg-type]
        connectors={"wordpress": cms},
        connected_slugs=frozenset({"wordpress"}),
    )
    payload = {
        "fix_kind": "broken_internal_link",
        "issue_id": issue.id,
        "page_id": page.id,
        "url": page.url,
        "target": "/missing",
        "title": page.title,
        "proposed_title": None,
        "proposed_meta": None,
        "proposed_body": fixes.unlink_broken(body, target="/missing"),
    }
    fix_mod._apply_issue_fix(
        fix_db, org, issue.id, cipher=PlainCipher(), ctx=ctx, payload=payload  # type: ignore[arg-type]
    )
    assert len(writes) == 1
    assert 'href="/missing"' not in writes[0][1]
    assert fix_db.get(SeoIssue, issue.id).status == "fixed"

    # Second apply: issue already fixed; still safe if called again with same payload
    # Re-open to simulate idempotent body rewrite when already unlinked.
    issue.status = "open"
    page.body_encrypted = writes[0][1]
    fix_db.flush()
    fix_mod._apply_issue_fix(
        fix_db, org, issue.id, cipher=PlainCipher(), ctx=ctx, payload=payload  # type: ignore[arg-type]
    )
    assert len(writes) == 2
    assert writes[1][1] == writes[0][1]


def test_approval_type_technical_fix_exists() -> None:
    from app.models.approval import ApprovalType

    assert ApprovalType.TECHNICAL_FIX.value == "Technical Fix"
