"""End to end through the SEO content pipeline, against a connector.

There was no test here, and the gap was expensive. On-Page SEO Sync had never
proposed a rewrite on real data — not once — because ``list_pages`` omits page
bodies by design and nothing had ever called ``read_page``. Every synced page
therefore had an empty body, ``_analyse_pages`` skipped all of them as
"nothing to analyse yet", and the run reported "analysed 0" as a success.

The reason it looked fine was the test double: ``fake_data.pages()`` returned
full bodies, word counts and schema types from the *listing*, which no real
connector does. A double more capable than the thing it stands in for tests a
system nobody has.

So these tests assert the shape of the real contract:

* a listing carries no body;
* the sync reads bodies for the pages it means to work on;
* only then can a page be analysed and a rewrite proposed.

The last one is the whole point of the agent, and nothing had been guarding it.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.agents.base import pages as page_source
from app.connectors.base.interfaces import RemotePage
from app.models.agent import AgentRecord
from app.models.seo import SeoPage
from app.db.base import new_id
from app.db.session import bind_tenant
from app.models.workspace import Organization
from app.orchestration.runner import run_agent
from app.services import connectors as connector_service
from app.services import provisioning
from app.services.encryption import OrgCipher
from tests.conftest import requires_db
from tests.support import fake_data


# ── The contract a listing actually has ────────────────────────────────────
def test_a_listing_carries_no_page_body():
    """If this ever starts returning bodies again, the read step stops being
    exercised and the bug it was hiding comes back invisible."""
    listing = fake_data.pages(org_id="org-1")
    assert listing, "the fixture should list some pages"
    for page in listing:
        assert page.body == "", f"{page.url} came back with a body from the listing"
        assert page.word_count == 0


def test_reading_a_page_is_what_supplies_the_body():
    listing = fake_data.pages(org_id="org-1")
    full = fake_data.page(org_id="org-1", remote_id=listing[0].remote_id)
    assert full.body
    assert full.word_count > 0


# ── Usability, which decides whether a body is worth analysing ─────────────
@pytest.mark.parametrize(
    ("metadata", "usable"),
    [
        # The Site Crawler: rendered HTML, always real content.
        ({"source": "rendered", "writable": False}, True),
        # Markdown in a repository, or a CMS node.
        ({"writable": True}, True),
        # src/app/page.jsx — a composition with no copy in it. Analysing this
        # gets a confident answer about an import block.
        ({"writable": False}, False),
        # A connector that says nothing is assumed to serve content.
        ({}, True),
    ],
)
def test_component_source_is_not_treated_as_page_content(metadata: dict, usable: bool):
    page = RemotePage(remote_id="x", url="/x", title="X", metadata=metadata)
    assert page_source.is_usable(page) is usable


# ── The pipeline, with a database ──────────────────────────────────────────
def _workspace(db):  # noqa: ANN001
    """A provisioned organisation, the way registration builds one."""
    org_id = new_id()
    org = Organization(
        id=org_id,
        name="Northgate Motors",
        slug="northgate",
        # Every page body is encrypted under this, so the pipeline cannot run
        # without it.
        wrapped_dek=OrgCipher.provision(org_id),
        dek_version=1,
        global_autonomy=True,
    )
    db.add(org)
    db.flush()
    # Row-level security refuses a write no session has claimed.
    bind_tenant(db, org.id)
    provisioning.provision_organization(db, org)
    db.flush()
    return org


def _connect(db, org, slug="fake_cms", **values):  # noqa: ANN001
    """Mark a connector connected, with credentials it will accept.

    The hints have to be real: build_connector_bundle leaves out an
    installation whose required fields are empty, and rightly so — a
    connector that looks available and raises "Missing credential" on every
    call reads like a fault in the agents.
    """
    record = connector_service.get_record(db, tenant_id=org.id, slug=slug)
    record.connected = True
    record.health = "healthy"
    record.credential_hints = {"siteUrl": "https://example.test", **values}
    db.flush()
    return record


def _agent(db, org, slug="on_page_seo_sync", scope=""):  # noqa: ANN001
    record = db.execute(
        select(AgentRecord).where(
            AgentRecord.tenant_id == org.id, AgentRecord.slug == slug
        )
    ).scalar_one()
    record.configured = True
    record.scope = scope
    db.flush()
    return record


@requires_db
def test_a_sync_reads_bodies_and_proposes_a_rewrite(clean_db):  # noqa: ANN001
    """The path that had never once completed on real connector behaviour."""
    db = clean_db
    org = _workspace(db)
    _connect(db, org)
    record = _agent(db, org)

    outcome = run_agent(db, record=record, org=org, trigger="test", force=True)

    stored = list(
        db.execute(select(SeoPage).where(SeoPage.tenant_id == org.id)).scalars()
    )
    assert stored, f"no pages were synced: {outcome.summary}"
    # The read step ran: a body and a real word count are present.
    assert any(page.word_count > 0 for page in stored), (
        "every page still has nought words, so read_page was never called — "
        f"summary was {outcome.summary!r}"
    )
    assert outcome.run.detail.get("bodies_read", 0) > 0
    # And with bodies present, the analysis can actually happen.
    assert outcome.run.detail.get("analysed", 0) > 0, outcome.summary


@requires_db
def test_the_auditor_does_not_call_every_page_thin(clean_db):  # noqa: ANN001
    """Word counts come from the read. Without it every page read as nought
    words, which is to say every page on every site was thin content."""
    db = clean_db
    org = _workspace(db)
    _connect(db, org)
    run_agent(db, record=_agent(db, org), org=org, trigger="test", force=True)

    counts = [
        page.word_count
        for page in db.execute(
            select(SeoPage).where(SeoPage.tenant_id == org.id)
        ).scalars()
    ]
    assert counts
    assert not all(count == 0 for count in counts)


# ── The reason a run with no pages gives ───────────────────────────────────
@requires_db
def test_a_scope_that_excludes_everything_says_so_and_shows_the_real_urls(clean_db):  # noqa: ANN001
    """The failure this whole change came from.

    The agent used to filter every page out on the scope, then fall through to
    the connector's "why was my listing empty" diagnosis — which reported on
    the repository's file extensions and told the operator to set a content
    folder. The pages were there. The scope was the problem, and nothing said
    the word "scope".
    """
    db = clean_db
    org = _workspace(db)
    _connect(db, org)
    record = _agent(db, org, scope="/nothing-matches-this")

    outcome = run_agent(db, record=record, org=org, trigger="test", force=True)

    assert outcome.status == "skipped"
    summary = outcome.summary
    assert "scope" in summary.lower(), summary
    # And it shows what the source *can* see, which is the actual fix.
    assert "/" in summary
    # It must not blame the source for a filter the agent applied.
    assert "look like pages" not in summary


@requires_db
def test_a_scope_stored_before_the_validator_existed_is_caught_at_run_time(clean_db):  # noqa: ANN001
    """``inventory`` with no leading slash was in a live database.

    Configure refuses it now, but the row was already saved, and a stored
    typo silently matched nothing for as long as it sat there. Validation at
    the write boundary does not protect a value already written.
    """
    db = clean_db
    org = _workspace(db)
    _connect(db, org)
    record = _agent(db, org, scope="inventory")

    outcome = run_agent(db, record=record, org=org, trigger="test", force=True)

    assert outcome.status == "skipped"
    assert "unusable" in outcome.summary or "start with /" in outcome.summary
