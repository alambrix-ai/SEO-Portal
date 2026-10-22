"""SEO Article Writer — plans a content calendar and writes the articles on it.

One pass does two things.

**Plan.** The calendar (agent setting ``_content_calendar``) is topped up so
it always holds about two weeks of articles at the configured rate. Topics
come from the model, which is given: searches the site already appears for
(any connected search-performance source) that no existing page covers, the
brand profile's keywords, the scope topics if any, and every existing page
and article so it does not plan a duplicate or a page that would compete with
one already ranking. Its suggestions are then checked again here, because a
prompt is a request: anything too close to an existing page, article or
planned topic is dropped. Dates are spread evenly across the coming days.

**Write.** Topics dated within the next week are written, in date order. Each
draft is a model call; it is sanitized to a small allowlist of tags, its links
are kept only when they point at a real page of the site, and it is checked
(lengths, structure, no empty sections, enough words, an FAQ). A draft that
fails is rewritten once with the reasons; a topic that fails on two separate
runs is marked skipped. Each article becomes a library item dated for its
calendar day, proposed for approval (high impact: it is new content on the
customer's site).

Batch rule: up to ``articles_per_week`` articles per run, never more than
:data:`MAX_ARTICLES_PER_RUN` (each is a long model call, so this bounds a
run's time and spend), and no more than the daily action allowance left —
but at least one, because proposals waiting for review do not use it.

Approving an article saves it to the connected CMS **as a draft** (a pull
request for repositories) on its date; nothing is ever made live. Without a
CMS that can create pages, an approved article is ready to download as HTML
or Markdown.
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base import scope as scope_rules
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
from app.agents.base.style import humanise, humanise_json
from app.agents.seo_article_writer import html_tools as ht
from app.agents.seo_article_writer import prompts
from app.connectors.base.connector import Capability
from app.core.exceptions import ConnectorError
from app.models.content import AssetKind, MarketingAsset
from app.models.seo import SeoPage
from app.models.workspace import Organization
from app.services import assets

SLUG = "seo_article_writer"
CALENDAR_KEY = "_content_calendar"

#: The most articles one run writes, however generous the settings.
MAX_ARTICLES_PER_RUN = 5
#: The calendar is kept this far ahead.
PLAN_HORIZON_DAYS = 14
#: Topics dated within this many days are written now, so there is time to review.
WRITE_AHEAD_DAYS = 7
#: Most topics asked for in one planning call.
MAX_TOPICS_PER_PLAN = 12
#: Runs a topic may fail on before it is skipped.
MAX_FAILED_RUNS = 2
#: Written and skipped entries kept on the calendar for reference.
KEEP_DONE = 60
#: Context sizes, so a large site cannot crowd the prompt.
MAX_PAGES_IN_PLAN = 120
MAX_LINK_CANDIDATES = 25
MAX_OPPORTUNITIES = 15

DEFAULT_PER_WEEK = 2
DEFAULT_MIN_WORDS = 1000
#: A draft may fall this far short of the minimum before it is rejected
#: (models count words loosely, and an FAQ is added on top of the body).
WORD_TOLERANCE = 0.8
META_TITLE_MAX = 60
META_DESCRIPTION_MAX = 155
META_DESCRIPTION_MIN = 50
TITLE_MAX = 110
MIN_FAQ = 2
MIN_SECTIONS = 2
#: Calendar days publish at this time (UTC).
PUBLISH_AT = time(9, 0)

_STOP = frozenset(
    "a an and are as at be by can do does for from how i in is it its me my near of on or our "
    "that the this to vs was what when where which who why will with you your best top guide".split()
)


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 1 and w not in _STOP}


def too_close(a: str, b: str) -> bool:
    """Whether two topics target the same search (word overlap, ignoring filler)."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    return len(ta & tb) / len(ta | tb) >= 0.75


def page_topics(pages: list[tuple[str, str]]) -> list[set[str]]:
    """Each page's topic words: its title plus the words in its URL."""
    return [_tokens(title) | _tokens(url.replace("/", " ").replace("-", " ")) for url, title in pages]


