"""Reading and rewriting content that lives in a repository.

Plenty of sites have no CMS. The content is Markdown, MDX or HTML committed
next to the code — Next.js, Astro, Hugo, Jekyll, Eleventy, or a hand-written
static site — and the "publish" button is a merge. For those sites the
repository *is* the content system, which is what the GitHub and Bitbucket
connectors present it as.

That makes two problems this module solves, and both are the kind that quietly
corrupt a customer's site if got wrong:

**Frontmatter must survive.** A content file is usually a YAML block followed
by prose. An agent rewriting the prose must not touch the block: it carries
the layout, the publish date, the canonical URL, redirects, feature flags.
Rewriting the whole file with generated text would drop all of it, and the
page would build wrong or not at all. So the file is split, the body is
replaced, and the block is put back byte for byte — except for the specific
keys the agent is meant to change, which are edited in place.

**A file path is not a URL.** ``content/blog/ev-charging.md`` is served at
``/blog/ev-charging``, ``src/pages/index.astro`` at ``/``, and
``content/about/index.md`` at ``/about``. Getting this wrong means the SEO
pipeline reports on URLs that do not exist, so the mapping is explicit,
configurable per installation, and tested.

Deliberately dependency-free and pure: no network, no repository, no vendor.
Both connectors share it, and it can be tested exhaustively without either.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

# Extensions the SEO pipeline can work with. A repository is full of files
# that are not pages, and walking a tree without this filter turns every
# component and stylesheet into a "page" to be optimised.
CONTENT_EXTENSIONS: tuple[str, ...] = (
    # Markup and prose
    ".md", ".mdx", ".markdown", ".mdown", ".html", ".htm", ".xhtml",
    ".rst", ".adoc", ".asciidoc", ".textile",
    # Static site generators and template languages
    ".astro", ".njk", ".liquid", ".hbs", ".handlebars", ".mustache",
    ".ejs", ".pug", ".jade", ".haml", ".slim", ".twig", ".volt",
    ".erb", ".rhtml", ".gohtml", ".tmpl", ".templ",
    # Server-rendered pages. A PHP or Rails or .NET site is a site, and
    # leaving these out is how a whole class of client finds nothing.
    ".php", ".blade.php", ".jsp", ".aspx", ".cshtml", ".razor", ".cfm",
)

#: Extensions whose body is prose, so replacing it means replacing writing.
#: Everything else recognised as a page is *source* — replacing its body means
#: replacing code.
PROSE_EXTENSIONS: tuple[str, ...] = (
    ".md", ".mdx", ".markdown", ".mdown", ".html", ".htm", ".xhtml",
    ".rst", ".adoc", ".asciidoc", ".textile",
)


def is_rewritable(path: str) -> bool:
    """Whether an agent may replace this file's body.

    Prose only. A ``.jsx`` page is a component tree — often a composition with
    no copy in it at all — and replacing its body with generated text would
    replace working code with prose and break the build. The platform lists
    such pages, because knowing the URL inventory is useful, and refuses to
    write them.
    """
    return path.lower().endswith(PROSE_EXTENSIONS)


#: Component-framework files. A page in a Next, React, Vue or Svelte site is
#: one of these, which is most dev-managed sites — the ones with no CMS at all
#: and therefore the ones this connector exists for.
#:
#: Kept separate from the list above because they need a stricter test: in a
#: Markdown repository nearly every ``.md`` is a page, whereas in a React
#: repository nearly every ``.tsx`` is *not* — it is a component, a hook, a
#: layout. Treating them alike would hand every button in the codebase to an
#: agent as a page to rewrite.
FRAMEWORK_EXTENSIONS: tuple[str, ...] = (".tsx", ".jsx", ".vue", ".svelte")

#: Directories that never contain pages, whatever is in them.
_NON_PAGE_DIRS = frozenset(
    {
        "components", "component", "ui", "layouts", "layout", "partials",
        "lib", "libs", "utils", "util", "helpers", "hooks", "context",
        "contexts", "store", "stores", "styles", "style", "css",
        "api", "server", "middleware", "types", "config", "scripts",
        "test", "tests", "__tests__", "e2e", "spec", "mocks", "fixtures",
        "public", "static", "assets", "images", "img", "fonts",
        "node_modules", ".git", ".next", "dist", "build", "out", "coverage",
    }
)

#: Segments that mark a *routing* directory: a framework file under one of
#: these is a page, and one outside them is a component.
#:
#: Deliberately narrow, and separate from the URL roots below — the two answer
#: different questions and collapsing them made ``src/App.tsx`` a page, since
#: `src` is a fine thing to strip from a URL and a terrible signal that a
#: `.tsx` file is a route.
_ROUTE_DIRS = frozenset(
    {
        "pages", "app", "routes", "views", "screens",
        # Laravel resources/views, Rails app/views, Django/Jinja templates.
        "templates", "template",
    }
)

#: Segments that are scaffolding rather than URL. A superset: everything that
#: routes, plus the project layout conventions that wrap it.
_URL_ROOT_DIRS = frozenset(
    _ROUTE_DIRS
    | {
        "src", "content", "_posts", "collections", "site",
        "resources", "wwwroot", "web", "public_html", "htdocs",
    }
)

#: Suffixes that mark a file as tooling rather than a page, wherever it sits.
_NON_PAGE_SUFFIXES = (".test", ".spec", ".stories", ".story", ".d", ".config")

#: Next.js App Router reserves these filenames for things that are not pages.
_FRAMEWORK_NON_PAGES = frozenset(
    {"layout", "loading", "error", "not-found", "template", "default",
     "global-error", "route", "middleware", "sitemap", "robots", "manifest",
     "opengraph-image", "twitter-image", "favicon"}
)

# Filenames that represent their parent directory rather than a page of their
# own, so the URL drops the last segment.
_INDEX_STEMS = frozenset({"index", "_index", "page", "+page"})

#: Repository documentation. Every repo has some, none of it is a page on the
#: site, and mapping README.md to "/" would point the home URL at it — after
#: which an SEO agent would rewrite the project's own documentation as
#: marketing copy.
_REPO_DOCS = frozenset(
    {
        "readme", "changelog", "contributing", "license", "licence",
        "security", "code_of_conduct", "codeowners", "authors", "notice",
        "support", "governance", "maintainers", "todo", "roadmap",
    }
)

#: SCREAMING_SNAKE_CASE at the repository root is documentation, everywhere.
#: The named list above cannot keep up on its own — it did not know about
#: PROJECT_STRUCTURE.md or SISTER_WEBSITE_SOP.txt.md, both of which were
#: classified as website pages, both of which were then eligible to be sent
#: to a model and rewritten as marketing copy. Nobody writes a page of their
#: site as an all-caps file in the repository root, and everybody writes
#: their internal notes that way.
_SHOUTED_STEM = re.compile(r"^[A-Z][A-Z0-9]*(?:[._-][A-Z0-9]+)*$")


def _is_repo_doc(path: str) -> bool:
    """Documentation for whoever maintains the repository, not a page on it."""
    segments = path.split("/")
    if len(segments) > 1:
        # Only at the root. docs/ has its own exclusion, and a real page can
        # legitimately live at content/ABOUT-US.md.
        return False
    # Strip every extension, so SISTER_WEBSITE_SOP.txt.md is caught too.
    stem = segments[-1].split(".", 1)[0]
    return bool(_SHOUTED_STEM.match(stem))


#: Next, SvelteKit, Nuxt and Remix all mark a dynamic segment with brackets
#: or a colon prefix.
_DYNAMIC_SEGMENT = re.compile(r"^(\[.*\]|:.+)")

_FRONTMATTER_RE = re.compile(r"\A(---|\+\+\+)\r?\n(.*?)\r?\n\1\r?\n?", re.DOTALL)
_LD_JSON_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE
)
_TAG_RE = re.compile(r"<[^>]+>")
_HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


@dataclass(slots=True)
class ContentFile:
    """A parsed content file: its frontmatter, its body, and how to put it back."""

    path: str
    #: The raw frontmatter block *without* its delimiters, exactly as written.
    frontmatter: str = ""
    #: The delimiter in use — ``---`` for YAML, ``+++`` for TOML (Hugo).
    fence: str = "---"
    body: str = ""

    @property
    def has_frontmatter(self) -> bool:
        return bool(self.fence and self.frontmatter is not None and self._had_block)

    _had_block: bool = False

    def render(self) -> str:
        """Reassemble the file. Round-trips an unmodified file unchanged."""
        if not self._had_block:
            return self.body
        return f"{self.fence}\n{self.frontmatter}\n{self.fence}\n{self.body}"


def parse(path: str, raw: str) -> ContentFile:
    """Split a content file into its frontmatter block and its body."""
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        return ContentFile(path=path, frontmatter="", fence="---", body=raw, _had_block=False)
    fence, block = match.group(1), match.group(2)
    return ContentFile(
        path=path,
        frontmatter=block,
        fence=fence,
        body=raw[match.end() :],
        _had_block=True,
    )


def frontmatter_value(file: ContentFile, key: str) -> str:
    """Read one top-level scalar out of the frontmatter.

    A deliberately small reader rather than a YAML parser: the only keys this
    platform touches are flat strings, and pulling in a parser would mean
    round-tripping the whole document through it — which is exactly how
    comments, key order and quoting styles get silently rewritten in somebody
    else's repository.
    """
    pattern = re.compile(rf"^{re.escape(key)}\s*:\s*(.*)$", re.MULTILINE | re.IGNORECASE)
    match = pattern.search(file.frontmatter)
    if not match:
        return ""
    return _unquote(match.group(1).strip())


def set_frontmatter_value(file: ContentFile, key: str, value: str) -> None:
    """Set one top-level scalar, preserving everything else in the block.

    Replaces the line if the key exists and appends it if not. The value is
    always quoted, because a title containing a colon is both extremely common
    and invalid unquoted YAML — the failure mode being a build that breaks on
    the customer's site rather than an error here.
    """
    if not file._had_block:
        # No block to edit: start one, so a title can still be set on a plain
        # HTML or Markdown file.
        file.frontmatter = f'{key}: {_quote(value)}'
        file.fence = "---"
        file._had_block = True
        return

    pattern = re.compile(rf"^({re.escape(key)}\s*:).*$", re.MULTILINE | re.IGNORECASE)
    replacement = rf"\g<1> {_quote(value)}"
    if pattern.search(file.frontmatter):
        file.frontmatter = pattern.sub(replacement, file.frontmatter, count=1)
    else:
        file.frontmatter = f"{file.frontmatter.rstrip()}\n{key}: {_quote(value)}"


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\")
    return value


# ── Titles, text and structured data ───────────────────────────────────────
def title_for(file: ContentFile) -> str:
    """The page's title, from the most authoritative place it appears."""
    for key in ("title", "seo_title", "heading", "name"):
        found = frontmatter_value(file, key)
        if found:
            return found
    heading = _HEADING_RE.search(file.body)
    if heading:
        return heading.group(1).strip()
    html_title = re.search(r"<title[^>]*>(.*?)</title>", file.body, re.DOTALL | re.IGNORECASE)
    if html_title:
        return _TAG_RE.sub("", html_title.group(1)).strip()
    stem = file.path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return stem.replace("-", " ").replace("_", " ").strip().title()


