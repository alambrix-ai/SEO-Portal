"""Replay recorded real vendor responses ("cassettes") against a connector.

A cassette folder (``tests/cassettes/<slug>/``) is written by
``manage.py verify <slug> --workspace <ws> --record`` — see
``docs/verification.md`` and ``app/connectors/base/cassettes.py``. Replaying
it runs the connector's own ``verify_steps()`` offline against what the vendor
really sent, so a change in our code that alters a call is caught here:

* every request must be the next one recorded (method, URL and body; dates
  are masked so "the last 30 days" does not drift), or the test fails naming
  the request it got and the one recorded;
* a step that makes fewer requests than recorded fails too;
* a step that fails on the recorded responses fails.

Use in a test::

    from tests.support.cassettes import replay

    def test_my_connector_against_recorded_reality():
        result = replay("my_connector")
        result.assert_ok()

or with the fixture (``replay_cassette``), which does the same.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.connectors.base import cassettes
from app.connectors.base.cassettes import CassetteMismatch
from app.connectors.base.credentials import Credentials


@dataclass(slots=True)
class ReplayStep:
    key: str
    ok: bool
    detail: str = ""


@dataclass(slots=True)
class ReplayResult:
    slug: str
    steps: list[ReplayStep] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(s.ok for s in self.steps)

    def assert_ok(self) -> None:
        failed = [s for s in self.steps if not s.ok]
        assert self.steps, f"{self.slug}: the cassette has no steps to replay"
        assert not failed, f"{self.slug}: " + "; ".join(f"{s.key}: {s.detail}" for s in failed)


def replay(
    slug_or_folder: str | Path,
    *,
    connector_class: type | None = None,
    directory: Path | None = None,
) -> ReplayResult:
    """Run a connector's recorded verify steps offline, strictly.

    Raises :class:`CassetteMismatch` (an AssertionError) when the connector's
    requests differ from the recording.
    """
    from app.connectors.base import registry

    folder = Path(slug_or_folder)
    if not folder.is_dir():
        folder = (directory or cassettes.DEFAULT_DIR) / str(slug_or_folder)
    cassette = cassettes.load(folder)
    klass = connector_class or registry.get_class(cassette.slug)
    assert klass is not None, f"No connector called {cassette.slug!r} is registered"
    connector = klass(
        Credentials(
            values=cassette.credentials,
            oauth_completed=bool(cassette.meta.get("oauth_completed", True)),
        ),
        org_id="cassette-replay",
    )
    player = cassettes.Player(cassette)
    result = ReplayResult(slug=cassette.slug)
    try:
        with player.active():
            steps = {s.key: s for s in connector.verify_steps()}
            for key in cassette.step_order:
                step = steps.get(key)
                if step is None:
                    raise CassetteMismatch(
                        f"[{cassette.slug}/{key}] The connector no longer has a step called "
                        f"{key!r}. Record the cassette again, or delete {key}.json."
                    )
                with player.step(key):
                    try:
                        detail, _ = step.run()
                        result.steps.append(ReplayStep(key=key, ok=True, detail=str(detail)))
                    except Exception as exc:  # noqa: BLE001
                        if player.errors:
                            break
                        result.steps.append(ReplayStep(key=key, ok=False, detail=str(exc)))
                if player.errors:
                    break
    finally:
        connector.close()
    if player.errors:
        raise CassetteMismatch("\n".join(player.errors))
    return result


@pytest.fixture
def replay_cassette():  # noqa: ANN201
    """``replay_cassette("slug")`` -> :class:`ReplayResult`, asserted ok."""

    def run(slug: str, **kwargs) -> ReplayResult:  # noqa: ANN003
        result = replay(slug, **kwargs)
        result.assert_ok()
        return result

    return run
