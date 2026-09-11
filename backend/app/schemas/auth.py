"""Request and response models for the account lifecycle."""
from __future__ import annotations

from datetime import datetime

from pydantic import EmailStr, Field, field_validator

from app.core.rbac import Role
from app.schemas.common import ApiModel

# A code is digits, but it arrives however the user's mail client and keyboard
# left it — spaced, hyphenated, padded. The field accepts that and the service
# normalises it; rejecting "123 456" here would be a validation error the user
# cannot understand from a form that shows six boxes.
CodeField = Field(min_length=4, max_length=32)


# ── Requests ───────────────────────────────────────────────────────────────
class RequestCodeRequest(ApiModel):
    """Ask for a one-time code, to sign in or to start a sign-up."""

    email: EmailStr


class RegisterRequest(ApiModel):
    organization_name: str = Field(min_length=2, max_length=160)
    full_name: str = Field(min_length=2, max_length=160)
    email: EmailStr
    # The code mailed by POST /auth/request-signup-code. Sign-up is two steps
    # precisely so that no organisation can exist behind an unproven address.
    code: str = CodeField
    primary_domain: str = Field(default="", max_length=255)
    accept_terms: bool = True

    @field_validator("primary_domain")
    @classmethod
    def _clean_domain(cls, value: str) -> str:
        cleaned = value.strip().lower()
        for prefix in ("https://", "http://", "www."):
            cleaned = cleaned.removeprefix(prefix)
        return cleaned.rstrip("/")


class LoginRequest(ApiModel):
    """Second step of signing in: the address, and the code sent to it."""

    email: EmailStr
    code: str = CodeField


class RefreshRequest(ApiModel):
    refresh_token: str


class InviteRequest(ApiModel):
    email: EmailStr
    role: Role


class AcceptInviteRequest(ApiModel):
    token: str
    full_name: str = Field(min_length=2, max_length=160)


# ── Responses ──────────────────────────────────────────────────────────────
class CodeChallengeResponse(ApiModel):
    """The answer to a code request.

    Carries no code, and says nothing about whether the address is registered:
    the response is byte-for-byte the same for an address with an account and
    one without, so it cannot be used to enumerate customers.
    """

    detail: str
    # Seconds a code remains valid once sent.
    expires_in: int
    # Seconds before another code may be requested for this address.
    resend_in: int


class TokenResponse(ApiModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int


class OrganizationOut(ApiModel):
    id: str
    name: str
    slug: str
    primary_domain: str
    global_autonomy: bool
    plan_name: str
    seats_total: int
    seats_used: int = 0
    renews_on: datetime | None = None


class UserOut(ApiModel):
    id: str
    name: str
    email: str
    role: Role
    role_label: str
    is_owner: bool
    # When the address was proven — at sign-up by a mailed code, or by opening
    # an invitation. It is the credential, so the console shows it.
    email_verified: bool
    last_login_at: datetime | None = None


class SessionOut(ApiModel):
    """Everything the console needs on load: identity, org, and access map."""

    user: UserOut
    organization: OrganizationOut
    # module -> full | view | none, straight from the RBAC matrix.
    access: dict[str, str]
    #: Modules still on after Portal Admin feature flags (intersection with access).
    enabled_modules: list[str] = []
    #: True when the caller's email is on PORTAL_ADMIN_EMAILS.
    is_portal_admin: bool = False
    onboarding_complete: bool
    pending_approvals: int
    # Counts for the navigation badges. Carried on the session rather than
    # fetched per screen: the sidebar is on every page, and it should not
    # need a request of its own to render a number.
    running_agents: int = 0
    total_agents: int = 0
    connected_connectors: int = 0
    total_connectors: int = 0
    #: route -> how much is waiting on that screen. Keyed by route because
    #: SEO & AEO and Technical SEO share one module and count different
    #: things. Absent or zero means the badge is not drawn.
    nav_counts: dict[str, int] = {}


class AuthResponse(ApiModel):
    tokens: TokenResponse
    session: SessionOut


class InvitationOut(ApiModel):
    id: str
    email: str
    role: Role
    role_label: str
    expires_at: datetime
    accepted_at: datetime | None = None


class InvitationPreview(ApiModel):
    """What the accept-invite screen shows before an account exists."""

    organization_name: str
    email: str
    role: Role
    role_label: str
    expires_at: datetime
