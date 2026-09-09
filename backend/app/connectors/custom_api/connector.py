"""Custom API / Webhook connector — for bespoke sites and microservices.

The escape hatch that makes the platform CMS-agnostic. A customer implements
four endpoints against the contract below and the whole SEO pipeline works
against their own stack; no plugin, no vendor.

Requests are signed with an HMAC of the body under the shared secret, in the
``X-AutoMarket-Signature`` header, so the receiving service can verify that a
write genuinely came from this platform and was not replayed or forged.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time

from app.connectors.base.connector import Capability, ConnectorSpec, HealthReport
from app.connectors.base.credentials import secret, text
from app.connectors.base.interfaces import CmsConnector, RemotePage
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")

# The contract a customer implements. Documented here because this is the
# connector whose shape they have to build against.
CONTRACT = """
GET  {base}/pages?limit=&prefix=   -> {"pages": [Page, ...]}
GET  {base}/pages/{id}             -> Page
PUT  {base}/pages/{id}             <- {"body": "...", "title": "..."}
POST {base}/pages/{id}/schema      <- {"json_ld": {...}}
GET  {base}/health                 -> {"ok": true}

Page = {
  "id": "string",           # your identifier, echoed back on writes
  "url": "/path",           # site-relative
  "title": "string",
  "body": "html",
  "schema_types": ["FAQPage", ...],   # optional
  "updated_at": "ISO-8601"            # optional
}
"""


class CustomApiConnector(CmsConnector):
    spec = ConnectorSpec(
        slug="custom_api",
        name="Custom API / Webhook",
        category="CMS",
        description=(
            "Point the platform at your own endpoints. Requests are HMAC-signed "
            "with your shared secret."
        ),
        fields=(
            text("webhookUrl", "Base URL", "https://yourdomain.com/automarket"),
            secret("secret", "Shared secret", "••••••••"),
        ),
        capabilities=frozenset(
            {
                Capability.LIST_PAGES,
                Capability.READ_PAGE,
                Capability.WRITE_PAGE,
                Capability.INJECT_SCHEMA,
            }
        ),
        base_url_field="webhookUrl",
    )

    # ── Signing ────────────────────────────────────────────────────────────
    def _sign(self, body: str) -> dict[str, str]:
        """HMAC-SHA256 over `timestamp.body`, so a capture cannot be replayed."""
        secret_value = self.credentials.require("secret")
        timestamp = str(int(time.time()))
        payload = f"{timestamp}.{body}".encode()
        signature = hmac.new(secret_value.encode(), payload, hashlib.sha256).hexdigest()
        return {
            "X-AutoMarket-Timestamp": timestamp,
            "X-AutoMarket-Signature": f"sha256={signature}",
        }

    def auth_headers(self) -> dict[str, str]:
        # GETs carry a signature over an empty body, so a receiver can verify
        # every request with one code path.
        return self._sign("")

    def _signed_post(self, method: str, path: str, payload: dict) -> object:
        body = json.dumps(payload, separators=(",", ":"))
        return self.request(
            method,
            path,
            json_body=payload,
            headers={**self._sign(body), "Content-Type": "application/json"},
        )

    # ── Reads ──────────────────────────────────────────────────────────────
    def list_pages(self, *, limit: int = 100, path_prefix: str = "") -> list[RemotePage]:
        data = self.request(
            "GET", "/pages", params={"limit": limit, "prefix": path_prefix or ""}
        )
        rows = (data or {}).get("pages") if isinstance(data, dict) else data
        return [self._to_remote(row) for row in (rows or [])][:limit]

    def read_page(self, remote_id: str) -> RemotePage:
        row = self.request("GET", f"/pages/{remote_id}")
        if not row:
            raise ConnectorError(f"No page returned for id {remote_id}")
        return self._to_remote(row if isinstance(row, dict) else {})

    # ── Writes ─────────────────────────────────────────────────────────────
    def write_page(self, remote_id: str, *, body: str, title: str | None = None) -> bool:
        payload: dict = {"body": body}
        if title:
            payload["title"] = title
        self._signed_post("PUT", f"/pages/{remote_id}", payload)
        return True

    def inject_schema(self, remote_id: str, *, json_ld: dict) -> bool:
        self._signed_post("POST", f"/pages/{remote_id}/schema", {"json_ld": json_ld})
        return True

    def register_webhook(self, *, callback_url: str, secret: str) -> bool:
        try:
            self._signed_post(
                "POST",
                "/webhooks",
                {"callback_url": callback_url, "secret": secret, "events": ["page.updated"]},
            )
        except ConnectorError as exc:
            log.info("Custom API declined webhook registration: %s", exc)
            return False
        return True

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def check_health(self) -> HealthReport:
        try:
            data = self.request("GET", "/health")
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        ok = bool((data or {}).get("ok", True))
        return HealthReport(
            ok=ok,
            detail="Endpoint reachable" if ok else "Endpoint reported not ok",
            checked_at=utcnow(),
        )

    # ── Mapping ────────────────────────────────────────────────────────────
    def _to_remote(self, row: dict) -> RemotePage:
        body = row.get("body") or ""
        return RemotePage(
            remote_id=str(row.get("id") or ""),
            url=str(row.get("url") or "/"),
            title=str(row.get("title") or ""),
            body=body,
            word_count=int(row.get("word_count") or len(_TAG_RE.sub(" ", body).split())),
            schema_types=list(row.get("schema_types") or []),
            updated_at=str(row.get("updated_at") or ""),
        )


CONNECTOR_CLASS = CustomApiConnector
