"""Load sample data into one workspace so the console is full.

This is for a named test workspace only. Connectors are marked connected with
sample hints (not real credentials) so the catalogue looks live; they are not
verified with vendors. Agents are configured and given history, but their next
run is pushed forward so the scheduler does not call live APIs.
"""
from __future__ import annotations

import argparse
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base.registry import all_agents
from app.connectors.base import registry as connector_registry
from app.core.crypto import email_index
from app.db.base import new_id, utcnow
from app.db.session import SessionLocal, bind_tenant, session_scope
from app.models.ads import (
    AdChannel,
    AdCreative,
    AudienceCluster,
    BudgetAllocation,
    FraudEvent,
)
from app.models.agent import AgentRecord, AgentRun, AgentStatus, RunStatus, record_timeline_step
from app.models.approval import ApprovalItem, ApprovalStatus, ApprovalType
from app.models.connector import ConnectorHealth, ConnectorRecord
from app.models.content import AssetKind, AssetStatus, GeneratedImage, MarketingAsset
from app.models.creative import BrandKit, CreativeJob, CreativeJobStatus, Product
from app.models.demo import DemoStatus, SiteAnalysis
from app.models.local import LocalRankCheck, ReviewReply
from app.models.membership import WorkspaceMembership
from app.models.offpage import BacklinkStatus, BacklinkTarget, CompetitorAlert, OutreachPitch
from app.models.seo import AeoQaPair, PageStatus, ReferralSpamEvent, SchemaPatch, SeoIssue, SeoPage
from app.models.social import SocialPostMetric
from app.models.workspace import OnboardingState, Organization, User
from app.services import approvals, assets, audit, brand_profile, metrics
from app.services.encryption import Ctx, OrgCipher
from app.services.provisioning import sync_catalog
from app.services.social_metrics import Point, record_points

EMAIL = "thakurdishant02@gmail.com"
WORKSPACE = "test-workspace-2"
SITE = "https://www.testworkspace.com"
MARKER = "demo_sample"

# 1×1 PNG so image rows satisfy NOT NULL without shipping a photo.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)

# Connectors that would make agents call a model or that Portal Admin turned off.
_SKIP_CONNECT = frozenset(
    {
        "openai",
        "perplexity",
        "google_gemini",
        "anthropic_claude",
        "xai_grok",
        "vllm",
        "ollama",
        "tiktok_ads",
        "smtp_email",
        "reddit",
    }
)

_RUNNING = frozenset(
    {
        "on_page_seo_sync",
        "technical_seo_auditor",
        "seo_article_writer",
        "social_media_manager",
        "email_marketer",
        "search_campaign_builder",
    }
)


def _day(offset: int) -> date:
    return utcnow().date() + timedelta(days=offset)


def _at(days: int, hour: int = 10) -> datetime:
    return datetime.combine(_day(days), datetime.min.time(), tzinfo=UTC) + timedelta(hours=hour)


def _wave(offset: int, base: float, amplitude: float) -> float:
    return round(base + amplitude * (0.55 + 0.45 * ((offset * 17) % 10) / 9), 2)


def cmd_seed_demo(args: argparse.Namespace) -> int:
    email = (args.email or EMAIL).strip().lower()
    slug = (args.workspace or WORKSPACE).strip()
    org_id, user_id = _resolve(email, slug)
    with session_scope(org_id) as db:
        org = db.get(Organization, org_id)
        user = db.get(User, user_id)
        if org is None or user is None:
            raise SystemExit("Workspace or member row was missing after pin.")
        counts = seed_workspace(db, org, user, force=bool(args.force))
    print(f"Sample data loaded into {slug} for {email}")
    for key, value in counts.items():
        print(f"  {key}: {value}")
    return 0


def _resolve(email: str, slug: str) -> tuple[str, str]:
    db = SessionLocal()
    try:
        memberships = list(
            db.execute(
                select(WorkspaceMembership).where(
                    WorkspaceMembership.email_index == email_index(email)
                )
            ).scalars()
        )
        if not memberships:
            raise SystemExit(f"No workspace seat for {email}")
        chosen: WorkspaceMembership | None = None
        names: list[str] = []
        for seat in memberships:
            org = db.get(Organization, seat.tenant_id)
            if org is None:
                continue
            names.append(f"{org.slug} ({'active' if org.is_active and seat.is_active else 'inactive'})")
            if org.slug == slug:
                chosen = seat
        if chosen is None:
            raise SystemExit(
                f"{email} has no seat in {slug}. Workspaces: {', '.join(names)}"
            )
        org = db.get(Organization, chosen.tenant_id)
        if org is None or not org.is_active or not chosen.is_active:
            raise SystemExit(f"Workspace {slug} is inactive; reactivate it first.")
        return chosen.tenant_id, chosen.user_id
    finally:
        db.close()


