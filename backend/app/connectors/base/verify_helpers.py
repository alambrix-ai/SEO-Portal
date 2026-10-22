"""Small shared pieces for writing a connector's live checks.

``base/verification.py`` is the contract; this module only removes the
repetition from the thirty-odd ``verify_steps`` implementations:

* :func:`read_step` / :func:`write_step` build a :class:`VerifyStep`;
* :func:`create_check_delete` is the shape of every write step — make
  something harmless, read it back, and *always* remove it again, even when
  the read-back fails;
* :func:`environment_field` and :class:`EnvironmentChoiceMixin` give a
  connector the optional "Test account" setting the runner and the sandbox
  base-URL switches read through ``account_environment()``.

Nothing here makes a vendor call of its own: every call a step makes goes
through the connector's ``request`` (or its own client, where the connector
already has one), so cassette recording sees exactly what the agents send.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.connectors.base.credentials import CredentialField, text
from app.connectors.base.verification import VerifyStep
from app.core.exceptions import ConnectorConfigError, ConnectorError

#: What every object a write step creates is called, so anybody who finds one
#: left behind knows where it came from and that it is safe to delete.
VERIFY_LABEL = "Synaprank.ai verification"

#: The values ``accountEnvironment`` may hold, as ``account_environment()``
#: returns them.
LIVE, TEST_ACCOUNT, SANDBOX = "live", "test_account", "sandbox"

#: What people actually type, mapped to the stored value.
_ENVIRONMENT_WORDS = {
    "": LIVE,
    "live": LIVE,
    "live account": LIVE,
    "production": LIVE,
    "test": TEST_ACCOUNT,
    "test account": TEST_ACCOUNT,
    "test_account": TEST_ACCOUNT,
    "test-account": TEST_ACCOUNT,
    "testaccount": TEST_ACCOUNT,
    "development store": TEST_ACCOUNT,
    "developer test account": TEST_ACCOUNT,
    "sandbox": SANDBOX,
    "sandbox account": SANDBOX,
}

_ENVIRONMENT_WORDING = {
    LIVE: "Live account",
    TEST_ACCOUNT: "Test account",
    SANDBOX: "Sandbox",
}


def marker(what: str = "") -> str:
    """A unique, recognisable name for something a write step creates."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{VERIFY_LABEL} {what} {stamp}".replace("  ", " ").strip()


def slug_marker(prefix: str = "synaprank-verify") -> str:
    """The same, shaped for a branch name, a handle or a file name."""
    return f"{prefix}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"


def yesterday() -> date:
    return date.today() - timedelta(days=1)


def plural(count: int, noun: str, many: str = "") -> str:
    return f"{count} {noun if count == 1 else (many or noun + 's')}"


def read_step(key: str, label: str, run: Callable[[], tuple[str, dict[str, Any]]]) -> VerifyStep:
    return VerifyStep(key=key, label=label, kind="read", run=run)


def write_step(
    key: str,
    label: str,
    run: Callable[[], tuple[str, dict[str, Any]]],
    *,
    requires_test_account: bool = True,
) -> VerifyStep:
    return VerifyStep(
        key=key, label=label, kind="write", run=run, requires_test_account=requires_test_account
    )


def create_check_delete(
    *,
    what: str,
    create: Callable[[], str],
    check: Callable[[str], Any] | None,
    delete: Callable[[str], Any],
) -> dict[str, Any]:
    """Create one thing, read it back, and delete it — whatever happens.

    ``create`` returns the new object's id. ``check`` (optional) reads it
    back and raises when it is not what was asked for. ``delete`` removes it
    and is attempted even when ``check`` fails, so a failed check never
    leaves anything behind. When the delete itself fails, the error names
    the object so it can be removed by hand.

    Returns evidence: ``{"created_id": ..., "deleted": True}``.
    """
    created = str(create() or "")
    if not created:
        raise ConnectorError(f"The {what} was sent, but no id came back to read or remove it.")
    failure: Exception | None = None
    try:
        if check is not None:
            check(created)
    except Exception as exc:  # noqa: BLE001 - reported after the clean-up
        failure = exc
    try:
        delete(created)
    except Exception as exc:  # noqa: BLE001
        raise ConnectorError(
            f"The test {what} (id {created}) was created but could not be removed: "
            f"{exc} Delete it by hand; it is named '{VERIFY_LABEL}'."
        ) from exc
    if failure is not None:
        raise ConnectorError(
            f"The test {what} was created and then removed, but reading it back "
            f"failed: {failure}"
        ) from failure
    return {"created_id": created, "deleted": True}


def normalise_environment(raw: str, allowed: tuple[str, ...] = (TEST_ACCOUNT, SANDBOX)) -> str:
    """``live``, ``test_account`` or ``sandbox`` from what was typed.

    Anything unrecognised, or not offered by this vendor, counts as live — the
    safe reading, since a live account gets read steps only.
    """
    value = _ENVIRONMENT_WORDS.get(" ".join((raw or "").strip().lower().split()), LIVE)
    return value if value in allowed else LIVE


def environment_field(*, allowed: tuple[str, ...], help_text: str) -> CredentialField:
    """The optional "Test account" setting.

    A text field (the form has no drop-down for ordinary fields) whose
    placeholder names the accepted words; :class:`EnvironmentChoiceMixin`
    reads whatever was typed generously.
    """
    choices = " / ".join(_ENVIRONMENT_WORDING[v] for v in (LIVE, *allowed))
    return text(
        "accountEnvironment",
        "Test account",
        choices,
        required=False,
        help_text=help_text,
    )


class EnvironmentChoiceMixin:
    """``account_environment()`` that accepts the words people type.

    Mix in before the connector base. ``environments`` lists what this vendor
    offers besides a live account.
    """

    environments: tuple[str, ...] = (TEST_ACCOUNT,)

    def account_environment(self) -> str:
        raw = self.credentials.get("accountEnvironment")  # type: ignore[attr-defined]
        return normalise_environment(raw, self.environments)


def refuse_unless_test(connector: Any, why: str) -> None:  # noqa: ANN401
    """Stop a write step on a connection not marked as a test or sandbox."""
    if connector.account_environment() == LIVE:
        raise ConnectorConfigError(
            f"This check only runs on a test or sandbox account: {why} Set "
            "'Test account' on the connection first."
        )
