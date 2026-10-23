"""Lexical topic clustering from Search Console queries and page keywords.

Deterministic and dependency-free: no embeddings service. Overlap uses the
same stop-worded tokenisation style as internal linking / article writer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_STOP = frozenset(
    "a an and are as at be by for from has have if in into is it its of on or "
    "our the their this to vs was what when where which who why will with you "
    "your best top guide how near me".split()
)
_WORD = re.compile(r"[a-z0-9]+", re.I)
#: Minimum Jaccard overlap to merge two seed phrases into one cluster.
MERGE_THRESHOLD = 0.35
#: Soft cap on clusters returned per run.
MAX_CLUSTERS = 12


@dataclass(frozen=True, slots=True)
class PageRef:
    id: str
    url: str
    title: str
    keywords: tuple[str, ...] = ()


@dataclass(slots=True)
class ClusterDraft:
    name: str
    seed_terms: list[str] = field(default_factory=list)
    page_ids: list[str] = field(default_factory=list)
    page_relevance: dict[str, float] = field(default_factory=dict)
    gap_score: float = 0.0
    #: Filled after the cluster row is persisted.
    cluster_id: str | None = None


def tokens(text: str) -> set[str]:
    return {
        w
        for w in _WORD.findall((text or "").lower())
        if len(w) > 1 and w not in _STOP
    }


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def normalise_phrase(text: str) -> str:
    return " ".join(_WORD.findall((text or "").lower()))


def cluster_queries(
    queries: list[str],
    pages: list[PageRef],
    *,
    max_clusters: int = MAX_CLUSTERS,
) -> list[ClusterDraft]:
    """Group queries by token overlap, attach covering pages, score gaps.

    Gap score is high when few synced pages overlap the cluster's seeds
    (0 = well covered, 100 = no covering page).
    """
    seeds = []
    seen: set[str] = set()
    for raw in queries:
        phrase = normalise_phrase(raw)
        if not phrase or phrase in seen:
            continue
        seen.add(phrase)
        seeds.append(phrase)
    if not seeds:
        # Fall back to page keywords when GSC is empty.
        for page in pages:
            for keyword in page.keywords:
                phrase = normalise_phrase(keyword)
                if phrase and phrase not in seen:
                    seen.add(phrase)
                    seeds.append(phrase)

    groups: list[list[str]] = []
    group_tokens: list[set[str]] = []
    for phrase in seeds:
        pt = tokens(phrase)
        if not pt:
            continue
        placed = False
        for index, gt in enumerate(group_tokens):
            if jaccard(pt, gt) >= MERGE_THRESHOLD:
                groups[index].append(phrase)
                group_tokens[index] = gt | pt
                placed = True
                break
        if not placed:
            groups.append([phrase])
            group_tokens.append(pt)

    # Largest groups first; drop singletons when we already have richer ones.
    ranked = sorted(
        zip(groups, group_tokens, strict=True),
        key=lambda pair: (-len(pair[0]), -len(pair[1]), pair[0][0]),
    )

    page_token_map = [
        (
            page,
            tokens(page.title)
            | tokens(page.url.replace("/", " ").replace("-", " "))
            | tokens(" ".join(page.keywords)),
        )
        for page in pages
    ]

    drafts: list[ClusterDraft] = []
    for group, gt in ranked:
        if len(drafts) >= max_clusters:
            break
        if len(group) == 1 and len(gt) < 2 and drafts:
            continue
        name = _name_for(group)
        members: list[str] = []
        relevance: dict[str, float] = {}
        for page, pt in page_token_map:
            score = jaccard(pt, gt)
            if score >= 0.15:
                members.append(page.id)
                relevance[page.id] = round(score, 4)
        coverage = min(1.0, len(members) / 3.0) if members else 0.0
        gap = round((1.0 - coverage) * 100.0, 1)
        drafts.append(
            ClusterDraft(
                name=name[:160],
                seed_terms=group[:24],
                page_ids=members,
                page_relevance=relevance,
                gap_score=gap,
            )
        )
    drafts.sort(key=lambda d: (-d.gap_score, -len(d.seed_terms), d.name))
    return drafts


def brief_outline(*, cluster_name: str, seed_terms: list[str], existing_titles: list[str]) -> dict:
    """Deterministic brief structure (no LLM required for the happy path)."""
    primary = seed_terms[0] if seed_terms else cluster_name
    title = f"The complete guide to {primary}".strip()[:255]
    must_cover = seed_terms[:8] or [cluster_name]
    avoid = [t for t in existing_titles if t][:8]
    body = (
        f"Cluster: {cluster_name}\n"
        f"Primary topic: {primary}\n"
        f"Must cover: {', '.join(must_cover)}\n"
        f"Angle: Practical buyer guide that answers the cluster's queries "
        f"without duplicating: {', '.join(avoid) if avoid else '(no close pages yet)'}.\n"
        f"CTA: Help the reader take the next step related to {primary}.\n"
    )
    return {
        "title": title,
        "target_keywords": must_cover,
        "body": body,
        "cluster_name": cluster_name,
    }


def validate_brief_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Brief payload must be an object")
    title = str(payload.get("title") or "").strip()
    body = str(payload.get("body") or "").strip()
    cluster_id = str(payload.get("cluster_id") or "") or None
    brief_id = str(payload.get("brief_id") or "")
    if not title or not body or not brief_id:
        raise ValueError("Brief payload needs title, body, and brief_id")
    keywords = payload.get("target_keywords") or []
    if not isinstance(keywords, list):
        raise ValueError("target_keywords must be a list")
    return {
        "brief_id": brief_id,
        "cluster_id": cluster_id,
        "title": title[:255],
        "body": body[:20_000],
        "target_keywords": [str(k)[:120] for k in keywords[:24]],
        "cluster_name": str(payload.get("cluster_name") or "")[:160],
    }


def _name_for(group: list[str]) -> str:
    # Prefer the shortest seed as a readable cluster label.
    ordered = sorted(group, key=lambda s: (len(s), s))
    label = ordered[0]
    return label[:1].upper() + label[1:] if label else "Topic"
