"""Strict checks for a generated search campaign, and the repair behind them.

The model drafts; this decides what survives. Every line is checked against
the platform limits (characters, counts, duplicates), Google's editorial
rules (no exclamation mark in a headline, at most one in an ad, no words in
capitals unless they are the brand's own or an acronym, no repeated
punctuation or symbols), and the claims rule: a price, a percentage, a
number, "best", "#1", "free", "guaranteed" and the like only survive when the
brand profile itself says it.

Repair is by dropping. A line that breaks a rule is removed, never
rewritten, because rewriting changes what the ad claims. An ad group that is
left below the minimums is dropped as a whole, and the caller asks the model
again once when the campaign is left with fewer than two.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.agents.base.style import humanise
from app.connectors.google_ads.connector import (
    CALLOUT_CHARS,
    RSA_DESCRIPTION_CHARS,
    RSA_HEADLINE_CHARS,
    RSA_MAX_DESCRIPTIONS,
    RSA_MAX_HEADLINES,
    RSA_MIN_DESCRIPTIONS,
    RSA_MIN_HEADLINES,
    RSA_PATH_CHARS,
    SITELINK_DESCRIPTION_CHARS,
    SITELINK_TEXT_CHARS,
)

MIN_AD_GROUPS, MAX_AD_GROUPS = 2, 6
MIN_KEYWORDS, MAX_KEYWORDS = 8, 20
MAX_NEGATIVES = 50
MAX_SITELINKS, MIN_SITELINKS = 4, 2
MAX_CALLOUTS = 6
#: Google Ads keyword limits: 80 characters, 10 words.
KEYWORD_CHARS, KEYWORD_WORDS = 80, 10
MATCH_TYPES = ("exact", "phrase", "broad")

#: Acronyms that may stay in capitals even when the profile does not use them.
ACRONYMS = frozenset(
    {"AC", "AI", "API", "B2B", "B2C", "CA", "CRM", "EMI", "ERP", "EU", "EV", "FAQ", "GST", "HR",
     "IT", "KYC", "NRI", "PDF", "PR", "SEO", "SUV", "UAE", "UK", "US", "USA", "VAT"}
)

#: Claims the business has to make itself before an ad may. Each is matched
#: against the ad line and against the brand profile; it survives only when
#: both match.
CLAIMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("best", re.compile(r"\bbest\b", re.I)),
    ("#1", re.compile(r"#\s?1\b|\bno\.?\s?1\b|\bnumber one\b", re.I)),
    ("cheapest", re.compile(r"\bcheapest\b|\blowest[- ]price", re.I)),
    ("guarantee", re.compile(r"\bguarant", re.I)),
    ("free", re.compile(r"(?<![-\w])free\b", re.I)),
    ("discount", re.compile(r"\bdiscount|\bcoupon|\bpromo code|%\s?off\b|\bclearance\b", re.I)),
    ("award", re.compile(r"\baward", re.I)),
    ("top rated", re.compile(r"\btop[- ]rated\b|\bhighest[- ]rated\b|\b5[- ]star\b", re.I)),
    ("leading", re.compile(r"\bleading\b|\bmarket leader\b", re.I)),
    ("unbeatable", re.compile(r"\bunbeatable\b|\bunmatched\b|\bworld[- ]class\b|\bno\.? one beats\b", re.I)),
    ("certified", re.compile(r"\bcertified\b|\baccredited\b|\blicensed\b", re.I)),
)
#: A number in an ad (a price, a percentage, years, a count) must be one the
#: profile states.
_NUMBER = re.compile(r"\d[\d,.]*")
_CURRENCY = re.compile(r"[₹$€£¥]|\b(rs|inr|usd|eur|gbp)\b\.?", re.I)
_REPEATED_PUNCTUATION = re.compile(r"([!?.,:;*])\1")
#: Characters Google Ads does not accept in a keyword.
_KEYWORD_INVALID = re.compile(r"[!@%^*()={};~`<>?\\|,\[\]\"+]")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9&']*")


@dataclass(slots=True)
class Rules:
    """What the checks are measured against for one business."""

    #: The brand profile as text, for the claims rule.
    profile_text: str = ""
    #: Words allowed in capitals: the brand's own spelling plus acronyms.
    allowed_caps: frozenset[str] = frozenset()
    #: Landing pages the ads may link to, normalised (see ``normalise_url``).
    landing_pages: dict[str, str] = field(default_factory=dict)
    #: The page used when an ad group's own choice is not a known page.
    fallback_url: str = ""

    @classmethod
    def build(
        cls,
        *,
        profile_text: str,
        business_name: str,
        pages: list[str],
        fallback_url: str,
        locations: str = "",
    ) -> Rules:
        """``locations`` (the agent setting) counts as the business's own words,
        so a sector or postcode number in it may appear in an ad."""
        own = f"{profile_text}\n{business_name}\n{locations}"
        caps = {w for w in _WORD.findall(own) if _all_caps(w)}
        return cls(
            profile_text=own,
            allowed_caps=frozenset(caps) | ACRONYMS,
            landing_pages={normalise_url(url): url for url in pages if normalise_url(url)},
            fallback_url=fallback_url,
        )


@dataclass(slots=True)
class Report:
    """What validation removed, for the run summary and the retry prompt."""

    dropped_lines: int = 0
    dropped_groups: int = 0
    reasons: list[str] = field(default_factory=list)

    def note(self, reason: str) -> None:
        self.dropped_lines += 1
        if reason not in self.reasons:
            self.reasons.append(reason)


# ── Small helpers ──────────────────────────────────────────────────────────
def _clean(text: object) -> str:
    return humanise(" ".join(str(text or "").split())).strip()


def _all_caps(word: str) -> bool:
    letters = [ch for ch in word if ch.isalpha()]
    return len(letters) >= 2 and all(ch.isupper() for ch in letters)


def normalise_url(url: str) -> str:
    """``https://Example.com/a/`` and ``https://example.com/a`` are one page."""
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    host = parts.netloc.lower().removeprefix("www.")
    return f"{host}{parts.path.rstrip('/')}" + (f"?{parts.query}" if parts.query else "")


