"""Site Crawler — the live site, as a crawler sees it.

The universal content source. It reads no source files and knows nothing about
the client's stack, so it works the same for WordPress, Next, Rails, Laravel,
Craft, a static export or something nobody here has heard of.

It exists because reading source files cannot work for a component-based site.
A Next page file is usually a composition — ``<Header /><Hero /><Outcomes />``
— with no copy in it; the words are spread across a dozen components and no
single file is the page. Counting that file's words measures its import block.
The rendered HTML is the only place the page exists as a page.

**It deliberately cannot write.** There is no way to PUT a change back to a
rendered page, and pretending otherwise would be the dishonest kind of
convenience. Writes go through the CMS or the repository connector; this one
supplies the truth about what is published, which is what every analysis in
the platform actually needs.

One finding it can make that nothing else can: if the HTML arriving here is
essentially empty because the content is assembled in the browser, the page is
invisible to crawlers that do not execute JavaScript. That is reported as
exactly that, rather than as thin content — the cause and the fix are
completely different.
"""
from __future__ import annotations

from collections import deque
from urllib.parse import urlsplit

from app.connectors.base import html_pages as hp
from app.connectors.base.connector import Capability, ConnectorSpec, HealthReport
from app.connectors.base.credentials import text
from app.connectors.base.interfaces import CmsConnector, RemotePage
from app.core.config import settings
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)


