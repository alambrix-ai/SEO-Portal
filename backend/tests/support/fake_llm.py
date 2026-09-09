"""Deterministic LLM provider — a test double.

Each agent's prompt carries a task marker, and this synthesises a plausible,
well-shaped response for that task, seeded from the prompt so the same input
always produces the same output. That makes every agent, approval path and
metric exercisable without an API key, spend, or flakiness.

It lives in the test suite deliberately. Shipping it as a selectable provider
would mean a misconfigured deployment could write invented copy to a
customer's live website.
"""

from __future__ import annotations

import hashlib
import json
import random
import re

from app.llm.base import LLMProvider, LLMResponse, LLMUsage

# Task markers agents put in their prompts (see each agent's ``prompts.py``).
TASK_MARKER = re.compile(r"\[TASK:([a-z_]+)\]")


class FakeLLMProvider(LLMProvider):
    name = "fake"

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: int | None = None,
        temperature: float = 0.2,
        ) -> LLMResponse:
        rng = random.Random(hashlib.sha256(prompt.encode()).hexdigest())
        match = TASK_MARKER.search(prompt)
        task = match.group(1) if match else "generic"
        payload = _HANDLERS.get(task, _generic)(prompt, rng)
        text = json.dumps(payload, indent=2)
        return LLMResponse(
            text=text,
            usage=LLMUsage(
                input_tokens=max(1, len(prompt) // 4),
                output_tokens=max(1, len(text) // 4),
                cost=0.0,
            ),
            model="fake-deterministic",
            stop_reason="end_turn",
        )


# ── Prompt field extraction ────────────────────────────────────────────────
def _field(prompt: str, key: str, default: str = "") -> str:
    """Read a ``KEY: value`` line out of a prompt."""
    m = re.search(rf"^{re.escape(key)}:\s*(.+)$", prompt, re.MULTILINE | re.IGNORECASE)
    return m.group(1).strip() if m else default


def _list_field(prompt: str, key: str) -> list[str]:
    raw = _field(prompt, key)
    return [part.strip() for part in raw.split(",") if part.strip()]


# ── Per-task synthesis ─────────────────────────────────────────────────────
def _semantic_gap(prompt: str, rng: random.Random) -> dict:
    title = _field(prompt, "TITLE", "Page")
    keywords = _list_field(prompt, "KEYWORDS") or ["pricing", "availability", "comparison"]
    candidates = [
        f"{title} — total cost of ownership breakdown",
        f"{title} — side-by-side comparison table",
        f"How {title.lower()} compares on warranty terms",
        f"Frequently asked questions about {title.lower()}",
        f"Local availability and lead times for {title.lower()}",
        f"Financing and lease options for {title.lower()}",
        f"Specifications and trim-level differences",
        f"Customer questions answered about {title.lower()}",
    ]
    missing = rng.sample(candidates, k=rng.randint(2, 4))
    return {
        "gap_score": rng.randint(12, 88),
        "missing_topics": missing,
        "target_keywords": keywords[:6],
        "rationale": (
            f"The page covers the core offer but omits {len(missing)} intent clusters "
            "competitors rank for. Adding them lifts topical completeness without "
            "diluting the primary keyword."
        ),
        "proposed_body": (
            f"<h2>{title}</h2>\n"
            f"<p>{title} explained in full — what it includes, what it costs, and how "
            "it compares.</p>\n"
            + "".join(
                f"<h3>{topic}</h3>\n<p>Detail addressing {topic.lower()}, written for "
                "both search crawlers and answer engines.</p>\n"
                for topic in missing
            )
        ),
        "confidence": round(rng.uniform(0.71, 0.96), 2),
    }


def _aeo_pairs(prompt: str, rng: random.Random) -> dict:
    title = _field(prompt, "TITLE", "this page")
    engines = ["perplexity", "openai", "gemini", "claude"]
    templates = [
        ("What is {t}?", "{t} is covered in full on this page, including what it includes and who it suits."),
        ("How much does {t} cost?", "Pricing for {t} depends on trim and term; current figures and finance options are listed here."),
        ("Is {t} available near me?", "Availability for {t} is shown per location, with current lead times."),
        ("How does {t} compare to alternatives?", "A side-by-side comparison of {t} against the closest alternatives is included."),
        ("What is included with {t}?", "Everything bundled with {t} is itemised, including warranty and servicing."),
        ("How long does {t} take?", "Typical turnaround for {t} is stated with the factors that change it."),
        ("Can I finance {t}?", "Finance and lease structures for {t} are set out with indicative monthly figures."),
    ]
    chosen = rng.sample(templates, k=rng.randint(3, 6))
    return {
        "pairs": [
            {
                "question": q.format(t=title),
                "answer": a.format(t=title),
                "target_engine": rng.choice(engines),
            }
            for q, a in chosen
        ]
    }


def _schema_graph(prompt: str, rng: random.Random) -> dict:
    title = _field(prompt, "TITLE", "Page")
    url = _field(prompt, "URL", "/")
    wanted = _list_field(prompt, "SCHEMA_TYPES") or ["FAQPage"]
    graphs = []
    for schema_type in wanted:
        if schema_type.lower().startswith("faq"):
            graphs.append(
                {
                    "schema_type": "FAQPage",
                    "json_ld": {
                        "@context": "https://schema.org",
                        "@type": "FAQPage",
                        "mainEntity": [
                            {
                                "@type": "Question",
                                "name": f"What is {title}?",
                                "acceptedAnswer": {
                                    "@type": "Answer",
                                    "text": f"{title} is described in detail on this page.",
                                },
                            }
                        ],
                    },
                }
            )
        elif schema_type.lower().startswith("product"):
            graphs.append(
                {
                    "schema_type": "Product",
                    "json_ld": {
                        "@context": "https://schema.org",
                        "@type": "Product",
                        "name": title,
                        "url": url,
                        "offers": {
                            "@type": "Offer",
                            "priceCurrency": "USD",
                            "price": str(rng.randrange(18_000, 74_000, 500)),
                            "availability": "https://schema.org/InStock",
                        },
                    },
                }
            )
        elif schema_type.lower().startswith("local"):
            graphs.append(
                {
                    "schema_type": "LocalBusiness",
                    "json_ld": {
                        "@context": "https://schema.org",
                        "@type": "LocalBusiness",
                        "name": title,
                        "url": url,
                        "openingHours": "Mo-Sa 09:00-18:00",
                    },
                }
            )
        else:
            graphs.append(
                {
                    "schema_type": "ItemList",
                    "json_ld": {
                        "@context": "https://schema.org",
                        "@type": "ItemList",
                        "name": title,
                        "numberOfItems": rng.randint(3, 12),
                    },
                }
            )
    return {"patches": graphs}


def _backlink_targets(prompt: str, rng: random.Random) -> dict:
    industry = _field(prompt, "INDUSTRY", "automotive")
    stems = [
        "central", "daily", "review", "guide", "insider", "weekly", "digest",
        "journal", "report", "hub", "network", "press",
    ]
    kinds = ["Guest Post", "Directory", "Resource Page"]
    targets = []
    for _ in range(rng.randint(3, 6)):
        stem = rng.choice(stems)
        tld = rng.choice(["com", "org", "net", "io"])
        targets.append(
            {
                "domain": f"{industry[:8]}-{stem}.{tld}",
                "authority": rng.randint(38, 88),
                "placement_type": rng.choice(kinds),
                "relevance": round(rng.uniform(0.62, 0.95), 2),
                "contact_email": f"editor@{industry[:8]}-{stem}.{tld}",
                "discovered_via": rng.choice(
                    ["topical crawl", "competitor gap", "resource index", "embedding match"]
                ),
            }
        )
    return {"targets": targets}


def _pr_pitch(prompt: str, rng: random.Random) -> dict:
    domain = _field(prompt, "DOMAIN", "example.com")
    brand = _field(prompt, "BRAND", "our team")
    angle = _field(prompt, "ANGLE", "an original data study")
    tone = rng.choice(["warm", "professional", "concise", "enthusiastic"])
    return {
        "subject": f"Contribution for {domain}: {angle}",
        "body": (
            f"Hi,\n\nI read your recent coverage on {domain} and thought {angle} would "
            f"suit your readers. {brand} has first-party data on this we would happily "
            "share, along with a draft that needs no editing to run.\n\n"
            "Happy to send the outline if useful.\n\nBest regards"
        ),
        "tone": tone,
        "sentiment_score": round(rng.uniform(0.35, 0.85), 2),
        "variant": rng.choice(["A", "B", "C"]),
    }


def _creative_variants(prompt: str, rng: random.Random) -> dict:
    product = _field(prompt, "PRODUCT", "our lineup")
    platforms = _list_field(prompt, "PLATFORMS") or ["Google", "Meta", "LinkedIn", "TikTok"]
    sizes = {
        "google": ["300x250", "728x90", "160x600"],
        "meta": ["1080x1080", "1200x628"],
        "linkedin": ["1200x627", "300x250"],
        "tiktok": ["1080x1920"],
        "dsp": ["300x600", "970x250"],
    }
    hooks = [
        "Built for the drive you actually do",
        "Priced to move this month",
        "The numbers speak for themselves",
        "See it before you decide",
        "Certified, inspected, ready",
    ]
    ctas = ["See offers", "Book a test drive", "Check availability", "Get a quote"]
    variants = []
    for platform in platforms:
        key = platform.lower().split()[0]
        for dims in sizes.get(key, ["300x250"])[: rng.randint(1, 2)]:
            variants.append(
                {
                    "platform": platform,
                    "dimensions": dims,
                    "headline": rng.choice(hooks),
                    "body_copy": f"{product} — full details, current pricing, local stock.",
                    "call_to_action": rng.choice(ctas),
                    "audience_segment": rng.choice(
                        ["in-market shoppers", "lookalike — high intent", "service retention",
                         "lease renewals"]
                    ),
                }
            )
    return {"variants": variants}


def _budget_plan(prompt: str, rng: random.Random) -> dict:
    return {
        "rationale": (
            "Shifted share toward the lowest measured CAC channels, capped per-channel "
            "movement to limit volatility, and left locked channels untouched."
        ),
        "confidence": round(rng.uniform(0.68, 0.94), 2),
    }


def _audience_clusters(prompt: str, rng: random.Random) -> dict:
    signals = [
        "inventory page depth", "finance calculator use", "service booking",
        "trade-in valuation", "location page views", "brochure download",
        "return visit within 7d", "comparison tool use",
    ]
    names = [
        "High-intent researchers", "Service retention", "Lease renewal window",
        "First-time buyers", "EV-curious commuters", "Trade-in ready",
    ]
    clusters = []
    for name in rng.sample(names, k=rng.randint(2, 4)):
        clusters.append(
            {
                "label": name,
                "size": rng.randrange(1_200, 48_000, 100),
                "cohesion": round(rng.uniform(0.55, 0.93), 2),
                "top_signals": rng.sample(signals, k=3),
            }
        )
    return {"clusters": clusters}


def _fraud_assessment(prompt: str, rng: random.Random) -> dict:
    reasons = [
        "Non-human click pattern", "IP farm cluster detected", "Sub-1s bounce spike",
        "Bot signature match", "Duplicate device fingerprint", "Impossible geo velocity",
        "Datacentre ASN origin",
    ]
    return {
        "verdict": "fraudulent" if rng.random() < 0.65 else "clean",
        "reason": rng.choice(reasons),
        "confidence": round(rng.uniform(0.72, 0.99), 2),
    }


def _spam_classification(prompt: str, rng: random.Random) -> dict:
    referrer = _field(prompt, "REFERRER", "unknown.tld")
    categories = [
        "Adult / Porn spam", "Ghost referral bot", "Spam / link-drop bot",
        "Crypto scam referrer", "Scraper / crawler farm",
    ]
    return {
        "referrer": referrer,
        "category": rng.choice(categories),
        "is_spam": True,
        "confidence": round(rng.uniform(0.8, 0.99), 2),
    }


def _generic(prompt: str, rng: random.Random) -> dict:
    return {
        "ok": True,
        "note": "FakeLLMProvider had no task-specific handler for this prompt.",
        "seed": rng.randint(1000, 9999),
    }


_HANDLERS = {
    "semantic_gap": _semantic_gap,
    "aeo_pairs": _aeo_pairs,
    "schema_graph": _schema_graph,
    "backlink_targets": _backlink_targets,
    "pr_pitch": _pr_pitch,
    "creative_variants": _creative_variants,
    "budget_plan": _budget_plan,
    "audience_clusters": _audience_clusters,
    "fraud_assessment": _fraud_assessment,
    "spam_classification": _spam_classification,
}
