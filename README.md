# AutoMarket AI

Autonomous SEO, AEO and programmatic ads for enterprise teams — a multi-tenant
platform where eleven agents run the whole pipeline, and one guardrail per
workspace decides whether each action publishes itself or waits for a person.

```
.
├── backend/     Python — FastAPI, PostgreSQL, the agent fleet and connectors
├── frontend/    React — the enterprise console
├── idea/        the PRD and design canvas this was built from
└── docker-compose.yml
```

## What it does

**Technical SEO.** Audits the crawled site continuously and turns it into a
working list: thin pages, missing structured data, internal links into
nothing, orphan pages nothing links to, duplicate titles, missing H1s and
meta descriptions, and — where a page-speed source is connected — the mobile
Core Web Vitals Google ranks on. Every finding carries **what to do about it**,
because a list of observations is a report and a list of actions is a service.
Findings persist with a first-seen date, can be accepted with a reason, and
count as fixed when they stop appearing — which is the number that shows the
work happened.

**Any stack.** The Site Crawler reads the published site as a search crawler
does, so the analysis works identically for WordPress, Next, Rails, Laravel or
a static export — including sites whose copy lives inside components rather
than in content files, where reading source would measure an import block.
Writes still go through the CMS or the repository, which is where the content
can actually be changed.

**SEO & AEO.** Crawls whichever CMS is connected — including a **GitHub or
Bitbucket repository**, for the many sites whose pages are Markdown or HTML
committed next to the code and whose publish button is a merge. Those two
connectors write by opening a **pull request**, never by committing to the
deploy branch: the team already reviews every other change to those files, and
that review is the right gate for generated prose. It also means nothing
reaches the site until a person merges.

Crawls whichever CMS is connected, scores each page's semantic
gap, rewrites the copy and writes it back. Splits body text into
question/answer blocks phrased for answer engines, injects them, then measures
whether those engines actually cite the site. Builds and patches JSON-LD as the
content underneath it changes. Blocks ghost, bot and adult-spam referrers
before they corrupt the analytics every other decision is made from.

**Off-page authority.** Works from connected search and backlink data to find
placements that fit on *both* authority and topical relevance, writes sentiment-tuned pitches to their
editors, and watches competitors' backlinks — when a competitor earns one, the
publisher has just proven it covers the subject and will link out, which is the
best moment to approach it.

**Programmatic ads.** Generates cross-channel creative from the product data
already synced off the site, so a price change on the page propagates into the
ads. Reallocates spend toward the lowest measured CAC on a bounded, auditable
schedule, with the channel split held at exactly 100% by both the engine and
the manual slider. Builds first-party lookalike audiences without cookie pools. Drops
invalid traffic in real time and records what it saved.

Every one of those is an agent in its own package under
[`backend/app/agents/`](backend/app/agents), and every external system is a
connector in its own package under
[`backend/app/connectors/`](backend/app/connectors).

## Quick start

Docker is the shortest path — it brings up PostgreSQL with TLS, Redis, the API
and the console, and migrates:

```bash
cp .env.compose.example .env    # then fill in the three secrets it asks for
docker compose up --build
```

Open <http://localhost:5173> and create a workspace from the sign-up screen.
There is no password to choose: you give an address, and a six-digit code is
emailed to it. The compose stack has no mail server, so that code is printed
to the API log — `docker compose logs -f api` is your inbox until you point
`SMTP_HOST` at a real relay.

For a free cloud deploy, see [`RENDER_DEPLOY.md`](RENDER_DEPLOY.md): Render
Docker API + Render static console + Neon Postgres (no Redis/Celery). Local
`docker compose` stays the full stack for development.

The workspace starts empty, as a real one does: connect a CMS or an ad account
on the Connectors screen, and the agents begin working on the next tick.

There is no demo data and no seeded login. Nothing in the platform fabricates
pages, spend or performance — every figure the console shows came from a
connected system or from an agent's own recorded work.

Running the two halves directly is documented in
[backend/README.md](backend/README.md) and
[frontend/README.md](frontend/README.md).

