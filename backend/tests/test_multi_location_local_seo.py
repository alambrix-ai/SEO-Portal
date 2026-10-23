"""Multi-location entities and per-location rank checks."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.connectors.base.connector import Capability
from app.db.base import Base, new_id
from app.models.local import LocalRankCheck
from app.models.locations import Location
from app.services import locations as loc_svc
from tests.test_seo_pipeline_fixes import TENANT, make_ctx


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "CHAR(36)"


def test_validate_location() -> None:
    loc = loc_svc.validate_location(name="Downtown Salon", city="Austin", region="TX")
    assert loc.slug == "downtown-salon"
    assert loc.city == "Austin"


def test_lat_bounds() -> None:
    with pytest.raises(ValueError):
        loc_svc.validate_location(name="X", lat=100)


def test_agent_registered_and_rls() -> None:
    from app.agents.base.registry import get_agent
    from app.db.rls import TENANT_SCOPED_TABLES

    assert get_agent("multi_location_local_seo") is not None
    assert "locations" in TENANT_SCOPED_TABLES


@pytest.fixture
def local_db():  # noqa: ANN201
    engine = create_engine("sqlite://")
    tables = [Location.__table__, LocalRankCheck.__table__]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        yield session
    engine.dispose()


class FakePlaces:
    slug = "google_places"
    name = "Google Places"
    radius_m = 5000

    def supports(self, capability: Capability) -> bool:
        return capability is Capability.READ_LOCAL_RANKINGS

    def search_around(self, phrase, business):  # noqa: ANN001, ANN201
        results = [
            SimpleNamespace(place_id=business.place_id, name=business.name, rating=4.5, review_count=10),
            SimpleNamespace(place_id="other", name="Rival", rating=4.0, review_count=5),
        ]

        def position_of(place_id: str):
            for i, r in enumerate(results, start=1):
                if r.place_id == place_id:
                    return i
            return None

        return SimpleNamespace(results=results, position_of=position_of)


def test_agent_writes_checks_with_location_id(local_db: Session) -> None:
    from app.agents.multi_location_local_seo.agent import AGENT

    location = Location(
        id=new_id(),
        tenant_id=TENANT,
        slug="downtown",
        name="Downtown Salon",
        brand="Acme",
        region="south",
        city="Austin",
        place_id="place_downtown",
        gbp_location_id="",
        status="active",
        notes_encrypted="",
    )
    local_db.add(location)
    local_db.flush()

    ctx = make_ctx(
        local_db,
        slug="multi_location_local_seo",
        connectors={"google_places": FakePlaces()},
        domain="example.com",
        scope="south",
    )
    result = AGENT.run(ctx)
    local_db.flush()
    assert result.skipped is False
    checks = list(local_db.execute(select(LocalRankCheck)).scalars())
    assert checks
    assert all(c.location_id == location.id for c in checks)
    assert any(c.position == 1 for c in checks)
