# Sign-in providers: social and ad platforms (phase 2A)

This page covers the second group of sign-in providers: Meta (Facebook and Instagram), LinkedIn, X, Pinterest, Reddit, TikTok, Microsoft Advertising and Bing Webmaster Tools. The general setup, the security model and Google are in [oauth-setup.md](oauth-setup.md).

Each vendor needs an app registered once by the platform operator. Until a vendor's environment keys are set, its integrations show the manual form (Bing Webmaster Tools shows its API-key guide instead), and workspace admins see a note naming the missing keys. Nothing breaks either way, and connections made by pasting tokens keep working under **Advanced**.

Vendor endpoints, scopes and lifetimes below were checked against each vendor's documentation in October 2026. Points the documentation leaves open are listed under each vendor.

## The redirect URI

Register this exact address with each vendor. **Portal Admin → Sign-in providers** shows it for every provider.

```
{PUBLIC_API_BASE_URL or PUBLIC_BASE_URL}/api/v1/oauth/callback/{provider}
```

| Vendor | `{provider}` | Environment keys |
|---|---|---|
| Meta (Facebook Login) | `meta` | `OAUTH_META_CLIENT_ID`, `OAUTH_META_CLIENT_SECRET` |
| LinkedIn Ads | `linkedin` | `OAUTH_LINKEDIN_CLIENT_ID`, `OAUTH_LINKEDIN_CLIENT_SECRET` |
| LinkedIn Pages | `linkedin_pages` | `OAUTH_LINKEDIN_PAGES_CLIENT_ID`, `OAUTH_LINKEDIN_PAGES_CLIENT_SECRET` |
| X | `x` | `OAUTH_X_CLIENT_ID`, `OAUTH_X_CLIENT_SECRET` |
| Pinterest | `pinterest` | `OAUTH_PINTEREST_CLIENT_ID`, `OAUTH_PINTEREST_CLIENT_SECRET` |
| Reddit | `reddit` | `OAUTH_REDDIT_CLIENT_ID`, `OAUTH_REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT`, plus `REDDIT_ENABLED=true` |
| TikTok for Business | `tiktok` | `OAUTH_TIKTOK_CLIENT_ID` (App ID), `OAUTH_TIKTOK_CLIENT_SECRET` (Secret) |
| Microsoft Advertising | `microsoft` | `OAUTH_MICROSOFT_CLIENT_ID`, `OAUTH_MICROSOFT_CLIENT_SECRET`, `MICROSOFT_ADS_DEVELOPER_TOKEN` |
| Bing Webmaster Tools | `bing_webmaster` | `OAUTH_BING_WEBMASTER_CLIENT_ID`, `OAUTH_BING_WEBMASTER_CLIENT_SECRET` |

Restart the API after changing them.

## New in this release: scopes, and who must reconnect

| Integration | Added | Required? | Without it |
|---|---|---|---|
| Facebook Pages | `read_insights` | Optional | Posting works; post metrics say "Reconnect Facebook to allow insights". |
| Instagram | `instagram_manage_insights` | Optional | Posting works; post metrics say "Reconnect Instagram to allow insights". |
| X | `media.write` | Optional | Posts go out as text, with a note that the image was left off. |
| Reddit | `identity`, `mysubreddits`, `submit`, `read` | Required | New integration, off until `REDDIT_ENABLED=true`. |

A sign-in that leaves out only optional scopes still connects. Connecting and posting never fail for lack of them.

**Existing X, Facebook Pages and Instagram connections must reconnect** (**Sign in again** on the integration) to grant the new scopes. Until they do, X posts stay text-only and Facebook and Instagram post metrics stay empty. The optional Meta permissions also need App Review (below) before customers outside the app's roles can grant them.

## What the customer sees

1. One button, such as **Sign in with Facebook**, with the vendor's mark. Under it, one sentence says what access is asked for and why.
2. They sign in at the vendor and allow access, then come back to the same screen.
3. They pick the ad account, Page, Instagram account, company page, board or site. If there is only one, it is picked automatically. X has nothing to pick: the signed-in account is connected straight away.
4. The connection is tested, then saved.

