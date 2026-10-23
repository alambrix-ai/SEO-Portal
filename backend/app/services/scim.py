"""SCIM token minting and membership provision/deactivate."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.rbac import Role
from app.db.base import utcnow
from app.models.enterprise_identity import ScimToken
from app.models.membership import WorkspaceMembership
from app.models.workspace import Organization, User
from app.services import audit
from app.services.encryption import OrgCipher


@dataclass(frozen=True, slots=True)
class MintedScimToken:
    row: ScimToken
    raw_token: str


def mint_token(
    db: Session,
    org: Organization,
    *,
    label: str,
    created_by_id: str | None,
    actor_email: str,
) -> MintedScimToken:
    label = (label or "").strip()[:120] or "SCIM token"
    raw = f"scim_{crypto.new_url_token(32)}"
    row = ScimToken(
        tenant_id=org.id,
        label=label,
        token_hash=crypto.hash_token(raw),
        token_prefix=raw[:12],
        created_by_id=created_by_id,
    )
    db.add(row)
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action="scim.token.mint",
        module="admin",
        context={"label": label, "prefix": row.token_prefix},
    )
    return MintedScimToken(row=row, raw_token=raw)


def revoke_token(
    db: Session, org: Organization, token_id: str, *, actor_email: str
) -> ScimToken:
    row = db.get(ScimToken, token_id)
    if row is None or row.tenant_id != org.id:
        raise LookupError("SCIM token not found")
    row.revoked_at = utcnow()
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action="scim.token.revoke",
        module="admin",
        context={"token_id": row.id},
    )
    return row


def authenticate_bearer(db: Session, raw_token: str) -> tuple[Organization, ScimToken] | None:
    if not raw_token or not raw_token.startswith("scim_"):
        return None
    digest = crypto.hash_token(raw_token)
    row = db.execute(
        select(ScimToken).where(
            ScimToken.token_hash == digest,
            ScimToken.revoked_at.is_(None),
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    org = db.get(Organization, row.tenant_id)
    if org is None or not org.is_active:
        return None
    row.last_used_at = utcnow()
    db.flush()
    return org, row


def map_group_to_role(groups: list[str], mapping: dict[str, str] | None = None) -> str:
    mapping = mapping or {
        "admins": Role.ADMIN.value,
        "managers": Role.MANAGER.value,
        "seo": Role.SEO.value,
        "ads": Role.ADS.value,
        "approvers": Role.APPROVER.value,
    }
    for group in groups:
        key = str(group).strip().lower()
        if key in mapping:
            Role(mapping[key])
            return mapping[key]
    return Role.CLIENT.value


def provision_user(
    db: Session,
    org: Organization,
    cipher: OrgCipher,
    *,
    email: str,
    name: str,
    role: str,
    external_id: str = "",
    actor: str = "scim",
) -> User:
    email = crypto.normalize_email(email)
    if "@" not in email:
        raise ValueError("valid email required")
    Role(role)
    index = crypto.email_index(email)
    user = db.execute(
        select(User).where(User.tenant_id == org.id, User.email_index == index)
    ).scalar_one_or_none()
    if user is None:
        user = User(
            tenant_id=org.id,
            name=name[:160] or email.split("@")[0],
            email=email,
            email_index=index,
            role=role,
            is_active=True,
            email_verified=True,
            email_verified_at=utcnow(),
            sso_provider="scim",
            sso_subject=(external_id or email)[:255],
        )
        db.add(user)
        db.flush()
    else:
        user.name = name[:160] or user.name
        user.role = role
        user.is_active = True
        if external_id:
            user.sso_subject = external_id[:255]
        db.flush()

    membership = db.execute(
        select(WorkspaceMembership).where(
            WorkspaceMembership.tenant_id == org.id,
            WorkspaceMembership.email_index == index,
        )
    ).scalar_one_or_none()
    if membership is None:
        db.add(
            WorkspaceMembership(
                tenant_id=org.id,
                email_index=index,
                user_id=user.id,
                is_active=True,
            )
        )
    else:
        membership.user_id = user.id
        membership.is_active = True
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor,
        action="scim.user.provision",
        module="admin",
        context={"role": role, "user_id": user.id},
    )
    return user


def deactivate_user(
    db: Session, org: Organization, *, email: str, actor: str = "scim"
) -> User | None:
    index = crypto.email_index(crypto.normalize_email(email))
    user = db.execute(
        select(User).where(User.tenant_id == org.id, User.email_index == index)
    ).scalar_one_or_none()
    if user is None:
        return None
    user.is_active = False
    membership = db.execute(
        select(WorkspaceMembership).where(
            WorkspaceMembership.tenant_id == org.id,
            WorkspaceMembership.email_index == index,
        )
    ).scalar_one_or_none()
    if membership is not None:
        membership.is_active = False
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor,
        action="scim.user.deactivate",
        module="admin",
        context={"user_id": user.id},
    )
    return user
