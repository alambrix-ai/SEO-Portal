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
        advanced=True,
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
        page_size = 100 if path_prefix else max(1, min(limit, 100))
        out: list[RemotePage] = []
        current = 1
        seen = 0
        while len(out) < limit:
            data = self.request(
                "GET",
                "/rest/V1/cmsPage/search",
                params={
                    "searchCriteria[pageSize]": page_size,
                    "searchCriteria[currentPage]": current,
                    "searchCriteria[filterGroups][0][filters][0][field]": "is_active",
                    "searchCriteria[filterGroups][0][filters][0][value]": 1,
                },
            )
            items = (data or {}).get("items") or []
            for row in items:
                node = self._to_remote(row)
                if not path_prefix or node.url.startswith(path_prefix):
                    out.append(node)
            seen += len(items)
            try:
                total = int((data or {}).get("total_count") or 0)
            except (TypeError, ValueError):
                total = 0
            # Magento answers a page past the end with the *last* page again
            # rather than an empty one, so total_count is what stops this.
            if not items or seen >= total:
                break
            current += 1
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

    # ── Live checks ────────────────────────────────────────────────────────
    def verify_steps(self) -> list:
        """Read the store views and CMS pages; save and delete one disabled page.

        The write step creates a CMS page with ``active: false`` — a disabled
        page answers 404 on the storefront — reads it back and deletes it.
        ``active`` is the name Magento's REST layer gives the page's
        ``isActive``/``setIsActive`` accessors, in both directions.
        """
        from app.connectors.base import verify_helpers as vh

        def store_views() -> tuple[str, dict]:
            views = self.request("GET", "/rest/V1/store/storeViews") or []
            if not isinstance(views, list):
                raise ConnectorError("Magento answered, but not with a list of store views.")
            return (
                f"Reached the store and found {vh.plural(len(views), 'store view')}.",
                {"count": len(views), "ids": [v.get("id") for v in views if isinstance(v, dict)]},
            )

        def pages() -> tuple[str, dict]:
            found = self.list_pages(limit=5)
            return (
                f"Read {vh.plural(len(found), 'enabled CMS page')}.",
                {"count": len(found), "ids": [p.remote_id for p in found]},
            )

        def one_page() -> tuple[str, dict]:
            found = self.list_pages(limit=1)
            if not found:
                return "The store has no enabled CMS pages, so there was nothing to open.", {"count": 0}
            page = self.read_page(found[0].remote_id)
            return (
                f"Opened one CMS page and read {vh.plural(page.word_count, 'word')} of its text.",
                {"id": page.remote_id, "words": page.word_count},
            )

        def disabled_page() -> tuple[str, dict]:
            title = vh.marker("disabled page")

            def create() -> str:
                row = self.request(
                    "POST",
                    "/rest/V1/cmsPage",
                    json_body={
                        "page": {
                            "identifier": vh.slug_marker(),
                            "title": title,
                            "content": "<p>Temporary disabled page made by a connection check. "
                            "Safe to delete.</p>",
                            "active": False,
                        }
                    },
                ) or {}
                return str(row.get("id") or "")

            def check(page_id: str) -> None:
                row = self.request("GET", f"/rest/V1/cmsPage/{page_id}") or {}
                if str(row.get("id")) != page_id:
                    raise ConnectorError("Magento did not return the page that was just saved.")
                active = row.get("active", row.get("is_active"))
                if active not in (False, 0, "0"):
                    raise ConnectorError("The page came back enabled, not disabled.")

            def delete(page_id: str) -> None:
                self.request("DELETE", f"/rest/V1/cmsPage/{page_id}")

            evidence = vh.create_check_delete(
                what="disabled CMS page", create=create, check=check, delete=delete
            )
            return (
                "Saved a disabled CMS page, read it back, and deleted it. Nothing was shown on the store.",
                evidence,
            )

        return [
            vh.read_step("store_views", "Reach the store and read its store views", store_views),
            vh.read_step("list_pages", "Read enabled CMS pages", pages),
            vh.read_step("read_page", "Open one CMS page and read its text", one_page),
            vh.write_step(
                "disabled_page_roundtrip",
                "Save a disabled CMS page, read it back, then delete it",
                disabled_page,
                requires_test_account=False,
            ),
        ]

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
