"""The SEO content agents' bookkeeping: what they pick, what they count, and
what they write back.

These run the agents' own queries against an in-memory SQLite database. That
is deliberately narrow: the conftest is right that SQLite cannot stand in for
row-level security or the metrics upsert, and nothing here touches either.
What these tests guard is selection and state — which pages a run takes on,
whether a page is stranded as "awaiting approval", whether a pair or a
citation is counted twice — and that logic lives in ordinary WHERE clauses
both databases evaluate the same way. Without a test database the real-schema
tests skip, and these defects were all invisible.
"""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.agents.aeo_qa_injector import agent as aeo
from app.agents.aeo_qa_injector import prompts as aeo_prompts
from app.agents.base import pages as page_rules
from app.agents.base.context import AgentContext
from app.agents.knowledge_graph_schema import agent as kg
from app.agents.on_page_seo_sync import agent as onpage
from app.agents.technical_seo_auditor import agent as auditor
from app.connectors.base.connector import Capability
from app.connectors.base.interfaces import CitationHit, RemotePage
from app.core.exceptions import ConnectorError
from app.db.base import Base, new_id, utcnow
from app.llm.base import LLMUsage
from app.models.agent import AgentRecord
from app.models.approval import ApprovalItem, ApprovalStatus
from app.models.seo import AeoQaPair, PageStatus, SchemaPatch, SeoIssue, SeoPage
from app.services import approvals


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    # Text affinity: SQLite's default for an unknown "UUID" type is NUMERIC,
    # which turns an all-digit hex id into a float.
    return "CHAR(36)"


TENANT = "11111111-1111-4111-8111-111111111111"
_TABLES = [
    SeoPage.__table__,
    AeoQaPair.__table__,
    SchemaPatch.__table__,
    SeoIssue.__table__,
    ApprovalItem.__table__,
]


@pytest.fixture
def db():  # noqa: ANN201
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=_TABLES)
    with Session(engine) as session:
        yield session
    engine.dispose()


# ── Doubles ────────────────────────────────────────────────────────────────
class PlainCipher:
    """The cipher's interface without the cryptography."""

    def encrypt(self, value, *, context):  # noqa: ANN001, ANN201
        return value or ""

    def decrypt(self, token, *, context, default=""):  # noqa: ANN001, ANN201
        return token or default

    def encrypt_json(self, value, *, context):  # noqa: ANN001, ANN201
        return json.dumps(value)

    def decrypt_json(self, token, *, context, default=None):  # noqa: ANN001, ANN201
        return json.loads(token) if token else default


