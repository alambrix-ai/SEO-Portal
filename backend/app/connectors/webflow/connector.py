"""Webflow connector — CMS collection items via the Data API v2.

Webflow models content as collections of items rather than pages, so a
"page" here is a CMS item; the collection is discovered on first use and
cached for the life of the connector instance.
"""
from __future__ import annotations

import re

from app.connectors.base.connector import (
    Capability,
    ConnectorSpec,
    HealthReport,
    OAuthBinding,
    OAuthChoice,
)
from app.connectors.base.credentials import from_platform_sign_in, secret, text
from app.connectors.base.interfaces import CmsConnector, RemotePage
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
# Field slugs Webflow sites conventionally use for body copy, in preference
# order — a site can name it anything, so several are tried.
_BODY_FIELDS = ("rich-text", "body", "content", "post-body", "description")
#: Bounds on the picker's look-ups (Webflow allows 60 requests a minute on
#: the lower plans).
_MAX_SITES = 10
_MAX_COLLECTIONS_PER_SITE = 10


def article_body_field(collection: dict) -> dict | None:
    """The rich text field new articles are written to, or None.

    Webflow's blog template calls it ``post-body``; otherwise the first
    rich text field in the collection is used.
    """
    rich = [f for f in collection.get("fields") or [] if f.get("type") == "RichText" and f.get("slug")]
    preferred = next((f for f in rich if f.get("slug") == "post-body"), None)
    return preferred or (rich[0] if rich else None)


