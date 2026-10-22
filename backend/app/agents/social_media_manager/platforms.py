"""Each network's rules, enforced in code rather than trusted to the model.

A post is kept as its parts — ``text``, ``hashtags``, ``link``, and a
``title`` for Pinterest and Reddit — and :func:`compose` builds what is
actually sent.
:func:`fit` makes a drafted post obey its network's limits (shortening, never
padding), and :func:`problem` re-checks an edited post just before it is
published, so a post that was lengthened by hand is refused with a reason
instead of being rejected by the platform.

Every limit is data on :class:`Network` (research R8, checked October 2026):
Instagram 2,200 characters and 30 hashtags, LinkedIn 3,000, X 280 (as X
counts them), Pinterest a 100-character title and an 800-character
description, Reddit a 300-character title and a 40,000-character body,
Facebook 63,206. Platforms change these without notice; when one does, the
figure here is the one line to change.

:data:`NETWORKS` are the networks the agent plans a week for.
:data:`PUBLISH_NETWORKS` adds those that are only published to when a person
schedules a post (Reddit: each post names its subreddit). :func:`rules_for`
looks a key up in the second.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True, slots=True)
class Network:
    key: str
    label: str
    #: The connector that publishes it.
    connector: str
    #: Characters allowed in the composed post.
    limit: int
    #: Image shape for images.generate.
    shape: str
    #: Local posting times, best first; enough for 14 posts a week.
    times: tuple[time, ...]
    weekdays_only: bool = False
    needs_image: bool = False
    needs_link: bool = False
    max_hashtags: int = 30
    links_clickable: bool = True
    #: The post needs a title (Reddit); Pinterest's is optional.
    needs_title: bool = False
    #: Characters allowed in the title; 0 when the network has none.
    title_limit: int = 0


NETWORKS: dict[str, Network] = {
    "facebook": Network(
        "facebook", "Facebook", "facebook_pages", 63206, "square",
        (time(13, 0), time(9, 30), time(18, 30)),
    ),
    "instagram": Network(
        "instagram", "Instagram", "instagram_business", 2200, "square",
        (time(11, 0), time(19, 0), time(8, 0)),
        needs_image=True, links_clickable=False,
    ),
    "linkedin": Network(
        "linkedin", "LinkedIn", "linkedin_pages", 3000, "square",
        (time(8, 30), time(12, 0), time(17, 0)), weekdays_only=True,
    ),
    "x": Network(
        "x", "X", "x_twitter", 280, "square",
        (time(12, 30), time(9, 0), time(17, 30)),
    ),
    "pinterest": Network(
        "pinterest", "Pinterest", "pinterest", 800, "portrait",
        (time(20, 30), time(14, 0), time(21, 30)),
        needs_image=True, needs_link=True, title_limit=100,
    ),
}
#: Reddit: published to only when a person schedules a post for a subreddit.
#: Hashtags mean nothing there and read as spam, so none are added.
REDDIT = Network(
    "reddit", "Reddit", "reddit", 40_000, "landscape",
    (time(9, 0), time(12, 30), time(18, 0)),
    max_hashtags=0, needs_title=True, title_limit=300,
)
#: Every network a post can be published to.
PUBLISH_NETWORKS: dict[str, Network] = {**NETWORKS, "reddit": REDDIT}
#: When nothing is connected and nothing was chosen, these are written as downloads.
FALLBACK_NETWORKS = ("facebook", "instagram", "linkedin")

PINTEREST_TITLE_LIMIT = NETWORKS["pinterest"].title_limit
REDDIT_TITLE_LIMIT = REDDIT.title_limit
#: Facebook allows 63,206 characters; short posts do far better.
FACEBOOK_TARGET_WORDS = 80
#: X counts every link as this many characters, whatever its length.
X_URL_LENGTH = 23

_URL = re.compile(r"https?://\S+", re.I)
_BARE_DOMAIN = re.compile(r"\b(?:www\.)\S+", re.I)


def rules_for(network: str) -> Network | None:
    """The rules for ``network``, including networks the agent does not plan for."""
    return PUBLISH_NETWORKS.get(network)


# ── Hashtags ───────────────────────────────────────────────────────────────
def clean_hashtags(raw: object, limit: int) -> list[str]:
    """``#Word`` tags, deduplicated, at most ``limit``."""
    if limit <= 0:
        return []
    items = raw if isinstance(raw, list) else re.split(r"[\s,]+", str(raw or ""))
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        word = re.sub(r"[^\w]", "", str(item or ""), flags=re.UNICODE)
        if not word or word.isdigit() or word.lower() in seen:
            continue
        seen.add(word.lower())
        out.append("#" + word)
        if len(out) >= limit:
            break
    return out


