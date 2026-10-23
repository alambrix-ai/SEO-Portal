"""Merchant-oriented checks on Creative Studio products + Product JSON-LD."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from app.agents.knowledge_graph_schema import prompts as schema_prompts
from app.core import netguard


@dataclass(frozen=True, slots=True)
class FeedIssue:
    kind: str
    summary: str
    recommendation: str
    patch: dict


def inspect_product(
    *,
    name: str,
    description: str,
    url: str,
    price_text: str,
    approved_claims: list,
) -> list[FeedIssue]:
    """Deterministic feed checks — no invented prices or ratings."""
    issues: list[FeedIssue] = []
    if not (description or "").strip() or len(description.strip()) < 40:
        issues.append(
            FeedIssue(
                kind="missing_description",
                summary="Product description is missing or too short for Merchant feeds",
                recommendation="Add a factual 40+ character description from approved claims only.",
                patch={
                    "description": _description_from_claims(name, approved_claims),
                },
            )
        )
    if not (url or "").strip():
        issues.append(
            FeedIssue(
                kind="missing_url",
                summary="Product has no landing page URL",
                recommendation="Set an https product URL before advertising the item.",
                patch={},
            )
        )
    elif not _safe_https(url):
        issues.append(
            FeedIssue(
                kind="unsafe_url",
                summary="Product URL is not a public https address",
                recommendation="Use an https URL on a public host.",
                patch={},
            )
        )
    if not (price_text or "").strip():
        issues.append(
            FeedIssue(
                kind="missing_price_text",
                summary="No price text on the product",
                recommendation="Add an operator-supplied price string (never invent one).",
                patch={},
            )
        )
    return issues


def build_product_json_ld(
    *,
    name: str,
    description: str,
    url: str,
    price_text: str,
) -> dict:
    """Minimal Product JSON-LD from verified fields only (no ratings)."""
    graph: dict = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": name.strip()[:200],
    }
    if description.strip():
        graph["description"] = description.strip()[:5000]
    if url.strip() and _safe_https(url):
        graph["url"] = url.strip()
    # Offer only when the operator supplied price text — never invent currency amounts.
    if price_text.strip() and url.strip() and _safe_https(url):
        graph["offers"] = {
            "@type": "Offer",
            "url": url.strip(),
            "description": price_text.strip()[:120],
        }
    err = schema_prompts.validate_json_ld(graph, "Product")
    if err:
        raise ValueError(err)
    return graph


def validate_patch(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Patch payload must be an object")
    finding_id = str(payload.get("finding_id") or "")
    product_id = str(payload.get("product_id") or "")
    if not finding_id or not product_id:
        raise ValueError("finding_id and product_id are required")
    patch = payload.get("patch") or {}
    if not isinstance(patch, dict):
        raise ValueError("patch must be an object")
    out = {
        "finding_id": finding_id,
        "product_id": product_id,
        "kind": str(payload.get("kind") or "")[:48],
        "patch": {},
        "json_ld": payload.get("json_ld"),
    }
    if "description" in patch:
        out["patch"]["description"] = str(patch["description"])[:5000]
    if "url" in patch:
        url = str(patch["url"]).strip()
        if url and not _safe_https(url):
            raise ValueError("URL must be public https")
        out["patch"]["url"] = url[:1000]
    if out["json_ld"] is not None:
        if not isinstance(out["json_ld"], dict):
            raise ValueError("json_ld must be an object")
        err = schema_prompts.validate_json_ld(out["json_ld"], "Product")
        if err:
            raise ValueError(err)
    return out


def _description_from_claims(name: str, claims: list) -> str:
    parts = [str(c).strip() for c in (claims or []) if str(c).strip()]
    text = f"{name.strip()}. " + " ".join(parts)
    text = text.strip()
    if len(text) < 40:
        text = (
            f"{name.strip()} is listed in the workspace product catalogue "
            "with operator-approved details pending expansion."
        )
    return text[:5000]


def _safe_https(url: str) -> bool:
    parts = urlsplit((url or "").strip())
    if parts.scheme.lower() != "https" or not parts.hostname:
        return False
    try:
        netguard.check_public_url(url.strip())
        return True
    except Exception:  # noqa: BLE001
        return False
