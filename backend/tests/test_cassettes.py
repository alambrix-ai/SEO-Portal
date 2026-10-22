"""Every recorded cassette replays cleanly against today's connector code.

Auto-discovers ``tests/cassettes/<slug>/`` folders (each written by
``manage.py verify <slug> --workspace <ws> --record``) and replays the
connector's ``verify_steps()`` against them, strictly. Passes trivially while
no cassettes exist.

To add one: connect the connector to a free test account in a workspace,
then run ``python manage.py verify <slug> --workspace <ws> --record`` from
the backend directory. Check the written JSON (it is scrubbed, but read it
before committing), commit the folder, and this test picks it up.
See docs/verification.md.
"""
from __future__ import annotations

import pytest

from app.connectors.base import cassettes
from tests.support.cassettes import replay

FOLDERS = cassettes.discover()


@pytest.mark.parametrize(
    "folder",
    FOLDERS or [None],
    ids=[f.name for f in FOLDERS] or ["no-cassettes-yet"],
)
def test_cassette_replays(folder) -> None:  # noqa: ANN001
    if folder is None:
        return
    replay(folder).assert_ok()
