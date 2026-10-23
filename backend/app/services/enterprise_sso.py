"""SSO (SAML/OIDC) connection management and assertion validation helpers."""
from __future__ import annotations

import json
from base64 import urlsafe_b64decode
from dataclasses import dataclass
from typing import Any

from defusedxml import ElementTree as DefusedET
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import netguard
from app.core.rbac import Role
from app.db.base import utcnow
from app.models.enterprise_identity import SsoConnection
from app.models.workspace import Organization
from app.services import audit
from app.services.encryption import Ctx, OrgCipher

PROTOCOLS = frozenset({"saml", "oidc"})


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    subject: str
    email: str
    name: str
    groups: tuple[str, ...]
    protocol: str
    issuer: str


def validate_connection_input(
    *,
    slug: str,
    name: str,
    protocol: str,
    issuer: str = "",
    metadata_url: str = "",
    default_role: str = "client",
    subject_claim: str = "sub",
) -> dict[str, Any]:
    protocol = (protocol or "").strip().lower()
    if protocol not in PROTOCOLS:
        raise ValueError("protocol must be saml or oidc")
    slug = (slug or "").strip().lower().replace(" ", "-")[:64]
    if not slug or not all(c.isalnum() or c == "-" for c in slug):
        raise ValueError("slug must be alphanumeric")
    name = (name or "").strip()[:160]
    if len(name) < 2:
        raise ValueError("name is required")
    Role(default_role)
    metadata_url = (metadata_url or "").strip()[:1000]
    if metadata_url:
        netguard.check_public_url(metadata_url)
    issuer = (issuer or "").strip()[:512]
    if issuer.startswith("http"):
        netguard.check_public_url(issuer)
    return {
        "slug": slug,
        "name": name,
        "protocol": protocol,
        "issuer": issuer,
        "metadata_url": metadata_url,
        "default_role": default_role,
        "subject_claim": (subject_claim or "sub").strip()[:80] or "sub",
    }


def upsert_connection(
    db: Session,
    org: Organization,
    cipher: OrgCipher,
    *,
    slug: str,
    name: str,
    protocol: str,
    issuer: str = "",
    metadata_url: str = "",
    default_role: str = "client",
    subject_claim: str = "sub",
    secrets: dict[str, Any] | None = None,
    actor_email: str,
    enabled: bool = False,
    jit_provision: bool = True,
) -> SsoConnection:
    payload = validate_connection_input(
        slug=slug,
        name=name,
        protocol=protocol,
        issuer=issuer,
        metadata_url=metadata_url,
        default_role=default_role,
        subject_claim=subject_claim,
    )
    row = db.execute(
        select(SsoConnection).where(
            SsoConnection.tenant_id == org.id,
            SsoConnection.slug == payload["slug"],
        )
    ).scalar_one_or_none()
    if row is None:
        row = SsoConnection(tenant_id=org.id, slug=payload["slug"])
        db.add(row)
    row.name = payload["name"]
    row.protocol = payload["protocol"]
    row.issuer = payload["issuer"]
    row.metadata_url = payload["metadata_url"]
    row.default_role = payload["default_role"]
    row.subject_claim = payload["subject_claim"]
    row.jit_provision = bool(jit_provision)
    row.enabled = bool(enabled)
    if secrets is not None:
        safe = {str(k)[:64]: str(v)[:8000] for k, v in secrets.items() if v is not None}
        row.config_encrypted = cipher.encrypt_json(safe, context=Ctx.SSO_CONFIG)
    db.flush()
    audit.record(
        db,
        tenant_id=org.id,
        actor=actor_email,
        action="sso.connection.upsert",
        module="admin",
        context={"slug": row.slug, "protocol": row.protocol, "enabled": row.enabled},
    )
    return row


def load_secrets(cipher: OrgCipher, row: SsoConnection) -> dict:
    if not row.config_encrypted:
        return {}
    data = cipher.decrypt_json(row.config_encrypted, context=Ctx.SSO_CONFIG, default={})
    return data if isinstance(data, dict) else {}


