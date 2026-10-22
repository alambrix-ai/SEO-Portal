"""The judgement half of the report, written by Claude from the measured facts.

Always the platform's own key (ANTHROPIC_API_KEY in the server .env), never a
workspace connector: a visitor has no workspace. Two calls run side by side —
diagnosis and strategy, and ready-to-use creative — so the report arrives in
the time of the slower one rather than both.

The model only ever sees what ``collect`` measured plus the pages' own text,
and is told to say nothing the pages do not support. The report labels these
sections as AI suggestions.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from app.core.config import settings
from app.core.exceptions import AppError
from app.llm.providers import AnthropicProvider
from app.services.demo_analysis.checks import Check
from app.services.demo_analysis.collect import PageFacts, SiteFacts


class DemoUnavailable(AppError):
    status_code = 503
    code = "demo_unavailable"


def model_name() -> str:
    return (settings.demo_model or settings.assistant_model or settings.llm_model or "").strip()


#: A full report section is a long answer; the shared 120 s agent timeout cuts
#: it off mid-stream (and the retry then starts it over).
LLM_TIMEOUT_SECONDS = 300.0


class _DemoClaude(AnthropicProvider):
    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=httpx.Timeout(LLM_TIMEOUT_SECONDS, connect=15.0),
                verify=True,
                headers={"User-Agent": "Synaprank/1.0 (website analysis)"},
            )
        return self._client


def provider() -> AnthropicProvider:
    key = (settings.anthropic_api_key or "").strip()
    model = model_name()
    if not key or not model:
        raise DemoUnavailable("The website analysis is not configured on this server yet.")
    return _DemoClaude(
        api_key=key,
        model=model,
        base_url=settings.anthropic_base_url,
        default_max_tokens=16000,
        effort=(settings.llm_effort or "medium"),
    )


SYSTEM = (
    "You are a senior growth marketer and technical SEO consultant writing a paid-quality audit "
    "for a business owner. You are given facts measured from their website today and the text of "
    "the pages read. Rules: (1) Ground every statement in the facts or page text; never invent "
    "numbers, rankings, traffic, reviews, prices or competitors' data. Every number you write must "
    "appear in the facts or page text — no outside studies or statistics (such as 'a 100 ms delay "
    "costs 1% of sales'), no revenue, cost or conversion estimates, and no thresholds other than "
    "the ones the checks state. (2) When you infer "
    "something (industry, audience, competitors), it must be a reasonable reading of the pages and "
    "you must not present it as measured. (3) Be specific to THIS business — name its services, "
    "places and customers. No generic filler. (4) Write in plain English for a non-technical owner, "
    "in the language of the site if it is not English. (5) Output JSON only."
)

STRATEGY_SHAPE = """{
  "business": {
    "name": "string", "what_it_does": "one sentence", "industry": "short",
    "audience": ["who buys"], "locations": ["places served, if any"],
    "offer": ["main products or services"], "tone": "how the site sounds",
    "business_model": "local service | ecommerce | B2B | SaaS | publisher | other"
  },
  "executive_summary": "4-6 sentences: where the site stands, the 3 things that matter most, and the upside",
  "strengths": ["specific things the site does well, citing evidence"],
  "top_priorities": [
    {"title": "short imperative", "why": "business impact, citing the measured evidence",
     "how": "concrete steps", "impact": "high|medium|low", "effort": "quick|medium|project",
     "timeframe": "this week|this month|this quarter"}
  ],
  "messaging": {
    "clarity_score": 0, "clarity_verdict": "can a visitor tell in 5 seconds what this is, for whom, and what to do next?",
    "current_headline": "the home page's main headline as found",
    "suggested_headline": "string", "suggested_subheadline": "string",
    "suggested_cta": "button text", "trust_gaps": ["missing proof: reviews, guarantees, credentials, prices..."]
  },
  "keyword_opportunities": [
    {"keyword": "phrase customers would type", "intent": "buy|research|local|compare",
     "why": "why it fits this business", "target_page": "existing URL or 'new page'"}
  ],
  "page_fixes": [
    {"url": "a page that was read", "suggested_title": "50-60 chars", "suggested_description": "140-160 chars", "note": "what was wrong"}
  ],
  "faq_suggestions": [{"q": "question customers ask", "a": "2-3 sentence answer drawn from the site"}],
  "competitors_to_research": [{"name": "business or domain", "why": "why it is a likely competitor"}],
  "local_actions": ["specific actions for Google Business Profile, reviews and local pages; empty if not local"],
  "roadmap": {
    "first_30_days": ["action"], "days_31_60": ["action"], "days_61_90": ["action"]
  }
}"""

GROWTH_SHAPE = """{
  "content_plan": [
    {"title": "article headline", "target_keyword": "phrase", "type": "guide|comparison|how-to|local|listicle|case study",
     "angle": "why a reader clicks, one sentence", "outline": ["H2", "H2", "H2"]}
  ],
  "search_campaign": {
    "campaign_name": "string", "goal": "leads|sales|calls|bookings",
    "budget_note": "how to think about the starting budget; no invented costs per click",
    "ad_groups": [
      {"name": "theme", "keywords": ["phrase or [exact] match"],
       "headlines": ["max 30 characters each, 5 items"], "descriptions": ["max 90 characters each, 2 items"],
       "landing_page": "URL from the site"}
    ],
    "negative_keywords": ["words to exclude"]
  }
}"""

CHANNELS_SHAPE = """{
  "social_posts": [
    {"network": "Instagram|Facebook|LinkedIn|X", "text": "ready-to-post copy with line breaks", "hashtags": ["tag"], "visual_idea": "what the image shows"}
  ],
  "email_campaigns": [
    {"name": "welcome|promotion|win-back|newsletter", "subject": "string", "preview_text": "string", "body_outline": ["point"], "cta": "button text"}
  ],
  "ad_creative_angles": [{"angle": "string", "hook": "first line of the ad", "audience": "who it targets"}]
}"""


def _facts_for_prompt(facts: SiteFacts, pages: list[PageFacts], checks: list[Check]) -> str:
    site = facts.as_dict()
    site.pop("pages", None)
    page_rows = [
        {
            "url": p.url, "status": p.status, "title": p.title, "meta_description": p.meta_description,
            "h1": p.h1, "h2": p.h2[:8], "words": p.word_count, "schema": p.schema_types,
        }
        for p in pages
    ]
    issues = [
        {"title": c.title, "status": c.status, "evidence": c.evidence}
        for c in checks if c.status in ("fail", "warn")
    ]
    texts = "\n\n".join(f"### {p.url}\n{p.text}" for p in pages if p.text)
    return (
        "MEASURED SITE FACTS (JSON):\n" + json.dumps(site, ensure_ascii=False, default=str)[:12000]
        + "\n\nPAGES READ (JSON):\n" + json.dumps(page_rows, ensure_ascii=False)[:12000]
        + "\n\nISSUES FOUND BY THE AUTOMATED CHECKS:\n" + json.dumps(issues, ensure_ascii=False)[:6000]
        + "\n\nPAGE TEXT:\n" + texts[:30000]
    )


def with_long_timeout(llm: Any) -> Any:
    """Give a workspace's model the same long read timeout as the demo's.

    Workspace models come from their connectors with the shared agent timeout,
    which a full report section outlasts. Every HTTP provider builds its
    client lazily, so presetting it is enough.
    """
    if getattr(llm, "_client", "missing") is None and getattr(llm, "base_url", ""):
        llm._client = httpx.Client(
            base_url=llm.base_url,
            timeout=httpx.Timeout(LLM_TIMEOUT_SECONDS, connect=15.0),
            verify=True,
            headers={"User-Agent": "Synaprank/1.0 (website analysis)"},
        )
    return llm


def write(
    facts: SiteFacts, pages: list[PageFacts], checks: list[Check], *, llm: Any = None
) -> tuple[dict[str, Any], dict[str, Any], dict[str, int]]:
    """``(strategy, creative, usage)``. Raises if the strategy call fails;
    the two creative calls are best effort and leave their sections empty.

    ``llm`` is the workspace's model for in-portal analyses; the public demo
    passes nothing and gets the platform's own Claude.
    """
    llm = with_long_timeout(llm) if llm is not None else provider()
    context = _facts_for_prompt(facts, pages, checks)
    business = facts.business if facts.business.get("provided") else {}
    listing = (
        f"\n\nBUSINESS LISTING LINK GIVEN BY THE OWNER: {json.dumps(business, ensure_ascii=False)}"
        if business else ""
    )

    strategy_prompt = (
        context + listing
        + "\n\nWrite the audit. top_priorities: 6-8 items ordered by impact, each tied to evidence above. "
        "keyword_opportunities: 12-15. page_fixes: one per page with a missing, duplicate or poor title or "
        "description (up to 8). faq_suggestions: 6. competitors_to_research: up to 5, only plausible ones. "
        "roadmap: 3-5 actions per phase."
    )
    growth_prompt = (
        context + listing
        + "\n\nWrite ready-to-use growth material for this business. content_plan: 8 articles. "
        "search_campaign: 3-4 ad groups; respect the character limits strictly. Use only claims the pages support."
    )
    channels_prompt = (
        context + listing
        + "\n\nWrite ready-to-use channel material for this business. social_posts: 6 across the networks "
        "that suit the business. email_campaigns: 3. ad_creative_angles: 4. Use only claims the pages support."
    )

    def call(prompt: str, shape: str, max_tokens: int) -> tuple[Any, Any]:
        return llm.complete_json(prompt, system=SYSTEM, schema_hint=shape, max_tokens=max_tokens, temperature=0.4)

    def optional(future) -> tuple[Any, Any]:  # noqa: ANN001
        try:
            return future.result()
        except Exception:  # noqa: BLE001 - the audit still stands without this part
            return {}, None

    with ThreadPoolExecutor(max_workers=3) as pool:
        strategy_future = pool.submit(call, strategy_prompt, STRATEGY_SHAPE, 9000)
        growth_future = pool.submit(call, growth_prompt, GROWTH_SHAPE, 6000)
        channels_future = pool.submit(call, channels_prompt, CHANNELS_SHAPE, 5000)
        strategy, strategy_usage = strategy_future.result()
        growth, growth_usage = optional(growth_future)
        channels, channels_usage = optional(channels_future)
    creative = {
        **(growth if isinstance(growth, dict) else {}),
        **(channels if isinstance(channels, dict) else {}),
    }
    usage = {"input_tokens": 0, "output_tokens": 0}
    for u in (strategy_usage, growth_usage, channels_usage):
        if u is not None:
            usage["input_tokens"] += int(getattr(u, "input_tokens", 0) or 0)
            usage["output_tokens"] += int(getattr(u, "output_tokens", 0) or 0)
    return (
        strategy if isinstance(strategy, dict) else {},
        creative if isinstance(creative, dict) else {},
        usage,
    )
