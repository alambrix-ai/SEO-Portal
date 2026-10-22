"""Email Marketer — writes email campaigns from the brand profile.

Programs (the ``programs`` setting):

* **welcome** — a four-email welcome series (days 0, 2, 5 and 9 after sign-up),
  written once and written again only when the brand profile changes;
* **newsletter** — one per week or month, summarising the articles published
  or approved in the library during that period (skipped when there are none);
* **winback** and **promo** — one of each per calendar month while enabled.

Every email is a library asset proposed for approval at high impact.
Approving it creates a **draft** on the connected email platform (Mailchimp,
Brevo; on Klaviyo a template) — it is never sent to anyone from here. With no
email platform connected the email is download-only (HTML, text, .eml).
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta

from sqlalchemy import func, select

from app.agents.base.context import AgentContext
from app.agents.base.contracts import (
    AgentAction,
    AgentCategory,
    AgentResult,
    AgentSpec,
    BaseAgent,
    Impact,
)
from app.agents.base.contracts import ConnectorUse, UseMode
from app.agents.email_marketer import prompts, render
from app.agents.email_marketer.publishing import PLATFORM_NAMES
from app.connectors.base.connector import Capability
from app.db.base import utcnow
from app.models.content import AssetKind, AssetStatus, MarketingAsset
from app.services import assets, images

SLUG = "email_marketer"
PROGRAMS = ("welcome", "newsletter", "winback", "promo")
DEFAULT_PROGRAMS = "welcome,newsletter"
FREQUENCIES = ("weekly", "monthly")
#: Internal state, not a setting a person edits: the brand-profile fingerprint
#: the current welcome series was written from.
WELCOME_HASH_SETTING = "welcome_profile_hash"
#: Email clients fetch images when the email is opened, possibly months later,
#: so the signed image link is made to last about a year.
IMAGE_LINK_SECONDS = 400 * 24 * 3600
#: Articles summarised in one newsletter, at most.
NEWSLETTER_ARTICLES = 6

SEQUENCE_NAMES = {
    "welcome": "Welcome series",
    "newsletter": "Newsletter",
    "winback": "Win-back",
    "promo": "Promotion",
}
_PROFILE_KEYS = (
    "business_name", "summary", "industry", "tone", "language", "call_to_action", "products_services",
    "value_propositions", "differentiators", "audiences", "keywords", "locations", "source_url", "address",
)


# ── Planning helpers ───────────────────────────────────────────────────────
def parse_programs(raw: object) -> tuple[list[str], list[str]]:
    """The enabled programs, in canonical order, and any names not recognised."""
    text = str(raw or "").strip() or DEFAULT_PROGRAMS
    names = [part.strip().lower().replace("-", "").replace(" ", "") for part in text.split(",") if part.strip()]
    aliases = {"winback": "winback", "promotion": "promo", "promotions": "promo", "newsletters": "newsletter"}
    names = [aliases.get(name, name) for name in names]
    return [p for p in PROGRAMS if p in names], [n for n in names if n not in PROGRAMS]


def period_key(program: str, today: date, frequency: str) -> str:
    if program == "newsletter" and frequency == "weekly":
        year, week, _ = today.isocalendar()
        return f"{year}-W{week:02d}"
    return f"{today.year}-{today.month:02d}"


def profile_hash(profile: dict) -> str:
    material = {key: profile.get(key) for key in _PROFILE_KEYS}
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def brand_links(profile: dict, primary_domain: str = "") -> list[str]:
    """Real pages of the business's own site the button may link to."""
    links: list[str] = []
    for url in [profile.get("source_url") or "", *(profile.get("pages_read") or [])]:
        if render.normalise_url(str(url)) and url not in links:
            links.append(str(url))
    if not links and primary_domain:
        links.append(f"https://{primary_domain.strip().strip('/')}")
    return links


def email_platform(ctx: AgentContext):  # noqa: ANN201
    """The email platform campaigns are created in, or None.

    The one chosen in this agent's settings when it is still connected;
    otherwise the first connected platform the agent can write for.
    """
    for connector in ctx.candidates(Capability.CREATE_EMAIL_CAMPAIGN):
        if connector.slug in render.PLATFORMS:
            return connector
    return None


def _existing_keys(ctx: AgentContext) -> set[tuple[str, str]]:
    """(program, period) of the emails this agent already wrote recently."""
    rows = ctx.db.execute(
        select(MarketingAsset).where(
            MarketingAsset.tenant_id == ctx.tenant_id,
            MarketingAsset.agent_slug == SLUG,
            MarketingAsset.kind == AssetKind.EMAIL,
            MarketingAsset.created_at >= utcnow() - timedelta(days=45),
        )
    ).scalars()
    return {(str((r.detail or {}).get("program")), str((r.detail or {}).get("period"))) for r in rows}


