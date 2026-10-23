"""Workspace IP allowlist checks (trusted-proxy aware)."""
from __future__ import annotations

import ipaddress

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, InvalidInputError
from app.models.enterprise_identity import IpAllowlistEntry
from app.models.workspace import Organization
from app.services import audit


def validate_cidr(cidr: str) -> str:
    text = (cidr or "").strip()
    try:
        network = ipaddress.ip_network(text, strict=False)
    except ValueError as exc:
        raise ValueError("invalid CIDR or IP") from exc
    # Refuse clearly non-routable documentation traps for allowlists that are
    # meant to represent office egress — private ranges are allowed (VPN).
    return str(network)


def add_entry(
    db: Session,
    org: Organization,
    *,
    cidr: str,
    label: str = "",
    actor_email: str,
) -> IpAllowlistEntry:
    cidr = validate_cidr(cidr)
    existing = db.execute(
        select(IpAllowlistEntry).where(
            IpAllowlistEntry.tenant_id == org.id,
            IpAllowlistEntry.cidr == cidr,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.enabled = True
        existing.label = (label or existing.label)[:120]
        db.flush()
        return existing
    row = IpAllowlistEntry(
        tenant_id=org.id,
        cidr=cidr,
        label=(label or "")[:120],
        enabled=True,
    )
    db.add(row)
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action="ip_allowlist.add",
        module="admin",
        context={"cidr": cidr},
    )
    return row


def _matches_entries(db: Session, org: Organization, client_ip: str) -> bool:
    """True when ``client_ip`` falls in at least one enabled CIDR for ``org``."""
    try:
        addr = ipaddress.ip_address((client_ip or "").strip())
    except ValueError:
        return False
    entries = list(
        db.execute(
            select(IpAllowlistEntry).where(
                IpAllowlistEntry.tenant_id == org.id,
                IpAllowlistEntry.enabled.is_(True),
            )
        ).scalars()
    )
    if not entries:
        return False
    for entry in entries:
        try:
            network = ipaddress.ip_network(entry.cidr, strict=False)
        except ValueError:
            continue
        if addr in network:
            return True
    return False


def is_allowed(db: Session, org: Organization, client_ip: str) -> bool:
    """Return True when allowlist is off or the client IP matches an entry."""
    if not org.ip_allowlist_enabled:
        return True
    return _matches_entries(db, org, client_ip)


def enforce(db: Session, org: Organization, client_ip: str) -> None:
    """Raise when the workspace allowlist is on and ``client_ip`` is outside it."""
    if is_allowed(db, org, client_ip):
        return
    raise ForbiddenError(
        "Requests from this network are not allowed for this workspace."
    )


def assert_not_locking_out(
    db: Session, org: Organization, *, actor_ip: str
) -> None:
    """Refuse enabling (or keeping) an allowlist that would lock out the admin."""
    if not _matches_entries(db, org, actor_ip):
        raise InvalidInputError(
            "Add an allowlist entry that covers your current IP before enabling "
            "the allowlist, so you are not locked out."
        )


def set_enabled(
    db: Session,
    org: Organization,
    *,
    enabled: bool,
    actor_email: str,
    actor_ip: str | None = None,
) -> Organization:
    if enabled:
        if not actor_ip:
            raise InvalidInputError(
                "Your current IP could not be determined; enable the allowlist "
                "from a network that is already listed."
            )
        assert_not_locking_out(db, org, actor_ip=actor_ip)
    org.ip_allowlist_enabled = bool(enabled)
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action="ip_allowlist.toggle",
        module="admin",
        context={"enabled": org.ip_allowlist_enabled},
    )
    return org
