"""Application settings, loaded from the environment / `.env`.

PostgreSQL is the only supported database. The platform is multi-tenant and
internet-facing, so several settings that would be conveniences elsewhere are
enforced here instead: TLS to the database, a present master encryption key,
and a non-default secret in production.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import math
import re
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import parse_qs, quote_plus, urlsplit

from pydantic import AliasChoices, Field, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

logger = logging.getLogger(__name__)

_INSECURE_DEFAULT_SECRET = "dev-only-secret-key-do-not-use-in-production"

#: Glob patterns for model ids, optionally "<connector>:"-prefixed.
_MODEL_PATTERN = re.compile(r"[A-Za-z0-9.\-_:/*?\[\]]{1,128}")
_MAX_PATTERNS = 100
_MAX_PRICE_ENTRIES = 500
_HOSTNAME = re.compile(
    r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*"
)
#: Domains that only ever appear in examples and templates.
_PLACEHOLDER_DOMAINS = frozenset({
    "example.com", "example.org", "example.net", "yourdomain.com",
    "yourcompany.com", "your-company.com", "company.com", "domain.com",
})
#: RFC 2606 / 6761 names that can never receive real mail.
_RESERVED_TLDS = frozenset({"example", "test", "invalid", "localhost", "local"})
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "::"})
#: sslmode values that refuse an unencrypted database connection.
_STRICT_SSLMODES = frozenset({"require", "verify-ca", "verify-full"})
_MAX_LLM_PORTS = 20


def _valid_hostname(value: str, setting: str) -> str:
    """A bare DNS hostname (no scheme, port, path or wildcard), lower-cased."""
    host = value.strip().lower().rstrip(".")
    if not _HOSTNAME.fullmatch(host):
        raise ValueError(f"{setting} entries must be plain hostnames")
    return host


def _is_public_https_origin(origin: str) -> bool:
    """True for an https origin on a real hostname (not '*' or loopback)."""
    try:
        parts = urlsplit(origin)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if parts.scheme != "https" or not host or "*" in origin:
        return False
    if host in _LOCAL_HOSTS or host.endswith(".localhost"):
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


def _sslmode_of(dsn: str) -> str:
    """The ``sslmode`` a DSN asks for, or ``""`` when it names none."""
    return (parse_qs(urlsplit(dsn).query).get("sslmode") or [""])[0]


def _private_host_entries(entries: list[str], setting: str) -> list[str]:
    """Hostnames or CIDRs (canonical form) for an egress allow-list. A
    catch-all range would switch the private-address protection off, so it
    is refused outright."""
    out: list[str] = []
    for item in entries:
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError:
            out.append(_valid_hostname(item, setting))
            continue
        if network.prefixlen == 0:
            raise ValueError(f"{setting} must not contain 0.0.0.0/0 or ::/0")
        out.append(str(network))
    return out


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    def __repr_args__(self):  # noqa: ANN204
        """Never print secrets: the settings object ends up in tracebacks and
        test failure output, and this one holds keys and the database URL."""
        for name, value in super().__repr_args__():
            if (
                name
                and value
                and not name.endswith("_base_url")
                and any(
                    word in name
                    for word in ("key", "secret", "token", "password", "_url", "dsn")
                )
            ):
                yield name, "***"
            else:
                yield name, value

    # ── Application ────────────────────────────────────────────────────────
    app_env: Literal["development", "staging", "production", "test"] = "development"
    app_name: str = "Synaprank.ai"
    api_v1_prefix: str = "/api/v1"
    log_level: str = "INFO"
    public_base_url: str = "http://localhost:5173"
    #: Where the API is reachable from the internet, when it is not behind the
    #: console's own address. Used for signed image links that Instagram and
    #: Pinterest fetch. Blank means the same as public_base_url.
    public_api_base_url: str = ""

    # ── Sessions & tokens ──────────────────────────────────────────────────
    secret_key: str = _INSECURE_DEFAULT_SECRET
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30
    invitation_ttl_days: int = 14

    # ── One-time passcodes ─────────────────────────────────────────────────
    # Sign-in is a code mailed to the address, so these numbers are the whole
    # security margin of the login system. Six digits is a million
    # possibilities, which is only safe because a code lives for minutes, dies
    # after a handful of wrong guesses, and is superseded the moment another
    # is requested. Lengthening the code buys far less than shortening its
    # life or tightening the attempt cap.
    login_code_length: int = 6
    login_code_ttl_minutes: int = 10
    login_code_max_attempts: int = 5
    # Minimum gap between two codes for the same address: stops the mailbox
    # being used as an amplifier, and stops an attacker cycling codes to widen
    # the space they can guess against.
    login_code_resend_seconds: int = 60

    # ── Encryption ─────────────────────────────────────────────────────────
    # Master key-encrypting key (KEK), base64 (32 bytes). Every organisation
    # gets its own data-encryption key, wrapped with this one.
    #   python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
    master_encryption_key: str = ""
    # Older KEKs kept for decrypting rows not yet re-wrapped, as
    # "<version>:<base64key>" entries.
    master_encryption_key_retired: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    master_key_version: int = 1
    # Separate HMAC key for blind indexes (deterministic lookup of encrypted
    # columns such as email). Must NOT be the same key as the KEK.
    blind_index_key: str = ""

    # ── Transport security ─────────────────────────────────────────────────
    force_https: bool = False
    hsts_max_age_seconds: int = 31_536_000
    secure_cookies: bool = True
    cookie_domain: str = ""
    trusted_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["*"]
    )
    #: TLS ends at a platform edge (Render, Heroku-style routers) whose
    #: addresses cannot be listed in TRUSTED_PROXIES. The app then never sees
    #: the original scheme, so it skips its own http -> https redirect (which
    #: would loop) and leaves the redirect to the edge. HSTS is still sent.
    edge_tls_terminated: bool = False

    # ── CORS ───────────────────────────────────────────────────────────────
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    # ── Database (PostgreSQL) ──────────────────────────────────────────────
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "automarket"
    postgres_user: str = "automarket"
    postgres_password: str = ""
    # disable | allow | prefer | require | verify-ca | verify-full
    postgres_sslmode: str = "prefer"
    postgres_sslrootcert: str = ""
    # Full DSN wins over the discrete fields above when set.
    database_url: str = ""
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle_seconds: int = 1800
    db_echo: bool = False
    # Postgres row-level security: every session pins its organisation id so a
    # missing WHERE clause cannot leak another tenant's rows.
    db_enforce_rls: bool = True
    #: Refuse to start when the runtime database role is a superuser, has
    #: BYPASSRLS, or owns any RLS-protected table: any of those switches
    #: row-level security or the append-only audit log off. True only for a
    #: throwaway environment that has no separate app role (dev/test opt-in;
    #: never production).
    allow_privileged_db_role: bool = False

    # ── Site crawler ───────────────────────────────────────────────────────
    # Ceiling on one crawl of a customer's site. Bounded because the platform
    # is fetching somebody's production pages on a schedule.
    crawler_max_pages: int = 300

    # ── What counts as a page in a repository ──────────────────────────────
    # Deliberately broad. We do not know what a client builds with, and the
    # cost of a missing extension is the failure this list was widened after:
    # a connected repository, a working token, and an agent reporting "0 pages
    # synced" as a success because every page in it was .jsx.
    #
    # Comma-separated, and additive rather than replacing — a stack nobody
    # here has heard of is one env var away from working.
    content_extensions_extra: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Directories that mark where routing starts, for deriving a page's URL.
    route_dirs_extra: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # ── Technical SEO audit ────────────────────────────────────────────────
    # Google publishes no minimum word count; this is a working threshold, so
    # it is a setting rather than a constant and can be argued with per
    # deployment.
    seo_thin_content_words: int = 300
    # Field-data calls are quota-limited, so the audit measures a bounded
    # number of pages per run rather than the whole site every time.
    seo_vitals_pages_per_run: int = 25
    # How many page bodies one sync fetches. A listing is one call; reading a
    # body is one call per page, against the customer's own CMS or repository,
    # so the whole site is not re-read every six hours. Pages with no stored
    # body are read first, so a new site fills in over a few runs.
    seo_pages_read_per_run: int = 25

    # ── Connectors ─────────────────────────────────────────────────────────
    connector_timeout_seconds: float = 20.0
    connector_max_attempts: int = 3
    #: Private hosts or CIDRs that connectors with a customer-typed address
    #: (WordPress, Magento, a custom API...) may reach, comma-separated. For
    #: intranet systems an administrator has approved; set in the server
    #: environment only. Public addresses need no entry; cloud metadata
    #: addresses stay blocked whatever is listed, and 0.0.0.0/0 or ::/0 is
    #: refused. Listed hosts are still https only (certificate verified);
    #: the Site Crawler never uses this list.
    connector_private_hosts_allowed: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    # Ceiling on a repository walk, so a connector pointed at a monorepo root
    # does not enumerate it. Narrow the content folder instead.
    repo_max_files: int = 4000
    repo_max_depth: int = 6

    # ── Sign-in providers (platform OAuth apps) ────────────────────────────
    # One OAuth app per vendor, registered by the platform operator, so that
    # customers connect with a "Sign in with <Vendor>" button instead of
    # pasting client ids and refresh tokens. A provider is offered only when
    # both its client id and secret are set. Every provider follows the same
    # naming: OAUTH_<PROVIDER>_CLIENT_ID / OAUTH_<PROVIDER>_CLIENT_SECRET.
    # The redirect URI to register with the vendor is
    # {PUBLIC_API_BASE_URL or PUBLIC_BASE_URL}/api/v1/oauth/callback/<provider>
    # and Portal Admin → Sign-in providers shows it exactly.
    oauth_google_client_id: str = ""
    oauth_google_client_secret: str = ""
    #: The Google Ads API developer token (from the platform's own manager
    #: account API Center). A platform setting, never asked of customers;
    #: Google Ads sign-in is offered only when it is set.
    google_ads_developer_token: str = ""
    #: How long a sign-in may take between leaving for the vendor and coming
    #: back. The state is single-use and refused after this.
    oauth_state_ttl_minutes: int = 10
    #: Identifies this platform to WordPress's "Authorize application" screen.
    #: A fixed UUID, so a site lists one entry per user, not one per connect.
    wordpress_app_id: str = "4f6e0d2a-7c1b-4f0e-9a51-2d8f3b6c9e11"

    # OAuth providers: social and ads (phase 2A)
    # Same naming as above; docs/oauth-setup-social-ads.md has each vendor's
    # registration steps, products and app-review requirements.
    #: Meta (Facebook Login): Meta Ads, Facebook Pages and Instagram.
    oauth_meta_client_id: str = ""
    oauth_meta_client_secret: str = ""
    #: Graph API version for every Meta connector and the Facebook sign-in.
    #: Blank uses the version this release was built for (see
    #: app/connectors/base/meta_graph.py). Set it when Meta retires that one.
    meta_graph_api_version: str = ""
    #: LinkedIn app with the Advertising API product (LinkedIn Ads).
    oauth_linkedin_client_id: str = ""
    oauth_linkedin_client_secret: str = ""
    #: A second LinkedIn app with the Community Management API (LinkedIn
    #: Pages). LinkedIn requires that product to be alone on its app.
    oauth_linkedin_pages_client_id: str = ""
    oauth_linkedin_pages_client_secret: str = ""
    #: The Linkedin-Version header (YYYYMM) sent to LinkedIn's REST API.
    #: LinkedIn retires each version about a year after release.
    linkedin_api_version: str = Field(default="202610", pattern=r"^\d{6}$")
    #: X (Twitter) OAuth 2.0 client, type "Web App" (confidential).
    oauth_x_client_id: str = ""
    oauth_x_client_secret: str = ""
    oauth_pinterest_client_id: str = ""
    oauth_pinterest_client_secret: str = ""
    #: TikTok for Business developer app: the App ID and its Secret.
    oauth_tiktok_client_id: str = ""
    oauth_tiktok_client_secret: str = ""
    #: Microsoft Entra app registration (Microsoft Advertising).
    oauth_microsoft_client_id: str = ""
    oauth_microsoft_client_secret: str = ""
    #: The Microsoft Advertising universal developer token (Developer
    #: Settings in the platform's own Microsoft Advertising account). A
    #: platform setting, never asked of customers; Microsoft Ads sign-in is
    #: offered only when it is set.
    microsoft_ads_developer_token: str = ""
    #: The TikTok API for Business sandbox address (for a connection marked
    #: Sandbox). TikTok's developer portal shows it on the app's Sandbox Ad
    #: Account page; the default could not be confirmed in TikTok's public
    #: documentation, so it can be changed here without a code change.
    tiktok_sandbox_base_url: str = "https://sandbox-ads.tiktok.com/open_api/v1.3"
    #: Bing Webmaster Tools OAuth client (Settings → API Access → OAuth client).
    oauth_bing_webmaster_client_id: str = ""
    oauth_bing_webmaster_client_secret: str = ""

    # OAuth providers: business apps (phase 2B)
    # Same naming; docs/oauth-setup-business-apps.md has each vendor's
    # registration steps, permissions and review/distribution requirements.
    #: Slack app ("Add to Slack", OAuth v2, bot token).
    oauth_slack_client_id: str = ""
    oauth_slack_client_secret: str = ""
    #: HubSpot public app (OAuth with refresh).
    oauth_hubspot_client_id: str = ""
    oauth_hubspot_client_secret: str = ""
    #: Salesforce external client app / connected app: its consumer key and
    #: consumer secret.
    oauth_salesforce_client_id: str = ""
    oauth_salesforce_client_secret: str = ""
    #: Mailchimp registered app (Account → Extras → Registered apps).
    oauth_mailchimp_client_id: str = ""
    oauth_mailchimp_client_secret: str = ""
    #: Klaviyo OAuth app (PKCE, refresh).
    oauth_klaviyo_client_id: str = ""
    oauth_klaviyo_client_secret: str = ""
    #: Shopify app from the Dev Dashboard (public or custom distribution).
    oauth_shopify_client_id: str = ""
    oauth_shopify_client_secret: str = ""
    #: The GitHub App's client id and secret (used once, to confirm who
    #: installed it), plus what mints installation tokens: its slug (from
    #: github.com/apps/<slug>), numeric App ID and private key (PEM; "\n"
    #: escapes are accepted for single-line environment values).
    oauth_github_client_id: str = ""
    oauth_github_client_secret: str = ""
    github_app_slug: str = ""
    github_app_id: str = ""
    github_app_private_key: str = ""
    #: Bitbucket Cloud OAuth consumer: its Key and Secret.
    oauth_bitbucket_client_id: str = ""
    oauth_bitbucket_client_secret: str = ""
    #: Webflow Data Client app.
    oauth_webflow_client_id: str = ""
    oauth_webflow_client_secret: str = ""
    #: Platform-provided Google API keys. When set, workspaces connect
    #: PageSpeed Insights / Google Maps search in one click and every call is
    #: made (and, for Places, billed) on the platform's own Cloud project.
    platform_pagespeed_api_key: str = ""
    platform_google_places_api_key: str = ""

    # ── Money ──────────────────────────────────────────────────────────────
    # The customer's ad spend and cost per acquisition are reported in their
    # own currency. Grouping is a separate setting because it is not implied
    # by the symbol: "indian" gives the lakh/crore grouping (₹12,34,567),
    # "western" gives thousands (€1,234,567).
    currency_symbol: str = "₹"
    currency_grouping: str = "indian"

    # ── What a new workspace gets ──────────────────────────────────────────
    # Named here rather than in the registration code, because "Enterprise
    # Plan" and a seat count are commercial facts that change without the
    # software changing.
    # Where a freshly reset agent sends notifications. "None" so a reset
    # cannot start delivering somewhere nobody chose.
    default_notify_channel: str = "None"
    default_plan_name: str = "Enterprise Plan"
    default_seats: int = 25
    default_plan_days: int = 30
    #: Billing provider id. ``fake`` needs no keys; real providers stay off
    #: until certified (see ``app.services.billing_provider.get_provider``).
    billing_provider: str = "fake"
    #: Plan catalogue + prices as JSON in BILLING_PLAN_CATALOG. Prices must
    #: never be hard-coded in application logic — only read from this setting.
    #: Shape: {slug: {name, price_monthly, currency, seats, features, metrics}}.
    billing_plan_catalog: Annotated[dict, NoDecode] = Field(default_factory=dict)

    # ── Enterprise SSO ─────────────────────────────────────────────────────
    #: When false (default), SSO login stays fail-closed: assertion/token
    #: verification refuses every call and connections cannot be enabled for
    #: sign-in. Turn on only after IdP certs / JWKS are configured and
    #: certified. Structural/unsigned acceptance is never allowed.
    sso_login_enabled: bool = False
    #: Max age for SAML Assertion/@IssueInstant and Conditions NotOnOrAfter
    #: skew window (seconds).
    sso_clock_skew_seconds: int = Field(default=120, ge=0, le=600)
    #: How long a consumed SAML Assertion ID / OIDC ``jti``/nonce is retained
    #: for replay rejection.
    sso_replay_ttl_seconds: int = Field(default=600, ge=60, le=3600)

    # ── Registration policy ────────────────────────────────────────────────
    allow_public_signup: bool = True
    # Blocks free-mail domains from creating organisations.
    block_public_email_domains: bool = False
    # Platform operators who can open /portal and toggle catalogue features.
    # Comma-separated emails; empty means nobody is a portal admin.
    portal_admin_emails: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # ── Rate limiting (per client IP) ──────────────────────────────────────
    rate_limit_enabled: bool = True
    auth_rate_limit_per_minute: int = 10
    api_rate_limit_per_minute: int = 240
    # Wrong codes before the account itself is locked, independently of any
    # single code's attempt cap.
    max_failed_logins: int = 8
    lockout_minutes: int = 15
    #: Signed-in requests one user may make per minute, whatever their address.
    user_rate_limit_per_minute: int = Field(default=120, ge=1)
    #: Model calls one workspace may start per minute (spend ceiling).
    llm_rate_limit_per_tenant_per_minute: int = Field(default=20, ge=1)
    #: Proxies whose X-Forwarded-For / X-Forwarded-Proto are believed, as IPs
    #: or CIDRs (comma-separated). Empty means the connecting address is the
    #: client. Never 0.0.0.0/0 or ::/0: that lets any caller pick its own IP.
    trusted_proxies: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # ── Request size ───────────────────────────────────────────────────────
    #: Largest request body accepted, in bytes (1 MB). Image uploads are
    #: exempt and capped by CREATIVE_UPLOAD_MAX_BYTES instead.
    max_request_body_bytes: int = Field(default=1_048_576, ge=1024, le=104_857_600)

    # ── Outbound email ─────────────────────────────────────────────────────
    # Prefer Resend (HTTPS) on hosts that block SMTP ports — Render free does.
    # If RESEND_API_KEY is set it is used instead of SMTP.
    resend_api_key: str = ""
    resend_base_url: str = "https://api.resend.com"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    email_from: str = "no-reply@automarket.ai"
    email_from_name: str = "Synaprank.ai"
    #: Development only: with SMTP_HOST empty, also write each one-time
    #: sign-in code to the log (never the address, subject or body). Any
    #: other APP_ENV refuses to start with it on.
    dev_log_sign_in_codes: bool = False

    # ── Orchestration ──────────────────────────────────────────────────────
    scheduler_enabled: bool = True
    scheduler_tick_seconds: int = 30
    scheduler_speed: float = 1.0

    # How much work one tick may claim, in total and per workspace, so a busy
    # organisation cannot monopolise a tick.
    scheduler_max_per_tick: int = 12
    scheduler_max_per_tenant_per_tick: int = 4
    # How long a claimed agent is left alone before another worker may retry
    # it. Longer than the slowest agent run, shorter than its interval.
    scheduler_claim_lease_minutes: int = 15
    # How often connected integrations are re-probed. A network call per
    # integration per workspace, so much slower than the agent tick.
    connector_health_sweep_minutes: int = 30
    # Consecutive failures before an agent is parked for an operator to look
    # at, rather than retried forever.
    agent_max_consecutive_failures: int = 5

    celery_enabled: bool = False
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"
    #: Worker processes for the default queues.
    celery_worker_concurrency: int = Field(default=2, ge=1, le=64)
    #: Recycle a worker process after this many tasks, so a slow leak (or a
    #: browser that grew) cannot accumulate.
    celery_max_tasks_per_child: int = Field(default=50, ge=1)
    #: Worker processes for the "render" queue. Each holds a browser.
    celery_render_concurrency: int = Field(default=1, ge=1, le=16)
    #: Recycle a render worker once it uses this much resident memory (KiB).
    celery_max_memory_per_child_kb: int = Field(default=1_500_000, ge=100_000)
    #: Redis for the worker heartbeat and other non-Celery uses (redis:// or
    #: rediss://). Blank falls back to CELERY_BROKER_URL when that is Redis.
    redis_url: str = ""

    # ── Migrations ─────────────────────────────────────────────────────────
    #: Whether the container entrypoint runs `alembic upgrade head` on start.
    #: Turn off when a separate release job owns migrations.
    run_migrations: bool = True
    #: DSN for migrations (a schema-owner role). Blank uses DATABASE_URL
    #: (with a loud warning from Alembic). ``DATABASE_ADMIN_URL`` is accepted
    #: as an alias for the same setting.
    migration_database_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "MIGRATION_DATABASE_URL", "DATABASE_ADMIN_URL", "migration_database_url"
        ),
    )

    # ── LLM transport knobs (optional) ─────────────────────────────────────
    # Which model an agent writes with comes from the connector the operator
    # picks on that agent — not from these settings. The fields below remain
    # for shared HTTP limits, Anthropic effort, and spend accounting only.
    # Legacy LLM_PROVIDER / LLM_MODEL / vendor API keys are unused by agents.
    llm_provider: str = ""
    llm_model: str = ""

    # One key per vendor — unused by agent runs (connectors hold the keys).
    # Kept so older .env files and get_provider() tooling still parse.
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    grok_api_key: str = ""

    # API roots. Overridable so a deployment can route through its own
    # gateway, a regional endpoint or an audited proxy without a code change.
    anthropic_base_url: str = "https://api.anthropic.com"
    openai_base_url: str = "https://api.openai.com"
    gemini_base_url: str = "https://generativelanguage.googleapis.com"
    grok_base_url: str = "https://api.x.ai"
    anthropic_api_version: str = "2023-06-01"

    # Thinking depth and spend dial, where the provider supports one.
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    llm_max_tokens: int = 16000
    llm_timeout_seconds: float = 120.0
    llm_max_attempts: int = 3

    # SEO Assistant (platform Anthropic key — not a workspace connector).
    #: Set ASSISTANT_MODEL in .env to a model id from your Anthropic Console.
    #: No default: the assistant reports "not configured" until it is set.
    assistant_model: str = ""

    # ── Public website analysis (the marketing-site demo) ──────────────────
    # Runs on the platform's own ANTHROPIC_API_KEY, never a workspace's.
    demo_enabled: bool = True
    #: Blank uses ASSISTANT_MODEL.
    demo_model: str = ""
    #: Origins of the marketing website allowed to call the demo endpoints.
    #: Added to CORS_ORIGINS, so the site does not need the console's list.
    demo_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    #: Analyses one network address may start per day, whatever device id it
    #: sends — the device id lives in the visitor's browser and can be reset.
    demo_max_per_ip_per_day: int = 3
    #: Analyses across all visitors per day, as a spend ceiling.
    demo_max_per_day: int = 300
    #: Analyses running at once in this process.
    demo_max_concurrent: int = 3
    #: Optional Google API key for PageSpeed Insights. Without one Google's
    #: shared anonymous quota is used, which is small and often exhausted.
    demo_pagespeed_api_key: str = ""
    #: In-portal site analyses one workspace may start per day (a plan can
    #: lower or raise it through entitlements.feature_policy).
    site_analysis_max_per_day: int = 10
    #: Audit history (Technical SEO, SEO & AEO scores, brand profile
    #: versions): snapshots kept per workspace per kind. The oldest beyond
    #: this are removed when a new one is recorded.
    audit_history_max_per_kind: int = 365

    # Cost attribution, in currency units per million tokens. Left at zero,
    # run costs read zero and a warning says so — rather than a built-in price
    # table that is wrong the week a vendor changes its rates, silently, in a
    # number the console reports as spend.
    llm_price_input_per_mtok: float = 0.0
    llm_price_output_per_mtok: float = 0.0
    #: Per-model prices, as JSON in LLM_PRICE_TABLE:
    #:   {"<connector>:<model pattern>": [1.25, 10.0]}
    #: Keys are "<connector>:<model pattern>", values are [input, output] in
    #: currency units per million tokens. Empty uses the two flat prices above.
    llm_price_table: Annotated[dict[str, list[float]], NoDecode] = Field(
        default_factory=dict
    )

    # ── Self-hosted models (Ollama, vLLM) ──────────────────────────────────
    #: Private hosts or CIDRs a self-hosted model connector may reach
    #: (comma-separated). Public addresses need no entry; cloud metadata
    #: addresses stay blocked whatever is listed here.
    self_hosted_llm_allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    self_hosted_llm_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    #: Plain http to a self-hosted model server. Off by default; even when on,
    #: only for addresses inside a listed private CIDR (never a hostname
    #: entry alone), and no credentials are sent over it.
    self_hosted_llm_allow_plain_http: bool = False
    #: Ports a self-hosted model server may listen on (comma-separated).
    self_hosted_llm_allowed_ports: Annotated[list[int], NoDecode] = Field(
        default_factory=lambda: [443, 8000, 11434]
    )

    # ── Creative Studio ────────────────────────────────────────────────────
    creative_enabled: bool = True
    #: Model ids Creative Studio may write with, as glob patterns. A pattern
    #: may be prefixed with "<connector>:" to apply to one connector only.
    #: The deny list wins over the allow list. Model ids never appear in
    #: code, so all four lists are empty here and an empty allow list allows
    #: nothing: Creative Studio offers no model until these are configured.
    #: .env.example carries the recommended values.
    creative_allowed_text_models: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    creative_denied_text_models: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    creative_allowed_image_models: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    creative_denied_image_models: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    # Monthly quotas per workspace (a plan may override them). 0 = no cap.
    creative_jobs_per_month: int = Field(default=100, ge=0)
    creative_images_per_month: int = Field(default=200, ge=0)
    creative_tokens_per_month: int = Field(default=0, ge=0)
    # Per-job ceilings.
    creative_max_formats_per_job: int = Field(default=8, ge=1, le=50)
    creative_max_revisions: int = Field(default=5, ge=0, le=50)
    creative_max_images_per_job: int = Field(default=4, ge=0, le=20)
    creative_max_tokens_per_call: int = Field(default=2000, ge=1, le=64_000)
    # Uploads and storage.
    creative_upload_max_bytes: int = Field(default=5_242_880, ge=1)
    creative_upload_max_pixels: int = Field(default=40_000_000, ge=1)
    creative_svg_max_bytes: int = Field(default=1_048_576, ge=1)
    creative_storage_max_bytes_per_tenant: int = Field(default=2 * 1024**3, ge=0)
    # Rendering.
    creative_renderer: Literal["chromium", "pillow"] = "chromium"
    #: Chromium's own sandbox needs user namespaces, which most containers do
    #: not grant. Off by default; the container itself is locked down instead.
    #: Production refuses it off unless CELERY_ENABLED is true (renders leave
    #: this process). CREATIVE_RENDER_ISOLATED does not count without Celery.
    creative_chromium_sandbox: bool = False
    #: The operator confirms Chromium runs only in a dedicated, locked-down
    #: render worker (own network, no capabilities, read-only root). Compose
    #: sets it on render-worker only. Production treats it as a valid excuse
    #: for unsandboxed Chromium only together with CELERY_ENABLED.
    creative_render_isolated: bool = False
    creative_render_timeout_seconds: int = Field(default=30, ge=1, le=300)
    #: Whole Creative Studio job (several formats, each rendered and checked).
    creative_render_soft_time_limit: int = Field(default=600, ge=1)
    creative_render_time_limit: int = Field(default=660, ge=1)
    creative_max_concurrent_renders: int = Field(default=2, ge=1)
    creative_job_max_concurrent: int = Field(default=2, ge=1)
    #: Google Display rejects banners over 150 KB.
    creative_banner_max_kb: int = Field(default=150, ge=1)

    # ── Stored files and fetched images ────────────────────────────────────
    #: "s3" is not implemented yet; production refuses it.
    storage_backend: Literal["postgres", "s3"] = "postgres"
    #: Hosts an image may be downloaded from when a model returns a link
    #: instead of the image itself (comma-separated hostnames). Empty turns
    #: link downloads off entirely.
    image_fetch_allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    image_fetch_max_bytes: int = Field(default=20_000_000, ge=1)

    # ── Retention (days) ───────────────────────────────────────────────────
    retention_login_codes_days: int = Field(default=7, ge=1)
    retention_refresh_tokens_days: int = Field(default=30, ge=1)
    retention_demo_analyses_days: int = Field(default=90, ge=1)

    # ── Audit export ───────────────────────────────────────────────────────
    audit_export_max_rows: int = Field(default=50_000, ge=1, le=1_000_000)
    audit_export_max_days: int = Field(default=366, ge=1, le=3660)

    # ── Reddit ─────────────────────────────────────────────────────────────
    #: Reddit requires written approval for commercial API use, so the
    #: connector is off until an operator turns it on.
    reddit_enabled: bool = False
    oauth_reddit_client_id: str = ""
    oauth_reddit_client_secret: str = ""
    #: "<platform>:<app id>:<version> (by /u/<username>)", as Reddit requires.
    reddit_user_agent: str = Field(default="", max_length=256)
    reddit_min_minutes_between_posts: int = Field(default=10, ge=1)

    # ── Social posting and metrics ─────────────────────────────────────────
    social_metrics_interval_hours: int = Field(default=6, ge=1)
    social_metrics_lookback_days: int = Field(default=30, ge=1, le=366)
    social_publish_max_attempts: int = Field(default=3, ge=1, le=10)
    social_publish_retry_minutes: int = Field(default=15, ge=1)
    #: X bills every read, so one metrics run reads at most this many posts.
    x_metrics_max_reads_per_run: int = Field(default=100, ge=0)
    metrics_backfill_days: int = Field(default=90, ge=0, le=366)
    #: Social posts one workspace may publish in any 24 hours. A workspace's
    #: own setting (org.settings["social_daily_post_cap"]) overrides it.
    social_daily_post_cap: int = Field(default=25, ge=1, le=500)
    #: How long the public link to a published post's picture stays valid
    #: (Reddit link posts, some Pinterest pins).
    published_link_days: int = Field(default=365, ge=1, le=5 * 365)
    insights_max_range_days: int = Field(default=366, ge=1, le=3660)

    # ── History retention ──────────────────────────────────────────────────
    history_keep_versions_default: int = Field(default=365, ge=1)
    #: 0 keeps versions for ever.
    history_keep_days_default: int = Field(default=0, ge=0)
    history_min_versions_kept: int = Field(default=5, ge=1)

    # ── Validators ─────────────────────────────────────────────────────────
    @field_validator(
        "cors_origins",
        "demo_allowed_origins",
        "trusted_hosts",
        "master_encryption_key_retired",
        "content_extensions_extra",
        "route_dirs_extra",
        "portal_admin_emails",
        "trusted_proxies",
        "self_hosted_llm_allowed_hosts",
        "self_hosted_llm_allowed_ports",
        "connector_private_hosts_allowed",
        "image_fetch_allowed_hosts",
        "creative_allowed_text_models",
        "creative_denied_text_models",
        "creative_allowed_image_models",
        "creative_denied_image_models",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("trusted_proxies")
    @classmethod
    def _parse_proxies(cls, v: list[str]) -> list[str]:
        """Each entry must be an IP or CIDR; stored in canonical CIDR form."""
        out: list[str] = []
        for item in v:
            try:
                out.append(str(ipaddress.ip_network(item, strict=False)))
            except ValueError:
                raise ValueError(
                    "TRUSTED_PROXIES entries must be IP addresses or CIDR ranges"
                ) from None
        return out

    @field_validator("self_hosted_llm_allowed_hosts")
    @classmethod
    def _parse_llm_hosts(cls, v: list[str]) -> list[str]:
        return _private_host_entries(v, "SELF_HOSTED_LLM_ALLOWED_HOSTS")

    @field_validator("self_hosted_llm_allowed_ports")
    @classmethod
    def _parse_llm_ports(cls, v: list[int]) -> list[int]:
        """TCP ports, 1-65535, a short list with no repeats."""
        if not v:
            raise ValueError("SELF_HOSTED_LLM_ALLOWED_PORTS must list at least one port")
        if len(v) > _MAX_LLM_PORTS:
            raise ValueError(
                f"SELF_HOSTED_LLM_ALLOWED_PORTS may list at most {_MAX_LLM_PORTS} ports"
            )
        if any(not 1 <= port <= 65535 for port in v):
            raise ValueError("SELF_HOSTED_LLM_ALLOWED_PORTS entries must be 1-65535")
        return list(dict.fromkeys(v))

    @field_validator("connector_private_hosts_allowed")
    @classmethod
    def _parse_connector_hosts(cls, v: list[str]) -> list[str]:
        return _private_host_entries(v, "CONNECTOR_PRIVATE_HOSTS_ALLOWED")

    @field_validator("redis_url")
    @classmethod
    def _redis_scheme(cls, v: str) -> str:
        v = v.strip()
        if v and not v.startswith(("redis://", "rediss://")):
            raise ValueError("REDIS_URL must start with redis:// or rediss://")
        return v

    @field_validator("image_fetch_allowed_hosts")
    @classmethod
    def _parse_image_hosts(cls, v: list[str]) -> list[str]:
        return [_valid_hostname(item, "IMAGE_FETCH_ALLOWED_HOSTS") for item in v]

    @field_validator(
        "creative_allowed_text_models",
        "creative_denied_text_models",
        "creative_allowed_image_models",
        "creative_denied_image_models",
    )
    @classmethod
    def _model_patterns(cls, v: list[str]) -> list[str]:
        if len(v) > _MAX_PATTERNS:
            raise ValueError(f"at most {_MAX_PATTERNS} model patterns are allowed")
        for item in v:
            if not _MODEL_PATTERN.fullmatch(item):
                raise ValueError(
                    "model patterns may use letters, digits, '.', '-', '_', "
                    "':', '/', '*', '?' and [ ] only, up to 128 characters"
                )
        return v

    @field_validator("billing_plan_catalog", mode="before")
    @classmethod
    def _parse_billing_catalog(cls, v: object) -> object:
        if isinstance(v, str):
            if not v.strip():
                return {}
            try:
                v = json.loads(v)
            except ValueError:
                raise ValueError("BILLING_PLAN_CATALOG must be valid JSON") from None
        if not isinstance(v, dict):
            raise ValueError("BILLING_PLAN_CATALOG must be a JSON object")
        if len(v) > 50:
            raise ValueError("BILLING_PLAN_CATALOG may hold at most 50 plans")
        for slug, plan in v.items():
            if not isinstance(slug, str) or not slug.strip() or len(slug) > 64:
                raise ValueError("BILLING_PLAN_CATALOG keys must be plan slugs")
            if not isinstance(plan, dict):
                raise ValueError("BILLING_PLAN_CATALOG values must be objects")
            price = plan.get("price_monthly", 0)
            if not isinstance(price, (int, float)) or isinstance(price, bool) or price < 0:
                raise ValueError("price_monthly must be a non-negative number")
        return v

    @field_validator("llm_price_table", mode="before")
    @classmethod
    def _parse_price_table(cls, v: object) -> object:
        """LLM_PRICE_TABLE is JSON. Anything else refuses to start, rather
        than silently reporting every run as free."""
        if isinstance(v, str):
            if not v.strip():
                return {}
            try:
                v = json.loads(v)
            except ValueError:
                raise ValueError("LLM_PRICE_TABLE must be valid JSON") from None
        if not isinstance(v, dict):
            raise ValueError("LLM_PRICE_TABLE must be a JSON object")
        if len(v) > _MAX_PRICE_ENTRIES:
            raise ValueError(
                f"LLM_PRICE_TABLE may hold at most {_MAX_PRICE_ENTRIES} entries"
            )
        for key, prices in v.items():
            if not isinstance(key, str) or not _MODEL_PATTERN.fullmatch(key):
                raise ValueError(
                    "LLM_PRICE_TABLE keys must be '<connector>:<model pattern>'"
                )
            if (
                not isinstance(prices, list)
                or len(prices) != 2
                or not all(
                    isinstance(p, (int, float))
                    and not isinstance(p, bool)
                    and math.isfinite(p)
                    and p >= 0
                    for p in prices
                )
            ):
                raise ValueError(
                    "LLM_PRICE_TABLE values must be [input price, output price], "
                    "two non-negative numbers"
                )
        return v

    @field_validator("portal_admin_emails")
    @classmethod
    def _lower_portal_emails(cls, v: list[str]) -> list[str]:
        return [item.lower() for item in v]

    @field_validator("content_extensions_extra", "route_dirs_extra")
    @classmethod
    def _lowercase(cls, v: list[str]) -> list[str]:
        """File extensions and directory names are matched case-insensitively."""
        return [item.lower() for item in v]

    @field_validator("database_url", "migration_database_url")
    @classmethod
    def _require_postgres(cls, v: str) -> str:
        if not v:
            return v
        # Neon and many hosts hand out postgresql:// or postgres://. SQLAlchemy
        # needs an explicit psycopg3 driver for this codebase.
        if v.startswith("postgres://"):
            v = "postgresql+psycopg://" + v[len("postgres://") :]
        elif v.startswith("postgresql://"):
            v = "postgresql+psycopg://" + v[len("postgresql://") :]
        scheme = urlsplit(v).scheme
        if not scheme.startswith("postgresql"):
            raise ValueError(
                "DATABASE_URL must be a PostgreSQL DSN "
                "(postgresql+psycopg://user:pass@host:5432/db)"
            )
        return v

    @model_validator(mode="after")
    def _production_guardrails(self) -> Settings:
        if self.app_env != "production":
            return self
        problems: list[str] = []
        if self.secret_key == _INSECURE_DEFAULT_SECRET or len(self.secret_key) < 32:
            problems.append("SECRET_KEY must be set to at least 32 random characters")
        if not self.master_encryption_key:
            problems.append("MASTER_ENCRYPTION_KEY must be set")
        if not self.blind_index_key:
            problems.append("BLIND_INDEX_KEY must be set")
        if self.effective_sslmode not in _STRICT_SSLMODES:
            problems.append(
                "POSTGRES_SSLMODE (or sslmode in DATABASE_URL) must be require, "
                "verify-ca or verify-full"
            )
        if (
            self.migration_database_url
            and _sslmode_of(self.migration_database_url) not in _STRICT_SSLMODES
        ):
            problems.append(
                "MIGRATION_DATABASE_URL must end in ?sslmode=require (or "
                "verify-ca / verify-full)"
            )
        hosts = [h.strip() for h in self.trusted_hosts]
        # Empty (TRUSTED_HOSTS='') is the same as missing: the host check is
        # not installed, so any Host header is accepted. Require at least one
        # concrete name; '*' is already refused.
        if "*" in hosts or not any(hosts):
            problems.append("TRUSTED_HOSTS must list the real hostnames")
        if not self.force_https:
            problems.append(
                "FORCE_HTTPS must be true so sessions and credentials are not "
                "sent in the clear"
            )
        if not self.resend_api_key:
            problems.append(
                "RESEND_API_KEY must be set in production: sign-in is a "
                "one-time code sent by email, and SMTP ports are blocked on "
                "Render (APP_ENV=development uses SMTP locally instead)"
            )
        if self.public_base_url.startswith("http://"):
            problems.append(
                "PUBLIC_BASE_URL must be https — it is the base for the "
                "invitation links sent by email"
            )
        if self.login_code_ttl_minutes > 30:
            problems.append(
                "LOGIN_CODE_TTL_MINUTES must be 30 or less — a mailed code is "
                "a live credential for as long as it is valid"
            )
        if self.login_code_max_attempts > 10:
            problems.append(
                "LOGIN_CODE_MAX_ATTEMPTS must be 10 or less, or a six-digit "
                "code becomes guessable"
            )
        if not self.rate_limit_enabled:
            problems.append(
                "RATE_LIMIT_ENABLED must be true on an internet-facing deployment"
            )
        if self.blind_index_key and self.blind_index_key == self.master_encryption_key:
            problems.append(
                "BLIND_INDEX_KEY must differ from MASTER_ENCRYPTION_KEY, or "
                "leaking one reveals what the other protects"
            )
        for name, origins in (
            ("CORS_ORIGINS", self.cors_origins),
            ("DEMO_ALLOWED_ORIGINS", self.demo_allowed_origins),
        ):
            if any(not _is_public_https_origin(origin) for origin in origins):
                problems.append(
                    f"{name} must list only https origins with real hostnames "
                    "(no '*', no localhost)"
                )
        if not self.db_enforce_rls:
            problems.append(
                "DB_ENFORCE_RLS must be true: row-level security is what keeps "
                "one organisation's data out of another's"
            )
        if any(
            email.rpartition("@")[2] in _PLACEHOLDER_DOMAINS
            or email.rpartition("@")[2].rpartition(".")[2] in _RESERVED_TLDS
            for email in self.portal_admin_emails
        ):
            problems.append(
                "PORTAL_ADMIN_EMAILS contains a placeholder address; list the "
                "real operators"
            )
        if self.storage_backend == "s3":
            problems.append(
                "STORAGE_BACKEND=s3 is not available yet; use postgres"
            )
        if any(
            ipaddress.ip_network(cidr).prefixlen == 0 for cidr in self.trusted_proxies
        ):
            problems.append(
                "TRUSTED_PROXIES must not contain 0.0.0.0/0 or ::/0: that lets "
                "any caller choose the address it is rate-limited by"
            )
        if self.force_https and not self.trusted_proxies and not self.edge_tls_terminated:
            problems.append(
                "FORCE_HTTPS needs TRUSTED_PROXIES (the proxy that terminates "
                "TLS) or EDGE_TLS_TERMINATED=true: otherwise every request "
                "looks like plain http and is redirected in a loop"
            )
        if (
            self.creative_renderer == "chromium"
            and not self.creative_chromium_sandbox
            and not self.celery_enabled
        ):
            problems.append(
                "CREATIVE_CHROMIUM_SANDBOX=false needs CELERY_ENABLED=true so "
                "renders leave this process; CREATIVE_RENDER_ISOLATED=true does "
                "not count without Celery. Or turn the sandbox on, or use "
                "CREATIVE_RENDERER=pillow"
            )
        if not self.trusted_proxies:
            logger.warning(
                "TRUSTED_PROXIES is empty: X-Forwarded-For is ignored and the "
                "connecting address is treated as the client"
            )
        if self.allow_privileged_db_role:
            logger.warning(
                "ALLOW_PRIVILEGED_DB_ROLE is true: a privileged database role "
                "is permitted (superuser, BYPASSRLS, or table owner). Never "
                "enable this for real customer data."
            )
        if self.self_hosted_llm_allow_plain_http:
            logger.warning(
                "SELF_HOSTED_LLM_ALLOW_PLAIN_HTTP is true: private self-hosted "
                "model servers may be reached without TLS"
            )
        if problems:
            raise ValueError(
                "Refusing to start in production:\n  - " + "\n  - ".join(problems)
            )
        return self

    @model_validator(mode="after")
    def _dev_only_switches(self) -> Settings:
        """Developer conveniences that would leak credentials anywhere else."""
        if self.dev_log_sign_in_codes and self.app_env != "development":
            raise ValueError(
                "DEV_LOG_SIGN_IN_CODES may only be true with APP_ENV=development: "
                "it writes one-time sign-in codes to the log"
            )
        return self

    @model_validator(mode="after")
    def _render_limits(self) -> Settings:
        if self.creative_render_soft_time_limit >= self.creative_render_time_limit:
            raise ValueError(
                "CREATIVE_RENDER_SOFT_TIME_LIMIT must be below "
                "CREATIVE_RENDER_TIME_LIMIT"
            )
        return self

    # ── Derived ────────────────────────────────────────────────────────────
    @property
    def active_llm_key(self) -> str:
        """The key for the selected provider, whichever that is."""
        return {
            "anthropic": self.anthropic_api_key,
            "openai": self.openai_api_key,
            "gemini": self.gemini_api_key,
            "grok": self.grok_api_key,
        }.get(self.llm_provider, "")

    @property
    def effective_sslmode(self) -> str:
        """The sslmode the engine connects with: DATABASE_URL's own, else
        POSTGRES_SSLMODE (the engine passes it when the URL names none)."""
        if self.database_url:
            return _sslmode_of(self.database_url) or self.postgres_sslmode
        return self.postgres_sslmode

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sqlalchemy_url(self) -> str:
        """Assemble the DSN when discrete POSTGRES_* fields were used."""
        if self.database_url:
            return self.database_url
        auth = quote_plus(self.postgres_user)
        if self.postgres_password:
            auth = f"{auth}:{quote_plus(self.postgres_password)}"
        return (
            f"postgresql+psycopg://{auth}@{self.postgres_host}:{self.postgres_port}"
            f"/{self.postgres_db}?sslmode={self.postgres_sslmode}"
        )

    @property
    def public_api_root(self) -> str:
        """Where the API is reachable from a browser, without a trailing slash."""
        return (self.public_api_base_url or self.public_base_url).rstrip("/")

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    def is_portal_admin_email(self, email: str) -> bool:
        """True when this address is on PORTAL_ADMIN_EMAILS."""
        address = (email or "").strip().lower()
        if not address or not self.portal_admin_emails:
            return False
        return address in self.portal_admin_emails


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