Client IDs, secrets, developer tokens and raw tokens never appear on this path. The paste-it-yourself fields stay under **Advanced: connect with your own … app or token**.

## How long a sign-in lasts

| Vendor | Access token | Renewal |
|---|---|---|
| Meta, Meta Ads | Long-lived user token, about 60 days | None. Facebook Login issues no refresh token to a web app. The connection shows its end date, warns 14 days before it, and asks the customer to **Sign in again** once it has expired. |
| Meta, Facebook Pages and Instagram | Page token from the long-lived user token | Does not expire. It still stops working if the person changes their password, loses their role on the Page, or removes the app. Graph error 190 is then reported as "Sign in again". |
| LinkedIn (both) | 60 days | Refreshed automatically when LinkedIn issues a refresh token (365 days, not extended by use). LinkedIn issues refresh tokens only to approved Marketing Developer Platform apps. Without one, the connection behaves like Meta Ads: it shows its end date, then asks for **Sign in again**. |
| X | 2 hours | Refreshed automatically. Each refresh returns a new single-use refresh token, which is written back to the connection straight away. |
| Pinterest | 30 days | Refreshed automatically. Each refresh returns a new continuous refresh token (valid 60 days from issue), which is stored. |
| TikTok for Business | Long-term token | Does not expire until the advertiser removes the authorisation. |
| Microsoft Advertising | About 1 hour | Refreshed automatically. Each refresh returns a new refresh token (90-day default lifetime), which is stored, so the connection does not age out while it is in use. |
| Bing Webmaster Tools | About 1 hour | Refreshed automatically. If Bing returns a new refresh token, it is stored. |

When a refresh token is replaced, the new one is saved with a compare-and-swap on the old one, so two workers refreshing at the same moment cannot overwrite each other. A worker whose single-use token was just spent by another worker picks up the replacement from the database instead of failing. A sign-in whose refresh tokens are single-use (X, Microsoft, Pinterest, Bing) is never copied to a second integration.

## Meta: Facebook Login (Meta Ads, Facebook Pages, Instagram)

One Meta app and one Facebook Login serve all three integrations. After one is connected, the drawer offers **Also connect with this Facebook account** for the others. Facebook then asks only for the extra permissions.

### Register

