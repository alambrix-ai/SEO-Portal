"""Prompt for the ambiguous-referrer escalation."""
from __future__ import annotations

SYSTEM = (
    "You classify website referral traffic as spam or legitimate. You are "
    "shown a referring domain and how the sessions from it behaved. Blocking "
    "a real referrer costs the site real visitors, so you only call something "
    "spam when the evidence is clear."
)

SCHEMA_HINT = """{
  "referrer": "the domain assessed",
  "category": "Adult / Porn spam | Ghost referral bot | Spam / link-drop bot | Crypto scam referrer | Scraper / crawler farm",
  "is_spam": true | false,
  "confidence": 0.0-1.0
}"""


def classify_referrer(
    *, referrer: str, sessions: int, bounce_rate: float, avg_seconds: float
) -> str:
    return f"""[TASK:spam_classification]
Classify this referral source.

REFERRER: {referrer}
SESSIONS: {sessions}
BOUNCE_RATE: {bounce_rate:.2f}
AVG_SESSION_SECONDS: {avg_seconds:.1f}

Static rules could not decide this one. Judge from the domain name and the
behavioural signature together. A near-total bounce rate with almost no time
on site suggests traffic that never rendered the page at all."""
