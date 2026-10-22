# Sign-in providers: setup for platform operators

Customers connect integrations with a **Sign in with Google** button, or by approving access on their own WordPress site, instead of pasting API keys, client IDs or refresh tokens. This works only after you register the platform's own app with each vendor once. This page covers the vendors in phase 1: Google and WordPress.

Until a provider is set up, its integrations show the sign-in button switched off with one sentence ("Sign in with Google isn't switched on for this workspace yet…") and a **Notify my admin** button, which adds the request to the workspace's notifications. Workspace admins are told to ask the platform administrator; portal admins get a **Set up Sign in with Google** link. Admins can still reach the connect-with-your-own-app-credentials form through a secondary "For developers" link. Bing Webmaster Tools, Mailchimp and Klaviyo keep their three-step key guide as the default meanwhile.

**Portal Admin → Sign-in providers** is a setup wizard per provider: status, numbered steps with a link to the vendor's console, the exact redirect URI with a Copy button, the permissions to switch on, and boxes for the values the console gives you. **No server restart or environment editing is needed.** Values saved there are encrypted with the platform master key, are never shown again (only the last four characters of a secret), and take the place of the environment keys below, which still work as a fallback. Google and GitHub credentials are checked with the vendor as soon as they are saved; other vendors are confirmed by connecting an integration. Saves and removals are written to the audit log.

## The redirect URI

```
{PUBLIC_API_BASE_URL or PUBLIC_BASE_URL}/api/v1/oauth/callback/{provider}
```

| Deployment | Example |
|---|---|
| Local development (Vite proxies `/api` to the API) | `http://localhost:5173/api/v1/oauth/callback/google` |
| Console and API on separate hosts (Render) | set `PUBLIC_API_BASE_URL=https://automarket-api.onrender.com`, which gives `https://automarket-api.onrender.com/api/v1/oauth/callback/google` |

After a sign-in, the browser is sent back to `PUBLIC_BASE_URL`, and only there: the return path is checked to be a path on the console.

## Google (Analytics 4, Search Console, Google Ads, Business Profile)

### 1. Create the OAuth client

