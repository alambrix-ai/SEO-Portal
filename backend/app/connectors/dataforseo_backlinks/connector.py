"""DataForSEO Backlinks — which sites link to a given domain.

This is the platform's source of real backlink data. Search Console and Bing
Webmaster Tools used to claim the capability, but neither exposes linking
domains through its public API (see the notes in those connectors), so the
competitor monitor was reporting nothing — or the customer's own pages — as
competitor links.

DataForSEO keeps its own crawl of the web's links and answers for *any*
domain, which is what makes a competitor comparison possible: we can ask who
links to the competitor and who links to the customer, and the difference is
the opportunity.

API reference: https://docs.dataforseo.com/v3/backlinks/

* Auth is HTTP Basic with the account's API login and API password.
* Every call is a POST of a JSON *array* of tasks; the response wraps each in
  ``tasks[]`` with its own ``status_code`` (20000 is success), and the data is
  in ``tasks[0].result[0]``.
* ``rank`` / ``domain_from_rank`` are on DataForSEO's default 0–1000 scale.
  They are divided by ten here so they sit in the same 0–100 range as every
  other authority figure the console shows.
* DataForSEO charges for each request, so this connector asks for one task per
  call and never polls.
"""
from __future__ import annotations

import base64
from typing import Any

from app.connectors.base.connector import SetupGuide
from app.connectors.base.connector import Capability, ConnectorSpec, HealthReport
from app.connectors.base.credentials import secret, text
from app.connectors.base.interfaces import AnalyticsConnector
from app.core.exceptions import ConnectorError
from app.core.logging import get_logger
from app.db.base import utcnow

log = get_logger(__name__)

API = "https://api.dataforseo.com/v3"
OK = 20000
#: The live endpoints accept at most this many rows per task.
MAX_LIMIT = 1000


def normalise_target(value: str) -> str:
    """A bare domain, as DataForSEO expects: no scheme, no www, no path."""
    domain = (value or "").strip().lower()
    for prefix in ("https://", "http://"):
        domain = domain.removeprefix(prefix)
    domain = domain.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    domain = domain.split(":", 1)[0].rstrip(".")
    return domain.removeprefix("www.")