def word_count(body: str) -> int:
    """Words a reader would see: markup and code fences do not count."""
    text = re.sub(r"```.*?```", " ", body, flags=re.DOTALL)
    text = _LD_JSON_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    return len([w for w in text.split() if any(c.isalnum() for c in w)])


def schema_types(body: str) -> list[str]:
    """The ``@type`` values of any JSON-LD already embedded in the file."""
    found: list[str] = []
    for block in _LD_JSON_RE.findall(body):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for entry in data if isinstance(data, list) else [data]:
            if isinstance(entry, dict) and entry.get("@type"):
                value = entry["@type"]
                found.extend(value if isinstance(value, list) else [str(value)])
    return list(dict.fromkeys(found))


def inject_json_ld(file: ContentFile, json_ld: dict) -> None:
    """Add or replace the file's JSON-LD block.

    Replaces an existing block rather than appending a second one: two
    conflicting graphs on a page is worse for a crawler than none, and
    appending on every run would grow the file without bound.
    """
    script = (
        '<script type="application/ld+json">\n'
        + json.dumps(json_ld, indent=2, ensure_ascii=False)
        + "\n</script>"
    )
    if _LD_JSON_RE.search(file.body):
        file.body = _LD_JSON_RE.sub(lambda _: script, file.body, count=1)
    else:
        file.body = f"{file.body.rstrip()}\n\n{script}\n"


