# AutoMarket AI — backend

FastAPI over PostgreSQL, the agent fleet, and the connector catalogue.

## Layout

```
app/
├── main.py              application factory, middleware, error handling
├── manage.py (../)      operations CLI
├── core/
│   ├── config.py        settings, with production guardrails
│   ├── crypto.py        AES-256-GCM envelope encryption, blind indexes
│   ├── security.py      one-time passcodes, access tokens
│   ├── rbac.py          the role/module access matrix
│   └── exceptions.py    domain errors mapped to HTTP responses
├── db/
│   ├── base.py          declarative base, UUID keys, JSONB, tenant FK
│   ├── session.py       engine, session factory, tenant pinning
│   ├── types.py         encrypted column types
│   └── rls.py           row-level security policies
├── models/
│   ├── identity.py      the two tables read before a session exists
│   └── ...              the rest, all tenant-scoped
├── schemas/             Pydantic request/response models
├── api/
│   ├── deps.py          auth, tenant pinning, RBAC guards
│   ├── middleware.py    security headers, request ids, rate limiting
│   └── v1/              one router per screen
├── agents/
│   ├── base/            the agent contract, context, registry, policy
│   └── <slug>/          one package per agent
├── connectors/
│   ├── base/            the connector contract, capabilities, registry
│   └── <slug>/          one package per connector
├── llm/                 the model provider (Anthropic)
├── orchestration/
│   ├── runner.py        runs one agent; decides apply vs queue
│   ├── scheduler.py     in-process scheduler with claim-before-run
│   └── tasks.py         Celery tasks for horizontal scale
└── services/            encryption, auth, approvals, metrics, connectors, …
```

## Running it

PostgreSQL 17+ is required. There is no SQLite fallback: the schema uses JSONB,
native UUIDs, `ON CONFLICT` upserts and row-level security, so testing against
anything else would prove the wrong thing.

```bash
python -m venv .venv && . .venv/Scripts/activate   # or bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`. At minimum, for local work:

```bash
# Generate the three secrets:
python -c "import secrets;print('SECRET_KEY='+secrets.token_urlsafe(48))"
python -c "import os,base64;print('MASTER_ENCRYPTION_KEY='+base64.b64encode(os.urandom(32)).decode())"
python -c "import os,base64;print('BLIND_INDEX_KEY='+base64.b64encode(os.urandom(32)).decode())"
```

Then create the database and roles once, as a superuser, and migrate:

```bash
psql -h localhost -U postgres -f scripts/bootstrap.sql
alembic upgrade head
uvicorn app.main:app --reload
```

Then create a workspace from the console's sign-up screen. Sign-up and
sign-in both send a one-time code by email; with `SMTP_HOST` empty the code is
printed to this process's log instead, so a local install needs no mail
server. The workspace starts empty: connect a CMS or an ad account and the
agents pick it up on the next tick.

`http://localhost:8000/docs` has the full API. `python manage.py check` reports
configuration and connectivity before you deploy.

## The CLI

```
python manage.py check           configuration and connectivity report
python manage.py init-db         create schema + RLS (alternative to alembic)
python manage.py sync-catalog    install agents/connectors added since signup
python manage.py run-agent <org-slug> <agent-slug>
python manage.py tick            run every due agent once
python manage.py rollup          daily metric rollup
python manage.py rotate-keys     re-wrap data keys under a new master key
```

## Configuration that matters

