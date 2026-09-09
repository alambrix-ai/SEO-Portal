"""Shared scope validators for the Configure dialog.

Scope is one text field that means something different to every agent — a
path pattern, a list of domains, a list of channel names, a set of schema
types. The value is only ever read at run time, so a wrong one used to cost
six hours and produce a run that reported doing nothing. These are the checks
that turn that into an error message at the moment somebody presses Save.

Each validator returns a problem in words the operator can act on, or ``None``.
They deliberately reject only what is *certainly* wrong: a validator that
guesses gets in the way of a legitimate value, and being unable to save a
correct configuration is worse than saving a doubtful one.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from fnmatch import fnmatch

_PATH_LIKE = re.compile(r"^/[A-Za-z0-9\-_/*.]*$")
_DOMAIN_LIKE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9\-]*[A-Za-z0-9])?(\.[A-Za-z0-9\-]+)+$")


def items(scope: str) -> list[str]:
    """Split a comma-separated scope, dropping the empties."""
    return [part.strip() for part in scope.split(",") if part.strip()]


def paths(scope: str) -> str | None:
    """Path prefixes or globs, e.g. ``/blog/*`` or ``/a/*, /b/*``.

    The one mistake worth catching is a missing leading slash:
    ``inventory/*`` matches nothing, and the agent reports "0 pages" rather
    than "your scope is wrong".
    """
    for entry in items(scope):
        if not entry.startswith("/"):
            return (
                f"Scope {entry!r} has to start with / — page paths are matched "
                f"from the site root, so /{entry.lstrip('/')} is probably what "
                "you meant."
            )
        if not _PATH_LIKE.match(entry):
            return (
                f"Scope {entry!r} is not a path. Use a prefix or a glob, like "
                "/blog/* — or leave it blank for the whole site."
            )
        if "://" in entry:
            return f"Scope {entry!r} looks like a full URL; use just the path."
    return None


def domains(scope: str) -> str | None:
    """Bare hostnames, e.g. ``competitor.com, another.co.uk``."""
    for entry in items(scope):
        candidate = entry.lower()
        for prefix in ("https://", "http://"):
            if candidate.startswith(prefix):
                return (
                    f"Use just the domain for {entry!r} — "
                    f"{candidate.removeprefix(prefix).split('/')[0]}."
                )
        if "/" in candidate:
            return f"Use just the domain for {entry!r}, without a path."
        # A bare word with no dot is a company name, not a domain, and both
        # are legitimate here — the link monitor matches on either.
        if "." in candidate and not _DOMAIN_LIKE.match(candidate):
            return f"{entry!r} is not a valid domain."
    return None


def one_of(scope: str, *, allowed: set[str], label: str) -> str | None:
    """A list drawn from a fixed vocabulary — channels, schema types."""
    lowered = {value.lower(): value for value in allowed}
    for entry in items(scope):
        if entry.lower() not in lowered:
            return (
                f"{entry!r} is not {label}. Choose from: "
                + ", ".join(sorted(allowed))
                + " — or leave it blank."
            )
    return None


_TYPE_NAME = re.compile(r"^[A-Z][A-Za-z0-9]{2,49}$")


def type_names(scope: str, *, examples: tuple[str, ...] = ()) -> str | None:
    """schema.org type names — checked by shape, not against a fixed list.

    schema.org has hundreds of types and clients are in every vertical, so an
    allow-list here is a decision about who the product is for. A name is
    accepted if it could be a type: PascalCase, alphanumeric, no spaces. That
    refuses "Product Page" and "produt_page" while allowing MedicalClinic,
    JobPosting and anything else somebody's sector actually needs.
    """
    for entry in items(scope):
        if not _TYPE_NAME.match(entry):
            hint = f" Examples: {', '.join(examples[:6])}." if examples else ""
            return (
                f"{entry!r} is not a schema.org type name. They are written in "
                f"PascalCase with no spaces.{hint}"
            )
    return None


def free_text(scope: str, *, max_items: int = 12, label: str = "entries") -> str | None:
    """A comma-separated list with no format, only a sane length.

    Used where the value is genuinely free — topics, story angles. The only
    real error is a list so long the agent's prompt loses focus.
    """
    found = items(scope)
    if len(found) > max_items:
        return f"That is {len(found)} {label}; keep it to {max_items} or fewer so the agent stays focused."
    for entry in found:
        if len(entry) > 120:
            return f"{entry[:40]!r}… is too long for one entry."
    return None


# ── Matching, not just validating ──────────────────────────────────────────
# The validator above says whether a scope is well formed. This says whether
# a URL is in it, which used to be done inline in four agents as
#
#     prefix = ctx.scope.rstrip("*")
#     ... if url.startswith(prefix)
#
# and that was wrong in three separate ways. A comma-separated scope the
# validator accepts — "/a/*, /b/*" — became the single prefix "/a/*, /b/" and
# matched nothing at all. "/inventory/*" excluded "/inventory" itself, which
# is normally the most important page in the section. And a bare "/blog"
# also matched "/blog-archive", which nobody means.
def path_matcher(scope: str) -> Callable[[str], bool]:
    """A predicate for "is this URL in scope".

    An empty scope matches everything: no scope means the whole site, which
    is the default and has to stay the permissive case.
    """
    entries = items(scope)
    if not entries:
        return lambda url: True

    patterns: list[str] = []
    for entry in entries:
        cleaned = entry.rstrip("/")
        if cleaned.endswith("/*"):
            # A section: the landing page and everything under it. Excluding
            # the landing page is never what somebody typing /blog/* wants.
            patterns.append(cleaned[:-2] or "/")
        else:
            patterns.append(cleaned or "/")

    def matches(url: str) -> bool:
        candidate = "/" + url.strip("/") if url.strip("/") else "/"
        for pattern in patterns:
            if pattern == "/":
                return True
            if "*" in pattern or "?" in pattern:
                if fnmatch(candidate, pattern) or fnmatch(candidate, pattern + "/*"):
                    return True
            # A path boundary, so /blog does not match /blog-archive.
            elif candidate == pattern or candidate.startswith(pattern + "/"):
                return True
        return False

    return matches


def describe(scope: str) -> str:
    """The scope as it should be read back to somebody in a message."""
    entries = items(scope)
    if not entries:
        return "the whole site"
    if len(entries) == 1:
        return entries[0]
    return ", ".join(entries[:-1]) + f" and {entries[-1]}"


_ESCAPE = "~"


def _escape_like(value: str) -> str:
    """Neutralise LIKE wildcards in the literal part of a path.

    A URL can legitimately contain % or _, and left alone those would widen
    the match: /a_b would also match /axb.
    """
    return (
        value.replace(_ESCAPE, _ESCAPE * 2)
        .replace("%", _ESCAPE + "%")
        .replace("_", _ESCAPE + "_")
    )


def sql_filter(scope: str, column):  # noqa: ANN001, ANN201 - a SQLAlchemy column
    """The same matching as :func:`path_matcher`, pushed into the query.

    Returns ``None`` for an empty scope so the caller adds no condition at
    all. Three agents filtered stored pages with ``url.like(prefix + "%")``
    after ``scope.rstrip("*")``, which quietly meant ``/blog`` also matched
    ``/blog-archive`` and a comma-separated scope matched nothing at all.
    """
    from sqlalchemy import or_

    entries = items(scope)
    if not entries:
        return None

    clauses = []
    for entry in entries:
        cleaned = entry.rstrip("/")
        if cleaned.endswith("/*"):
            cleaned = cleaned[:-2]
        if not cleaned or cleaned == "/":
            return None
        safe = _escape_like(cleaned)
        if "*" in cleaned:
            # A glob in the middle: /product/*/spec.
            clauses.append(column.like(safe.replace("*", "%"), escape=_ESCAPE))
        else:
            # The section landing page, and everything beneath it — with the
            # separator, so /blog does not pull in /blog-archive.
            clauses.append(column == cleaned)
            clauses.append(column.like(f"{safe}/%", escape=_ESCAPE))
    return or_(*clauses)
