# Sign-in providers: business apps (phase 2B)

This page covers Slack, HubSpot, Salesforce, Mailchimp, Klaviyo, Shopify, GitHub, Bitbucket and Webflow, plus the included Google keys for PageSpeed Insights and Google Maps search, and the guided setups for Adobe Analytics and Zapier. Phase 1 (Google, WordPress) and the redirect-URI basics are in [oauth-setup.md](oauth-setup.md).

Every provider here is optional. Until its keys are set, the integration shows its previous manual form or key guide, and workspace admins see a note naming the missing keys. **Portal Admin → Sign-in providers** shows each provider's status, missing keys, requested permissions and exact redirect URI.

## The redirect URI

Register this URI with each vendor:

```
{PUBLIC_API_BASE_URL or PUBLIC_BASE_URL}/api/v1/oauth/callback/{provider}
```

`{provider}` is one of `slack`, `hubspot`, `salesforce`, `mailchimp`, `klaviyo`, `shopify`, `github`, `bitbucket` or `webflow`. Most vendors require `https` (Slack, HubSpot and Shopify always do), so local testing needs an https tunnel with `PUBLIC_API_BASE_URL` set to the tunnel address.

## What the customer sees

1. One branded button (**Add to Slack**, **Install on GitHub**, **Sign in with HubSpot**, …) and one sentence about the access requested. Shopify first asks for the store address. Salesforce shows a **This is a sandbox** tick box.
2. They approve on the vendor's own page and come straight back.
3. They pick the channel, audience, blog, repository or site. When there is only one, it is picked automatically and the connection is tested and saved.

Client IDs, secrets, keys and tokens never appear on this path. The previous pasted-credential setups still work under **Advanced**.

## Summary

| Integration | Provider | Button | Permissions requested | Picker | Tokens |
|---|---|---|---|---|---|
| Slack | `slack` | Add to Slack | bot: `chat:write`, `chat:write.public`, `channels:read` | Public channel | Bot token, does not expire |
| HubSpot | `hubspot` | Sign in with HubSpot | `oauth`, `crm.objects.deals.read`, `crm.objects.contacts.read` | Won deal stage (per pipeline) | 30-minute access token, refreshed |
| Salesforce | `salesforce` | Sign in with Salesforce | `api`, `refresh_token` | None (one org) | Refreshed; PKCE |
| Mailchimp | `mailchimp` | Sign in with Mailchimp | none (Mailchimp has no scopes) | Audience | Does not expire |
| Klaviyo | `klaviyo` | Sign in with Klaviyo | `accounts:read`, `templates:read`, `templates:write` | None (one account) | 1-hour access token, refreshed; PKCE |
| Shopify | `shopify` | Sign in with Shopify | `write_content`, `write_products` | Blog for new articles | Expiring offline token, refreshed and rotated |
| GitHub | `github` | Install on GitHub | GitHub App permissions: Contents (read and write), Pull requests (read and write), Metadata (read) | Repository | Installation tokens minted per hour |
| Bitbucket | `bitbucket` | Sign in with Bitbucket | consumer permissions: Repositories (write), Pull requests (write) | Repository | About 2-hour access token, refreshed and rotated |
| Webflow | `webflow` | Sign in with Webflow | `sites:read`, `cms:read`, `cms:write` | Site, paired with the collection new articles go to | No refresh token documented |
| PageSpeed Insights | (platform key) | Use the included access | — | — | Platform API key |
| Google Maps search | (platform key) | Use the included access | — | — | Platform API key |

Each permission comes from the connector's own API calls. Nothing is requested "just in case".

## Slack

