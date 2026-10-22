"""Shopify connector — Admin REST API for pages, products and metafields."""
from __future__ import annotations

import json
import re

from app.connectors.base import verify_helpers as vh
from app.connectors.base.connector import (
    Capability,
    ConnectorSpec,
    HealthReport,
    OAuthBinding,
    OAuthChoice,
    SignInInput,
)
from app.connectors.base.credentials import from_platform_sign_in, secret, text
from app.connectors.base.interfaces import CmsConnector, RemotePage
from app.connectors.base.oauth import OAuthTokenMixin
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

#: REST Admin API version: the latest stable release (2026-10, supported until
#: 16 October 2027). Shopify supports each version for at least twelve months:
#: https://shopify.dev/docs/api/usage/versioning. The REST Admin API is legacy
#: but still served; the endpoints used here (shop, blogs, articles, pages,
#: products and their metafields) are unchanged in 2026-10.
API_VERSION = "2026-10"
_TAG_RE = re.compile(r"<[^>]+>")


def parse_blogs(data: dict, *, shop_name: str = "") -> list[OAuthChoice]:
    """Where new articles go: each blog on the store (``GET /blogs.json``).

    A store with no blog yet still connects — pages and products work — and
    is offered as one choice that leaves the blog to be created later.
    """
    out = [
        OAuthChoice(
            id=str(row["id"]),
            label=str(row.get("title") or row["id"]),
            detail=f"New articles are saved here as drafts · /blogs/{row.get('handle') or ''}",
            values={"blogId": str(row["id"])},
        )
        for row in data.get("blogs") or []
        if row.get("id")
    ]
    if not out:
        out.append(
            OAuthChoice(
                id="no-blog",
                label=shop_name or "This store",
                detail="No blog yet — pages and products only, until you add one",
                values={"blogId": ""},
            )
        )
    return out


