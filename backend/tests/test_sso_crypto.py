"""Cryptographic SAML/OIDC verification — negative and positive cases."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from authlib.jose import JsonWebKey, jwt as authlib_jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from lxml import etree
from signxml import XMLSigner

from app.services import enterprise_sso


SAML_NS = "urn:oasis:names:tc:SAML:2.0:assertion"
ISSUER = "https://idp.example.com"
AUDIENCE = "synaprank"


@pytest.fixture(autouse=True)
def _enable_sso(monkeypatch):  # noqa: ANN001, ANN201
    monkeypatch.setattr(enterprise_sso.settings, "sso_login_enabled", True)
    enterprise_sso.reset_replay_cache()
    yield
    enterprise_sso.reset_replay_cache()


@pytest.fixture
def rsa_material():  # noqa: ANN201
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-idp")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    return {"key": key, "priv": priv, "pub": pub, "cert_pem": pem}


def _oidc_token(priv: bytes, claims: dict, alg: str = "RS256") -> str:
    token = authlib_jwt.encode({"alg": alg}, claims, JsonWebKey.import_key(priv))
    return token.decode() if isinstance(token, bytes) else token


def _saml_assertion(
    *,
    priv: bytes,
    cert_pem: str,
    name_id: str = "user@example.com",
    assertion_id: str = "_assert-1",
    audience: str = AUDIENCE,
    not_on_or_after: str = "2099-01-01T00:00:00Z",
    not_before: str = "2020-01-01T00:00:00Z",
    recipient: str = "https://app.example.com/acs",
    in_response_to: str = "req-1",
) -> etree._Element:
    assertion = etree.Element(
        f"{{{SAML_NS}}}Assertion",
        nsmap={None: SAML_NS},
        ID=assertion_id,
        IssueInstant="2026-01-01T00:00:00Z",
        Version="2.0",
    )
    etree.SubElement(assertion, f"{{{SAML_NS}}}Issuer").text = ISSUER
    etree.SubElement(assertion, f"{{{SAML_NS}}}NameID").text = name_id
    conds = etree.SubElement(
        assertion,
        f"{{{SAML_NS}}}Conditions",
        NotBefore=not_before,
        NotOnOrAfter=not_on_or_after,
    )
    ar = etree.SubElement(conds, f"{{{SAML_NS}}}AudienceRestriction")
    etree.SubElement(ar, f"{{{SAML_NS}}}Audience").text = audience
    sc = etree.SubElement(assertion, f"{{{SAML_NS}}}SubjectConfirmation", Method="urn:oasis:names:tc:SAML:2.0:cm:bearer")
    etree.SubElement(
        sc,
        f"{{{SAML_NS}}}SubjectConfirmationData",
        Recipient=recipient,
        InResponseTo=in_response_to,
        NotOnOrAfter=not_on_or_after,
    )
    return XMLSigner(
        c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#"
    ).sign(assertion, key=priv, cert=cert_pem.encode())


def test_fail_closed_when_sso_disabled(rsa_material, monkeypatch):  # noqa: ANN001
    from app.core.exceptions import FeatureNotConfiguredError

    monkeypatch.setattr(enterprise_sso.settings, "sso_login_enabled", False)
    with pytest.raises(FeatureNotConfiguredError, match="not configured"):
        enterprise_sso.verify_oidc_id_token(
            id_token="a.b.c",
            expected_issuer=ISSUER,
            expected_audience=AUDIENCE,
            public_key_pem=rsa_material["pub"].decode(),
        )


def test_cannot_enable_connection_when_sso_disabled(rsa_material, monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(enterprise_sso.settings, "sso_login_enabled", False)
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.db.base import Base
    from app.models.enterprise_identity import SsoConnection
    from app.models.workspace import AuditLogEntry, Organization
    from tests.test_seo_pipeline_fixes import TENANT, PlainCipher

    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine, tables=[Organization.__table__, AuditLogEntry.__table__, SsoConnection.__table__]
    )
    with Session(engine) as db:
        org = Organization(
            id=TENANT,
            name="Acme",
            slug="acme-sso",
            wrapped_dek="t",
            dek_version=1,
            plan_name="E",
            plan_tier="enterprise",
            seats_total=1,
            is_active=True,
            data_region="eu-central",
            settings={},
            ip_allowlist_enabled=False,
            mfa_required=False,
        )
        db.add(org)
        db.flush()
        from app.core.exceptions import FeatureNotConfiguredError

        with pytest.raises(FeatureNotConfiguredError, match="not configured"):
            enterprise_sso.upsert_connection(
                db,
                org,
                PlainCipher(),
                slug="okta",
                name="Okta",
                protocol="oidc",
                issuer="urn:example:idp",
                enabled=True,
                actor_email="a@example.com",
            )
    engine.dispose()


def test_oidc_valid_rs256(rsa_material):  # noqa: ANN001
    now = int(time.time())
    token = _oidc_token(
        rsa_material["priv"],
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "user-1",
            "email": "a@example.com",
            "exp": now + 600,
            "iat": now,
            "nbf": now - 5,
            "jti": "jti-ok",
            "nonce": "n-1",
        },
    )
    identity = enterprise_sso.verify_oidc_id_token(
        id_token=token,
        expected_issuer=ISSUER,
        expected_audience=AUDIENCE,
        public_key_pem=rsa_material["pub"].decode(),
        expected_nonce="n-1",
    )
    assert identity.email == "a@example.com"
    assert identity.subject == "user-1"


def test_oidc_rejects_alg_none(rsa_material):  # noqa: ANN001
    import base64
    import json

    def b64(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    now = int(time.time())
    token = ".".join(
        [
            b64({"alg": "none", "typ": "JWT"}),
            b64(
                {
                    "iss": ISSUER,
                    "aud": AUDIENCE,
                    "sub": "u",
                    "exp": now + 600,
                }
            ),
            "",
        ]
    )
    with pytest.raises(ValueError):
        enterprise_sso.verify_oidc_id_token(
            id_token=token,
            expected_issuer=ISSUER,
            expected_audience=AUDIENCE,
            public_key_pem=rsa_material["pub"].decode(),
        )


def test_oidc_rejects_wrong_audience(rsa_material):  # noqa: ANN001
    now = int(time.time())
    token = _oidc_token(
        rsa_material["priv"],
        {
            "iss": ISSUER,
            "aud": "other-app",
            "sub": "u",
            "exp": now + 600,
            "iat": now,
            "jti": "jti-aud",
        },
    )
    with pytest.raises(ValueError, match="audience"):
        enterprise_sso.verify_oidc_id_token(
            id_token=token,
            expected_issuer=ISSUER,
            expected_audience=AUDIENCE,
            public_key_pem=rsa_material["pub"].decode(),
        )


def test_oidc_rejects_expired(rsa_material, monkeypatch):  # noqa: ANN001
    monkeypatch.setattr(enterprise_sso.settings, "sso_clock_skew_seconds", 0)
    now = int(time.time())
    token = _oidc_token(
        rsa_material["priv"],
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "u",
            "exp": now - 600,
            "iat": now - 1200,
            "jti": "jti-exp",
        },
    )
    with pytest.raises(ValueError):
        enterprise_sso.verify_oidc_id_token(
            id_token=token,
            expected_issuer=ISSUER,
            expected_audience=AUDIENCE,
            public_key_pem=rsa_material["pub"].decode(),
        )


def test_oidc_rejects_replay(rsa_material):  # noqa: ANN001
    now = int(time.time())
    token = _oidc_token(
        rsa_material["priv"],
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "u",
            "exp": now + 600,
            "iat": now,
            "jti": "jti-replay",
        },
    )
    enterprise_sso.verify_oidc_id_token(
        id_token=token,
        expected_issuer=ISSUER,
        expected_audience=AUDIENCE,
        public_key_pem=rsa_material["pub"].decode(),
    )
    with pytest.raises(ValueError, match="replay"):
        enterprise_sso.verify_oidc_id_token(
            id_token=token,
            expected_issuer=ISSUER,
            expected_audience=AUDIENCE,
            public_key_pem=rsa_material["pub"].decode(),
        )


def test_oidc_rejects_tampered_payload(rsa_material):  # noqa: ANN001
    now = int(time.time())
    token = _oidc_token(
        rsa_material["priv"],
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "u",
            "exp": now + 600,
            "iat": now,
            "jti": "jti-tamp",
        },
    )
    parts = token.split(".")
    # Flip a character in the signature
    sig = parts[2]
    flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
    bad = ".".join([parts[0], parts[1], flipped])
    with pytest.raises(ValueError):
        enterprise_sso.verify_oidc_id_token(
            id_token=bad,
            expected_issuer=ISSUER,
            expected_audience=AUDIENCE,
            public_key_pem=rsa_material["pub"].decode(),
        )


def test_saml_valid(rsa_material):  # noqa: ANN001
    signed = _saml_assertion(priv=rsa_material["priv"], cert_pem=rsa_material["cert_pem"])
    identity = enterprise_sso.verify_saml_assertion_xml(
        xml_bytes=etree.tostring(signed),
        idp_cert_pem=rsa_material["cert_pem"],
        expected_audience=AUDIENCE,
        expected_recipient="https://app.example.com/acs",
        expected_in_response_to="req-1",
    )
    assert identity.email == "user@example.com" or identity.subject == "user@example.com"


def test_saml_rejects_unsigned(rsa_material):  # noqa: ANN001
    xml = (
        f'<Assertion xmlns="{SAML_NS}" ID="_u">'
        f"<NameID>user@example.com</NameID></Assertion>"
    ).encode()
    with pytest.raises(ValueError, match="not signed"):
        enterprise_sso.verify_saml_assertion_xml(
            xml_bytes=xml, idp_cert_pem=rsa_material["cert_pem"]
        )


def test_saml_rejects_tampered_nameid(rsa_material):  # noqa: ANN001
    signed = _saml_assertion(priv=rsa_material["priv"], cert_pem=rsa_material["cert_pem"])
    for el in signed.iter():
        if el.tag.rsplit("}", 1)[-1] == "NameID":
            el.text = "attacker@evil.example"
            break
    with pytest.raises(ValueError, match="signature"):
        enterprise_sso.verify_saml_assertion_xml(
            xml_bytes=etree.tostring(signed),
            idp_cert_pem=rsa_material["cert_pem"],
            expected_audience=AUDIENCE,
        )


def test_saml_rejects_wrong_audience(rsa_material):  # noqa: ANN001
    signed = _saml_assertion(
        priv=rsa_material["priv"],
        cert_pem=rsa_material["cert_pem"],
        audience="other-sp",
        assertion_id="_aud-bad",
    )
    with pytest.raises(ValueError, match="audience"):
        enterprise_sso.verify_saml_assertion_xml(
            xml_bytes=etree.tostring(signed),
            idp_cert_pem=rsa_material["cert_pem"],
            expected_audience=AUDIENCE,
        )


def test_saml_rejects_expired(rsa_material):  # noqa: ANN001
    signed = _saml_assertion(
        priv=rsa_material["priv"],
        cert_pem=rsa_material["cert_pem"],
        assertion_id="_expired",
        not_on_or_after="2020-01-01T00:00:00Z",
        not_before="2019-01-01T00:00:00Z",
    )
    with pytest.raises(ValueError, match="expired"):
        enterprise_sso.verify_saml_assertion_xml(
            xml_bytes=etree.tostring(signed),
            idp_cert_pem=rsa_material["cert_pem"],
            expected_audience=AUDIENCE,
        )


def test_saml_rejects_replay(rsa_material):  # noqa: ANN001
    signed = _saml_assertion(
        priv=rsa_material["priv"],
        cert_pem=rsa_material["cert_pem"],
        assertion_id="_replay-1",
    )
    raw = etree.tostring(signed)
    enterprise_sso.verify_saml_assertion_xml(
        xml_bytes=raw,
        idp_cert_pem=rsa_material["cert_pem"],
        expected_audience=AUDIENCE,
        expected_recipient="https://app.example.com/acs",
        expected_in_response_to="req-1",
    )
    with pytest.raises(ValueError, match="replay"):
        enterprise_sso.verify_saml_assertion_xml(
            xml_bytes=raw,
            idp_cert_pem=rsa_material["cert_pem"],
            expected_audience=AUDIENCE,
            expected_recipient="https://app.example.com/acs",
            expected_in_response_to="req-1",
        )


def test_saml_rejects_signature_wrapping(rsa_material):  # noqa: ANN001
    """Attacker wraps a signed Assertion and injects a hostile sibling NameID.

    Verification must bind identity to the signed subtree, not the envelope.
    """
    signed = _saml_assertion(
        priv=rsa_material["priv"],
        cert_pem=rsa_material["cert_pem"],
        name_id="victim@example.com",
        assertion_id="_wrap-victim",
    )
    # Build a Response with an unsigned attacker Assertion first, then the
    # signed victim Assertion. Naive parsers that take the first NameID lose.
    response = etree.Element(
        f"{{{SAML_NS}}}Response",
        nsmap={None: SAML_NS},
        ID="_resp",
        Version="2.0",
        IssueInstant="2026-01-01T00:00:00Z",
    )
    attacker = etree.SubElement(response, f"{{{SAML_NS}}}Assertion", ID="_attacker", Version="2.0")
    etree.SubElement(attacker, f"{{{SAML_NS}}}NameID").text = "attacker@evil.example"
    response.append(signed)

    identity = enterprise_sso.verify_saml_assertion_xml(
        xml_bytes=etree.tostring(response),
        idp_cert_pem=rsa_material["cert_pem"],
        expected_audience=AUDIENCE,
        expected_recipient="https://app.example.com/acs",
        expected_in_response_to="req-1",
    )
    assert "attacker" not in identity.subject
    assert "attacker" not in identity.email
    assert "victim" in identity.subject or "victim" in identity.email