- **Register:** [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → From scratch.
- **OAuth & Permissions:** add the redirect URI under *Redirect URLs*. Under *Bot Token Scopes*, add `chat:write` (post), `chat:write.public` (post to a public channel without inviting the app) and `channels:read` (list public channels for the picker). Add no user scopes. Leave **token rotation off**, because the platform stores the bot token, which then does not expire.
- **Distribution:** an app installs only in its own workspace until you open **Manage Distribution** and click **Activate Public Distribution**, which requires the https redirect URL. A Slack Marketplace listing (and its review) is optional.
- **Keys:** `OAUTH_SLACK_CLIENT_ID`, `OAUTH_SLACK_CLIENT_SECRET` (from *Basic Information → App Credentials*).
- **Customer flow:** **Add to Slack** → approve → pick a public channel. Private channels are not listed. A customer who wants one can invite the app to it and use the manual route.

## HubSpot

- **Register:** a public app (distribution type *marketplace*) on the HubSpot developer platform. Add the redirect URI. The app's required scopes must match exactly what the platform requests: `oauth`, `crm.objects.deals.read` and `crm.objects.contacts.read`. HubSpot refuses an authorize request whose scopes differ from the app's required scopes.
- **Review:** a marketplace listing is optional, but HubSpot caps unlisted marketplace-distribution apps (created or migrated on platform 2025.2 or later) at **25 installs** until the app is listed. Plan the listing review before you have more than 25 customers on HubSpot.
- **Keys:** `OAUTH_HUBSPOT_CLIENT_ID`, `OAUTH_HUBSPOT_CLIENT_SECRET`.
- **Account:** the HubSpot account ID (Hub ID) is taken from the token response, so the customer never types it. Tokens are exchanged and refreshed at `https://api.hubapi.com/oauth/v3/token`.
- **Picker:** the closed-won stage of each deal pipeline. A portal with one pipeline connects without a pick.

## Salesforce

- **Register:** since Spring '26, new **connected apps** can no longer be created (except from packages) unless Salesforce Support re-enables it. Create an **External Client App** instead (Setup → External Client App Manager). Existing connected apps keep working.
  - Enable OAuth and add the redirect URI as callback URL.
  - OAuth scopes: *Manage user data via APIs (`api`)* and *Perform requests at any time (`refresh_token`, `offline_access`)*.
  - Require PKCE (the platform always sends it) and require the secret for the web server flow.
  - Refresh-token policy: valid until revoked.
- **Customer orgs:** since September 2025 a user can authorise an *uninstalled* app only with the **Approve Uninstalled Connected Apps** permission, which admins have by default. Other users need their admin to install or approve the app first, so suggest that an admin connects Salesforce. For wide distribution, package the app (managed package or AppExchange). AppExchange security review is optional for this flow, but large customers often ask for it.
- **Sandbox:** the **This is a sandbox** tick box sends the sign-in to `test.salesforce.com` instead of `login.salesforce.com`. The org's address (`instance_url`) comes back with the token. It must be an https `*.salesforce.com`, `*.force.com`, `*.cloudforce.com` or `*.salesforce.mil` host, or the sign-in is refused.
- **Keys:** `OAUTH_SALESFORCE_CLIENT_ID` (consumer key) and `OAUTH_SALESFORCE_CLIENT_SECRET` (consumer secret).
- **Tokens:** Salesforce states no access-token lifetime, so a stored token is trusted for 15 minutes and then refreshed on the same login host.

## Mailchimp

- **Register:** in a Mailchimp account, **Profile → Extras → Registered apps → Register an app** ([admin.mailchimp.com/account/oauth2](https://admin.mailchimp.com/account/oauth2/)). Set the redirect URI. Mailchimp documents `127.0.0.1` rather than `localhost` for local callbacks.
- **Permissions:** Mailchimp OAuth has no scopes. A connected app acts as the account. The platform only creates drafts and never calls send, schedule or test-send, and the access sentence says this plainly.
- **Data centre:** read from `https://login.mailchimp.com/oauth2/metadata` (header `Authorization: OAuth <token>`) and kept as a readable hint (for example `us21`). Calls then go to `https://<dc>.api.mailchimp.com/3.0` with a Bearer token.
- **Picker:** the account's audiences. The draft's from name and reply-to come from the audience's campaign defaults and stay editable afterwards.
- **Keys:** `OAUTH_MAILCHIMP_CLIENT_ID`, `OAUTH_MAILCHIMP_CLIENT_SECRET`. Tokens do not expire.
- **Advanced:** the API-key guide (Profile → Extras → API keys) remains available under *Advanced: use an API key instead*.

## Klaviyo

- **Register:** create an OAuth app in Klaviyo's developer settings ([manage apps](https://www.klaviyo.com/manage-apps)). Add the redirect URI and the scopes `accounts:read`, `templates:read` and `templates:write`. Klaviyo expects `accounts:read` on every app, and this connector also uses it for its health check. `templates:read` lets the connection check read back the test template it saves ([Get Template](https://developers.klaviyo.com/en/reference/get_template) requires it). Connections signed in before it was added keep saving templates; if the read-back is refused, the check says "Sign in again to grant the new permission."
- **API revision:** requests send the `revision` header `2026-07-15`, the newest stable revision in Klaviyo's [changelog](https://developers.klaviyo.com/en/docs/changelog_). A connection that still holds the old form default (`2025-07-15`) uses the new one; a revision someone typed is kept.
- **Flow:** PKCE is required. The token endpoint (`https://a.klaviyo.com/oauth/token`) takes the client ID and secret as an HTTP Basic header, which the platform sends on both the exchange and refreshes. Access tokens last about an hour, and the refresh token lasts until the app is uninstalled or goes 90 days unused.
- **Review:** a listing in Klaviyo's App Marketplace needs a review and, per Klaviyo, a few active installs first. An unlisted app works for the accounts that install it.
- **Keys:** `OAUTH_KLAVIYO_CLIENT_ID`, `OAUTH_KLAVIYO_CLIENT_SECRET`.
- **Advanced:** the private-key guide is still available.

## Shopify

> **Constraint:** OAuth for other people's stores needs a Shopify app with **public** or **custom** distribution, created in the Shopify Dev Dashboard. A custom-distribution app installs on one store, or the stores of one Plus organisation, through an install link. To serve many unrelated merchants, the app must be **public**, which means Shopify's app review. Also, since April 2025, new apps submitted to the App Store must use the GraphQL Admin API only, but this connector uses the (legacy) REST Admin API. That is fine for custom-distribution apps and existing apps, but an App Store submission would first need the connector moved to GraphQL.

- **API version:** REST Admin API `2026-10`, the latest stable version, supported until October 2027 ([versioning](https://shopify.dev/docs/api/usage/versioning)).
- **Register:** in the [Dev Dashboard](https://dev.shopify.com/dashboard), create the app, set the redirect URI as an allowed redirection URL, and request the access scopes `write_content` (pages, blogs, articles and their metafields) and `write_products` (product descriptions and metafields). Each write scope includes its read scope.
- **Store address:** the customer types `mystore.myshopify.com`, the bare store name, or their `admin.shopify.com/store/<name>` address. Anything else is refused before sign-in, including a custom domain, a port, a path or another host, because the client secret is sent to that host. The value must match `^[a-z0-9][a-z0-9-]*\.myshopify\.com$`. The checked address is kept with the sign-in's server-side state, and the callback's `shop` must equal it.
- **Callback verification:** Shopify's `hmac` is verified (HMAC-SHA256 with the client secret over all other query parameters, sorted) in constant time before the code is used. An unsigned or forged callback never reaches the token endpoint.
- **Tokens:** the exchange asks for an **expiring offline token** (`expiring=1`), which Shopify requires of new public apps (and of all public apps for GraphQL calls from 1 January 2027). The access token lasts about an hour. The refresh token lasts 90 days and is **replaced on every refresh**, and the new one is written back to the connection. An app on non-expiring tokens still works: no refresh token comes back, and the token lasts until the app is uninstalled.
- **Picker:** the store's blogs (where new articles are saved as hidden drafts). A store with no blog still connects.
- **Keys:** `OAUTH_SHOPIFY_CLIENT_ID`, `OAUTH_SHOPIFY_CLIENT_SECRET`.

## GitHub (GitHub App, chosen over an OAuth App)

**Why a GitHub App:** the connector reads files, creates branches, commits and opens pull requests in one repository. An OAuth App can do that only with the classic `repo` scope, which grants every private repository the person can reach. A GitHub App instead has fine-grained permissions (Contents and Pull requests only), is installed on the repositories the customer picks, and works with installation tokens that last an hour and are limited to those repositories. Nothing long-lived is stored per workspace, only the installation ID.

- **Register:** **Settings → Developer settings → GitHub Apps → New GitHub App** (under the platform's organisation).
  - *Callback URL:* the redirect URI above. Tick **Request user authorization (OAuth) during installation**, so GitHub returns to the callback with a code, the installation ID and the state. Leave *Setup URL* empty, and leave *Expire user authorization tokens* at its default.
  - *Webhook:* not needed (untick Active).
  - *Repository permissions:* **Contents: Read and write**, **Pull requests: Read and write**. Metadata (read) is added automatically. Request no account or organisation permissions.
  - *Where can this app be installed:* **Any account** (a public app). Only public apps can be installed by others from `github.com/apps/<slug>/installations/new`. No review by GitHub is needed. A Marketplace listing is optional.
  - Generate a **private key** (PEM).
- **Keys:**
  - `OAUTH_GITHUB_CLIENT_ID`, `OAUTH_GITHUB_CLIENT_SECRET` (the App's client ID and a client secret).
  - `GITHUB_APP_SLUG` (from `github.com/apps/<slug>`).
  - `GITHUB_APP_ID` (the numeric App ID).
  - `GITHUB_APP_PRIVATE_KEY` (the PEM; a single line with `\n` escapes is accepted).
  - **Install on GitHub** is offered only when all five are set.
- **Verification:** GitHub warns that the installation ID on a redirect can be forged. The platform exchanges the code for the installer's own token, checks that the installation is in their `GET /user/installations`, then discards that token. It is not stored.
- **Tokens:** an RS256 JWT (`iat` 60 s in the past, `exp` 9 minutes ahead, `iss` the App ID) is exchanged at `POST /app/installations/{id}/access_tokens` for an hour-long installation token, cached until 5 minutes before expiry. The health check confirms that the installation really has Contents and Pull requests write access.
- **Picker:** the repositories the installation was given. The deploy branch is pre-filled from the default branch.
- **Advanced:** a fine-grained personal access token still works as before.

## Bitbucket Cloud

- **Register:** in the platform's Bitbucket workspace settings, open **OAuth consumers → Add consumer**. Set the callback URL to the redirect URI. Grant permissions: **Repositories: Write** and **Pull requests: Write**. Bitbucket takes the scopes from the consumer's settings, so the authorize request carries none.
- **Keys:** `OAUTH_BITBUCKET_CLIENT_ID` (the consumer *Key*) and `OAUTH_BITBUCKET_CLIENT_SECRET` (the *Secret*).
- **Tokens:** the client authenticates with HTTP Basic. Access tokens last about two hours, and each refresh returns a new refresh token, which is written back. An unused refresh token expires after about three months.
- **Picker:** repositories the signed-in account is a member of, most recently updated first.
- **Review:** none. Atlassian deprecated app passwords (the old manual route) in 2026. The repository access-token route remains under Advanced.

## Webflow

- **Register:** in the platform's Webflow workspace, **Workspace settings → Apps & Integrations → Develop → Create an App**, with the *Data Client (REST API)* building block. Set the redirect URI and the scopes `sites:read`, `cms:read` and `cms:write`.
- **Approval (important):** a newly registered app can be installed only by users in the app's own workspace. Other workspaces can install it only after it has been **approved and published in the Webflow Marketplace**. Submit it for review before offering Webflow sign-in to customers.
- **Keys:** `OAUTH_WEBFLOW_CLIENT_ID`, `OAUTH_WEBFLOW_CLIENT_SECRET`.
- **Tokens:** Webflow documents no refresh token. The token lasts until the app is uninstalled, or until 365 days without use.
- **Picker:** each site the customer authorised, paired with the CMS collection that has a rich text field. The article writer's `articlesCollectionId` and body field are set by the pick, preferring the blog template's `post-body`. A site with no such collection connects without article saving.

## PageSpeed Insights and Google Maps search: included access

```
PLATFORM_PAGESPEED_API_KEY=AIza…
PLATFORM_GOOGLE_PLACES_API_KEY=AIza…
```

When one of these is set, the integration offers **Use the included access**. PageSpeed connects in one click. Maps search asks only for the business name and search radius. The connection stores no key of its own: it is marked `keySource=platform` and reads the platform key at call time. If the setting is removed later, those connections say the included access is no longer offered. A workspace can always paste its own key under **Advanced: use my own key**, and its own key then takes precedence.

**Quota and cost:**

- *PageSpeed Insights* is free, but every workspace on the included key shares one project's quota (a per-day and per-100-seconds limit). Raise the quota in Google Cloud, or limit pages per run, before many workspaces use it.
- *Places API (New)* is **billed per request** to the platform's billing account. Text Search is a paid SKU, and each tracked phrase for each workspace is one request per run. Set a budget alert and per-day quota caps in Google Cloud. Restrict the key to the Places API (New) and to the API server's egress IPs.
- Create separate keys for the two settings and restrict each to its own API.

## Adobe Analytics and Zapier

These remain guided **Advanced** setups, with plain numbered steps.

- **Adobe Analytics:** Adobe offers no per-customer sign-in for the Analytics 2.0 API that a platform could register once. An Adobe admin creates a Developer Console project with the **Adobe Analytics** API and an **OAuth Server-to-Server** credential (JWT credentials are retired), with a product profile that includes the report suite. They then copy the Client ID, the client secret ("Retrieve client secret") and the Organization ID. The Global Company ID is under Analytics **Admin → Company settings → API access**.
- **Zapier:** in Zapier, create a Zap with **Webhooks by Zapier → Catch Hook**, copy the webhook URL from the *Test* tab, and paste it. Webhooks by Zapier is a premium app and needs a paid Zapier plan. The signing secret is optional and is meant for custom endpoints.

## Environment keys (all optional)

```
OAUTH_SLACK_CLIENT_ID=            OAUTH_SLACK_CLIENT_SECRET=
OAUTH_HUBSPOT_CLIENT_ID=          OAUTH_HUBSPOT_CLIENT_SECRET=
OAUTH_SALESFORCE_CLIENT_ID=       OAUTH_SALESFORCE_CLIENT_SECRET=
OAUTH_MAILCHIMP_CLIENT_ID=        OAUTH_MAILCHIMP_CLIENT_SECRET=
OAUTH_KLAVIYO_CLIENT_ID=          OAUTH_KLAVIYO_CLIENT_SECRET=
OAUTH_SHOPIFY_CLIENT_ID=          OAUTH_SHOPIFY_CLIENT_SECRET=
OAUTH_GITHUB_CLIENT_ID=           OAUTH_GITHUB_CLIENT_SECRET=
GITHUB_APP_SLUG=  GITHUB_APP_ID=  GITHUB_APP_PRIVATE_KEY=
OAUTH_BITBUCKET_CLIENT_ID=        OAUTH_BITBUCKET_CLIENT_SECRET=
OAUTH_WEBFLOW_CLIENT_ID=          OAUTH_WEBFLOW_CLIENT_SECRET=
PLATFORM_PAGESPEED_API_KEY=       PLATFORM_GOOGLE_PLACES_API_KEY=
```

Restart the API after changing them.

## How it works (for developers)

- Each provider in `PROVIDERS` sets `extension="<vendor>"`, and the vendor-specific steps live in `app/services/oauth_business.py`:
  - checking inputs typed before sign-in (`prepare`);
  - templated hosts (`https://{shop}/…`, `https://{login_host}/…`);
  - the GitHub installation URL;
  - Shopify's HMAC check (`verify_callback`);
  - the code exchange and what comes back with the token: Slack team, HubSpot `hub_id`, Salesforce `instance_url`, Mailchimp data centre, GitHub installation.
- A connector declares questions to ask before sign-in with `OAuthBinding.sign_in_inputs` and its vendor wording with `OAuthBinding.button_label`. The checked values are stored with the sign-in's single-use state row, never trusted from the callback.
- Non-secret values that come with a token (`TokenSet.extra`) are stored as readable hints. Tokens are encrypted as before. The label of the picked item is kept as the `oauthChoice` hint, for display.
- Refresh, Basic client authentication and rotation write-back use the shared `OAuthTokenMixin`. A connector whose token endpoint depends on the store or org (Shopify, Salesforce) computes `token_url` from its own fields.
- The included Google keys use `ConnectorSpec.platform_key` (`PlatformKey`) and the `keySource=platform` hint.
- No database migration was needed.
- Tests: `tests/test_oauth_business_apps.py`.
