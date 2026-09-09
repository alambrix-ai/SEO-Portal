"""A test mailbox.

Sign-in is a one-time code sent by email, which means the test suite has to be
able to read the mail — there is no other way to complete a login, and
deliberately so: nothing in the product can hand back a code, and the stored
row holds only an HMAC.

So this intercepts delivery at the last function before the SMTP socket and
keeps the messages in memory. Installed for every test, so no test can reach a
real mail server even by accident.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services import email as email_service

_DIGITS = re.compile(r"\b(\d{4,10})\b")


@dataclass
class Mailbox:
    sent: list[email_service.Message] = field(default_factory=list)

    def clear(self) -> None:
        self.sent.clear()

    def to(self, address: str) -> list[email_service.Message]:
        return [m for m in self.sent if m.to == address.lower()]

    def latest(self, address: str) -> email_service.Message:
        messages = self.to(address)
        assert messages, f"No mail was sent to {address}"
        return messages[-1]

    def code_for(self, address: str) -> str:
        """The code out of the most recent message to an address.

        Read from the subject line, which is where a recipient sees it too.
        """
        message = self.latest(address)
        match = _DIGITS.search(message.subject)
        assert match, f"No code in the subject {message.subject!r}"
        return match.group(1)

    def count_for(self, address: str) -> int:
        return len(self.to(address))


_current: Mailbox | None = None


def current() -> Mailbox:
    """The mailbox installed for the running test.

    Module-level so a plain helper function — ``register(client)`` in the API
    tests — can read a code without every caller having to pass the fixture
    down. The autouse fixture replaces it per test, so nothing leaks between
    them.
    """
    assert _current is not None, "The mailbox fixture is not installed"
    return _current


def install(monkeypatch) -> Mailbox:  # noqa: ANN001 - pytest's MonkeyPatch
    global _current
    box = Mailbox()

    def capture(message: email_service.Message) -> bool:
        box.sent.append(message)
        return True

    monkeypatch.setattr(email_service, "_send", capture)
    _current = box
    return box