## How it is built

### The agent contract

An agent declares what it is and implements one method:

```python
class OnPageSeoSyncAgent(BaseAgent):
    spec = AgentSpec(
        slug="on_page_seo_sync",
        name="On-Page SEO Sync",
        category=AgentCategory.SEO_AEO,
        default_interval=timedelta(hours=6),
        # Capabilities, not vendor names: any connected CMS satisfies this.
        required_capabilities=(Capability.LIST_PAGES, Capability.WRITE_PAGE),
        max_impact=Impact.HIGH,
    )

    def run(self, ctx: AgentContext) -> AgentResult:
        ...
        return AgentResult(actions=[...], metric_label="142 pages synced")
```

It never publishes anything itself. It *proposes* `AgentAction`s, each carrying
an impact level and the callable that would apply it. The runner
([`orchestration/runner.py`](backend/app/orchestration/runner.py)) then decides
per action, from three inputs:

| Guardrail | Low-impact action | High-impact action |
|---|---|---|
| `full` | applied | applied |
| `hybrid` | applied | queued for approval |
| `human` | queued | queued |

An agent cannot be started at all until somebody has configured it. Every
agent ships with a working default schedule, so that gate is not about missing
values — it is about a missing decision, and "the defaults looked fine" is a
poor account of why a customer's pages were rewritten. A manual "Run now" is
gated the same way, because it publishes and spends exactly as a scheduled run
does. "Launch workspace" starts the part of the fleet that has been configured
and says plainly how much of it that was.

A per-agent switch set to human review overrides a permissive workspace
guardrail, and a daily action cap bounds how much any agent can do unattended.

Because a queued action stores its own payload, approving it days later applies
**exactly** what was proposed, through the same code path an autonomous action
would have taken — no second model call, no divergence between the two routes.

Adding an agent is adding a folder: the registry discovers it, and every
workspace is provisioned with it.

### The connector contract

Agents ask for *a CMS* or *an ad platform*, never for WordPress or Google Ads
by name. Connectors implement capability interfaces
([`connectors/base/interfaces.py`](backend/app/connectors/base/interfaces.py))
and normalise each vendor's shape into one vocabulary. Each declares the
credential fields its dialog should render, which is why a new integration
needs no frontend change.

Credentials are verified with the vendor when they are submitted, so a
connector cannot be in the connected state without having worked, and its
health is kept current by a scheduled sweep rather than by a Test button
somebody has to remember. Agent configuration is checked the same way — at
Save, with every problem reported at once.

A connector that has no credentials, or whose vendor cannot do something,
fails or declines honestly and the agents route around it — an agent reports
"waiting on a connector that can write pages" rather than proceeding on
invented data. Test doubles for all of this live in the test suite, not in the
product.

### Multi-tenancy and encryption

This is built to be deployed for real organisations, so isolation and
encryption are structural rather than added on:

- **Row-level security** on every tenant-scoped table. Each transaction pins
  its organisation, and a query that forgets its `WHERE tenant_id` returns
  nothing rather than another customer's rows. Policies are installed with
  `FORCE`, and the application connects as a role that owns nothing.
- **Per-organisation encryption keys.** Every workspace gets its own AES-256-GCM
  data key, stored only wrapped under a master key held outside the database.
  Customer payloads — page bodies, outreach copy, ad copy, connector
  credentials, pending approvals — are encrypted under *that customer's* key,
  so one organisation's key cannot read another's data.
- **Authenticated context.** Every ciphertext is bound to its column,
  organisation and record, so a row cannot be moved between fields to change
  what the application reads.
- **Encrypted personal data with searchable lookup.** Names and email addresses
  are encrypted columns; sign-in matches on an HMAC blind index instead, so
  the plaintext never appears in a query. The two tables that have to be read
  before a session exists — the address directory and the one-time codes —
  hold nothing but those HMACs.
- **TLS in transit**, to the database and to every connector, with certificate
  verification never disabled.