def seed_workspace(
    db: Session, org: Organization, user: User, *, force: bool = False
) -> dict[str, int]:
    settings = dict(org.settings or {})
    if settings.get(MARKER) and not force:
        print("Already loaded (pass --force to write again).")
        return {"skipped": 1}

    bind_tenant(db, org.id)
    sync_catalog(db, org)
    cipher = OrgCipher.for_org(org)

    org.primary_domain = "testworkspace.com"
    org.plan_name = "Enterprise Plan"
    org.seats_total = 25
    org.renews_on = _day(40)
    org.global_autonomy = False
    brand_profile.update(
        db,
        org,
        {
            "timezone": "Asia/Kolkata",
            "business_name": "Test Workspace",
            "summary": "A sample fitness studio used to walk through every Synaprank.ai screen.",
            "industry": "Fitness studio",
            "tone": "Warm and clear",
            "language": "English",
            "call_to_action": "Book a trial class",
            "products_services": ["Personal training", "Group classes", "Nutrition coaching"],
            "audiences": ["Busy professionals in Noida", "Parents looking for weekend classes"],
            "value_propositions": ["Small groups", "Coaches who remember your name"],
            "differentiators": ["Trial class with a written plan"],
            "keywords": ["gym in noida", "personal trainer noida", "beginner fitness class"],
            "competitors": ["Cult.fit", "Gold's Gym"],
            "locations": ["Noida", "Greater Noida"],
        },
    )

    onboard = db.execute(
        select(OnboardingState).where(OnboardingState.tenant_id == org.id)
    ).scalar_one_or_none()
    if onboard is not None:
        onboard.completed = True
        onboard.step = 3
        onboard.domain = "testworkspace.com"
        onboard.cms = "WordPress"
        onboard.guardrail = "hybrid"

    counts = {
        "connectors": _connect_catalogue(db, org, user, cipher),
        "agents": _configure_agents(db, org, user),
        "metrics": _metrics(db, org),
        "seo": _seo(db, org, cipher),
        "content": _content(db, org, user, cipher),
        "ads": _ads(db, org, cipher),
        "offpage": _offpage(db, org, cipher),
        "local": _local(db, org, cipher),
        "creative": _creative(db, org, user, cipher),
        "approvals": _approvals(db, org, user, cipher),
        "analysis": _site_analysis(db, org, user),
        "spam": _spam(db, org),
    }
    settings = dict(org.settings or {})
    settings[MARKER] = True
    settings[f"{MARKER}_at"] = utcnow().isoformat()
    org.settings = settings
    audit.record_system_action(
        db,
        tenant_id=org.id,
        action="loaded sample data for the test workspace",
        module="admin",
        context={"email": EMAIL, "sample": True},
    )
    return counts


def _connect_catalogue(db: Session, org: Organization, user: User, cipher: OrgCipher) -> int:
    now = utcnow()
    added = 0
    offered = {spec.slug for spec in connector_registry.all_specs()}
    records = list(
        db.execute(select(ConnectorRecord).where(ConnectorRecord.tenant_id == org.id)).scalars()
    )
    for record in records:
        if record.slug not in offered or record.slug in _SKIP_CONNECT:
            continue
        if record.connected:
            continue
        record.connected = True
        record.oauth_completed = True
        record.connected_at = now - timedelta(days=12)
        record.connected_by = user.name
        record.last_sync_at = now - timedelta(hours=6)
        record.last_health_check_at = now
        record.health = ConnectorHealth.OK.value
        record.last_error = ""
        record.credential_hints = {
            "accountName": "Test Workspace (sample)",
            "siteUrl": SITE,
        }
        record.credentials_encrypted = cipher.encrypt_json(
            {"sample": "not-a-credential"}, context=Ctx.CONNECTOR_CREDENTIALS
        )
        record.credentials_version = max(record.credentials_version, 1)
        added += 1
    return added


def _configure_agents(db: Session, org: Organization, user: User) -> int:
    names = {agent.spec.slug: agent.spec.name for agent in all_agents()}
    records = list(db.execute(select(AgentRecord).where(AgentRecord.tenant_id == org.id)).scalars())
    later = utcnow() + timedelta(days=14)
    updated = 0
    for record in records:
        record.configured = True
        record.scope = SITE
        record.notify_channel = "Email"
        record.autonomy = record.slug not in {"digital_pr_outreach", "on_page_seo_sync"}
        if record.slug in _RUNNING:
            record.status = AgentStatus.RUNNING.value
            record.next_run_at = later
            record.last_run_at = utcnow() - timedelta(hours=3)
            record.metric_label = "Last run 3 hours ago"
            record.actions_today = 4
        elif record.slug == "click_fraud_controller":
            record.status = AgentStatus.ERROR.value
            record.last_error = "Sample: the ad account paused the placement we tried to block."
            record.last_run_at = utcnow() - timedelta(hours=8)
            record.metric_label = "Needs attention"
            record.next_run_at = None
        else:
            record.status = AgentStatus.PAUSED.value
            record.last_run_at = utcnow() - timedelta(days=1)
            record.metric_label = "Paused after a successful run"
            record.next_run_at = None
        updated += 1
        _agent_runs(db, org, record, names.get(record.slug, record.name))
    return updated


