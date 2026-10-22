"""Live verification of a connector against a real (test or sandbox) account.

The unit tests prove our code matches our reading of each vendor's docs. Only
a live call proves the vendor agrees. This module is the contract every
connector's live check follows; the runner, the stored results and the
"Beta" labels are built on it (see ``app/services/verification.py``).

A connector declares its checks by overriding ``BaseConnector.verify_steps``
and returning ``VerifyStep``s. Each step is one small, safe call:

* ``kind="read"``  — reads something (list accounts, fetch a page). Never
  changes anything at the vendor.
* ``kind="write"`` — creates something harmless and **undoes it in the same
  step** (create a PAUSED campaign, read it back, delete it; save a draft post,
  delete it). A write step must leave the account as it found it, and must
  never start spending money or publish anything publicly.

Steps run in order and stop at the first failure. ``run`` returns a short
plain-language ``detail`` for the report, and may return ``evidence`` (ids of
what was read or created) — never secrets.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

StepKind = Literal["read", "write"]

#: Run-only options (``manage.py verify --set verifyX=...``, the Run dialog's
#: Advanced options) are credential keys with this prefix. They reach the
#: connector in memory for one run and are never saved.
OPTION_PREFIX = "verify"


class VerifySkipped(Exception):
    """Raised by a step's ``run`` when it cannot run *by choice*, not by fault.

    For an opt-in the operator has not given (a paid post without
    ``verifyAllowPaidPost=yes``). The runner records the step as skipped with
    this message, does not count it as a failure, and carries on with the
    next step. The message is shown as-is: plain words, no secrets.
    """


@dataclass(frozen=True, slots=True)
class VerifyStep:
    #: Stable id, e.g. "list_accounts", "create_paused_campaign".
    key: str
    #: Plain words for the report, e.g. "Read the ad accounts this sign-in can use".
    label: str
    kind: StepKind
    #: Does the work. Returns (detail, evidence). Raises on failure — the
    #: runner records the exception's message as the failure reason.
    run: Callable[[], tuple[str, dict[str, Any]]]
    #: Write steps only run when the operator allows writes (``--allow-writes``)
    #: and the connection is marked as a test/sandbox account.
    requires_test_account: bool = False


@dataclass(slots=True)
class StepResult:
    key: str
    label: str
    kind: StepKind
    ok: bool
    skipped: bool = False
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0


@dataclass(slots=True)
class VerifyReport:
    slug: str
    #: "sandbox" | "test_account" | "live"
    environment: str
    ok: bool
    steps: list[StepResult] = field(default_factory=list)
    #: When a step could not be run (no write permission, not a test account).
    note: str = ""
