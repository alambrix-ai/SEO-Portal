"""Audit-log export: formula neutralisation, bounds, pagination, access.

The route tests replace the database with a stand-in, so they run anywhere.
The tenant-isolation and keyset-walk tests need ``TEST_DATABASE_URL``.
"""
from __future__ import annotations

import base64
import csv
import io
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.core import csv_safe
from app.core.config import settings
from app.core.exceptions import InvalidInputError
from app.services import audit
from tests.conftest import requires_db

TENANT_ID = "00000000-0000-0000-0000-0000000a0d17"
ENTRY_ID = "11111111-1111-4111-8111-111111111111"
URL = "/api/v1/admin/audit/export"
ABUSE_CELLS = ('=HYPERLINK("https://evil.example","Open")', "@SUM(A1:A9)", "-2+3", "+cmd|' /C calc'!A0")


# ── csv_safe.neutralise (human / Excel exports) ────────────────────────────
@pytest.mark.parametrize("cell", [*ABUSE_CELLS, "\t=1+1", "\r=1+1"])
def test_formula_cells_are_neutralised(cell: str) -> None:
    assert csv_safe.neutralise(cell) == "'" + cell


@pytest.mark.parametrize("cell", ["revoked an invitation", "Q3 report", "", "a=b", "1+1", "user@example.test"])
def test_ordinary_cells_pass_through_unchanged(cell: str) -> None:
    assert csv_safe.neutralise(cell) == cell


def test_none_and_numbers_become_text() -> None:
    assert csv_safe.neutralise(None) == ""
    assert csv_safe.neutralise(42) == "42"
    # A negative number is still a formula trigger for a spreadsheet.
    assert csv_safe.neutralise(-5) == "'-5"


def test_rows_to_csv_neutralises_header_and_every_cell() -> None:
    data = csv_safe.rows_to_csv(["=evil", "ok"], [list(ABUSE_CELLS[:2]), ABUSE_CELLS[2:]])
    text = data.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["'=evil", "ok"]
    for row in rows[1:]:
        assert all(cell.startswith("'") for cell in row), row


# ── csv_safe.check_ads_editor_cell (tool-imported export) ──────────────────
@pytest.mark.parametrize("cell", [ABUSE_CELLS[0], ABUSE_CELLS[1], " =1+1", "\t@x", "+=1+1", "+ @SUM(A1)"])
def test_ads_editor_refuses_equals_and_at(cell: str) -> None:
    with pytest.raises(csv_safe.UnsafeCellError):
        csv_safe.check_ads_editor_cell(cell)


def test_ads_editor_strips_a_leading_plus() -> None:
    assert csv_safe.check_ads_editor_cell("+cmd") == "cmd"
    assert csv_safe.check_ads_editor_cell("++running shoes") == "running shoes"


@pytest.mark.parametrize("cell", ["-2+3", "-free", "running shoes", ""])
def test_ads_editor_leaves_negatives_and_text_alone(cell: str) -> None:
    # A leading "-" is Ads Editor's negative-keyword syntax: not escaped.
    assert csv_safe.check_ads_editor_cell(cell) == cell


# ── Bounds and cursors ─────────────────────────────────────────────────────
def test_a_400_day_range_is_refused() -> None:
    start = date(2025, 1, 1)
    with pytest.raises(InvalidInputError, match="days or fewer"):
        audit.export_window(start, start + timedelta(days=399))


def test_the_maximum_range_is_allowed_and_inclusive() -> None:
    start = date(2025, 1, 1)
    end = start + timedelta(days=settings.audit_export_max_days - 1)
    lo, hi = audit.export_window(start, end)
    assert lo == datetime(2025, 1, 1, tzinfo=UTC)
    assert hi == datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=UTC)


def test_a_backwards_range_is_refused() -> None:
    with pytest.raises(InvalidInputError):
        audit.export_window(date(2025, 2, 1), date(2025, 1, 1))


def test_cursor_round_trips() -> None:
    at = datetime(2025, 3, 4, 5, 6, 7, 891011, tzinfo=UTC)
    assert audit.decode_cursor(audit.encode_cursor(at, ENTRY_ID)) == (at, ENTRY_ID)


@pytest.mark.parametrize(
    "cursor",
    [
        "not base64!!",
        "x" * (audit.CURSOR_MAX_LENGTH + 1),
        audit.encode_cursor(datetime(2025, 1, 1, tzinfo=UTC), "'; DROP TABLE audit_log; --"),
        # A naive timestamp is refused rather than guessed at.
        base64.urlsafe_b64encode(f"2025-01-01T00:00:00|{ENTRY_ID}".encode()).decode(),
    ],
)
def test_forged_cursors_are_refused(cursor: str) -> None:
    with pytest.raises(InvalidInputError):
        audit.decode_cursor(cursor)


def test_filename_is_header_safe() -> None:
    assert audit.export_filename(date(2025, 1, 1), date(2025, 1, 31), "csv") == (
        "audit-log-2025-01-01-to-2025-01-31.csv"
    )
    assert audit.export_filename(date(2025, 1, 1), date(2025, 1, 1), 'x"\r\nSet-Cookie: a') == (
        "audit-log-2025-01-01-to-2025-01-01.txt"
    )