def text_problem(text: str, rules: Rules, *, limit: int, headline: bool = False) -> str | None:
    """Why one line of ad text cannot be used, or ``None``."""
    if not text:
        return "empty line"
    if len(text) > limit:
        return f"longer than {limit} characters"
    if headline and "!" in text:
        return "exclamation mark in a headline"
    if text.count("!") > 1:
        return "more than one exclamation mark"
    if _REPEATED_PUNCTUATION.search(text):
        return "repeated punctuation"
    if any(unicodedata.category(ch) == "So" for ch in text):
        return "symbols or emoji"
    for word in _WORD.findall(text):
        if _all_caps(word) and word not in rules.allowed_caps:
            return "a word in capitals"
    for label, pattern in CLAIMS:
        if pattern.search(text) and not pattern.search(rules.profile_text):
            return f"a claim the brand profile does not make ({label})"
    if _CURRENCY.search(text) and not _CURRENCY.search(rules.profile_text):
        return "a price the brand profile does not state"
    for number in _NUMBER.findall(text):
        figure = number.rstrip(".,")
        if figure and not re.search(rf"(?<!\d){re.escape(figure)}(?!\d)", rules.profile_text):
            return "a number the brand profile does not state"
    return None


def _lines(raw: object, rules: Rules, report: Report, *, limit: int, cap: int, headline: bool) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        text = _clean(item)
        problem = text_problem(text, rules, limit=limit, headline=headline)
        if problem is None and text.lower() in seen:
            problem = "duplicate line"
        if problem:
            report.note(f"{'headline' if headline else 'line'}: {problem}")
            continue
        seen.add(text.lower())
        out.append(text)
    if len(out) > cap:
        out = out[:cap]
    return out


