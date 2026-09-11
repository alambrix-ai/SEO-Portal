"""Multi-workspace: one email across isolated organisations."""
from __future__ import annotations

import pytest

from tests.conftest import requires_db
from tests.test_api_flows import auth, configure, register

pytestmark = requires_db


def test_create_second_workspace_lists_two_cards(client):  # noqa: ANN001
    body = register(client, email="multi@northgate.example")
    token = body["tokens"]["access_token"]
    first_org = body["session"]["organization"]["id"]

    session = client.get("/api/v1/auth/me", headers=auth(token)).json()
    assert len(session["workspaces"]) == 1
    assert session["workspaces"][0]["is_current"] is True

    created = client.post(
        "/api/v1/workspaces",
        json={"name": "Second Desk", "primary_domain": "second.example"},
        headers=auth(token),
    )
    assert created.status_code == 200, created.text
    payload = created.json()
    second_org = payload["session"]["organization"]["id"]
    assert second_org != first_org
    assert payload["session"]["organization"]["name"] == "Second Desk"
    assert payload["session"]["onboarding_complete"] is False

    cards = payload["session"]["workspaces"]
    assert len(cards) == 2
    assert sum(1 for c in cards if c["is_current"]) == 1
    assert {c["id"] for c in cards} == {first_org, second_org}


def test_switch_workspace_isolates_agent_data(client):  # noqa: ANN001
    body = register(client, email="isolate@northgate.example")
    token_a = body["tokens"]["access_token"]
    org_a = body["session"]["organization"]["id"]

    configure(client, token_a, "on_page_seo_sync")
    client.post("/api/v1/agents/on_page_seo_sync/resume", headers=auth(token_a))

    agents_a = client.get("/api/v1/agents", headers=auth(token_a)).json()
    running_a = sum(1 for a in agents_a if a["status"] == "running")
    assert running_a >= 1

    created = client.post(
        "/api/v1/workspaces",
        json={"name": "Isolated Desk"},
        headers=auth(token_a),
    )
    assert created.status_code == 200, created.text
    token_b = created.json()["tokens"]["access_token"]
    org_b = created.json()["session"]["organization"]["id"]
    assert org_b != org_a

    agents_b = client.get("/api/v1/agents", headers=auth(token_b)).json()
    assert all(a["status"] != "running" for a in agents_b)
    assert all(not a["configured"] for a in agents_b)

    switched = client.post(
        f"/api/v1/workspaces/{org_a}/switch",
        headers=auth(token_b),
    )
    assert switched.status_code == 200, switched.text
    token_back = switched.json()["tokens"]["access_token"]
    assert switched.json()["session"]["organization"]["id"] == org_a

    agents_again = client.get("/api/v1/agents", headers=auth(token_back)).json()
    assert sum(1 for a in agents_again if a["status"] == "running") >= 1


def test_cannot_switch_to_foreign_workspace(client):  # noqa: ANN001
    a = register(client, email="owner-a@northgate.example", org="Alpha Co")
    b = register(client, email="owner-b@northgate.example", org="Beta Co")
    token_a = a["tokens"]["access_token"]
    org_b = b["session"]["organization"]["id"]

    refused = client.post(
        f"/api/v1/workspaces/{org_b}/switch",
        headers=auth(token_a),
    )
    assert refused.status_code == 403, refused.text


def test_onboarding_complete_is_per_workspace(client):  # noqa: ANN001
    body = register(client, email="perws@northgate.example")
    token = body["tokens"]["access_token"]
    org_a = body["session"]["organization"]["id"]

    client.put(
        "/api/v1/onboarding",
        json={"step": 3, "domain": "perws.example", "cms": "WordPress", "guardrail": "hybrid"},
        headers=auth(token),
    )
    done = client.post("/api/v1/onboarding/complete", headers=auth(token))
    assert done.status_code == 200, done.text

    me = client.get("/api/v1/auth/me", headers=auth(token)).json()
    assert me["onboarding_complete"] is True

    created = client.post(
        "/api/v1/workspaces",
        json={"name": "Fresh Desk"},
        headers=auth(token),
    )
    assert created.status_code == 200, created.text
    session_b = created.json()["session"]
    assert session_b["organization"]["id"] != org_a
    assert session_b["onboarding_complete"] is False
    cards = {c["id"]: c for c in session_b["workspaces"]}
    assert cards[org_a]["onboarding_complete"] is True
    assert cards[session_b["organization"]["id"]]["onboarding_complete"] is False


def test_delete_workspace_soft_hides_and_keeps_one(client):  # noqa: ANN001
    body = register(client, email="delete-ws@northgate.example")
    token = body["tokens"]["access_token"]
    org_a = body["session"]["organization"]["id"]

    # Cannot delete the only workspace.
    refused = client.delete(f"/api/v1/workspaces/{org_a}", headers=auth(token))
    assert refused.status_code == 409, refused.text

    created = client.post(
        "/api/v1/workspaces",
        json={"name": "Spare Desk", "primary_domain": "spare.example"},
        headers=auth(token),
    )
    assert created.status_code == 200, created.text
    token_b = created.json()["tokens"]["access_token"]
    org_b = created.json()["session"]["organization"]["id"]
    assert len(created.json()["session"]["workspaces"]) == 2

    # Soft-delete the current workspace → land on the other.
    deleted = client.delete(f"/api/v1/workspaces/{org_b}", headers=auth(token_b))
    assert deleted.status_code == 200, deleted.text
    session = deleted.json()["session"]
    assert session["organization"]["id"] == org_a
    assert all(c["id"] != org_b for c in session["workspaces"])
    assert len(session["workspaces"]) == 1

    # Deleted workspace cannot be switched back onto.
    blocked = client.post(
        f"/api/v1/workspaces/{org_b}/switch",
        headers=auth(deleted.json()["tokens"]["access_token"]),
    )
    assert blocked.status_code in {403, 404}, blocked.text
