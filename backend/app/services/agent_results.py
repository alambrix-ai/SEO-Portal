"""What each agent has produced, as one table per agent.

Every agent writes its output to its own tables (pages, issues, Q&A pairs,
schema patches, pitches, creatives, blocks …). Those were spread across five
screens — and pitch drafts were on none of them — so "what did this agent
actually do?" had no single answer. This module answers it per agent, from
those tables, in one generic shape the console can render without knowing
any agent.

Each builder reads only rows that agent writes (see the ``*(`` constructors in
each agent package). Budget rows are the exception — created at setup and
editable by hand — so that table is titled as the current split, not as the
agent's work.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.money import money
from app.models.ads import CHANNEL_LABELS, AdCreative, AudienceCluster, BudgetAllocation, FraudEvent
from app.models.budget import CrossChannelBudgetProposal
from app.models.competitor_ads import CompetitorAd
from app.models.content_refresh import ContentRefreshCandidate
from app.models.internal_link import InternalLinkSuggestion
from app.models.landing import LandingPage
from app.models.locations import Location
from app.models.offpage import BacklinkTarget, CompetitorAlert, OutreachPitch
from app.models.product_feed import ProductFeedFinding
from app.models.pseo import PseoPageJob
from app.models.repurpose import ContentRepurposeJob
from app.models.seo import (
    PAGE_STATUS_LABELS,
    AeoQaPair,
    ReferralSpamEvent,
    SchemaPatch,
    SeoIssue,
    SeoPage,
)
from app.models.seo_fix import SeoIssueFixProposal
from app.models.social_mentions import SocialMention
from app.models.topic_cluster import TopicCluster
from app.models.video_script import VideoScriptJob
from app.models.visibility import VisibilityObservation
from app.services.encryption import Ctx, OrgCipher

ROW_LIMIT = 25


@dataclass(slots=True)
class Column:
    key: str
    label: str
    #: text | status | number | muted
    kind: str = "text"


@dataclass(slots=True)
class Row:
    cells: dict[str, str]
    #: Tone of each status cell: success | warning | danger | neutral | info.
    tones: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Results:
    title: str
    description: str
    metrics: list[tuple[str, str]]
    columns: list[Column]
    rows: list[Row]
    total: int
    empty: str
    link: str = ""
    link_label: str = ""


def _when(moment) -> str:  # noqa: ANN001
    return moment.strftime("%d %b %Y, %H:%M") if moment else ""


def _count(db: Session, model, tenant_id: str, *conditions) -> int:  # noqa: ANN001
    return int(
        db.execute(
            select(func.count()).select_from(model).where(model.tenant_id == tenant_id, *conditions)
        ).scalar_one()
    )


def _decrypt(cipher: OrgCipher, value: str, context: Ctx) -> str:
    if not value:
        return ""
    try:
        return cipher.decrypt(value, context=context)
    except Exception:  # noqa: BLE001 - one unreadable row must not hide the rest
        return "(could not be read)"


# ── SEO & AEO ────────────────────────────────────────────────────────────────
def _on_page(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    rows = db.execute(
        select(SeoPage).where(SeoPage.tenant_id == tenant_id).order_by(SeoPage.updated_at.desc()).limit(ROW_LIMIT)
    ).scalars()
    tones = {"flagged": "warning", "queued": "warning", "rewritten": "success", "live": "neutral"}
    by_status = dict(
        db.execute(
            select(SeoPage.status, func.count()).where(SeoPage.tenant_id == tenant_id).group_by(SeoPage.status)
        ).all()
    )
    total = sum(by_status.values())
    return Results(
        title="Pages it has collected and improved",
        description=(
            "Every page it has read from your website. Content gap runs from 0 to 100 — the higher it "
            "is, the more the page could be improved. Pages scoring 45 or more get a rewrite."
        ),
        metrics=[
            ("Pages collected", str(total)),
            ("Awaiting approval", str(by_status.get("queued", 0))),
            ("Rewritten", str(by_status.get("rewritten", 0))),
        ],
        columns=[
            Column("page", "Page"),
            Column("status", "Status", "status"),
            Column("gap", "Content gap", "number"),
            Column("words", "Words", "number"),
            Column("synced", "Last read", "muted"),
        ],
        rows=[
            Row(
                cells={
                    "page": page.title or page.url,
                    "page_sub": page.url,
                    "status": PAGE_STATUS_LABELS.get(page.status, page.status),
                    "gap": str(page.gap_score),
                    "words": str(page.word_count),
                    "synced": _when(page.last_synced_at),
                },
                tones={"status": tones.get(page.status, "neutral")},
            )
            for page in rows
        ],
        total=total,
        empty="No pages collected yet. They appear after its first run.",
        link="/seo",
        link_label="Open SEO & AEO",
    )


def _auditor(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    from app.services.views import issue_out

    rows = db.execute(
        select(SeoIssue)
        .where(SeoIssue.tenant_id == tenant_id)
        .order_by((SeoIssue.status == "open").desc(), SeoIssue.last_seen_at.desc())
        .limit(ROW_LIMIT)
    ).scalars()
    sev_tone = {"high": "danger", "medium": "warning", "low": "neutral"}
    status_label = {"open": "Open", "fixed": "Fixed", "ignored": "Ignored"}
    status_tone = {"open": "warning", "fixed": "success", "ignored": "neutral"}
    return Results(
        title="Problems it has found",
        description="Each problem comes with a recommended fix on the Technical SEO screen. Fixed means it stopped appearing.",
        metrics=[
            ("Open", str(_count(db, SeoIssue, tenant_id, SeoIssue.status == "open"))),
            ("High severity, open", str(_count(db, SeoIssue, tenant_id, SeoIssue.status == "open", SeoIssue.severity == "high"))),
            ("Fixed", str(_count(db, SeoIssue, tenant_id, SeoIssue.status == "fixed"))),
        ],
        columns=[
            Column("problem", "Problem"),
            Column("severity", "Severity", "status"),
            Column("status", "Status", "status"),
            Column("seen", "Found", "muted"),
        ],
        rows=[
            Row(
                cells={
                    "problem": out.kind_label,
                    "problem_sub": out.url,
                    "severity": out.severity.capitalize(),
                    "status": status_label.get(out.status, out.status),
                    "seen": _when(issue.first_seen_at),
                },
                tones={"severity": sev_tone.get(out.severity, "neutral"), "status": status_tone.get(out.status, "neutral")},
            )
            for issue in rows
            for out in [issue_out(issue)]
        ],
        total=_count(db, SeoIssue, tenant_id),
        empty="No problems recorded yet.",
        link="/technical",
        link_label="Open Technical SEO",
    )


def _aeo(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    rows = db.execute(
        select(AeoQaPair, SeoPage.url)
        .join(SeoPage, SeoPage.id == AeoQaPair.page_id)
        .where(AeoQaPair.tenant_id == tenant_id)
        .order_by(AeoQaPair.created_at.desc())
        .limit(ROW_LIMIT)
    ).all()
    citations = db.execute(
        select(func.coalesce(func.sum(AeoQaPair.citation_count), 0)).where(AeoQaPair.tenant_id == tenant_id)
    ).scalar_one()
    return Results(
        title="Questions and answers it has written",
        description="Written from your own page content. “On the page” means it has been published to that page.",
        metrics=[
            ("Written", str(_count(db, AeoQaPair, tenant_id))),
            ("On the page", str(_count(db, AeoQaPair, tenant_id, AeoQaPair.injected.is_(True)))),
            ("Mentions found in AI answers", str(int(citations))),
        ],
        columns=[
            Column("question", "Question"),
            Column("status", "Status", "status"),
            Column("mentions", "AI mentions", "number"),
        ],
        rows=[
            Row(
                cells={
                    "question": _decrypt(cipher, pair.question_encrypted, Ctx.QA_QUESTION),
                    "question_sub": url,
                    "status": "On the page" if pair.injected else "Not published yet",
                    "mentions": str(pair.citation_count),
                },
                tones={"status": "success" if pair.injected else "warning"},
            )
            for pair, url in rows
        ],
        total=_count(db, AeoQaPair, tenant_id),
        empty="No questions written yet.",
        link="/seo",
        link_label="Open SEO & AEO",
    )


def _schema(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    rows = db.execute(
        select(SchemaPatch, SeoPage.url)
        .join(SeoPage, SeoPage.id == SchemaPatch.page_id)
        .where(SchemaPatch.tenant_id == tenant_id)
        .order_by(SchemaPatch.created_at.desc())
        .limit(ROW_LIMIT)
    ).all()
    label = {"queued": "Not published yet", "injected": "Published", "rejected": "Not used"}
    tone = {"queued": "warning", "injected": "success", "rejected": "neutral"}
    return Results(
        title="Structured data it has prepared",
        description="“Not used” covers versions that were rejected or replaced by a newer one.",
        metrics=[
            ("Published", str(_count(db, SchemaPatch, tenant_id, SchemaPatch.status == "injected"))),
            ("Not published yet", str(_count(db, SchemaPatch, tenant_id, SchemaPatch.status == "queued"))),
        ],
        columns=[
            Column("type", "Type"),
            Column("status", "Status", "status"),
            Column("when", "Published", "muted"),
        ],
        rows=[
            Row(
                cells={
                    "type": patch.schema_type,
                    "type_sub": url,
                    "status": label.get(patch.status, patch.status),
                    "when": _when(patch.injected_at),
                },
                tones={"status": tone.get(patch.status, "neutral")},
            )
            for patch, url in rows
        ],
        total=_count(db, SchemaPatch, tenant_id),
        empty="No structured data prepared yet.",
        link="/seo",
        link_label="Open SEO & AEO",
    )


def _spam(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    rows = db.execute(
        select(ReferralSpamEvent)
        .where(ReferralSpamEvent.tenant_id == tenant_id)
        .order_by(ReferralSpamEvent.at.desc())
        .limit(ROW_LIMIT)
    ).scalars()
    sessions = db.execute(
        select(func.coalesce(func.sum(ReferralSpamEvent.sessions_affected), 0)).where(
            ReferralSpamEvent.tenant_id == tenant_id
        )
    ).scalar_one()
    return Results(
        title="Spam websites it has blocked",
        description=(
            "Sites that sent fake or junk visits. Add them to your analytics filters "
            "(in GA4: Admin → Data streams → Configure tag settings → List unwanted referrals)."
        ),
        metrics=[
            ("Sites blocked", str(_count(db, ReferralSpamEvent, tenant_id))),
            ("Junk visits involved", str(int(sessions))),
        ],
        columns=[
            Column("site", "Website"),
            Column("category", "Type"),
            Column("visits", "Visits", "number"),
            Column("when", "Blocked", "muted"),
        ],
        rows=[
            Row(
                cells={
                    "site": event.referrer,
                    "category": event.category,
                    "visits": str(event.sessions_affected),
                    "when": _when(event.at),
                }
            )
            for event in rows
        ],
        total=_count(db, ReferralSpamEvent, tenant_id),
        empty="No spam sites blocked yet.",
        link="/seo",
        link_label="Open SEO & AEO",
    )


# ── Off-page ─────────────────────────────────────────────────────────────────
_TARGET_TONE = {"Discovered": "info", "Pitched": "warning", "Replied": "warning", "Won": "success", "Rejected": "neutral"}


def _discovery(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    mine = BacklinkTarget.discovered_via != "competitor gap"
    rows = db.execute(
        select(BacklinkTarget)
        .where(BacklinkTarget.tenant_id == tenant_id, mine)
        .order_by(BacklinkTarget.created_at.desc())
        .limit(ROW_LIMIT)
    ).scalars()
    return Results(
        title="Websites it has suggested",
        description=(
            "Suggestions only — nothing is sent to these sites by this agent. Relevance is how well "
            "the site fits your topics. Authority is measured only when DataForSEO is connected."
        ),
        metrics=[
            ("Suggested", str(_count(db, BacklinkTarget, tenant_id, mine))),
            ("Links won", str(_count(db, BacklinkTarget, tenant_id, mine, BacklinkTarget.status == "Won"))),
        ],
        columns=[
            Column("site", "Website"),
            Column("type", "Placement"),
            Column("relevance", "Relevance", "number"),
            Column("status", "Status", "status"),
            Column("when", "Suggested", "muted"),
        ],
        rows=[
            Row(
                cells={
                    "site": target.domain,
                    "type": target.placement_type,
                    "relevance": f"{round(target.relevance * 100)}%",
                    "status": target.status,
                    "when": _when(target.created_at),
                },
                tones={"status": _TARGET_TONE.get(target.status, "neutral")},
            )
            for target in rows
        ],
        total=_count(db, BacklinkTarget, tenant_id, mine),
        empty="No websites suggested yet.",
        link="/offpage",
        link_label="Open Off-Page & PR",
    )


def _pitches(db: Session, tenant_id: str, cipher: OrgCipher, *, counter: bool) -> list[Row]:
    rows = db.execute(
        select(OutreachPitch)
        .where(OutreachPitch.tenant_id == tenant_id, OutreachPitch.is_counter_pitch.is_(counter))
        .order_by(OutreachPitch.created_at.desc())
        .limit(ROW_LIMIT)
    ).scalars()
    label = {
        "drafted": "Waiting for approval",
        "sent": "Emailed to the publisher",
        "team_draft": "Draft sent to your team",
        "rejected": "Rejected",
        "expired": "Approval expired",
    }
    tone = {
        "drafted": "warning",
        "sent": "success",
        "team_draft": "info",
        "rejected": "neutral",
        "expired": "neutral",
    }
    return [
        Row(
            cells={
                "site": pitch.target_domain,
                "subject": _decrypt(cipher, pitch.subject_encrypted, Ctx.PITCH_SUBJECT),
                "status": label.get(pitch.status, pitch.status.capitalize()),
                "when": _when(pitch.sent_at or pitch.created_at),
            },
            tones={"status": tone.get(pitch.status, "neutral")},
        )
        for pitch in rows
    ]


_PITCH_COLUMNS = [
    Column("site", "Website"),
    Column("subject", "Subject"),
    Column("status", "Status", "status"),
    Column("when", "Date", "muted"),
]


def _outreach(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    mine = OutreachPitch.is_counter_pitch.is_(False)
    return Results(
        title="Pitches it has written",
        description=(
            "A pitch is emailed to the publisher only when you have added their contact address "
            "on the Off-Page screen and an email (SMTP) integration is connected. Otherwise the "
            "draft is sent to your team to forward. Drafts waiting for approval are under Approvals."
        ),
        metrics=[
            ("Written", str(_count(db, OutreachPitch, tenant_id, mine))),
            ("Emailed to publishers", str(_count(db, OutreachPitch, tenant_id, mine, OutreachPitch.status == "sent"))),
            ("Drafts sent to your team", str(_count(db, OutreachPitch, tenant_id, mine, OutreachPitch.status == "team_draft"))),
        ],
        columns=_PITCH_COLUMNS,
        rows=_pitches(db, tenant_id, cipher, counter=False),
        total=_count(db, OutreachPitch, tenant_id, mine),
        empty="No pitches written yet.",
        link="/approvals",
        link_label="Open Approvals",
    )


def _competitor(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    rows = db.execute(
        select(CompetitorAlert)
        .where(CompetitorAlert.tenant_id == tenant_id)
        .order_by(CompetitorAlert.detected_at.desc())
        .limit(ROW_LIMIT)
    ).scalars()
    return Results(
        title="Linking websites it has recorded",
        description=(
            "Websites that link to a competitor but not to you, from DataForSEO backlink data. "
            "Each can be answered with a counter-pitch."
        ),
        metrics=[
            ("Recorded", str(_count(db, CompetitorAlert, tenant_id))),
            ("Counter-pitches sent", str(_count(db, CompetitorAlert, tenant_id, CompetitorAlert.counter_launched.is_(True)))),
        ],
        columns=[
            Column("site", "Linking website"),
            Column("competitor", "Competitor"),
            Column("status", "Counter-pitch", "status"),
            Column("when", "Recorded", "muted"),
        ],
        rows=[
            Row(
                cells={
                    "site": alert.source_domain,
                    "competitor": alert.competitor,
                    "status": "Sent" if alert.counter_launched else "Not yet",
                    "when": _when(alert.detected_at),
                },
                tones={"status": "success" if alert.counter_launched else "neutral"},
            )
            for alert in rows
        ],
        total=_count(db, CompetitorAlert, tenant_id),
        empty="Nothing recorded yet.",
        link="/offpage",
        link_label="Open Off-Page & PR",
    )


# ── Ads ──────────────────────────────────────────────────────────────────────
def _creative(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    rows = db.execute(
        select(AdCreative).where(AdCreative.tenant_id == tenant_id).order_by(AdCreative.created_at.desc()).limit(ROW_LIMIT)
    ).scalars()
    label = {"generated": "Not published yet", "live": "Published", "rejected": "Rejected"}
    tone = {"generated": "warning", "live": "success", "rejected": "neutral"}
    return Results(
        title="Ads it has written",
        description="Text ads written from your product pages. “Published” means uploaded to that ad platform.",
        metrics=[
            ("Written", str(_count(db, AdCreative, tenant_id))),
            ("Published", str(_count(db, AdCreative, tenant_id, AdCreative.status == "live"))),
        ],
        columns=[
            Column("headline", "Headline"),
            Column("platform", "Platform"),
            Column("status", "Status", "status"),
            Column("when", "Written", "muted"),
        ],
        rows=[
            Row(
                cells={
                    "headline": _decrypt(cipher, creative.headline_encrypted, Ctx.CREATIVE_HEADLINE),
                    "headline_sub": creative.source_page_url,
                    "platform": creative.platform,
                    "status": label.get(creative.status, creative.status),
                    "when": _when(creative.created_at),
                },
                tones={"status": tone.get(creative.status, "neutral")},
            )
            for creative in rows
        ],
        total=_count(db, AdCreative, tenant_id),
        empty="No ads written yet.",
        link="/ads",
        link_label="Open Paid Media",
    )


def _budget(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    rows = list(
        db.execute(
            select(BudgetAllocation).where(BudgetAllocation.tenant_id == tenant_id).order_by(BudgetAllocation.percent.desc())
        ).scalars()
    )
    return Results(
        title="Current budget split",
        description=(
            "The split as it stands now — this agent changes it, and so can anyone on the Paid Media "
            "screen. Spend and conversions are from the last 7 days it read."
        ),
        metrics=[("Platforms", str(len(rows))), ("Locked", str(sum(1 for r in rows if r.locked)))],
        columns=[
            Column("channel", "Platform"),
            Column("share", "Share", "number"),
            Column("spend", "Spend", "number"),
            Column("conversions", "Conversions", "number"),
            Column("cac", "Cost per conversion", "number"),
            Column("locked", "Locked", "status"),
        ],
        rows=[
            Row(
                cells={
                    "channel": CHANNEL_LABELS.get(row.channel, row.channel.title()),
                    "share": f"{row.percent}%",
                    "spend": money(row.spend),
                    "conversions": str(row.conversions),
                    "cac": money(row.cac, decimals=2) if row.cac else "—",
                    "locked": "Locked" if row.locked else "No",
                },
                tones={"locked": "warning" if row.locked else "neutral"},
            )
            for row in rows
        ],
        total=len(rows),
        empty="No budget platforms set up yet.",
        link="/ads",
        link_label="Open Paid Media",
    )


def _audiences(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    rows = db.execute(
        select(AudienceCluster).where(AudienceCluster.tenant_id == tenant_id).order_by(AudienceCluster.size.desc()).limit(ROW_LIMIT)
    ).scalars()
    return Results(
        title="Customer groups it has built",
        description="“Live” groups are current. Platforms lists where the audience has been created.",
        metrics=[
            ("Groups", str(_count(db, AudienceCluster, tenant_id))),
            ("Live", str(_count(db, AudienceCluster, tenant_id, AudienceCluster.is_live.is_(True)))),
        ],
        columns=[
            Column("group", "Group"),
            Column("size", "People", "number"),
            Column("platforms", "Platforms"),
            Column("status", "Status", "status"),
        ],
        rows=[
            Row(
                cells={
                    "group": cluster.label,
                    "size": f"{cluster.size:,}",
                    "platforms": ", ".join(cluster.activated_channels or []) or "None yet",
                    "status": "Live" if cluster.is_live else "Retired",
                },
                tones={"status": "success" if cluster.is_live else "neutral"},
            )
            for cluster in rows
        ],
        total=_count(db, AudienceCluster, tenant_id),
        empty="No customer groups built yet.",
        link="/ads",
        link_label="Open Paid Media",
    )


def _fraud(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    rows = db.execute(
        select(FraudEvent).where(FraudEvent.tenant_id == tenant_id).order_by(FraudEvent.at.desc()).limit(ROW_LIMIT)
    ).scalars()
    saved = db.execute(
        select(func.coalesce(func.sum(FraudEvent.spend_saved), 0)).where(FraudEvent.tenant_id == tenant_id)
    ).scalar_one()
    return Results(
        title="Sources it has blocked",
        description="Estimated spend saved is the blocked visits multiplied by an estimated cost per click.",
        metrics=[
            ("Sources blocked", str(_count(db, FraudEvent, tenant_id))),
            ("Estimated spend saved", money(float(saved))),
        ],
        columns=[
            Column("source", "Source"),
            Column("reason", "Why"),
            Column("channel", "Platform"),
            Column("when", "Blocked", "muted"),
        ],
        rows=[
            Row(
                cells={
                    "source": event.source,
                    "reason": event.reason,
                    "channel": CHANNEL_LABELS.get(event.channel, event.channel) or "—",
                    "when": _when(event.at),
                }
            )
            for event in rows
        ],
        total=_count(db, FraudEvent, tenant_id),
        empty="No sources blocked yet.",
        link="/ads",
        link_label="Open Paid Media",
    )


# ── Local business ───────────────────────────────────────────────────────────
def _stars(rating, count) -> str:  # noqa: ANN001
    if rating is None:
        return "No rating"
    reviews = f" ({count:,} reviews)" if count is not None else ""
    return f"{rating:.1f}★{reviews}"


def _local_search(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    from app.models.local import LocalRankCheck

    latest_at = db.execute(
        select(func.max(LocalRankCheck.checked_at)).where(LocalRankCheck.tenant_id == tenant_id)
    ).scalar_one()
    if latest_at is None:
        latest: list = []
    else:
        latest = list(
            db.execute(
                select(LocalRankCheck)
                .where(LocalRankCheck.tenant_id == tenant_id, LocalRankCheck.checked_at == latest_at)
                .order_by(LocalRankCheck.phrase)
            ).scalars()
        )
    previous: dict[str, int | None] = {}
    if latest_at is not None:
        prior_at = db.execute(
            select(func.max(LocalRankCheck.checked_at)).where(
                LocalRankCheck.tenant_id == tenant_id, LocalRankCheck.checked_at < latest_at
            )
        ).scalar_one()
        if prior_at is not None:
            previous = {
                row.phrase: row.position
                for row in db.execute(
                    select(LocalRankCheck).where(
                        LocalRankCheck.tenant_id == tenant_id, LocalRankCheck.checked_at == prior_at
                    )
                ).scalars()
            }

    def change(row) -> tuple[str, str]:  # noqa: ANN001
        if row.phrase not in previous:
            return "First check", "neutral"
        before, now = previous[row.phrase], row.position
        if before == now:
            return "No change", "neutral"
        if now is None:
            return "Dropped out of the top 20", "danger"
        if before is None:
            return "New in the top 20", "success"
        moved = before - now
        return (f"Up {moved}", "success") if moved > 0 else (f"Down {-moved}", "danger")

    def position(row) -> tuple[str, str]:  # noqa: ANN001
        if row.position is None:
            return f"Not in the top {row.results_seen or 20}", "warning"
        tone = "success" if row.position <= 3 else "info" if row.position <= 10 else "warning"
        return f"#{row.position} of {row.results_seen}", tone

    rows = []
    for row in latest:
        top = (row.top_results or [{}])[0] if row.top_results else {}
        first = (
            "You"
            if top.get("place_id") == row.place_id
            else f"{top.get('name', '—')} · {_stars(top.get('rating'), top.get('review_count'))}"
            if top
            else "—"
        )
        pos_text, pos_tone = position(row)
        change_text, change_tone = change(row)
        rows.append(
            Row(
                cells={"phrase": row.phrase, "position": pos_text, "change": change_text, "first": first},
                tones={"position": pos_tone, "change": change_tone},
            )
        )

    me = latest[0] if latest else None
    found = [r.position for r in latest if r.position is not None]
    return Results(
        title="Where you appear in Google Maps searches",
        description=(
            "Positions from Google's own Maps search results for each phrase, searched "
            f"around your business{f' within {me.radius_m // 1000} km' if me and me.radius_m >= 1000 else ''}. "
            "Each person's results vary a little with their exact location, so use this "
            "to see movement over time."
        ),
        metrics=[
            ("Your Google rating", _stars(me.rating, me.review_count) if me else "—"),
            ("Best position", f"#{min(found)}" if found else "Not in the top 20"),
            ("Searches tracked", str(len(latest))),
        ],
        columns=[
            Column("phrase", "Search"),
            Column("position", "Your position", "status"),
            Column("change", "Since last check", "status"),
            Column("first", "Listed first"),
        ],
        rows=rows,
        total=len(rows),
        empty="No searches checked yet. Results appear after its first run.",
    )


def _reviews(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    from app.models.local import ReviewReply

    rows = db.execute(
        select(ReviewReply)
        .where(ReviewReply.tenant_id == tenant_id)
        .order_by(ReviewReply.created_at.desc())
        .limit(ROW_LIMIT)
    ).scalars()
    label = {
        "drafted": "Waiting for approval",
        "posted": "Posted",
        "rejected": "Turned down",
        "discarded": "Draft failed the safety check",
    }
    tone = {"drafted": "warning", "posted": "success", "rejected": "neutral", "discarded": "neutral"}

    def clip(text: str, size: int = 140) -> str:
        text = " ".join(text.split())
        return text if len(text) <= size else text[: size - 1] + "…"

    return Results(
        title="Replies to your Google reviews",
        description=(
            "“Waiting for approval” replies are on the Approvals screen. A review is never "
            "replied to twice. A draft that failed the safety check is tried again after a week."
        ),
        metrics=[
            ("Posted", str(_count(db, ReviewReply, tenant_id, ReviewReply.status == "posted"))),
            ("Waiting for approval", str(_count(db, ReviewReply, tenant_id, ReviewReply.status == "drafted"))),
            ("Turned down", str(_count(db, ReviewReply, tenant_id, ReviewReply.status == "rejected"))),
        ],
        columns=[
            Column("review", "Review"),
            Column("stars", "Rating"),
            Column("reply", "Reply"),
            Column("status", "Status", "status"),
            Column("when", "Date", "muted"),
        ],
        rows=[
            Row(
                cells={
                    "review": reply.reviewer or "Anonymous",
                    "review_sub": clip(_decrypt(cipher, reply.review_encrypted, Ctx.REVIEW_TEXT)) or "(rating only)",
                    "stars": f"{reply.stars}★" if reply.stars else "—",
                    "reply": clip(_decrypt(cipher, reply.reply_encrypted, Ctx.REVIEW_REPLY), 160),
                    "status": label.get(reply.status, reply.status),
                    "when": _when(reply.posted_at or reply.created_at),
                },
                tones={"status": tone.get(reply.status, "neutral")},
            )
            for reply in rows
        ],
        total=_count(db, ReviewReply, tenant_id),
        empty="No replies written yet.",
        link="/approvals",
        link_label="Open Approvals",
    )


def _status_table(
    db: Session,
    tenant_id: str,
    *,
    model: type,
    title: str,
    description: str,
    empty: str,
    label_attr: str,
    order_attr: str = "created_at",
    status_attr: str = "status",
    link: str = "/agents",
    link_label: str = "Open Agents",
) -> Results:
    """Newest-first status table for roadmap agents that write one row type."""
    order_col = getattr(model, order_attr)
    rows = db.execute(
        select(model)
        .where(model.tenant_id == tenant_id)
        .order_by(order_col.desc())
        .limit(ROW_LIMIT)
    ).scalars()
    return Results(
        title=title,
        description=description,
        metrics=[("Recorded", str(_count(db, model, tenant_id)))],
        columns=[
            Column("item", "Item"),
            Column("status", "Status", "status"),
            Column("when", "When", "muted"),
        ],
        rows=[
            Row(
                cells={
                    "item": str(getattr(row, label_attr, "") or "")[:160],
                    "status": str(getattr(row, status_attr, "") or ""),
                    "when": _when(getattr(row, order_attr, None)),
                },
                tones={"status": "neutral"},
            )
            for row in rows
        ],
        total=_count(db, model, tenant_id),
        empty=empty,
        link=link,
        link_label=link_label,
    )


def _ai_visibility(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    return _status_table(
        db,
        tenant_id,
        model=VisibilityObservation,
        title="Answer-engine observations",
        description="Citations recorded when answer engines were asked your tracked questions.",
        empty="No observations yet. They appear after the first visibility run.",
        label_attr="engine",
        order_attr="observed_at",
        status_attr="engine",
        link="/visibility",
        link_label="Open AI Visibility",
    )


def _content_refresh(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    return _status_table(
        db,
        tenant_id,
        model=ContentRefreshCandidate,
        title="Pages flagged for refresh",
        description="Search Console pages with material click or ranking drops.",
        empty="No refresh candidates yet.",
        label_attr="url",
        link="/approvals",
        link_label="Open Approvals",
    )


def _technical_fix(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    return _status_table(
        db,
        tenant_id,
        model=SeoIssueFixProposal,
        title="Technical fix proposals",
        description="CMS or git fixes proposed for open audit findings.",
        empty="No fix proposals yet.",
        label_attr="fix_kind",
        link="/approvals",
        link_label="Open Approvals",
    )


def _internal_linking(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    return _status_table(
        db,
        tenant_id,
        model=InternalLinkSuggestion,
        title="Internal link suggestions",
        description="Proposed links from one synced page to a related page.",
        empty="No link suggestions yet.",
        label_attr="anchor",
        link="/approvals",
        link_label="Open Approvals",
    )


def _topic_clusters(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    return _status_table(
        db,
        tenant_id,
        model=TopicCluster,
        title="Topic clusters",
        description="Query and keyword clusters with coverage gaps.",
        empty="No topic clusters yet.",
        label_attr="name",
        link="/topics",
        link_label="Open Topics",
    )


def _pseo(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    return _status_table(
        db,
        tenant_id,
        model=PseoPageJob,
        title="Programmatic page jobs",
        description="Template renders proposed for CMS publish.",
        empty="No programmatic pages proposed yet.",
        label_attr="path",
        link="/approvals",
        link_label="Open Approvals",
    )


def _product_feed(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    return _status_table(
        db,
        tenant_id,
        model=ProductFeedFinding,
        title="Product feed findings",
        description="Merchant-ready gaps found on workspace products.",
        empty="No product feed findings yet.",
        label_attr="summary",
        link="/approvals",
        link_label="Open Approvals",
    )


def _content_repurpose(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    return _status_table(
        db,
        tenant_id,
        model=ContentRepurposeJob,
        title="Repurpose packs",
        description="Social, email and short-script packs drafted from approved articles.",
        empty="No repurpose packs yet.",
        label_attr="source_title",
        link="/approvals",
        link_label="Open Approvals",
    )


def _video_scripts(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    return _status_table(
        db,
        tenant_id,
        model=VideoScriptJob,
        title="Video scripts",
        description="Short-form scripts drafted for Creative Studio.",
        empty="No video scripts yet.",
        label_attr="title",
        link="/approvals",
        link_label="Open Approvals",
    )


def _cross_channel(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    return _status_table(
        db,
        tenant_id,
        model=CrossChannelBudgetProposal,
        title="Cross-channel budget proposals",
        description="Blended-ROAS reallocations awaiting approval.",
        empty="No budget proposals yet.",
        label_attr="rationale",
        link="/approvals",
        link_label="Open Approvals",
    )


def _landing_pages(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    return _status_table(
        db,
        tenant_id,
        model=LandingPage,
        title="Landing pages",
        description="Draft landing pages and experiment candidates.",
        empty="No landing pages yet.",
        label_attr="title",
        link="/approvals",
        link_label="Open Approvals",
    )


def _competitor_ads(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    return _status_table(
        db,
        tenant_id,
        model=CompetitorAd,
        title="Competitor ads seen",
        description="Public ad-library creatives captured for configured competitors.",
        empty="No competitor ads recorded yet.",
        label_attr="advertiser",
        order_attr="last_seen_at",
        status_attr="platform",
        link="/agents",
        link_label="Open Agents",
    )


def _locations(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    return _status_table(
        db,
        tenant_id,
        model=Location,
        title="Locations tracked",
        description="Active multi-location entities used for local rank checks.",
        empty="No locations configured yet.",
        label_attr="name",
        link="/agents",
        link_label="Open Agents",
    )


def _social_mentions(db: Session, tenant_id: str, cipher: OrgCipher) -> Results:
    return _status_table(
        db,
        tenant_id,
        model=SocialMention,
        title="Social mentions",
        description="Ingested mentions with sentiment and reply routing status.",
        empty="No social mentions yet.",
        label_attr="author",
        order_attr="observed_at",
        link="/approvals",
        link_label="Open Approvals",
    )


BUILDERS: dict[str, Callable[[Session, str, OrgCipher], Results]] = {
    "on_page_seo_sync": _on_page,
    "technical_seo_auditor": _auditor,
    "aeo_qa_injector": _aeo,
    "knowledge_graph_schema": _schema,
    "referral_spam_guard": _spam,
    "backlink_node_discovery": _discovery,
    "digital_pr_outreach": _outreach,
    "competitor_link_monitor": _competitor,
    "dynamic_creative_optimizer": _creative,
    "predictive_budget_engine": _budget,
    "first_party_audience_modeler": _audiences,
    "click_fraud_controller": _fraud,
    "local_search_tracker": _local_search,
    "review_responder": _reviews,
    "ai_visibility_tracker": _ai_visibility,
    "content_refresh": _content_refresh,
    "technical_fix_applier": _technical_fix,
    "internal_linking": _internal_linking,
    "topic_cluster_planner": _topic_clusters,
    "pseo_page_builder": _pseo,
    "product_feed_optimizer": _product_feed,
    "content_repurposer": _content_repurpose,
    "creative_video_scripts": _video_scripts,
    "cross_channel_budget": _cross_channel,
    "landing_page_builder": _landing_pages,
    "competitor_ad_watch": _competitor_ads,
    "multi_location_local_seo": _locations,
    "social_listening": _social_mentions,
}


def results_for(db: Session, *, slug: str, tenant_id: str, cipher: OrgCipher) -> Results | None:
    """Results for an agent: listed here, or declared as ``RESULTS`` in its package."""
    builder = BUILDERS.get(slug)
    if builder is None:
        import importlib

        try:
            builder = getattr(importlib.import_module(f"app.agents.{slug}"), "RESULTS", None)
        except ImportError:
            builder = None
    return builder(db, tenant_id, cipher) if builder else None


# ── Content agents: one table shape for every asset kind ────────────────────
def asset_results(
    db: Session,
    tenant_id: str,
    *,
    agent_slug: str,
    title: str,
    description: str,
    empty: str,
    channel_label: str = "Channel",
) -> Results:
    """The library rows one content agent produced, newest first.

    Content agents use this for their ``RESULTS`` so every one reads the same:
    what it is, where it goes, its status and when.
    """
    from app.api.v1.content import status_label
    from app.models.content import AssetStatus, MarketingAsset

    tone = {
        AssetStatus.DRAFT: "warning",
        AssetStatus.APPROVED: "info",
        AssetStatus.SCHEDULED: "info",
        AssetStatus.PUBLISHED: "success",
        AssetStatus.EXPORTED: "success",
        AssetStatus.REJECTED: "neutral",
        AssetStatus.FAILED: "danger",
    }
    mine = MarketingAsset.agent_slug == agent_slug
    rows = db.execute(
        select(MarketingAsset)
        .where(MarketingAsset.tenant_id == tenant_id, mine)
        .order_by(MarketingAsset.created_at.desc())
        .limit(ROW_LIMIT)
    ).scalars()
    return Results(
        title=title,
        description=description,
        metrics=[
            ("Written", str(_count(db, MarketingAsset, tenant_id, mine))),
            ("Waiting for approval", str(_count(db, MarketingAsset, tenant_id, mine, MarketingAsset.status == AssetStatus.DRAFT))),
            ("Sent to the platform", str(_count(db, MarketingAsset, tenant_id, mine, MarketingAsset.status == AssetStatus.PUBLISHED))),
        ],
        columns=[
            Column("title", "Title"),
            Column("channel", channel_label),
            Column("status", "Status", "status"),
            Column("when", "Date", "muted"),
        ],
        rows=[
            Row(
                cells={
                    "title": asset.title,
                    "title_sub": asset.error or asset.remote_url,
                    "channel": asset.channel.replace("_", " ").title(),
                    "status": status_label(asset.kind, asset.status),
                    "when": _when(asset.published_at or asset.scheduled_for or asset.created_at),
                },
                tones={"status": tone.get(asset.status, "neutral")},
            )
            for asset in rows
        ],
        total=_count(db, MarketingAsset, tenant_id, mine),
        empty=empty,
        link="/content",
        link_label="Open Content library",
    )
