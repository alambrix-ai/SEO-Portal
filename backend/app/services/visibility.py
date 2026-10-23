"""AI visibility attribution and persistence helpers.

Scraped / model-returned URLs and answer text are untrusted. This module only
normalises hostnames and stores structured observations — it never follows
instructions found in answer text.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base.aeo_base import domain_matches, normalise_host
from app.connectors.base.interfaces import VisibilityProbe
from app.db.base import utcnow
from app.models.visibility import (
    VisibilityObservation,
    VisibilityQuery,
    VisibilitySettings,
)
from app.services.encryption import Ctx, OrgCipher

#: Max competitor domains a workspace may track.
MAX_COMPETITORS = 40
#: Max active questions probed per agent run (matches connector cap).
MAX_QUERIES_PER_RUN = 6
#: Stored answer excerpt length (characters) after encryption input trim.
MAX_ANSWER_EXCERPT_CHARS = 2_000


@dataclass(frozen=True, slots=True)
class Attribution:
    brand_cited: bool
    brand_position: int | None
    cited_domains: list[str]
    competitors_cited: list[str]


def attribute_sources(
    sources: list[str],
    *,
    brand_domain: str,
    competitors: list[str],
) -> Attribution:
    """Map ordered citation URLs to brand / competitor / other hosts."""
    seen: list[str] = []
    for url in sources:
        host = normalise_host(url)
        if host and host not in seen:
            seen.append(host)

    brand_position: int | None = None
    for position, url in enumerate(sources, start=1):
        if domain_matches(url, brand_domain):
            brand_position = position
            break

    competitor_hosts = [normalise_host(c) for c in competitors]
    competitor_hosts = [h for h in competitor_hosts if h]
    flagged: list[str] = []
    for host in seen:
        for rival in competitor_hosts:
            if host == rival or host.endswith(f".{rival}"):
                if host not in flagged:
                    flagged.append(host)
                break

    return Attribution(
        brand_cited=brand_position is not None,
        brand_position=brand_position,
        cited_domains=seen,
        competitors_cited=flagged,
    )


def normalise_competitor_list(raw: list[str] | None) -> list[str]:
    """Dedupe and normalise competitor hostnames; drop empties."""
    out: list[str] = []
    for item in raw or []:
        if not isinstance(item, str):
            continue
        host = normalise_host(item.strip())
        if host and host not in out:
            out.append(host)
        if len(out) >= MAX_COMPETITORS:
            break
    return out


def get_or_create_settings(db: Session, tenant_id: str) -> VisibilitySettings:
    row = db.execute(
        select(VisibilitySettings).where(VisibilitySettings.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if row is not None:
        return row
    row = VisibilitySettings(tenant_id=tenant_id, competitor_domains=[])
    db.add(row)
    db.flush()
    return row


def record_observation(
    db: Session,
    *,
    tenant_id: str,
    cipher: OrgCipher,
    query: VisibilityQuery | None,
    question: str,
    probe: VisibilityProbe,
    brand_domain: str,
    competitors: list[str],
    run_id: str = "",
) -> VisibilityObservation:
    """Persist one probe as a tenant-scoped observation."""
    attr = attribute_sources(
        probe.sources,
        brand_domain=brand_domain,
        competitors=competitors,
    )
    excerpt = (probe.answer_excerpt or "")[:MAX_ANSWER_EXCERPT_CHARS]
    obs = VisibilityObservation(
        tenant_id=tenant_id,
        query_id=query.id if query is not None else None,
        question_encrypted=cipher.encrypt(question, context=Ctx.VISIBILITY_QUESTION),
        engine=(probe.engine or "")[:32],
        brand_cited=attr.brand_cited,
        brand_position=attr.brand_position,
        cited_domains=attr.cited_domains,
        competitors_cited=attr.competitors_cited,
        answer_excerpt_encrypted=(
            cipher.encrypt(excerpt, context=Ctx.VISIBILITY_ANSWER) if excerpt else ""
        ),
        run_id=(run_id or "")[:36],
        observed_at=utcnow(),
    )
    db.add(obs)
    return obs
