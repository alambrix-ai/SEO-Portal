"""Account lifecycle endpoints.

Sign-in here is two calls, not one: ask for a code, then present it. There is
no password endpoint anywhere in this router — no login-with-password, no
reset, no change — because there is no password. See
:mod:`app.services.auth` for why, and for the rules the codes obey.

All of the write paths are rate limited by the middleware in ``app.main``,
which matters more than usual: the rate limiter is what stops the
code-request endpoints being used to mail-bomb an address or to enumerate
which addresses have accounts.
"""
from __future__ import annotations

from fastapi import APIRouter, Request, status
from sqlalchemy import func, select

from app.api.deps import ClientIp, CurrentUserDep, DbSession
from app.core.config import settings
from app.core.rbac import ROLE_LABELS, Role, access_map
from app.models.workspace import OnboardingState, Organization, User
from app.schemas.auth import (
    AcceptInviteRequest,
    AuthResponse,
    CodeChallengeResponse,
    InvitationPreview,
    LoginRequest,
    OrganizationOut,
    RefreshRequest,
    RegisterRequest,
    RequestCodeRequest,
    SessionOut,
    TokenResponse,
    UserOut,
)
from app.schemas.common import Message
from app.services import approvals, audit, auth as auth_service
from app.services.auth import AuthResult, CodeChallenge

router = APIRouter(prefix="/auth", tags=["auth"])


# ── Serialisation ──────────────────────────────────────────────────────────
def _user_out(user: User) -> UserOut:
    role = Role(user.role)
    return UserOut(
        id=user.id,
        name=user.name,
        email=user.email,
        role=role,
        role_label=ROLE_LABELS[role],
        is_owner=user.is_owner,
        email_verified=user.email_verified,
        last_login_at=user.last_login_at,
    )


def _org_out(db: DbSession, org: Organization) -> OrganizationOut:
    seats_used = db.execute(
        select(func.count())
        .select_from(User)
        .where(User.tenant_id == org.id, User.is_active.is_(True))
    ).scalar_one()
    return OrganizationOut(
        id=org.id,
        name=org.name,
        slug=org.slug,
        primary_domain=org.primary_domain,
        global_autonomy=org.global_autonomy,
        plan_name=org.plan_name,
        seats_total=org.seats_total,
        seats_used=seats_used,
        renews_on=None,
    )


def build_session(db: DbSession, user: User, org: Organization) -> SessionOut:
    """The payload the console bootstraps from.

    The access map comes straight from the RBAC matrix, so the navigation the
    user sees and the rules the API enforces are the same table.
    """
    state = db.execute(
        select(OnboardingState).where(OnboardingState.tenant_id == org.id)
    ).scalar_one_or_none()

    from app.models.agent import AgentRecord, AgentStatus
    from app.services import connectors as connector_service
    from app.services import views

    agents = list(
        db.execute(select(AgentRecord).where(AgentRecord.tenant_id == org.id)).scalars()
    )
    from app.services import portal_features

    allowed_agents = portal_features.enabled_agent_slugs(db)
    agents = [a for a in agents if a.slug in allowed_agents]
    running = sum(1 for a in agents if a.status == AgentStatus.RUNNING.value)
    total_agents = len(agents)
    connected, total_connectors = connector_service.counts(db, tenant_id=org.id)
    counts = views.nav_counts(db, tenant_id=org.id, role=user.role)
    access = access_map(user.role)
    enabled = portal_features.enabled_modules(db)
    # Hide modules the portal has turned off (access stays for audit/API checks).
    gated_access = {
        module: level if module in enabled else "none"
        for module, level in access.items()
    }
    return SessionOut(
        user=_user_out(user),
        organization=_org_out(db, org),
        access=gated_access,
        enabled_modules=sorted(enabled),
        is_portal_admin=settings.is_portal_admin_email(user.email),
        onboarding_complete=bool(state and state.completed),
        pending_approvals=approvals.pending_count(db, tenant_id=org.id),
        running_agents=running,
        total_agents=total_agents,
        connected_connectors=connected,
        total_connectors=total_connectors,
        nav_counts=counts,
    )


def _auth_response(db: DbSession, result: AuthResult) -> AuthResponse:
    return AuthResponse(
        tokens=TokenResponse(
            access_token=result.tokens.access_token,
            refresh_token=result.tokens.refresh_token,
            expires_in=result.tokens.expires_in,
        ),
        session=build_session(db, result.user, result.organization),
    )


def _challenge(challenge: CodeChallenge, detail: str) -> CodeChallengeResponse:
    return CodeChallengeResponse(
        detail=detail, expires_in=challenge.expires_in, resend_in=challenge.resend_in
    )


# ── Step one: ask for a code ───────────────────────────────────────────────
@router.post("/request-code", response_model=CodeChallengeResponse)
def request_code(
    payload: RequestCodeRequest, db: DbSession, request: Request, ip: ClientIp
) -> CodeChallengeResponse:
    """Mail a sign-in code.

    Answers identically whether or not the address has an account, and whether
    or not a code was actually sent — a caller cannot tell from this response
    which addresses are customers. The wording says "if" for the same reason.
    """
    challenge = auth_service.request_sign_in_code(
        db,
        email=str(payload.email),
        ip_address=ip,
        user_agent=request.headers.get("user-agent", ""),
    )
    db.commit()
    return _challenge(
        challenge, "If that address has an account, a sign-in code is on its way"
    )


