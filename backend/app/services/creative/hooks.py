"""Creative Studio's hooks into the approvals queue, the content library and history.

Registered on import (the ``creative_studio`` agent package imports this, and
the registry imports every agent package at startup):

* ``assets.register_rerenderer("creative")`` — after "Edit text" has saved
  re-validated copy (``assets.edit_creative_text``), record a
  ``creative_text`` version and redraw the design in a worker once the
  request commits. No model is called.
* ``assets.register_asset_reviser("creative")`` — "Ask for changes" queues a
  revision job carrying the reviewer's feedback, still encrypted; the worker
  decrypts it, fences it as untrusted text and queues the result with
  ``approvals.queue_revision``.
* ``history.register_restorer("creative_text")`` — restoring a text version
  re-validates it against today's rules, saves it and redraws. A design that
  is approved (or exported, publishing or published) is never changed this
  way: that would skip the review. "Ask for changes" makes a new version.
"""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, InvalidInputError, NotFoundError
from app.core.logging import get_logger
from app.models.approval import ApprovalItem
from app.models.content import AssetKind, AssetStatus, MarketingAsset
from app.models.history import SnapshotKind
from app.models.workspace import Organization
from app.services import approvals, assets, history
from app.services.encryption import OrgCipher

log = get_logger(__name__)

#: A restore would change what a reviewer approved (or what has gone out).
_LOCKED = (
    AssetStatus.APPROVED,
    AssetStatus.EXPORTED,
    AssetStatus.PUBLISHING,
    AssetStatus.PUBLISHED,
)
APPROVED_MESSAGE = "This design is approved. Ask for changes to make a new version."


def _claim_hashes(db: Session, org: Organization, asset: MarketingAsset, claim_ids: Any) -> list[str]:  # noqa: ANN401
    """Hashes of the approved claim texts the saved copy points at, as of now."""
    from app.models.creative import Product
    from app.services.creative import pipeline

    ids = [i for i in (claim_ids or []) if isinstance(i, int) and not isinstance(i, bool)]
    product = assets._tenant_row(db, Product, org.id, (asset.detail or {}).get("product_id"))
    approved = list(product.approved_claims or []) if product is not None else []
    return pipeline.claim_hashes(approved, ids)


def _mark_pending(
    db: Session, org: Organization, asset: MarketingAsset, *, claim_hashes: list[str]
) -> None:
    from app.services.creative import pipeline

    asset.detail = {**(asset.detail or {}), "preview_pending": True, "claim_hashes": claim_hashes}
    db.flush()
    pipeline.schedule_rerender(db, org.id, asset.id)


@assets.register_rerenderer(AssetKind.CREATIVE)
def rerender_creative(db: Session, org: Organization, asset: MarketingAsset, content: dict) -> None:
    """Edited text was saved: version it, then redraw in a worker (no model call)."""
    from app.services.creative import pipeline

    pipeline.record_text_version(db, org, asset.id, content, summary="Text edited", agent_slug=None)
    copy = content.get("copy") if isinstance(content, dict) else None
    claim_ids = copy.get("claim_ids") if isinstance(copy, dict) else None
    # The edit was validated against today's claims: record what they say.
    _mark_pending(db, org, asset, claim_hashes=_claim_hashes(db, org, asset, claim_ids))


@assets.register_asset_reviser(AssetKind.CREATIVE)
def revise_creative(
    db: Session,
    org: Organization,
    asset: MarketingAsset,
    item: ApprovalItem,
    request: approvals.RevisionRequest,
) -> approvals.RevisionOutcome:
    """Queue a rewrite of one creative with the reviewer's feedback."""
    from app.services.creative import pipeline

    job = pipeline.create_revision_job(db, org, asset, item, request)
    log.info("Creative revision requested: approval=%s job=%s", item.id, job.id)
    return approvals.RevisionOutcome(
        approval_id=None,
        message="Changes requested. The revised design will appear in your queue in a minute or two.",
    )


@history.register_restorer(SnapshotKind.CREATIVE_TEXT)
def restore_creative_text(
    db: Session, org: Organization, subject_id: str, document: dict[str, Any], *, user: Any  # noqa: ANN401
) -> dict[str, Any]:
    """Write an old text version back to its creative, checked like an edit."""
    from app.models.creative import BrandKit, Product
    from app.services.creative import pipeline
    from app.services.creative.formats import get_format
    from app.services.creative.qa import ProductFacts, check_copy_content
    from app.services.creative.schema import parse_copy

    _ = user  # the history service audits the restore
    asset = assets.get(db, tenant_id=org.id, asset_id=subject_id)
    if asset.kind != AssetKind.CREATIVE:
        raise NotFoundError("That item is no longer in the library")
    if asset.status in _LOCKED:
        raise ConflictError(APPROVED_MESSAGE, details={"reason": "approved"})
    cipher = OrgCipher.for_org(org)
    current = assets.content_of(asset, cipher)
    format_id = document.get("format_id") if isinstance(document, dict) else None
    copy = document.get("copy") if isinstance(document, dict) else None
    if not isinstance(format_id, str) or not isinstance(copy, dict):
        raise InvalidInputError("This version can't be restored because it has no text.")
    if format_id != current.get("format_id"):
        raise ConflictError("This version is for a different size of design, so it can't be restored here.")
    try:
        spec = get_format(format_id)
    except ValueError as exc:
        raise ConflictError("This design's size is no longer available.") from exc

    detail = asset.detail or {}
    product = assets._tenant_row(db, Product, org.id, detail.get("product_id"))
    kit = assets._tenant_row(db, BrandKit, org.id, detail.get("brand_kit_id"))
    approved = list(product.approved_claims or []) if product is not None else []
    fields = {k: copy[k] for k in ("headline", "body", "cta", "claim_ids") if k in copy}
    try:
        parsed = parse_copy(fields, format_id=format_id, approved_claims=approved)
    except ValidationError as exc:
        raise InvalidInputError(assets._validation_message(exc)) from exc
    report = check_copy_content(
        parsed,
        spec=spec,
        facts=ProductFacts.from_product(product) if product is not None else ProductFacts(),
        banned_phrases=(kit.banned_phrases or []) if kit is not None else (),
    )
    if not report.passed:
        first = next(i for i in report.issues if i.severity == "error")
        raise InvalidInputError(first.message, details={"qa": report.to_dict()})

    restored = {**current, "copy": parsed.model_dump()}
    assets.set_content(asset, cipher, restored)
    _mark_pending(db, org, asset, claim_hashes=pipeline.claim_hashes(approved, parsed.claim_ids))
    return {"format_id": format_id, "copy": parsed.model_dump()}


__all__ = ["APPROVED_MESSAGE", "rerender_creative", "restore_creative_text", "revise_creative"]