class ScriptedLLM:
    """Answers every prompt from one function, and remembers the prompts."""

    def __init__(self, answer) -> None:  # noqa: ANN001
        self.answer = answer
        self.prompts: list[str] = []

    def complete_json(self, prompt, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.prompts.append(prompt)
        value = self.answer(prompt) if callable(self.answer) else self.answer
        return value, LLMUsage()


class FakeCms:
    slug = "wordpress"

    def __init__(self, live: dict[str, str] | None = None, *, schema_field: bool = False) -> None:
        self.live = dict(live or {})
        self.writes: list[tuple[str, str]] = []
        self.schema_field = schema_field

    def supports(self, capability: Capability) -> bool:
        return capability in {Capability.LIST_PAGES, Capability.READ_PAGE, Capability.WRITE_PAGE}

    def read_page(self, remote_id: str) -> RemotePage:
        return RemotePage(remote_id=remote_id, url="/", title="", body=self.live.get(remote_id, ""))

    def write_page(self, remote_id: str, *, body: str, title: str | None = None) -> bool:
        self.writes.append((remote_id, body))
        self.live[remote_id] = body
        return True

    def inject_schema(self, remote_id: str, *, json_ld: dict) -> bool:
        if not self.schema_field:
            raise NotImplementedError
        return True


def make_ctx(
    db: Session,
    *,
    slug: str = "on_page_seo_sync",
    scope: str = "",
    max_actions: int = 20,
    answer=None,  # noqa: ANN001
    connectors: dict | None = None,
    domain: str = "acme.test",
) -> AgentContext:
    record = AgentRecord(
        slug=slug,
        name=slug,
        category="seo",
        scope=scope,
        settings={},
        max_actions_per_day=max_actions,
        actions_today=0,
        actions_today_date="",
    )
    org = SimpleNamespace(id=TENANT, primary_domain=domain, name="Acme", settings={})
    return AgentContext(
        db=db,
        org=org,  # type: ignore[arg-type]
        record=record,
        cipher=PlainCipher(),  # type: ignore[arg-type]
        llm=ScriptedLLM(answer or {}),  # type: ignore[arg-type]
        connectors=dict(connectors or {}),
        connected_slugs=frozenset(connectors or {}),
    )


def add_page(
    db: Session,
    url: str,
    *,
    body: str = "Our widget costs less than you think and ships fast.",
    status: str = PageStatus.LIVE.value,
    cms_slug: str = "wordpress",
    **values,  # noqa: ANN003
) -> SeoPage:
    page = SeoPage(
        id=new_id(),
        tenant_id=TENANT,
        url=url,
        title=url.strip("/") or "Home",
        cms_id=f"cms-{url}",
        cms_slug=cms_slug,
        body_encrypted=body,
        status=status,
        schema_types=[],
        missing_topics=[],
        target_keywords=[],
        **values,
    )
    db.add(page)
    db.flush()
    return page


def add_pending(db: Session, *, target_kind: str, target_id: str, agent_slug: str = "x") -> ApprovalItem:
    item = ApprovalItem(
        tenant_id=TENANT,
        type="t",
        title="t",
        agent_slug=agent_slug,
        target_kind=target_kind,
        target_id=target_id,
        status=ApprovalStatus.PENDING.value,
        submitted_at=utcnow(),
    )
    db.add(item)
    db.flush()
    return item


# ── Shared page rules ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("metadata", "writable"),
    [
        ({"source": "rendered", "writable": False}, False),
        ({"source": "rendered"}, False),
        ({"writable": False}, False),
        ({"writable": True}, True),
        ({}, True),
    ],
)
def test_crawled_pages_are_readable_but_never_writable(metadata: dict, writable: bool):
    page = RemotePage(remote_id="x", url="/x", title="X", metadata=metadata)
    assert page_rules.is_writable(page) is writable


def test_the_crawler_is_a_read_only_source_and_a_cms_is_not():
    blocked = page_rules.read_only_sources()
    assert "site_crawler" in blocked
    assert "wordpress" not in blocked


def _source(slug: str, pages: list[RemotePage]) -> SimpleNamespace:
    return SimpleNamespace(slug=slug, list_pages=lambda limit: pages)


def test_harvest_prefers_the_copy_that_can_be_written_back():
    crawled = _source("site_crawler", [RemotePage("u", "/a", "A", metadata={"source": "rendered"})])
    cms = _source("wordpress", [RemotePage("1", "/a", "A", metadata={"writable": True})])
    ctx = SimpleNamespace(
        scope="",
        candidates=lambda capability: [crawled, cms],
        connector_name=lambda slug: slug,
    )
    assert page_rules.harvest(ctx, need_usable=True, prefer_writable=True).source is cms
    # Without the preference the first usable source still wins, as before.
    assert page_rules.harvest(ctx, need_usable=True).source is crawled


def test_harvest_falls_back_to_the_crawler_when_nothing_can_write():
    crawled = _source("site_crawler", [RemotePage("u", "/a", "A", metadata={"source": "rendered"})])
    ctx = SimpleNamespace(
        scope="",
        candidates=lambda capability: [crawled],
        connector_name=lambda slug: slug,
    )
    found = page_rules.harvest(ctx, need_usable=True, prefer_writable=True)
    assert found.source is crawled and found.ok


@pytest.mark.parametrize(("remaining", "expected"), [(0, 6), (3, 6), (14, 14), (400, 25)])
def test_batch_grows_with_the_daily_allowance_within_bounds(remaining: int, expected: int):
    ctx = SimpleNamespace(remaining_actions=remaining)
    assert page_rules.batch_size(ctx, floor=6) == expected


def test_current_body_reads_the_live_page_and_refreshes_the_copy(db):
    page = add_page(db, "/a", body="stale copy")
    ctx = make_ctx(db)
    cms = FakeCms({page.cms_id: "edited live copy"})
    assert page_rules.current_body(ctx, cms, page) == "edited live copy"
    assert page.body_encrypted == "edited live copy"


def test_current_body_refuses_to_write_over_nothing(db):
    page = add_page(db, "/a", body="")
    with pytest.raises(ConnectorError):
        page_rules.current_body(make_ctx(db), FakeCms(), page)