def _agent_runs(db: Session, org: Organization, record: AgentRecord, name: str) -> None:
    existing = db.scalar(
        select(AgentRun.id).where(
            AgentRun.tenant_id == org.id, AgentRun.agent_slug == record.slug
        ).limit(1)
    )
    if existing:
        return
    for days, status, actions in ((1, RunStatus.SUCCEEDED.value, 6), (3, RunStatus.SUCCEEDED.value, 4), (8, RunStatus.FAILED.value, 0)):
        started = _at(-days, 9)
        finished = started + timedelta(minutes=7)
        detail = record_timeline_step({}, "Reading the site", done=1, total=3)
        detail = record_timeline_step(detail, "Writing recommendations", done=3, total=3)
        run = AgentRun(
            tenant_id=org.id,
            agent_slug=record.slug,
            trigger="schedule",
            triggered_by="sample data",
            status=status,
            started_at=started,
            finished_at=finished,
            duration_ms=7 * 60 * 1000,
            actions_taken=actions if status == RunStatus.SUCCEEDED.value else 0,
            actions_queued=1 if record.slug == "on_page_seo_sync" else 0,
            summary=(
                "Sample run finished: pages checked and recommendations queued."
                if status == RunStatus.SUCCEEDED.value
                else "Sample run stopped: the CMS was not reachable."
            ),
            error="" if status == RunStatus.SUCCEEDED.value else "Sample failure for the history list.",
            step="",
            tokens_in=12000 + days * 400,
            tokens_out=2800 + days * 90,
            llm_connector="anthropic_claude",
            llm_model="claude-sonnet-4-5",
            detail=detail,
        )
        db.add(run)
        audit.record_agent_action(
            db,
            tenant_id=org.id,
            agent_name=name,
            action=run.summary,
            module="agents",
        )


def _metrics(db: Session, org: Organization) -> int:
    points: list[Point] = []
    for offset in range(-45, 1):
        day = _day(offset)
        visits = _wave(offset, 420, 90)
        points.extend(
            [
                Point(day, "google_analytics_4", "sessions", visits),
                Point(day, "google_analytics_4", "conversions", round(visits * 0.04, 2)),
                Point(day, "search_console", "clicks", _wave(offset, 180, 40)),
                Point(day, "search_console", "impressions", _wave(offset, 5400, 800)),
                Point(day, "search_console", "position", round(8.4 - offset * 0.02, 2)),
                Point(day, "bing_webmaster", "clicks", _wave(offset, 40, 10)),
                Point(day, "bing_webmaster", "impressions", _wave(offset, 900, 120)),
                Point(day, "bing_webmaster", "position", 12.1),
                Point(day, "google_ads", "spend", _wave(offset, 85, 20)),
                Point(day, "google_ads", "clicks", _wave(offset, 110, 25)),
                Point(day, "google_ads", "impressions", _wave(offset, 4200, 600)),
                Point(day, "google_ads", "conversions", _wave(offset, 6, 2)),
                Point(day, "meta_ads", "spend", _wave(offset, 60, 15)),
                Point(day, "meta_ads", "clicks", _wave(offset, 95, 18)),
                Point(day, "meta_ads", "impressions", _wave(offset, 7800, 900)),
                Point(day, "meta_ads", "conversions", _wave(offset, 5, 1.5)),
                Point(day, "linkedin_ads", "spend", _wave(offset, 35, 8)),
                Point(day, "linkedin_ads", "clicks", _wave(offset, 22, 6)),
                Point(day, "linkedin_ads", "impressions", _wave(offset, 1600, 200)),
                Point(day, "linkedin_ads", "conversions", _wave(offset, 2, 0.6)),
                Point(day, "social", "reach", _wave(offset, 2400, 400), "facebook"),
                Point(day, "social", "engagements", _wave(offset, 180, 40), "facebook"),
                Point(day, "social", "reach", _wave(offset, 3100, 500), "instagram"),
                Point(day, "social", "engagements", _wave(offset, 260, 50), "instagram"),
                Point(day, "social", "reach", _wave(offset, 900, 120), "linkedin"),
                Point(day, "social", "engagements", _wave(offset, 70, 15), "linkedin"),
            ]
        )
        metrics.record_snapshot(
            db,
            tenant_id=org.id,
            day=day,
            values={
                "organic_sessions": int(visits),
                "conversions": int(visits * 0.04),
                "ad_spend": _wave(offset, 180, 30),
            },
        )
        metrics.accumulate(
            db,
            tenant_id=org.id,
            day=day,
            values={
                "pages_optimised": 1 if offset % 3 == 0 else 0,
                "backlinks_won": 1 if offset % 11 == 0 else 0,
                "pitches_sent": 1 if offset % 5 == 0 else 0,
                "fraud_blocked": 2 if offset % 4 == 0 else 0,
                "spend_saved": 12 if offset % 4 == 0 else 0,
                "referral_spam_blocked": 3 if offset % 2 == 0 else 0,
                "agent_runs": 8,
                "actions_autonomous": 3,
                "actions_approved": 2,
                "aeo_citations": 1 if offset % 6 == 0 else 0,
            },
        )
    return record_points(db, tenant_id=org.id, points=points)