def test_csv_export_neutralises_cells_and_omits_ip_addresses() -> None:
    row = {
        "id": ENTRY_ID,
        "at": "2025-01-01T00:00:00+00:00",
        "actor": "@SUM(A1)",
        "actor_type": "user",
        "actor_id": "",
        "action": ABUSE_CELLS[0],
        "module": "admin",
        "context": "{}",
        "ip_address": "203.0.113.9",
    }
    text = audit.export_csv([row]).decode("utf-8-sig")
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert parsed[0]["action"] == "'" + ABUSE_CELLS[0]
    assert parsed[0]["actor"] == "'@SUM(A1)"
    assert "ip_address" not in parsed[0]
    assert "203.0.113.9" not in text
    assert "ip_address" not in audit.EXPORT_COLUMNS


class _CapturingDb:
    """Records the statement export_page builds; returns no rows."""

    def __init__(self, entries: list[Any] | None = None) -> None:
        self.statements: list[Any] = []
        self._entries = entries or []

    def execute(self, stmt: Any) -> Any:  # noqa: ANN401
        self.statements.append(stmt)
        return SimpleNamespace(scalars=lambda: iter(self._entries))


def test_export_query_is_tenant_filtered_bounded_and_keyset_ordered() -> None:
    db = _CapturingDb()
    cursor = audit.encode_cursor(datetime(2025, 1, 2, tzinfo=UTC), ENTRY_ID)
    audit.export_page(db, tenant_id=TENANT_ID, start=date(2025, 1, 1), end=date(2025, 1, 31), cursor=cursor)  # type: ignore[arg-type]
    compiled = db.statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "audit_log.tenant_id = " in sql
    assert TENANT_ID in compiled.params.values()
    assert "ORDER BY audit_log.at, audit_log.id" in sql
    assert compiled.params["param_1"] == settings.audit_export_max_rows + 1


def _entry(i: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"00000000-0000-4000-8000-{i:012d}",
        at=datetime(2025, 1, 1, tzinfo=UTC) + timedelta(minutes=i),
        actor="Test Admin",
        actor_type="user",
        actor_id=None,
        action=f"did {i}",
        module="admin",
        context={"n": i},
        ip_address="203.0.113.9",
    )


def test_next_cursor_only_when_more_rows_remain() -> None:
    entries = [_entry(i) for i in range(3)]
    page = audit.export_page(
        _CapturingDb(entries), tenant_id=TENANT_ID, start=date(2025, 1, 1), end=date(2025, 1, 1), limit=2  # type: ignore[arg-type]
    )
    assert [r["action"] for r in page.rows] == ["did 0", "did 1"]
    assert page.next_cursor == audit.encode_cursor(entries[1].at, entries[1].id)
    last = audit.export_page(
        _CapturingDb(entries[:2]), tenant_id=TENANT_ID, start=date(2025, 1, 1), end=date(2025, 1, 1), limit=2  # type: ignore[arg-type]
    )
    assert last.next_cursor is None


def test_page_size_can_never_exceed_the_setting() -> None:
    db = _CapturingDb()
    audit.export_page(db, tenant_id=TENANT_ID, start=date(2025, 1, 1), end=date(2025, 1, 1), limit=10**9)  # type: ignore[arg-type]
    compiled = db.statements[0].compile(dialect=postgresql.dialect())
    assert compiled.params["param_1"] == settings.audit_export_max_rows + 1


# ── Route ──────────────────────────────────────────────────────────────────
class _FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


def _current_user(role: str = "admin") -> object:
    from app.api.deps import CurrentUser

    return CurrentUser(
        user=SimpleNamespace(id="22222222-2222-4222-8222-222222222222", name="Test Admin", role=role, tenant_id=TENANT_ID),
        organization=SimpleNamespace(id=TENANT_ID),
        claims={},
    )


def _app(current: object, session: _FakeSession) -> FastAPI:
    from app.api.deps import get_current_user
    from app.db.session import get_db
    from app.main import create_app
    from tests.conftest import _noop_lifespan

    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    app.dependency_overrides[get_current_user] = lambda: current
    app.dependency_overrides[get_db] = lambda: session
    return app


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(audit, "record_user_action", lambda _db, **kw: calls.append(kw))
    return calls


@pytest.fixture
def page(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"next": None, "calls": []}

    def fake_export_page(_db: Any, **kw: Any) -> audit.ExportPage:  # noqa: ANN401
        audit.export_window(kw["start"], kw["end"])
        if kw.get("cursor"):
            audit.decode_cursor(kw["cursor"])
        state["calls"].append(kw)
        row = audit._export_row(_entry(1))  # noqa: SLF001
        row["action"] = ABUSE_CELLS[0]
        return audit.ExportPage(rows=[row], next_cursor=state["next"])

    monkeypatch.setattr(audit, "export_page", fake_export_page)
    return state