def test_current_body_falls_back_to_the_stored_copy_when_the_read_is_empty(db):
    page = add_page(db, "/a", body="stored copy")
    assert page_rules.current_body(make_ctx(db), FakeCms(), page) == "stored copy"


# ── On-Page SEO Sync ───────────────────────────────────────────────────────
_GAP = {
    "gap_score": 80,
    "missing_topics": ["delivery"],
    "target_keywords": ["widget"],
    "proposed_body": "A better page about widgets.",
    "rationale": "It never mentions delivery.",
}


def test_a_queued_page_with_no_approval_item_is_offered_again(db):
    held = add_page(db, "/held", status=PageStatus.QUEUED.value, proposed_body_encrypted="new")
    gone = add_page(db, "/gone", status=PageStatus.QUEUED.value)
    waiting = add_page(db, "/waiting", status=PageStatus.QUEUED.value, proposed_body_encrypted="new")
    add_pending(db, target_kind="seo_page", target_id=waiting.id, agent_slug="on_page_seo_sync")

    assert onpage.AGENT._release_stranded(make_ctx(db)) == 2
    assert held.status == PageStatus.FLAGGED.value
    assert gone.status == PageStatus.LIVE.value
    assert waiting.status == PageStatus.QUEUED.value

    actions = onpage.AGENT._propose_rewrites(make_ctx(db))
    assert [a.payload["url"] for a in actions] == ["/held"]


def test_an_empty_page_is_not_stamped_as_analysed(db):
    empty = add_page(db, "/empty", body="")
    full = add_page(db, "/full")
    ctx = make_ctx(db, answer=_GAP)

    assert onpage.AGENT._analyse_pages(ctx) == 1
    assert empty.last_synced_at is None
    assert full.last_synced_at is not None and full.status == PageStatus.FLAGGED.value


def test_analysis_and_proposals_stay_inside_the_scope_and_off_the_crawler(db):
    inside = add_page(db, "/blog/post")
    outside = add_page(db, "/pricing")
    crawled = add_page(db, "/blog/crawled", cms_slug="site_crawler")
    ctx = make_ctx(db, scope="/blog/*", answer=_GAP)

    assert onpage.AGENT._analyse_pages(ctx) == 1
    assert inside.status == PageStatus.FLAGGED.value
    assert outside.last_synced_at is None and crawled.last_synced_at is None

    outside.status = PageStatus.FLAGGED.value
    outside.proposed_body_encrypted = "x"
    crawled.status = PageStatus.FLAGGED.value
    crawled.proposed_body_encrypted = "x"
    actions = onpage.AGENT._propose_rewrites(ctx)
    assert [a.payload["url"] for a in actions] == ["/blog/post"]


def test_a_larger_daily_allowance_analyses_more_pages_per_run(db):
    for index in range(12):
        add_page(db, f"/p{index}")
    assert onpage.AGENT._analyse_pages(make_ctx(db, max_actions=3, answer=_GAP)) == 6
    for page in db.execute(select(SeoPage)).scalars():
        page.last_synced_at = None
    assert onpage.AGENT._analyse_pages(make_ctx(db, max_actions=10, answer=_GAP)) == 10


def test_a_rewritten_page_edited_afterwards_becomes_eligible_again(db):
    stale = utcnow() - onpage.ANALYSIS_TTL - timedelta(days=1)
    edited = add_page(db, "/edited", body="our copy", status=PageStatus.REWRITTEN.value, last_synced_at=stale)
    untouched = add_page(db, "/same", body="our copy", status=PageStatus.REWRITTEN.value, last_synced_at=stale)
    ctx = make_ctx(db, answer=_GAP)

    onpage._store_body(ctx, edited, "somebody changed this")
    onpage._store_body(ctx, untouched, "our copy")
    assert edited.status == PageStatus.LIVE.value
    assert untouched.status == PageStatus.REWRITTEN.value

    assert onpage.AGENT._analyse_pages(ctx) == 1
    assert edited.status == PageStatus.FLAGGED.value


def test_a_read_only_source_is_named_in_the_summary():
    text = onpage.OnPageSeoSyncAgent._summary(
        source="Site Crawler", synced=3, read=3, analysed=0, proposed=0, awaiting=0,
        read_only_source="Site Crawler",
    )
    assert "cannot publish" in text and "synced 3 pages" in text