def _recent_articles(ctx: AgentContext, days: int) -> list[dict]:
    """Articles published or approved in the library in the last ``days`` days."""
    since = utcnow() - timedelta(days=days)
    rows = ctx.db.execute(
        select(MarketingAsset)
        .where(
            MarketingAsset.tenant_id == ctx.tenant_id,
            MarketingAsset.kind == AssetKind.ARTICLE,
            MarketingAsset.status.in_([AssetStatus.PUBLISHED, AssetStatus.APPROVED]),
            func.coalesce(MarketingAsset.published_at, MarketingAsset.updated_at) >= since,
        )
        .order_by(func.coalesce(MarketingAsset.published_at, MarketingAsset.updated_at).desc())
        .limit(NEWSLETTER_ARTICLES)
    ).scalars()
    out = []
    for row in rows:
        content = assets.content_of(row, ctx.cipher)
        summary = next(
            (str(content.get(k) or "") for k in ("meta_description", "summary", "excerpt", "description") if content.get(k)),
            "",
        )
        url = row.remote_url if render.normalise_url(row.remote_url or "") else ""
        out.append({"title": row.title, "summary": " ".join(summary.split())[:300], "url": url})
    return out


# ── Agent ──────────────────────────────────────────────────────────────────
class EmailMarketerAgent(BaseAgent):
    spec = AgentSpec(
        slug=SLUG,
        name="Email Marketer",
        category=AgentCategory.CONTENT,
        description=(
            "Writes a welcome series, newsletters, win-back and promotional emails from your "
            "brand profile, as drafts for your email platform. It never sends email."
        ),
        default_interval=timedelta(days=7),
        default_schedule="Weekly",
        # It needs an AI model to write; requires_llm picks which one.
        any_of_capabilities=(Capability.COMPLETE,),
        max_impact=Impact.HIGH,
        default_max_actions_per_day=10,
        requires_llm=True,
        connector_use=(
            ConnectorUse(
                Capability.CREATE_EMAIL_CAMPAIGN,
                UseMode.ONE,
                question="Which email platform should campaigns be created in?",
            ),
            ConnectorUse(
                Capability.GENERATE_IMAGE,
                UseMode.ONE,
                question="Which AI service should create email images?",
            ),
        ),
    )

    def preflight(self, ctx: AgentContext) -> str | None:
        if ctx.with_capability(Capability.COMPLETE) is None:
            return (
                "Waiting on an AI model to write the emails — connect OpenAI, Anthropic Claude "
                "or Google Gemini under Integrations."
            )
        return super().preflight(ctx)

    def run(self, ctx: AgentContext) -> AgentResult:
        profile = ctx.brand_profile
        if not profile:
            return AgentResult.skip("Set up your brand profile first — every email is written from it.")
        links = brand_links(profile, getattr(ctx.org, "primary_domain", "") or "")
        if not links:
            return AgentResult.skip(
                "Your brand profile has no website address, so the emails would have no page to "
                "link to. Generate the profile from your website."
            )
        programs, unknown = parse_programs(ctx.setting("programs"))
        frequency = str(ctx.setting("newsletter_frequency") or "weekly").strip().lower()
        if frequency not in FREQUENCIES:
            frequency = "weekly"
        platform = email_platform(ctx)
        channel = platform.slug if platform is not None else "export"
        # An image connection without an image model chosen: the emails are
        # still written, without a header picture, and the run says why.
        image_problem = images.image_setup_problem(ctx)
        with_image = (
            ctx.with_capability(Capability.GENERATE_IMAGE) is not None
            and not image_problem
            and images.is_publicly_reachable()
        )

        notes: list[str] = []
        if image_problem:
            notes.append("emails have no header picture. " + image_problem)
        if unknown:
            notes.append(f"ignored unknown programs: {', '.join(unknown)}")
        budget = max(0, int(ctx.remaining_actions or 0))
        existing = _existing_keys(ctx)
        writer = _Writer(ctx, profile=profile, links=links, channel=channel, with_image=with_image)
        actions: list[AgentAction] = []

        if "welcome" in programs:
            fingerprint = profile_hash(profile)
            if ctx.setting(WELCOME_HASH_SETTING) == fingerprint:
                pass  # already written from this profile
            elif budget < len(prompts.WELCOME_SERIES):
                notes.append(
                    f"the welcome series needs {len(prompts.WELCOME_SERIES)} approvals and today's limit "
                    "has fewer left; it will be written on a later run"
                )
            else:
                written, problem = writer.welcome(fingerprint)
                if written:
                    actions += written
                    budget -= len(written)
                    ctx.set_setting(WELCOME_HASH_SETTING, fingerprint)
                else:
                    notes.append(f"the welcome series was not written ({problem}); it is tried again next run")

        for program in ("newsletter", "winback", "promo"):
            if program not in programs:
                continue
            period = period_key(program, ctx.today, frequency)
            if (program, period) in existing:
                continue
            if budget < 1:
                notes.append(f"the {SEQUENCE_NAMES[program].lower()} email waits for the next run (daily limit)")
                continue
            articles: list[dict] = []
            if program == "newsletter":
                articles = _recent_articles(ctx, 7 if frequency == "weekly" else 31)
                if not articles:
                    notes.append(f"no newsletter for {period}: no articles were published or approved in that time")
                    continue
            action, problem = writer.single(program, period, articles)
            if action is None:
                notes.append(f"the {SEQUENCE_NAMES[program].lower()} email was not written ({problem})")
                continue
            actions.append(action)
            budget -= 1

        if not actions:
            reason = "; ".join(notes) or "nothing new to write: every enabled program is up to date for this period"
            return AgentResult.skip(reason[:1].upper() + reason[1:])

        where = {
            "export": "approving one marks it ready to download (no email platform is connected)",
            "klaviyo": "approving one saves it as a template in Klaviyo — no campaign is created and nothing is sent",
        }.get(channel, f"approving one creates a draft campaign in {PLATFORM_NAMES.get(channel, channel)} — nothing is sent")
        summary = f"wrote {len(actions)} email{'s' if len(actions) != 1 else ''}; {where}"
        if notes:
            summary += ". Also: " + "; ".join(notes)
        return AgentResult(
            summary=summary,
            actions=actions,
            metric_label=f"{len(actions)} emails written",
            detail={"platform": channel, "programs": programs, "images": with_image},
        )


