"""Competitor ad library parsing, egress guards, and agent ingest."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.competitor_ads import CompetitorAd
from app.services import competitor_ads as ads
from tests.test_seo_pipeline_fixes import TENANT, make_ctx


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "CHAR(36)"


def test_library_url_is_https_public() -> None:
    url = ads.library_url("meta", "Acme Widgets")
    assert url.startswith("https://")
    assert ads.check_library_url(url) == url


def test_private_url_rejected() -> None:
    with pytest.raises(Exception):
        ads.check_library_url("http://127.0.0.1/ads")


def test_parse_json_payload() -> None:
    raw = json.dumps(
        {
            "ads": [
                {
                    "id": "ad_1",
                    "headline": "Buy now",
                    "body": "Limited offer on industrial widgets.",
                    "cta": "Shop",
                }
            ]
        }
    )
    snaps = ads.parse_library_payload("google", "Acme", raw)
    assert len(snaps) == 1
    assert snaps[0].external_id == "ad_1"
    block = ads.as_untrusted(snaps[0])
    assert "competitor_ad" in block


def test_agent_registered_and_rls() -> None:
    from app.agents.base.registry import get_agent
    from app.db.rls import TENANT_SCOPED_TABLES

    assert get_agent("competitor_ad_watch") is not None
    assert "competitor_ads" in TENANT_SCOPED_TABLES


@pytest.fixture
def ads_db():  # noqa: ANN201
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[CompetitorAd.__table__])
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_agent_ingests_new_ads(ads_db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agents import competitor_ad_watch as pkg
    from app.agents.competitor_ad_watch.agent import AGENT

    payload = json.dumps(
        {
            "ads": [
                {
                    "id": "meta_99",
                    "headline": "Faster shipping",
                    "body": "Competitor claims two-day delivery worldwide.",
                    "cta": "Learn more",
                }
            ]
        }
    ).encode()

    def fake_fetch(url: str) -> bytes:
        assert url.startswith("https://")
        return payload

    monkeypatch.setattr(pkg.agent, "_FETCH_OVERRIDE", fake_fetch)
    ctx = make_ctx(
        ads_db, slug="competitor_ad_watch", domain="example.com", scope="Rival Co"
    )
    result = AGENT.run(ctx)
    ads_db.flush()
    assert result.skipped is False
    rows = list(ads_db.execute(select(CompetitorAd)).scalars())
    # meta + google both get the same fixture → 2 rows same external id different platform
    assert len(rows) == 2
    assert all(r.copy_encrypted for r in rows)
