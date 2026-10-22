"""Writing renewed sign-in tokens back to a connection.

Some vendors replace the refresh token on every use: X's are single-use,
Microsoft's and Pinterest's are re-issued with a fresh lifetime each time.
A connector that refreshes must therefore store the new one, or the
connection breaks on its next refresh (X) or when the original lifetime
runs out (Microsoft, Pinterest).

Connectors run without a database session of their own, so this opens a
short one. The write is a compare-and-swap on the refresh token that was
used: if the stored one has changed meanwhile (another worker refreshed
first, or someone reconnected), nothing is overwritten. The row lock waits
a few seconds at most; failing to save is logged, never raised, because the
access token in hand is still good for this run.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, text

from app.connectors.base.credentials import ACCESS_TOKEN_EXPIRY_KEY
from app.core.logging import get_logger

log = get_logger(__name__)


def _record_and_cipher(db, org_id: str, slug: str, *, lock: bool):  # noqa: ANN001, ANN202
    from app.models.connector import ConnectorRecord
    from app.models.workspace import Organization
    from app.services.encryption import OrgCipher

    query = select(ConnectorRecord).where(
        ConnectorRecord.tenant_id == org_id, ConnectorRecord.slug == slug
    )
    if lock:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SET LOCAL lock_timeout = '5s'"))
        query = query.with_for_update()
    record = db.execute(query).scalar_one_or_none()
    org = db.get(Organization, org_id) if record is not None else None
    if record is None or org is None:
        return None, None
    return record, OrgCipher.for_org(org)


def save_rotated(
    org_id: str,
    slug: str,
    *,
    used_refresh: str,
    refresh: str,
    access: str,
    expires_at: float,
) -> bool:
    """Store a renewed refresh token (and its access token). True if saved."""
    from app.db.session import session_scope
    from app.services.encryption import Ctx

    try:
        with session_scope(org_id) as db:
            record, cipher = _record_and_cipher(db, org_id, slug, lock=True)
            if record is None:
                return False
            values = cipher.decrypt_json(
                record.credentials_encrypted, context=Ctx.CONNECTOR_CREDENTIALS, default={}
            ) or {}
            if values.get("refreshToken") != used_refresh:
                return False
            values["refreshToken"] = refresh
            values["accessToken"] = access
            values[ACCESS_TOKEN_EXPIRY_KEY] = str(int(expires_at))
            record.credentials_encrypted = cipher.encrypt_json(
                values, context=Ctx.CONNECTOR_CREDENTIALS
            )
            record.token_expires_at = datetime.fromtimestamp(expires_at, UTC)
            return True
    except Exception as exc:  # noqa: BLE001 - the token in hand still works
        log.warning("%s: renewed sign-in could not be saved (%s)", slug, type(exc).__name__)
        return False


def load_current(org_id: str, slug: str) -> dict[str, str]:
    """The stored refresh token, access token and its expiry. Empty on failure."""
    from app.db.session import session_scope
    from app.services.encryption import Ctx

    try:
        with session_scope(org_id) as db:
            record, cipher = _record_and_cipher(db, org_id, slug, lock=False)
            if record is None:
                return {}
            values = cipher.decrypt_json(
                record.credentials_encrypted, context=Ctx.CONNECTOR_CREDENTIALS, default={}
            ) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("%s: stored sign-in could not be read (%s)", slug, type(exc).__name__)
        return {}
    keys = ("refreshToken", "accessToken", ACCESS_TOKEN_EXPIRY_KEY)
    return {k: str(values[k]) for k in keys if values.get(k)}