# ── Paths and URLs ─────────────────────────────────────────────────────────
def content_extensions() -> tuple[str, ...]:
    """Recognised page extensions, plus anything the deployment added.

    Additive on purpose: a stack nobody here has heard of should be one
    environment variable away from working, not a code change and a release.
    """
    from app.core.config import settings

    extra = tuple(
        ext if ext.startswith(".") else f".{ext}"
        for ext in settings.content_extensions_extra
    )
    return CONTENT_EXTENSIONS + FRAMEWORK_EXTENSIONS + extra


def route_dirs() -> frozenset[str]:
    from app.core.config import settings

    return _ROUTE_DIRS | set(settings.route_dirs_extra)


def is_content_path(
    path: str,
    extensions: tuple[str, ...] | None = None,
    *,
    content_root_configured: bool = False,
) -> bool:
    """Whether this repository file is a page the SEO pipeline can work on.

    ``content_root_configured`` changes how strict the framework rule is. If
    the operator has named the folder their pages live in, that is better
    information than any convention, so a framework file under it is taken at
    its word. Without one, a routing directory has to appear in the path —
    otherwise pointing this at a React repository would report every component
    as a page.
    """
    known = extensions if extensions is not None else content_extensions()
    lowered = path.lower()
    segments = lowered.split("/")
    name = segments[-1]
    stem = name.rsplit(".", 1)[0]

    # Partials, drafts and component fragments are not pages. The underscore
    # prefix is the near-universal convention for them across static site
    # generators, with `_index` the documented exception in Hugo. SvelteKit's
    # `+page.svelte` is the other deliberate exception.
    if name.startswith("_") and stem not in _INDEX_STEMS:
        return False
    if any(segment in _NON_PAGE_DIRS for segment in segments[:-1]):
        return False
    if stem in _REPO_DOCS or stem.replace("-", "_") in _REPO_DOCS:
        return False
    if _is_repo_doc(path):
        return False
    # A dynamic route is a template, not a page. src/app/blog/[slug]/page.jsx
    # has no URL of its own — it renders many — so the SEO pipeline, which is
    # per-URL, cannot report on it honestly. Worse, rewriting it would rewrite
    # the template every post shares.
    if any(_DYNAMIC_SEGMENT.match(segment) for segment in segments):
        return False

    if lowered.endswith(FRAMEWORK_EXTENSIONS):
        # SvelteKit prefixes routes with +; strip it before the name checks.
        bare = stem.removeprefix("+")
        if bare in _FRAMEWORK_NON_PAGES:
            return False
        # Home.test.tsx and Button.stories.tsx are tooling, not pages, and a
        # configured content folder must not sweep them in.
        if bare.endswith(_NON_PAGE_SUFFIXES):
            return False
        if content_root_configured:
            return True
        return any(segment in route_dirs() for segment in segments[:-1])

    return lowered.endswith(known)


