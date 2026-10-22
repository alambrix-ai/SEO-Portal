"""How a search campaign leaves the library: published to Google Ads, or downloaded.

Registered for the ``search_campaign`` kind when this package is imported:

* one **publisher**, routed by the asset's channel. ``google_ads`` creates the
  campaign through the Google Ads integration, paused. ``microsoft_ads`` has
  no publishing path here (those assets are marked export-only), so asking to
  publish one says to upload the bulk file instead;
* three **exporters**: a Google Ads Editor import file, a Microsoft
  Advertising bulk upload file and a plain spreadsheet. JSON and plain text
  come from the library itself.

The content document (written by the agent) is::

    {"name", "goal", "daily_budget", "monthly_budget", "budget_is_recommendation",
     "currency", "locations", "themes", "microsoft_time_zone",
     "ad_groups": [{"name", "theme", "landing_url",
                    "keywords": [{"text", "match_type": "exact|phrase|broad"}],
                    "ad": {"headlines", "descriptions", "path1", "path2"}}],
     "negative_keywords": [{"text", "match_type"}],
     "sitelinks": [{"text", "description1", "description2", "url"}],
     "callouts": [str]}
"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.connectors.base.connector import Capability
from app.connectors.google_ads.connector import RSA_MAX_DESCRIPTIONS, RSA_MAX_HEADLINES
from app.core.exceptions import ConflictError, ConnectorError
from app.models.content import AssetKind, MarketingAsset
from app.models.workspace import Organization
from app.services import assets
from app.services.assets import ExportFile

KIND = AssetKind.SEARCH_CAMPAIGN


def _amount(value: object) -> str:
    try:
        return f"{float(value or 0):.2f}"
    except (TypeError, ValueError):
        return ""


def _groups(content: dict) -> list[dict]:
    return [g for g in content.get("ad_groups") or [] if isinstance(g, dict)]


def _ad(group: dict) -> dict:
    ad = group.get("ad")
    return ad if isinstance(ad, dict) else {}


# ── Publishing ─────────────────────────────────────────────────────────────
@assets.register_publisher(KIND)
def publish(db: Session, org: Organization, asset: MarketingAsset, content: dict) -> None:
    """Create the campaign on the platform the asset is for."""
    if asset.channel != "google_ads":
        raise ConflictError(
            "Microsoft Advertising campaigns are not created directly. Download the "
            "Microsoft Advertising bulk file and upload it under Bulk Operations."
        )
    from app.services.connectors import build_connector_bundle

    connector = build_connector_bundle(db, org).instances.get("google_ads")
    if connector is None or not connector.supports(Capability.CREATE_SEARCH_CAMPAIGN):
        raise ConnectorError(
            "Google Ads is not connected, so this campaign cannot be created. Connect it "
            "under Integrations and approve again, or download the Google Ads Editor file."
        )
    result = connector.create_search_campaign(  # type: ignore[attr-defined]
        name=str(content.get("name") or asset.title),
        daily_budget=float(content.get("daily_budget") or 0),
        ad_groups=_groups(content),
        negative_keywords=list(content.get("negative_keywords") or []),
        sitelinks=list(content.get("sitelinks") or []),
        callouts=list(content.get("callouts") or []),
    )
    asset.remote_id = result.resource_name
    asset.remote_url = f"https://ads.google.com/aw/adgroups?campaignId={result.campaign_id}"
    detail = {**(asset.detail or {}), "extensions_added": result.extensions_added}
    if result.note:
        detail["publish_note"] = result.note
    asset.detail = detail


# ── Google Ads Editor ──────────────────────────────────────────────────────
#: Google Ads Editor import columns (Account → Import → Paste text / From
#: file). Editor reads a row's type from the columns filled in on it:
#: Campaign row  — Campaign, Campaign Type, Networks, Campaign Daily Budget,
#:                 Bid Strategy Type, Campaign Status
#: Ad group row  — Campaign, Ad Group, Max CPC (blank: Maximize clicks sets
#:                 bids), Ad Group Status
#: Keyword row   — Campaign, Ad Group, Keyword, Criterion Type
#:                 (Exact | Phrase | Broad), Status
#: Negative row  — Campaign, Keyword, Criterion Type (Negative Exact |
#:                 Negative Phrase | Negative Broad), Ad Group left blank so
#:                 it applies to the whole campaign
#: Ad row        — Campaign, Ad Group, Ad type (Responsive search ad),
#:                 Headline 1..15, Description 1..4, Path 1, Path 2,
#:                 Final URL, Status
#: Tab-separated UTF-16 with a byte-order mark, which is what Editor expects.
EDITOR_HEADLINES = [f"Headline {i}" for i in range(1, RSA_MAX_HEADLINES + 1)]
EDITOR_DESCRIPTIONS = [f"Description {i}" for i in range(1, RSA_MAX_DESCRIPTIONS + 1)]
EDITOR_COLUMNS = [
    "Campaign",
    "Campaign Type",
    "Networks",
    "Campaign Daily Budget",
    "Bid Strategy Type",
    "Campaign Status",
    "Ad Group",
    "Max CPC",
    "Ad Group Status",
    "Keyword",
    "Criterion Type",
    "Status",
    "Ad type",
    *EDITOR_HEADLINES,
    *EDITOR_DESCRIPTIONS,
    "Path 1",
    "Path 2",
    "Final URL",
]


def _row(columns: list[str], values: dict[str, object]) -> list[object]:
    return [values.get(c, "") for c in columns]


def editor_rows(content: dict) -> list[dict[str, object]]:
    name = str(content.get("name") or "")
    rows: list[dict[str, object]] = [
        {
            "Campaign": name,
            "Campaign Type": "Search",
            "Networks": "Google search",
            "Campaign Daily Budget": _amount(content.get("daily_budget")),
            "Bid Strategy Type": "Maximize clicks",
            "Campaign Status": "Paused",
        }
    ]
    for group in _groups(content):
        rows.append({"Campaign": name, "Ad Group": group.get("name", ""), "Ad Group Status": "Enabled"})
        for keyword in group.get("keywords") or []:
            rows.append(
                {
                    "Campaign": name,
                    "Ad Group": group.get("name", ""),
                    "Keyword": keyword.get("text", ""),
                    "Criterion Type": str(keyword.get("match_type") or "phrase").capitalize(),
                    "Status": "Enabled",
                }
            )
        ad = _ad(group)
        row: dict[str, object] = {
            "Campaign": name,
            "Ad Group": group.get("name", ""),
            "Ad type": "Responsive search ad",
            "Path 1": ad.get("path1", ""),
            "Path 2": ad.get("path2", ""),
            "Final URL": group.get("landing_url", ""),
            "Status": "Enabled",
        }
        row.update(zip(EDITOR_HEADLINES, ad.get("headlines") or [], strict=False))
        row.update(zip(EDITOR_DESCRIPTIONS, ad.get("descriptions") or [], strict=False))
        rows.append(row)
    for keyword in content.get("negative_keywords") or []:
        rows.append(
            {
                "Campaign": name,
                "Keyword": keyword.get("text", ""),
                "Criterion Type": "Negative " + str(keyword.get("match_type") or "phrase").capitalize(),
            }
        )
    return rows


@assets.register_exporter(KIND, "google_ads_editor", "Google Ads Editor (CSV)")
def export_editor(asset: MarketingAsset, content: dict) -> ExportFile:
    rows = [_row(EDITOR_COLUMNS, r) for r in editor_rows(content)]
    return ExportFile(
        f"{assets.slug_for(asset.title)}-google-ads-editor.csv",
        "text/csv; charset=utf-16",
        assets.csv_bytes(EDITOR_COLUMNS, rows, utf16=True),
    )


# ── Microsoft Advertising bulk file ────────────────────────────────────────
#: Microsoft Advertising bulk upload, format version 6.0 (Bulk Operations →
#: Upload). The first record after the header is ``Format Version`` with the
#: version in the Name column. New objects get negative Ids, and children
#: point at them through Parent Id (their names are filled in as well):
#: Campaign                  — Status Paused, Budget (daily), Budget Type
#:                             DailyBudgetStandard, Bid Strategy Type
#:                             MaxClicks, Campaign Type Search, Time Zone
#: Ad Group                  — Status Active, Parent Id = campaign Id
#: Keyword                   — Keyword, Match Type Exact | Phrase | Broad
#: Campaign Negative Keyword — Keyword, Match Type Exact | Phrase (Microsoft
#:                             has no broad negatives; broad becomes phrase)
#: Responsive Search Ad      — Headline and Description as JSON lists of
#:                             {"text": ...}, Path 1, Path 2, Final Url
#: Sitelinks and callouts are not written to this file.
BULK_FORMAT_VERSION = "6.0"
BULK_COLUMNS = [
    "Type",
    "Status",
    "Id",
    "Parent Id",
    "Campaign",
    "Ad Group",
    "Name",
    "Budget",
    "Budget Type",
    "Bid Strategy Type",
    "Campaign Type",
    "Time Zone",
    "Keyword",
    "Match Type",
    "Final Url",
    "Headline",
    "Description",
    "Path 1",
    "Path 2",
]
CAMPAIGN_ID = -1


def bulk_rows(content: dict) -> list[dict[str, object]]:
    name = str(content.get("name") or "")
    rows: list[dict[str, object]] = [
        {"Type": "Format Version", "Name": BULK_FORMAT_VERSION},
        {
            "Type": "Campaign",
            "Status": "Paused",
            "Id": CAMPAIGN_ID,
            "Campaign": name,
            "Budget": _amount(content.get("daily_budget")),
            "Budget Type": "DailyBudgetStandard",
            "Bid Strategy Type": "MaxClicks",
            "Campaign Type": "Search",
            "Time Zone": str(content.get("microsoft_time_zone") or ""),
        },
    ]
    for index, group in enumerate(_groups(content)):
        group_id = -(index + 10)
        group_name = group.get("name", "")
        rows.append(
            {
                "Type": "Ad Group",
                "Status": "Active",
                "Id": group_id,
                "Parent Id": CAMPAIGN_ID,
                "Campaign": name,
                "Ad Group": group_name,
            }
        )
        for keyword in group.get("keywords") or []:
            rows.append(
                {
                    "Type": "Keyword",
                    "Status": "Active",
                    "Parent Id": group_id,
                    "Campaign": name,
                    "Ad Group": group_name,
                    "Keyword": keyword.get("text", ""),
                    "Match Type": str(keyword.get("match_type") or "phrase").capitalize(),
                }
            )
        ad = _ad(group)
        rows.append(
            {
                "Type": "Responsive Search Ad",
                "Status": "Active",
                "Parent Id": group_id,
                "Campaign": name,
                "Ad Group": group_name,
                "Headline": json.dumps([{"text": t} for t in ad.get("headlines") or []], ensure_ascii=False),
                "Description": json.dumps(
                    [{"text": t} for t in ad.get("descriptions") or []], ensure_ascii=False
                ),
                "Path 1": ad.get("path1", ""),
                "Path 2": ad.get("path2", ""),
                "Final Url": group.get("landing_url", ""),
            }
        )
    for keyword in content.get("negative_keywords") or []:
        match = str(keyword.get("match_type") or "phrase").lower()
        rows.append(
            {
                "Type": "Campaign Negative Keyword",
                "Status": "Active",
                "Parent Id": CAMPAIGN_ID,
                "Campaign": name,
                "Keyword": keyword.get("text", ""),
                "Match Type": "Exact" if match == "exact" else "Phrase",
            }
        )
    return rows


@assets.register_exporter(KIND, "microsoft_bulk", "Microsoft Advertising bulk file (CSV)")
def export_bulk(asset: MarketingAsset, content: dict) -> ExportFile:
    rows = [_row(BULK_COLUMNS, r) for r in bulk_rows(content)]
    return ExportFile(
        f"{assets.slug_for(asset.title)}-microsoft-bulk.csv",
        "text/csv; charset=utf-8",
        assets.csv_bytes(BULK_COLUMNS, rows),
    )


# ── Plain spreadsheet ──────────────────────────────────────────────────────
PLAIN_COLUMNS = ["Level", "Ad group", "Item", "Text", "Match type", "Characters", "Landing page"]


def plain_rows(content: dict) -> list[list[object]]:
    budget = _amount(content.get("daily_budget"))
    if content.get("budget_is_recommendation"):
        budget += " (recommended)"
    rows: list[list[object]] = [
        ["Campaign", "", "Name", content.get("name", ""), "", "", ""],
        ["Campaign", "", "Goal", content.get("goal", ""), "", "", ""],
        ["Campaign", "", "Daily budget", budget, "", "", ""],
        ["Campaign", "", "Status", "Paused", "", "", ""],
        ["Campaign", "", "Bidding", "Maximize clicks", "", "", ""],
        ["Campaign", "", "Locations", ", ".join(content.get("locations") or []), "", "", ""],
    ]
    for group in _groups(content):
        name, url = group.get("name", ""), group.get("landing_url", "")
        for keyword in group.get("keywords") or []:
            rows.append(["Ad group", name, "Keyword", keyword.get("text", ""), keyword.get("match_type", ""), "", url])
        ad = _ad(group)
        for number, line in enumerate(ad.get("headlines") or [], start=1):
            rows.append(["Ad group", name, f"Headline {number}", line, "", len(line), url])
        for number, line in enumerate(ad.get("descriptions") or [], start=1):
            rows.append(["Ad group", name, f"Description {number}", line, "", len(line), url])
        for key in ("path1", "path2"):
            if ad.get(key):
                rows.append(["Ad group", name, f"Path {key[-1]}", ad[key], "", len(ad[key]), url])
    for keyword in content.get("negative_keywords") or []:
        rows.append(["Campaign", "", "Negative keyword", keyword.get("text", ""), keyword.get("match_type", ""), "", ""])
    for link in content.get("sitelinks") or []:
        text = " / ".join(t for t in (link.get("text"), link.get("description1"), link.get("description2")) if t)
        rows.append(["Campaign", "", "Sitelink", text, "", len(link.get("text") or ""), link.get("url", "")])
    for callout in content.get("callouts") or []:
        rows.append(["Campaign", "", "Callout", callout, "", len(callout), ""])
    return rows


@assets.register_exporter(KIND, "csv", "Spreadsheet (CSV)")
def export_plain(asset: MarketingAsset, content: dict) -> ExportFile:
    return ExportFile(
        f"{assets.slug_for(asset.title)}.csv",
        "text/csv; charset=utf-8",
        assets.csv_bytes(PLAIN_COLUMNS, plain_rows(content)),
    )
