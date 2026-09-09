"""What the product says about itself, and what it keeps to itself.

An earlier version of the credential notice named the cipher and described
the key hierarchy — "AES-256-GCM under a data key belonging only to your
workspace, itself wrapped by a master key held outside the database". Every
word of it was true, which is not the same as it being ours to publish. It
told an attacker which primitive to attack and where the layers are, and it
told the customer nothing they could act on: what they need is the promise,
not the mechanism.

The same applies to the stack. A connector's help text should say where to
find a token in *their* vendor console, never what this platform is built
from, and never why a decision inside it was made the way it was.

These assertions cover the strings the API actually serves to the console.
They do not cover code comments or docstrings, which are for whoever
maintains this and are the right place for the reasoning.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from app.agents.base.registry import agent_slugs, get_agent
from app.connectors.base import registry

#: Cryptographic primitives and key-management vocabulary. Naming these in
#: the interface is disclosure without benefit.
CRYPTO = re.compile(
    r"\b(AES(-?256)?|GCM|CBC|ChaCha|HKDF|PBKDF2|bcrypt|scrypt|argon2"
    r"|blind ?index|envelope encryption|master key|data key|key.encrypting"
    r"|nonce|initialisation vector|IV)\b",
    re.I,
)

#: What the platform is built with. None of it is the customer's concern.
STACK = re.compile(
    r"\b(FastAPI|SQLAlchemy|Alembic|psycopg|uvicorn|Pydantic|tenacity"
    r"|PostgreSQL|Postgres|JSONB|row.level security|RLS|Celery|Redis"
    r"|React|Vite|TypeScript|httpx)\b",
    re.I,
)

#: Vocabulary from inside the implementation, which reads as a leak even
#: when it is harmless — an operator should not have to know that a
#: "registry" or a "bundle" exists to read an error message.
INTERNALS = re.compile(
    r"\b(the runner|the registry|the bundle|the scheduler tick|tenant_id"
    r"|row of the|the ORM|our codebase|the repo(sitory)? layer)\b",
    re.I,
)

#: HMAC is deliberately allowed on these two. The customer verifies the
#: signature on their *own* endpoint, so the scheme is part of the
#: integration contract rather than an internal detail — withholding it
#: would make the connector unusable.
SIGNING_CONTRACT = {"custom_api", "zapier_webhooks"}


def _connector_strings():
    for spec in registry.all_specs():
        yield spec.slug, "description", spec.description
        for index, requirement in enumerate(spec.requirements):
            yield spec.slug, f"requirement[{index}]", requirement
        for field in spec.fields:
            yield spec.slug, f"{field.key}.label", field.label
            yield spec.slug, f"{field.key}.help", field.help_text
            yield spec.slug, f"{field.key}.placeholder", field.placeholder


def _agent_strings():
    for slug in agent_slugs():
        spec = get_agent(slug).spec
        yield slug, "description", spec.description
        yield slug, "scope_placeholder", spec.scope_placeholder
        yield slug, "name", spec.name


ALL = [*_connector_strings(), *_agent_strings()]


def test_the_interface_never_names_a_cryptographic_primitive():
    offenders = [
        (owner, where, text)
        for owner, where, text in ALL
        if text and CRYPTO.search(text)
    ]
    assert offenders == [], offenders


def test_the_interface_never_names_the_stack():
    offenders = [
        (owner, where, text)
        for owner, where, text in ALL
        if text and STACK.search(text)
    ]
    assert offenders == [], offenders


def test_the_interface_does_not_speak_in_implementation_terms():
    offenders = [
        (owner, where, text)
        for owner, where, text in ALL
        if text and INTERNALS.search(text)
    ]
    assert offenders == [], offenders


def test_signing_is_disclosed_only_where_the_customer_must_verify_it():
    """HMAC is named on exactly the two connectors that hand a signature to
    the customer's own endpoint, and nowhere else.

    Both directions matter: naming it elsewhere is disclosure, and *removing*
    it from these two would leave somebody unable to verify a request they
    are receiving.
    """
    named = {
        owner
        for owner, _where, text in _connector_strings()
        if text and re.search(r"\bHMAC\b", text)
    }
    assert named == SIGNING_CONTRACT, named


#: Copy narrating *this product's* own history. The onboarding wizard had
#: "This step used to be three tick boxes that recorded a preference nothing
#: read", which is a changelog entry pointed at somebody trying to set up a
#: workspace.
#:
#: Deliberately matched on self-reference rather than on the words "used to"
#: or "no longer" alone. A vendor's history is legitimate and often the most
#: useful thing on the form — Bitbucket's help text says app passwords were
#: deprecated and no longer authenticate, which is exactly why a customer's
#: old credential stopped working. A blunter rule flagged it, and deleting
#: that sentence to satisfy a test would have made the product worse.
OWN_HISTORY = re.compile(
    r"(this (step|screen|page|field|connector|agent) (used to|no longer)"
    r"|we (used to|changed|removed|rewrote|now)"
    r"|in an earlier version|previously this)",
    re.I,
)


def test_the_interface_does_not_narrate_its_own_history():
    """Somebody setting up a workspace is not an audience for a changelog."""
    offenders = [
        (owner, where, text)
        for owner, where, text in ALL
        if text and OWN_HISTORY.search(text)
    ]
    assert offenders == [], offenders


# ── The console's own copy ─────────────────────────────────────────────────
# The leak happened here, not in the API: the credential notice was written
# directly into the page. Scanning the source is cruder than scanning served
# strings, but it covers the place that actually went wrong, and a rule that
# only guards where the mistake did not happen is not much of a rule.
FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src"

#: Comments are where the reasoning belongs, so they are removed before the
#: copy is checked — including the JSX `{/* … */}` form.
_JSX_COMMENT = re.compile(r"\{/\*.*?\*/\}", re.S)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"^\s*//.*$", re.M)


def _visible_copy() -> list[tuple[str, str]]:
    """Each frontend source file with its comments stripped."""
    out: list[tuple[str, str]] = []
    for path in sorted(FRONTEND.rglob("*.ts*")):
        text = path.read_text(encoding="utf-8")
        text = _JSX_COMMENT.sub(" ", text)
        text = _BLOCK_COMMENT.sub(" ", text)
        text = _LINE_COMMENT.sub(" ", text)
        out.append((path.name, text))
    return out


@pytest.mark.skipif(not FRONTEND.exists(), reason="frontend sources not present")
def test_the_console_never_names_a_cryptographic_primitive():
    """The notice said "AES-256-GCM under a data key … wrapped by a master
    key". True, and none of the customer's business: it names the primitive
    to attack and the shape of the key hierarchy, and nobody reading it can
    act on either."""
    offenders = [
        (name, match.group(0))
        for name, text in _visible_copy()
        for match in [CRYPTO.search(text)]
        if match
    ]
    assert offenders == [], offenders


@pytest.mark.skipif(not FRONTEND.exists(), reason="frontend sources not present")
def test_the_console_never_names_the_stack():
    offenders = [
        (name, match.group(0))
        for name, text in _visible_copy()
        for match in [STACK.search(text)]
        if match
    ]
    # React and TypeScript appear in imports and type positions all over a
    # React codebase, so only the words that could not be code are checked.
    prose_only = [
        (name, hit)
        for name, hit in offenders
        if hit.lower() not in {"react", "typescript", "vite", "httpx"}
    ]
    assert prose_only == [], prose_only