| Setting | Why |
|---|---|
| `MASTER_ENCRYPTION_KEY` | Wraps every organisation's data key. Losing it loses all customer payloads; leaking it undoes encryption at rest. Keep it in a KMS or secret manager. |
| `BLIND_INDEX_KEY` | Separate HMAC key for email lookup and token hashing. Must differ from the master key. |
| `POSTGRES_SSLMODE` | `require` or stricter in production; startup refuses an unencrypted link. |
| `DB_ENFORCE_RLS` | Leave on. It is the backstop that keeps one organisation's rows out of another's console. |
| `SMTP_HOST` | Mail is the authentication channel, not a notification channel — sign-in is a code emailed to the address. Unset, codes go to the log; production refuses to start that way. |
| `LOGIN_CODE_TTL_MINUTES` | How long a mailed code stays live. It is a credential sitting in a mailbox, so this is the number that matters most; production caps it at 30. |
| `LOGIN_CODE_MAX_ATTEMPTS` | Wrong guesses before the code is dead. With `MAX_FAILED_LOGINS`, this is what makes a six-digit space unwalkable; production caps it at 10. |
| `LOGIN_CODE_RESEND_SECONDS` | Minimum gap between codes for one address, so the sign-in form is not a mail cannon. |
| `CURRENCY_SYMBOL` / `CURRENCY_GROUPING` | What the customer's spend and CAC are reported in. `indian` grouping gives ₹12,34,567 and ₹4.57Cr; `western` gives 1,234,567 and 1.2M. `agent_runs.cost_usd` is exempt — Anthropic bills in dollars. |
| `SCHEDULER_SPEED` | Divides every agent interval. Raise it in development so runs are observable in minutes rather than hours. |
| `LLM_PROVIDER` / `LLM_MODEL` | Which vendor runs the agents (`anthropic`, `openai`, `gemini`, `grok`) and which model. `LLM_MODEL` has **no default anywhere**: the model decides what gets written to a customer's site and what it costs, so it is named explicitly or no provider is built. |
| `LLM_PRICE_INPUT_PER_MTOK` / `..._OUTPUT_...` | Your contracted rates. Unset, run costs read zero and a startup warning says so — rather than a built-in price table that goes stale silently in a number the console reports as spend. |
| `<PROVIDER>_BASE_URL` | Route through your own gateway, a regional endpoint or an audited proxy, with no code change. |
| `ANTHROPIC_API_KEY` | The agents cannot generate content without it. There is deliberately no offline substitute: a provider that invents copy must not be reachable by configuration when the target is a customer's live site. Without a key, agents run and skip their generating work, reporting "waiting on a model credential". |
| `CELERY_ENABLED` | `true` turns the scheduler into a dispatcher and moves agent runs to workers. |

`APP_ENV=production` refuses to start unless the deployment is actually
deployable — see `Settings._production_guardrails` in
[`core/config.py`](app/core/config.py). It checks:

- a real `SECRET_KEY`, and both encryption keys, with the blind-index key
  different from the master key;
- TLS to the database (`POSTGRES_SSLMODE=require` or stricter) and
  `FORCE_HTTPS`, with an https `PUBLIC_BASE_URL` so emailed links are not
  plain-text;
- explicit `TRUSTED_HOSTS` rather than `*`, and rate limiting on — the rate
  limiter is what stops the code endpoints being used to enumerate addresses
  or flood a mailbox;
- an `LLM_MODEL`, and the API key for whichever `LLM_PROVIDER` is selected;
- an `SMTP_HOST`, unconditionally: sign-in is a code sent by email, so a
  deployment without mail is one nobody can log in to;
- sane code parameters — a TTL of 30 minutes or less and at most 10 attempts,
  because a six-digit code is only safe while both are small.

It fails with the full list of what is missing, not the first problem.

## Signing in

There are no passwords. Signing in is two calls: `POST /auth/request-code`
mails a six-digit code, `POST /auth/login` redeems it for a session. Signing
up is the same shape — `POST /auth/request-signup-code` first, so that no
organisation can ever exist behind an address nobody has proven.

The reasoning, and the trade, are written out at the top of
[`services/auth.py`](app/services/auth.py). In short: nothing is stored that a
breach could turn into a credential, nothing can be reused from another site's
breach, and there is no reset flow to phish — at the cost of making the
mailbox the single point of compromise and mail delivery a hard dependency of
logging in.

Everything that makes an emailed number safe to log in with lives in
`issue_code` and `consume_code`:

| Rule | Why |
|---|---|
| Minutes to live | A code in a mailbox is a live credential; it should not be one for an hour. |
| Single use | `consumed_at` is set on success, so reading the mail later gets you nothing. |
| Attempt-capped | Five wrong guesses burns the row. A million possibilities is nothing to a script; this is what makes it unguessable. |
| Superseded on reissue | Only one code per address is ever live, so unused codes do not widen the space to guess against. |
| Account lockout | An attacker can request codes they cannot read and guess a few times against each; `MAX_FAILED_LOGINS` is what makes that terminate. |
| HMAC at rest | A leaked `login_codes` row yields no usable code. |
| Uninformative responses | `POST /auth/request-code` answers identically for an address with an account and one without, so the sign-in screen is not a customer list. |

Invitations need no code: the invite link arrived in the invitee's mailbox,
which proves the same thing. `POST /auth/sign-out-everywhere` is the one
remedy a user can apply themselves — a spent code cannot be replayed, but a
session minted from one lasts until it is revoked.

## Starting an agent

