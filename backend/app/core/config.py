"""Application settings, loaded from the environment / `.env`.

PostgreSQL is the only supported database. The platform is multi-tenant and
internet-facing, so several settings that would be conveniences elsewhere are
enforced here instead: TLS to the database, a present master encryption key,
and a non-default secret in production.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_INSECURE_DEFAULT_SECRET = "dev-only-secret-key-do-not-use-in-production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Application ────────────────────────────────────────────────────────
    app_env: Literal["development", "staging", "production", "test"] = "development"
    app_name: str = "AutoMarket AI"
    api_v1_prefix: str = "/api/v1"
    log_level: str = "INFO"
    public_base_url: str = "http://localhost:5173"

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
    # Ceiling on a repository walk, so a connector pointed at a monorepo root
    # does not enumerate it. Narrow the content folder instead.
    repo_max_files: int = 4000
    repo_max_depth: int = 6

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
    email_from_name: str = "AutoMarket AI"

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

    # Cost attribution, in currency units per million tokens. Left at zero,
    # run costs read zero and a warning says so — rather than a built-in price
    # table that is wrong the week a vendor changes its rates, silently, in a
    # number the console reports as spend.
    llm_price_input_per_mtok: float = 0.0
    llm_price_output_per_mtok: float = 0.0

    # ── Validators ─────────────────────────────────────────────────────────
    @field_validator(
        "cors_origins",
        "trusted_hosts",
        "master_encryption_key_retired",
        "content_extensions_extra",
        "route_dirs_extra",
        "portal_admin_emails",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
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

    @field_validator("database_url")
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
        if self.effective_sslmode in ("disable", "allow"):
            problems.append("POSTGRES_SSLMODE must be 'require' or stricter")
        if "*" in self.trusted_hosts:
            problems.append("TRUSTED_HOSTS must list the real hostnames")
        if not self.force_https:
            problems.append(
                "FORCE_HTTPS must be true so sessions and credentials are not "
                "sent in the clear"
            )
        if not self.smtp_host and not self.resend_api_key:
            problems.append(
                "SMTP_HOST or RESEND_API_KEY must be set: sign-in is a "
                "one-time code sent by email, so without mail nobody can "
                "log in (prefer RESEND_API_KEY on Render — SMTP ports are blocked)"
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
        if problems:
            raise ValueError(
                "Refusing to start in production:\n  - " + "\n  - ".join(problems)
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
        if self.database_url:
            from urllib.parse import parse_qs

            q = parse_qs(urlsplit(self.database_url).query)
            return (q.get("sslmode") or [self.postgres_sslmode])[0]
        return self.postgres_sslmode

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sqlalchemy_url(self) -> str:
        """Assemble the DSN when discrete POSTGRES_* fields were used."""
        if self.database_url:
            return self.database_url
        from urllib.parse import quote_plus

        auth = quote_plus(self.postgres_user)
        if self.postgres_password:
            auth = f"{auth}:{quote_plus(self.postgres_password)}"
        return (
            f"postgresql+psycopg://{auth}@{self.postgres_host}:{self.postgres_port}"
            f"/{self.postgres_db}?sslmode={self.postgres_sslmode}"
        )

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
