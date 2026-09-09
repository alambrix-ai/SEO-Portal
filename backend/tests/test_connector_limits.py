"""Platform limitations, and the end of guessed credentials.

Two related ideas. A platform's *limits* are stated before the operator tries,
because "your plan does not include the API" cannot be fixed by retyping a
token and the vendor's own 401 does not say it. And nothing is *guessed*: a
credential the operator did not give is an error, not a default, because every
one of these defaults pointed the connector at something real and wrong.
"""
from __future__ import annotations

import pytest

from app.connectors.base import registry
from app.connectors.base.credentials import Credentials
from app.core.exceptions import ConnectorConfigError, ConnectorError


# ── Requirements, stated up front ──────────────────────────────────────────
def test_the_platforms_with_plan_limits_declare_them():
    """The ones where a free tier simply cannot work."""
    for slug in ("wordpress", "webflow", "shopify", "github", "bitbucket"):
        spec = registry.get_spec(slug)
        assert spec is not None, slug
        assert spec.requirements, f"{slug} states no requirements"


def test_wordpress_names_the_plans_that_cannot_be_connected():
    """The case that prompted this: a free WordPress.com site.

    No token will ever work there, so it has to be said before somebody
    spends an afternoon on it.
    """
    text = " ".join(registry.get_spec("wordpress").requirements).lower()
    assert "wordpress.com" in text
    assert "business" in text
    assert "5.6" in text  # application passwords need it
    assert "https" in text


# ── Diagnosis, when it still fails ─────────────────────────────────────────
def _wordpress(site: str):
    klass = registry.get_class("wordpress")
    return klass(
        Credentials(
            values={"siteUrl": site, "username": "u", "applicationPassword": "p"}
        ),
        org_id="o",
    )


def test_a_hosted_wordpress_is_diagnosed_as_a_plan_problem():
    """Not as a bad password, which is what the raw 401 looks like."""
    explanation = _wordpress("https://myblog.wordpress.com").diagnose(
        ConnectorError("WordPress returned 401")
    )
    assert explanation is not None
    assert "Business plan" in explanation
    # And it points at the alternative, since their content may not be in a CMS.
    assert "GitHub" in explanation


def test_plain_http_is_diagnosed_before_the_credentials_are_blamed():
    explanation = _wordpress("http://selfhosted.example").diagnose(
        ConnectorError("WordPress returned 401")
    )
    assert explanation is not None
    assert "HTTP" in explanation


def test_a_missing_rest_api_is_distinguished_from_a_bad_token():
    missing = _wordpress("https://selfhosted.example").diagnose(
        ConnectorError("WordPress returned 404")
    )
    rejected = _wordpress("https://selfhosted.example").diagnose(
        ConnectorError("WordPress returned 401")
    )
    assert "REST API" in (missing or "")
    assert "plugin" in (missing or "")
    assert "application password" in (rejected or "")
    assert missing != rejected


def test_an_unrecognised_failure_leaves_the_original_error_alone():
    """A connector that does not know what happened must not invent a cause."""
    assert (
        _wordpress("https://selfhosted.example").diagnose(
            ConnectorError("Could not reach WordPress: timed out")
        )
        is None
    )


# ── Nothing is guessed ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("slug", "values", "missing"),
    [
        # An application password belongs to a specific user, so a guessed
        # username authenticates as nobody and the 401 looks like a bad token.
        ("wordpress", {"siteUrl": "https://a.test", "applicationPassword": "p"}, "username"),
        # A guessed channel means posting somewhere the customer's whole
        # company can see.
        ("slack", {}, "defaultChannel"),
        # A guessed deal stage counts the wrong deals as conversions, and the
        # number looks plausible either way.
        ("hubspot", {"hubId": "1", "accessToken": "k"}, "wonStageId"),
        # A guessed model changes what is asked and what it costs.
        ("openai", {"apiKey": "k"}, "model"),
        ("perplexity", {"apiKey": "k"}, "model"),
    ],
)
def test_a_credential_nobody_gave_is_an_error_not_a_default(
    slug: str, values: dict, missing: str
):
    klass = registry.get_class(slug)
    assert klass is not None, slug
    instance = klass(Credentials(values=values), org_id="o")

    with pytest.raises(ConnectorError) as caught:
        # Each of these reads the missing credential on its first real call.
        if slug == "wordpress":
            instance.auth_headers()
        elif slug == "slack":
            instance.notify(subject="s", message="m")
        elif slug == "hubspot":
            instance.read_conversions(days=7)
        else:
            instance.check_citations(queries=["q"], domain="a.test")
    assert missing in str(caught.value)