def _seo(db: Session, org: Organization, cipher: OrgCipher) -> int:
    if db.scalar(select(SeoPage.id).where(SeoPage.tenant_id == org.id).limit(1)):
        return 0
    pages = [
        ("/", "Test Workspace — fitness in Noida", PageStatus.LIVE.value, 12, ["FAQPage"], True),
        ("/personal-training", "Personal training", PageStatus.FLAGGED.value, 48, ["Service"], False),
        ("/group-classes", "Group classes", PageStatus.QUEUED.value, 36, [], False),
        ("/nutrition", "Nutrition coaching", PageStatus.REWRITTEN.value, 22, ["Article"], True),
        ("/blog/first-gym-session", "What to expect in your first session", PageStatus.LIVE.value, 8, ["Article", "FAQPage"], True),
        ("/contact", "Book a trial class", PageStatus.LIVE.value, 18, ["LocalBusiness"], True),
    ]
    created = 0
    for path, title, status, gap, schemas, live in pages:
        page = SeoPage(
            tenant_id=org.id,
            url=f"{SITE}{path}",
            title=title,
            gap_score=gap,
            aeo_pairs=2 if live else 0,
            schema_types=schemas,
            status=status,
            cms_id=path.strip("/") or "home",
            cms_slug="wordpress",
            word_count=780 if live else 240,
            body_encrypted=cipher.encrypt(
                f"<p>Sample copy for {title}. Coaches, small groups, a written plan after the trial class.</p>",
                context=Ctx.PAGE_BODY,
            ),
            proposed_body_encrypted=cipher.encrypt(
                f"<p>Rewritten sample for {title}: clearer promise, local keywords, a booking CTA.</p>",
                context=Ctx.PAGE_PROPOSED,
            )
            if status != PageStatus.LIVE.value
            else "",
            rewrite_rationale_encrypted=cipher.encrypt(
                "The page never names Noida or the trial class.",
                context=Ctx.PAGE_RATIONALE,
            )
            if status != PageStatus.LIVE.value
            else "",
            missing_topics=["trial class", "Noida location"] if gap > 20 else [],
            target_keywords=["gym in noida", "personal trainer noida"],
            last_crawled_at=utcnow() - timedelta(hours=5),
            last_synced_at=utcnow() - timedelta(hours=5),
        )
        db.add(page)
        db.flush()
        created += 1
        db.add(
            AeoQaPair(
                tenant_id=org.id,
                page_id=page.id,
                question_encrypted=cipher.encrypt(
                    f"Does Test Workspace offer {title.lower()}?", context=Ctx.QA_QUESTION
                ),
                answer_encrypted=cipher.encrypt(
                    "Yes. Book a trial class and leave with a written plan.",
                    context=Ctx.QA_ANSWER,
                ),
                target_engine="all",
                injected=live,
                injected_at=utcnow() - timedelta(days=2) if live else None,
                citation_count=4 if live else 0,
            )
        )
        if schemas:
            db.add(
                SchemaPatch(
                    tenant_id=org.id,
                    page_id=page.id,
                    schema_type=schemas[0],
                    json_ld_encrypted=cipher.encrypt_json(
                        {"@type": schemas[0], "name": title}, context=Ctx.SCHEMA_JSONLD
                    ),
                    status="injected" if live else "queued",
                    injected_at=utcnow() - timedelta(days=2) if live else None,
                )
            )
        kinds = [
            ("missing_meta_description", "medium", "Add a unique meta description naming the trial class."),
            ("thin_content", "high", "The page is under 300 words; add what a first visit looks like."),
            ("image_missing_alt", "low", "Coach photos have no alt text."),
        ]
        for i, (kind, severity, summary) in enumerate(kinds):
            if path == "/contact" and i:
                continue
            db.add(
                SeoIssue(
                    tenant_id=org.id,
                    page_id=page.id,
                    url=page.url,
                    kind=kind,
                    severity=severity,
                    target="" if kind != "image_missing_alt" else "/images/coach.png",
                    summary=summary,
                    recommendation=summary,
                    evidence={"word_count": page.word_count},
                    status="fixed" if live and i == 0 else "open",
                    first_seen_at=utcnow() - timedelta(days=20),
                    last_seen_at=utcnow() - timedelta(hours=5),
                    resolved_at=utcnow() - timedelta(days=4) if live and i == 0 else None,
                )
            )
    return created


