"""Google Gemini connector — citation tracking via grounded generation.

Gemini's grounding metadata names the web sources behind an answer, which is
what makes AI Overviews-style visibility measurable.
"""
from __future__ import annotations

from app.connectors.base.aeo_base import AEO_CAPABILITIES, BaseAeoConnector, urls_in_text
from app.connectors.base.connector import ConnectorSpec, HealthReport
from app.connectors.base.credentials import secret
from app.connectors.base.llm_fields import gemini_llm_fields
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)


class GoogleGeminiConnector(BaseAeoConnector):
    engine = "gemini"

    spec = ConnectorSpec(
        slug="google_gemini",
        name="Google Gemini",
        category="AI models",
        description="Writing for agents, and citation checks in Gemini answers.",
        fields=(
            secret("apiKey", "API key", "AIza••••••••"),
            *gemini_llm_fields(),
        ),
        capabilities=AEO_CAPABILITIES,
        docs_url="https://ai.google.dev/gemini-api/docs",
        base_url="https://generativelanguage.googleapis.com/v1beta",
    )

    def auth_headers(self) -> dict[str, str]:
        return {
            # Gemini takes the key in a header rather than a query parameter,
            # which keeps it out of request logs and proxies.
            "x-goog-api-key": self.credentials.require("apiKey"),
            "Content-Type": "application/json",
        }

    def _probe_credentials(self) -> None:
        model = self.credentials.require("model")
        self.request("GET", f"/models/{model}")

    def check_health(self) -> HealthReport:
        try:
            self._probe_credentials()
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        except Exception as exc:  # noqa: BLE001 - a probe must not raise
            log.warning("Gemini health probe failed: %s", exc)
            return HealthReport(
                ok=False,
                detail="Could not verify those credentials. Check the key and model name.",
                checked_at=utcnow(),
            )
        return HealthReport(ok=True, detail="Connection looks good", checked_at=utcnow())

    def _ask_with_search(self, query: str) -> list[str]:
        model = self.credentials.require("model")
        data = self.request(
            "POST",
            f"/models/{model}:generateContent",
            json_body={
                "contents": [{"role": "user", "parts": [{"text": query}]}],
                # Grounding is what produces the citation metadata.
                "tools": [{"google_search": {}}],
                "generationConfig": {"maxOutputTokens": 400},
            },
        )
        payload = data if isinstance(data, dict) else {}
        candidates = payload.get("candidates") or []
        if not candidates:
            return []

        candidate = candidates[0] or {}
        grounding = candidate.get("groundingMetadata") or {}
        urls: list[str] = []

        for chunk in grounding.get("groundingChunks") or []:
            web = (chunk or {}).get("web") or {}
            # `uri` is a redirect wrapper; the actual domain is in `title`
            # when Gemini resolves it, so both are considered.
            for key in ("uri", "title"):
                value = web.get(key)
                if value and ("." in str(value)):
                    urls.append(str(value))
                    break

        if urls:
            seen: set[str] = set()
            return [u for u in urls if not (u in seen or seen.add(u))]

        text = "".join(
            part.get("text") or ""
            for part in ((candidate.get("content") or {}).get("parts") or [])
        )
        return urls_in_text(text)


CONNECTOR_CLASS = GoogleGeminiConnector