1. In the [Google Cloud console](https://console.cloud.google.com/), create or pick the project that will own the platform's app.
2. Enable these APIs (**APIs & Services → Library**):
   - Google Analytics Data API and Google Analytics Admin API (GA4)
   - Google Search Console API
   - Google Ads API
   - My Business Account Management API, My Business Business Information API, Business Profile Performance API and Google My Business API (Business Profile). These need [Business Profile API access](https://developers.google.com/my-business/content/prereqs) to be approved first. Until it is, their quota is zero and sign-in succeeds but listing locations fails.
3. Set up the **OAuth consent screen** (Google Auth Platform → Branding / Audience / Data access): app name, support email, authorised domain, privacy policy and terms links.
4. Under **Data access**, add the scopes below.
5. Under **Clients**, create an **OAuth client ID** of type **Web application**, and add the redirect URI above under *Authorised redirect URIs*. Add each environment's URI (local, staging, production) separately.
6. Copy the client ID and secret into the API server's environment.

### 2. Scopes

Each integration asks only for its own scope when it is connected (incremental authorisation, `include_granted_scopes=true`). `openid` and `email` are always added so the console can show which account signed in.

| Integration | Scope | Why |
|---|---|---|
| Google Analytics 4 | `https://www.googleapis.com/auth/analytics.readonly` | Reports and the property list. It never writes to Analytics. |
| Search Console | `https://www.googleapis.com/auth/webmasters.readonly` | Search analytics and the site list. |
| Google Ads | `https://www.googleapis.com/auth/adwords` | This is the Ads API's only scope. It covers reporting and the budget, ad and campaign changes the platform makes. |
| Business Profile | `https://www.googleapis.com/auth/business.manage` | This is the only Business Profile scope. The platform reads listings and reviews and posts the review replies you approve. |
| (all) | `openid`, `email` | Shows "Signed in as …" and pre-selects the same account next time. |

### 3. Environment keys

```
OAUTH_GOOGLE_CLIENT_ID=…apps.googleusercontent.com
OAUTH_GOOGLE_CLIENT_SECRET=…
GOOGLE_ADS_DEVELOPER_TOKEN=…        # required only for Google Ads sign-in
PUBLIC_API_BASE_URL=https://…       # when the API is not on PUBLIC_BASE_URL
```

Restart the API after changing them. Google Ads sign-in is offered only when `GOOGLE_ADS_DEVELOPER_TOKEN` is set too. The token belongs to the platform's own Google Ads manager account (**Tools → API Center**), and customers never see or enter it.

### 4. Verification and access levels

- **Testing first.** While the consent screen's publishing status is *Testing*, only the Google accounts listed as **test users** can sign in. Google also expires refresh tokens for apps in Testing after 7 days, so a connection made then stops working a week later and has to be signed in again.
- **Production needs verification.** The Analytics, Search Console, Ads and Business Profile scopes are not basic scopes. Publishing the app to *In production* for everyone requires Google's OAuth app verification. The **Data access** page shows each scope's classification. Allow time for Google's review, which needs a demo video, a privacy policy and domain verification.
- **Google Ads developer token.** A token with *Test* access reaches only test accounts. Apply for **Basic** access (or Standard) in the API Center before customers connect real accounts.
- **Business Profile** needs the API access approval described in step 1.

### What the customer sees

1. They click **Sign in with Google**. A sentence under the button says what access is asked for and why.
2. They sign in at Google and allow access, then come back to the same screen.
3. They pick the GA4 property, Search Console site, Google Ads account or business location. If there is only one, it is picked automatically.
4. The connection is tested, then saved.

For Google Ads, accounts reached through a manager (MCC) account are listed with the manager. The platform sends the manager's ID as `login-customer-id`, so the customer never types it.

After one Google integration is connected, the drawer offers **Also connect with this Google account** for the others. If the account has already granted that integration's scope, it connects without leaving the page. Otherwise Google asks only for the extra permission, with the same account pre-selected.

### How tokens are kept

The refresh token, access token and its expiry are stored in the integration's encrypted credentials under the workspace's own key, the same as any pasted secret. They are never returned to the browser or written to logs. The query strings of callback URLs are also removed from the access log. The connection is marked as using the platform's app, so refreshes use `OAUTH_GOOGLE_CLIENT_*` from the environment. If those are removed later, these connections report that the platform's sign-in app is no longer set up.

Workspaces that pasted their own refresh token, client ID and secret keep working unchanged. That route is still available under **Advanced: connect with your own Google Cloud app**.

## WordPress (one-click approval)

There is nothing to register. WordPress's built-in **Authorize Application** screen creates an application password for the customer's user and hands it back to the platform.

- The customer types their site address and clicks **Continue to WordPress**. They sign in to their site if asked and click **Yes, I approve of this connection**, then come straight back.
- Requirements: WordPress 5.6 or newer with application passwords enabled. Some security plugins turn them off, and the console says so. The site must use **https**.
- The platform's callback must also be **https**. WordPress refuses an `http://` success URL unless the WordPress site itself is set up as a local development environment. Local testing against a real site therefore needs an https tunnel to the API, with `PUBLIC_API_BASE_URL` set to the tunnel's address.
- The site address is checked against the internal-address guard before anything is fetched. The approval must come back from the same site, and the approval link is signed and single-use.
- The app appears in each user's profile under **Application Passwords** as *Synaprank.ai*, where the site owner can revoke it at any time.

Optional environment key: `WORDPRESS_APP_ID` (a UUID that identifies the platform to WordPress; a fixed default is built in).

## Security summary

- The state is HMAC-signed with `SECRET_KEY`, expires after `OAUTH_STATE_TTL_MINUTES` (10 by default) and can be redeemed once. Each sign-in has a row in `oauth_states` (migration `0016_oauth_states`) that is deleted atomically when the callback redeems it, so a replay fails on every worker.
- PKCE (S256) is used for Google. The verifier stays on the server, encrypted under the workspace key.
- The callback re-checks that the person who started the sign-in is still active and can still manage integrations.
- Errors such as cancelled consent, an expired link or a refused exchange come back as one plain sentence on the console, never as a stack trace.

## Adding a vendor (phase 2)

1. Add `oauth_<vendor>_client_id` and `oauth_<vendor>_client_secret` to `Settings` (`app/core/config.py`) and to `.env.example`.
2. Add an `OAuthProvider` (authorize and token URLs, PKCE, extra parameters) to `PROVIDERS` in `app/connectors/base/oauth_providers.py`.
3. On each of the vendor's connectors, set `spec.oauth = OAuthBinding(provider=…, scopes=…, choice_fields=…, access_summary=…)` and implement `list_oauth_choices()` if the account has to pick something.
4. Mix in `OAuthTokenMixin` with `token_url` if its tokens are refreshed.
5. Register the redirect URI that Portal Admin shows, and add a section to this page.

Social and ad platforms (Meta, LinkedIn, X, Pinterest, TikTok, Microsoft Advertising, Bing Webmaster Tools): see [oauth-setup-social-ads.md](oauth-setup-social-ads.md).

Business apps (Slack, HubSpot, Salesforce, Mailchimp, Klaviyo, Shopify, GitHub, Bitbucket, Webflow, the included PageSpeed/Places keys, Adobe Analytics and Zapier): see [oauth-setup-business-apps.md](oauth-setup-business-apps.md).
