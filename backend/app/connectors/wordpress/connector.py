"""WordPress connector — REST API v2 plus a content webhook.

Auth is an application password over Basic, which is what WordPress issues for
programmatic access; it is stored encrypted and sent only over TLS.
"""
from __future__ import annotations

import base64
import re

from app.connectors.base.connector import (
    Capability,
    ConnectorSpec,
    HealthReport,
    OAuthBinding,
)
from app.connectors.base.credentials import secret, text
from app.connectors.base.interfaces import CmsConnector, RemotePage
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_LD_JSON_RE = re.compile(
    r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL
)


class WordPressConnector(CmsConnector):
    spec = ConnectorSpec(
        slug="wordpress",
        name="WordPress",
        category="CMS",
        description="Crawl and write back pages and posts through the WordPress REST API.",
        fields=(
            text("siteUrl", "Site URL", "https://yourdealership.com"),
            text(
                "username",
                "Username",
                "seo-editor",
                help_text=(
                    "The WordPress login name that owns the application "
                    "password — not the display name, and not an email "
                    "address unless that is genuinely the login. Application "
                    "passwords are per-user, so this is not optional, and it "
                    "must be an account that can edit pages."
                ),
            ),
            # WordPress's own name for it, capitalised as WordPress
            # writes it, and generated in six groups of four. Calling it an
            # API key sends people looking for a setting that does not exist.
            secret(
                "applicationPassword",
                "Application Password",
                "abcd EFGH ijkl MNOP qrst UVWX",
                help_text=(
                    "Users → Profile → Application Passwords on the site. "
                    "Paste it with the spaces; they are ignored on the wire."
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
            "A self-hosted WordPress (WordPress 5.6 or newer) — or a "
            "WordPress.com site on the Business plan or higher.",
            "WordPress.com Free, Personal and Premium plans do not expose the "
            "REST API with application passwords, so those cannot be "
            "connected this way.",
            "An application password, created under Users → Profile → "
            "Application Passwords. Your login password will not work.",
            "The site must be served over HTTPS: WordPress refuses "
            "application passwords over plain HTTP.",
            "Optional: to keep structured data out of the page body, register "
            "the automarket_json_ld post meta key with show_in_rest and print it "
            "in the page head. Without that, JSON-LD is added to the page body "
            "as a script tag instead.",
        ),
        docs_url="https://developer.wordpress.org/rest-api/",
        base_url_field="siteUrl",
        # One click: WordPress's own "Authorize application" screen creates
        # the application password and hands it back — nothing to copy.
        oauth=OAuthBinding(
            provider="wordpress",
            platform_fields=frozenset(),
            token_fields=frozenset({"username", "applicationPassword"}),
            access_summary=(
                "WordPress will ask you to approve access for your user. Agents can "
                "then read your pages and posts and save the changes you approve."
            ),
        ),
    )

    # ── Auth ───────────────────────────────────────────────────────────────
    def auth_headers(self) -> dict[str, str]:
        user = self.credentials.require("username")
        token = self.credentials.require("applicationPassword")
        encoded = base64.b64encode(f"{user}:{token}".encode()).decode()
        return {"Authorization": f"Basic {encoded}"}

    # ── Diagnosis ──────────────────────────────────────────────────────────
    def diagnose(self, error: Exception) -> str | None:
        """Explain the failure in terms of what the operator can change.

        WordPress is the connector where this matters most, because the same
        401 means three different things and only one of them is a typo. The
        hosted tiers are the case worth naming outright: no amount of retyping
        a token will make the REST API appear on a Free plan, and the vendor's
        own response does not say so.
        """
        site = (self.credentials.get("siteUrl") or "").lower()
        text = str(error)

        if ".wordpress.com" in site:
            return (
                "This looks like a WordPress.com hosted site. The REST API with "
                "application passwords is only available on the Business plan "
                "and above — the Free, Personal and Premium plans do not expose "
                "it, so no token will work here. Either upgrade the plan, or "
                "connect a self-hosted WordPress instead. If your content is in "
                "a repository rather than a CMS, the GitHub or Bitbucket "
                "connector may be the better fit."
            )
        if site.startswith("http://"):
            return (
                "The site URL is plain HTTP. WordPress refuses application "
                "passwords over an unencrypted connection, and so does this "
                "platform — use the https:// address."
            )
        if "404" in text or "no readable" in text.lower():
            return (
                f"No WordPress REST API answered at {site or 'that address'}. "
                "Check the site URL, and that the REST API has not been "
                "disabled by a security plugin — some hardening plugins turn it "
                "off by default."
            )
        if "401" in text or "403" in text:
            return (
                "WordPress rejected the credentials. Use an application "
                "password (Users → Profile → Application Passwords), not your "
                "login password, and make sure the account it belongs to can "
                "edit pages."
            )
        return None

    # ── Reads ──────────────────────────────────────────────────────────────
    def list_pages(self, *, limit: int = 100, path_prefix: str = "") -> list[RemotePage]:
        collected: list[RemotePage] = []
        # Pages and posts are separate collections in WordPress but the same
        # thing to the SEO pipeline, so both are pulled.
        # WordPress caps per_page at 100, so a site with more than that used
        # to be truncated at its first hundred; the collection is now paged
        # through (X-WP-TotalPages says how far) until the limit is met.
        for endpoint in ("/wp-json/wp/v2/pages", "/wp-json/wp/v2/posts"):
            page = 1
            while len(collected) < limit:
                response = self.request(
                    "GET",
                    endpoint,
                    params={
                        "per_page": 100 if path_prefix else min(limit - len(collected), 100),
                        "page": page,
                        "status": "publish",
                    },
                    return_response=True,
                )
                try:
                    rows = response.json() if response.content else []
                except ValueError as exc:
                    raise ConnectorError(
                        "WordPress sent a response that could not be read. Try again."
                    ) from exc
                for row in rows if isinstance(rows, list) else []:
                    node = self._to_page(row)
                    if path_prefix and not node.url.startswith(path_prefix):
                        continue
                    collected.append(node)
                try:
                    total_pages = int(response.headers.get("X-WP-TotalPages") or 1)
                except ValueError:
                    total_pages = 1
                if page >= total_pages or not rows:
                    break
                page += 1
            if len(collected) >= limit:
                break
        return collected[:limit]

    def read_page(self, remote_id: str) -> RemotePage:
        # A numeric id could be either collection; pages are tried first.
        for endpoint in ("pages", "posts"):
            try:
                row = self.request("GET", f"/wp-json/wp/v2/{endpoint}/{remote_id}")
            except ConnectorError:
                continue
            if row:
                return self._to_page(row)
        raise ConnectorError(f"WordPress has no page or post with id {remote_id}")

    # ── Writes ─────────────────────────────────────────────────────────────
    def write_page(
        self,
        remote_id: str,
        *,
        body: str,
        title: str | None = None,
        meta_description: str | None = None,
    ) -> bool:
        payload: dict = {"content": body}
        if title:
            payload["title"] = title
        if meta_description is not None:
            # Themes and SEO plugins use the excerpt as the fallback description.
            payload["excerpt"] = meta_description
        for endpoint in ("pages", "posts"):
            try:
                self.request("POST", f"/wp-json/wp/v2/{endpoint}/{remote_id}", json_body=payload)
            except ConnectorError:
                continue
            return True
        raise ConnectorError(f"Could not write to WordPress node {remote_id}")

    def inject_schema(self, remote_id: str, *, json_ld: dict) -> bool:
        """Store the graph in the ``automarket_json_ld`` post meta field.

        Requirement: WordPress silently **drops** meta keys that are not
        registered for REST (``register_post_meta('', 'automarket_json_ld',
        ['show_in_rest' => true, 'single' => true, 'type' => 'string'])`` in a
        plugin or the theme), and something must print that meta into the
        page head. The update still answers 200 when the key is dropped, so
        success is judged by whether the key comes back in the response.

        When it does not, this raises NotImplementedError and the caller
        (the knowledge-graph agent) writes the graph into the page body as a
        ``<script type="application/ld+json">`` tag instead — same result for
        a crawler. Before, the 200 was taken as success and the graph went
        nowhere.
        """
        import json

        for endpoint in ("pages", "posts"):
            try:
                row = self.request(
                    "POST",
                    f"/wp-json/wp/v2/{endpoint}/{remote_id}",
                    json_body={"meta": {"automarket_json_ld": json.dumps(json_ld)}},
                )
            except ConnectorError:
                continue
            meta = (row or {}).get("meta") if isinstance(row, dict) else None
            if isinstance(meta, dict) and "automarket_json_ld" in meta:
                return True
            # The node exists, but the meta key is not registered for REST.
            break
        raise NotImplementedError(
            "This WordPress site does not expose the automarket_json_ld meta "
            "field over REST"
        )

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
        """Create a new post as a draft; return ``(post id, link)``.

        ``POST /wp/v2/posts`` with ``status: draft``, so nothing is visible on
        the site until somebody publishes it in WordPress. The meta
        description goes in the excerpt, which themes and SEO plugins use as
        the fallback description. Core WordPress has no SEO-title field, so
        ``meta_title`` is not sent.
        """
        if status != "draft":
            raise ValueError("Only drafts are created; publishing is done in WordPress")
        row = self.request(
            "POST",
            "/wp-json/wp/v2/posts",
            json_body={
                "title": title,
                "content": body_html,
                "slug": slug,
                "excerpt": meta_description,
                "status": "draft",
            },
        )
        post_id = (row or {}).get("id") if isinstance(row, dict) else None
        if not post_id:
            raise ConnectorError("WordPress did not return the new post. Check it in WordPress.")
        return str(post_id), str(row.get("link") or "")

    def register_webhook(self, *, callback_url: str, secret: str) -> bool:
        """Best-effort subscription so edits reach the platform immediately.

        Core WordPress has no webhook API, so this depends on a plugin being
        present; a missing endpoint is not an error worth failing a connect for.
        """
        try:
            self.request(
                "POST",
                "/wp-json/automarket/v1/webhooks",
                json_body={"callback_url": callback_url, "secret": secret, "events": ["post.updated"]},
            )
        except ConnectorError:
            log.info("WordPress site has no webhook endpoint; falling back to polling")
            return False
        return True

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def check_health(self) -> HealthReport:
        try:
            info = self.request("GET", "/wp-json")
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        return HealthReport(
            ok=True,
            detail=f"Connected to {info.get('name') or 'WordPress'}",
            checked_at=utcnow(),
            metadata={"namespaces": len(info.get("namespaces") or [])},
        )

    # ── Live verification ──────────────────────────────────────────────────
    def verify_steps(self) -> list:
        """Read the site and its pages; save, edit and delete one draft post.

        The write step uses the agents' own calls (``create_page`` and
        ``write_page``), on a draft nobody can see, and removes it with
        ``DELETE …?force=true`` so it skips the trash as well.
        """
        from app.connectors.base import verify_helpers as vh

        def site() -> tuple[str, dict]:
            info = self.request("GET", "/wp-json") or {}
            namespaces = info.get("namespaces") or []
            if "wp/v2" not in namespaces:
                raise ConnectorError("The site answered, but its content API (wp/v2) is switched off.")
            return (
                f"Reached the site '{info.get('name') or 'WordPress'}' and its content API.",
                {"namespaces": len(namespaces)},
            )

        def pages() -> tuple[str, dict]:
            found = self.list_pages(limit=5)
            return (
                f"Read {vh.plural(len(found), 'published page or post', 'published pages and posts')}.",
                {"count": len(found), "ids": [p.remote_id for p in found]},
            )

        def draft() -> tuple[str, dict]:
            title = vh.marker("draft")

            def create() -> str:
                post_id, _ = self.create_page(
                    title=title,
                    body_html="<p>Temporary draft made by a connection check. Safe to delete.</p>",
                    slug=vh.slug_marker(),
                    status="draft",
                )
                return post_id

            def check(post_id: str) -> None:
                self.write_page(post_id, body="<p>Edited by the connection check.</p>")
                row = self.request(
                    "GET", f"/wp-json/wp/v2/posts/{post_id}", params={"context": "edit"}
                ) or {}
                if row.get("status") != "draft":
                    raise ConnectorError(f"The post came back as {row.get('status')!r}, not a draft.")

            def delete(post_id: str) -> None:
                self.request("DELETE", f"/wp-json/wp/v2/posts/{post_id}", params={"force": "true"})

            evidence = vh.create_check_delete(what="draft post", create=create, check=check, delete=delete)
            return "Saved a draft post, edited it, and deleted it permanently. Nothing was published.", evidence

        return [
            vh.read_step("site_info", "Reach the site and its content API", site),
            vh.read_step("list_pages", "Read published pages and posts", pages),
            vh.write_step(
                "draft_post_roundtrip",
                "Save a hidden draft post, edit it, then delete it",
                draft,
                requires_test_account=False,
            ),
        ]

    # ── Mapping ────────────────────────────────────────────────────────────
    def _to_page(self, row: dict) -> RemotePage:
        rendered = (row.get("content") or {}).get("rendered") or ""
        title = (row.get("title") or {}).get("rendered") or ""
        link = row.get("link") or ""
        return RemotePage(
            remote_id=str(row.get("id") or ""),
            url=self._path_of(link),
            title=_TAG_RE.sub("", title).strip(),
            body=rendered,
            word_count=len(_TAG_RE.sub(" ", rendered).split()),
            schema_types=self._schema_types(rendered),
            updated_at=str(row.get("modified") or ""),
            metadata={"slug": row.get("slug") or "", "type": row.get("type") or ""},
        )

    @staticmethod
    def _path_of(link: str) -> str:
        """Site-relative path, so URLs are comparable across environments."""
        if not link:
            return "/"
        from urllib.parse import urlsplit

        parts = urlsplit(link)
        return parts.path or "/"

    @staticmethod
    def _schema_types(html: str) -> list[str]:
        import json

        found: list[str] = []
        for block in _LD_JSON_RE.findall(html or ""):
            try:
                graph = json.loads(block)
            except (ValueError, TypeError):
                continue
            candidates = graph if isinstance(graph, list) else [graph]
            for entry in candidates:
                if isinstance(entry, dict) and entry.get("@type"):
                    schema_type = entry["@type"]
                    found.extend(
                        schema_type if isinstance(schema_type, list) else [schema_type]
                    )
        return sorted({str(t) for t in found})


CONNECTOR_CLASS = WordPressConnector
