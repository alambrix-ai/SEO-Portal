"""Webflow connector — CMS collection items via the Data API v2.

Webflow models content as collections of items rather than pages, so a
"page" here is a CMS item; the collection is discovered on first use and
cached for the life of the connector instance.
"""
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
# Field slugs Webflow sites conventionally use for body copy, in preference
# order — a site can name it anything, so several are tried.
_BODY_FIELDS = ("rich-text", "body", "content", "post-body", "description")


class WebflowConnector(CmsConnector):
    spec = ConnectorSpec(
        slug="webflow",
        name="Webflow",
        category="CMS",
        description="Sync CMS collection items through the Webflow Data API.",
        fields=(
            text("siteId", "Site ID", "62f1a2b3c4d5e6f7a8b9c0d1"),
            secret(
                "siteToken",
                "Site token",
                "••••••••",
                help_text=(
                    "Site settings → Apps & integrations → API access. A "
                    "workspace token is a different object and will not "
                    "authorise site content."
                ),
            ),
        ),
        capabilities=frozenset(
            {Capability.LIST_PAGES, Capability.READ_PAGE, Capability.WRITE_PAGE}
        ),
        requirements=(
            "A Webflow site on a paid Site plan — the CMS API is not "
            "available on the free plan.",
            "A Site API token with CMS read and write access.",
        ),
        docs_url="https://developers.webflow.com/data/reference",
        base_url="https://api.webflow.com/v2",
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._collections: list[dict] | None = None

    def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self.credentials.require("siteToken"),
            "accept-version": "2.0.0",
            "Content-Type": "application/json",
        }

    # ── Collections ────────────────────────────────────────────────────────
    def _site_collections(self) -> list[dict]:
        if self._collections is None:
            site_id = self.credentials.require("siteId")
            data = self.request("GET", f"/sites/{site_id}/collections")
            self._collections = (data or {}).get("collections") or []
        return self._collections

    # ── Reads ──────────────────────────────────────────────────────────────
    def list_pages(self, *, limit: int = 100, path_prefix: str = "") -> list[RemotePage]:
        out: list[RemotePage] = []
        for collection in self._site_collections():
            if len(out) >= limit:
                break
            collection_id = collection.get("id")
            if not collection_id:
                continue
            data = self.request(
                "GET",
                f"/collections/{collection_id}/items",
                params={"limit": min(limit - len(out), 100)},
            )
            slug = collection.get("slug") or "items"
            for row in (data or {}).get("items") or []:
                node = self._to_remote(row, collection_slug=slug, collection_id=collection_id)
                if not path_prefix or node.url.startswith(path_prefix):
                    out.append(node)
        return out[:limit]

    def read_page(self, remote_id: str) -> RemotePage:
        collection_id, _, item_id = remote_id.partition(":")
        if not item_id:
            raise ConnectorError("Webflow ids look like '<collectionId>:<itemId>'")
        row = self.request("GET", f"/collections/{collection_id}/items/{item_id}")
        return self._to_remote(row or {}, collection_slug="", collection_id=collection_id)

    # ── Writes ─────────────────────────────────────────────────────────────
    def write_page(self, remote_id: str, *, body: str, title: str | None = None) -> bool:
        collection_id, _, item_id = remote_id.partition(":")
        if not item_id:
            raise ConnectorError("Webflow ids look like '<collectionId>:<itemId>'")

        current = self.request("GET", f"/collections/{collection_id}/items/{item_id}")
        field_data = dict((current or {}).get("fieldData") or {})
        target_field = next((f for f in _BODY_FIELDS if f in field_data), _BODY_FIELDS[0])
        field_data[target_field] = body
        if title:
            field_data["name"] = title

        self.request(
            "PATCH",
            f"/collections/{collection_id}/items/{item_id}",
            json_body={"fieldData": field_data},
        )
        # An item edit is only visible once the site is published again.
        self._publish(collection_id, item_id)
        return True

    def _publish(self, collection_id: str, item_id: str) -> None:
        try:
            self.request(
                "POST",
                f"/collections/{collection_id}/items/publish",
                json_body={"itemIds": [item_id]},
            )
        except ConnectorError as exc:
            # The write succeeded; publishing may be restricted by plan.
            log.warning("Webflow item saved but not published: %s", exc)

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def check_health(self) -> HealthReport:
        try:
            collections = self._site_collections()
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        return HealthReport(
            ok=True,
            detail=f"Connected, {len(collections)} collection(s)",
            checked_at=utcnow(),
        )

    # ── Mapping ────────────────────────────────────────────────────────────
    def _to_remote(
        self, row: dict, *, collection_slug: str, collection_id: str
    ) -> RemotePage:
        field_data = row.get("fieldData") or {}
        body = next((field_data[f] for f in _BODY_FIELDS if field_data.get(f)), "")
        slug = field_data.get("slug") or ""
        prefix = f"/{collection_slug}" if collection_slug else ""
        return RemotePage(
            remote_id=f"{collection_id}:{row.get('id')}",
            url=f"{prefix}/{slug}".replace("//", "/"),
            title=field_data.get("name") or slug,
            body=body,
            word_count=len(_TAG_RE.sub(" ", body).split()),
            updated_at=str(row.get("lastUpdated") or ""),
            metadata={"collection": collection_slug},
        )


CONNECTOR_CLASS = WebflowConnector