def covered_by(phrase: str, topics: list[set[str]]) -> bool:
    """Whether some page already covers every meaningful word of ``phrase``."""
    wanted = _tokens(phrase)
    return bool(wanted) and any(wanted <= have for have in topics)


def _int_setting(ctx: AgentContext, key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(float(str(ctx.setting(key, "") or default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def batch_size(ctx: AgentContext, per_week: int) -> int:
    """Articles one run may write (see the module docstring for the rule)."""
    try:
        remaining = int(ctx.remaining_actions)
    except (AttributeError, TypeError, ValueError):
        remaining = 0
    return max(1, min(MAX_ARTICLES_PER_RUN, per_week, remaining))


def spread_dates(start: date, count: int, per_week: int) -> list[date]:
    """``count`` dates from ``start``, ``per_week`` to a week, evenly spaced."""
    step = 7 / max(1, per_week)
    # Half days round up (not to even), so 2 a week is days 0, 4, 7, 11.
    return [start + timedelta(days=int(i * step + 0.5)) for i in range(count)]


def _domain(org: Organization) -> str:
    raw = (getattr(org, "primary_domain", "") or "").strip().lower()
    for prefix in ("https://", "http://"):
        raw = raw.removeprefix(prefix)
    return raw.strip("/").split("/", 1)[0]


def _path(url: str) -> str:
    from urllib.parse import urlsplit

    value = (url or "").strip()
    if value.lower().startswith(("http://", "https://")):
        value = urlsplit(value).path or "/"
    value = value.split("#", 1)[0].split("?", 1)[0]
    return value.rstrip("/") or "/"


class SiteLinks:
    """The site's real pages, as the only things an article may link to."""

    def __init__(self, pages: list[tuple[str, str]], *, domain: str) -> None:
        self.domain = domain
        self.pages = [(url, title) for url, title in pages if url]
        self.paths = {_path(url) for url, _ in self.pages}

    def href(self, url: str) -> str:
        path = _path(url)
        return f"https://{self.domain}{path}" if self.domain else path

    def allows(self, href: str) -> bool:
        from urllib.parse import urlsplit

        value = (href or "").strip()
        if value.lower().startswith(("http://", "https://")):
            host = (urlsplit(value).hostname or "").lower().removeprefix("www.")
            if not self.domain or host != self.domain.removeprefix("www."):
                return False
        elif not value.startswith("/"):
            return False
        return _path(value) in self.paths

    def candidates(self, topic: dict) -> list[tuple[str, str]]:
        wanted = _tokens(" ".join([topic.get("keyword", ""), topic.get("title", ""), *topic.get("secondary", [])]))
        scored = []
        for url, title in self.pages:
            overlap = len(wanted & (_tokens(title) | _tokens(url.replace("/", " ").replace("-", " "))))
            scored.append((overlap, len(url), url, title))
        scored.sort(key=lambda row: (-row[0], row[1]))
        return [(self.href(url), title) for _, _, url, title in scored[:MAX_LINK_CANDIDATES]]


# ── Validation ─────────────────────────────────────────────────────────────
def clean_article(raw: object, *, topic: dict, links: SiteLinks, language: str) -> dict:
    """The model's draft, normalised: punctuation, HTML allowlist, real links only."""
    cleaned = humanise_json(raw if isinstance(raw, dict) else {})
    data: dict = cleaned if isinstance(cleaned, dict) else {}

    def text(key: str, limit: int = 400) -> str:
        return " ".join(str(data.get(key) or "").split())[:limit]

    body = ht.sanitize(str(data.get("body_html") or ""), allow_href=links.allows)
    title = text("title", 200) or str(topic.get("title") or "")
    faq = []
    for item in data.get("faq") or []:
        if isinstance(item, dict):
            q = " ".join(str(item.get("q") or item.get("question") or "").split())
            a = " ".join(str(item.get("a") or item.get("answer") or "").split())
            if q and a:
                faq.append({"q": q[:300], "a": a[:1500]})
    used = set(ht.links_in(body))
    internal = []
    for item in data.get("internal_links") or []:
        url = str((item or {}).get("url") or "") if isinstance(item, dict) else ""
        if url in used and url not in {i["url"] for i in internal}:
            internal.append({"url": url, "anchor": " ".join(str(item.get("anchor") or "").split())[:200]})
    for url in used:  # links the model used but did not list
        if url not in {i["url"] for i in internal}:
            internal.append({"url": url, "anchor": ""})
    secondary = [
        " ".join(str(k).split())[:80]
        for k in (data.get("secondary_keywords") or topic.get("secondary") or [])
        if str(k).strip()
    ][:8]
    words = ht.word_count(body)
    return {
        "title": title,
        "slug": assets.slug_for(text("slug", 120) or title)[:80],
        "meta_title": text("meta_title", 200),
        "meta_description": text("meta_description", 400),
        "target_keyword": text("target_keyword", 120) or str(topic.get("keyword") or ""),
        "secondary_keywords": secondary,
        "outline": ht.headings(body),
        "body_html": body,
        "faq_heading": text("faq_heading", 120) or "Frequently asked questions",
        "faq": faq[:8],
        "internal_links": internal,
        "word_count": words + sum(len((i["q"] + " " + i["a"]).split()) for i in faq),
        "body_word_count": words,
        "language": language,
    }


def problems_with(article: dict, *, min_words: int) -> list[str]:
    """Why a cleaned draft cannot be proposed; empty when it can."""
    found: list[str] = []
    if not article["title"]:
        found.append("the title is empty")
    elif len(article["title"]) > TITLE_MAX:
        found.append(f"the title is {len(article['title'])} characters; keep it under {TITLE_MAX}")
    if not article["meta_title"]:
        found.append("meta_title is empty")
    elif len(article["meta_title"]) > META_TITLE_MAX:
        found.append(f"meta_title is {len(article['meta_title'])} characters; the limit is {META_TITLE_MAX}")
    description = article["meta_description"]
    if len(description) < META_DESCRIPTION_MIN or len(description) > META_DESCRIPTION_MAX:
        found.append(
            f"meta_description is {len(description)} characters; it must be between "
            f"{META_DESCRIPTION_MIN} and {META_DESCRIPTION_MAX}"
        )
    if not article["slug"] or article["slug"] == "export":
        found.append("the slug is empty")
    if len(article["outline"]) < MIN_SECTIONS:
        found.append(f"the body needs at least {MIN_SECTIONS} <h2> sections")
    empty = ht.empty_sections(article["body_html"])
    if empty:
        found.append("these headings have no content under them: " + "; ".join(empty[:5]))
    needed = int(min_words * WORD_TOLERANCE)
    if article["body_word_count"] < needed:
        found.append(f"the body has {article['body_word_count']} words; it must have at least {min_words}")
    if len(article["faq"]) < MIN_FAQ:
        found.append(f"the FAQ needs at least {MIN_FAQ} questions with answers")
    return found


# ── The agent ──────────────────────────────────────────────────────────────
class SeoArticleWriterAgent(BaseAgent):
    spec = AgentSpec(
        slug=SLUG,
        name="SEO Article Writer",
        category=AgentCategory.CONTENT,
        description=(
            "Plans a content calendar from your search data and brand profile, and writes "
            "SEO articles for it."
        ),
        default_interval=timedelta(days=1),
        default_schedule="Daily",
        max_impact=Impact.HIGH,
        default_max_actions_per_day=5,
        scope_placeholder="Topics to write about, comma separated (optional)",
        requires_llm=True,
        connector_use=(
            ConnectorUse(
                Capability.CREATE_PAGE,
                UseMode.ONE,
                question="Which website should approved articles be saved to?",
                tries_in_turn=True,
                automatic="is set up to create pages",
            ),
            ConnectorUse(
                Capability.GENERATE_IMAGE,
                UseMode.ONE,
                question="Which AI service should create header images?",
            ),
            ConnectorUse(
                Capability.READ_SEARCH_PERFORMANCE,
                UseMode.ALL,
                question="Which search data should this agent plan topics from?",
                group="search data sources",
            ),
        ),
    )

    def validate_scope(self, scope: str) -> str | None:
        return scope_rules.free_text(scope, max_items=12, label="topics")

    def run(self, ctx: AgentContext) -> AgentResult:
        per_week = _int_setting(ctx, "articles_per_week", DEFAULT_PER_WEEK, 1, 14)
        min_words = _int_setting(ctx, "min_words", DEFAULT_MIN_WORDS, 600, 3000)
        profile = ctx.brand_profile or {}
        language = (
            str(ctx.setting("language", "") or "").strip()
            or str(profile.get("language") or "").strip()
            or "English"
        )
        brief = ctx.brand_brief
        scope_topics = scope_rules.items(ctx.scope)
        pages = self._pages(ctx)
        if not (brief or ctx.industry or scope_topics or pages):
            return AgentResult.skip(
                "Add a brand profile (or enter topics in this agent's scope) so the writer "
                "knows what your business does."
            )
        business = prompts.business_block(
            brief, name=ctx.org.name, industry=ctx.industry, domain=_domain(ctx.org)
        )
        links = SiteLinks(pages, domain=_domain(ctx.org))
        calendar = load_calendar(ctx.setting(CALENDAR_KEY))

        # ── Plan ──
        ctx.progress("Planning the content calendar")
        planned_now, plan_note = self._top_up(
            ctx,
            calendar,
            per_week=per_week,
            language=language,
            business=business,
            pages=pages,
            scope_topics=scope_topics,
            brand_keywords=[str(k) for k in profile.get("keywords") or []][:20],
        )

        # ── Write ──
        today = ctx.today
        due = sorted(
            (
                e for e in calendar
                if e["status"] == "planned" and date.fromisoformat(e["date"]) <= today + timedelta(days=WRITE_AHEAD_DAYS)
            ),
            key=lambda e: e["date"],
        )
        batch = due[: batch_size(ctx, per_week)]
        publisher, setup_notes = choose_publisher(ctx)
        channel = publisher.slug if publisher is not None else "export"
        from app.services import images

        # An image connection without an image model chosen: articles are
        # still written, without a header picture, and the summary says why.
        image_problem = images.image_setup_problem(ctx)
        can_image = ctx.with_capability(Capability.GENERATE_IMAGE) is not None and not image_problem
        if image_problem:
            setup_notes = [*(setup_notes or []), "Articles have no header picture. " + image_problem]
        actions: list[AgentAction] = []
        failed = 0
        image_failures = 0
        for index, entry in enumerate(batch, start=1):
            ctx.progress(f"Writing articles ({index} of {len(batch)})", done=index - 1, total=len(batch))
            article = self._draft(ctx, entry, business=business, language=language, min_words=min_words, links=links)
            if article is None:
                failed += 1
                entry["failures"] = int(entry.get("failures") or 0) + 1
                if entry["failures"] >= MAX_FAILED_RUNS:
                    entry["status"] = "skipped"
                continue
            image_ids: list[str] = []
            if can_image:
                image_id = self._header_image(ctx, article["title"])
                if image_id:
                    image_ids.append(image_id)
                    article["header_image_id"] = image_id
                else:
                    image_failures += 1
            when = datetime.combine(date.fromisoformat(entry["date"]), PUBLISH_AT, tzinfo=timezone.utc)
            asset = assets.create(
                ctx.db,
                tenant_id=ctx.tenant_id,
                cipher=ctx.cipher,
                agent_slug=SLUG,
                kind=AssetKind.ARTICLE,
                channel=channel,
                title=article["title"],
                content=article,
                run_id=ctx.run_id or "",
                scheduled_for=when,
                image_ids=image_ids,
                detail={
                    "target_keyword": article["target_keyword"],
                    "word_count": article["word_count"],
                    "slug": article["slug"],
                    "planned_for": entry["date"],
                    "internal_links": len(article["internal_links"]),
                    "faq": len(article["faq"]),
                    "has_image": bool(image_ids),
                    "language": language,
                    **({"export_only": True} if publisher is None else {}),
                },
            )
            entry["status"] = "written"
            entry["asset_id"] = asset.id
            where = ctx.connector_name(channel) if publisher is not None else "download"
            actions.append(
                AgentAction(
                    kind="create_article",
                    title=f"New article: {article['title']}",
                    impact=Impact.HIGH,
                    approval_type="SEO Article",
                    target_kind=assets.TARGET_KIND,
                    target_id=asset.id,
                    payload={
                        "asset_id": asset.id,
                        "title": article["title"],
                        "summary": article["meta_description"],
                        "channel": channel,
                        "target_keyword": article["target_keyword"],
                        "word_count": article["word_count"],
                        "scheduled_for": entry["date"],
                    },
                    apply=lambda c, a=asset: assets.release(c.db, c.org, a, c.cipher),
                    audit=(
                        f"approved the article “{article['title']}”"
                        + (f" to be saved as a draft in {where}" if publisher is not None else " for download")
                    ),
                )
            )

        ctx.set_setting(CALENDAR_KEY, trim_calendar(calendar))
        return AgentResult(
            summary=self._summary(
                written=len(actions),
                failed=failed,
                planned_now=planned_now,
                plan_note=plan_note,
                calendar=calendar,
                has_brief=bool(brief),
                publisher_name=ctx.connector_name(channel) if publisher is not None else "",
                image_failures=image_failures,
                more_due=max(0, len(due) - len(batch)),
                setup_notes=setup_notes,
            ),
            actions=actions,
            metric_label=f"{sum(1 for e in calendar if e['status'] == 'planned')} articles planned",
            detail={
                "written": len(actions),
                "failed": failed,
                "planned_this_run": planned_now,
                "upcoming": [
                    {"date": e["date"], "keyword": e["keyword"], "status": e["status"]}
                    for e in sorted(calendar, key=lambda e: e["date"])
                    if e["status"] == "planned"
                ][:30],
            },
        )

    # ── Reading the site ───────────────────────────────────────────────────
    @staticmethod
    def _pages(ctx: AgentContext) -> list[tuple[str, str]]:
        rows = ctx.db.execute(
            select(SeoPage.url, SeoPage.title).where(SeoPage.tenant_id == ctx.tenant_id).limit(1000)
        ).all()
        return [(str(url or ""), str(title or "")) for url, title in rows if url]

    @staticmethod
    def _written_topics(ctx: AgentContext) -> list[str]:
        """Keywords and titles of every article already in the library."""
        rows = ctx.db.execute(
            select(MarketingAsset.title, MarketingAsset.detail).where(
                MarketingAsset.tenant_id == ctx.tenant_id, MarketingAsset.kind == AssetKind.ARTICLE
            )
        ).all()
        out: list[str] = []
        for title, detail in rows:
            keyword = str((detail or {}).get("target_keyword") or "")
            out.extend(t for t in (keyword, str(title or "")) if t)
        return out

    @staticmethod
    def _opportunities(ctx: AgentContext, pages: list[tuple[str, str]]) -> list[str]:
        """Searches the site appears for that no existing page covers."""
        queries: list[str] = []
        for source in ctx.all_with_capability(Capability.READ_SEARCH_PERFORMANCE):
            try:
                data = source.read_search_performance(days=28)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 - one source failing is not fatal
                ctx.log_warning("Search performance unavailable from %s: %s", source.slug, exc)
                continue
            queries.extend(str(q) for q in (data or {}).get("top_queries") or [] if str(q).strip())
        covered = page_topics(pages)
        out: list[str] = []
        for query in queries:
            wanted = _tokens(query)
            if not wanted or any(wanted <= have for have in covered):
                continue
            if not any(too_close(query, q) for q in out):
                out.append(query.strip())
        return out[:MAX_OPPORTUNITIES]

    # ── Planning ───────────────────────────────────────────────────────────
    def _top_up(
        self,
        ctx: AgentContext,
        calendar: list[dict],
        *,
        per_week: int,
        language: str,
        business: str,
        pages: list[tuple[str, str]],
        scope_topics: list[str],
        brand_keywords: list[str],
    ) -> tuple[int, str]:
        today = ctx.today
        # Dates in the past that were never written are moved forward.
        open_entries = sorted((e for e in calendar if e["status"] == "planned"), key=lambda e: e["date"])
        target = max(1, round(per_week * PLAN_HORIZON_DAYS / 7))
        need = min(MAX_TOPICS_PER_PLAN, target - len(open_entries))
        if need <= 0:
            self._reschedule_overdue(open_entries, today, per_week)
            return 0, ""

        opportunities = self._opportunities(ctx, pages)
        written = self._written_topics(ctx)
        on_calendar = [e["keyword"] for e in calendar] + [e["title"] for e in calendar if e.get("title")]
        try:
            data = ctx.ask_json(
                prompts.plan(
                    business=business,
                    count=need,
                    language=language,
                    scope_topics=scope_topics,
                    opportunities=opportunities,
                    brand_keywords=brand_keywords,
                    existing_pages=[(u, t) for u, t in pages[:MAX_PAGES_IN_PLAN]],
                    written=(written + on_calendar)[:150],
                ),
                system=prompts.SYSTEM,
                schema_hint=prompts.PLAN_SCHEMA,
            )
        except Exception as exc:  # noqa: BLE001 - writing can continue from the existing plan
            ctx.log_warning("Planning failed: %s", exc)
            self._reschedule_overdue(open_entries, today, per_week)
            return 0, "planning failed this run and will be tried again"

        taken = [*written, *on_calendar]
        page_texts = [t for url, title in pages for t in (title, url.replace("/", " ").replace("-", " "))]
        covered = page_topics(pages)
        chosen: list[dict] = []
        dropped = 0
        suggestions = data.get("topics") if isinstance(data, dict) else None
        for item in suggestions if isinstance(suggestions, list) else []:
            if not isinstance(item, dict):
                continue
            keyword = humanise(" ".join(str(item.get("target_keyword") or "").split()))[:120]
            title = humanise(" ".join(str(item.get("title") or "").split()))[:200]
            if not keyword:
                continue
            if (
                any(too_close(keyword, t) for t in taken)
                or any(too_close(keyword, t) for t in page_texts)
                or covered_by(keyword, covered)
            ) or (
                title and any(too_close(title, t) for t in taken)
            ):
                dropped += 1
                continue
            secondary = [humanise(" ".join(str(k).split()))[:80] for k in item.get("secondary_keywords") or [] if str(k).strip()][:6]
            chosen.append(
                {
                    "id": uuid.uuid4().hex[:12],
                    "keyword": keyword,
                    "title": title,
                    "angle": humanise(" ".join(str(item.get("angle") or "").split()))[:300],
                    "secondary": secondary,
                    "source": "search" if any(too_close(keyword, q) for q in opportunities) else "plan",
                    "status": "planned",
                    "asset_id": "",
                    "failures": 0,
                }
            )
            taken.extend([keyword, title])
            if len(chosen) >= need:
                break

        self._reschedule_overdue(open_entries, today, per_week)
        last = max((date.fromisoformat(e["date"]) for e in calendar if e["status"] != "skipped"), default=None)
        step = 7 / per_week
        start = max(today + timedelta(days=1), (last + timedelta(days=int(step + 0.5))) if last else today)
        for entry, when in zip(chosen, spread_dates(start, len(chosen), per_week), strict=True):
            entry["date"] = when.isoformat()
            calendar.append(entry)
        note = ""
        if dropped:
            note = f"{dropped} suggested topics were dropped as too close to existing pages or articles"
        return len(chosen), note

    @staticmethod
    def _reschedule_overdue(open_entries: list[dict], today: date, per_week: int) -> None:
        """Planned topics whose day passed unwritten move to the coming days."""
        overdue = [e for e in open_entries if date.fromisoformat(e["date"]) < today]
        for entry, when in zip(overdue, spread_dates(today + timedelta(days=1), len(overdue), per_week), strict=True):
            entry["date"] = when.isoformat()

    # ── Writing ────────────────────────────────────────────────────────────
    def _draft(
        self, ctx: AgentContext, entry: dict, *, business: str, language: str, min_words: int, links: SiteLinks
    ) -> dict | None:
        problems: list[str] | None = None
        candidates = links.candidates(entry)
        for _attempt in range(2):
            try:
                raw = ctx.ask_json(
                    prompts.article(
                        business=business,
                        language=language,
                        min_words=min_words,
                        topic=entry,
                        links=candidates,
                        problems=problems,
                    ),
                    system=prompts.SYSTEM,
                    schema_hint=prompts.ARTICLE_SCHEMA,
                    max_tokens=max(6000, min_words * 4),
                )
            except Exception as exc:  # noqa: BLE001 - one article failing must not stop the rest
                ctx.log_warning("Article draft failed for %r: %s", entry.get("keyword"), exc)
                return None
            article = clean_article(raw, topic=entry, links=links, language=language)
            problems = problems_with(article, min_words=min_words)
            if not problems:
                return article
            ctx.log_warning("Article draft rejected (%s)", "; ".join(problems))
        return None

    @staticmethod
    def _header_image(ctx: AgentContext, title: str) -> str:
        """One landscape image for the article, or "" — never fails the article."""
        from app.services import images

        try:
            image = images.generate(
                ctx.db,
                ctx.org,
                prompts.image(title=title, industry=ctx.industry),
                shape="landscape",
                provider=images.provider_for(ctx),
            )
        except Exception as exc:  # noqa: BLE001 - the article stands without its image
            ctx.log_warning("Header image failed: %s", exc)
            return ""
        return str(image.id or "")

    # ── Reporting ──────────────────────────────────────────────────────────
    @staticmethod
    def _summary(
        *,
        written: int,
        failed: int,
        planned_now: int,
        plan_note: str,
        calendar: list[dict],
        has_brief: bool,
        publisher_name: str,
        image_failures: int,
        more_due: int,
        setup_notes: list[str] | None = None,
    ) -> str:
        upcoming = sorted((e for e in calendar if e["status"] == "planned"), key=lambda e: e["date"])
        parts = [f"{written} article{'s' if written != 1 else ''} written"]
        if planned_now:
            parts.append(f"{planned_now} new topic{'s' if planned_now != 1 else ''} added to the calendar")
        if upcoming:
            parts.append(f"{len(upcoming)} planned, next on {upcoming[0]['date']}")
        sentences = ["; ".join(parts) + "."]
        if failed:
            sentences.append(
                f"{failed} draft{'s' if failed != 1 else ''} did not pass the quality checks and will be tried "
                "again next run (a topic is skipped after two failed runs)."
            )
        if more_due:
            sentences.append(f"{more_due} more due soon will be written next run.")
        if plan_note:
            sentences.append(plan_note[:1].upper() + plan_note[1:] + ".")
        if image_failures:
            sentences.append(f"{image_failures} header image{'s' if image_failures != 1 else ''} could not be created.")
        sentences.extend(setup_notes or [])
        if publisher_name:
            sentences.append(f"Approved articles are saved as drafts in {publisher_name}.")
        else:
            sentences.append("No connected CMS can save drafts, so approved articles are ready to download.")
        if not has_brief:
            sentences.append("Add a brand profile to get articles that sound more like your business.")
        return " ".join(sentences)


def choose_publisher(ctx: AgentContext):  # noqa: ANN201 - a connector or None, and notes
    """The CMS articles are saved to: the chosen one, or the first that is set up.

    With a website chosen in this agent's settings (and still connected),
    only that one is considered. Otherwise each connected CMS that can create
    pages is tried in catalogue order.

    A connector can have the capability but be missing the one setting it
    needs to create pages (Webflow's articles collection); it says so through
    ``create_page_problem`` and the next one is tried, so an article is not
    queued for a destination that would refuse it on approval.
    """
    notes: list[str] = []
    for connector in ctx.candidates(Capability.CREATE_PAGE):
        check = getattr(connector, "create_page_problem", None)
        problem = check() if callable(check) else None
        if problem:
            notes.append(problem)
            continue
        return connector, notes
    return None, notes


# ── Calendar storage ───────────────────────────────────────────────────────
def load_calendar(raw: object) -> list[dict]:
    """The stored calendar, with malformed entries dropped."""
    out: list[dict] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not str(item.get("keyword") or "").strip():
            continue
        try:
            date.fromisoformat(str(item.get("date")))
        except ValueError:
            continue
        status = item.get("status") if item.get("status") in ("planned", "written", "skipped") else "planned"
        out.append(
            {
                "id": str(item.get("id") or uuid.uuid4().hex[:12]),
                "keyword": str(item["keyword"]).strip(),
                "title": str(item.get("title") or ""),
                "angle": str(item.get("angle") or ""),
                "secondary": [str(s) for s in item.get("secondary") or []][:6],
                "source": str(item.get("source") or "plan"),
                "date": str(item["date"]),
                "status": status,
                "asset_id": str(item.get("asset_id") or ""),
                "failures": int(item.get("failures") or 0),
            }
        )
    return out


def trim_calendar(calendar: list[dict]) -> list[dict]:
    """Every planned entry, plus the most recent finished ones."""
    planned = [e for e in calendar if e["status"] == "planned"]
    done = sorted((e for e in calendar if e["status"] != "planned"), key=lambda e: e["date"])[-KEEP_DONE:]
    return sorted(planned + done, key=lambda e: e["date"])


# ── Publishing and exports ─────────────────────────────────────────────────
@assets.register_publisher(AssetKind.ARTICLE)
def publish_article(db: Session, org: Organization, asset: MarketingAsset, content: dict) -> None:
    """Save the article as a draft in the CMS it was written for."""
    from app.connectors.base.git_base import GitHostConnector
    from app.services.connectors import build_connector_bundle

    bundle = build_connector_bundle(db, org)
    connector = bundle.instances.get(asset.channel)
    name = bundle.names.get(asset.channel, asset.channel.replace("_", " ").title())
    if connector is None:
        raise ConnectorError(
            f"{name} is not connected, so this article cannot be saved there. Reconnect it "
            "and approve again, or download the article instead."
        )
    if not connector.supports(Capability.CREATE_PAGE):
        raise ConnectorError(f"{name} cannot create new pages. Download the article instead.")
    body = ht.sanitize(str(content.get("body_html") or ""))
    faq = ht.faq_html(str(content.get("faq_heading") or ""), content.get("faq") or [])
    if faq:
        body = f"{body}\n{faq}"
    if not ht.text_of(body):
        raise ConnectorError("This article has no body text to save.")
    title = str(content.get("title") or asset.title)
    extra: dict = {}
    if isinstance(connector, GitHostConnector):
        extra["body_markdown"] = ht.to_markdown(body)
    remote_id, url = connector.create_page(  # type: ignore[attr-defined]
        title=title,
        body_html=body,
        slug=str(content.get("slug") or assets.slug_for(title)),
        meta_title=str(content.get("meta_title") or ""),
        meta_description=str(content.get("meta_description") or ""),
        status="draft",
        **extra,
    )
    asset.remote_id = str(remote_id or "")[:255]
    asset.remote_url = str(url or "")[:1000]


def _export_date(asset: MarketingAsset) -> str:
    when = asset.scheduled_for or asset.created_at
    return when.date().isoformat() if when else date.today().isoformat()


@assets.register_exporter(AssetKind.ARTICLE, "html", "Web page (HTML)")
def export_html(asset: MarketingAsset, content: dict) -> assets.ExportFile:
    stem = str(content.get("slug") or assets.slug_for(asset.title))
    return assets.ExportFile(f"{stem}.html", "text/html; charset=utf-8", ht.document(content).encode("utf-8"))


@assets.register_exporter(AssetKind.ARTICLE, "markdown", "Markdown")
def export_markdown(asset: MarketingAsset, content: dict) -> assets.ExportFile:
    stem = str(content.get("slug") or assets.slug_for(asset.title))
    text = ht.markdown_document(content, date=_export_date(asset))
    return assets.ExportFile(f"{stem}.md", "text/markdown; charset=utf-8", text.encode("utf-8"))


AGENT = SeoArticleWriterAgent()
