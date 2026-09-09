"""Prompt for the ambiguous-traffic escalation."""
from __future__ import annotations

import json

SYSTEM = (
    "You assess whether paid traffic is invalid. You are given one source and "
    "its behavioural signals. Blocking valid traffic costs the advertiser real "
    "customers, so you only return a fraudulent verdict when the signals "
    "genuinely cannot describe a human visitor. Low engagement alone is not "
    "fraud: plenty of real people bounce."
)

SCHEMA_HINT = """{
  "verdict": "fraudulent | clean",
  "reason": "Non-human click pattern | IP farm cluster detected | Sub-1s bounce spike | Bot signature match | Duplicate device fingerprint | Impossible geo velocity | Datacentre ASN origin | Click flooding from one source",
  "confidence": 0.0-1.0
}"""


def assess_traffic(
    *,
    source: str,
    channel: str,
    sessions: int,
    bounce_rate: float,
    avg_seconds: float,
    signals: dict | None = None,
) -> str:
    extra = (
        "\nSIGNALS:\n" + json.dumps(signals, indent=2, sort_keys=True, default=str)
        if signals
        else ""
    )
    return f"""[TASK:fraud_assessment]
Assess this traffic source.

SOURCE: {source}
CHANNEL: {channel}
SESSIONS: {sessions}
BOUNCE_RATE: {bounce_rate:.2f}
AVG_SESSION_SECONDS: {avg_seconds:.1f}{extra}

Heuristics could not settle this one. Weigh the signals together rather than
individually, and say clean if a plausible human pattern would produce them."""