Two steps, enforced by the API rather than only by the console:

1. `PUT /agents/{slug}/config` — schedule, scope, notify channel, daily cap.
   This sets `configured`.
2. `POST /agents/{slug}/resume` — starts it. Refused with a 409 naming the
   agent while `configured` is false, and so is `POST /agents/{slug}/run`: a
   manual run is not a preview, it publishes and spends like any other.

`DELETE /agents/{slug}/config` removes the configuration and stops the agent,
which closes the gate again. Its run history and output are untouched — it
resets a setting, it does not erase what the agent did.

`POST /onboarding/complete` starts the configured part of the fleet and
reports how much that was, rather than claiming the fleet is live.

**Every agent declares where its facts come from**, via `required_capabilities`
or `any_of_capabilities`, and preflight is what enforces it. An agent with
neither can run on nothing, which sounds harmless and is not: one of them used
to ask the model for candidate domains, authority scores and contact
addresses, then record them as discovered opportunities — invented figures
presented as measurements, with a fabricated email address one agent away from
being written to. A test asserts the list of such agents is empty.

## Adding an agent

Create `app/agents/my_agent/` with an `__init__.py` exporting `AGENT`:

```python
class MyAgent(BaseAgent):
    spec = AgentSpec(
        slug="my_agent",                 # must equal the folder name
        name="My Agent",
        category=AgentCategory.SEO_AEO,
        description="What it does, in one line.",
        default_interval=timedelta(hours=6),
        # Capabilities, not vendor names: the agent needs *a CMS*, so any
        # connector that can do this satisfies it — including one added later.
        required_capabilities=(Capability.LIST_PAGES, Capability.WRITE_PAGE),
        max_impact=Impact.HIGH,
    )

    def run(self, ctx: AgentContext) -> AgentResult:
        data = ctx.ask_json(prompt, system=SYSTEM, schema_hint=SCHEMA)
        return AgentResult(
            summary="...",
            actions=[AgentAction(kind="...", title="...", impact=Impact.HIGH,
                                 apply=my_callable, payload={...})],
        )

AGENT = MyAgent()
```

The registry discovers it, `manage.py sync-catalog` installs it into existing
workspaces, and it appears in the console. If its actions can be queued for
approval, register an applier with `@register_applier("my_target_kind")` so an
approved item can be applied later from its stored payload.

## The technical audit

[`agents/technical_seo_auditor/checks.py`](app/agents/technical_seo_auditor/checks.py)
is pure functions over a list of `PageSnapshot`s — no database, no network, no
model — which is what lets each rule be tested on its own. These are findings
a customer will argue with ("why is this page thin"), so being able to point
at the exact test matters.

Three properties the checks hold to:

- **Every finding says what to do.** The recommendation is stored on the row,
  not recomputed, so a finding reads the same in a report as on the day it was
  raised.
- **Nothing measured is invented.** Core Web Vitals are field data from real
  devices and cannot be derived from HTML, so the speed checks produce
  *nothing at all* without a connected page-speed source. A page with no
  sample is unmeasured, which is not the same as good — and the screen says
  so rather than showing an empty list.
- **Severity means expected impact.** A broken internal link and a missing alt
  attribute are both real; treating them as equally urgent is how audit tools
  train people to ignore them.

`SeoIssue` rows are reconciled each run rather than rewritten: a finding seen
again keeps its `first_seen_at`, one that has gone is marked `fixed` (not
deleted, so the work is countable), and one somebody ignored stays ignored —
reopening it every run would make the Ignore button useless.

## No fallbacks

A deliberate, load-bearing property, and the reason several things here look
stricter than they need to.

There is **one model provider and one model**, both named in the environment.
If the configured one is unreachable, the call fails and the agent reports
that it is waiting on a model. It does not retry on another model, ask another
vendor, or return an empty completion an agent might treat as content. Copy on
a customer's page should only ever come from the model somebody chose — which
is also why Anthropic's server-side refusal fallbacks are switched off rather
than merely unused.