def site_choices(site: dict, collections: list[dict]) -> list[OAuthChoice]:
    """One choice per collection that can hold articles, for one site.

    ``collections`` are full collection objects (with ``fields``). A site
    with none is still offered, without a place for new articles.
    """
    site_id = str(site.get("id") or "")
    name = str(site.get("displayName") or site.get("shortName") or site_id)
    out: list[OAuthChoice] = []
    for collection in collections:
        field = article_body_field(collection)
        collection_id = str(collection.get("id") or "")
        if field is None or not collection_id:
            continue
        out.append(
            OAuthChoice(
                id=f"{site_id}:{collection_id}",
                label=f"{name} — articles go to “{collection.get('displayName') or collection_id}”",
                detail=f"Article text goes in “{field.get('displayName') or field['slug']}”",
                values={
                    "siteId": site_id,
                    "articlesCollectionId": collection_id,
                    "articlesBodyField": str(field["slug"]),
                },
            )
        )
    if not out and site_id:
        out.append(
            OAuthChoice(
                id=site_id,
                label=name,
                detail="No collection with a rich text field, so new articles are not saved here",
                values={"siteId": site_id, "articlesCollectionId": ""},
            )
        )
    return out


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
            text(
                "articlesCollectionId",
                "Collection ID for new articles",
                "62f1a2b3c4d5e6f7a8b9c0d2",
                required=False,
                help_text=(
                    "The CMS collection new articles are saved to as drafts, usually your "
                    "Blog Posts collection. Its ID is shown in the collection's settings in "
                    "the Webflow Designer. Leave empty if you do not want articles saved here."
                ),
            ),
            text(
                "articlesBodyField",
                "Article body field",
                "post-body",
                required=False,
                default="post-body",
                help_text=(
                    "The slug of the rich text field that holds the article, as shown in "
                    "the collection's field settings. Webflow's blog template calls it "
                    "post-body."
                ),
            ),
        ),
        capabilities=frozenset(
            {
                Capability.LIST_PAGES,
                Capability.READ_PAGE,
                Capability.WRITE_PAGE,
                Capability.CREATE_PAGE,
            }
        ),
        requirements=(
            "A Webflow site on a paid Site plan — the CMS API is not "
            "available on the free plan.",
            "A Site API token with CMS read and write access.",
        ),
        docs_url="https://developers.webflow.com/data/reference",
        base_url="https://api.webflow.com/v2",
        # sites:read lists the sites for the picker; cms:read and cms:write
        # list, read, create and publish collection items.
        oauth=OAuthBinding(
            provider="webflow",
            scopes=("sites:read", "cms:read", "cms:write"),
            platform_fields=frozenset({"siteToken"}),
            token_fields=frozenset({"siteToken", "accessToken"}),
            access_summary=(
                "Lets agents read and update the CMS items on the site you pick, and save "
                "new articles as drafts. Designs and pages outside the CMS are not touched."
            ),
            choice_noun="site",
            choice_fields=("siteId", "articlesCollectionId", "articlesBodyField"),
        ),
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._collections: list[dict] | None = None

    def auth_headers(self) -> dict[str, str]:
        token = self.credentials.get("siteToken")
        if not token and from_platform_sign_in(self.credentials.values):
            token = self.credentials.require("accessToken")
        elif not token:
            token = self.credentials.require("siteToken")
        return {
            "Authorization": "Bearer " + token,
            "accept-version": "2.0.0",
            "Content-Type": "application/json",
        }

    # ── Sign-in ────────────────────────────────────────────────────────────
    def list_oauth_choices(self) -> list[OAuthChoice]:
        """Each authorised site, paired with the collection articles go to."""
        sites = (self.request("GET", "/sites") or {}).get("sites") or []
        choices: list[OAuthChoice] = []
        for site in sites[:_MAX_SITES]:
            site_id = site.get("id")
            if not site_id:
                continue
            listed = (self.request("GET", f"/sites/{site_id}/collections") or {}).get("collections") or []
            detailed = [
                self.request("GET", f"/collections/{c['id']}") or {}
                for c in listed[:_MAX_COLLECTIONS_PER_SITE]
                if c.get("id")
            ]
            choices.extend(site_choices(site, detailed))
        return choices

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
            slug = self._slug_of(collection)
            # Offset paging, 100 at a time (Webflow's maximum), until the
            # collection's reported total or the limit is reached. One page
            # per collection used to be all that was ever read.
            offset = 0
            while len(out) < limit:
                page_size = 100 if path_prefix else min(limit - len(out), 100)
                data = self.request(
                    "GET",
                    f"/collections/{collection_id}/items",
                    params={"limit": page_size, "offset": offset},
                )
                items = (data or {}).get("items") or []
                for row in items:
                    node = self._to_remote(row, collection_slug=slug, collection_id=collection_id)
                    if not path_prefix or node.url.startswith(path_prefix):
                        out.append(node)
                total = ((data or {}).get("pagination") or {}).get("total")
                offset += len(items)
                if not items or (isinstance(total, int) and offset >= total):
                    break
                if total is None and len(items) < page_size:
                    break
        return out[:limit]

    @staticmethod
    def _slug_of(collection: dict) -> str:
        return collection.get("slug") or "items"

    def read_page(self, remote_id: str) -> RemotePage:
        collection_id, _, item_id = remote_id.partition(":")
        if not item_id:
            raise ConnectorError("Webflow ids look like '<collectionId>:<itemId>'")
        row = self.request("GET", f"/collections/{collection_id}/items/{item_id}")
        # Same URL as list_pages gives it: "/<collection slug>/<item slug>".
        # An empty slug here made a re-read page look like a different URL.
        collection = next(
            (c for c in self._site_collections() if c.get("id") == collection_id), {}
        )
        return self._to_remote(
            row or {}, collection_slug=self._slug_of(collection), collection_id=collection_id
        )

    # ── Writes ─────────────────────────────────────────────────────────────
    def write_page(
        self,
        remote_id: str,
        *,
        body: str,
        title: str | None = None,
        meta_description: str | None = None,
    ) -> bool:
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

    def create_page_problem(self) -> str | None:
        """Why ``create_page`` cannot work with this setup, or None."""
        if not (self.credentials.get("articlesCollectionId") or "").strip():
            return (
                "Webflow has no Collection ID for new articles, so it cannot save them. "
                "Add it on the Webflow integration."
            )
        return None

    def create_page(
        self,
        *,
        title: str,
        body_html: str,
        slug: str,
        meta_title: str = "",
        meta_description: str = "",
        status: str = "draft",
    ) -> tuple[str, str]:
        """Create a draft item in the articles collection; return ``(id, "")``.

        ``POST /collections/{id}/items`` with ``isDraft: true``, and never
        published: a draft item is not on the live site until somebody
        publishes it in Webflow, so there is no public link to return. Only
        ``name``, ``slug`` and the configured body field are set, because
        every other field is specific to the collection.
        """
        if status != "draft":
            raise ValueError("Only drafts are created; publishing is done in Webflow")
        problem = self.create_page_problem()
        if problem:
            from app.core.exceptions import ConnectorConfigError

            raise ConnectorConfigError(problem)
        collection_id = self.credentials.get("articlesCollectionId").strip()
        body_field = (self.credentials.get("articlesBodyField") or "").strip() or "post-body"
        row = self.request(
            "POST",
            f"/collections/{collection_id}/items",
            json_body={
                "isArchived": False,
                "isDraft": True,
                "fieldData": {"name": title, "slug": slug, body_field: body_html},
            },
        )
        item_id = (row or {}).get("id") if isinstance(row, dict) else None
        if not item_id:
            raise ConnectorError("Webflow did not return the new item. Check the collection in Webflow.")
        return f"{collection_id}:{item_id}", ""

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

    # ── Live checks ────────────────────────────────────────────────────────
    def verify_steps(self) -> list:
        """Read the site's collections and items; save and delete one draft item.

        The write step is the article writer's own ``create_page`` (an item
        with ``isDraft: true``), read back and deleted. It never publishes:
        a draft item is not on the live site, and the site is not published.
        """
        from app.connectors.base import verify_helpers as vh
        from app.core.exceptions import ConnectorConfigError

        def collections() -> tuple[str, dict]:
            found = self._site_collections()
            return (
                f"Found {vh.plural(len(found), 'CMS collection')} on the site.",
                {"count": len(found), "ids": [c.get("id") for c in found if c.get("id")]},
            )

        def pages() -> tuple[str, dict]:
            found = self.list_pages(limit=5)
            return (
                f"Read {vh.plural(len(found), 'CMS item')}.",
                {"count": len(found), "ids": [p.remote_id for p in found]},
            )

        def one_page() -> tuple[str, dict]:
            found = self.list_pages(limit=1)
            if not found:
                return "The site's collections have no items yet, so there was nothing to open.", {"count": 0}
            page = self.read_page(found[0].remote_id)
            return (
                f"Opened one CMS item and read {vh.plural(page.word_count, 'word')} of its text.",
                {"id": page.remote_id, "words": page.word_count},
            )

        def draft() -> tuple[str, dict]:
            problem = self.create_page_problem()
            if problem:
                raise ConnectorConfigError(problem)
            title = vh.marker("draft item")

            def create() -> str:
                remote_id, _ = self.create_page(
                    title=title,
                    body_html="<p>Temporary draft made by a connection check. Safe to delete.</p>",
                    slug=vh.slug_marker(),
                    status="draft",
                )
                return remote_id

            def check(remote_id: str) -> None:
                collection_id, _, item_id = remote_id.partition(":")
                row = self.request("GET", f"/collections/{collection_id}/items/{item_id}") or {}
                if row.get("isDraft") is not True:
                    raise ConnectorError("The item came back without its draft mark.")
                if (row.get("fieldData") or {}).get("name") != title:
                    raise ConnectorError("The item came back with a different name.")

            def delete(remote_id: str) -> None:
                collection_id, _, item_id = remote_id.partition(":")
                self.request("DELETE", f"/collections/{collection_id}/items/{item_id}")

            evidence = vh.create_check_delete(what="draft item", create=create, check=check, delete=delete)
            return (
                "Saved a draft CMS item, read it back, and deleted it. Nothing was published.",
                evidence,
            )

        return [
            vh.read_step("list_collections", "Read the site's CMS collections", collections),
            vh.read_step("list_pages", "Read CMS items", pages),
            vh.read_step("read_page", "Open one CMS item and read its text", one_page),
            vh.write_step(
                "draft_item_roundtrip",
                "Save a draft CMS item, read it back, then delete it",
                draft,
                requires_test_account=False,
            ),
        ]

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