#: Directories that mark where routing starts, for deriving a URL when the
#: operator has not named a content folder.
def _url_roots() -> frozenset[str]:
    from app.core.config import settings

    return _URL_ROOT_DIRS | set(settings.route_dirs_extra)


def path_to_url(path: str, *, content_root: str = "", url_prefix: str = "") -> str:
    """Turn a repository path into the URL the page is served at.

    ``content/blog/ev-charging.md``  → ``/blog/ev-charging``
    ``src/pages/index.astro``        → ``/``
    ``src/app/blog/page.jsx``        → ``/blog``
    ``content/about/index.md``       → ``/about``

    A configured ``content_root`` is authoritative — the operator knows their
    own repository. Without one, the routing directory is derived from the
    path, because the alternative was reporting a Next app's home page as
    ``/src/app`` and every SEO figure attached to a URL that does not exist.
    The last routing segment wins, so ``src/app`` resolves past both.
    """
    trimmed = path.strip("/")
    root = content_root.strip("/")
    if root and trimmed.startswith(f"{root}/"):
        trimmed = trimmed[len(root) + 1 :]
    elif root and trimmed == root:
        trimmed = ""
    elif not root:
        segments = trimmed.split("/")
        roots = _url_roots()
        # Only the *leading* run of routing directories is scaffolding.
        # Taking the last one found anywhere was worse: in Rails,
        # app/views/posts/show.html.erb has a legitimate "posts" segment in
        # its URL, and a rule that strips through the last match deletes it.
        cut = 0
        while cut < len(segments) - 1 and segments[cut].lower() in roots:
            cut += 1
        trimmed = "/".join(segments[cut:])

    # Strip the extension — all of it. Compound extensions are the norm
    # outside the JavaScript world: home.blade.php is served at /home, and
    # show.html.erb at /show. Stripping only the last part left /home.blade,
    # which is a URL that does not exist.
    head, _, name = trimmed.rpartition("/")
    words = {part.lstrip(".") for ext in content_extensions() for part in ext.split(".") if part}
    parts = name.split(".")
    while len(parts) > 1 and parts[-1].lower() in words:
        parts.pop()
    trimmed = f"{head}/{'.'.join(parts)}" if head else ".".join(parts)

    segments = [s for s in trimmed.split("/") if s]
    if segments and segments[-1].lower() in _INDEX_STEMS:
        segments.pop()

    prefix = url_prefix.strip("/")
    if prefix:
        segments = [*prefix.split("/"), *segments]

    return "/" + "/".join(segments) if segments else "/"
