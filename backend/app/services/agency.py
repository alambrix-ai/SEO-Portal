"""Agency parent accounts, client links, and white-label branding."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.config import settings
from app.models.agency import Agency, AgencyClientLink, AgencyMembership, WhiteLabelSettings
from app.models.workspace import Organization
from app.services import audit
from app.services.encryption import Ctx, OrgCipher

_SLUG = re.compile(r"^[a-z0-9]([a-z0-9-]{0,78}[a-z0-9])?$")
_HOSTNAME = re.compile(
    r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*"
)
ALLOWED_MEMBER_ROLES = frozenset({"owner", "admin", "member"})
ALLOWED_ACCESS = frozenset({"view", "manage"})


def _valid_slug(slug: str) -> str:
    slug = (slug or "").strip().lower()
    if not _SLUG.fullmatch(slug):
        raise ValueError("invalid agency slug")
    return slug


def _email_index(email: str) -> str:
    return crypto.email_index(crypto.normalize_email(email))


def create_agency(
    db: Session,
    *,
    name: str,
    slug: str,
    owner_email: str,
    audit_tenant_id: str,
) -> Agency:
    """Create an agency and its owner membership.

    ``audit_tenant_id`` is the acting workspace (admin action context); the
    agency itself is not a tenant.
    """
    name = (name or "").strip()
    if not name or len(name) > 160:
        raise ValueError("invalid agency name")
    slug = _valid_slug(slug)
    existing = db.execute(select(Agency).where(Agency.slug == slug)).scalar_one_or_none()
    if existing is not None:
        raise ValueError("agency slug already taken")
    agency = Agency(name=name, slug=slug, is_active=True)
    db.add(agency)
    db.flush()
    member = AgencyMembership(
        agency_id=agency.id,
        email_index=_email_index(owner_email),
        role="owner",
        is_active=True,
    )
    db.add(member)
    db.flush()
    audit.record(
        db,
        tenant_id=audit_tenant_id,
        actor=crypto.fingerprint(owner_email),
        action="agency.create",
        module="admin",
        context={"agency_id": agency.id, "slug": slug},
    )
    return agency


def add_member(
    db: Session,
    agency: Agency,
    *,
    email: str,
    role: str = "member",
    actor_email: str,
    audit_tenant_id: str,
) -> AgencyMembership:
    role = (role or "member").strip().lower()
    if role not in ALLOWED_MEMBER_ROLES:
        raise ValueError("invalid agency role")
    if not _require_agency_role(db, agency, actor_email, {"owner", "admin"}):
        raise ValueError("agency admin required")
    index = _email_index(email)
    row = db.execute(
        select(AgencyMembership).where(
            AgencyMembership.agency_id == agency.id,
            AgencyMembership.email_index == index,
        )
    ).scalar_one_or_none()
    if row is None:
        row = AgencyMembership(agency_id=agency.id, email_index=index)
        db.add(row)
    row.role = role
    row.is_active = True
    db.flush()
    audit.record(
        db,
        tenant_id=audit_tenant_id,
        actor=crypto.fingerprint(actor_email),
        action="agency.member.upsert",
        module="admin",
        context={"agency_id": agency.id, "role": role},
    )
    return row


def _require_agency_role(
    db: Session, agency: Agency, email: str, roles: set[str]
) -> bool:
    index = _email_index(email)
    row = db.execute(
        select(AgencyMembership).where(
            AgencyMembership.agency_id == agency.id,
            AgencyMembership.email_index == index,
            AgencyMembership.is_active.is_(True),
        )
    ).scalar_one_or_none()
    return row is not None and row.role in roles


def link_client(
    db: Session,
    agency: Agency,
    client: Organization,
    *,
    access_level: str = "view",
    actor_email: str,
    audit_tenant_id: str,
) -> AgencyClientLink:
    """Link a client workspace. Caller must be an agency owner/admin.

    Does not grant shared tenant_id access — client rows stay under the
    client's RLS; agency users must pin the client tenant explicitly.
    """
    if not _require_agency_role(db, agency, actor_email, {"owner", "admin"}):
        raise ValueError("agency admin required")
    access_level = (access_level or "view").strip().lower()
    if access_level not in ALLOWED_ACCESS:
        raise ValueError("invalid access_level")
    row = db.execute(
        select(AgencyClientLink).where(
            AgencyClientLink.agency_id == agency.id,
            AgencyClientLink.client_tenant_id == client.id,
        )
    ).scalar_one_or_none()
    if row is None:
        row = AgencyClientLink(agency_id=agency.id, client_tenant_id=client.id)
        db.add(row)
    row.access_level = access_level
    row.is_active = True
    db.flush()
    audit.record(
        db,
        tenant_id=audit_tenant_id,
        actor=crypto.fingerprint(actor_email),
        action="agency.client.link",
        module="admin",
        context={
            "agency_id": agency.id,
            "client_fp": crypto.fingerprint(client.id),
            "access_level": access_level,
        },
    )
    return row


def list_client_tenant_ids(db: Session, agency: Agency, *, email: str) -> list[str]:
    """Client tenants visible to an active agency member (roll-up listing)."""
    if not _require_agency_role(db, agency, email, {"owner", "admin", "member"}):
        raise ValueError("agency membership required")
    rows = db.execute(
        select(AgencyClientLink.client_tenant_id).where(
            AgencyClientLink.agency_id == agency.id,
            AgencyClientLink.is_active.is_(True),
        )
    ).scalars()
    return list(rows)


def _validate_custom_domain(domain: str) -> str:
    """Hostname only — never expands auth/CORS origins by itself."""
    domain = (domain or "").strip().lower().rstrip(".")
    if not domain:
        return ""
    if "://" in domain:
        host = (urlsplit(domain).hostname or "").lower()
    else:
        host = domain.split("/")[0].split(":")[0]
    if not _HOSTNAME.fullmatch(host):
        raise ValueError("invalid custom_domain")
    # Auth origin checks stay on settings.cors_origins / demo_allowed_origins.
    # A white-label domain is display metadata unless an operator also adds it
    # to those allowlists — never auto-trust it for login cookies/CORS.
    _ = settings.cors_origins
    return host


def upsert_white_label(
    db: Session,
    agency: Agency,
    cipher: OrgCipher,
    *,
    custom_domain: str = "",
    email_from_name: str = "",
    branding: dict | None = None,
    brand_safety: dict | None = None,
    enabled: bool = False,
    actor_email: str,
    audit_tenant_id: str,
) -> WhiteLabelSettings:
    if not _require_agency_role(db, agency, actor_email, {"owner", "admin"}):
        raise ValueError("agency admin required")
    domain = _validate_custom_domain(custom_domain)
    email_from_name = (email_from_name or "").strip()[:120]
    row = db.execute(
        select(WhiteLabelSettings).where(WhiteLabelSettings.agency_id == agency.id)
    ).scalar_one_or_none()
    if row is None:
        row = WhiteLabelSettings(agency_id=agency.id)
        db.add(row)
    row.custom_domain = domain
    row.email_from_name = email_from_name
    row.brand_safety = brand_safety or {}
    row.enabled = bool(enabled)
    if branding is not None:
        # Encrypt under the acting workspace cipher — never store logo URLs/colours clear.
        row.branding_encrypted = cipher.encrypt_json(branding, context=Ctx.WHITE_LABEL)
    db.flush()
    audit.record(
        db,
        tenant_id=audit_tenant_id,
        actor=crypto.fingerprint(actor_email),
        action="agency.white_label.upsert",
        module="admin",
        context={"agency_id": agency.id, "enabled": bool(enabled), "has_domain": bool(domain)},
    )
    return row


def decrypt_branding(
    cipher: OrgCipher, row: WhiteLabelSettings
) -> dict:
    if not row.branding_encrypted:
        return {}
    return cipher.decrypt_json(row.branding_encrypted, context=Ctx.WHITE_LABEL)