def _content(db: Session, org: Organization, user: User, cipher: OrgCipher) -> int:
    if db.scalar(select(MarketingAsset.id).where(MarketingAsset.tenant_id == org.id).limit(1)):
        return 0
    image = GeneratedImage(
        tenant_id=org.id,
        created_at=utcnow() - timedelta(days=2),
        prompt="Sample studio floor, no faces",
        provider="sample",
        mime="image/png",
        width=1,
        height=1,
        data=_PNG,
    )
    db.add(image)
    db.flush()
    items = [
        (AssetKind.ARTICLE, "wordpress", "What to expect in your first session", AssetStatus.PUBLISHED, "seo_article_writer"),
        (AssetKind.ARTICLE, "wordpress", "A four-week beginner plan", AssetStatus.DRAFT, "seo_article_writer"),
        (AssetKind.SOCIAL_POST, "facebook", "Trial class this Saturday", AssetStatus.PUBLISHED, "social_media_manager"),
        (AssetKind.SOCIAL_POST, "instagram", "Small groups. Coaches who remember your name.", AssetStatus.PUBLISHED, "social_media_manager"),
        (AssetKind.SOCIAL_POST, "linkedin", "Why professionals in Noida start here", AssetStatus.SCHEDULED, "social_media_manager"),
        (AssetKind.EMAIL, "mailchimp", "Your trial class is booked", AssetStatus.APPROVED, "email_marketer"),
        (AssetKind.SEARCH_CAMPAIGN, "google_ads", "Personal training — Noida", AssetStatus.APPROVED, "search_campaign_builder"),
        (AssetKind.CREATIVE, "meta_ads", "Book a trial — 300×250", AssetStatus.PUBLISHED, "creative_studio"),
    ]
    created = 0
    for kind, channel, title, status, agent in items:
        content = {
            "text": f"Sample {kind.replace('_', ' ')} for Test Workspace. {title}.",
            "hashtags": ["NoidaFitness", "TrialClass"] if kind == AssetKind.SOCIAL_POST else [],
            "link": SITE,
        }
        asset = assets.create(
            db,
            tenant_id=org.id,
            cipher=cipher,
            agent_slug=agent,
            kind=kind,
            channel=channel,
            title=title,
            content=content,
            detail={"sample": True, "keyword": "gym in noida", "word_count": 620},
            image_ids=[image.id] if kind in {AssetKind.SOCIAL_POST, AssetKind.CREATIVE} else [],
        )
        asset.status = status
        if status == AssetStatus.PUBLISHED:
            asset.published_at = utcnow() - timedelta(days=2)
            asset.remote_id = f"sample-{created+1}"
            asset.remote_url = f"https://www.facebook.com/testworkspace/posts/{created+1}"
        if status == AssetStatus.SCHEDULED:
            asset.scheduled_for = utcnow() + timedelta(days=1)
        created += 1
        if kind == AssetKind.SOCIAL_POST and status == AssetStatus.PUBLISHED:
            db.add(
                SocialPostMetric(
                    tenant_id=org.id,
                    asset_id=asset.id,
                    network=channel if channel != "facebook" else "facebook",
                    remote_id=asset.remote_id,
                    fetched_at=utcnow() - timedelta(hours=2),
                    impressions=4200 + created * 200,
                    reach=3100 + created * 150,
                    engagements=240 + created * 20,
                    clicks=90 + created * 8,
                    likes=120,
                    comments=18,
                    shares=11,
                    saves=None if channel == "facebook" else 14,
                )
            )
    return created


def _ads(db: Session, org: Organization, cipher: OrgCipher) -> int:
    shares = {
        AdChannel.GOOGLE.value: (28, 42.5, 1840.0, 44),
        AdChannel.META.value: (24, 38.0, 1260.0, 33),
        AdChannel.LINKEDIN.value: (18, 91.0, 640.0, 7),
        AdChannel.TIKTOK.value: (0, 0.0, 0.0, 0),
        AdChannel.MICROSOFT.value: (10, 55.0, 410.0, 8),
        AdChannel.DSP.value: (20, 67.0, 720.0, 11),
    }
    rows = list(
        db.execute(select(BudgetAllocation).where(BudgetAllocation.tenant_id == org.id)).scalars()
    )
    for row in rows:
        percent, cac, spend, conversions = shares.get(row.channel, (row.percent, 0.0, 0.0, 0))
        row.percent = percent
        row.cac = cac
        row.spend = spend
        row.conversions = conversions
        row.last_shift = 2 if row.channel == AdChannel.GOOGLE.value else -1
    if not db.scalar(select(AdCreative.id).where(AdCreative.tenant_id == org.id).limit(1)):
        for platform, dims, status, ctr in (
            ("google_ads", "300x250", "live", 1.8),
            ("meta_ads", "1080x1080", "live", 2.1),
            ("linkedin_ads", "1200x627", "queued", 0.9),
            ("google_ads", "728x90", "generated", 0.0),
        ):
            db.add(
                AdCreative(
                    tenant_id=org.id,
                    platform=platform,
                    dimensions=dims,
                    headline_encrypted=cipher.encrypt(
                        "Book a trial class in Noida", context=Ctx.CREATIVE_HEADLINE
                    ),
                    body_copy_encrypted=cipher.encrypt(
                        "Small groups. A written plan after class one.",
                        context=Ctx.CREATIVE_BODY,
                    ),
                    call_to_action="Book now",
                    source_page_url=f"{SITE}/personal-training",
                    source_product="Personal training",
                    audience_segment="Professionals nearby",
                    status=status,
                    impressions=18000 if status == "live" else 0,
                    clicks=320 if status == "live" else 0,
                    ctr=ctr,
                )
            )
    if not db.scalar(select(AudienceCluster.id).where(AudienceCluster.tenant_id == org.id).limit(1)):
        db.add(
            AudienceCluster(
                tenant_id=org.id,
                label="Weekday professionals",
                size=6400,
                cohesion=0.72,
                top_signals=["evening class search", "trial class page"],
                activated_channels=["google", "meta"],
                is_live=True,
                refreshed_at=utcnow() - timedelta(days=2),
            )
        )
        db.add(
            AudienceCluster(
                tenant_id=org.id,
                label="Weekend beginners",
                size=2100,
                cohesion=0.61,
                top_signals=["first session article"],
                activated_channels=[],
                is_live=False,
                refreshed_at=utcnow() - timedelta(days=6),
            )
        )
    if not db.scalar(select(FraudEvent.id).where(FraudEvent.tenant_id == org.id).limit(1)):
        for days, source, reason, saved in (
            (1, "display-network.example", "Datacentre clicks", 38.0),
            (2, "rewarded-app.example", "Incentivised traffic", 22.0),
            (5, "hidden-placement.example", "Below-fold stacked ads", 14.5),
        ):
            db.add(
                FraudEvent(
                    tenant_id=org.id,
                    at=_at(-days, 14),
                    source=source,
                    reason=reason,
                    action="Blocked",
                    channel=AdChannel.GOOGLE.value,
                    impressions_blocked=12000,
                    spend_saved=saved,
                    signal={"sample": True},
                )
            )
    return 1


