# Connector verification

The unit tests mock every vendor with responses we wrote ourselves. They prove
our code matches **our reading** of each vendor's docs, not that the vendor
agrees. Verification closes that gap:

1. Each connector has a few **live checks** (`verify_steps()` in its package).
2. You run them against a **free test or sandbox account** (`manage.py verify`,
   or Portal Admin → Verification).
3. The result is stored. A connector whose **latest** run passed within the
   last **30 days** is *verified*. Every other connector shows a small **Beta**
   pill to customers ("Not yet confirmed against a live account. It may not
   work as expected."), or can be hidden from them entirely.
4. With `--record`, the real HTTP exchanges are saved (scrubbed) as
   **cassettes**, and the test suite replays them offline, so a change in our
   code that alters a call fails a test.

The per-vendor setup (what to create, what each check does, what it costs) is in
[verification-sandboxes.md](verification-sandboxes.md). This page covers how the
framework is used.

---

## 1. Free test accounts

| Kind | Vendors | Free test option (details in verification-sandboxes.md) |
|---|---|---|
| Ads | Google Ads | Test manager account; client accounts made under it are test accounts. A developer token with test access is enough. |
| | Meta Ads | Sandbox ad account from the Marketing API tools of your app. |
| | Microsoft Ads | Sandbox environment (separate sandbox sign-up and addresses). |
| | TikTok Ads | Sandbox ad account from the TikTok for Business developer portal. |
| | LinkedIn Ads | No sandbox. An ad account with no payment method; writes stay draft/paused. |
| Analytics and search | GA4, Search Console, Bing Webmaster | Free; use a test site you own. |
| | PageSpeed Insights, Google Places | Free tier API key (or the platform's included key). |
| | DataForSEO | Its free sandbox endpoint, or trial credit. |
| | Adobe Analytics | No free account; needs a customer's report suite (read only). |
| CMS and code | WordPress | A throwaway self-hosted site (local, Docker) with an application password. |
| | Shopify | A free development store from a Partner account. |
| | Webflow, Magento | A free Webflow site; Magento Open Source run locally. |
| | GitHub, Bitbucket | Free account with a private test repository. |
| | Site Crawler | Any public website. Read only. |
| CRM and email | HubSpot | Developer test account. |
| | Salesforce | Free Developer Edition org (or a sandbox org). |
| | Mailchimp, Klaviyo, Brevo | Free plans. Writes create drafts and delete them; nothing is sent. |
| | SMTP | A test inbox service (Mailtrap, Ethereal). |
| Social | Facebook Pages, Instagram, LinkedIn Pages | A test Page / professional account you admin, with the app in development mode. |
| | Pinterest | Pinterest's API sandbox. |
| | X | Posting is charged per post on X's current API plans; that step runs only with `verifyAllowPaidPost=yes`. |
| AI | OpenAI, Claude, Gemini, Perplexity | An API key with a little credit (Gemini has a free tier). Reads only. |
| Other | Slack, Zapier webhooks, Custom API | A free Slack workspace; a free Zapier Catch Hook or a webhook.site address. |

Check each vendor's current terms before relying on "free".

### Marking a connection as a test account

What a connection points at is its `accountEnvironment` value: `live` (the
default), `test_account` or `sandbox`. It decides whether **write steps marked
"test account"** may run.

* **Connectors with a Test account setting** (Google Ads, Meta Ads, Microsoft
  Ads, TikTok Ads, Shopify, HubSpot, Slack, Pinterest): type *Test account* or
  *Sandbox* in it when connecting. Some of these also switch to the vendor's
  sandbox addresses.
* **Every other connector:** say so for the run instead:
  * CLI: `--environment test_account` (or `sandbox`);
  * Portal Admin: tick **This connection is a test account** in the Run dialog.

  This applies to that one run (it is stored with the result) and is never
  saved on the connection.

Only mark a connection as a test account when the account behind it really is
one. The spend-capable checks also ask the vendor to confirm it before creating
anything.

---

## 2. Running a check

### From the command line (backend directory)

```
python manage.py verify <slug> --workspace <workspace id or slug>
python manage.py verify <slug> --workspace <ws> --allow-writes --environment test_account
python manage.py verify <slug> --workspace <ws> --set verifyImageUrl=https://example.com/a.jpg
python manage.py verify --all --workspace <ws>
```

* The connector is built from that workspace's saved connection, exactly as the
  agents build it. Connect it in the workspace first.
* Steps run in order and **stop at the first failure**. The rest are listed as
  "Not run".
* **Read steps** always run. **Write steps** run only with `--allow-writes`;
  those marked "test account" also need a test or sandbox connection (see
  above). Otherwise they are listed as skipped, with the reason.
* `--set verifyKEY=VALUE` (repeatable) passes a run-only option some checks
  read (for example `verifyAllowPaidPost=yes`, `verifyImageUrl=...`). Only
  names starting with `verify` are accepted, and they are never saved.
* A step whose opt-in is missing is shown as **skipped** with its message, not
  as failed.
* `--all` runs every connector connected in that workspace that has a check.
* The result is printed as a table and stored. Exit code: `0` when everything
  checked passed, `1` when anything failed (or had no check), `2` for a usage
  problem (unknown workspace or connector).

Example output:

```
Site Crawler (site_crawler) - Live account - workspace acme
  PASS  read   fetch_home_page    0.41s  Fetched the home page: 812 words, 64 links
  Result: PASSED
```

### From Portal Admin

Portal Admin → **Verification** lists every connector: *Verified · date ·
environment*, *Failed · reason*, *Expired*, *Never checked*, or *No live check
written yet*. **Run** asks which workspace's connection to use, then offers
**This connection is a test account**, **Allow test writes** (only for a test
or sandbox connection) and **Advanced options** (`verify…` run-only options).
The check runs in the background; the row shows *Checking…* and updates when
it is done. **Report** expands the latest run step by step.

The switch **Hide unverified connectors from customers** removes every
connector without a current pass from workspace catalogues (the Integrations
screen, agent connector choices, the assistant's suggestions). Workspaces that
already connected one keep it, so nothing that works stops working.

Report details never contain credential values: any value from the
connection's credentials is replaced (`[hidden]` for secrets, `[fieldName]`
otherwise) before the report is stored.

---

## 3. Recording cassettes

```
python manage.py verify <slug> --workspace <ws> --record [--allow-writes --environment test_account]
```

When the run passes, the scrubbed exchanges of every step that ran are written
to `backend/tests/cassettes/<slug>/`:

* `_meta.json`: when and against which environment it was recorded, the step
  order, and the **fake** credentials a replay uses;
* `<step>.json`: that step's requests and responses, in order.

A failed run writes nothing. Re-recording overwrites the files of the steps
that ran.

**Scrubbing** (`app/connectors/base/cassettes.py`) replaces real values with
consistent fakes, so a replay still makes sense:

* every credential value: secrets become `test-<field>`; URLs and hosts become
  `https://<field>.example.test`; emails `<field>@example.test`; numeric ids a
  fake of the same length; anything else `test-<field>`. Short values, a
  field's default, paths and model names stay;
* request headers are dropped except Content-Type and Accept (so Authorization,
  API-key headers and cookies are never written); responses keep only
  Content-Type, Location and Link;
* secret query and form parameters (`key`, `access_token`, `token`,
  `client_secret`, `code`, `signature`, ...) and JSON values under secret-named
  keys (`access_token`, `password`, `apiKey`, ...) become `REDACTED`;
* email addresses become `user<n>@example.test`;
* long numeric ids the vendor sent (9+ digits: account, customer, campaign,
  property ids) become same-length fakes everywhere they appear;
* binary bodies are not kept, only their size.

Before anything is written, the output is checked once more for every secret
credential value; if one is found, nothing is written. **Still read the files
before committing them**: names, addresses or free text in a vendor's response
are not scrubbed.

**Replay.** `tests/test_cassettes.py` finds every cassette folder and replays it
with `tests/support/cassettes.py`: the connector is built with the fake
credentials, its `verify_steps()` run, and each request must be the next one
recorded (method, URL and body; dates are masked so "the last 30 days" does not
drift). An extra, missing or changed request fails the test, naming both. No
request ever reaches the network. To use it in your own test:

```python
from tests.support.cassettes import replay

def test_wordpress_against_recorded_reality():
    replay("wordpress").assert_ok()
```

Limits: only `httpx.Client` traffic is recorded (not async clients or SMTP), and
a request containing a code constant that looks like a vendor id may need
re-recording after a change.

---

## 4. Expiry

* Each run is a row in `connector_verifications` (migration 0018), with the
  environment used, every step's result and who ran it.
* **Verified** = the **latest** run of that connector type passed **and**
  finished within `VERIFIED_FOR_DAYS` (30). A later failure removes "verified"
  at once; a pass simply lapses after 30 days and shows as *Expired* in Portal
  Admin until it is run again. Vendors change their APIs, so re-run monthly.
* Results are cached per server process for 30 seconds. Until migration 0018
  has run, every connector reads as unverified (with one warning in the log).
* Other parts of the product ask the same question through
  `app.services.verification.is_verified(db, slug)`; for example, unverified
  connectors need approval before agents act through them.