# ── AEO Q&A Injector ───────────────────────────────────────────────────────
_PAIRS = {
    "pairs": [
        {"question": "How fast does the widget ship?", "answer": "Widgets ship fast."},
        {"question": "Is the widget cheap?", "answer": "It costs less than you think."},
    ]
}


def test_pairs_are_counted_when_injected_not_when_generated(db):
    page = add_page(db, "/widget")
    cms = FakeCms({page.cms_id: "Live copy someone edited."})
    ctx = make_ctx(db, slug="aeo_qa_injector", answer=_PAIRS, connectors={"wordpress": cms})

    generated, actions = aeo.AGENT._generate(ctx)
    assert generated == 2 and len(actions) == 1
    assert page.aeo_pairs == 0

    actions[0].apply(ctx)
    assert page.aeo_pairs == 2
    written = cms.writes[-1][1]
    # Merged into the live page, not the stale stored copy.
    assert written.startswith("Live copy someone edited.")
    assert "How fast does the widget ship?" in written


def test_a_second_injection_keeps_the_first_on_the_page(db):
    page = add_page(db, "/widget")
    cms = FakeCms({page.cms_id: "Live copy."})
    ctx = make_ctx(db, slug="aeo_qa_injector", answer=_PAIRS, connectors={"wordpress": cms})
    aeo.AGENT._generate(ctx)[1][0].apply(ctx)

    ctx.llm.answer = {"pairs": [{"question": "Can I return it?", "answer": "Yes, within 30 days."}]}
    aeo.AGENT._generate(ctx)[1][0].apply(ctx)
    written = cms.writes[-1][1]
    assert "How fast does the widget ship?" in written and "Can I return it?" in written
    assert written.count(aeo_prompts.SECTION_START) == 1
    assert page.aeo_pairs == 3


def test_a_page_with_a_pending_injection_is_left_alone(db):
    page = add_page(db, "/widget")
    add_pending(db, target_kind="aeo_pair", target_id=page.id, agent_slug="aeo_qa_injector")
    ctx = make_ctx(db, slug="aeo_qa_injector", answer=_PAIRS)
    assert aeo.AGENT._generate(ctx) == (0, [])
    assert ctx.llm.prompts == []


def test_pairs_that_never_reached_the_page_are_offered_again_not_regenerated(db):
    page = add_page(db, "/widget")
    ctx = make_ctx(db, slug="aeo_qa_injector", answer=_PAIRS)
    first = aeo.AGENT._generate(ctx)[1][0]
    # The cap held it back: no approval item, nothing injected.
    second_generated, second = aeo.AGENT._generate(ctx)
    assert second_generated == 0 and len(ctx.llm.prompts) == 1
    assert second[0].payload["pair_ids"] == first.payload["pair_ids"]
    assert db.execute(select(AeoQaPair).where(AeoQaPair.page_id == page.id)).scalars().all().__len__() == 2


def test_crawled_pages_get_no_qa_blocks(db):
    add_page(db, "/crawled", cms_slug="site_crawler")
    ctx = make_ctx(db, slug="aeo_qa_injector", answer=_PAIRS)
    assert aeo.AGENT._pages_needing_pairs(ctx) == []


def test_rejected_pairs_are_removed_and_injected_ones_kept(db):
    page = add_page(db, "/widget")
    kept = AeoQaPair(tenant_id=TENANT, page_id=page.id, question_encrypted="q1", answer_encrypted="a1", injected=True)
    dropped = AeoQaPair(tenant_id=TENANT, page_id=page.id, question_encrypted="q2", answer_encrypted="a2")
    db.add_all([kept, dropped])
    db.flush()
    item = SimpleNamespace(target_id=page.id)
    approvals.REJECTERS["aeo_pair"](
        db, SimpleNamespace(id=TENANT), item, {"pair_ids": [kept.id, dropped.id]}
    )
    remaining = {p.id for p in db.execute(select(AeoQaPair)).scalars()}
    assert remaining == {kept.id}


class FakeEngine:
    slug = "perplexity"

    def __init__(self, hits) -> None:  # noqa: ANN001
        self.hits = hits

    def supports(self, capability: Capability) -> bool:
        return capability is Capability.CHECK_CITATIONS

    def check_citations(self, *, queries, domain):  # noqa: ANN001, ANN201
        if isinstance(self.hits, Exception):
            raise self.hits
        return list(self.hits)