class _Writer:
    """Writes, validates and stores emails for one run."""

    def __init__(self, ctx: AgentContext, *, profile: dict, links: list[str], channel: str, with_image: bool) -> None:
        self.ctx = ctx
        self.profile = profile
        self.links = links
        self.channel = channel
        self.with_image = with_image
        self.brief = ctx.brand_brief
        self.facts = self.brief + "\n" + json.dumps({k: profile.get(k) for k in _PROFILE_KEYS}, ensure_ascii=False)

    # ── One email's copy ───────────────────────────────────────────────────
    def copy(self, brief: str, *, sequence: str = "", articles: list[dict] | None = None) -> tuple[dict | None, str]:
        links = list(self.links) + [a["url"] for a in articles or [] if a.get("url")]
        facts = self.facts + "\n" + json.dumps(articles or [], ensure_ascii=False)
        problems: list[str] = []
        for _attempt in range(2):  # one retry, told what was wrong
            try:
                raw = self.ctx.ask_json(
                    prompts.write_email(
                        brand_brief=self.brief,
                        brief=brief,
                        links=links,
                        articles=articles,
                        sequence=sequence,
                        previous_problems=problems or None,
                    ),
                    system=prompts.SYSTEM,
                    schema_hint=prompts.SCHEMA_HINT,
                )
            except Exception as exc:  # noqa: BLE001 - one failed call must not stop the run
                self.ctx.log_warning("Email draft failed: %s", exc)
                return None, "the AI model did not answer"
            clean, problems = render.validate(raw, allowed_urls=links, facts=facts, needs_alt=self.with_image)
            if not problems:
                return clean, ""
        self.ctx.log_warning("Discarded email draft: %s", "; ".join(problems))
        return None, problems[0]

    def _image(self, clean: dict) -> tuple[str, list[str]]:
        if not self.with_image:
            return "", []
        try:
            image = images.generate(
                self.ctx.db,
                self.ctx.org,
                prompts.image_prompt(
                    described=clean.get("image_prompt", ""),
                    business=self.profile.get("business_name") or getattr(self.ctx.org, "name", ""),
                    industry=self.profile.get("industry", ""),
                ),
                shape="landscape",
                provider=images.provider_for(self.ctx),
            )
        except Exception as exc:  # noqa: BLE001 - an email without a picture is still an email
            self.ctx.log_warning("Header image not created: %s", exc)
            return "", []
        return images.public_url(self.ctx.tenant_id, image.id, seconds=IMAGE_LINK_SECONDS), [image.id]

    # ── Storing ────────────────────────────────────────────────────────────
    def store(self, clean: dict, *, program: str, title: str, detail: dict) -> AgentAction:
        ctx = self.ctx
        image_url, image_ids = self._image(clean)
        content = {
            "program": program,
            "sequence": SEQUENCE_NAMES[program],
            "subject": clean["subject"],
            "preview_text": clean["preview_text"],
            "headline": clean["headline"],
            "paragraphs": clean["paragraphs"],
            "cta_label": clean["cta_label"],
            "cta_url": clean["cta_url"],
            "segment_suggestion": clean["segment_suggestion"],
            "image_url": image_url,
            "image_alt": clean["image_alt"] if image_url else "",
            "from_name": str(ctx.setting("from_name") or "").strip()[:100],
            "footer": {
                "business": self.profile.get("business_name") or getattr(ctx.org, "name", ""),
                "address": str(self.profile.get("address") or "").strip(),
                "language": self.profile.get("language", ""),
            },
        }
        content["html"] = render.render_html(content, channel=self.channel)
        content["text"] = render.render_text(content, channel=self.channel)
        detail = {
            **detail,
            "program": program,
            "sequence": SEQUENCE_NAMES[program],
            "platform": self.channel,
            "image": bool(image_url),
        }
        if self.channel == "export":
            detail["export_only"] = True
        asset = assets.create(
            ctx.db,
            tenant_id=ctx.tenant_id,
            cipher=ctx.cipher,
            agent_slug=SLUG,
            kind=AssetKind.EMAIL,
            channel=self.channel,
            title=title,
            content=content,
            run_id=ctx.run_id or "",
            scheduled_for=None,  # publishing only makes a draft; sending is scheduled in the platform
            image_ids=image_ids,
            detail=detail,
        )
        on_approval = {
            "export": "Marks it ready to download. Nothing is sent.",
            "klaviyo": "Saves it as a template in Klaviyo. No campaign is created and nothing is sent.",
        }.get(self.channel, f"Creates a draft campaign in {PLATFORM_NAMES.get(self.channel, self.channel)}. Nothing is sent.")
        return AgentAction(
            kind="create_email_draft",
            title=f"Email draft: {clean['subject']}",
            impact=Impact.HIGH,
            approval_type="Email campaign",
            target_kind=assets.TARGET_KIND,
            target_id=asset.id,
            payload={
                "asset_id": asset.id,
                "subject": clean["subject"],
                "preview_text": clean["preview_text"],
                "sequence": SEQUENCE_NAMES[program],
                "position": detail.get("position", 1),
                "segment_suggestion": clean["segment_suggestion"],
                "on_approval": on_approval,
            },
            apply=lambda c, a=asset: assets.release(c.db, c.org, a, c.cipher),
            audit=f"approved the email “{clean['subject']}” ({on_approval.rstrip('.').lower()})",
        )

    # ── Programs ───────────────────────────────────────────────────────────
    def welcome(self, fingerprint: str) -> tuple[list[AgentAction], str]:
        """All four emails, or none: a series with a gap is worse than a late one."""
        total = len(prompts.WELCOME_SERIES)
        written: list[dict] = []
        for position, (day, purpose) in enumerate(prompts.WELCOME_SERIES, start=1):
            self.ctx.progress(f"Writing the welcome series ({position} of {total})", done=position - 1, total=total)
            clean, problem = self.copy(
                f"Email {position} of {total} in a welcome series, sent on day {day} after sign-up. {purpose}",
                sequence=f"Welcome series, email {position} of {total}, day {day}",
            )
            if clean is None:
                return [], f"email {position}: {problem}"
            written.append(clean)
        actions = []
        for position, ((day, _purpose), clean) in enumerate(zip(prompts.WELCOME_SERIES, written, strict=True), start=1):
            actions.append(
                self.store(
                    clean,
                    program="welcome",
                    title=f"Welcome {position}/{total} (day {day}): {clean['subject']}",
                    detail={"position": position, "of": total, "send_day": day, "period": "", "profile_hash": fingerprint},
                )
            )
        return actions, ""

    def single(self, program: str, period: str, articles: list[dict]) -> tuple[AgentAction | None, str]:
        self.ctx.progress(f"Writing the {SEQUENCE_NAMES[program].lower()} email")
        clean, problem = self.copy(prompts.PROGRAM_BRIEFS[program], articles=articles or None)
        if clean is None:
            return None, problem
        detail = {"position": 1, "of": 1, "period": period}
        if articles:
            detail["articles"] = len(articles)
        return self.store(clean, program=program, title=f"{SEQUENCE_NAMES[program]} {period}: {clean['subject']}", detail=detail), ""


AGENT = EmailMarketerAgent()