def _authority(raw: Any) -> int:  # noqa: ANN401 - vendor JSON
    """DataForSEO's 0–1000 rank folded into 0–100. 0 means not reported."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, round(value / 10)))


def _int(raw: Any) -> int:  # noqa: ANN401 - vendor JSON
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


GUIDE = SetupGuide(
    steps=(
        'Open the API Access page in your DataForSEO dashboard with the button below.',
        'Your API login is the email address shown there. Your API password is shown on the same page — it is not the password you sign in with.',
        'Paste both here.',
    ),
    key_url='https://app.dataforseo.com/api-access',
    key_url_label='Open DataForSEO API Access',
    key_field='password',
    key_hint='Use the API password from the API Access page, not your sign-in password',
)


class DataForSeoBacklinksConnector(AnalyticsConnector):
    spec = ConnectorSpec(
        slug="dataforseo_backlinks",
        guide=GUIDE,
        name="DataForSEO Backlinks",
        category="Analytics & Search",
        description=(
            "Which sites link to you and to your competitors, from DataForSEO's "
            "backlink index."
        ),
        fields=(
            text(
                "login",
                "API login",
                "you@yourcompany.com",
                help_text="Shown under API Access in the DataForSEO dashboard.",
            ),
            secret(
                "password",
                "API password",
                "••••••••",
                help_text=(
                    "The API password from the DataForSEO dashboard — not the "
                    "password you sign in to the dashboard with."
                ),
            ),
        ),
        capabilities=frozenset({Capability.READ_BACKLINKS}),
        requirements=(
            "A paid DataForSEO account with the Backlinks API enabled. "
            "DataForSEO charges for every request; see its Backlinks API pricing.",
            "The account's API login and API password (from API Access in the "
            "DataForSEO dashboard).",
        ),
        docs_url="https://docs.dataforseo.com/v3/backlinks/",
        base_url=API,
    )

    def auth_headers(self) -> dict[str, str]:
        login = self.credentials.require("login")
        password = self.credentials.require("password")
        token = base64.b64encode(f"{login}:{password}".encode()).decode("ascii")
        return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    # ── Envelope ───────────────────────────────────────────────────────────
    def _first_result(self, data: Any, *, what: str) -> dict:  # noqa: ANN401
        """``tasks[0].result[0]``, or a clear error when DataForSEO refused.

        DataForSEO reports most failures with HTTP 200 and a non-20000
        ``status_code``, either on the whole response or on the task, so the
        HTTP status alone would treat "out of credit" as an empty answer.
        """
        if not isinstance(data, dict):
            raise ConnectorError(f"DataForSEO sent an unreadable answer while trying to {what}.")
        self._check_status(data, what=what)
        tasks = data.get("tasks") or []
        if not tasks or not isinstance(tasks[0], dict):
            raise ConnectorError(f"DataForSEO returned no task while trying to {what}.")
        task = tasks[0]
        self._check_status(task, what=what)
        results = task.get("result") or []
        first = results[0] if results else None
        return first if isinstance(first, dict) else {}

    @staticmethod
    def _check_status(block: dict, *, what: str) -> None:
        code = block.get("status_code")
        if code is None or _int(code) == OK:
            return
        message = str(block.get("status_message") or "no reason given").strip()[:200]
        raise ConnectorError(f"DataForSEO could not {what}: {message} (code {code}).")

    # ── Backlinks ──────────────────────────────────────────────────────────
    def read_backlinks(
        self, *, domain: str, limit: int = 50, filters: list | None = None
    ) -> list[dict[str, Any]]:
        """The domains linking to ``domain``, one row per linking domain."""
        target = normalise_target(domain)
        if not target:
            raise ConnectorError("A domain is needed to look up backlinks.")
        task: dict[str, Any] = {
            "target": target,
            "limit": max(1, min(int(limit), MAX_LIMIT)),
            "mode": "one_per_domain",
        }
        if filters:
            task["filters"] = filters

        data = self.request("POST", "/backlinks/backlinks/live", json_body=[task])
        result = self._first_result(data, what=f"read the backlinks of {target}")

        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in result.get("items") or []:
            if not isinstance(item, dict):
                continue
            linking = normalise_target(str(item.get("domain_from") or ""))
            if not linking or linking in seen or linking == target:
                continue
            seen.add(linking)
            out.append(
                {
                    "domain": linking,
                    "authority": _authority(item.get("domain_from_rank")),
                    "anchor": str(item.get("anchor") or ""),
                    "url": str(item.get("url_from") or ""),
                    "url_to": str(item.get("url_to") or ""),
                    "dofollow": bool(item.get("dofollow")),
                    "first_seen": str(item.get("first_seen") or ""),
                }
            )
        return out

    def read_domain_summary(self, *, domain: str) -> dict[str, Any]:
        """The domain's own rank and referring-domain count.

        ``found`` is False when DataForSEO's index has nothing for it — for a
        domain somebody suggested, that usually means it does not exist.
        """
        target = normalise_target(domain)
        if not target:
            raise ConnectorError("A domain is needed to look up its backlink profile.")
        data = self.request("POST", "/backlinks/summary/live", json_body=[{"target": target}])
        result = self._first_result(data, what=f"read the backlink profile of {target}")
        referring = _int(result.get("referring_domains"))
        backlinks = _int(result.get("backlinks"))
        authority = _authority(result.get("rank"))
        return {
            "domain": target,
            "authority": authority,
            "referring_domains": referring,
            "backlinks": backlinks,
            "found": bool(referring or backlinks or authority),
        }

    # ── Live checks ────────────────────────────────────────────────────────
    #: A domain DataForSEO's index always has links for, so the paid check
    #: does not depend on the customer's own site.
    VERIFY_DOMAIN = "dataforseo.com"

    def verify_steps(self) -> list:
        """Read the account (free), then make one backlinks call of a single
        row — the competitor monitor's own call at its smallest size.

        DataForSEO charges per request, so the second step costs a few cents.
        """
        from app.connectors.base import verify_helpers as vh

        def account() -> tuple[str, dict]:
            data = self.request("GET", "/appendix/user_data")
            result = self._first_result(data, what="read the account")
            money = result.get("money") if isinstance(result.get("money"), dict) else {}
            balance = money.get("balance")
            detail = "Signed in to DataForSEO (this call is free)."
            evidence: dict = {"account_found": bool(result)}
            if isinstance(balance, int | float):
                detail += f" Balance ${balance:,.2f}."
                evidence["balance_usd"] = round(float(balance), 2)
            return detail, evidence

        def backlinks() -> tuple[str, dict]:
            rows = self.read_backlinks(domain=self.VERIFY_DOMAIN, limit=1)
            return (
                f"Read {vh.plural(len(rows), 'linking site')} for {self.VERIFY_DOMAIN} from the backlink index.",
                {"count": len(rows), "target": self.VERIFY_DOMAIN},
            )

        return [
            vh.read_step("account", "Read the DataForSEO account and balance (free)", account),
            vh.read_step(
                "backlinks_one_row",
                "Read one backlink of an example domain (paid: about 2-3 US cents)",
                backlinks,
            ),
        ]

    # ── Lifecycle ──────────────────────────────────────────────────────────
    def check_health(self) -> HealthReport:
        try:
            data = self.request("GET", "/appendix/user_data")
            result = self._first_result(data, what="read the account")
        except ConnectorError as exc:
            return HealthReport(ok=False, detail=str(exc), checked_at=utcnow())
        login = str(result.get("login") or self.credentials.get("login"))
        money = result.get("money") if isinstance(result.get("money"), dict) else {}
        balance = money.get("balance")
        detail = f"Signed in as {login}"
        if isinstance(balance, int | float):
            detail += f"; balance ${balance:,.2f}"
        return HealthReport(ok=True, detail=detail, checked_at=utcnow())

    def diagnose(self, error: Exception) -> str | None:
        message = str(error)
        if "401" in message or "40100" in message:
            return (
                "DataForSEO refused the login. Use the API login and API password "
                "from API Access in the DataForSEO dashboard, not your sign-in password."
            )
        if "402" in message or "40200" in message or "Payment" in message:
            return (
                "DataForSEO says the account cannot pay for this request. Add credit "
                "or enable the Backlinks API on the account."
            )
        return None


CONNECTOR_CLASS = DataForSeoBacklinksConnector
