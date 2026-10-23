"""Quality gates for programmatic pages: thin, near-dupe, cannibalisation."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_WORD = re.compile(r"[a-z0-9]+", re.I)
_STOP = frozenset(
    "a an and are as at be by for from has have if in into is it its of on or "
    "our the their this to vs was with you your".split()
)

#: Minimum distinct content tokens after stop-word removal.
MIN_UNIQUE_TOKENS = 40
#: Jaccard similarity above which two bodies are near-duplicates.
NEAR_DUPE_THRESHOLD = 0.85
#: Overlap with an existing SeoPage title/url/keywords that blocks publish.
CANNIBALISE_THRESHOLD = 0.75


@dataclass(frozen=True, slots=True)
class QualityResult:
    ok: bool
    reason: str = ""
    unique_tokens: int = 0
    content_hash: str = ""


def tokens(text: str) -> set[str]:
    return {
        w
        for w in _WORD.findall((text or "").lower())
        if len(w) > 1 and w not in _STOP
    }


def content_hash(body: str) -> str:
    normalised = " ".join(sorted(tokens(body)))
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def assess(
    body: str,
    *,
    title: str,
    path: str,
    existing_hashes: set[str],
    existing_page_tokens: list[set[str]],
) -> QualityResult:
    """Reject thin, near-duplicate, or cannibalising pages before approval."""
    body_tokens = tokens(body)
    digest = content_hash(body)
    count = len(body_tokens)
    if count < MIN_UNIQUE_TOKENS:
        return QualityResult(
            ok=False,
            reason=f"thin_content:{count}",
            unique_tokens=count,
            content_hash=digest,
        )
    if digest in existing_hashes:
        return QualityResult(
            ok=False,
            reason="near_duplicate:hash",
            unique_tokens=count,
            content_hash=digest,
        )
    page_sig = tokens(f"{title} {path.replace('/', ' ').replace('-', ' ')}")
    for other in existing_page_tokens:
        if jaccard(page_sig, other) >= CANNIBALISE_THRESHOLD:
            return QualityResult(
                ok=False,
                reason="cannibalisation",
                unique_tokens=count,
                content_hash=digest,
            )
        if jaccard(body_tokens, other) >= NEAR_DUPE_THRESHOLD:
            return QualityResult(
                ok=False,
                reason="near_duplicate:body",
                unique_tokens=count,
                content_hash=digest,
            )
    return QualityResult(ok=True, unique_tokens=count, content_hash=digest)


def parse_dataset_rows(raw_rows: list | dict) -> list[dict]:
    """Normalise uploaded JSON/CSV-shaped rows; reject non-objects."""
    if isinstance(raw_rows, dict):
        raw_rows = raw_rows.get("rows") or []
    if not isinstance(raw_rows, list):
        raise ValueError("Dataset must be a list of objects")
    out: list[dict] = []
    for row in raw_rows[:500]:
        if not isinstance(row, dict):
            continue
        cleaned = {
            str(k)[:64]: str(v)[:2000] if not isinstance(v, (int, float, bool)) else v
            for k, v in row.items()
            if str(k).isidentifier() or str(k).replace("_", "").isalnum()
        }
        if cleaned:
            out.append(cleaned)
    return out
