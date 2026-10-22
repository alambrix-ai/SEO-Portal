"""Sign-in providers: the platform-level half of "Sign in with <Vendor>".

A *provider* is a vendor's OAuth 2.0 authorization server plus the app the
platform operator registered with it. Everything a connector shares with the
other connectors of the same vendor lives here — the endpoints, PKCE, the
extra parameters, and where the client id and secret come from. What is
particular to one connector (its scopes, the fields the platform fills in)
is declared on that connector's spec as an
:class:`~app.connectors.base.connector.OAuthBinding`.

Adding a vendor
---------------
1. Add ``oauth_<name>_client_id`` and ``oauth_<name>_client_secret`` to
   :class:`app.core.config.Settings` (and ``.env.example``).
2. Add an :class:`OAuthProvider` to :data:`PROVIDERS` below.
3. On each of the vendor's connectors, set ``spec.oauth = OAuthBinding(...)``
   with the minimum scopes, and implement ``list_oauth_choices`` if the
   account has to pick something (an ad account, a page).
4. Mix in :class:`~app.connectors.base.oauth.OAuthTokenMixin` with
   ``token_url`` set if its tokens are refreshed.
5. Add a :class:`~app.services.provider_setup.ProviderSetup` (steps, field
   labels as the vendor's console shows them) so Portal Admin can set it up.
6. Register the redirect URI Portal Admin shows with the vendor, and add a
   section to ``docs/oauth-setup.md``.

A provider is *available* only when its client id and secret are set; until
then the console says sign-in is being set up (and keeps a vendor's guided
key steps where it has them).

Where the values come from
--------------------------
Portal Admin -> Sign-in providers saves them in ``platform_oauth_apps``
(encrypted); a value saved there wins over the server setting of the same
name. :func:`platform_setting` is the one place both are read.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.connectors.base.connector import ConnectorSpec
from app.connectors.base.meta_graph import GRAPH_API_VERSION
from app.core.config import settings


def platform_setting(key: str) -> str:
    """A sign-in provider value: saved in Portal Admin, else the server
    setting of the same name (``oauth_google_client_id``,
    ``google_ads_developer_token``, ``github_app_private_key``...)."""
    from app.services import platform_oauth_apps

    return platform_oauth_apps.stored_value(key) or str(getattr(settings, key, "") or "").strip()


@dataclass(frozen=True, slots=True)
class OAuthProvider:
    #: Path segment of the callback URL, and the settings prefix.
    name: str
    #: The vendor's name as users know it ("Google").
    label: str
    #: ``oauth2`` is the authorization-code flow; ``wordpress_app_password``
    #: is WordPress's own application-password approval screen, which needs
    #: no platform app and is always available.
    kind: Literal["oauth2", "wordpress_app_password"] = "oauth2"
    authorize_url: str = ""
    token_url: str = ""
    #: Scopes added to every connector's own (identity, so the console can
    #: say which account signed in).
    base_scopes: tuple[str, ...] = ()
    pkce: bool = True
    #: Sent on every authorize request.
    extra_params: dict[str, str] = field(default_factory=dict)
    #: Where the operator registers the platform's app.
    console_url: str = ""
    # ── Vendor differences (defaults are standard OAuth 2.0, as Google) ────
    #: Joins scopes in the authorize URL (Pinterest wants commas).
    scope_separator: str = " "
    #: How the client authenticates at the token endpoint: ``body`` sends
    #: client_id/client_secret as form fields; ``basic`` sends them as an
    #: HTTP Basic header (X, Pinterest).
    token_auth: Literal["body", "basic"] = "body"
    #: Refuse a sign-in that returns no refresh token. False for vendors
    #: whose tokens are long-lived instead (Meta, TikTok, LinkedIn without
    #: programmatic refresh): the access token and its expiry are stored.
    refresh_required: bool = True
    #: Each refresh returns a new refresh token that replaces the old one
    #: (X, Microsoft). Such a sign-in is never copied to a sibling
    #: connection, and refreshed tokens are written back.
    rotates_refresh_tokens: bool = False
    #: Name of the vendor-specific hooks in :mod:`app.services.oauth_vendors`
    #: (authorize URL, code exchange, account label). Empty: none.
    flavor: str = ""
    #: Phase 2B: name of the vendor-specific hooks in
    #: :mod:`app.services.oauth_business` (inputs checked before sign-in,
    #: templated hosts such as ``https://{shop}/…``, callback verification,
    #: and what the token response carries besides the token). Empty: none.
    extension: str = ""
    #: A server setting (a boolean) that must be on before this provider is
    #: offered at all, for a vendor whose terms need the operator's own
    #: approval first (Reddit). Empty: none.
    enabled_setting: str = ""

    # ── Client credentials ────────────────────────────────────────────────
    @property
    def client_id(self) -> str:
        return platform_setting(f"oauth_{self.name}_client_id")

    @property
    def client_secret(self) -> str:
        return platform_setting(f"oauth_{self.name}_client_secret")

    @property
    def enabled(self) -> bool:
        """False while :attr:`enabled_setting` names a server switch that is off."""
        return not self.enabled_setting or bool(getattr(settings, self.enabled_setting, False))

    @property
    def settings_keys(self) -> tuple[str, ...]:
        if self.kind != "oauth2":
            return ()
        return (
            # The switch is listed only while it is the thing still missing.
            *((self.enabled_setting.upper(),) if not self.enabled else ()),
            f"OAUTH_{self.name.upper()}_CLIENT_ID",
            f"OAUTH_{self.name.upper()}_CLIENT_SECRET",
        )

    @property
    def configured(self) -> bool:
        if self.kind != "oauth2":
            return True
        return self.enabled and bool(self.client_id and self.client_secret)

    @property
    def redirect_uri(self) -> str:
        """The exact URI to register with the vendor."""
        return f"{settings.public_api_root}{settings.api_v1_prefix}/oauth/callback/{self.name}"


GOOGLE = OAuthProvider(
    name="google",
    label="Google",
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    token_url="https://oauth2.googleapis.com/token",
    base_scopes=("openid", "email"),
    pkce=True,
    extra_params={
        # A refresh token, so the connection keeps working after an hour.
        "access_type": "offline",
        # Google only returns a refresh token on consent; asking every time
        # guarantees one even when this account has signed in before.
        "prompt": "consent",
        # Incremental authorization: each Google connector asks only for its
        # own scope, and the token that comes back also carries the scopes
        # granted before.
        "include_granted_scopes": "true",
    },
    console_url="https://console.cloud.google.com/apis/credentials",
)

WORDPRESS = OAuthProvider(
    name="wordpress",
    label="WordPress",
    kind="wordpress_app_password",
    pkce=False,
)

# ── OAuth providers: social and ads (phase 2A) ─────────────────────────────
# Setup per vendor: docs/oauth-setup-social-ads.md.
#: Facebook Login: Meta Ads, Facebook Pages, Instagram. Codes are exchanged
#: (GET) and then swapped for a long-lived user token (~60 days); there is
#: no refresh token. Page tokens derived from it do not expire.
META = OAuthProvider(
    name="meta",
    label="Facebook",
    authorize_url=f"https://www.facebook.com/{GRAPH_API_VERSION}/dialog/oauth",
    token_url=f"https://graph.facebook.com/{GRAPH_API_VERSION}/oauth/access_token",
    # PKCE is documented only for Meta's OIDC flow; state and the secret
    # protect this confidential code flow.
    pkce=False,
    scope_separator=",",
    refresh_required=False,
    flavor="meta",
    console_url="https://developers.facebook.com/apps/",
)
#: LinkedIn, Advertising API app. Access tokens last 60 days; approved
#: Marketing Developer Platform apps also get a 365-day refresh token.
LINKEDIN = OAuthProvider(
    name="linkedin",
    label="LinkedIn",
    authorize_url="https://www.linkedin.com/oauth/v2/authorization",
    token_url="https://www.linkedin.com/oauth/v2/accessToken",
    # LinkedIn offers PKCE to native (loopback) clients only.
    pkce=False,
    refresh_required=False,
    console_url="https://www.linkedin.com/developers/apps",
)
#: LinkedIn, Community Management API app. LinkedIn requires that product
#: to be the only one on its app, hence a second provider.
LINKEDIN_PAGES = OAuthProvider(
    name="linkedin_pages",
    label="LinkedIn",
    authorize_url="https://www.linkedin.com/oauth/v2/authorization",
    token_url="https://www.linkedin.com/oauth/v2/accessToken",
    pkce=False,
    refresh_required=False,
    console_url="https://www.linkedin.com/developers/apps",
)
#: X OAuth 2.0 (confidential client, PKCE). Access tokens last two hours;
#: each refresh returns a new single-use refresh token.
X = OAuthProvider(
    name="x",
    label="X",
    authorize_url="https://x.com/i/oauth2/authorize",
    token_url="https://api.x.com/2/oauth2/token",
    pkce=True,
    token_auth="basic",
    rotates_refresh_tokens=True,
    flavor="x",
    console_url="https://developer.x.com/en/portal/dashboard",
)
#: Pinterest API v5. Access tokens last 30 days and are refreshed.
PINTEREST = OAuthProvider(
    name="pinterest",
    label="Pinterest",
    authorize_url="https://www.pinterest.com/oauth/",
    token_url="https://api.pinterest.com/v5/oauth/token",
    pkce=False,
    scope_separator=",",
    token_auth="basic",
    rotates_refresh_tokens=True,
    flavor="pinterest",
    console_url="https://developers.pinterest.com/apps/",
)
#: TikTok for Business (Marketing API). Its own authorize page and a JSON
#: token call; the long-term token does not expire until revoked. The
#: permissions are chosen on the developer app, not in the URL.
TIKTOK = OAuthProvider(
    name="tiktok",
    label="TikTok",
    authorize_url="https://business-api.tiktok.com/portal/auth",
    token_url="https://business-api.tiktok.com/open_api/v1.3/oauth2/access_token/",
    pkce=False,
    refresh_required=False,
    flavor="tiktok",
    console_url="https://business-api.tiktok.com/portal/apps",
)
#: Microsoft identity platform, for Microsoft Advertising. Refresh tokens
#: replace themselves on every use.
MICROSOFT = OAuthProvider(
    name="microsoft",
    label="Microsoft",
    authorize_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
    token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
    base_scopes=("openid", "email", "offline_access"),
    pkce=True,
    extra_params={"prompt": "select_account"},
    rotates_refresh_tokens=True,
    console_url="https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade",
)
#: Bing Webmaster Tools' own OAuth server (not Entra).
BING_WEBMASTER = OAuthProvider(
    name="bing_webmaster",
    label="Bing",
    authorize_url="https://www.bing.com/webmasters/oauth/authorize",
    token_url="https://www.bing.com/webmasters/oauth/token",
    pkce=False,
    # Microsoft's page says refresh tokens last until revoked; developers
    # report them being replaced on use. Either way the newest is kept.
    rotates_refresh_tokens=True,
    console_url="https://www.bing.com/webmasters/",
)
#: Reddit. Commercial use of its API needs Reddit's written approval, so the
#: provider stays off until the operator sets REDDIT_ENABLED. Access tokens
#: last an hour; ``duration=permanent`` asks for a refresh token, and a
#: refresh does not return a new one (the original is kept). No PKCE; the
#: client authenticates with Basic. Every call, the token calls included,
#: carries ``settings.reddit_user_agent`` (flavor "reddit").
REDDIT = OAuthProvider(
    name="reddit",
    label="Reddit",
    authorize_url="https://www.reddit.com/api/v1/authorize",
    token_url="https://www.reddit.com/api/v1/access_token",
    pkce=False,
    extra_params={"duration": "permanent"},
    token_auth="basic",
    refresh_required=True,
    flavor="reddit",
    enabled_setting="reddit_enabled",
    console_url="https://www.reddit.com/prefs/apps",
)
SOCIAL_ADS_PROVIDERS = (
    META, LINKEDIN, LINKEDIN_PAGES, X, PINTEREST, TIKTOK, MICROSOFT, BING_WEBMASTER, REDDIT,
)
# ── end of phase 2A providers ──────────────────────────────────────────────

# ── OAuth providers: business apps (phase 2B) ──────────────────────────────
# Setup per vendor: docs/oauth-setup-business-apps.md. Vendor-specific steps
# live in app/services/oauth_business.py (``extension``).
#: Slack OAuth v2 ("Add to Slack"). The bot token does not expire.
SLACK = OAuthProvider(
    name="slack",
    label="Slack",
    authorize_url="https://slack.com/oauth/v2/authorize",
    token_url="https://slack.com/api/oauth.v2.access",
    pkce=False,
    scope_separator=",",
    refresh_required=False,
    extension="slack",
    console_url="https://api.slack.com/apps",
)
#: HubSpot public app. Access tokens last 30 minutes; the refresh token is
#: long-lived. "oauth" is the base scope every HubSpot app carries.
HUBSPOT = OAuthProvider(
    name="hubspot",
    label="HubSpot",
    authorize_url="https://app.hubspot.com/oauth/authorize",
    token_url="https://api.hubapi.com/oauth/v3/token",
    base_scopes=("oauth",),
    pkce=False,
    extension="hubspot",
    console_url="https://developers.hubspot.com/",
)
#: Salesforce web-server flow with PKCE. ``{login_host}`` is
#: login.salesforce.com, or test.salesforce.com for a sandbox.
SALESFORCE = OAuthProvider(
    name="salesforce",
    label="Salesforce",
    authorize_url="https://{login_host}/services/oauth2/authorize",
    token_url="https://{login_host}/services/oauth2/token",
    pkce=True,
    extension="salesforce",
    console_url="https://login.salesforce.com/",
)
#: Mailchimp. No scopes; the token does not expire.
MAILCHIMP = OAuthProvider(
    name="mailchimp",
    label="Mailchimp",
    authorize_url="https://login.mailchimp.com/oauth2/authorize",
    token_url="https://login.mailchimp.com/oauth2/token",
    pkce=False,
    refresh_required=False,
    extension="mailchimp",
    console_url="https://admin.mailchimp.com/account/oauth2/",
)
#: Klaviyo: PKCE is required, the client authenticates with Basic, access
#: tokens last an hour and the refresh token is kept.
KLAVIYO = OAuthProvider(
    name="klaviyo",
    label="Klaviyo",
    authorize_url="https://www.klaviyo.com/oauth/authorize",
    token_url="https://a.klaviyo.com/oauth/token",
    pkce=True,
    token_auth="basic",
    extension="klaviyo",
    console_url="https://www.klaviyo.com/manage-apps",
)
#: Shopify authorization code grant on the store's own host. Expiring
#: offline tokens: an hour, with a 90-day refresh token replaced on use.
SHOPIFY = OAuthProvider(
    name="shopify",
    label="Shopify",
    authorize_url="https://{shop}/admin/oauth/authorize",
    token_url="https://{shop}/admin/oauth/access_token",
    pkce=False,
    scope_separator=",",
    # An app on non-expiring offline tokens returns no refresh token; that
    # token then lasts until the app is uninstalled.
    refresh_required=False,
    rotates_refresh_tokens=True,
    extension="shopify",
    console_url="https://dev.shopify.com/dashboard",
)
#: A GitHub App, installed on the repositories the person chooses. Its
#: client id/secret confirm who installed it; the connector then works with
#: installation tokens minted from the app's private key.
GITHUB = OAuthProvider(
    name="github",
    label="GitHub",
    authorize_url="https://github.com/apps/{app_slug}/installations/new",
    token_url="https://github.com/login/oauth/access_token",
    pkce=False,
    refresh_required=False,
    extension="github",
    console_url="https://github.com/settings/apps",
)
#: Bitbucket Cloud OAuth consumer. Scopes are set on the consumer itself;
#: access tokens last about two hours and each refresh replaces the
#: refresh token.
BITBUCKET = OAuthProvider(
    name="bitbucket",
    label="Bitbucket",
    authorize_url="https://bitbucket.org/site/oauth2/authorize",
    token_url="https://bitbucket.org/site/oauth2/access_token",
    pkce=False,
    token_auth="basic",
    rotates_refresh_tokens=True,
    extension="bitbucket",
    console_url="https://bitbucket.org/account/workspaces/",
)
#: Webflow Data API v2 app. No refresh token is documented; the token lasts
#: until the app is uninstalled (or a year without use).
WEBFLOW = OAuthProvider(
    name="webflow",
    label="Webflow",
    authorize_url="https://webflow.com/oauth/authorize",
    token_url="https://api.webflow.com/oauth/access_token",
    pkce=False,
    refresh_required=False,
    extension="webflow",
    console_url="https://webflow.com/dashboard",
)
BUSINESS_APP_PROVIDERS = (
    SLACK, HUBSPOT, SALESFORCE, MAILCHIMP, KLAVIYO, SHOPIFY, GITHUB, BITBUCKET, WEBFLOW,
)
# ── end of phase 2B providers ──────────────────────────────────────────────

PROVIDERS: dict[str, OAuthProvider] = {
    p.name: p for p in (GOOGLE, WORDPRESS, *SOCIAL_ADS_PROVIDERS, *BUSINESS_APP_PROVIDERS)
}


def get(name: str) -> OAuthProvider | None:
    return PROVIDERS.get((name or "").strip().lower())


def provider_for(spec: ConnectorSpec | None) -> OAuthProvider | None:
    if spec is None or spec.oauth is None:
        return None
    return get(spec.oauth.provider)


def missing_settings(spec: ConnectorSpec) -> list[str]:
    """The environment keys still to set before this connector's sign-in
    can be offered. Empty when it is ready."""
    provider = provider_for(spec)
    if provider is None:
        return []
    missing: list[str] = []
    if not provider.configured:
        missing.extend(provider.settings_keys)
    for key in spec.oauth.requires_settings if spec.oauth else ():
        if not platform_setting(key):
            missing.append(key.upper())
    return missing


def sign_in_available(spec: ConnectorSpec | None) -> bool:
    return provider_for(spec) is not None and not missing_settings(spec)  # type: ignore[arg-type]


#: Scopes a connector asks for but can work without: a sign-in that leaves
#: one out still connects, and only the feature that needs it is missing
#: (the connector then says to reconnect for it). Keyed by connector slug.
#: Posting scopes are never optional.
OPTIONAL_SCOPES: dict[str, frozenset[str]] = {
    # Post insights: without them posting works and metrics say
    # "Reconnect Facebook/Instagram to allow insights".
    "facebook_pages": frozenset({"read_insights"}),
    "instagram_business": frozenset({"instagram_manage_insights"}),
    # Image upload: without it posts go out as text, with a note.
    "x_twitter": frozenset({"media.write"}),
}


def optional_scopes(spec: ConnectorSpec | None) -> frozenset[str]:
    """The scopes ``spec`` asks for but does not need in order to connect."""
    return OPTIONAL_SCOPES.get(spec.slug, frozenset()) if spec is not None else frozenset()


def required_only(spec: ConnectorSpec | None, absent: list[str]) -> list[str]:
    """``absent`` (scopes a sign-in did not grant) without the optional ones.

    This decides ``permissions_missing``: a grant missing only optional
    scopes still connects.
    """
    optional = optional_scopes(spec)
    return [scope for scope in absent if scope not in optional]


def scopes_for(spec: ConnectorSpec, inputs: dict[str, str] | None = None) -> tuple[str, ...]:
    """Everything the authorize request asks for, de-duplicated, in order.

    ``inputs`` are the checked answers to the connector's sign-in inputs; a
    ticked sandbox toggle can swap the connector's scopes (Microsoft Ads).
    """
    provider = provider_for(spec)
    own = spec.oauth.scopes_for_inputs(inputs) if spec.oauth else ()
    wanted = [*(provider.base_scopes if provider else ()), *own]
    return tuple(dict.fromkeys(wanted))
