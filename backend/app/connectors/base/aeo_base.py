"""Shared behaviour for answer-engine monitoring connectors.

These connectors answer one question: *is this engine citing the customer?*
That is what makes AEO measurable rather than aspirational — the platform
injects Q&A blocks, then checks whether the engines that matter actually
retrieve them.

The method is the same for every engine: put the question to it with search
enabled, then look for the customer's domain among the sources it used. What
differs is the request shape and where the citations live in the response, so
each vendor file supplies only that.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from app.connectors.base.connector import Capability, HealthReport
from app.connectors.base.interfaces import AeoMonitorConnector, CitationHit
from app.core.exceptions import ConnectorConfigError, ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

# Citation checks plus text generation: the same apiKey + model credentials
# power both AEO monitoring and agent writing.
AEO_CAPABILITIES = frozenset({Capability.CHECK_CITATIONS, Capability.COMPLETE})

# Queries per run. Each is a paid model call against a live search tool, so
# the cap is deliberate: citation tracking should cost cents, not dollars.
MAX_QUERIES_PER_CHECK = 6


def normalise_host(url: str) -> str:
    """Bare hostname, lowercased, without ``www.``."""
    if not url:
        return ""
    candidate = url if "//" in url else f"//{url}"
    host = (urlsplit(candidate).netloc or "").lower()
    return host.removeprefix("www.")


def domain_matches(candidate_url: str, domain: str) -> bool:
    """True when a cited URL belongs to the customer's domain."""
    target = normalise_host(domain)
    host = normalise_host(candidate_url)
    if not target or not host:
        return False
    return host == target or host.endswith(f".{target}")


_URL_RE = re.compile(r"https?://[^\s\)\]\"'<>]+")


def urls_in_text(text: str) -> list[str]:
    """Fallback extraction for engines that do not return structured sources."""
    return _URL_RE.findall(text or "")


class BaseAeoConnector(AeoMonitorConnector):
    """Shared citation-matching logic for every answer engine."""

    #: Engine key recorded on generated Q&A pairs.
    engine: str = ""

    def check_citations(self, *, queries: list[str], domain: str) -> list[CitationHit]:
        if not domain:
            return []
        hits: list[CitationHit] = []
        for query in queries[:MAX_QUERIES_PER_CHECK]:
            try:
                sources = self._ask_with_search(query)
            except ConnectorConfigError:
                # Not swallowed. This fails every query identically, so
                # absorbing it would report "no citations found" for what is
                # actually an unfinished setup.
                raise
            except ConnectorError as exc:
                # One failed query should not lose the others' results.
                log.warning("%s citation check failed for %r: %s", self.name, query, exc)
                continue

            for position, url in enumerate(sources, start=1):
                if domain_matches(url, domain):
                    hits.append(
                        CitationHit(
                            engine=self.engine,
                            query=query,
                            cited_url=url,
                            position=position,
                        )
                    )
                    # One hit per query is what the metric counts; ranking
                    # beyond the first citation is not meaningful here.
                    break
        return hits

    def _ask_with_search(self, query: str) -> list[str]:
        """Put ``query`` to the engine and return the URLs it cited, in order."""
        raise NotImplementedError(f"{self.name} does not implement citation checks")

    def check_health(self) -> HealthReport:
        """Cheap credential probe — never a full search.

        Subclasses override ``_probe_credentials``. The old default called
        ``_ask_with_search``, which burned free-tier quota and showed rate-limit
        errors to people simply trying to connect.
        """
        try:
            self._probe_credentials()
        except ConnectorConfigError:
            raise
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        except NotImplementedError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        return HealthReport(ok=True, detail="Connection looks good", checked_at=utcnow())

    def _probe_credentials(self) -> None:
        """Verify the key without doing real citation work. Override per vendor."""
        raise NotImplementedError(
            f"{self.name} needs a connection check before it can be saved"
        )
