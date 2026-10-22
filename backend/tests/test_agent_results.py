"""Every agent has a results view, and every view's queries are valid SQL."""
from __future__ import annotations

from sqlalchemy.dialects import postgresql

from app.agents.base.registry import all_agents
from app.services import agent_results


class _Result:
    def scalars(self):
        return iter(())

    def all(self):
        return []

    def scalar_one(self):
        return 0


class _CompilingSession:
    """Compiles each statement for PostgreSQL and returns empty results."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement):  # noqa: ANN001
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return _Result()


class _Cipher:
    def decrypt(self, value, *, context):  # noqa: ANN001
        return value


def test_every_agent_has_a_results_builder():
    slugs = {agent.slug for agent in all_agents()}
    assert set(agent_results.BUILDERS) <= slugs
    for slug in slugs:
        builder = agent_results.BUILDERS.get(slug)
        if builder is None:
            import importlib

            builder = getattr(importlib.import_module(f"app.agents.{slug}"), "RESULTS", None)
        assert builder is not None, slug


def test_every_builder_queries_only_its_tenant_and_renders_empty():
    for slug in [agent.slug for agent in all_agents()]:
        db = _CompilingSession()
        results = agent_results.results_for(db, slug=slug, tenant_id="t1", cipher=_Cipher())
        assert results is not None
        assert results.title and results.empty and results.columns
        assert results.rows == []
        assert db.statements, slug
        for sql in db.statements:
            assert "tenant_id" in sql, (slug, sql)


def test_status_columns_are_declared_for_tones():
    for slug in [agent.slug for agent in all_agents()]:
        results = agent_results.results_for(_CompilingSession(), slug=slug, tenant_id="t", cipher=_Cipher())
        keys = {column.key for column in results.columns}
        assert len(keys) == len(results.columns), slug