# ── Length ─────────────────────────────────────────────────────────────────
def x_length(value: str) -> int:
    """Length as X counts it: links are 23; most non-Latin characters and emoji count twice."""
    total = 0
    position = 0
    for match in _URL.finditer(value):
        total += _weighted(value[position : match.start()]) + X_URL_LENGTH
        position = match.end()
    return total + _weighted(value[position:])


def _weighted(value: str) -> int:
    count = 0
    for ch in value:
        point = ord(ch)
        light = point <= 4351 or 8192 <= point <= 8205 or 8208 <= point <= 8223 or 8242 <= point <= 8247
        count += 1 if light else 2
    return count


def length(network: str, value: str) -> int:
    return x_length(value) if network == "x" else len(value)


# ── Composition ────────────────────────────────────────────────────────────
def compose(network: str, text: str, hashtags: list[str], link: str = "") -> str:
    """The post body as it is sent (the Pinterest description or the Reddit
    body; the title is separate).

    Facebook and Reddit send the link as its own field, so it is not repeated
    in the text.
    """
    text = (text or "").strip()
    tags = " ".join(hashtags or [])
    if network == "x":
        parts = [text] + ([link] if link and link not in text else []) + ([tags] if tags else [])
        return " ".join(p for p in parts if p)
    if network == "pinterest":
        return " ".join(p for p in (text, tags) if p)
    if network == "reddit":
        return text
    pieces = [text]
    if network == "linkedin" and link and link not in text:
        pieces.append(link)
    if tags:
        pieces.append(tags)
    return "\n\n".join(p for p in pieces if p)


def _shorten(text: str, budget: int, network: str) -> str:
    """Cut ``text`` at a word boundary so it measures at most ``budget``."""
    if length(network, text) <= budget:
        return text
    words = text.split()
    while words and length(network, " ".join(words) + "…") > budget:
        words.pop()
    if not words:
        # One word longer than the budget (a title typed without spaces):
        # cut inside it rather than leaving nothing.
        cut = _hard_cut(text, budget - 1, network)
        return cut + "…" if cut and length(network, cut + "…") <= budget else ""
    return " ".join(words).rstrip(",;:-–—") + "…"


def _hard_cut(text: str, budget: int, network: str) -> str:
    """The longest start of ``text`` measuring at most ``budget``, in one pass."""
    if network != "x":
        return text[: max(0, budget)]
    total = 0
    for index, ch in enumerate(text):
        total += _weighted(ch)
        if total > budget:
            return text[:index]
    return text