@router.post("/request-signup-code", response_model=CodeChallengeResponse)
def request_signup_code(
    payload: RequestCodeRequest, db: DbSession, request: Request, ip: ClientIp
) -> CodeChallengeResponse:
    """Mail a code to prove an address before a workspace is created.

    This one does report an address that is already registered — somebody
    trying to sign up needs to be told to sign in instead.
    """
    challenge = auth_service.request_sign_up_code(
        db,
        email=str(payload.email),
        ip_address=ip,
        user_agent=request.headers.get("user-agent", ""),
    )
    db.commit()
    return _challenge(challenge, "Check your inbox for the verification code")


# ── Step two: present it ───────────────────────────────────────────────────
@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
def register(
    payload: RegisterRequest, db: DbSession, request: Request, ip: ClientIp
) -> AuthResponse:
    """Create an organisation and its first Super Admin, given a valid code.

    The workspace is provisioned with the full agent fleet and connector
    catalogue, and its own data-encryption key, before this returns.
    """
    result = auth_service.register_organization(
        db,
        organization_name=payload.organization_name,
        full_name=payload.full_name,
        email=str(payload.email),
        code=payload.code,
        primary_domain=payload.primary_domain,
        user_agent=request.headers.get("user-agent", ""),
        ip_address=ip,
    )
    db.commit()
    return _auth_response(db, result)


@router.post("/login", response_model=AuthResponse)
def login(
    payload: LoginRequest, db: DbSession, request: Request, ip: ClientIp
) -> AuthResponse:
    """Redeem a sign-in code for a session."""
    result = auth_service.authenticate(
        db,
        email=str(payload.email),
        code=payload.code,
        user_agent=request.headers.get("user-agent", ""),
        ip_address=ip,
    )
    db.commit()
    return _auth_response(db, result)


@router.post("/refresh", response_model=AuthResponse)
def refresh(
    payload: RefreshRequest, db: DbSession, request: Request, ip: ClientIp
) -> AuthResponse:
    """Rotate a refresh token. Reuse of a spent token revokes the family."""
    result = auth_service.refresh_session(
        db,
        refresh_token=payload.refresh_token,
        user_agent=request.headers.get("user-agent", ""),
        ip_address=ip,
    )
    db.commit()
    return _auth_response(db, result)


@router.post("/logout", response_model=Message)
def logout(payload: RefreshRequest, db: DbSession) -> Message:
    auth_service.logout(db, refresh_token=payload.refresh_token)
    db.commit()
    return Message(detail="Signed out")


# ── Session ────────────────────────────────────────────────────────────────
@router.get("/me", response_model=SessionOut)
def me(current: CurrentUserDep, db: DbSession) -> SessionOut:
    return build_session(db, current.user, current.organization)


@router.post("/sign-out-everywhere", response_model=Message)
def sign_out_everywhere(current: CurrentUserDep, db: DbSession) -> Message:
    """Revoke every session this user has, including the one calling.

    With no password, this is the one remedy a user can apply themselves: if
    they think somebody has been in their mailbox, ending every live session
    is what actually cuts that access off. A stolen code cannot be reused —
    it was spent — but a session minted from one lasts until it is revoked.
    """
    count = auth_service.revoke_all_sessions(db, current.user, "signed_out_everywhere")
    audit.record_user_action(
        db, user=current.user, action="signed out of every device", module="admin"
    )
    db.commit()
    return Message(
        detail=(
            f"Signed out of {count} session{'s' if count != 1 else ''} — "
            "sign in again with a new code"
        )
    )


# ── Invitations ────────────────────────────────────────────────────────────
@router.get("/invitation", response_model=InvitationPreview)
def preview_invitation(token: str, db: DbSession) -> InvitationPreview:
    """Resolve an invite link so the accept screen can show its context."""
    invitation, org = auth_service.peek_invitation(db, token=token)
    role = Role(invitation.role)
    return InvitationPreview(
        organization_name=org.name,
        email=invitation.email,
        role=role,
        role_label=ROLE_LABELS[role],
        expires_at=invitation.expires_at,
    )


@router.post("/accept-invitation", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
def accept_invitation(
    payload: AcceptInviteRequest, db: DbSession, request: Request, ip: ClientIp
) -> AuthResponse:
    """Join an organisation. The invite token is the proof; no code is needed."""
    result = auth_service.accept_invitation(
        db,
        token=payload.token,
        full_name=payload.full_name,
        user_agent=request.headers.get("user-agent", ""),
        ip_address=ip,
    )
    db.commit()
    return _auth_response(db, result)


# ── Sign-in policy, for the auth screens ───────────────────────────────────
@router.get("/policy")
def policy() -> dict:
    """What the sign-in and sign-up screens should offer and say.

    ``sso_providers`` is empty on a deployment with no identity provider
    configured, and the console hides the buttons rather than showing ones
    that cannot work. Single sign-on is not implemented in this build; the
    field is here so adding it is a server change only.

    The console reads ``code_length`` to draw the right number of boxes and
    ``code_expires_in``/``resend_in`` to run its countdowns, so the screen
    always describes the server's actual rules rather than a second copy that
    can drift.
    """
    return {
        "public_signup": settings.allow_public_signup,
        "sso_providers": [],
        "auth_method": "email_code",
        "code_length": settings.login_code_length,
        "code_expires_in": settings.login_code_ttl_minutes * 60,
        "resend_in": settings.login_code_resend_seconds,
        "work_email_required": settings.block_public_email_domains,
        "roles": [
            {"value": role.value, "label": label} for role, label in ROLE_LABELS.items()
        ],
    }
