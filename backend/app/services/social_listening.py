"""Sentiment scoring and reply drafts for inbound social mentions."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.llm import untrusted

_NEG = frozenset(
    "awful bad broken delayed disappointed fraud horrible late scam slow terrible "
    "rude refund cancel never worst angry upset fail failed failing".split()
)
_POS = frozenset(
    "amazing awesome great love loved loving excellent fantastic helpful perfect "
    "recommend recommended thanks thank wonderful".split()
)
_WORD = re.compile(r"[a-z0-9']+", re.I)


@dataclass(frozen=True, slots=True)
class MentionIn:
    network: str
    external_id: str
    author: str
    text: str


@dataclass(frozen=True, slots=True)
class Sentiment:
    label: str  # positive | neutral | negative
    score: float


def parse_scope_mentions(scope: str) -> list[MentionIn]:
    """Parse ``network|external_id|author|text`` lines from agent scope (tests / manual)."""
    out: list[MentionIn] = []
    for line in (scope or "").splitlines():
        parts = [p.strip() for p in line.split("|", 3)]
        if len(parts) != 4:
            continue
        network, external_id, author, text = parts
        if not network or not external_id or not text:
            continue
        out.append(
            MentionIn(
                network=network[:40].lower(),
                external_id=external_id[:128],
                author=author[:200],
                text=text[:4000],
            )
        )
    return out[:50]


def score_sentiment(text: str) -> Sentiment:
    tokens = [t.lower() for t in _WORD.findall(text or "")]
    if not tokens:
        return Sentiment("neutral", 0.0)
    neg = sum(1 for t in tokens if t in _NEG)
    pos = sum(1 for t in tokens if t in _POS)
    raw = (pos - neg) / max(len(tokens), 1)
    if neg > pos and neg > 0:
        return Sentiment("negative", round(raw, 4))
    if pos > neg and pos > 0:
        return Sentiment("positive", round(raw, 4))
    return Sentiment("neutral", round(raw, 4))


def draft_reply(*, business: str, author: str, text: str, sentiment: str) -> str:
    """Deterministic, non-LLM reply template — no invented claims."""
    business = (business or "our team").strip()[:80]
    author = (author or "there").strip()[:80] or "there"
    if sentiment == "negative":
        return (
            f"Hi {author}, thank you for sharing this with {business}. "
            "We're sorry this fell short — please contact us through our official "
            "support channel so we can make it right. We do not discuss private "
            "account details in public threads."
        )[:1000]
    if sentiment == "positive":
        return (
            f"Hi {author}, thank you for the kind words about {business} — "
            "we appreciate you."
        )[:1000]
    return (
        f"Hi {author}, thanks for mentioning {business}. "
        "We're here if you need anything."
    )[:1000]


def as_untrusted(text: str) -> str:
    return untrusted.wrap("social_mention", text or "")


def external_id_for(network: str, author: str, text: str) -> str:
    digest = hashlib.sha256(f"{network}|{author}|{text}".encode()).hexdigest()[:24]
    return f"m_{digest}"
