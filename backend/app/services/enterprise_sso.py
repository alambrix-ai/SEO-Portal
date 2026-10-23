"""SSO (SAML/OIDC) connection management and cryptographic verification.

SAML assertions are verified with **signxml** (exclusive C14N, X.509 key
resolution, signature-wrapping resistant by consuming only the signed
subtree). OIDC ID tokens are verified with **Authlib** JOSE (signature via
JWKS or PEM, iss/aud/exp/nbf/iat/nonce).

SSO login is **fail-closed**: ``settings.sso_login_enabled`` defaults to
false, and verification always requires real cryptographic material — never
structural/unsigned acceptance.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from xml.etree import ElementTree as StdET

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from defusedxml import ElementTree as DefusedET
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import netguard
from app.core.config import settings
from app.core.rbac import Role
from app.db.base import utcnow
from app.models.enterprise_identity import SsoConnection
from app.models.workspace import Organization
from app.services import audit
from app.services.encryption import Ctx, OrgCipher

PROTOCOLS = frozenset({"saml", "oidc"})
_NS = {
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
    "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
}

#: In-process replay ledger: assertion_id / jti → expires_at monotonic.
_REPLAY: dict[str, float] = {}
_REPLAY_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    subject: str
    email: str
    name: str
    groups: tuple[str, ...]
    protocol: str
    issuer: str


def _require_sso_login_enabled() -> None:
    if not settings.sso_login_enabled:
        raise ValueError("SSO login is disabled (fail-closed)")


def _replay_seen(key: str) -> bool:
    now = time.monotonic()
    ttl = float(settings.sso_replay_ttl_seconds)
    with _REPLAY_LOCK:
        dead = [k for k, exp in _REPLAY.items() if exp <= now]
        for k in dead:
            del _REPLAY[k]
        if key in _REPLAY:
            return True
        _REPLAY[key] = now + ttl
        return False


def reset_replay_cache() -> None:
    """Tests only: clear the in-process replay ledger."""
    with _REPLAY_LOCK:
        _REPLAY.clear()


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
    # Fail closed: never enable a connection for login while the platform flag
    # is off — admin may still store draft IdP config.
    if enabled and not settings.sso_login_enabled:
        raise ValueError("SSO login is disabled (fail-closed); cannot enable connection")
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
    row.enabled = bool(enabled) and settings.sso_login_enabled
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
    jwks: dict | list | None = None,
    public_key_pem: str | None = None,
    expected_nonce: str | None = None,
    algorithms: tuple[str, ...] = ("RS256", "ES256"),
) -> VerifiedIdentity:
    """Cryptographically verify an OIDC ID token (Authlib JOSE).

    Requires ``settings.sso_login_enabled`` and either a JWKS document or a
    PEM public key. Rejects ``alg=none``, expired/nbf tokens, wrong iss/aud,
    missing sub, and replayed ``jti`` / nonce.
    """
    _require_sso_login_enabled()
    if not id_token or id_token.count(".") != 2:
        raise ValueError("id_token must be a compact JWT")
    if not expected_issuer or not expected_audience:
        raise ValueError("issuer and audience are required")
    if jwks is None and not public_key_pem:
        raise ValueError("JWKS or public key is required for OIDC verification")

    from authlib.jose import JsonWebKey, JsonWebToken
    from authlib.jose.errors import JoseError

    jwt = JsonWebToken(list(algorithms))
    try:
        if public_key_pem:
            key = JsonWebKey.import_key(public_key_pem.encode("utf-8"))
        else:
            key = JsonWebKey.import_key_set(jwks if isinstance(jwks, dict) else {"keys": jwks})
        claims = jwt.decode(id_token, key)
        claims.validate(
            now=int(utcnow().timestamp()),
            leeway=int(settings.sso_clock_skew_seconds),
        )
    except JoseError as exc:
        raise ValueError("OIDC token verification failed") from exc
    except Exception as exc:  # noqa: BLE001 - map any crypto failure to ValueError
        raise ValueError("OIDC token verification failed") from exc

    # Authlib validate() covers exp/nbf/iat; enforce iss/aud/nonce explicitly.
    iss = str(claims.get("iss") or "")
    if iss != expected_issuer:
        raise ValueError("issuer mismatch")
    aud = claims.get("aud")
    audiences = aud if isinstance(aud, list) else [aud]
    if expected_audience not in audiences:
        raise ValueError("audience mismatch")
    if expected_nonce is not None:
        if str(claims.get("nonce") or "") != expected_nonce:
            raise ValueError("nonce mismatch")
    sub = str(claims.get("sub") or "").strip()
    if not sub:
        raise ValueError("missing subject")
    email = str(claims.get("email") or "").strip().lower()
    if email and "@" not in email:
        raise ValueError("invalid email claim")

    replay_key = str(claims.get("jti") or "").strip()
    if not replay_key and expected_nonce:
        replay_key = f"nonce:{expected_nonce}"
    if replay_key and _replay_seen(f"oidc:{expected_issuer}:{replay_key}"):
        raise ValueError("replayed token")

    groups = claims.get("groups") or claims.get("roles") or []
    if not isinstance(groups, list):
        groups = []
    return VerifiedIdentity(
        subject=sub[:255],
        email=email[:320],
        name=str(claims.get("name") or email or sub)[:160],
        groups=tuple(str(g)[:80] for g in groups[:32]),
        protocol="oidc",
        issuer=iss[:512],
    )


def verify_saml_assertion_xml(
    *,
    xml_bytes: bytes,
    idp_cert_pem: str,
    expected_audience: str = "",
    expected_recipient: str = "",
    expected_in_response_to: str = "",
) -> VerifiedIdentity:
    """Verify a SAML 2.0 Assertion with signxml (fail-closed).

    Performs X.509 signature verification with exclusive C14N, then enforces
    Audience, Recipient, NotOnOrAfter, and InResponseTo on the **signed**
    subtree only (signature-wrapping resistant). Assertion ``ID`` values are
    recorded against an in-process replay ledger.
    """
    _require_sso_login_enabled()
    if not idp_cert_pem or "BEGIN CERTIFICATE" not in idp_cert_pem:
        raise ValueError("IdP certificate is required")
    if not xml_bytes or not xml_bytes.strip():
        raise ValueError("empty SAML document")

    cert = x509.load_pem_x509_certificate(idp_cert_pem.encode("utf-8"))
    # Reject obviously hostile XML before signature work (XXE / billion laughs).
    try:
        DefusedET.fromstring(xml_bytes)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("invalid SAML XML") from exc

    from lxml import etree
    from signxml import InvalidCertificate, InvalidDigest, InvalidInput, InvalidSignature
    from signxml import XMLVerifier

    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as exc:
        raise ValueError("invalid SAML XML") from exc

    if not any(el.tag.rsplit("}", 1)[-1] == "Signature" for el in root.iter()):
        raise ValueError("SAML assertion is not signed")

    try:
        verified = XMLVerifier().verify(
            root,
            x509_cert=cert.public_bytes(serialization.Encoding.PEM).decode(),
        )
    except (InvalidSignature, InvalidDigest, InvalidCertificate, InvalidInput) as exc:
        raise ValueError("SAML signature verification failed") from exc
    except Exception as exc:  # noqa: BLE001
        raise ValueError("SAML signature verification failed") from exc

    # Consume only the signed element — wrapping another Assertion around a
    # signed decoy must not let attacker-controlled NameID win.
    signed = verified.signed_xml
    if signed is None:
        raise ValueError("SAML signature verification failed")

    identity = _identity_from_signed_assertion(
        signed,
        expected_audience=expected_audience,
        expected_recipient=expected_recipient,
        expected_in_response_to=expected_in_response_to,
    )
    assertion_id = (signed.get("ID") or signed.get("Id") or "").strip()
    if assertion_id and _replay_seen(f"saml:{assertion_id}"):
        raise ValueError("replayed assertion")
    return identity


def _identity_from_signed_assertion(
    signed,
    *,
    expected_audience: str,
    expected_recipient: str,
    expected_in_response_to: str,
) -> VerifiedIdentity:
    # signed may be Assertion or Response; prefer Assertion.
    assertion = signed
    local = assertion.tag.rsplit("}", 1)[-1]
    if local == "Response":
        found = None
        for el in assertion.iter():
            if el.tag.rsplit("}", 1)[-1] == "Assertion":
                found = el
                break
        if found is None:
            raise ValueError("SAML Response missing Assertion")
        assertion = found

    _enforce_conditions(
        assertion,
        expected_audience=expected_audience,
        expected_recipient=expected_recipient,
        expected_in_response_to=expected_in_response_to,
    )

    subject = _first_text_lxml(assertion, ("NameID", "NameId")) or ""
    email = _attr_value_lxml(assertion, "email") or _attr_value_lxml(assertion, "mail") or ""
    if email and "@" not in email:
        raise ValueError("invalid email in assertion")
    if not subject and email:
        subject = email
    if not subject:
        raise ValueError("SAML assertion missing NameID")
    return VerifiedIdentity(
        subject=subject[:255],
        email=(email or "").lower()[:320],
        name=(_attr_value_lxml(assertion, "displayName") or email or subject)[:160],
        groups=tuple(_attr_values_lxml(assertion, "groups")[:32]),
        protocol="saml",
        issuer=(_first_text_lxml(assertion, ("Issuer",)) or "")[:512],
    )


def _enforce_conditions(
    assertion,
    *,
    expected_audience: str,
    expected_recipient: str,
    expected_in_response_to: str,
) -> None:
    skew = int(settings.sso_clock_skew_seconds)
    now = utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    not_on_or_after = None
    not_before = None
    audiences: list[str] = []
    for el in assertion.iter():
        local = el.tag.rsplit("}", 1)[-1]
        if local == "Conditions":
            raw_after = el.get("NotOnOrAfter")
            raw_before = el.get("NotBefore")
            if raw_after:
                not_on_or_after = _parse_saml_time(raw_after)
            if raw_before:
                not_before = _parse_saml_time(raw_before)
        if local == "Audience" and (el.text or "").strip():
            audiences.append((el.text or "").strip())
        if local == "SubjectConfirmationData":
            recip = (el.get("Recipient") or "").strip()
            if expected_recipient and recip and recip != expected_recipient:
                raise ValueError("recipient mismatch")
            in_resp = (el.get("InResponseTo") or "").strip()
            if expected_in_response_to and in_resp and in_resp != expected_in_response_to:
                raise ValueError("InResponseTo mismatch")
            sc_after = el.get("NotOnOrAfter")
            if sc_after:
                sc_dt = _parse_saml_time(sc_after)
                if sc_dt.tzinfo is None:
                    sc_dt = sc_dt.replace(tzinfo=timezone.utc)
                if now.timestamp() - skew >= sc_dt.timestamp():
                    raise ValueError("assertion expired")

    if not_before is not None:
        nb = not_before if not_before.tzinfo else not_before.replace(tzinfo=timezone.utc)
        if now.timestamp() + skew < nb.timestamp():
            raise ValueError("assertion not yet valid")
    if not_on_or_after is not None:
        na = (
            not_on_or_after
            if not_on_or_after.tzinfo
            else not_on_or_after.replace(tzinfo=timezone.utc)
        )
        if now.timestamp() - skew >= na.timestamp():
            raise ValueError("assertion expired")

    if expected_audience:
        if not audiences:
            raise ValueError("audience missing")
        if expected_audience not in audiences:
            raise ValueError("audience mismatch")


def _parse_saml_time(value: str) -> datetime:
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid SAML timestamp") from exc


def _first_text_lxml(root, local_names: tuple[str, ...]) -> str:
    for el in root.iter():
        local = el.tag.rsplit("}", 1)[-1]
        if local in local_names and (el.text or "").strip():
            return (el.text or "").strip()
    return ""


def _attr_value_lxml(root, name: str) -> str:
    values = _attr_values_lxml(root, name)
    return values[0] if values else ""


def _attr_values_lxml(root, name: str) -> list[str]:
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


def fetch_metadata(url: str) -> bytes:
    from app.core import egress

    netguard.check_public_url(url)
    with egress.pinned_client(timeout=20.0) as client:
        response = egress.fetch(client, "GET", url, max_bytes=512_000, truncate=True)
        return response.content


# Keep StdET import referenced for type checkers / accidental use elsewhere.
_ = StdET