def fit(network: str, post: dict, *, hashtag_limit: int) -> dict:
    """The post, made to obey the network's rules. Returns a new dict."""
    rules = PUBLISH_NETWORKS[network]
    text = str(post.get("text") or "").strip()
    hashtags = clean_hashtags(post.get("hashtags"), min(hashtag_limit, rules.max_hashtags))
    # Hashtags written inline count against the same budget; keep them in the text.
    link = str(post.get("link") or "").strip()
    title = " ".join(str(post.get("title") or "").split())

    if not rules.links_clickable:
        # Instagram captions cannot carry clickable links; a URL there is just noise.
        text = _BARE_DOMAIN.sub("", _URL.sub("", text))
        text = re.sub(r"[ \t]{2,}", " ", text).strip()
        link = ""
        inline = len(re.findall(r"(?<![\w#])#\w+", text))
        hashtags = hashtags[: max(0, rules.max_hashtags - inline)]

    if network == "facebook":
        words = text.split()
        if len(words) > FACEBOOK_TARGET_WORDS * 2:
            text = " ".join(words[: FACEBOOK_TARGET_WORDS * 2]) + "…"

    if rules.title_limit:
        title = _shorten(title or text.split(".")[0], rules.title_limit, network)

    # Over the limit: hashtags go first, then the text is shortened.
    while hashtags and length(network, compose(network, text, hashtags, link)) > rules.limit:
        hashtags = hashtags[:-1]
    overflow = length(network, compose(network, text, hashtags, link)) - rules.limit
    if overflow > 0:
        text = _shorten(text, max(0, length(network, text) - overflow), network)

    return {**post, "text": text, "hashtags": hashtags, "link": link, "title": title if rules.title_limit else ""}


def problem(network: str, content: dict) -> str | None:
    """Why this post cannot be published as it stands, or None."""
    rules = PUBLISH_NETWORKS.get(network)
    if rules is None:
        return f"{network!r} is not a network this agent publishes to."
    text = str(content.get("text") or "").strip()
    hashtags = list(content.get("hashtags") or [])
    link = str(content.get("link") or "")
    if not text:
        return "This post has no text."
    body = compose(network, text, hashtags, link)
    size = length(network, body)
    if size > rules.limit:
        what = {"pinterest": "description", "reddit": "post text"}.get(network, "post")
        return (
            f"This {rules.label} {what} is {size} characters"
            + (" as X counts them (each link counts as 23)" if network == "x" else "")
            + f"; {rules.label} allows {rules.limit}. Shorten it and publish again."
        )
    if network == "instagram":
        tags = len(hashtags) + len(re.findall(r"(?<![\w#])#\w+", text))
        if tags > rules.max_hashtags:
            return f"Instagram allows at most {rules.max_hashtags} hashtags; this post has {tags}."
    if rules.title_limit:
        title = " ".join(str(content.get("title") or "").split())
        noun = "Pin title" if network == "pinterest" else f"{rules.label} title"
        if rules.needs_title and not title:
            return f"A {rules.label} post needs a title. Add one and publish again."
        if len(title) > rules.title_limit:
            return f"The {noun} is {len(title)} characters; {rules.label} allows {rules.title_limit}."
    if not rules.max_hashtags and hashtags:
        return f"{rules.label} posts don't use hashtags. Remove them and publish again."
    if network == "pinterest" and not link:
        return "A Pin needs a link to your website."
    return None


# ── Scheduling ─────────────────────────────────────────────────────────────
def zone_for(org_settings: dict | None) -> tuple[ZoneInfo | timezone, str]:
    """The workspace's time zone if it has a valid one, else UTC."""
    name = str((org_settings or {}).get("timezone") or "").strip()
    if name:
        try:
            return ZoneInfo(name), name
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return timezone.utc, "UTC"


def week_days(today_local: date) -> list[date]:
    """The seven days the run plans for: tomorrow onwards."""
    return [today_local + timedelta(days=offset) for offset in range(1, 8)]


def slots(network: str, count: int, days: list[date], tz: ZoneInfo | timezone) -> list[datetime]:
    """``count`` posting times across ``days``, evenly spread, in UTC.

    One post a day at the network's best time while there are days to spare;
    more than one a day only when ``count`` needs it. LinkedIn skips weekends.
    """
    rules = PUBLISH_NETWORKS[network]
    usable = [d for d in days if not (rules.weekdays_only and d.weekday() >= 5)] or days
    if count <= 0:
        return []
    per_day = min(len(rules.times), math.ceil(count / len(usable)))
    chosen_times = sorted(rules.times[:per_day])
    grid = [datetime.combine(d, t, tzinfo=tz) for d in usable for t in chosen_times]
    count = min(count, len(grid))
    picks = [grid[math.floor(i * len(grid) / count)] for i in range(count)]
    return [p.astimezone(timezone.utc) for p in picks]
