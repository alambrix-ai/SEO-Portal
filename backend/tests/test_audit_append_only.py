"""The audit log is append-only in the database itself.

The migration cannot be run here (there is no disposable database in this
environment), so it is executed against a recording stand-in for
``alembic.op`` and checked against ``app.db.audit_guard``. The database tests
install the guard inside their own transaction and roll it back, so the shared
test schema is left as it was.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.db import audit_guard
from tests.conftest import requires_db

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _load_migration(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"m_{name}", VERSIONS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOp:
    """Stands in for ``alembic.op``: records calls, touches no database."""

    def __init__(self) -> None:
        self.executed: list[str] = []

    def execute(self, statement: str) -> None:
        self.executed.append(statement)


def _run(direction: str, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    module = _load_migration("0020_audit_append_only")
    recorder = _RecordingOp()
    monkeypatch.setattr(module, "op", recorder)
    getattr(module, direction)()
    return recorder.executed


# ── Migration ──────────────────────────────────────────────────────────────
def test_migration_chain_and_ids() -> None:
    module = _load_migration("0020_audit_append_only")
    previous = _load_migration("0019_creative_studio")
    assert module.revision == "0020_audit_append_only"
    assert module.down_revision == previous.revision == "0019_creative_studio"
    assert len(module.revision) <= 32
    assert not [p.name for p in VERSIONS.glob("0020_*.py") if p.name != "0020_audit_append_only.py"]


def test_migration_installs_exactly_what_the_guard_declares(monkeypatch: pytest.MonkeyPatch) -> None:
    # The migration holds a frozen copy; at creation it must match the guard
    # the tests install, or the tests would prove a different trigger.
    assert _run("upgrade", monkeypatch) == audit_guard.install_statements()
    assert _run("downgrade", monkeypatch) == audit_guard.uninstall_statements()


def test_trigger_blocks_update_and_delete_but_not_insert(monkeypatch: pytest.MonkeyPatch) -> None:
    executed = _run("upgrade", monkeypatch)
    trigger = next(s for s in executed if s.startswith("CREATE TRIGGER"))
    assert "BEFORE UPDATE OR DELETE ON audit_log" in trigger
    assert "FOR EACH ROW" in trigger
    assert "INSERT" not in trigger
    function = next(s for s in executed if "CREATE OR REPLACE FUNCTION" in s)
    assert "RAISE EXCEPTION" in function
    # No escape hatch keyed on a session setting the app role could set.
    assert "current_setting" not in function


def test_revoke_is_guarded_on_the_app_role_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    executed = _run("upgrade", monkeypatch)
    revoke = next(s for s in executed if "REVOKE" in s)
    assert "REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM automarket_app" in revoke
    assert "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'automarket_app')" in revoke


def test_downgrade_removes_the_trigger_and_restores_the_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    executed = _run("downgrade", monkeypatch)
    assert executed[0] == "DROP TRIGGER IF EXISTS trg_audit_log_append_only ON audit_log"
    assert executed[1] == "DROP FUNCTION IF EXISTS audit_log_append_only()"
    assert "GRANT UPDATE, DELETE ON audit_log TO automarket_app" in executed[2]
    assert "pg_roles" in executed[2]


# ── Database behaviour ─────────────────────────────────────────────────────
def _guarded_entry(db: Any, org: Any) -> Any:  # noqa: ANN401
    from app.db.session import bind_tenant
    from app.services import audit

    bind_tenant(db, org.id)
    audit_guard.install(db.connection())
    return audit.record(db, tenant_id=org.id, actor="Test Admin", action="did a thing")


@requires_db
def test_update_on_the_audit_log_raises(clean_db, org) -> None:  # noqa: ANN001
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    entry = _guarded_entry(clean_db, org)
    try:
        with pytest.raises(DBAPIError, match="append-only"):
            with clean_db.begin_nested():
                clean_db.execute(
                    text("UPDATE audit_log SET action = 'rewritten' WHERE id = :id"),
                    {"id": entry.id},
                )
        # Through the ORM as well, which is how an application bug would do it.
        with pytest.raises(DBAPIError, match="append-only"):
            with clean_db.begin_nested():
                entry.action = "rewritten"
                clean_db.flush()
    finally:
        clean_db.rollback()  # also drops the trigger installed above


@requires_db
def test_delete_on_the_audit_log_raises(clean_db, org) -> None:  # noqa: ANN001
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    entry = _guarded_entry(clean_db, org)
    try:
        with pytest.raises(DBAPIError, match="append-only"):
            with clean_db.begin_nested():
                clean_db.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": entry.id})
    finally:
        clean_db.rollback()


@requires_db
def test_appending_still_works_under_the_guard(clean_db, org) -> None:  # noqa: ANN001
    from sqlalchemy import func, select

    from app.models.workspace import AuditLogEntry
    from app.services import audit

    try:
        _guarded_entry(clean_db, org)
        audit.record(clean_db, tenant_id=org.id, actor="Test Admin", action="did another")
        count = clean_db.execute(
            select(func.count()).select_from(AuditLogEntry).where(
                AuditLogEntry.tenant_id == org.id, AuditLogEntry.action.like("did %")
            )
        ).scalar_one()
        assert count == 2
    finally:
        clean_db.rollback()
