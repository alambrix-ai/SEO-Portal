"""FastAPI dependencies: authentication, tenant pinning, and RBAC guards.

Two things happen for every authenticated request, in this order:

1. the bearer token is verified and the user loaded;
2. the transaction is **pinned to that user's organisation**, so every
   subsequent query is inside a row-level-security boundary.

Because the pin happens here and nowhere else, no endpoint can accidentally
read across tenants — even one that forgets its own ``WHERE tenant_id``.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.exceptions import AuthError, ForbiddenError
from app.core.rbac import ROLE_LABELS, Access, Module, Role, access_for
from app.core.security import decode_access_token
from app.core.config import settings
from app.db.session import bind_tenant, get_db
from app.models.workspace import Organization, User
from app.services.encryption import OrgCipher

# auto_error=False so a missing header raises our own AuthError with a
# consistent body, rather than FastAPI's default shape.
bearer_scheme = HTTPBearer(auto_error=False, scheme_name="Bearer")

DbSession = Annotated[Session, Depends(get_db)]


def client_ip(request: Request, x_forwarded_for: str | None = Header(default=None)) -> str:
    """Best-effort client address for audit rows and rate limiting.

    ``X-Forwarded-For`` is only trusted for its left-most entry, and only
    because this service is expected to sit behind a proxy that sets it.
    """
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else ""


ClientIp = Annotated[str, Depends(client_ip)]


class CurrentUser:
    """The authenticated caller, their organisation, and its cipher."""

    __slots__ = ("user", "organization", "_cipher", "claims", "_enabled_modules")

    def __init__(
        self,
        user: User,
        organization: Organization,
        claims: dict,
        *,
        enabled_modules: frozenset[str] | None = None,
    ) -> None:
        self.user = user
        self.organization = organization
        self.claims = claims
        self._cipher: OrgCipher | None = None
        # Portal feature flags for modules; None means "not loaded, allow RBAC only".
        self._enabled_modules = enabled_modules

    @property
    def tenant_id(self) -> str:
        return self.organization.id

    @property
    def role(self) -> Role:
        return Role(self.user.role)

    @property
    def role_label(self) -> str:
        return ROLE_LABELS[self.role]

    @property
    def cipher(self) -> OrgCipher:
        """Unwrapped organisation key, built once per request."""
        if self._cipher is None:
            self._cipher = OrgCipher.for_org(self.organization)
        return self._cipher

    # ── Access helpers ─────────────────────────────────────────────────────
    def access(self, module: Module | str) -> Access:
        key = module.value if isinstance(module, Module) else str(module)
        if self._enabled_modules is not None and key not in self._enabled_modules:
            return Access.NONE
        return access_for(self.role, module)

    def can_view(self, module: Module | str) -> bool:
        return self.access(module) in (Access.FULL, Access.VIEW)

    def can_write(self, module: Module | str) -> bool:
        return self.access(module) is Access.FULL

    def require_view(self, module: Module | str) -> None:
        if not self.can_view(module):
            raise ForbiddenError("Restricted for your role")

    def require_write(self, module: Module | str) -> None:
        if not self.can_view(module):
            raise ForbiddenError("Restricted for your role")
        if not self.can_write(module):
            # The exact wording the console shows as a toast.
            raise ForbiddenError("View-only access for your role")

    @property
    def is_portal_admin(self) -> bool:
        return settings.is_portal_admin_email(self.user.email)

    def require_portal_admin(self) -> None:
        if not self.is_portal_admin:
            raise ForbiddenError("Portal administration is restricted")


def get_current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> CurrentUser:
    if credentials is None or not credentials.credentials:
        raise AuthError("Sign in to continue")

    claims = decode_access_token(credentials.credentials)
    user_id = claims.get("sub")
    tenant_id = claims.get("tid")
    if not user_id or not tenant_id:
        raise AuthError("Malformed session token")

    # Pin the organisation before loading anything, so even these first two
    # reads happen inside the RLS boundary.
    bind_tenant(db, tenant_id)

    user = db.get(User, user_id)
    if user is None or not user.is_active or user.tenant_id != tenant_id:
        raise AuthError("This account is no longer active")

    # A role change revokes live sessions, but a token minted seconds before
    # could still carry the old role; the database is the authority.
    if claims.get("role") != user.role:
        raise AuthError("Your access level changed — sign in again")

    organization = db.get(Organization, tenant_id)
    if organization is None or not organization.is_active:
        raise AuthError("This workspace is no longer active")

    from app.services import portal_features

    return CurrentUser(
        user=user,
        organization=organization,
        claims=claims,
        enabled_modules=portal_features.enabled_modules(db),
    )


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


def require_portal_admin(current: CurrentUserDep) -> CurrentUser:
    """Platform operators listed in PORTAL_ADMIN_EMAILS only."""
    current.require_portal_admin()
    return current


PortalAdminDep = Annotated[CurrentUser, Depends(require_portal_admin)]


def require_module(module: Module, *, write: bool = False) -> Callable[..., CurrentUser]:
    """Dependency factory guarding one module.

    Usage::

        @router.get("/", dependencies=[Depends(require_module(Module.SEO))])
    """

    def guard(current: CurrentUserDep) -> CurrentUser:
        if write:
            current.require_write(module)
        else:
            current.require_view(module)
        return current

    return guard
