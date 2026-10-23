"""Social mention sentiment and reply routing."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.approval import ApprovalItem
from app.models.social_mentions import SocialMention
from app.services import social_listening as listening
from tests.test_seo_pipeline_fixes import TENANT, make_ctx


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "JSON"


@compiles(PG_UUID, "sqlite")
def _uuid_on_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN003, ANN202
    return "CHAR(36)"


def test_negative_sentiment() -> None:
    result = listening.score_sentiment("This service is terrible and late, awful experience")
    assert result.label == "negative"


def test_parse_scope() -> None:
    items = listening.parse_scope_mentions(
        "twitter|1|Sam|This is terrible and broken\nlinkedin|2|Alex|Great job amazing"
    )
    assert len(items) == 2
    assert items[0].network == "twitter"


def test_untrusted_fence() -> None:
    block = listening.as_untrusted("Competitor said awful things")
    assert "social_mention" in block


def test_agent_registered_and_rls() -> None:
    from app.agents.base.registry import get_agent
    from app.db.rls import TENANT_SCOPED_TABLES

    assert get_agent("social_listening") is not None
    assert "social_mentions" in TENANT_SCOPED_TABLES


@pytest.fixture
def mention_db():  # noqa: ANN201
    engine = create_engine("sqlite://")
    tables = [SocialMention.__table__, ApprovalItem.__table__]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_agent_routes_negative_as_high(mention_db: Session) -> None:
    from app.agents.social_listening.agent import AGENT

    scope = (
        "twitter|tw1|Sam|This service is terrible and late, awful experience\n"
        "linkedin|li1|Alex|Great job, amazing and wonderful team"
    )
    ctx = make_ctx(
        mention_db, slug="social_listening", domain="example.com", scope=scope
    )
    result = AGENT.run(ctx)
    mention_db.flush()
    assert result.skipped is False
    assert len(result.actions) == 1
    assert result.actions[0].impact.value == "high"
    rows = list(mention_db.execute(select(SocialMention)).scalars())
    assert len(rows) == 2
    negative = next(r for r in rows if r.external_id == "tw1")
    assert negative.status == "routed"
    assert negative.reply_encrypted
