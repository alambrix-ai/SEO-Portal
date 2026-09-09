"""Every agent's output has somewhere to be read.

Three models were written by agents and referenced by no API module and no
view builder: ``AeoQaPair``, ``SchemaPatch`` and ``OutreachPitch``. One
workspace had 24 finished question-and-answer pairs stored and nowhere at
all to read them, so the agent that produced them looked inert — which is
exactly how it was reported.

Two approvals were also un-reviewable. The AEO injection carried a URL and a
list of internal ids, not the questions; the PR pitch carried a domain, a
tone and a variant letter, not the subject or the body. Approving that
second one sends an email to a stranger under the customer's name.
"""
from __future__ import annotations

from tests.conftest import requires_db


@requires_db
def test_a_pages_qa_pairs_and_schema_are_readable(client, clean_db):  # noqa: ANN001
    """The page a reviewer opens carries the work every agent did on it."""
    from sqlalchemy import select

    from app.db.session import bind_tenant
    from app.models.seo import AeoQaPair, SeoPage
    from app.models.workspace import Organization
    from app.services.encryption import Ctx, OrgCipher
    from tests.test_api_flows import auth, register

    body = register(client)
    token = body["tokens"]["access_token"]
    org_id = body["session"]["organization"]["id"]
    bind_tenant(clean_db, org_id)
    org = clean_db.get(Organization, org_id)
    cipher = OrgCipher.for_org(org)

    page = SeoPage(
        tenant_id=org_id, url="/pricing", title="Pricing", cms_slug="fake_cms"
    )
    clean_db.add(page)
    clean_db.flush()
    clean_db.add(
        AeoQaPair(
            tenant_id=org_id,
            page_id=page.id,
            question_encrypted=cipher.encrypt(
                "How much does it cost?", context=Ctx.QA_QUESTION
            ),
            answer_encrypted=cipher.encrypt(
                "Plans start at a fixed monthly fee.", context=Ctx.QA_ANSWER
            ),
        )
    )
    clean_db.commit()

    detail = client.get(f"/api/v1/seo/pages/{page.id}", headers=auth(token)).json()
    assert len(detail["qa_pairs"]) == 1
    pair = detail["qa_pairs"][0]
    # The text itself, not an id.
    assert pair["question"] == "How much does it cost?"
    assert "fixed monthly fee" in pair["answer"]
    assert pair["injected"] is False
    assert pair["status"]
    assert detail["schema_patches"] == []


def test_an_approval_carries_the_content_it_approves():
    """Both of these were approvals somebody could not inform themselves
    about: the reviewer saw a URL, or a domain and a variant letter."""
    import inspect

    from app.agents.aeo_qa_injector import agent as aeo
    from app.agents.digital_pr_outreach import agent as pr

    aeo_source = inspect.getsource(aeo)
    # The questions and answers, not just the row ids.
    assert '"pairs": [' in aeo_source
    assert "question" in aeo_source and "answer" in aeo_source

    pr_source = inspect.getsource(pr)
    # The email that will actually be sent.
    assert '"subject":' in pr_source
    assert '"body":' in pr_source


def test_the_pitcher_will_not_invent_the_claim_it_pitches():
    """With no angle configured it fell back to "an original data study from
    our own sales data" — a statement about the customer's business that may
    be untrue, offered to a publisher under their name."""
    import inspect

    from app.agents.digital_pr_outreach import agent as pr

    source = inspect.getsource(pr)
    assert "our own sales data" not in source
    assert "Set the story angle" in source