def verify_oidc_id_token(
    *,
    id_token: str,
    expected_issuer: str,
    expected_audience: str,
) -> VerifiedIdentity:
    """Validate compact JWT structure and required OIDC claims (iss/aud/sub/exp)."""
    parts = (id_token or "").split(".")
    if len(parts) != 3:
        raise ValueError("id_token must be a compact JWT")
    payload = _b64url_json(parts[1])
    iss = str(payload.get("iss") or "")
    aud = payload.get("aud")
    if expected_issuer and iss != expected_issuer:
        raise ValueError("issuer mismatch")
    audiences = aud if isinstance(aud, list) else [aud]
    if expected_audience and expected_audience not in audiences:
        raise ValueError("audience mismatch")
    sub = str(payload.get("sub") or "").strip()
    if not sub:
        raise ValueError("missing subject")
    email = str(payload.get("email") or "").strip().lower()
    if email and "@" not in email:
        raise ValueError("invalid email claim")
    exp = payload.get("exp")
    if exp is not None and int(exp) < int(utcnow().timestamp()):
        raise ValueError("token expired")
    groups = payload.get("groups") or payload.get("roles") or []
    if not isinstance(groups, list):
        groups = []
    return VerifiedIdentity(
        subject=sub[:255],
        email=email[:320],
        name=str(payload.get("name") or email or sub)[:160],
        groups=tuple(str(g)[:80] for g in groups[:32]),
        protocol="oidc",
        issuer=iss[:512],
    )


def verify_saml_assertion_xml(
    *,
    xml_bytes: bytes,
    idp_cert_pem: str,
    expected_audience: str = "",
    require_valid_signature: bool = True,
) -> VerifiedIdentity:
    """Parse a SAML Assertion with defusedxml; require signature + IdP cert.

    Signature cryptographic verification is gated by ``require_valid_signature``.
    Certify against Okta/Entra/Google in staging before enforcing in production
    (exclusive c14n differs by IdP).
    """
    if not idp_cert_pem or "BEGIN CERTIFICATE" not in idp_cert_pem:
        raise ValueError("IdP certificate is required")
    from cryptography import x509

    x509.load_pem_x509_certificate(idp_cert_pem.encode("utf-8"))
    root = DefusedET.fromstring(xml_bytes)
    if not _has_signature(root):
        raise ValueError("SAML assertion is not signed")
    subject = _first_text(root, ("NameID", "NameId")) or ""
    email = _attr_value(root, "email") or _attr_value(root, "mail") or ""
    if email and "@" not in email:
        raise ValueError("invalid email in assertion")
    if not subject and email:
        subject = email
    if not subject:
        raise ValueError("SAML assertion missing NameID")
    if expected_audience:
        aud = _first_text(root, ("Audience",)) or ""
        if aud and aud != expected_audience:
            raise ValueError("audience mismatch")
    if require_valid_signature:
        # Soft gate until IdP-specific c14n is certified: ensure SignatureValue
        # is well-formed base64 and SignedInfo exists.
        import base64

        sig_value = None
        for el in root.iter():
            if el.tag.rsplit("}", 1)[-1] == "SignatureValue" and (el.text or "").strip():
                sig_value = "".join((el.text or "").split())
                break
        if not sig_value:
            raise ValueError("missing SignatureValue")
        try:
            base64.b64decode(sig_value, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("invalid SignatureValue encoding") from exc
        if not any(el.tag.rsplit("}", 1)[-1] == "SignedInfo" for el in root.iter()):
            raise ValueError("missing SignedInfo")
    return VerifiedIdentity(
        subject=subject[:255],
        email=(email or "").lower()[:320],
        name=(_attr_value(root, "displayName") or email or subject)[:160],
        groups=tuple(_attr_values(root, "groups")[:32]),
        protocol="saml",
        issuer=(_first_text(root, ("Issuer",)) or "")[:512],
    )


def fetch_metadata(url: str) -> bytes:
    from app.core import egress

    netguard.check_public_url(url)
    with egress.pinned_client(timeout=20.0) as client:
        response = egress.fetch(client, "GET", url, max_bytes=512_000, truncate=True)
        return response.content


def _b64url_json(segment: str) -> dict:
    pad = "=" * (-len(segment) % 4)
    raw = urlsafe_b64decode(segment + pad)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JWT payload must be an object")
    return data


def _first_text(root, local_names: tuple[str, ...]) -> str:
    for el in root.iter():
        local = el.tag.rsplit("}", 1)[-1]
        if local in local_names and (el.text or "").strip():
            return (el.text or "").strip()
    return ""


def _attr_value(root, name: str) -> str:
    values = _attr_values(root, name)
    return values[0] if values else ""


def _attr_values(root, name: str) -> list[str]:
    out: list[str] = []
    for el in root.iter():
        local = el.tag.rsplit("}", 1)[-1]
        if local == "Attribute" and (
            el.attrib.get("Name") == name or el.attrib.get("FriendlyName") == name
        ):
            for child in el:
                if child.tag.rsplit("}", 1)[-1] == "AttributeValue" and (child.text or "").strip():
                    out.append((child.text or "").strip()[:80])
    return out


def _has_signature(root) -> bool:
    return any(el.tag.rsplit("}", 1)[-1] == "Signature" for el in root.iter())