def clean_keyword(raw: object) -> str:
    """A keyword as Google accepts it: lower case, no match-type syntax."""
    text = " ".join(_KEYWORD_INVALID.sub(" ", str(raw or "").lower()).split()).strip(" .-'")
    if not text or len(text) > KEYWORD_CHARS or len(text.split()) > KEYWORD_WORDS:
        return ""
    return text


def _keywords(raw: object, report: Report, *, cap: int, exclude: set[str] = frozenset()) -> list[dict]:  # type: ignore[assignment]
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        text = clean_keyword(item.get("text"))
        match = str(item.get("match_type") or item.get("match") or "phrase").strip().lower()
        if match not in MATCH_TYPES:
            match = "phrase"
        if not text:
            report.note("keyword: unusable text")
            continue
        if (text, match) in seen or text in exclude:
            report.note("keyword: duplicate")
            continue
        seen.add((text, match))
        out.append({"text": text, "match_type": match})
        if len(out) == cap:
            break
    return out


def path_field(raw: object) -> str:
    """A display path: no spaces, letters, digits and hyphens, 15 characters."""
    text = re.sub(r"[^\w-]", "", "-".join(str(raw or "").split()), flags=re.UNICODE)
    return text[:RSA_PATH_CHARS].strip("-_")


def landing_url(raw: object, rules: Rules, *, hints: list[str]) -> str:
    """The ad group's landing page: its own choice if it is a known page.

    Otherwise the known page whose address shares most words with the ad
    group's theme and keywords, and failing that the fallback (home) page.
    """
    choice = rules.landing_pages.get(normalise_url(str(raw or "")))
    if choice:
        return choice
    words = {w for hint in hints for w in re.findall(r"[a-z0-9]+", hint.lower()) if len(w) > 2}
    best, best_score = "", 0
    for key, url in rules.landing_pages.items():
        score = len(words & set(re.findall(r"[a-z0-9]+", key)))
        if score > best_score:
            best, best_score = url, score
    return best or rules.fallback_url


# ── The campaign ───────────────────────────────────────────────────────────
def ad_group(raw: dict, rules: Rules, report: Report) -> tuple[dict | None, str]:
    """A cleaned ad group, or ``None`` with the reason it cannot be used."""
    name = _clean(raw.get("name"))[:255]
    theme = _clean(raw.get("theme")) or name
    if not name:
        return None, "an ad group had no name"
    keywords = _keywords(raw.get("keywords"), report, cap=MAX_KEYWORDS)
    ad = raw.get("ad") if isinstance(raw.get("ad"), dict) else raw
    headlines = _lines(ad.get("headlines"), rules, report, limit=RSA_HEADLINE_CHARS,
                       cap=RSA_MAX_HEADLINES, headline=True)
    descriptions = _lines(ad.get("descriptions"), rules, report, limit=RSA_DESCRIPTION_CHARS,
                          cap=RSA_MAX_DESCRIPTIONS, headline=False)
    # At most one exclamation mark in the whole ad.
    kept, bang = [], False
    for line in descriptions:
        if "!" in line:
            if bang:
                report.note("line: more than one exclamation mark in the ad")
                continue
            bang = True
        kept.append(line)
    descriptions = kept
    if len(keywords) < MIN_KEYWORDS:
        return None, f"{name!r} was left with {len(keywords)} keywords (needs {MIN_KEYWORDS})"
    if len(headlines) < RSA_MIN_HEADLINES:
        return None, f"{name!r} was left with {len(headlines)} headlines (needs {RSA_MIN_HEADLINES})"
    if len(descriptions) < RSA_MIN_DESCRIPTIONS:
        return None, f"{name!r} was left with {len(descriptions)} descriptions (needs {RSA_MIN_DESCRIPTIONS})"
    url = landing_url(raw.get("landing_url"), rules, hints=[theme, name, *(k["text"] for k in keywords)])
    if not url:
        return None, f"{name!r} has no landing page on your website"
    path1, path2 = path_field(ad.get("path1")), path_field(ad.get("path2"))
    if not path1:
        path1, path2 = path2, ""
    return {
        "name": name,
        "theme": theme,
        "landing_url": url,
        "keywords": keywords,
        "ad": {"headlines": headlines, "descriptions": descriptions, "path1": path1, "path2": path2},
    }, ""