@pytest.fixture
def session() -> _FakeSession:
    return _FakeSession()


@pytest.fixture
def client(session: _FakeSession, page: dict[str, Any], recorded: list) -> Iterator[TestClient]:  # noqa: ARG001
    with TestClient(_app(_current_user(), session)) as test_client:
        yield test_client


PARAMS = {"from": "2025-01-01", "to": "2025-01-31", "format": "csv"}


@pytest.mark.parametrize("role", ["manager", "seo", "ads", "approver", "client"])
def test_only_admin_writers_can_export(role: str, page: dict, recorded: list, session: _FakeSession) -> None:
    with TestClient(_app(_current_user(role), session)) as test_client:
        response = test_client.get(URL, params=PARAMS)
    assert response.status_code == 403
    assert page["calls"] == [] and recorded == [] and session.commits == 0


def test_csv_export_is_neutralised_named_and_audited(
    client: TestClient, page: dict, recorded: list, session: _FakeSession
) -> None:
    page["next"] = audit.encode_cursor(datetime(2025, 1, 1, tzinfo=UTC), ENTRY_ID)
    response = client.get(URL, params=PARAMS, headers={"X-Forwarded-For": "203.0.113.9"})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == (
        'attachment; filename="audit-log-2025-01-01-to-2025-01-31.csv"'
    )
    assert response.headers["x-next-cursor"] == page["next"]
    assert response.headers["cache-control"] == "no-store"
    parsed = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert parsed[0]["action"] == "'" + ABUSE_CELLS[0]
    assert "203.0.113.9" not in response.text
    # The export is itself audited, with ids and counts only.
    assert len(recorded) == 1 and session.commits == 1
    assert recorded[0]["action"] == "exported the audit log"
    assert recorded[0]["context"]["rows"] == 1 and recorded[0]["context"]["more"] is True
    assert page["calls"][0]["tenant_id"] == TENANT_ID


def test_json_export_and_last_page_has_no_cursor(client: TestClient, page: dict) -> None:
    response = client.get(URL, params={**PARAMS, "format": "json"})
    assert response.status_code == 200
    assert response.headers["content-disposition"].endswith('.json"')
    assert "x-next-cursor" not in response.headers
    body = json.loads(response.content)
    assert body["columns"] == list(audit.EXPORT_COLUMNS)
    assert "ip_address" not in body["rows"][0]


def test_a_400_day_range_gets_422(client: TestClient, recorded: list) -> None:
    response = client.get(URL, params={**PARAMS, "from": "2024-01-01", "to": "2025-02-03"})
    assert response.status_code == 422
    assert recorded == []


@pytest.mark.parametrize(
    "params",
    [
        {**PARAMS, "format": "xlsx"},
        {**PARAMS, "from": "yesterday"},
        {"to": "2025-01-31", "format": "csv"},
        {**PARAMS, "cursor": "x" * (audit.CURSOR_MAX_LENGTH + 1)},
        {**PARAMS, "cursor": "not-a-cursor!"},
    ],
)
def test_bad_parameters_get_422(client: TestClient, recorded: list, params: dict) -> None:
    assert client.get(URL, params=params).status_code == 422
    assert recorded == []


# ── Database ───────────────────────────────────────────────────────────────
@requires_db
def test_export_never_includes_another_workspaces_rows(clean_db, org, second_org) -> None:  # noqa: ANN001
    from app.db.session import bind_tenant

    today = datetime.now(UTC).date()
    bind_tenant(clean_db, second_org.id)
    audit.record(clean_db, tenant_id=second_org.id, actor="Second Admin", action="second secret")
    clean_db.commit()
    bind_tenant(clean_db, org.id)
    audit.record(clean_db, tenant_id=org.id, actor="Test Admin", action="first visible")
    clean_db.commit()

    page = audit.export_page(clean_db, tenant_id=org.id, start=today - timedelta(days=1), end=today)
    actions = [r["action"] for r in page.rows]
    assert "first visible" in actions
    assert "second secret" not in actions


@requires_db
def test_keyset_pages_walk_every_row_once(clean_db, org) -> None:  # noqa: ANN001
    from app.db.session import bind_tenant

    bind_tenant(clean_db, org.id)
    at = datetime.now(UTC).replace(microsecond=0)
    for i in range(5):
        # Two rows share each timestamp, so the id tiebreak is exercised.
        audit.record(clean_db, tenant_id=org.id, actor="Test Admin", action=f"walk {i}", at=at + timedelta(seconds=i // 2))
    clean_db.commit()

    seen: list[str] = []
    cursor = None
    for _ in range(10):
        page = audit.export_page(clean_db, tenant_id=org.id, start=at.date(), end=at.date() + timedelta(days=1), cursor=cursor, limit=2)
        seen.extend(r["action"] for r in page.rows if r["action"].startswith("walk "))
        cursor = page.next_cursor
        if cursor is None:
            break
    assert sorted(seen) == [f"walk {i}" for i in range(5)]
    assert len(seen) == len(set(seen))
