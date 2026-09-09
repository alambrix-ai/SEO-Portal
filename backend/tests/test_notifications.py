"""The bell, and what makes it light up.

Nothing guarded this, and the failure was structural rather than a slip: the
audit log of a live workspace held 44 `user` rows, 4 `system` rows and no
`agent` rows at all. ``unseen_count`` correctly ignores the viewer's own
actions, so for one person working alone the count was always nought and the
indicator could never appear. The bell was not broken — it had nothing to
show, because queueing a proposal for approval wrote no audit entry, and that
is the *default* outcome for every agent on this platform.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.core.rbac import access_map
from app.db.base import utcnow
from app.models.workspace import AuditLogEntry, Organization, User
from app.services import audit
from tests.conftest import requires_db


def _allowed(user: User) -> set[str]:
    return {module for module, level in access_map(user.role).items() if level != "none"}


def _register(client, db):  # noqa: ANN001
    """A workspace through the real registration flow, then pinned.

    The pin matters: row-level security is FORCEd, so a session that has not
    claimed an organisation can neither read nor write its rows. Registration
    happens on the app's own session, so this one has to claim the tenant
    before it can see what was created.
    """
    from app.db.session import bind_tenant
    from tests.test_api_flows import register

    body = register(client)
    org_id = body["session"]["organization"]["id"]
    bind_tenant(db, org_id)
    org = db.get(Organization, org_id)
    user = db.execute(select(User).where(User.tenant_id == org.id)).scalars().first()
    token = body["tokens"]["access_token"]
    return org, user, {"Authorization": f"Bearer {token}"}


def _count(db, org, user) -> int:  # noqa: ANN001
    return audit.unseen_count(
        db,
        tenant_id=org.id,
        since=user.notifications_seen_at,
        viewer_id=user.id,
        modules=_allowed(user),
    )


@requires_db
def test_an_agents_proposal_is_something_the_user_is_told_about(client, clean_db):  # noqa: ANN001
    """The gap that made the whole feature inert.

    Every agent ships paused and under human review, so "queued for approval"
    is what agents normally do. It was recorded nowhere, which meant the panel
    had nothing an agent had done to show and the dot never appeared.
    """
    db = clean_db
    org, user, _headers = _register(client, db)
    audit.mark_seen(db, user=user, tenant_id=org.id)
    db.flush()
    assert _count(db, org, user) == 0

    audit.record_agent_action(
        db,
        tenant_id=org.id,
        agent_name="On-Page SEO Sync",
        action="proposed Rewrite: \"Pricing\" page copy for approval",
        module="seo",
        context={"awaiting_approval": True},
    )
    db.flush()

    assert _count(db, org, user) == 1


@requires_db
def test_the_indicator_survives_a_reload(client, clean_db):  # noqa: ANN001
    """The marker is a column on the user, not state in the tab.

    A dot held in React would come back on every reload, or vanish on one —
    both wrong. It has to be the same answer on a laptop and a phone.
    """
    db = clean_db
    org, user, headers = _register(client, db)

    audit.record_agent_action(
        db, tenant_id=org.id, agent_name="Technical SEO Auditor",
        action="opened 3 issues", module="seo",
    )
    db.commit()

    first = client.get("/api/v1/notifications", headers=headers).json()
    assert first["unseen"] >= 1
    # A second read, as a reload would do, still reports it unseen: reading
    # the panel is not the same as saying it was seen.
    again = client.get("/api/v1/notifications", headers=headers).json()
    assert again["unseen"] == first["unseen"]

    assert client.post("/api/v1/notifications/seen", headers=headers).status_code == 200
    after = client.get("/api/v1/notifications", headers=headers).json()
    assert after["unseen"] == 0
    # And it stays cleared across a reload.
    assert client.get("/api/v1/notifications", headers=headers).json()["unseen"] == 0


@requires_db
def test_your_own_clicks_do_not_light_up_your_own_bell(client, clean_db):  # noqa: ANN001
    """A dot that appears because you just pressed something trains people to
    ignore the dot."""
    db = clean_db
    org, user, _headers = _register(client, db)
    audit.mark_seen(db, user=user, tenant_id=org.id)
    db.flush()

    audit.record_user_action(
        db, user=user, action="paused On-Page SEO Sync", module="agents"
    )
    db.flush()
    assert _count(db, org, user) == 0


@requires_db
def test_an_entry_written_while_the_panel_was_being_read_is_not_marked_seen(
    client, clean_db  # noqa: ANN001
):
    """The panel is one request and the mark is a second.

    With a wall-clock marker, anything an agent wrote in the gap was stamped
    as seen without ever being displayed. Agents here can tick every minute,
    so the gap is not theoretical. The marker is set to the newest entry that
    existed, not to the time of the click.
    """
    db = clean_db
    org, user, _headers = _register(client, db)

    audit.record_agent_action(
        db, tenant_id=org.id, agent_name="Agent A", action="did the first thing",
        module="seo",
    )
    db.flush()

    # The user opens the panel: this is what the mark-seen request does.
    audit.mark_seen(db, user=user, tenant_id=org.id)
    db.flush()
    assert _count(db, org, user) == 0

    # An entry that landed a moment after the mark. Under the old wall-clock
    # marker this fell inside the window the click had already covered and
    # was never counted; against the newest-entry marker it is still unseen.
    late = AuditLogEntry(
        tenant_id=org.id,
        actor="Agent B",
        actor_type="agent",
        action="did the second thing",
        module="seo",
        at=utcnow() + timedelta(milliseconds=1),
    )
    db.add(late)
    db.flush()

    assert _count(db, org, user) == 1


@requires_db
def test_a_module_the_viewer_cannot_open_does_not_leave_a_dot_they_cannot_clear(
    client, clean_db  # noqa: ANN001
):
    """The panel is filtered by the access map, so the count must be too —
    otherwise the badge never clears and there is nothing behind it."""
    db = clean_db
    org, user, _headers = _register(client, db)
    audit.mark_seen(db, user=user, tenant_id=org.id)
    db.flush()

    audit.record_agent_action(
        db, tenant_id=org.id, agent_name="Predictive Budget Engine",
        action="moved 4% of spend to search", module="ads",
    )
    db.flush()

    visible = audit.unseen_count(
        db, tenant_id=org.id, since=user.notifications_seen_at,
        viewer_id=user.id, modules={"seo"},
    )
    assert visible == 0
