"""The public end of the sign-in flows: the redirect URI vendors call back.

``GET /api/v1/oauth/callback/{provider}`` is the address registered with
each vendor (Google) and the success/reject address handed to WordPress. It
needs no session — the signed, single-use state it carries is the
authorisation — and it always answers with a redirect to the console's own
address, never with an error page or a trace.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.api.deps import DbSession
from app.services import oauth_flow

router = APIRouter(prefix="/oauth", tags=["oauth"])

#: Query parameters the flows read. Anything else a vendor appends is ignored.
_ACCEPTED = (
    "state", "code", "error", "scope",
    # TikTok for Business names the code auth_code.
    "auth_code",
    # WordPress application-password approval.
    "site_url", "user_login", "password", "success", "rejected",
    # Phase 2B: GitHub App installation, Shopify's signed callback.
    "installation_id", "setup_action", "shop", "hmac", "timestamp", "host",
)


@router.get("/callback/{provider}", include_in_schema=False)
def oauth_callback(provider: str, request: Request, db: DbSession) -> RedirectResponse:
    params = {key: request.query_params.get(key, "") for key in _ACCEPTED}
    # The whole query, as sent: Shopify signs every parameter it adds, so
    # its HMAC is checked over all of them, not only the ones read here.
    raw = list(request.query_params.multi_items())
    target = oauth_flow.handle_callback(db, provider, params, raw=raw)
    response = RedirectResponse(target, status_code=303)
    # The URL that reached us can carry a one-time code or a WordPress
    # application password; nothing about it should be cached or leaked
    # onward as a referrer.
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