class SiteCrawlerConnector(CmsConnector):
    spec = ConnectorSpec(
        slug="site_crawler",
        name="Site Crawler",
        category="CMS",
        description=(
            "Reads the live site as a search crawler does. Works with any "
            "stack, and is the only source that sees what is actually "
            "published rather than what the source files say."
        ),
        fields=(
            text("siteUrl", "Site URL", "https://yourcompany.com"),
            text(
                "discovery",
                "Discovery",
                "auto",
                required=False,
                default="auto",
                help_text=(
                    "Leave blank for auto. auto finds the sitemap (listed in robots.txt, or at "
                    "/sitemap.xml) and follows links from the home page when "
                    "there is none. sitemap reads only the sitemap path below. "
                    "crawl always follows internal links from the home page."
                ),
            ),
            text(
                "sitemapPath",
                "Sitemap path",
                "/sitemap.xml",
                required=False,
                help_text=(
                    "Needed only when Discovery is sitemap. With auto, a path "
                    "entered here is tried first."
                ),
            ),
        ),
        capabilities=frozenset({Capability.LIST_PAGES, Capability.READ_PAGE}),
        requirements=(
            "The site must be reachable over HTTPS from this server — a "
            "staging site behind a VPN or basic auth cannot be crawled.",
            "Only the website address is needed: the sitemap is found "
            "automatically, and without one the crawler follows internal "
            "links from the home page.",
            "robots.txt is respected. If it disallows everything, nothing is "
            "crawled — which is itself worth knowing.",
            "It reads the published site and cannot write to it. Pair it with "
            "a CMS or repository connector for the rewrites.",
        ),
        docs_url="",
        base_url_field="siteUrl",
    )

    # ── Configuration ──────────────────────────────────────────────────────
    @property
    def site(self) -> str:
        return self.credentials.require("siteUrl").rstrip("/")

    @property
    def discovery(self) -> str:
        return (self.credentials.get("discovery") or "auto").strip().lower()

    @property
    def max_pages(self) -> int:
        return settings.crawler_max_pages

    def _path_of(self, url: str) -> str:
        return urlsplit(url).path or "/"

    def _fetch(self, url: str) -> tuple[str, int]:
        """Fetch one URL as text, returning ``(body, status)``."""
        path = url[len(self.site) :] if url.startswith(self.site) else url
        body = self.request("GET", path or "/", expect_json=False)
        return (body if isinstance(body, str) else ""), 200

    # ── Discovery ──────────────────────────────────────────────────────────
    def _robots(self) -> list[str]:
        try:
            body, _ = self._fetch(f"{self.site}/robots.txt")
        except ConnectorError:
            # No robots.txt is not an error — it means nothing is disallowed.
            return []
        return hp.disallowed_paths(body)

    def _sitemap_url(self, path: str) -> str:
        return f"{self.site}{path if path.startswith('/') else '/' + path}"

    def _from_sitemap(self) -> list[str]:
        configured = (self.credentials.get("sitemapPath") or "").strip()
        if not configured:
            raise ConnectorConfigError(
                "Enter the sitemap path (for example /sitemap.xml), or set "
                "Discovery to auto."
            )
        found = self._collect_sitemaps([self._sitemap_url(configured)])
        if not found:
            raise ConnectorError(
                f"No usable sitemap at {configured}. Point Sitemap path at the "
                "right file, or switch Discovery to 'auto' or 'crawl' to follow internal "
                "links from the home page instead."
            )
        return found[: self.max_pages]

    def _robots_sitemaps(self) -> list[str]:
        """Sitemaps the site lists in robots.txt (``Sitemap:`` lines)."""
        try:
            body, _ = self._fetch(f"{self.site}/robots.txt")
        except ConnectorError:
            return []
        out: list[str] = []
        for line in body.splitlines():
            key, _, value = line.partition(":")
            if key.strip().lower() == "sitemap":
                url = value.strip()
                if url and hp.same_site(url, self.site):
                    out.append(url)
        return out

    def _from_auto(self) -> list[str]:
        """The sitemap wherever the site says it is, else follow links.

        This is what makes "just the website address" enough: robots.txt
        names the sitemap on most sites, the conventional paths cover most
        of the rest, and a site with none is crawled from its home page.
        """
        configured = (self.credentials.get("sitemapPath") or "").strip()
        candidates = [
            *([self._sitemap_url(configured)] if configured else []),
            *self._robots_sitemaps(),
            *(self._sitemap_url(p) for p in ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml")),
        ]
        for candidate in dict.fromkeys(candidates):
            found = self._collect_sitemaps([candidate])
            if found:
                return found[: self.max_pages]
        return self._from_crawl()

    def _collect_sitemaps(self, starts: list[str]) -> list[str]:
        """Page URLs from these sitemaps, following sitemap indexes."""
        found: list[str] = []
        queue = deque(starts)
        seen_sitemaps: set[str] = set()

        while queue and len(found) < self.max_pages:
            target = queue.popleft()
            if target in seen_sitemaps:
                continue
            seen_sitemaps.add(target)
            try:
                body, _ = self._fetch(target)
            except ConnectorError as exc:
                # A missing or unreachable sitemap is the common case, and the
                # raw 404 says nothing about what to do next. Swallowed here
                # so the message below is the one the operator sees.
                log.info("site_crawler: %s unavailable (%s)", target, exc)
                continue
            locations, is_index = hp.sitemap_urls(body)
            if not locations:
                continue
            if is_index:
                # An index points at more sitemaps, not at pages. Treating its
                # entries as pages is how a crawl ends up auditing nothing.
                queue.extend(locations[: self.max_pages])
                continue
            found.extend(loc for loc in locations if hp.same_site(loc, self.site))
        return found

    def _from_crawl(self) -> list[str]:
        """Breadth-first from the home page, following internal links."""
        disallow = self._robots()
        start = f"{self.site}/"
        seen = {start}
        queue = deque([start])
        pages: list[str] = []

        while queue and len(pages) < self.max_pages:
            url = queue.popleft()
            if not hp.is_allowed(self._path_of(url), disallow):
                continue
            try:
                body, _ = self._fetch(url)
            except ConnectorError as exc:
                log.info("site_crawler: %s unreachable (%s)", url, exc)
                continue
            pages.append(url)
            for href in hp.parse(url, body).links:
                target = hp.absolute(href, base=url)
                if not target or target in seen or not hp.same_site(target, self.site):
                    continue
                seen.add(target)
                queue.append(target)
        return pages

    # ── Reads ──────────────────────────────────────────────────────────────
    def list_pages(self, *, limit: int = 100, path_prefix: str = "") -> list[RemotePage]:
        mode = self.discovery
        if mode == "sitemap":
            urls = self._from_sitemap()
        elif mode == "crawl":
            urls = self._from_crawl()
        else:
            urls = self._from_auto()
        disallow = self._robots()

        pages: list[RemotePage] = []
        for url in urls:
            path = self._path_of(url)
            if path_prefix and not path.startswith(path_prefix):
                continue
            if not hp.is_allowed(path, disallow):
                continue
            pages.append(
                RemotePage(
                    remote_id=url,
                    url=path,
                    # Left for read_page: a listing of two hundred pages would
                    # otherwise be two hundred fetches of the customer's site.
                    title="",
                    metadata={"absolute_url": url, "source": "rendered"},
                )
            )
            if len(pages) >= limit:
                break
        return pages

    def read_page(self, remote_id: str) -> RemotePage:
        body, status = self._fetch(remote_id)
        parsed = hp.parse(remote_id, body, status=status)
        return RemotePage(
            remote_id=remote_id,
            url=self._path_of(remote_id),
            title=parsed.title or (parsed.headings[0] if parsed.headings else ""),
            body=parsed.text,
            word_count=parsed.word_count,
            schema_types=parsed.schema_types,
            metadata={
                "absolute_url": remote_id,
                "source": "rendered",
                "description": parsed.description,
                "canonical": parsed.canonical,
                "headings": parsed.headings,
                "images_without_alt": parsed.images_without_alt,
                "links": parsed.links,
                "robots_noindex": parsed.robots_noindex,
                # The finding nothing else in the platform can make.
                "client_side_rendered": parsed.client_side_rendered,
                # Rendered pages cannot be written back. Carried so an agent
                # knows before it proposes a rewrite it could never apply.
                "writable": False,
            },
        )

    def write_page(self, remote_id: str, *, body: str, title: str | None = None) -> bool:
        raise ConnectorError(
            "The Site Crawler reads the published site and cannot write to it. "
            "Connect the CMS or the repository the site is built from, and the "
            "rewrite will go there."
        )

    # ── Live verification ──────────────────────────────────────────────────
    def verify_steps(self) -> list:
        """One read: fetch the home page and parse it as the agents do."""
        from app.connectors.base.verification import VerifyStep

        def fetch_home() -> tuple[str, dict]:
            body, _ = self._fetch(f"{self.site}/")
            home = hp.parse(self.site, body)
            if not body.strip():
                raise ConnectorError("The home page came back empty.")
            return (
                f"Fetched the home page: {home.word_count} words, "
                f"{len(home.links)} links"
                + (", content assembled in the browser" if home.client_side_rendered else ""),
                {
                    "title": home.title,
                    "words": home.word_count,
                    "links": len(home.links),
                    "client_side_rendered": home.client_side_rendered,
                },
            )

        return [
            VerifyStep(
                key="fetch_home_page",
                label="Fetch and read the site's home page",
                kind="read",
                run=fetch_home,
            )
        ]

    # ── Health ─────────────────────────────────────────────────────────────
    def check_health(self) -> HealthReport:
        try:
            body, _ = self._fetch(f"{self.site}/")
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())

        home = hp.parse(self.site, body)
        if home.client_side_rendered:
            # Reachable, and reporting a real problem with the site rather
            # than with the connection.
            return HealthReport(
                ok=True,
                detail=(
                    "Reachable, but the home page HTML is nearly empty — the "
                    "content is assembled in the browser. Search crawlers that "
                    "do not run JavaScript see almost nothing."
                ),
                checked_at=utcnow(),
                metadata={"client_side_rendered": True},
            )
        return HealthReport(
            ok=True,
            detail=f"Reachable, {home.word_count} words on the home page",
            checked_at=utcnow(),
        )

    def diagnose(self, error: Exception) -> str | None:
        text = str(error)
        if "401" in text or "403" in text:
            return (
                f"{self.site} refused the request. A site behind basic auth, a "
                "VPN or a bot filter cannot be crawled from here — use the CMS "
                "or repository connector instead."
            )
        if "404" in text and "sitemap" in text.lower():
            return (
                "No sitemap was found. Set Sitemap path, or switch Discovery to "
                "'crawl' so links are followed from the home page."
            )
        if "Could not reach" in text:
            return (
                f"{self.site} did not answer. Check the URL includes https:// "
                "and that the site is publicly reachable."
            )
        return None


CONNECTOR_CLASS = SiteCrawlerConnector
