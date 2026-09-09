"""SEO / AEO domain models: pages, Q&A pairs, schema patches, spam events.

Page copy is customer content, so bodies are held in ``*_encrypted`` columns
under the organisation's own data key (see ``app.services.encryption``).
Everything the console needs to *list and sort* pages — url, title, scores,
status — stays queryable in the clear.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import FK, PK, Base, TenantScopedMixin, TimestampMixin, new_id


class PageStatus(StrEnum):
    """Lifecycle of one page through the optimisation pipeline.

    ``flagged``   - the crawler found a semantic gap; a rewrite is available
    ``queued``    - the rewrite is waiting on human approval
    ``rewritten`` - approved and written back to the CMS
    ``live``      - verified live on the site, no gap outstanding
    """

    FLAGGED = "flagged"
    QUEUED = "queued"
    REWRITTEN = "rewritten"
    LIVE = "live"


PAGE_STATUS_LABELS: dict[str, str] = {
    PageStatus.FLAGGED.value: "Needs approval",
    PageStatus.QUEUED.value: "Awaiting approval",
    PageStatus.REWRITTEN.value: "Rewritten",
    PageStatus.LIVE.value: "Live",
}


class SeoPage(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "seo_pages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "url", name="uq_seo_page_tenant_url"),
        Index("ix_seo_pages_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)

    # 0-100: how much of the page's target semantic space is unaddressed.
    gap_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    aeo_pairs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    schema_types: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(
        String(16), default=PageStatus.LIVE.value, nullable=False
    )

    # Identifier of the node in the customer's CMS, for writing back.
    cms_id: Mapped[str] = mapped_column(String(120), default="")
    cms_slug: Mapped[str] = mapped_column(String(64), default="")
    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ── Customer content (organisation-key encrypted) ──────────────────────
    body_encrypted: Mapped[str] = mapped_column(Text, default="")
    proposed_body_encrypted: Mapped[str] = mapped_column(Text, default="")
    rewrite_rationale_encrypted: Mapped[str] = mapped_column(Text, default="")

    missing_topics: Mapped[list] = mapped_column(JSONB, default=list)
    target_keywords: Mapped[list] = mapped_column(JSONB, default=list)

    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def schema_text(self) -> str:
        return ", ".join(self.schema_types or [])


class AeoQaPair(Base, TimestampMixin, TenantScopedMixin):
    """A conversational Q&A block produced for answer-engine retrieval."""

    __tablename__ = "aeo_qa_pairs"
    __table_args__ = (Index("ix_qa_tenant_page", "tenant_id", "page_id"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    page_id: Mapped[str] = mapped_column(
        FK, ForeignKey("seo_pages.id", ondelete="CASCADE"), index=True, nullable=False
    )
    question_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    answer_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    # Which crawler this phrasing targets: perplexity | openai | gemini | claude | all
    target_engine: Mapped[str] = mapped_column(String(32), default="all", nullable=False)
    injected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    injected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    citation_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class SchemaPatch(Base, TimestampMixin, TenantScopedMixin):
    """A JSON-LD graph fragment queued for, or written to, a page."""

    __tablename__ = "schema_patches"
    __table_args__ = (Index("ix_patch_tenant_status", "tenant_id", "status"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    page_id: Mapped[str] = mapped_column(
        FK, ForeignKey("seo_pages.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # FAQPage | Product | LocalBusiness | ItemList | BreadcrumbList
    schema_type: Mapped[str] = mapped_column(String(64), nullable=False)
    json_ld_encrypted: Mapped[str] = mapped_column(Text, default="")
    # queued | injected | rejected
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    # Set when the patch replaces an existing graph rather than adding one.
    replaces_patch_id: Mapped[str | None] = mapped_column(FK, nullable=True)
    injected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SeoIssue(Base, TimestampMixin, TenantScopedMixin):
    """One technical finding on one page.

    Kept as a row per (page, kind, target) rather than a report blob, for
    three reasons that matter to the people using it:

    * **It can be worked through.** A finding is something somebody fixes, so
      it needs a state — open, fixed, or deliberately ignored — and the
      Ignore has to survive the next run or the same argument happens weekly.
    * **"Fixed" is observable.** ``first_seen_at`` and ``resolved_at`` are
      what let the console say "31 issues fixed this month", which is the
      number that shows the work was worth paying for.
    * **It is not fabricated.** Every row traces to a crawled page and a rule
      in ``checks.py``; nothing here is a score a model invented.

    The recommendation is stored rather than recomputed so a finding reads the
    same in a report as it did on the day it was raised, even after the rule's
    wording changes.
    """

    __tablename__ = "seo_issues"
    __table_args__ = (
        Index("ix_issue_tenant_status", "tenant_id", "status"),
        Index("ix_issue_tenant_severity", "tenant_id", "severity"),
        # The identity of a finding across runs: the same problem on the same
        # page with the same target is the same issue, not a new one.
        UniqueConstraint("tenant_id", "page_id", "kind", "target", name="uq_issue_identity"),
    )

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    page_id: Mapped[str] = mapped_column(
        FK, ForeignKey("seo_pages.id", ondelete="CASCADE"), index=True, nullable=False
    )
    url: Mapped[str] = mapped_column(String(512), nullable=False)

    kind: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(8), nullable=False)
    #: Distinguishes several findings of one kind on one page — the specific
    #: broken link, for instance. Empty for whole-page findings.
    target: Mapped[str] = mapped_column(String(512), default="", nullable=False)

    summary: Mapped[str] = mapped_column(Text, nullable=False)
    recommendation: Mapped[str] = mapped_column(Text, default="")
    #: Non-sensitive specifics: the word count, the LCP, the link. Not
    #: encrypted, because none of it is customer content — it is measurements
    #: of the customer's public pages.
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)

    # open | fixed | ignored
    status: Mapped[str] = mapped_column(String(12), default="open", nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Why somebody chose to live with it. Required by the API when ignoring,
    #: so "ignored" never means "nobody remembers".
    ignore_note: Mapped[str] = mapped_column(Text, default="")


class ReferralSpamEvent(Base, TenantScopedMixin):
    """One blocked referrer from the traffic-quality guard."""

    __tablename__ = "referral_spam_events"
    __table_args__ = (Index("ix_refspam_tenant_at", "tenant_id", "at"),)

    id: Mapped[str] = mapped_column(PK, primary_key=True, default=new_id)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    referrer: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), default="Blocked", nullable=False)
    sessions_affected: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
