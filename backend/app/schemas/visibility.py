"""AI visibility tracker API models."""
from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from app.schemas.common import ApiModel
from app.services.visibility import MAX_COMPETITORS


class VisibilityQueryIn(ApiModel):
    question: str = Field(min_length=3, max_length=500)
    label: str = Field(default="", max_length=120)
    tags: list[str] = Field(default_factory=list, max_length=12)
    active: bool = True

    @field_validator("question")
    @classmethod
    def strip_question(cls, value: str) -> str:
        text = value.strip()
        if len(text) < 3:
            raise ValueError("Enter a longer question")
        return text

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for tag in value:
            if not isinstance(tag, str):
                continue
            cleaned = tag.strip()[:40]
            if cleaned and cleaned not in out:
                out.append(cleaned)
        return out[:12]


class VisibilityQueryOut(ApiModel):
    id: str
    question: str
    label: str
    active: bool
    tags: list[str]
    created_at: datetime


class VisibilityCompetitorsIn(ApiModel):
    competitor_domains: list[str] = Field(default_factory=list, max_length=MAX_COMPETITORS)


class VisibilityCompetitorsOut(ApiModel):
    competitor_domains: list[str]


class VisibilityObservationOut(ApiModel):
    id: str
    query_id: str | None
    question: str
    engine: str
    brand_cited: bool
    brand_position: int | None
    cited_domains: list[str]
    competitors_cited: list[str]
    answer_excerpt: str
    observed_at: datetime


class VisibilityEngineShare(ApiModel):
    engine: str
    probes: int
    brand_citations: int
    share: float


class VisibilityCompetitorShare(ApiModel):
    domain: str
    sightings: int


class VisibilitySummaryOut(ApiModel):
    probes: int
    brand_citations: int
    brand_share: float
    by_engine: list[VisibilityEngineShare]
    top_competitors: list[VisibilityCompetitorShare]
    read_only: bool = False


class VisibilityWorkspaceOut(ApiModel):
    queries: list[VisibilityQueryOut]
    competitors: list[str]
    recent: list[VisibilityObservationOut]
    summary: VisibilitySummaryOut
    read_only: bool = False