def _offpage(db: Session, org: Organization, cipher: OrgCipher) -> int:
    if db.scalar(select(BacklinkTarget.id).where(BacklinkTarget.tenant_id == org.id).limit(1)):
        return 0
    created = 0
    specs = [
        ("noidahealth.example", 54, "Guest Post", BacklinkStatus.WON.value, True),
        ("delhifit.example", 61, "Resource Page", BacklinkStatus.PITCHED.value, False),
        ("weekendwellness.example", 43, "Directory", BacklinkStatus.DISCOVERED.value, False),
        ("coachdigest.example", 58, "Guest Post", BacklinkStatus.REPLIED.value, False),
    ]
    for domain, authority, placement, status, won in specs:
        target = BacklinkTarget(
            tenant_id=org.id,
            domain=domain,
            authority=authority,
            placement_type=placement,
            status=status,
            relevance=0.81 if won else 0.64,
            contact_email=f"editor@{domain}",
            discovered_via="sample data",
            notes="Sample placement for the off-page screen.",
            won_at=utcnow() - timedelta(days=6) if won else None,
        )
        db.add(target)
        db.flush()
        db.add(
            OutreachPitch(
                tenant_id=org.id,
                target_id=target.id,
                target_domain=domain,
                subject_encrypted=cipher.encrypt(
                    f"A local beginner guide your readers would use — {domain}",
                    context=Ctx.PITCH_SUBJECT,
                ),
                body_encrypted=cipher.encrypt(
                    "Sample pitch: a practical first-session guide written for Noida readers.",
                    context=Ctx.PITCH_BODY,
                ),
                tone="professional",
                sentiment_score=0.22,
                variant="A",
                status="sent" if status != BacklinkStatus.DISCOVERED.value else "drafted",
                sent_at=utcnow() - timedelta(days=4) if status != BacklinkStatus.DISCOVERED.value else None,
            )
        )
        created += 1
    db.add(
        CompetitorAlert(
            tenant_id=org.id,
            competitor="Cult.fit",
            source_domain="noidahealth.example",
            authority=54,
            text="A competitor earned a mention on a local health blog.",
            detected_at=utcnow() - timedelta(days=3),
            counter_launched=True,
        )
    )
    return created


def _local(db: Session, org: Organization, cipher: OrgCipher) -> int:
    if db.scalar(select(LocalRankCheck.id).where(LocalRankCheck.tenant_id == org.id).limit(1)):
        return 0
    for days, phrase, position in (
        (0, "gym in noida", 3),
        (0, "personal trainer noida", 5),
        (1, "beginner fitness class noida", 2),
        (3, "gym in noida", 4),
    ):
        db.add(
            LocalRankCheck(
                tenant_id=org.id,
                checked_at=_at(-days, 11),
                phrase=phrase,
                position=position,
                results_seen=20,
                radius_m=5000,
                place_id="sample-place",
                business_name="Test Workspace",
                rating=4.6,
                review_count=128,
                top_results=[
                    {"name": "Cult.fit Noida", "position": 1, "rating": 4.5, "review_count": 900},
                    {"name": "Gold's Gym", "position": 2, "rating": 4.3, "review_count": 500},
                    {"name": "Test Workspace", "position": position, "rating": 4.6, "review_count": 128},
                ],
            )
        )
    for i, (stars, reviewer, status) in enumerate(
        ((5, "Asha K.", "posted"), (4, "Rahul S.", "drafted"), (2, "Priya M.", "drafted")),
        start=1,
    ):
        db.add(
            ReviewReply(
                tenant_id=org.id,
                review_name=f"accounts/sample/locations/1/reviews/r{i}",
                reviewer=reviewer,
                stars=stars,
                review_time=utcnow() - timedelta(days=i * 2),
                review_encrypted=cipher.encrypt(
                    "Sample review: the coaches explained every movement."
                    if stars >= 4
                    else "Sample review: the evening class started late.",
                    context=Ctx.REVIEW_TEXT,
                ),
                reply_encrypted=cipher.encrypt(
                    "Thank you — we will keep the groups small."
                    if stars >= 4
                    else "Thank you for telling us. The 7pm class now has a door person.",
                    context=Ctx.REVIEW_REPLY,
                ),
                status=status,
                posted_at=utcnow() - timedelta(days=1) if status == "posted" else None,
            )
        )
    return 1


