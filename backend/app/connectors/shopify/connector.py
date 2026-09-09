"""Shopify connector — Admin REST API for pages, products and metafields."""
from __future__ import annotations

import json
import re

from app.connectors.base.connector import Capability, ConnectorSpec, HealthReport
from app.connectors.base.credentials import secret, text
from app.connectors.base.interfaces import CmsConnector, RemotePage
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

API_VERSION = "2025-01"
_TAG_RE = re.compile(r"<[^>]+>")


class ShopifyConnector(CmsConnector):
    spec = ConnectorSpec(
        slug="shopify",
        name="Shopify",
        category="CMS",
        description="Sync storefront pages and product copy through the Admin API.",
        fields=(
            text(
                "storeDomain",
                "Store domain",
                "your-store.myshopify.com",
                help_text=(
                    "The Shopify-assigned .myshopify.com domain, not your "
                    "custom domain — the Admin API only answers on the former."
                ),
            ),
            secret("adminToken", "Admin API access token", "shpat_••••••••"),
        ),
        capabilities=frozenset(
            {
                Capability.LIST_PAGES,
                Capability.READ_PAGE,
                Capability.WRITE_PAGE,
                Capability.INJECT_SCHEMA,
            }
        ),
        requirements=(
            "A custom app installed on the store, with Admin API "
            "access. The storefront token will not work.",
            "read_products and write_products scopes at minimum; "
            "content rewriting also needs read_content and "
            "write_content.",
        ),
        docs_url="https://shopify.dev/docs/api/admin-rest",
    )

    @property
    def base_url(self) -> str:
        domain = self.credentials.get("storeDomain").strip().rstrip("/")
        if not domain:
            return ""
        if not domain.startswith("http"):
            domain = "https://" + domain
        return f"{domain}/admin/api/{API_VERSION}"

    def auth_headers(self) -> dict[str, str]:
        return {
            "X-Shopify-Access-Token": self.credentials.require("adminToken"),
            "Content-Type": "application/json",
        }

    # ── Reads ──────────────────────────────────────────────────────────────
    def list_pages(self, *, limit: int = 100, path_prefix: str = "") -> list[RemotePage]:
        collected: list[RemotePage] = []
        data = self.request("GET", "/pages.json", params={"limit": min(limit, 250)})
        for row in (data or {}).get("pages") or []:
            node = self._page_to_remote(row)
            if not path_prefix or node.url.startswith(path_prefix):
                collected.append(node)

        # Product descriptions are the commercially important copy on a
        # Shopify store, so the pipeline treats them as pages too.
        remaining = limit - len(collected)
        if remaining > 0:
            products = self.request(
                "GET", "/products.json", params={"limit": min(remaining, 250)}
            )
            for row in (products or {}).get("products") or []:
                node = self._product_to_remote(row)
                if not path_prefix or node.url.startswith(path_prefix):
                    collected.append(node)

        return collected[:limit]

    def read_page(self, remote_id: str) -> RemotePage:
        kind, _, raw_id = remote_id.partition(":")
        if kind == "product":
            data = self.request("GET", f"/products/{raw_id}.json")
            return self._product_to_remote((data or {}).get("product") or {})
        page_id = raw_id or remote_id
        data = self.request("GET", f"/pages/{page_id}.json")
        return self._page_to_remote((data or {}).get("page") or {})

    # ── Writes ─────────────────────────────────────────────────────────────
    def write_page(self, remote_id: str, *, body: str, title: str | None = None) -> bool:
        kind, _, raw_id = remote_id.partition(":")
        if kind == "product":
            product: dict = {"id": int(raw_id), "body_html": body}
            if title:
                product["title"] = title
            self.request("PUT", f"/products/{raw_id}.json", json_body={"product": product})
            return True

        page_id = raw_id or remote_id
        page: dict = {"id": int(page_id), "body_html": body}
        if title:
            page["title"] = title
        self.request("PUT", f"/pages/{page_id}.json", json_body={"page": page})
        return True

    def inject_schema(self, remote_id: str, *, json_ld: dict) -> bool:
        """Store the graph as a metafield the theme renders in the head."""
        kind, _, raw_id = remote_id.partition(":")
        self.request(
            "POST",
            "/metafields.json",
            json_body={
                "metafield": {
                    "namespace": "automarket",
                    "key": "json_ld",
                    "type": "json",
                    "value": json.dumps(json_ld),
                    "owner_resource": "product" if kind == "product" else "page",
                    "owner_id": int(raw_id or remote_id),
                }
            },
        )
        return True

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def check_health(self) -> HealthReport:
        try:
            data = self.request("GET", "/shop.json")
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        shop = (data or {}).get("shop") or {}
        name = shop.get("name") or "store"
        return HealthReport(ok=True, detail=f"Connected to {name}", checked_at=utcnow())

    # ── Mapping ────────────────────────────────────────────────────────────
    def _page_to_remote(self, row: dict) -> RemotePage:
        body = row.get("body_html") or ""
        handle = row.get("handle") or ""
        return RemotePage(
            remote_id=str(row.get("id") or ""),
            url=f"/pages/{handle}",
            title=row.get("title") or "",
            body=body,
            word_count=len(_TAG_RE.sub(" ", body).split()),
            updated_at=str(row.get("updated_at") or ""),
            metadata={"handle": handle},
        )

    def _product_to_remote(self, row: dict) -> RemotePage:
        body = row.get("body_html") or ""
        handle = row.get("handle") or ""
        variants = row.get("variants") or []
        price = variants[0].get("price") if variants else ""
        return RemotePage(
            remote_id=f"product:{row.get('id')}",
            url=f"/products/{handle}",
            title=row.get("title") or "",
            body=body,
            word_count=len(_TAG_RE.sub(" ", body).split()),
            schema_types=["Product"],
            updated_at=str(row.get("updated_at") or ""),
            metadata={"price": price, "vendor": row.get("vendor") or ""},
        )


CONNECTOR_CLASS = ShopifyConnector