def test_citations_are_measured_not_accumulated(db):
    page = add_page(db, "/widget")
    pair = AeoQaPair(
        tenant_id=TENANT, page_id=page.id, question_encrypted="Is it cheap?",
        answer_encrypted="Yes.", injected=True, injected_at=utcnow(),
    )
    db.add(pair)
    db.flush()
    hit = CitationHit(engine="perplexity", query="Is it cheap?", cited_url="https://acme.test/widget")
    engine = FakeEngine([hit])
    ctx = make_ctx(db, slug="aeo_qa_injector", connectors={"perplexity": engine})

    assert aeo.AGENT._measure_citations(ctx) == (1, 1)
    assert aeo.AGENT._measure_citations(ctx) == (1, 0)
    assert pair.citation_count == 1

    engine.hits = []
    assert aeo.AGENT._measure_citations(ctx) == (0, 0)
    assert pair.citation_count == 0

    # A failed check is not a measurement: the stored figure stands.
    pair.citation_count = 3
    engine.hits = RuntimeError("down")
    assert aeo.AGENT._measure_citations(ctx) == (0, 0)
    assert pair.citation_count == 3


# ── Knowledge Graph & Schema ───────────────────────────────────────────────
def _graph(name: str = "Widget", url: str = "https://acme.test/widget") -> dict:
    return {
        "patches": [
            {
                "schema_type": "Product",
                "json_ld": {"@context": "https://schema.org", "@type": "Product", "name": name, "url": url},
            }
        ]
    }


def test_a_queued_patch_is_updated_in_place_not_duplicated(db):
    page = add_page(db, "/widget", body="The widget price is low.")
    ctx = make_ctx(db, slug="knowledge_graph_schema", answer=_graph())

    first = kg.AGENT._build_patches(ctx)
    second = kg.AGENT._build_patches(ctx)
    assert first[0].target_id == second[0].target_id

    ctx.llm.answer = _graph(name="Widget Pro")
    third = kg.AGENT._build_patches(ctx)
    assert third[0].target_id == first[0].target_id
    patches = db.execute(select(SchemaPatch).where(SchemaPatch.page_id == page.id)).scalars().all()
    assert len(patches) == 1 and "Widget Pro" in patches[0].json_ld_encrypted


def test_a_rejected_graph_is_not_offered_again(db):
    add_page(db, "/widget", body="The widget price is low.")
    ctx = make_ctx(db, slug="knowledge_graph_schema", answer=_graph())
    action = kg.AGENT._build_patches(ctx)[0]

    approvals.REJECTERS["schema_patch"](
        db, SimpleNamespace(id=TENANT), SimpleNamespace(target_id=action.target_id), action.payload
    )
    assert db.get(SchemaPatch, action.target_id).status == "rejected"
    assert kg.AGENT._build_patches(ctx) == []


def test_reviews_rotate_through_the_site(db):
    for index in range(7):
        add_page(db, f"/p{index}", body="The widget price is low.")
    ctx = make_ctx(db, slug="knowledge_graph_schema", max_actions=0, answer={"patches": []})

    first = {p.url for p in kg.AGENT._pages_to_review(ctx)}
    kg.AGENT._build_patches(ctx)
    second = {p.url for p in kg.AGENT._pages_to_review(ctx)}
    assert len(first) == 5
    # The two never reviewed come first, then the oldest reviews.
    assert {f"/p{i}" for i in range(7)} - first <= second


def test_no_placeholder_domain_in_the_graph(db):
    add_page(db, "/widget", body="The widget price is low.")
    ctx = make_ctx(db, slug="knowledge_graph_schema", domain="", answer=_graph(url="/widget"))
    actions = kg.AGENT._build_patches(ctx)
    assert "example.com" not in ctx.llm.prompts[0]
    assert "url" not in actions[0].payload["json_ld"]


def test_no_faq_graph_on_a_page_with_the_faq_section(db):
    body = "Questions. " + aeo_prompts.merge_qa_section("Copy about the widget faq.", ["<div>q</div>"])
    page = add_page(db, "/faq", body=body)
    ctx = make_ctx(db, slug="knowledge_graph_schema", scope="FAQPage, Product")
    assert kg.AGENT._wanted_types(ctx, body, page) == ["Product"]
    assert "FAQPage" not in kg.AGENT._wanted_types(make_ctx(db), body, page)