def _creative(db: Session, org: Organization, user: User, cipher: OrgCipher) -> int:
    if db.scalar(select(BrandKit.id).where(BrandKit.tenant_id == org.id).limit(1)):
        return 0
    kit = BrandKit(
        tenant_id=org.id,
        name="Test Workspace kit",
        is_default=True,
        primary_color="#0f766e",
        secondary_color="#134e4a",
        accent_color="#f59e0b",
        text_color="#111827",
        background_color="#ffffff",
        heading_font="montserrat",
        body_font="inter",
        voice="Warm, specific, never shouty.",
        banned_phrases=["guaranteed abs", "miracle"],
        disclaimer="Results vary. This is sample studio copy.",
    )
    db.add(kit)
    product = Product(
        tenant_id=org.id,
        name="Trial class",
        description="A 50-minute session that ends with a written plan.",
        url=f"{SITE}/contact",
        price_text="Complimentary",
        approved_claims=["Small groups", "A written plan after class one"],
        disclaimer="Sample product for Creative Studio.",
    )
    db.add(product)
    db.flush()
    db.add(
        CreativeJob(
            tenant_id=org.id,
            created_by=user.id,
            status=CreativeJobStatus.SUCCEEDED,
            brand_kit_id=kit.id,
            product_id=product.id,
            formats=["banner_300x250", "instagram_feed"],
            text_connector="anthropic_claude",
            text_model="claude-sonnet-4-5",
            brief_encrypted=cipher.encrypt(
                "Announce the Saturday trial class for professionals in Noida.",
                context=Ctx.CREATIVE_BRIEF,
            ),
            revision_count=1,
            outputs=[{"format": "banner_300x250", "qa": "pass"}],
            tokens_in=2400,
            tokens_out=800,
            images_generated=1,
            started_at=utcnow() - timedelta(hours=5),
            finished_at=utcnow() - timedelta(hours=5, minutes=-4),
        )
    )
    return 1


def _approvals(db: Session, org: Organization, user: User, cipher: OrgCipher) -> int:
    if db.scalar(select(ApprovalItem.id).where(ApprovalItem.tenant_id == org.id).limit(1)):
        return 0
    page = db.execute(
        select(SeoPage).where(
            SeoPage.tenant_id == org.id, SeoPage.status == PageStatus.FLAGGED.value
        )
    ).scalars().first()
    items = [
        (
            ApprovalType.CONTENT_REWRITE.value,
            "Rewrite Personal training for Noida searchers",
            "on_page_seo_sync",
            "On-Page SEO Sync",
            "seo_page",
            page.id if page else None,
            ApprovalStatus.PENDING.value,
            {"url": f"{SITE}/personal-training"},
        ),
        (
            ApprovalType.PR_PITCH.value,
            "Pitch Delhi Fit a beginner session guide",
            "digital_pr_outreach",
            "Digital PR Outreach",
            "outreach_pitch",
            None,
            ApprovalStatus.PENDING.value,
            {"domain": "delhifit.example"},
        ),
        (
            ApprovalType.AD_CREATIVE.value,
            "LinkedIn banner: trial class",
            "dynamic_creative_optimizer",
            "Dynamic Creative Optimizer",
            "ad_creative",
            None,
            ApprovalStatus.APPROVED.value,
            {"platform": "linkedin_ads"},
        ),
        (
            ApprovalType.AEO_INJECTION.value,
            "Add a trial-class Q&A to the home page",
            "aeo_qa_injector",
            "AEO Q&A Injector",
            "aeo_pair",
            None,
            ApprovalStatus.REJECTED.value,
            {"page": SITE},
        ),
    ]
    for type_, title, slug, name, kind, target, status, payload in items:
        submitted = utcnow() - timedelta(hours=6)
        row = ApprovalItem(
            tenant_id=org.id,
            type=type_,
            title=title,
            agent_slug=slug,
            agent_name=name,
            target_kind=kind,
            target_id=target,
            payload_encrypted=cipher.encrypt_json(payload, context=Ctx.APPROVAL_PAYLOAD),
            payload_digest=approvals.payload_digest(payload),
            meta="sample data",
            status=status,
            submitted_at=submitted,
        )
        if status != ApprovalStatus.PENDING.value:
            row.decided_at = submitted + timedelta(hours=2)
            row.decided_by = user.name
            row.decided_by_id = user.id
            row.decision_note = "Sample decision for the queue history."
        db.add(row)
    return len(items)


