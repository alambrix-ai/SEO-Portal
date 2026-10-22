"""The one Graph API version every Meta connector and the Facebook sign-in use.

Meta Ads, Facebook Pages, Instagram and the Facebook Login dialog all import
it from here, so moving forward is a one-line change. Versions and their end
dates: https://developers.facebook.com/docs/graph-api/changelog (v26.0 was
released on 29 July 2026; Meta has not yet given its end date). The
Marketing API retires versions sooner than the rest of the Graph API — see
https://developers.facebook.com/docs/marketing-api/overview/versioning — so
check that page too when moving this.
"""
from __future__ import annotations

from app.core.config import settings

#: The version this release was built and checked against.
BUILT_FOR_VERSION = "v26.0"
#: The version in use. ``META_GRAPH_API_VERSION`` in the server settings
#: overrides it (then restart), so a version Meta retires can be replaced
#: without a code change.
GRAPH_API_VERSION = (settings.meta_graph_api_version or "").strip() or BUILT_FOR_VERSION
if not GRAPH_API_VERSION.startswith("v"):
    GRAPH_API_VERSION = f"v{GRAPH_API_VERSION}"

#: Versions the Facebook Page and Instagram connect forms used to fill in by
#: themselves (their "Graph API version" field has a default, and defaults
#: are saved with the connection). A connection holding exactly one of these
#: never chose it, so it is read as the current version; anything else typed
#: there is honoured.
PREVIOUS_DEFAULT_VERSIONS = frozenset({"v23.0"})


def effective_version(saved: str) -> str:
    """The version to call with, given what the connection has saved."""
    raw = (saved or "").strip()
    if not raw:
        return GRAPH_API_VERSION
    version = raw if raw.startswith("v") else f"v{raw}"
    return GRAPH_API_VERSION if version in PREVIOUS_DEFAULT_VERSIONS else raw


def version_problem(error: Exception, version: str = "") -> str | None:
    """A plain message when Meta refused the API version itself.

    Graph answers an expired or unknown version with error code 2635 (or a
    message naming the version as deprecated / not supported). Read from the
    redacted vendor text attached to the error by ``BaseConnector.request``.
    """
    body = str(getattr(error, "vendor_body", "") or "").lower()
    if not body:
        return None
    retired = '"code":2635' in body.replace(" ", "") or (
        "version" in body and any(word in body for word in ("deprecated", "no longer", "not supported", "unsupported", "invalid version"))
    )
    if not retired:
        return None
    used = version or GRAPH_API_VERSION
    return (
        f"Meta no longer accepts Graph API {used}. Set META_GRAPH_API_VERSION in the server "
        "settings to the current version listed at "
        "https://developers.facebook.com/docs/graph-api/changelog, then restart."
    )