def test_every_guessed_default_is_now_a_declared_field():
    """So the console asks for it rather than the code inventing it."""
    for slug, key in (
        ("wordpress", "username"),
        ("slack", "defaultChannel"),
        ("hubspot", "wonStageId"),
        ("openai", "model"),
        ("perplexity", "model"),
        ("smtp_email", "notifyTo"),
    ):
        spec = registry.get_spec(slug)
        field = next((f for f in spec.fields if f.key == key), None)
        assert field is not None, f"{slug} does not declare {key}"
        assert field.required, f"{slug}.{key} is declared but optional"


def test_a_git_connector_will_not_guess_the_deploy_branch():
    """A repository on master would have every read fail with a stray 404.

    Worse, one that has a main *alongside* the branch that deploys would get
    pull requests opened against the wrong one.
    """
    klass = registry.get_class("github")
    instance = klass(
        Credentials(values={"repository": "acme/site", "token": "t"}), org_id="o"
    )
    with pytest.raises(ConnectorError, match="branch"):
        _ = instance.branch


# ── Repository input, which is where the 404s came from ────────────────────
def _github(repository: str):
    klass = registry.get_class("github")
    return klass(
        Credentials(values={"repository": repository, "token": "t", "branch": "main"}),
        org_id="o",
    )


@pytest.mark.parametrize(
    "written",
    [
        "acme/site",
        "https://github.com/acme/site",
        "https://github.com/acme/site.git",
        "http://github.com/acme/site/",
        "git@github.com:acme/site.git",
        "ssh://git@github.com/acme/site.git",
        "  acme/site  ",
        "/acme/site/",
    ],
)
def test_a_repository_is_accepted_however_it_was_pasted(written: str):
    """People paste what they have, which is the address bar.

    Only stripping slashes turned a pasted URL into a request for
    ``/repos/https://github.com/acme/site`` — answered with a 404 that blamed
    the repository rather than the input.
    """
    assert _github(written).repository == "acme/site"


@pytest.mark.parametrize("written", ["acme", "https://github.com/acme", "a/b/c", "/"])
def test_a_repository_that_is_not_owner_slash_repo_fails_locally(written: str):
    """With a message, rather than a 404 from a vendor that will not say why."""
    with pytest.raises(ConnectorConfigError) as caught:
        _ = _github(written).repository
    assert "owner/repo" in str(caught.value)


def test_githubs_404_is_explained_rather_than_forwarded():
    """GitHub answers 404, not 403, for a repository a token cannot see.

    That is deliberate on their part — a 403 would confirm it exists — and it
    means one status covers four problems, only one of which is a wrong name.
    Forwarding it verbatim sends the operator to check the thing most likely
    to be correct.
    """
    explanation = _github("acme/site").diagnose(
        ConnectorError('GitHub rejected the request (404): {"message": "Not Found"}')
    )
    assert explanation is not None
    assert "404 rather than 403" in explanation
    # All four causes, in the order they are worth checking.
    assert "fine-grained token" in explanation
    assert "different account" in explanation
    assert "approved" in explanation
    assert "owner/repo" in explanation


def test_an_expired_token_and_a_missing_scope_are_told_apart():
    connector = _github("acme/site")
    expired = connector.diagnose(ConnectorError("GitHub returned 401"))
    refused = connector.diagnose(ConnectorError("GitHub returned 403"))
    assert "expired" in (expired or "")
    assert "Contents: read and write" in (refused or "")
    assert expired != refused


def test_bitbucket_explains_its_own_404_in_its_own_terms():
    klass = registry.get_class("bitbucket")
    connector = klass(
        Credentials(values={"repository": "team/site", "token": "t", "branch": "main"}),
        org_id="o",
    )
    explanation = connector.diagnose(ConnectorError("Bitbucket returned 404"))
    # Workspace ID, not display name — the Bitbucket-specific trap.
    assert "workspace/repo" in (explanation or "")
    assert "display name" in (explanation or "")