def sitelinks(raw: object, rules: Rules, report: Report) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        text = _clean(item.get("text"))
        problem = text_problem(text, rules, limit=SITELINK_TEXT_CHARS, headline=True)
        url = rules.landing_pages.get(normalise_url(str(item.get("url") or "")))
        if problem is None and not url:
            problem = "not a page on your website"
        if problem is None and text.lower() in seen:
            problem = "duplicate line"
        if problem:
            report.note(f"sitelink: {problem}")
            continue
        first, second = _clean(item.get("description1")), _clean(item.get("description2"))
        if any(
            text_problem(line, rules, limit=SITELINK_DESCRIPTION_CHARS) for line in (first, second)
        ):
            # Both description lines or neither.
            if first or second:
                report.note("sitelink: description dropped")
            first = second = ""
        seen.add(text.lower())
        out.append({"text": text, "description1": first, "description2": second, "url": url})
        if len(out) == MAX_SITELINKS:
            break
    if len(out) < MIN_SITELINKS:
        return []
    return out


def campaign(raw: object, rules: Rules) -> tuple[dict, Report, list[str]]:
    """Clean a model draft. Returns the campaign, what was removed, and problems."""
    data = raw if isinstance(raw, dict) else {}
    report = Report()
    problems: list[str] = []
    groups: list[dict] = []
    names: set[str] = set()
    for item in data.get("ad_groups") or []:
        if not isinstance(item, dict):
            continue
        group, problem = ad_group(item, rules, report)
        if group is None:
            report.dropped_groups += 1
            problems.append(problem)
            continue
        if group["name"].lower() in names:
            report.dropped_groups += 1
            problems.append(f"{group['name']!r} appeared twice")
            continue
        names.add(group["name"].lower())
        groups.append(group)
        if len(groups) == MAX_AD_GROUPS:
            break
    positives = {k["text"] for g in groups for k in g["keywords"]}
    negatives = _keywords(data.get("negative_keywords"), report, cap=MAX_NEGATIVES, exclude=positives)
    callouts = _lines(data.get("callouts"), rules, report, limit=CALLOUT_CHARS, cap=MAX_CALLOUTS,
                      headline=True)
    cleaned = {
        "name": _clean(data.get("campaign_name"))[:200],
        "ad_groups": groups,
        "negative_keywords": negatives,
        "sitelinks": sitelinks(data.get("sitelinks"), rules, report),
        "callouts": callouts,
    }
    if len(groups) < MIN_AD_GROUPS:
        problems.append(f"only {len(groups)} usable ad groups (needs {MIN_AD_GROUPS})")
    return cleaned, report, problems


def usable(cleaned: dict) -> bool:
    return len(cleaned.get("ad_groups") or []) >= MIN_AD_GROUPS


def merge(first: dict, second: dict) -> dict:
    """The retry's ad groups added to the first attempt's, without repeats."""
    names = {g["name"].lower() for g in first.get("ad_groups") or []}
    groups = list(first.get("ad_groups") or [])
    for group in second.get("ad_groups") or []:
        if group["name"].lower() not in names and len(groups) < MAX_AD_GROUPS:
            groups.append(group)
            names.add(group["name"].lower())
    positives = {k["text"] for g in groups for k in g["keywords"]}
    negatives = [
        k for k in (second.get("negative_keywords") or first.get("negative_keywords") or [])
        if k["text"] not in positives
    ]
    return {
        "name": second.get("name") or first.get("name") or "",
        "ad_groups": groups,
        "negative_keywords": negatives,
        "sitelinks": second.get("sitelinks") or first.get("sitelinks") or [],
        "callouts": second.get("callouts") or first.get("callouts") or [],
    }
