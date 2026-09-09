"""Magento (Adobe Commerce) connector — REST API for CMS pages and products."""
from __future__ import annotations

import re

from app.connectors.base.connector import Capability, ConnectorSpec, HealthReport
from app.connectors.base.credentials import secret, text
from app.connectors.base.interfaces import CmsConnector, RemotePage
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


class MagentoConnector(CmsConnector):
    spec = ConnectorSpec(
        slug="magento",
        name="Magento",
        category="CMS",
        description="Read and write CMS pages on a self-hosted or cloud Magento store.",
        fields=(
            text("baseUrl", "Base URL", "https://store.example.com"),
            secret(
                "accessToken",
                "Access token",
                "••••••••",
                help_text=(
                    "System → Extensions → Integrations: create an "
                    "integration, activate it, and Adobe Commerce shows an "
                    "Access Token once."
                ),
            ),
        ),
        capabilities=frozenset(
            {Capability.LIST_PAGES, Capability.READ_PAGE, Capability.WRITE_PAGE}
        ),
        docs_url="https://developer.adobe.com/commerce/webapi/rest/",
        base_url_field="baseUrl",
    )

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self.credentials.require("accessToken"),
            "Content-Type": "application/json",
        }

    # ── Reads ──────────────────────────────────────────────────────────────
    def list_pages(self, *, limit: int = 100, path_prefix: str = "") -> list[RemotePage]:
        # Magento's search criteria syntax is verbose but it is the only way
        # to page the CMS collection.
        data = self.request(
            "GET",
            "/rest/V1/cmsPage/search",
            params={
                "searchCriteria[pageSize]": min(limit, 100),
                "searchCriteria[currentPage]": 1,
                "searchCriteria[filterGroups][0][filters][0][field]": "is_active",
                "searchCriteria[filterGroups][0][filters][0][value]": 1,
            },
        )
        out: list[RemotePage] = []
        for row in (data or {}).get("items") or []:
            node = self._to_remote(row)
            if not path_prefix or node.url.startswith(path_prefix):
                out.append(node)
        return out[:limit]

    def read_page(self, remote_id: str) -> RemotePage:
        row = self.request("GET", f"/rest/V1/cmsPage/{remote_id}")
        if not row:
            raise ConnectorError(f"Magento has no CMS page with id {remote_id}")
        return self._to_remote(row)

    # ── Writes ─────────────────────────────────────────────────────────────
    def write_page(self, remote_id: str, *, body: str, title: str | None = None) -> bool:
        payload: dict = {"id": int(remote_id), "content": body}
        if title:
            payload["title"] = title
        self.request("PUT", f"/rest/V1/cmsPage/{remote_id}", json_body={"page": payload})
        return True

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def check_health(self) -> HealthReport:
        try:
            info = self.request("GET", "/rest/V1/store/storeViews")
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        count = len(info or [])
        return HealthReport(
            ok=True, detail=f"Connected, {count} store view(s)", checked_at=utcnow()
        )

    # ── Mapping ────────────────────────────────────────────────────────────
    def _to_remote(self, row: dict) -> RemotePage:
        body = row.get("content") or ""
        identifier = row.get("identifier") or ""
        return RemotePage(
            remote_id=str(row.get("id") or ""),
            url="/" + identifier.lstrip("/"),
            title=row.get("title") or identifier,
            body=body,
            word_count=len(_TAG_RE.sub(" ", body).split()),
            updated_at=str(row.get("update_time") or ""),
            metadata={"identifier": identifier, "page_layout": row.get("page_layout") or ""},
        )


CONNECTOR_CLASS = MagentoConnector
