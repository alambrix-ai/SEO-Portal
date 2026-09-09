"""Deterministic click-fraud detection.

Most invalid traffic announces itself in the signals: it comes from a
datacentre, it bounces in under a second, the same device fingerprint appears
dozens of times, or the click rate is physically impossible. Deciding those
here — cheaply, repeatably, and with a named reason — is what lets this agent
run continuously and still explain every block it made.

Only sources these rules genuinely cannot settle are escalated to the model.
"""
from __future__ import annotations

from enum import StrEnum

from app.connectors.base.interfaces import TrafficSample

# Used when a channel has no measured cost per click yet.
FALLBACK_CPC = 1.20

# Signal keys safe to persist on a fraud event. Anything not listed — an IP,
# a device id, a user agent — is detection input only and is never stored.
SAFE_SIGNAL_KEYS: frozenset[str] = frozenset(
    {
        "asn_type",
        "is_datacenter",
        "fingerprint_repeat_count",
        "clicks_per_minute",
        "geo_velocity_kmh",
        "ua_family",
        "viewport_area",
        "has_mouse_events",
    }
)


class Verdict(StrEnum):
    CLEAN = "clean"
    UNCERTAIN = "uncertain"
    FRAUD = "fraud"


# Named reasons, matching what the console shows in the flag column.
class Reason:
    NON_HUMAN = "Non-human click pattern"
    IP_FARM = "IP farm cluster detected"
    BOUNCE_SPIKE = "Sub-1s bounce spike"
    BOT_SIGNATURE = "Bot signature match"
    DUPLICATE_FINGERPRINT = "Duplicate device fingerprint"
    GEO_VELOCITY = "Impossible geo velocity"
    DATACENTER = "Datacentre ASN origin"
    CLICK_FLOOD = "Click flooding from one source"


def parse_scope(scope: str) -> frozenset[str]:
    """Operator-supplied always-block sources from the Configure dialog."""
    if not scope:
        return frozenset()
    return frozenset(part.strip() for part in scope.split(",") if part.strip())


def assess(
    sample: TrafficSample, *, always_block: frozenset[str] = frozenset()
) -> tuple[Verdict, float, str]:
    """Return ``(verdict, confidence, reason)`` for one traffic source."""
    if sample.source in always_block:
        return Verdict.FRAUD, 1.0, Reason.NON_HUMAN

    signals = sample.signals or {}

    # ── Hard signals ───────────────────────────────────────────────────────
    if signals.get("is_datacenter") is True or signals.get("asn_type") == "hosting":
        # Real customers do not browse from a hosting provider's network.
        return Verdict.FRAUD, 0.97, Reason.DATACENTER

    repeats = _as_int(signals.get("fingerprint_repeat_count"))
    if repeats >= 25:
        return Verdict.FRAUD, 0.96, Reason.DUPLICATE_FINGERPRINT

    velocity = _as_float(signals.get("geo_velocity_kmh"))
    if velocity >= 1000:
        # Faster than a passenger aircraft between two clicks.
        return Verdict.FRAUD, 0.95, Reason.GEO_VELOCITY

    clicks_per_minute = _as_float(signals.get("clicks_per_minute"))
    if clicks_per_minute >= 30:
        return Verdict.FRAUD, 0.94, Reason.CLICK_FLOOD

    ua_family = str(signals.get("ua_family") or "").lower()
    if ua_family in ("headless", "phantomjs", "selenium", "puppeteer", "curl", "python-requests"):
        return Verdict.FRAUD, 0.98, Reason.BOT_SIGNATURE

    # ── Behavioural signals ────────────────────────────────────────────────
    # A click that never rendered anything: total bounce, no time on site.
    if (
        sample.bounce_rate >= 0.98
        and sample.avg_session_seconds <= 1.0
        and sample.sessions >= 5
    ):
        return Verdict.FRAUD, 0.92, Reason.BOUNCE_SPIKE

    # Volume with uniformly zero engagement, and no mouse activity at all.
    if (
        sample.sessions >= 40
        and sample.bounce_rate >= 0.95
        and signals.get("has_mouse_events") is False
    ):
        return Verdict.FRAUD, 0.9, Reason.NON_HUMAN

    if repeats >= 10 and sample.bounce_rate >= 0.9:
        return Verdict.FRAUD, 0.88, Reason.IP_FARM

    # ── Suspicious but not conclusive ──────────────────────────────────────
    suspicion = 0
    if sample.bounce_rate >= 0.9:
        suspicion += 1
    if sample.avg_session_seconds <= 3.0:
        suspicion += 1
    if repeats >= 5:
        suspicion += 1
    if clicks_per_minute >= 12:
        suspicion += 1
    if _as_float(signals.get("viewport_area")) in (0.0, 1.0):
        # A 1x1 or absent viewport is typical of stacked or hidden ads.
        suspicion += 1

    if suspicion >= 3 and sample.sessions >= 3:
        return Verdict.UNCERTAIN, 0.5, Reason.NON_HUMAN

    return Verdict.CLEAN, 0.0, ""


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
