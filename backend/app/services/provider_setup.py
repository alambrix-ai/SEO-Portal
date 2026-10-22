"""What Portal Admin shows to set up each sign-in provider.

Pure data: per provider, the values its vendor console gives you (labelled
the way that console labels them), the steps in plain words, and where the
permissions are chosen. The redirect URI, the console link and which
integrations a provider unlocks are filled in from
:mod:`app.connectors.base.oauth_providers` and the connector registry.

The steps are condensed from ``docs/oauth-setup.md``,
``docs/oauth-setup-social-ads.md`` and ``docs/oauth-setup-business-apps.md``;
those documents keep the detail (review timelines, edge cases).

Every value is stored under the name of its server setting
(``oauth_google_client_id``, ``google_ads_developer_token``), so a value
saved here and one set in the environment are the same thing, and the saved
one wins.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AppField:
    #: The server setting this value stands for (``oauth_google_client_id``).
    key: str
    #: As the vendor's console labels it.
    label: str
    secret: bool = False
    #: A multi-line value (a PEM private key).
    multiline: bool = False
    #: Empty when the provider needs it; otherwise the integration that
    #: alone needs it ("Google Ads").
    needed_for: str = ""
    help: str = ""
    placeholder: str = ""


@dataclass(frozen=True, slots=True)
class ProviderSetup:
    name: str
    #: Order on the page: most useful to a marketing team first.
    rank: int
    #: What the vendor calls its console ("Google Cloud console").
    console_label: str
    steps: tuple[str, ...]
    client_id_label: str = "Client ID"
    client_secret_label: str = "Client secret"
    client_id_placeholder: str = ""
    extras: tuple[AppField, ...] = ()
    #: Where in the console the permissions below are switched on.
    permissions_where: str = ""
    #: Permissions chosen on the app itself rather than asked for at sign-in
    #: (TikTok, GitHub, Bitbucket). Shown in place of the requested scopes.
    app_permissions: tuple[str, ...] = ()
    #: One sentence on approval by the vendor, when it gates real customers.
    review_note: str = ""

    @property
    def fields(self) -> tuple[AppField, ...]:
        return (
            AppField(
                key=f"oauth_{self.name}_client_id",
                label=self.client_id_label,
                placeholder=self.client_id_placeholder,
            ),
            AppField(key=f"oauth_{self.name}_client_secret", label=self.client_secret_label, secret=True),
            *self.extras,
        )


SETUPS: dict[str, ProviderSetup] = {
    s.name: s
    for s in (
        ProviderSetup(
            name="google",
            rank=10,
            console_label="Google Cloud console",
            steps=(
                "Open the Google Cloud console and create a project for this platform (or pick the one you use).",
                "Under APIs & Services → Library, enable the Google Analytics Data API, Google Analytics Admin "
                "API, Google Search Console API, Google Ads API and the Business Profile APIs.",
                "Under Google Auth Platform, fill in Branding (app name, support email, your domain, privacy "
                "policy and terms links), then add the permissions listed below under Data access.",
                "Under Clients, create an OAuth client of type “Web application”. Paste the redirect URI "
                "below into “Authorised redirect URIs”.",
                "Copy the Client ID and Client secret into the boxes below and save.",
                "For Google Ads, also paste the Developer token from your Google Ads manager account "
                "(Tools → API Center).",
            ),
            client_id_placeholder="1234567890-abc.apps.googleusercontent.com",
            extras=(
                AppField(
                    key="google_ads_developer_token",
                    label="Developer token",
                    secret=True,
                    needed_for="Google Ads",
                    help="From your Google Ads manager account, Tools → API Center. Apply for Basic access "
                    "before customers connect real accounts.",
                ),
            ),
            permissions_where="Google Auth Platform → Data access",
            review_note=(
                "While the app is in Testing, only the test users you list can sign in, and their "
                "connections stop after 7 days. Publish it and complete Google's verification before "
                "customers connect."
            ),
        ),
        ProviderSetup(
            name="meta",
            rank=20,
            console_label="Meta for Developers",
            steps=(
                "Open Meta for Developers and create an app of type “Business”.",
                "Add the Facebook Login product. Under its Settings, paste the redirect URI below into "
                "“Valid OAuth Redirect URIs”.",
                "Add the Marketing API product (for Meta Ads) and the Instagram product (for Instagram).",
                "Leave “Require app secret” switched off.",
                "In App settings → Basic, copy the App ID and App secret into the boxes below and save.",
            ),
            client_id_label="App ID",
            client_secret_label="App secret",
            permissions_where="App Review → Permissions and features",
            review_note=(
                "Meta's App Review and Business Verification are needed before people outside your app's "
                "roles can sign in."
            ),
        ),
        ProviderSetup(
            name="linkedin",
            rank=30,
            console_label="LinkedIn Developers",
            steps=(
                "Open LinkedIn Developers and create an app linked to your company's LinkedIn Page. This app "
                "is for LinkedIn Ads.",
                "Under Products, request the Advertising API.",
                "Under Auth, paste the redirect URI below into “Authorized redirect URLs for your app”.",
                "A super admin of your LinkedIn Page verifies the app (Settings → Verify).",
                "Copy the Client ID and Primary Client Secret from the Auth tab into the boxes below and save.",
            ),
            client_secret_label="Primary Client Secret",
            permissions_where="Products → Advertising API",
        ),
        ProviderSetup(
            name="linkedin_pages",
            rank=31,
            console_label="LinkedIn Developers",
            steps=(
                "LinkedIn requires a second app for company pages. Open LinkedIn Developers and create another "
                "app linked to your company's LinkedIn Page.",
                "Under Products, request only the Community Management API (LinkedIn allows no other product "
                "on this app).",
                "Under Auth, paste the redirect URI below into “Authorized redirect URLs for your app”.",
                "A super admin of your LinkedIn Page verifies the app.",
                "Copy the Client ID and Primary Client Secret into the boxes below and save.",
            ),
            client_secret_label="Primary Client Secret",
            permissions_where="Products → Community Management API",
        ),
        ProviderSetup(
            name="microsoft",
            rank=40,
            console_label="Microsoft Entra admin center",
            steps=(
                "Open the Microsoft Entra admin center with a work or school account and create a new app "
                "registration.",
                "Set Supported account types to “Accounts in any organizational directory and personal "
                "Microsoft accounts”.",
                "Add a Web redirect URI and paste the address below.",
                "Under Certificates & secrets, create a client secret and copy its Value straight away.",
                "Copy the Application (client) ID from the Overview page and the secret Value into the boxes "
                "below.",
                "In your own Microsoft Advertising account (Settings → Developer settings), request a "
                "universal Developer token and paste it below.",
            ),
            client_id_label="Application (client) ID",
            client_secret_label="Client secret value",
            extras=(
                AppField(
                    key="microsoft_ads_developer_token",
                    label="Developer token",
                    secret=True,
                    needed_for="Microsoft Ads",
                    help="From your Microsoft Advertising account, Settings → Developer settings. Choose a "
                    "universal token.",
                ),
            ),
        ),
        ProviderSetup(
            name="bing_webmaster",
            rank=50,
            console_label="Bing Webmaster Tools",
            steps=(
                "Sign in to Bing Webmaster Tools with the platform's account.",
                "Go to Settings → API Access, accept the terms and choose “OAuth client”.",
                "Enter a name and paste the redirect URI below.",
                "Copy the Client ID and Client secret into the boxes below and save.",
            ),
        ),
        ProviderSetup(
            name="x",
            rank=60,
            console_label="X Developer Portal",
            steps=(
                "Open the X Developer Portal, choose your project's app and open “User authentication "
                "settings”.",
                "Set App permissions to “Read and write” and Type of App to “Web App, Automated App or Bot”.",
                "Paste the redirect URI below as the Callback URI, and add your website URL.",
                "Under Keys and tokens, copy the OAuth 2.0 Client ID and Client Secret (not the API Key) into "
                "the boxes below and save.",
            ),
            client_id_label="OAuth 2.0 Client ID",
            client_secret_label="Client Secret",
        ),
        ProviderSetup(
            name="tiktok",
            rank=70,
            console_label="TikTok API for Business",
            steps=(
                "Register as a developer on TikTok API for Business and wait for approval.",
                "Create a developer app and paste the redirect URI below as the “Advertiser redirect URL”.",
                "Under Scope of permission, tick the permissions listed below.",
                "Submit the app for review (about 2–3 business days).",
                "Copy the App ID and Secret into the boxes below and save.",
            ),
            client_id_label="App ID",
            client_secret_label="Secret",
            permissions_where="Scope of permission",
            app_permissions=("Ad Account Management", "Reporting", "Ads Management", "Creative Management"),
        ),
        ProviderSetup(
            name="pinterest",
            rank=80,
            console_label="Pinterest Developers",
            steps=(
                "Open Pinterest Developers and create an app connected to your business account.",
                "Paste the redirect URI below into the app's Redirect URIs.",
                "Copy the App ID and App secret key into the boxes below and save.",
            ),
            client_id_label="App ID",
            client_secret_label="App secret key",
            review_note="Pinterest grants Trial access first; request Standard access before going live.",
        ),
        ProviderSetup(
            name="reddit",
            rank=85,
            console_label="Reddit app preferences",
            steps=(
                "Get Reddit's written approval to use its API for your business first. Reddit does not "
                "allow commercial use without it.",
                "Signed in to the Reddit account that owns the app, open Reddit's app preferences and "
                "choose “create another app”. Pick the type “web app”.",
                "Paste the redirect URI below into “redirect uri”.",
                "Copy the client ID (shown under the app's name) and the secret into the boxes below.",
                "Fill in the User-Agent in the form Reddit asks for, for example "
                "“web:com.example.marketing:1.0 (by /u/your_reddit_name)”, and save.",
                "Ask your server administrator to set REDDIT_ENABLED to true. Until then Reddit stays "
                "switched off for every workspace.",
            ),
            client_id_label="client ID",
            client_secret_label="secret",
            extras=(
                AppField(
                    key="reddit_user_agent",
                    label="User-Agent",
                    help="Reddit requires it on every request: <platform>:<app id>:<version> "
                    "(by /u/<Reddit username>).",
                    placeholder="web:com.example.marketing:1.0 (by /u/your_reddit_name)",
                ),
            ),
            review_note=(
                "Needs Reddit's approval: Reddit requires written approval before its API is used "
                "commercially, so Reddit is switched off until your server administrator turns it on."
            ),
        ),
        ProviderSetup(
            name="hubspot",
            rank=90,
            console_label="HubSpot developer account",
            steps=(
                "In your HubSpot developer account, create a public app.",
                "Under Auth, paste the redirect URI below as the Redirect URL.",
                "Set the required scopes to exactly the permissions listed below. HubSpot refuses a sign-in "
                "when they differ.",
                "Copy the Client ID and Client secret into the boxes below and save.",
            ),
            permissions_where="Auth → Scopes (all required)",
            review_note="An unlisted app is limited to 25 installs until it is listed in HubSpot's marketplace.",
        ),
        ProviderSetup(
            name="salesforce",
            rank=100,
            console_label="Salesforce Setup",
            steps=(
                "In Salesforce Setup, open External Client App Manager and create a new External Client App.",
                "Enable OAuth and paste the redirect URI below as the Callback URL.",
                "Add the permissions listed below, require PKCE, and require the secret for the web server "
                "flow.",
                "Set the refresh token policy to “valid until revoked”.",
                "Copy the Consumer Key and Consumer Secret into the boxes below and save.",
            ),
            client_id_label="Consumer Key",
            client_secret_label="Consumer Secret",
            permissions_where="OAuth Settings → Selected OAuth Scopes",
        ),
        ProviderSetup(
            name="mailchimp",
            rank=110,
            console_label="Mailchimp",
            steps=(
                "In Mailchimp, open Profile → Extras → Registered apps and choose “Register an app”.",
                "Paste the redirect URI below as the Redirect URI.",
                "Copy the Client ID and Client Secret into the boxes below and save.",
            ),
            client_secret_label="Client Secret",
        ),
        ProviderSetup(
            name="klaviyo",
            rank=120,
            console_label="Klaviyo",
            steps=(
                "In Klaviyo, open Manage apps and create an app.",
                "Paste the redirect URI below and add the permissions listed below.",
                "Copy the Client ID and Client secret into the boxes below and save.",
            ),
            permissions_where="the app's Scopes",
        ),
        ProviderSetup(
            name="shopify",
            rank=130,
            console_label="Shopify Dev Dashboard",
            steps=(
                "In the Shopify Dev Dashboard, create an app with public or custom distribution.",
                "Paste the redirect URI below into “Allowed redirection URL(s)”.",
                "Request the access scopes listed below.",
                "Copy the Client ID and Secret into the boxes below and save.",
            ),
            client_secret_label="Secret",
            permissions_where="Access scopes",
            review_note="To serve many unrelated stores, the app must be public, which needs Shopify's review.",
        ),
        ProviderSetup(
            name="webflow",
            rank=140,
            console_label="Webflow",
            steps=(
                "In your Webflow workspace, open Workspace settings → Apps & Integrations → Develop and create "
                "an app with the Data Client building block.",
                "Paste the redirect URI below and add the permissions listed below.",
                "Copy the Client ID and Client secret into the boxes below and save.",
            ),
            permissions_where="the app's Scopes",
            review_note="Other Webflow workspaces can install the app only after Webflow approves it.",
        ),
        ProviderSetup(
            name="slack",
            rank=150,
            console_label="Slack API",
            steps=(
                "At api.slack.com/apps, choose Create New App → From scratch.",
                "Under OAuth & Permissions, paste the redirect URI below into Redirect URLs, and add the "
                "Bot Token Scopes listed below. Leave token rotation off.",
                "Under Manage Distribution, choose Activate Public Distribution.",
                "From Basic Information → App Credentials, copy the Client ID and Client Secret into the "
                "boxes below and save.",
            ),
            client_secret_label="Client Secret",
            permissions_where="OAuth & Permissions → Bot Token Scopes",
        ),
        ProviderSetup(
            name="github",
            rank=160,
            console_label="GitHub Developer settings",
            steps=(
                "In your organisation's Settings → Developer settings → GitHub Apps, choose New GitHub App.",
                "Paste the redirect URI below as the Callback URL, and tick “Request user authorization "
                "(OAuth) during installation”. Untick Webhook → Active.",
                "Under Repository permissions, set the permissions listed below. Under “Where can this "
                "GitHub App be installed?”, choose Any account.",
                "Generate a client secret and a private key.",
                "Copy the Client ID, Client secret, App ID, the app's public link name and the private key "
                "into the boxes below and save.",
            ),
            extras=(
                AppField(key="github_app_id", label="App ID", placeholder="123456"),
                AppField(
                    key="github_app_slug",
                    label="Public link name",
                    placeholder="your-app",
                    help="The last part of the app's public page, github.com/apps/<name>.",
                ),
                AppField(
                    key="github_app_private_key",
                    label="Private key",
                    secret=True,
                    multiline=True,
                    help="Open the downloaded .pem file and paste all of it.",
                    placeholder="-----BEGIN RSA PRIVATE KEY-----",
                ),
            ),
            permissions_where="Permissions & events → Repository permissions",
            app_permissions=("Contents: Read and write", "Pull requests: Read and write"),
        ),
        ProviderSetup(
            name="bitbucket",
            rank=170,
            console_label="Bitbucket workspace settings",
            steps=(
                "In your Bitbucket workspace settings, open OAuth consumers → Add consumer.",
                "Paste the redirect URI below as the Callback URL.",
                "Grant the permissions listed below.",
                "Copy the Key and Secret into the boxes below and save.",
            ),
            client_id_label="Key",
            client_secret_label="Secret",
            permissions_where="the consumer's Permissions",
            app_permissions=("Repositories: Write", "Pull requests: Write"),
        ),
    )
}


def setup_for(name: str) -> ProviderSetup | None:
    return SETUPS.get(name)


def field_index() -> dict[str, tuple[str, AppField]]:
    """``{setting name: (provider, field)}`` for every value that can be saved."""
    return {f.key: (s.name, f) for s in SETUPS.values() for f in s.fields}


__all__ = ["SETUPS", "AppField", "ProviderSetup", "field_index", "setup_for"]