- **Key rotation** without downtime: ciphertexts carry their key version, and
  `manage.py rotate-keys` re-wraps data keys under a new master key while old
  rows stay readable.

### Accounts, and why there are no passwords

Public self-service: anyone can create an organisation and becomes its Super
Admin. Signing in means proving control of a mailbox — the server emails a
one-time code and you send it back. That is the entire credential.

The reasoning:

- **Nothing stored can become a credential.** No password hashes to crack
  offline; a leaked `login_codes` row is an HMAC of a number that expired
  minutes after it was issued.
- **Nothing can be reused.** Most account takeovers are a password from
  another site's breach. There is no password here to reuse.
- **No reset flow**, which is usually the softest path into an account, and no
  "forgot your password" mail to imitate. The sign-in mail *is* the reset.
- **Every sign-in is visible** to the account holder, in their inbox, as it
  happens.

The trade is real and worth stating plainly: the mailbox becomes the single
point of compromise, and mail delivery becomes a hard dependency of logging
in. So production refuses to start without SMTP configured, and a code is
treated as the live credential it is — minutes to live, single use, five
attempts, replaced the moment another is requested, and an account lockout on
top so that requesting fresh codes to guess against does not scale. A code
request answers identically whether or not the address has an account, so the
sign-in screen cannot be used as a customer list.

Teammates are invited by link; accepting needs no code, because the link
already arrived in their mailbox. Sessions use short-lived access tokens with
rotating refresh tokens, and presenting a spent refresh token revokes the
whole family, because that is what a stolen token looks like. "Sign out
everywhere" is the one remedy a user can apply themselves.

Six roles, one table
([`core/rbac.py`](backend/app/core/rbac.py)), enforced by the API and used by
the console to build its own navigation — so what a user can see and what they
can do cannot drift apart.

## Verification

```bash
cd backend
pytest                                    # logic tests, no database needed
TEST_DATABASE_URL=postgresql+psycopg://... pytest   # plus the integration suite
```

390 tests: encryption (including key rotation and cross-tenant isolation), the
one-time-code rules and token handling, money formatting, the RBAC matrix, the autonomy policy,
the budget allocator, the fraud and spam detectors, creative validation,
audience clustering, the cross-tenant scheduler, and end-to-end API flows
against a real PostgreSQL — sign-up, tenant isolation, the connect-run-approve
loop, and role enforcement.

The sign-in rules are tested as rules, not as happy paths: that a code works
exactly once, that a new one retires the old, that an expired one is refused,
that guessing burns the code and then locks the account, that the throttle
suppresses a second mail without saying so, and that a code request for a
registered address is indistinguishable from one for a stranger.

Anything that walks every workspace is tested against **two** organisations,
not one: a per-transaction tenant pin only conflicts once there is a second
tenant, so a single-tenant test cannot see that class of bug.

The console is typechecked (`npm run typecheck`) and was walked screen by
screen in a real browser during development.

## Tech

Reported in the currency the customer budgets in — rupees by default, with
lakh/crore grouping (`₹12,34,567`, `₹4.57Cr`), set by `CURRENCY_SYMBOL` and
`CURRENCY_GROUPING`. Model API cost stays in US dollars, because that is what
Anthropic bills in and an invented conversion rate would be worse than a
second unit.

Python 3.12 · FastAPI · SQLAlchemy 2 · PostgreSQL 17+ · Alembic ·
AES-256-GCM · HMAC-SHA256 · Celery + Redis (optional) ·
Anthropic Claude / OpenAI / Gemini / Grok · React 18 · TypeScript · Vite

Four model vendors are supported and exactly one runs, chosen by
`LLM_PROVIDER` and `LLM_MODEL` — neither of which has a default, because the
model decides what gets written to a customer's site and what it costs. There
is no offline substitute and no fallback chain: an agent that publishes to a
live website must not be able to publish invented copy because of a
configuration flag, nor copy from a vendor nobody chose because the first one
timed out. Without a key the agents run, report, and skip the work that needs
a model. The deterministic provider the tests use is a test double in
`backend/tests/support/`.