**No credential is guessed.** Every one of these used to have a default, and
each pointed the connector at something real and wrong: a WordPress username
(`automarket`, so the application password authenticated as nobody), a Slack
channel (`#general`, visible to the customer's whole company), a HubSpot
closed-won stage (counting the wrong deals, in a number that looked plausible
either way), and an LLM model on the citation-check connectors. They are
declared fields now, and `Credentials.require` raises rather than inventing.

That raise is a `ConnectorConfigError`, which the per-item error handling
deliberately does **not** swallow. A transient failure of one query or one
page is logged and skipped so the batch survives; a missing credential fails
every item identically, and absorbing it five times turned a misconfiguration
into a report of "0 citations found" — which reads as a finding rather than a
fault.

The one thing kept that reads like a fallback is `extract_json`, which tries
a second parse of the *same* response when a model wraps its JSON in prose.
That is tolerant parsing of one answer, not a second path to a different
answer, and removing it would only make agents fail more often for no gain.

## Configuration is checked when it is submitted

There is no Test button, and no test endpoint. Both sides are verified at the
moment somebody presses Save:

- **Connector credentials** go to the vendor first, via
  `verify_credentials` in [`services/connectors.py`](app/services/connectors.py),
  and nothing is written unless the vendor accepts them. The vendor's own
  refusal is passed through — "the token can read this repository but not
  write to it" is worth far more than "invalid credentials". A connector
  therefore cannot be in the connected state without having worked, and its
  health says `ok` immediately rather than `unknown`.
- **Agent configuration** is checked by `_validate_config` in
  [`api/v1/agents.py`](app/api/v1/agents.py), which reports every problem at
  once. Each agent validates its own scope through `validate_scope`, using the
  shared rules in [`agents/base/scope.py`](app/agents/base/scope.py) — a path
  scope missing its leading slash matches nothing, and used to surface six
  hours later as a run that reported "0 pages".

The line between refusing and warning is deliberate. A value that is **wrong**
is refused: it would store a configuration that can never work. A value that
is merely **ineffective** — a notify channel with no connector behind it — is
saved with a warning, because refusing it would block the first thing a new
workspace has to do, since an agent cannot start until it is configured and
nothing is connected yet.

`HealthReport.probed` is what makes the connector half honest: a connector
with no probe reports `probed=False`, and "fine" and "not checked" do not
arrive as the same answer. Without it, a connector with no health check would
have accepted every wrong password as a successful connection.

Health then stays current by itself: the scheduler's `sweep_connector_health`
re-probes every connected integration every 30 minutes, across every
workspace. That is the other half of removing the button — a health state that
only updates when somebody remembers to press something is a health state
nobody trusts.

## Where a page's content actually is

Two sources, and choosing the wrong one silently produces nonsense.

**A repository connector reads source files.** That works when the copy *is* a
file — Markdown with frontmatter, an HTML page. It does not work for a
component-based site, and that is the common case rather than the edge one. A
Next page file is usually a composition:

```jsx
export default function HomePage() {
  return (<><Header /><Hero /><Outcomes /><SiteFooter /></>)
}
```

There is no copy in it. The words live in a dozen components and no single
file is the page. Measured from source, that page is 45 words titled "Page";
measured from the rendered site it is 1,255 words titled "Alambrix —
Intelligence, woven in." The first number is the length of an import block.

**The Site Crawler reads the published page** — the HTML a search crawler
receives. It is the universal source: no source files, no knowledge of the
stack, identical behaviour for WordPress, Next, Rails, Laravel or a static
export. It deliberately declares no write capability, because there is no way
to PUT a change back to a rendered page.

So the shape of a working setup is usually **both**: the crawler for what is
published, and a CMS or repository connector for the writes.

`content_files.is_rewritable` is the guard that keeps them apart. A `.jsx`
page is listed — the URL inventory is worth having — and `write_page` and
`inject_schema` refuse it outright, because replacing its body would replace
working code with prose and a `<script>` block appended to it is a syntax
error. Both refusals name the Site Crawler as the thing to connect instead.

One finding only the crawler can make: if the HTML arriving is essentially
empty because the content is assembled in the browser, the page is invisible
to crawlers that do not run JavaScript. Reported as exactly that, never as
thin content — the cause and the fix have nothing in common.

## Not assuming the client's stack

A recurring failure mode, and the reason several lists here are broader than
they look. A connected GitHub repository once reported "0 pages synced" as a
success because the recognised extensions were Markdown and HTML and every
page in it was `.jsx` — the connector worked, the token worked, and the agent
had quietly decided the site had no content.

So:

- **~40 page extensions** are recognised, well past the JavaScript world:
  Blade, ERB, Twig, Razor, Handlebars, Pug, Slim, PHP, JSP, reST, AsciiDoc and
  the framework page files. `CONTENT_EXTENSIONS_EXTRA` and `ROUTE_DIRS_EXTRA`
  extend both lists from the environment, so an unheard-of stack is a variable
  rather than a release.
- **Two separate lists, deliberately.** `_ROUTE_DIRS` decides whether a
  framework file is a page; `_URL_ROOT_DIRS` decides what to strip from a
  path to get a URL. Collapsing them made `src/App.tsx` a page, because `src`
  is a fine thing to strip and a terrible signal that a `.tsx` is a route.
- **Schema types are validated by shape, not membership.** Ten allowed types
  quietly meant "this platform is for car dealers"; a recipe site, a clinic or
  a job board would have been refused a type it needed.
- **The onboarding CMS list comes from the connector registry**, so a new
  integration appears there the moment it exists — and it ends with
  "Other / not listed", because a client may be on something with no
  connector yet and the wizard must not imply otherwise.
- **An empty listing is explained, not reported as success.** The connector
  returns an extension histogram: the difference between "your repository is
  empty" and "your pages are .tsx and they live in src/pages".

## A repository as a CMS

`github` and `bitbucket` present a git repository as a content system, for
sites with no portal — Hugo, Astro, Next, Jekyll, or hand-written HTML. Three
things about them are worth knowing before changing anything:

- **Platform limits are stated before the attempt.** `ConnectorSpec.requirements`
  lists what a vendor needs — a plan tier, a minimum version, a token scope —
  and the console shows it in the connect dialog. `BaseConnector.diagnose`
  then translates a failure: WordPress.com Free, Personal and Premium plans do
  not expose the REST API with application passwords, so no token will ever
  work there, and neither the vendor's 401 nor a retry will say so.
- **Writes open a pull request.** These repositories have a deploy pipeline on
  the default branch, so a commit there ships. The agent's change arrives as a
  branch and a PR the customer's own CI and reviewers see, which makes
  `write_page` returning True mean "a pull request is open", not "the page is
  live".
- **Frontmatter is preserved byte for byte** except the keys the agent is meant
  to change. It carries layout, dates, canonical URLs and feature flags;
  replacing the file with generated prose would break the build.
- **A file path is not a URL**, and the mapping is per-installation
  (`contentPath`, `urlPrefix`). Guessing it means the SEO pipeline reports on
  URLs that do not exist.

The shared logic is in [`connectors/base/git_base.py`](app/connectors/base/git_base.py)
and [`content_files.py`](app/connectors/base/content_files.py); each vendor
supplies six API primitives.

## Adding a connector

Create `app/connectors/my_service/` exporting `CONNECTOR_CLASS`, subclassing
the capability interface that fits (`CmsConnector`, `AdsConnector`,
`AnalyticsConnector`, `CrmConnector`, `AeoMonitorConnector`,
`NotificationConnector`). Declare the credential fields; the console renders
them automatically, encrypts the secret ones, and never returns them.

Implement only what the vendor can actually do. An unsupported call raises a
clear error naming the platform, and the agents route around it rather than
fabricating a result.

## Testing

```bash
pip install -r requirements-dev.txt
pytest                                              # logic only — see below
TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/automarket_test pytest
```

**Run it with the database.** Without `TEST_DATABASE_URL` every
database-backed test skips: the API flows, the scheduler, row-level security
and the whole agent pipeline. That is roughly a quarter of the suite, and a
skip is quiet — the run still says "passed" in green.

It cost something real. On-Page SEO Sync had never once proposed a rewrite,
because `list_pages` omits page bodies by design and nothing called
`read_page`; every synced page had an empty body, every gap analysis was
skipped, and `word_count` stayed at nought, which also made every page on
every site look like thin content to the auditor. The tests that would have
caught it were the ones not being run. `pytest` now prints whether the
database tests are on or dormant in its header, for exactly that reason.

The integration suite creates and drops its own schema, so point it at a
throwaway database — `assert_disposable` refuses to run unless the database
name contains `test`, `tmp` or `scratch`.

Test doubles live in [`tests/support/`](tests/support): a deterministic model
provider, a set of fake connectors, and a mailbox that captures outbound mail
— the suite has to be able to read a sign-in code, because there is no other
way to complete a login and nothing in the product will hand one back. All
three are installed by autouse fixtures, so no test can reach a real model,
connector or mail server even by accident. They are in the
test suite rather than the application on purpose — a fake data source or a
fake copywriter reachable from configuration is a production hazard, not a
feature. Cross-tenant behaviour is tested with the `second_org` fixture as
well as `org`, because a per-transaction tenant pin only misbehaves once there
is a second tenant.