class ShopifyConnector(vh.EnvironmentChoiceMixin, OAuthTokenMixin, CmsConnector):
    #: A free development store is Shopify's test account. It answers on the
    #: same addresses as a real store, so only the live checks read this.
    environments = (vh.TEST_ACCOUNT,)

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
            text(
                "blogId",
                "Blog ID for new articles",
                "123456789",
                required=False,
                help_text=(
                    "Where new articles are saved as drafts. Find it in the address bar "
                    "when you open the blog under Online Store → Blog posts → Manage blogs. "
                    "Leave empty to use the store's first blog."
                ),
            ),
            vh.environment_field(
                allowed=(vh.TEST_ACCOUNT,),
                help_text=(
                    "Choose Test account if this is a free development store, made from a "
                    "Shopify Partners or Dev Dashboard account. Leave it empty for a real store."
                ),
            ),
        ),
        capabilities=frozenset(
            {
                Capability.LIST_PAGES,
                Capability.READ_PAGE,
                Capability.WRITE_PAGE,
                Capability.INJECT_SCHEMA,
                Capability.CREATE_PAGE,
            }
        ),
        requirements=(
            "A custom app installed on the store, with Admin API "
            "access. The storefront token will not work.",
            "read_products and write_products scopes at minimum; "
            "content rewriting also needs read_content and "
            "write_content.",
            "Saving new blog articles needs read_content and write_content, "
            "and at least one blog on the store.",
        ),
        docs_url="https://shopify.dev/docs/api/admin-rest",
        # write_content: pages, blogs, articles and their metafields;
        # write_products: product descriptions and product metafields. Each
        # write scope includes its read scope.
        oauth=OAuthBinding(
            provider="shopify",
            scopes=("write_content", "write_products"),
            platform_fields=frozenset({"adminToken", "storeDomain"}),
            token_fields=frozenset({"adminToken", "accessToken", "refreshToken"}),
            access_summary=(
                "Lets agents read and update your store's pages, blog articles and product "
                "descriptions. New articles are saved as hidden drafts; orders and "
                "customers are not touched."
            ),
            choice_noun="blog",
            choice_fields=("blogId",),
            sign_in_inputs=(
                SignInInput(
                    key="shop",
                    label="Your Shopify store address",
                    placeholder="mystore.myshopify.com",
                    help_text="Shown in Shopify under Settings → Domains.",
                    field="storeDomain",
                ),
            ),
        ),
    )

    @property
    def token_url(self) -> str:  # type: ignore[override]
        """The store's own token endpoint, for refreshing an expiring token."""
        from app.services.oauth_business import SHOP_DOMAIN

        domain = (self.credentials.get("storeDomain") or "").strip().lower()
        return f"https://{domain}/admin/oauth/access_token" if SHOP_DOMAIN.fullmatch(domain) else ""

    @property
    def base_url(self) -> str:
        domain = self.credentials.get("storeDomain").strip().rstrip("/")
        if not domain:
            return ""
        if not domain.startswith("http"):
            domain = "https://" + domain
        return f"{domain}/admin/api/{API_VERSION}"

    def auth_headers(self) -> dict[str, str]:
        pasted = self.credentials.get("adminToken")
        if pasted or not from_platform_sign_in(self.credentials.values):
            token = self.credentials.require("adminToken")
        else:
            token = self.access_token
        return {
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json",
        }

    # ── Sign-in ────────────────────────────────────────────────────────────
    def list_oauth_choices(self) -> list[OAuthChoice]:
        data = self.request("GET", "/blogs.json") or {}
        return parse_blogs(data, shop_name=self._shop_name())

    # ── Reads ──────────────────────────────────────────────────────────────
    def list_pages(self, *, limit: int = 100, path_prefix: str = "") -> list[RemotePage]:
        collected: list[RemotePage] = []
        # With a prefix, rows are filtered after fetching, so pages are
        # fetched at full size and followed until enough match.
        page_size = 250 if path_prefix else max(1, min(limit, 250))
        for row in self._paged_until(
            "/pages.json", "pages", limit=limit, page_size=page_size,
            convert=self._page_to_remote, path_prefix=path_prefix,
        ):
            collected.append(row)

        # Product descriptions are the commercially important copy on a
        # Shopify store, so the pipeline treats them as pages too.
        remaining = limit - len(collected)
        if remaining > 0:
            collected.extend(
                self._paged_until(
                    "/products.json", "products", limit=remaining,
                    page_size=250 if path_prefix else max(1, min(remaining, 250)),
                    convert=self._product_to_remote, path_prefix=path_prefix,
                )
            )
        return collected[:limit]

    def _paged_until(
        self, path: str, key: str, *, limit: int, page_size: int, convert, path_prefix: str  # noqa: ANN001
    ) -> list[RemotePage]:
        out: list[RemotePage] = []
        response = self.request("GET", path, params={"limit": page_size}, return_response=True)
        while True:
            try:
                data = response.json() if response.content else {}
            except ValueError as exc:
                raise ConnectorError(
                    "Shopify sent a response that could not be read. Try again."
                ) from exc
            for row in (data or {}).get(key) or []:
                node = convert(row)
                if not path_prefix or node.url.startswith(path_prefix):
                    out.append(node)
            # Cursor paging: the next page's URL (with page_info) is in the
            # Link header and must be followed exactly as given.
            next_url = (response.links.get("next") or {}).get("url")
            if not next_url or len(out) >= limit:
                return out[:limit]
            response = self.request("GET", next_url, return_response=True)

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
        """Store the graph as the ``automarket.json_ld`` metafield.

        Written on the owning resource's own metafields endpoint — the
        top-level ``/metafields.json`` creates *shop* metafields, so the old
        call never attached the graph to the page. A metafield key is unique
        per resource, so a second POST answers 422; an existing one is found
        first and updated in place instead. The theme must render
        ``{{ page.metafields.automarket.json_ld }}`` (or the product
        equivalent) in the head for it to reach the page.
        """
        kind, _, raw_id = remote_id.partition(":")
        owner = "products" if kind == "product" else "pages"
        owner_id = raw_id if kind in ("product", "page") else remote_id
        value = json.dumps(json_ld)

        existing = self.request(
            "GET",
            f"/{owner}/{owner_id}/metafields.json",
            params={"namespace": "automarket", "key": "json_ld"},
        )
        match = next(
            (
                m
                for m in (existing or {}).get("metafields") or []
                if m.get("namespace") == "automarket" and m.get("key") == "json_ld"
            ),
            None,
        )
        if match and match.get("id"):
            self.request(
                "PUT",
                f"/{owner}/{owner_id}/metafields/{match['id']}.json",
                json_body={"metafield": {"id": match["id"], "value": value, "type": "json"}},
            )
            return True

        self.request(
            "POST",
            f"/{owner}/{owner_id}/metafields.json",
            json_body={
                "metafield": {
                    "namespace": "automarket",
                    "key": "json_ld",
                    "type": "json",
                    "value": value,
                }
            },
        )
        return True

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
        """Create an unpublished blog article; return ``(id, admin link)``.

        ``POST /blogs/{blog_id}/articles.json`` with ``published: false``, so
        it stays hidden until somebody publishes it in Shopify. The blog is
        the configured one, or the store's first. The meta description is
        sent as the article summary; the SEO title is not set (Shopify keeps
        it in a metafield this does not write).
        """
        if status != "draft":
            raise ValueError("Only drafts are created; publishing is done in Shopify")
        blog_id = (self.credentials.get("blogId") or "").strip() or self._first_blog_id()
        article: dict = {
            "title": title,
            "body_html": body_html,
            "handle": slug,
            "published": False,
        }
        author = self._shop_name()
        if author:
            article["author"] = author
        if meta_description:
            from html import escape

            article["summary_html"] = f"<p>{escape(meta_description, quote=False)}</p>"
        data = self.request("POST", f"/blogs/{blog_id}/articles.json", json_body={"article": article})
        row = (data or {}).get("article") or {}
        article_id = row.get("id")
        if not article_id:
            raise ConnectorError("Shopify did not return the new article. Check it in Shopify.")
        return f"article:{article_id}", f"{self._admin_root}/articles/{article_id}"

    @property
    def _admin_root(self) -> str:
        return self.base_url.split("/admin/api/", 1)[0] + "/admin"

    def _first_blog_id(self) -> str:
        data = self.request("GET", "/blogs.json")
        blogs = (data or {}).get("blogs") or []
        if not blogs or not blogs[0].get("id"):
            from app.core.exceptions import ConnectorConfigError

            raise ConnectorConfigError(
                "This Shopify store has no blog yet. Create one under Online Store → "
                "Blog posts, or enter a Blog ID on the Shopify integration."
            )
        return str(blogs[0]["id"])

    def _shop_name(self) -> str:
        try:
            data = self.request("GET", "/shop.json")
        except ConnectorError:
            return ""
        return str(((data or {}).get("shop") or {}).get("name") or "")

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def check_health(self) -> HealthReport:
        try:
            data = self.request("GET", "/shop.json")
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        shop = (data or {}).get("shop") or {}
        name = shop.get("name") or "store"
        return HealthReport(ok=True, detail=f"Connected to {name}", checked_at=utcnow())

    # ── Live checks ────────────────────────────────────────────────────────
    def verify_steps(self) -> list:
        """Read the store, its pages and blogs; save and delete one hidden article.

        The write step is the article writer's own ``create_page`` — an
        article with ``published: false``, which the storefront does not
        show — read back and then deleted.
        """

        def store() -> tuple[str, dict]:
            shop = (self.request("GET", "/shop.json") or {}).get("shop") or {}
            if not shop:
                raise ConnectorError("Shopify answered, but did not describe the store.")
            return f"Reached the store '{shop.get('name') or 'Shopify'}'.", {"shop_id": shop.get("id")}

        def pages() -> tuple[str, dict]:
            found = self.list_pages(limit=5)
            return (
                f"Read {vh.plural(len(found), 'page or product', 'pages and products')}.",
                {"count": len(found), "ids": [p.remote_id for p in found]},
            )

        def one_page() -> tuple[str, dict]:
            found = self.list_pages(limit=1)
            if not found:
                return "The store has no pages or products yet, so there was nothing to open.", {"count": 0}
            page = self.read_page(found[0].remote_id)
            return (
                f"Opened one page and read {vh.plural(page.word_count, 'word')} of its text.",
                {"id": page.remote_id, "words": page.word_count},
            )

        def blogs() -> tuple[str, dict]:
            choices = self.list_oauth_choices()
            real = [c for c in choices if c.values.get("blogId")]
            return (
                f"Found {vh.plural(len(real), 'blog')} where new articles can be saved.",
                {"count": len(real), "ids": [c.id for c in real]},
            )

        def article() -> tuple[str, dict]:
            blog_id = (self.credentials.get("blogId") or "").strip() or self._first_blog_id()
            title = vh.marker("draft article")

            def create() -> str:
                remote_id, _ = self.create_page(
                    title=title,
                    body_html="<p>Temporary hidden article made by a connection check. Safe to delete.</p>",
                    slug=vh.slug_marker(),
                    status="draft",
                )
                return remote_id.partition(":")[2]

            def check(article_id: str) -> None:
                row = (
                    self.request("GET", f"/blogs/{blog_id}/articles/{article_id}.json") or {}
                ).get("article") or {}
                if str(row.get("id")) != article_id:
                    raise ConnectorError("Shopify did not return the article that was just saved.")
                if row.get("published_at"):
                    raise ConnectorError("The article came back as published, not hidden.")

            def delete(article_id: str) -> None:
                self.request("DELETE", f"/blogs/{blog_id}/articles/{article_id}.json")

            evidence = vh.create_check_delete(
                what="hidden article", create=create, check=check, delete=delete
            )
            return (
                "Saved a hidden (unpublished) article, read it back, and deleted it. "
                "Nothing was shown on the store.",
                {**evidence, "blog_id": blog_id},
            )

        return [
            vh.read_step("store_info", "Reach the store", store),
            vh.read_step("list_pages", "Read pages and products", pages),
            vh.read_step("read_page", "Open one page and read its text", one_page),
            vh.read_step("list_blogs", "Read the blogs new articles go to", blogs),
            vh.write_step(
                "hidden_article_roundtrip",
                "Save a hidden article, read it back, then delete it",
                article,
                requires_test_account=False,
            ),
        ]

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