def _site_analysis(db: Session, org: Organization, user: User) -> int:
    if db.scalar(select(SiteAnalysis.id).where(SiteAnalysis.tenant_id == org.id).limit(1)):
        return 0
    now = utcnow()
    report: dict[str, Any] = {
        "website": SITE,
        "generated_at": now.isoformat(),
        "duration_s": 42,
        "overall": {"score": 76, "grade": "C"},
        "categories": {
            "technical": {"label": "Technical health", "about": "Crawl and index", "score": 82, "passed": 8, "total": 10},
            "content": {"label": "Content", "about": "What the pages say", "score": 71, "passed": 5, "total": 7},
            "local": {"label": "Local", "about": "Maps and NAP", "score": 68, "passed": 3, "total": 5},
        },
        "summary": {"checks": 22, "passed": 16, "warnings": 4, "failed": 2, "pages_read": 6, "links_checked": 18},
        "checks": [
            {"id": "https", "category": "technical", "title": "HTTPS", "status": "pass", "evidence": "The site is served over HTTPS.", "why": "Browsers mark HTTP as not secure.", "fix": "Keep the certificate renewed."},
            {"id": "meta", "category": "content", "title": "Meta descriptions", "status": "warn", "evidence": "Two service pages reuse the home description.", "why": "Search listings look identical.", "fix": "Write a unique description per service."},
            {"id": "nap", "category": "local", "title": "Name, address, phone", "status": "fail", "evidence": "The footer phone number does not match the contact page.", "why": "Maps listings trust a consistent NAP.", "fix": "Use one phone number everywhere."},
        ],
        "measured": {"pagespeed": {"measured": True, "scores": {"performance": 64, "seo": 88}}, "trackers": ["Google Analytics 4"], "platform": "WordPress"},
        "ai": {
            "model": "sample",
            "strategy": {
                "business": {"name": "Test Workspace", "industry": "Fitness studio"},
                "executive_summary": "The site is findable. The trial class is the offer to put on every service page.",
                "top_priorities": [{"title": "One phone number", "why": "Local listings", "impact": "high"}],
                "keyword_opportunities": [{"keyword": "gym in noida", "intent": "local", "why": "already ranking on page one", "target_page": "/"}],
                "roadmap": {"first_30_days": ["Fix NAP", "Unique meta descriptions"]},
            },
            "creative": {},
        },
        "automation": [
            {"agent": "technical_seo_auditor", "name": "Technical SEO Auditor", "what_it_does": "Keeps crawl issues on a list you can work through.", "addresses": ["HTTPS", "Meta descriptions"]},
            {"agent": "local_search_tracker", "name": "Local Search Tracker", "what_it_does": "Watches Maps positions for the phrases that matter.", "addresses": ["Name, address, phone"]},
        ],
        "notes": ["This report is sample data for the test workspace."],
    }
    db.add(
        SiteAnalysis(
            tenant_id=org.id,
            requested_by=user.id,
            requested_by_name=user.name,
            website_url=SITE,
            business_url="https://maps.google.com/?q=Test+Workspace+Noida",
            status=DemoStatus.DONE,
            progress=[
                {"key": "crawl", "label": "Read the site", "state": "done", "detail": "6 pages"},
                {"key": "checks", "label": "Ran the checks", "state": "done", "detail": "22 checks"},
                {"key": "report", "label": "Wrote the report", "state": "done", "detail": ""},
            ],
            report=report,
            context={"connectors": [{"name": "WordPress", "slug": "wordpress"}], "sample": True},
            model="sample",
            started_at=now - timedelta(minutes=8),
            finished_at=now - timedelta(minutes=7),
        )
    )
    return 1


def _spam(db: Session, org: Organization) -> int:
    if db.scalar(select(ReferralSpamEvent.id).where(ReferralSpamEvent.tenant_id == org.id).limit(1)):
        return 0
    for days, referrer, category in (
        (1, "free-traffic.example", "known_spam"),
        (2, "button-directory.example", "injected_referrer"),
        (4, "casino-rank.example", "known_spam"),
    ):
        db.add(
            ReferralSpamEvent(
                tenant_id=org.id,
                at=_at(-days, 16),
                referrer=referrer,
                category=category,
                action="Blocked",
                sessions_affected=14,
                confidence=0.94,
            )
        )
    return 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Load sample data into the test workspace")
    parser.add_argument("--email", default=EMAIL)
    parser.add_argument("--workspace", default=WORKSPACE, help="organisation slug")
    parser.add_argument("--force", action="store_true", help="write again if sample data is already there")
    return parser


if __name__ == "__main__":
    from app.core.logging import configure_logging

    configure_logging()
    raise SystemExit(cmd_seed_demo(build_parser().parse_args()))