1. At [developers.facebook.com/apps](https://developers.facebook.com/apps/), create an app of type **Business**.
2. Add the **Facebook Login** product (or Facebook Login for Business). Under **Settings → Valid OAuth Redirect URIs**, add `…/api/v1/oauth/callback/meta`.
3. Add the **Marketing API** product for Meta Ads, and **Instagram** (Instagram API with Facebook Login) for Instagram.
4. Copy the App ID and App Secret into `OAUTH_META_CLIENT_ID` and `OAUTH_META_CLIENT_SECRET`.
5. Leave **Require app secret** (appsecret_proof) off. The connectors do not send it.

### Permissions

| Integration | Permissions | Why |
|---|---|---|
| Meta Ads | `ads_read`, `ads_management`, `pages_show_list`, `pages_read_engagement` | `ads_read` reads insights. `ads_management` sets campaign budgets and creates creatives, paused ads and website custom audiences. Meta lists the two Pages permissions as dependencies of `ads_management`. |
| Facebook Pages | `pages_show_list`, `pages_manage_posts`, `pages_read_engagement`; optional `read_insights` | List the Pages to choose from, publish posts and photos, and read the Page back for the health check. `read_insights` reads each post's metrics. |
| Instagram | `instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement`; optional `instagram_manage_insights` | Find the Instagram professional account linked to a Page, then publish image posts. `instagram_manage_insights` reads each post's metrics (they can lag up to 48 hours). |

The sign-in reads back which permissions were actually granted (`/me/permissions`), because Facebook lets a person untick some. If a required one is missing, the customer is told to sign in again and allow it. A missing optional one (`read_insights`, `instagram_manage_insights`) does not block the connection.

### Picker

- **Meta Ads:** ad accounts from `/me/adaccounts`, with their account ID, currency and status. Closed accounts are left out.
- **Facebook Pages:** Pages from `/me/accounts` where the person's role includes creating content. The Page token is stored encrypted and is never sent to the browser.
- **Instagram:** Instagram accounts linked to those Pages (`instagram_business_account`). The linked Page's token is used for publishing.

### App Review and Business Verification

- Until the app has **Advanced Access**, only people with a role on the app (admins, developers, testers) can grant these permissions. Every permission above needs **App Review** for Advanced Access. For publishing, apply for `pages_manage_posts` and `instagram_content_publish`; for post metrics, also `read_insights` and `instagram_manage_insights`.
- **Business Verification** is required for any app requesting Advanced Access.
- The Marketing API starts at **Standard Access**, which is heavily rate-limited. Apply for Advanced Access to `ads_management` and `ads_read` through App Review.
- Custom audiences also need the Custom Audience Terms accepted for each ad account.

### Uncertain

- Meta's documentation contradicts itself on whether Instagram publishing takes a Page token or a User token. The connector uses the linked Page's token, as the content-publishing guide says. A pasted user token still works under Advanced.
- `instagram_basic` and `instagram_content_publish` are not on Meta's list of permissions exempt from 90-day data-access expiry. If a person is inactive with the app for 90 days, Instagram publishing may need **Sign in again** even though the Page token itself has not expired.
- `business_management` is not requested. Meta says it "may be required" for some Business-portfolio setups. Add it to the Meta Ads binding if customers report ad accounts or Pages missing from the picker.
- Sign-in, Meta Ads, Facebook Pages and Instagram all use Graph API **v26.0** (released 29 July 2026), set once in `app/connectors/base/meta_graph.py`. Meta lists the end dates on its [Graph API changelog](https://developers.facebook.com/docs/graph-api/changelog); the Marketing API retires versions sooner (see its [versioning page](https://developers.facebook.com/docs/marketing-api/overview/versioning)). Facebook Page and Instagram connections that still hold the old form default (v23.0) use v26.0; a version someone typed is kept.

## LinkedIn (LinkedIn Ads, LinkedIn Pages)

LinkedIn requires the **Community Management API** to be the *only* product on its app. LinkedIn Ads (Advertising API) and LinkedIn Pages therefore need **two separate apps**, both linked to the platform's own LinkedIn Page.

### Register

1. At [linkedin.com/developers/apps](https://www.linkedin.com/developers/apps), create app 1 for LinkedIn Ads. Request the **Advertising API** product. Under **Auth**, add `…/api/v1/oauth/callback/linkedin`. Put its client ID and secret in `OAUTH_LINKEDIN_CLIENT_*`.
2. Create app 2 for LinkedIn Pages. Request only the **Community Management API**. Add `…/api/v1/oauth/callback/linkedin_pages`, and put its credentials in `OAUTH_LINKEDIN_PAGES_CLIENT_*`.
3. A super admin of the platform's LinkedIn Page must verify each app.

### Scopes

| Integration | Scopes | Why |
|---|---|---|
| LinkedIn Ads | `r_ads`, `r_ads_reporting`, `rw_ads`, `w_organization_social` | List ad accounts, read analytics, set campaign budgets and create draft creatives. `w_organization_social` creates the company-page post a creative sponsors. |
| LinkedIn Pages | `w_organization_social`, `r_organization_social`, `rw_organization_admin` | Post and upload images as the page, read its posts (health check), list the pages the member administers with their names, and read post metrics (`organizationalEntityShareStatistics`). No new scopes; metrics need the Community Management API. |

### Picker

- **LinkedIn Ads:** every ad account the member can reach (`/rest/adAccounts?q=search`). Removed and cancelled accounts are left out.
- **LinkedIn Pages:** company pages where the member is an Administrator or Content Administrator (`/rest/organizationAcls`), named from `/rest/organizations/{id}`.

### Access and review

- **Advertising API:** apply under the app's Products tab. The Development tier allows unlimited reads but edits on up to 5 ad accounts. Standard tier is requested by support ticket with a demo video, and review takes up to 30 business days.
- **Community Management API:** the Development tier is limited to 500 calls per app and 100 per member per day. Standard tier needs an access form, a screen recording of each use case and test credentials. Only registered legal organisations with a verified business email are eligible.
- **Refresh tokens:** LinkedIn issues these only to approved Marketing Developer Platform partners. Without them, LinkedIn sign-ins last 60 days and the console asks for **Sign in again**.

### Uncertain

- LinkedIn's documentation says the token response's `scope` is space-separated. Comma-separated values are reported in practice, so both are accepted.
- The organization field in `organizationAcls` is named both `organization` and `organizationTarget` in LinkedIn's samples, so both are read.

## X

### Register

1. In the [X developer portal](https://developer.x.com/en/portal/dashboard), open the project's app and go to **User authentication settings**.
2. Set **App permissions** to **Read and write**, and **Type of App** to **Web App, Automated App or Bot** (a confidential client).
3. Add the callback URI `…/api/v1/oauth/callback/x` and a website URL.
4. Copy the **OAuth 2.0 Client ID and Client Secret** (not the API key and secret) into `OAUTH_X_CLIENT_*`.

### Scopes

`tweet.read`, `tweet.write` and `users.read` are needed for `POST /2/tweets`. `GET /2/users/me` needs `users.read` and `tweet.read`. `offline.access` gives the refresh token. **Optional:** `media.write` uploads a post's image (chunked `POST /2/media/upload`). The flow uses PKCE (S256), and the token endpoint is called with HTTP Basic client authentication.

### What changed for customers

**Sign in with X** is now the default. The OAuth 1.0a keys (API key and secret, access token and secret) and a pasted OAuth 2.0 token still work under **Advanced**.

### Limits

- **Images need `media.write`.** With it, a post's image (JPG, PNG or WebP, up to 5 MB) is uploaded and attached. Without it, or if X refuses the upload with 403, the post goes out as text with a note, and the customer is asked to reconnect X to allow images. Connections made before this release do not have the scope.
- **Metrics:** each post's public metrics are read with `GET /2/tweets/:id`. X bills every read, so one metrics run reads at most `X_METRICS_MAX_READS_PER_RUN` posts (default 100).
- **Cost:** X's API is pay-per-use. Its pricing page lists a charge per post, and a higher one for posts that contain a link. The platform's X account pays for posts made through the platform's app. A refused post is reported as "ask your platform administrator".

### Uncertain

- That refresh tokens are single-use, and roughly how long they last, come from X staff answers on the developer forum, not from the documentation page. The connector stores each new refresh token either way.

## Pinterest

### Register

1. At [developers.pinterest.com/apps](https://developers.pinterest.com/apps/), create an app connected to the platform's business account.
2. Add the redirect URI `…/api/v1/oauth/callback/pinterest`.
3. Copy the App ID and secret key into `OAUTH_PINTEREST_CLIENT_*`.

### Scopes

`boards:read`, `boards:write`, `boards:read_secret`, `boards:write_secret`, `pins:read`, `pins:write`, `pins:read_secret`, `pins:write_secret` and `user_accounts:read` (names as on Pinterest's [scopes list](https://developers.pinterest.com/docs/getting-started/set-up-authentication-and-authorization/)).

- `boards:read`, `boards:write`, `pins:read` and `pins:write` are what Pinterest's API description lists for `POST /v5/pins`.
- The four `_secret` scopes let the connection check make a secret board with one Pin, read it back and delete both, so the test never posts publicly. They also let a secret board be chosen in the picker.
- `user_accounts:read` names the account for the health check.

**Enable all nine in the Pinterest app** before customers sign in. Connections made before the `_secret` scopes were added keep working for public boards. If the connection check needs a secret board and Pinterest refuses, the message says "Sign in again to grant the new permission."

**Picker:** boards from `/v5/boards`, followed through all pages.

**Metrics:** Pin analytics (`/v5/pins/{id}/analytics`) use the existing `pins:read` and `user_accounts:read` scopes, so no reconnect is needed.

### Access tiers

New apps get **Trial** access. Pins created under Trial are visible only to their creator (sandbox). Apply for **Standard** access (**My apps → Upgrade**) with a video of the full sign-in and a Pin being created, plus a privacy policy, before customers rely on it.

### Uncertain

- Whether `pins:write` and `boards:read` alone would be enough for `POST /v5/pins`. All four scopes Pinterest's API description lists are requested.
- Apps created before 25 September 2025 must opt in to continuous refresh tokens. Register a new app, or check the app's token settings.

## Reddit

Off by default. Commercial use of Reddit's API needs Reddit's **written approval**, so the integration stays hidden until an operator sets `REDDIT_ENABLED=true`. Get the approval first.

### Register

1. At [reddit.com/prefs/apps](https://www.reddit.com/prefs/apps), create an app of type **web app**, signed in as the account that will own it.
2. Set the redirect URI to `…/api/v1/oauth/callback/reddit`.
3. Copy the client ID and secret into `OAUTH_REDDIT_CLIENT_ID` and `OAUTH_REDDIT_CLIENT_SECRET`.
4. Set `REDDIT_USER_AGENT` to `<platform>:<app id>:<version> (by /u/<username>)`, as Reddit requires. Every call sends it.
5. Set `REDDIT_ENABLED=true` and restart the API.

### Scopes

`identity` (name the account), `mysubreddits` (list the communities to post in), `submit` (post) and `read` (read posts back with their score). All four are required. The sign-in asks for a permanent refresh token; access tokens last an hour and are refreshed automatically.

### Limits

- Posts to one subreddit are spaced at least `REDDIT_MIN_MINUTES_BETWEEN_POSTS` apart (default 10) per workspace. When Reddit rate-limits a post, it is rescheduled rather than failed.
- A post's picture is shared as a signed public link, valid for `PUBLISHED_LINK_DAYS` (default 365).
- Each community's own rules (title formats, flair) are enforced by Reddit when the post is submitted; a refusal is shown with a plain reason.
- Metrics are score, upvote ratio and comment count only.

## TikTok for Business (TikTok Ads)

### Register

1. Register as a developer at the [TikTok API for Business portal](https://business-api.tiktok.com/portal/apps) and wait for approval.
2. Create a developer app. Set the **Advertiser redirect URL** to `…/api/v1/oauth/callback/tiktok`.
3. Under **Scope of permission**, tick **Ad Account Management**, **Reporting**, **Ads Management** and **Creative Management**. TikTok sets permissions on the app, not per sign-in, and the consent screen lists the ones ticked here.
4. Submit for review, which takes about 2 to 3 business days. Copy the App ID and Secret into `OAUTH_TIKTOK_CLIENT_ID` and `OAUTH_TIKTOK_CLIENT_SECRET`.

### How the sign-in works

- The customer is sent to TikTok's advertiser authorisation page (`/portal/auth?app_id=…`), confirms, and comes back with an `auth_code`.
- The `auth_code` is exchanged for a long-term token (`/open_api/v1.3/oauth2/access_token/`).
- **Picker:** the advertisers that sign-in authorised (`/oauth2/advertiser/get/`, called with the platform's App ID and Secret).

### Uncertain

- The `/portal/auth?app_id=…&state=…&redirect_uri=…` URL form appears in TikTok's documentation only as a screenshot. The portal shows the exact **Advertiser authorization URL** for the app, so compare it after registering.

## Microsoft Advertising

### Register

1. In the [Microsoft Entra admin centre](https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade), create an app registration. Sign in with a work or school account; Microsoft no longer accepts a personal account for registration.
2. Set **Supported account types** to **Accounts in any organizational directory and personal Microsoft accounts**.
3. Add a **Web** redirect URI: `…/api/v1/oauth/callback/microsoft`.
4. Create a client secret. Copy the Application (client) ID and the secret value into `OAUTH_MICROSOFT_CLIENT_*`.
5. **Developer token:** sign in to the platform's own Microsoft Advertising account as a Super Admin. Open **Settings → Developer settings** (https://ads.microsoft.com/cc/Settings/DevSettings) and request a **universal** developer token. Put it in `MICROSOFT_ADS_DEVELOPER_TOKEN`.
   - The token is a platform setting. Customers never see or enter it.
   - Microsoft Ads sign-in is offered only when it is set.
   - A universal token works for any customer who signs in.

### Scope

`https://ads.microsoft.com/msads.manage` is the API's only scope, plus `offline_access` for the refresh token and `openid email` to show which account signed in. The flow uses PKCE and `prompt=select_account`.

**Sandbox:** the connect screen has a **This is a sandbox account** tick box. Ticked, the sign-in asks for `https://si.ads.microsoft.com/msads.manage` instead, at the same `login.microsoftonline.com/common` endpoint. This is the sandbox default in Microsoft's own [Bing Ads Python SDK](https://github.com/BingAds/BingAds-Python-SDK/blob/main/bingads/authorization.py). The connection is then saved as a sandbox one (`accountEnvironment = sandbox`), so the account picker, every API call and every token refresh use the sandbox hosts and scope. The same app registration serves both. The person signing in needs a sandbox user: an outlook.com Microsoft account, as Microsoft's [sandbox guide](https://learn.microsoft.com/en-us/advertising/guides/sandbox) requires.

**Picker:** every account of every customer the user has a role in (Customer Management `GetUser`, then `GetAccountsInfo` per customer). The customer ID and account ID are saved together, so the customer never types either.

### Notes

- API calls now send the token as `Authorization: Bearer`, which is what Microsoft's REST (JSON) documentation specifies, instead of the SOAP-style `AuthenticationToken` header.
- A customer whose Entra tenant blocks the Microsoft Advertising service principal sees error AADSTS650052, and their tenant admin has to allow it.
- Microsoft requires multi-factor authentication for Microsoft Advertising API sign-ins.

## Bing Webmaster Tools

### Register

1. In [Bing Webmaster Tools](https://www.bing.com/webmasters/), sign in with the platform's account. Go to **Settings → API Access**, accept the terms, and choose **OAuth client**.
2. Enter a name and the redirect URI `…/api/v1/oauth/callback/bing_webmaster`.
3. Copy the client ID and secret into `OAUTH_BING_WEBMASTER_CLIENT_*`.

### Scope

`webmaster.read`, because the connector only reads query stats and the site list. `webmaster.manage` would also allow URL submission and settings changes.

**Picker:** verified sites from `GetUserSites`.

### Fallback

Until this provider is set up, the integration shows its three-step API-key guide as the default path. Once it is set up, the key is still available under **Advanced: use an API key instead**.

### Uncertain

- Microsoft's OAuth page sends Bearer calls to `www.bing.com/webmaster/api.svc/json/…`, while the method reference uses `ssl.bing.com` with `?apikey=`. Signed-in connections use the `www.bing.com` host; key connections are unchanged.
- Microsoft's documentation says Bing refresh tokens last until revoked. Developers report them being replaced on each use. Any new refresh token returned is stored.
- Microsoft's page writes the scope as both `Webmaster.read` and `webmaster.read`. The lowercase form from its example URL is used.
- Whether the `state` parameter is echoed back is not documented. The flow depends on it, so test a sign-in after registering.

## Where this lives in the code

- **Providers:** `app/connectors/base/oauth_providers.py`, in the "phase 2A" block. The knobs are `scope_separator`, `token_auth`, `refresh_required`, `rotates_refresh_tokens` and `flavor`.
- **Vendor-specific flow steps:** `app/services/oauth_vendors.py`. This covers Meta's long-lived exchange and granted permissions, TikTok's authorize page and JSON exchange, and naming the signed-in account.
- **Storing replaced refresh tokens:** `app/services/oauth_tokens.py`, called from `OAuthTokenMixin` in `app/connectors/base/oauth.py`.
- **Each connector's `spec.oauth` and `list_oauth_choices()`:** in its own module.
- **Optional scopes:** `OPTIONAL_SCOPES` in `app/connectors/base/oauth_providers.py`.
- **Tests:** `tests/test_oauth_social_ads.py`.