# ── The vendor's own words ─────────────────────────────────────────────────
# Every label here was checked against the vendor's current documentation.
# The cost of getting one wrong is not cosmetic: an operator hunting a
# WordPress screen for an "API key" does not find one, because WordPress does
# not have one, and concludes the platform is broken. These assertions are the
# record of what was verified, so a future edit that reverts to a plausible
# invention fails here instead of in somebody's onboarding call.
@pytest.mark.parametrize(
    ("slug", "key", "label"),
    [
        # WordPress issues an Application Password, per user, and calls the
        # other half a username (the login, not the display name).
        ("wordpress", "applicationPassword", "Application Password"),
        ("wordpress", "username", "Username"),
        # Webflow's word is "site token" — a workspace token is a different
        # object that will not authorise site content.
        ("webflow", "siteToken", "Site token"),
        # HubSpot renamed Portal ID to Hub ID and sunset API keys entirely;
        # what this takes is a private app access token.
        ("hubspot", "hubId", "Hub ID"),
        ("hubspot", "accessToken", "Private app access token"),
        # Shopify's own name for the credential a custom app generates.
        ("shopify", "adminToken", "Admin API access token"),
        # Salesforce's standard field is LeadSource.
        ("salesforce", "leadSource", "Lead Source"),
        # GA4's admin screen shows a bare numeric PROPERTY ID; the
        # "properties/" prefix is an API path detail we add ourselves.
        ("google_analytics_4", "propertyId", "Property ID"),
        # Google Ads: the manager-account header, which auth_headers() has
        # always sent and no dialog used to offer.
        ("google_ads", "loginCustomerId", "Login customer ID"),
        # Adobe needs the global company id in every 2.0 path, and it is not
        # the report suite id.
        ("adobe_analytics", "globalCompanyId", "Global Company ID"),
        # GitHub issues several kinds of token; this one is a PAT.
        ("github", "token", "Personal access token"),
    ],
)
def test_a_field_uses_the_name_the_vendor_uses(slug: str, key: str, label: str):
    spec = registry.get_spec(slug)
    assert spec is not None, slug
    found = {field.key: field.label for field in spec.fields}
    assert key in found, f"{slug} has no {key!r} field; it has {sorted(found)}"
    assert found[key] == label


@pytest.mark.parametrize(
    ("slug", "key"),
    [
        # Every Google Ads and Microsoft Advertising call carries a developer
        # token. Declaring it optional produced a connector that saved
        # happily and then failed on first use, which is the worst of both.
        ("google_ads", "developerToken"),
        ("microsoft_ads", "developerToken"),
        # Sent as x-api-key on every Adobe request.
        ("adobe_analytics", "clientId"),
    ],
)
def test_a_credential_the_api_always_needs_is_not_optional(slug: str, key: str):
    spec = registry.get_spec(slug)
    field = next(f for f in spec.fields if f.key == key)
    assert field.required is True


def test_a_developer_token_is_stored_as_a_secret():
    """It authenticates the caller to the API, so it is not an account id."""
    for slug in ("google_ads", "microsoft_ads"):
        field = next(
            f for f in registry.get_spec(slug).fields if f.key == "developerToken"
        )
        assert field.is_secret, slug


def test_no_field_label_hard_codes_a_currency():
    """The budget fields said "(USD)" while every figure beside them was in
    rupees. The label follows CURRENCY_SYMBOL now."""
    from app.core.config import settings

    for spec in registry.all_specs():
        for field in spec.fields:
            assert "USD" not in field.label, f"{spec.slug}.{field.key}"
            if field.key == "monthlyBudget":
                assert settings.currency_symbol in field.label, spec.slug


@pytest.mark.parametrize(
    ("entered", "expected"),
    [
        ("1000000", 1_000_000.0),
        # What somebody actually types into a rupee field.
        ("10,00,000", 1_000_000.0),
        ("1,000,000", 1_000_000.0),
        ("₹10,00,000", 1_000_000.0),
    ],
)
def test_a_grouped_budget_is_read_at_its_real_value(entered: str, expected: float):
    """It used to return the 10,000 default on any ValueError, so a grouped
    figure silently became a hundredth of the budget and pacing was wrong by
    that factor with nothing saying so."""
    klass = registry.get_class("google_ads")
    connector = klass(
        Credentials(values={"customerId": "1", "monthlyBudget": entered}), org_id="o"
    )
    assert connector.monthly_budget == expected


def test_a_budget_that_is_not_a_number_is_refused_not_defaulted():
    klass = registry.get_class("google_ads")
    connector = klass(
        Credentials(values={"customerId": "1", "monthlyBudget": "ten lakh"}),
        org_id="o",
    )
    with pytest.raises(ConnectorConfigError, match="not a number"):
        _ = connector.monthly_budget