def test_the_script_tag_goes_into_the_live_page(db):
    page = add_page(db, "/widget", body="stale stored copy, price low")
    cms = FakeCms({page.cms_id: "live copy, price low"})
    ctx = make_ctx(db, slug="knowledge_graph_schema", answer=_graph(), connectors={"wordpress": cms})
    kg.AGENT._build_patches(ctx)[0].apply(ctx)
    written = cms.writes[-1][1]
    assert written.startswith("live copy") and "application/ld+json" in written


# ── Technical SEO Auditor ──────────────────────────────────────────────────
def test_issues_outside_the_scope_are_not_marked_fixed(db):
    inside = add_page(db, "/blog/a", body='<a href="/pricing">p</a>', word_count=50)
    outside = add_page(db, "/pricing", body='<a href="/blog/a">b</a>', word_count=50)
    db.add(
        SeoIssue(
            tenant_id=TENANT, page_id=outside.id, url="/pricing", kind="thin_content",
            severity="high", target="", summary="thin", status="open",
            first_seen_at=utcnow(), last_seen_at=utcnow(),
        )
    )
    db.flush()
    ctx = make_ctx(db, slug="technical_seo_auditor", scope="/blog/*")
    auditor.AGENT.run(ctx)

    issues = db.execute(select(SeoIssue)).scalars().all()
    assert {i.page_id for i in issues if i.status == "open" and i.kind == "thin_content"} == {
        inside.id,
        outside.id,
    }
    kinds = {i.kind for i in issues if i.page_id == inside.id}
    # /pricing exists, it is merely out of scope; and it links to /blog/a.
    assert "broken_internal_link" not in kinds
    assert "orphan_page" not in kinds


class FakeSpeed:
    slug = "pagespeed_insights"

    def __init__(self) -> None:
        self.asked: list[str] = []

    def supports(self, capability: Capability) -> bool:
        return capability is Capability.READ_PAGE_SPEED

    def read_vitals(self, url: str):  # noqa: ANN201
        self.asked.append(url)
        return SimpleNamespace(lcp_ms=1000, cls=None, inp_ms=None, origin_level_only=False)


def test_speed_readings_rotate_and_unmeasured_pages_keep_their_issues(db, monkeypatch):
    monkeypatch.setattr(auditor.settings, "seo_vitals_pages_per_run", 1)
    first = add_page(db, "/a", word_count=900)
    second = add_page(db, "/b", word_count=100)
    for page in (first, second):
        db.add(
            SeoIssue(
                tenant_id=TENANT, page_id=page.id, url=page.url, kind="slow_lcp_mobile",
                severity="high", target="", summary="slow", status="open",
                first_seen_at=utcnow(), last_seen_at=utcnow(),
            )
        )
    db.flush()
    speed = FakeSpeed()
    ctx = make_ctx(db, slug="technical_seo_auditor", connectors={"pagespeed_insights": speed})

    auditor.AGENT.run(ctx)
    auditor.AGENT.run(ctx)
    assert sorted(speed.asked) == ["https://acme.test/a", "https://acme.test/b"]

    db.expire_all()
    statuses = {
        issue.url: issue.status
        for issue in db.execute(select(SeoIssue).where(SeoIssue.kind == "slow_lcp_mobile")).scalars()
    }
    # Each was fixed only by its own fast reading.
    assert statuses == {"/a": "fixed", "/b": "fixed"}


def test_a_page_not_measured_this_pass_keeps_its_speed_issue(db, monkeypatch):
    monkeypatch.setattr(auditor.settings, "seo_vitals_pages_per_run", 1)
    pages = [add_page(db, "/a"), add_page(db, "/b")]
    for page in pages:
        db.add(
            SeoIssue(
                tenant_id=TENANT, page_id=page.id, url=page.url, kind="slow_lcp_mobile",
                severity="high", target="", summary="slow", status="open",
                first_seen_at=utcnow(), last_seen_at=utcnow(),
            )
        )
    db.flush()
    result = auditor.AGENT.run(
        make_ctx(db, slug="technical_seo_auditor", connectors={"pagespeed_insights": FakeSpeed()})
    )
    statuses = sorted(i.status for i in db.execute(select(SeoIssue).where(SeoIssue.kind == "slow_lcp_mobile")).scalars())
    assert statuses == ["fixed", "open"]
    assert result.detail["resolved"] == 1
