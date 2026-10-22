# Live verification: test and sandbox accounts

Every connector declares a few **live checks** (`verify_steps()` in its package).
They are run with `manage.py verify <slug>` against a real vendor account. This
page is the per-vendor list of the free test or sandbox account to create, what
the checks do there, what is safe, and what (if anything) it costs.

## How the checks behave

* **Read steps** never change anything. They call the same endpoints the agents
  use (list accounts, yesterday's report, list pages…), with tiny limits.
* **Write steps** create one harmless thing named **"Synaprank.ai verification …"**,
  read it back, and delete it **in the same step**. The delete runs even when
  the read-back fails. If the delete itself fails, the error names the id so it
  can be removed by hand.
* Write steps run only with `--allow-writes`. Steps marked **test account** also
  need the connection marked as a test or sandbox account. Mark it either with
  the connection's **Test account** setting, where the connector has one, or with
  `--environment test_account|sandbox` on `manage.py verify`.
* Spend-capable checks (Google Ads, Microsoft Ads, TikTok, LinkedIn Ads) also
  confirm with the vendor itself that the account is a test or sandbox account
  before creating anything. A check whose environment doesn't match refuses
  before making any call.
* No write step ever sends an email, schedules a campaign, enables an ad, or
  publishes a page.

The **Test account** setting (`accountEnvironment`) is on google_ads, meta_ads,
microsoft_ads, tiktok_ads, shopify, hubspot, slack and pinterest. Type *Test account*
or *Sandbox* into it; blank means a live account. Salesforce works it out from
its login address (see below).

---

## Ad platforms

### Google Ads: test account
- **Create it:**
  1. Sign in with a Google account that is **not** linked to your production manager account.
  2. Open <https://ads.google.com/nav/selectaccount?sf=mt> ("Create a test manager account").
  3. In the test manager, go to **Accounts → + → Create new account**. Client accounts made there are test accounts automatically and show a red "Test account" label.
- **Connection:**
  - **Customer ID** = the test client's 10-digit id.
  - **Login customer ID** = the test manager's id. This is required.
  - **Test account** = "Test account".
  - A developer token with only **test access** can call test accounts.
- **Checks:**
  - **Reads:** list reachable accounts; read the account, including `customer.test_account`; yesterday's metrics (empty in a test account); the placement report.
  - **Write, only with `--allow-writes` and only after Google confirms `test_account = true`:** one `googleAds:mutate` creating a 1.00/day budget and a **PAUSED** Search campaign; read it back with GAQL; remove the campaign, then the budget.
- **Safety and cost:** test accounts never serve ads and have no billing, so this is free. The limit is 50 test accounts per hierarchy, and accounts unused for a year are deleted.
- Docs: <https://developers.google.com/google-ads/api/docs/best-practices/test-accounts>, <https://developers.google.com/google-ads/api/docs/concepts/account-types>.

### Meta Ads: sandbox ad account
- **Create it:**
  1. At developers.facebook.com, open **My Apps** and pick a Business-type app where you are Admin or Developer.
  2. Add the **Marketing API** product.
  3. Go to **Marketing API → Tools** and create a **sandbox ad account**. There is one per app.
  4. Link it to a Page you admin.
  5. Generate a user token with `ads_management` and `ads_read`.
- **Connection:** Ad account ID = the sandbox `act_…`; **Test account** = "Sandbox". Sandbox accounts use the same graph.facebook.com addresses as live ones.
- **Checks:**
  - **Reads:** list ad accounts; read the account; insights. A sandbox has no insights, so a refusal there counts as a pass on a sandbox connection only.
  - **Write:** `POST /act_…/campaigns` with `status=PAUSED`, `objective=OUTCOME_TRAFFIC`, `special_ad_categories=[]` and `is_adset_budget_sharing_enabled=false`; read it back; `DELETE /{id}`.
- **Safety and cost:** a sandbox account never delivers ads and needs no payment method, so this is free.
- Docs: <https://developers.facebook.com/blog/post/2023/06/21/marketing-api-sandbox-capability-now-re-enabled>, campaign reference <https://developers.facebook.com/docs/marketing-api/reference/ad-campaign-group>.
- **Version:** Graph API `v26.0`, shared by every Meta connector (`app/connectors/base/meta_graph.py`). Creatives are sent with `destination_spec.destination_type = WEBSITE_AND_SHOP_OPT_OUT`, because from v26.0 Meta otherwise sends people to the advertiser's Meta shop when there is one ([v26.0 changelog](https://developers.facebook.com/docs/graph-api/changelog/version26.0)).

### Microsoft Advertising: sandbox (different addresses)
- **Create it:** this is free, with no payment details.
  1. Go to <https://sandbox.bingads.microsoft.com> and choose **Sign up now**.
  2. Use a **new @outlook.com Microsoft account**. Other email domains are not accepted.
  3. At **Go Live**, choose **Skip payment information**.
  4. Note the Customer ID and Account ID.
- **Developer token:** the universal sandbox token `BBD37VB98`. On a Sandbox connection with no developer token, the connector uses it automatically.
- **Sign-in:**
  - Use your own Entra app registration. It must allow personal Microsoft accounts (tenant `common`). No separate registration is required, and no universal sandbox client id is documented.
  - Authorize: `https://login.microsoftonline.com/common/oauth2/v2.0/authorize`
  - Token: `https://login.microsoftonline.com/common/oauth2/v2.0/token`. This is the same as live; the token address does not change.
  - Scope: **`https://si.ads.microsoft.com/msads.manage offline_access`**
  - **With the platform's sign-in:** tick **This is a sandbox account** before choosing **Sign in with Microsoft**, then sign in as the sandbox user. **Sandbox sign-in needs an outlook.com sandbox account**: Microsoft accepts only Microsoft accounts made with an outlook.com address in the sandbox. The sign-in asks for the sandbox scope, the connection is saved with **Test account = Sandbox**, and the account picker, API calls and token refreshes all use the sandbox.
  - **Without it:** get the refresh token yourself while signed in as the sandbox user. On the connection, under Advanced, paste the refresh token, client id and secret, and set **Test account = Sandbox**.
- **Sandbox hosts, switched automatically when Test account = Sandbox:**
  - `https://campaign.api.sandbox.bingads.microsoft.com/CampaignManagement/v13`
  - `https://reporting.api.sandbox.bingads.microsoft.com/Reporting/v13`
  - `https://clientcenter.api.sandbox.bingads.microsoft.com/CustomerManagement/v13`
- **Checks:**
  - **Reads:** list reachable accounts (`User/Query`, `AccountsInfo/Query`); read search campaigns; today's account performance report. Zero rows is normal in a sandbox.
  - **Write, sandbox only:** add a **Paused** Search campaign with a 1.00 daily budget and no ads; read it back (`Campaigns/QueryByIds`); delete it (`DELETE /Campaigns`).
- **Safety and cost:** sandbox ads never serve and payment isn't supported there, so this is free. Microsoft asks that the sandbox be used single-threaded; it may be down for maintenance.
- Docs: <https://learn.microsoft.com/en-us/advertising/guides/sandbox>, <https://learn.microsoft.com/en-us/advertising/guides/web-service-addresses>, <https://learn.microsoft.com/en-us/advertising/guides/authentication-oauth-get-tokens>.

### TikTok Ads: sandbox (different address)
- **Create it:** this is free.
  1. Create a TikTok for Business account and a developer app at <https://business-api.tiktok.com/portal> (My Apps).
  2. Open the app, then **Sandbox Ad Account** in the sidebar. It shows the sandbox Advertiser ID.
  3. Under **Access Token**, click **Generate**.
- **Connection:** paste the sandbox Advertiser ID and the generated token into the access-token field (no sign-in is involved), and set **Test account = Sandbox**.
- **Sandbox host:** `https://sandbox-ads.tiktok.com/open_api/v1.3` by default. Live is `https://business-api.tiktok.com/open_api/v1.3`.
  - This default comes from third-party guides. TikTok's official docs could not confirm it: the portal docs aren't publicly readable, and TikTok's official SDK doesn't name a sandbox host.
  - To change it without a code change, set **`TIKTOK_SANDBOX_BASE_URL`** in the server environment to the address your TikTok developer portal shows, including `/open_api/v1.3`. It must start with `https://`. Restart the API and workers afterwards.
  - On a Sandbox connection, a failed check ends with: "If TikTok rejects the sandbox address, set TIKTOK_SANDBOX_BASE_URL to the address shown in your TikTok developer portal."
  - The sandbox has no separate sign-in or token address here, because its token is generated in the portal and pasted in.
- **Checks:**
  - **Reads:** `advertiser/info`; today's integrated report. The report may be refused in a sandbox (error 40009), which hasn't been confirmed.
  - **Write, sandbox only:** `campaign/create` with `operation_status=DISABLE` and `BUDGET_MODE_INFINITE`; `campaign/get`; `campaign/status/update` with `operation_status=DELETE`.
- **Safety and cost:** a sandbox is isolated and never serves, so this is free.
- Docs: <https://business-api.tiktok.com/portal/docs/sandbox-accounts/v1.3>. That page could not be fetched automatically (HTTP 403). The request fields were cross-checked against TikTok's official SDK (github.com/tiktok/tiktok-business-api-sdk), which has no sandbox host.

### LinkedIn Ads: test ad account
- **Create it.** This can only be done through the API, not Campaign Manager. Send `POST https://api.linkedin.com/rest/adAccounts` with the `LinkedIn-Version` header and this body:
  ```json
  {"currency":"USD","name":"…","reference":"urn:li:organization:{id}","type":"BUSINESS","test":true}
  ```
  You can have one test account per developer app.
- **Connection:** use `--environment test_account`. The connector has no Test account setting.
- **Checks:**
  - **Reads:** list ad accounts; read the account, including `test`; today's analytics (always empty for a test account).
  - **Write, only after LinkedIn confirms `test: true`:** create a **DRAFT** campaign group with no budget; read it back; `DELETE` it. LinkedIn allows deleting only DRAFT groups.
- **Safety and cost:** test accounts are never served or billed, so this is free.
- Docs: <https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads/account-structure/create-and-manage-accounts>, <https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads/account-structure/create-and-manage-campaign-groups>.

### DSP / SSP exchange
This is the platform's own contract, so there is no vendor sandbox. Point **API host** at a staging adapter.
- **Checks:** read the summary report and the supply-quality report. Nothing is written, because nothing this connector creates can be undone.

---

## CMS and code hosts

### WordPress
- **Create it:** any self-hosted WordPress 5.6+ over HTTPS. A free local or staging install (wordpress.org, Local, InstaWP) is fine. Create an **Application Password** under Users → Profile.
- **Checks:**
  - **Reads:** `GET /wp-json` (the content API must be on); list published pages and posts.
  - **Write:** create a **draft** post through the agents' `create_page`; edit it through `write_page`; read it back with `context=edit` and confirm it is still a draft; `DELETE …?force=true`, which bypasses the trash.
- **Safety and cost:** a draft is never visible on the site. The write step runs with `--allow-writes`; it doesn't need a test account. Free.

### Shopify: development store
- **Create it:**
  1. Sign up at <https://www.shopify.com/partners> or the Dev Dashboard (<https://dev.shopify.com>).
  2. Go to **Dev stores → Add dev store**. It is free and processes no real transactions.
  3. Add a blog.
  4. Connect by signing in with the store address.
  5. Set **Test account**.
- **Checks:**
  - **Reads:** the shop; 5 pages and products; one page; the blogs.
  - **Write:** create an **unpublished** article through `create_page`; read it back and confirm `published_at` is empty; `DELETE` it.
- **Safety and cost:** nothing appears on the storefront, so a test account isn't needed. Apps subscribed to `articles/create` webhooks do see the event. Free.
- Docs: <https://shopify.dev/docs/apps/build/dev-dashboard/development-stores>, <https://shopify.dev/docs/api/admin-rest/latest/resources/article>.

### Webflow
- **Create it:** a free account and a site from the blog template; the Starter plan includes the CMS API, up to 50 items. Create a site token with CMS read and write, and set the articles collection.
- **Checks:**
  - **Reads:** collections; 5 items; one item.
  - **Write:** create one item with `isDraft:true`; read it back; `DELETE` it. The site is never published.
- **Cost:** free.

### Magento / Adobe Commerce
- **Create it:** there is no hosted sandbox, so use a local Magento Open Source 2.4 install (Docker).
  1. Go to **System → Extensions → Integrations**.
  2. Give it the resources Content → Pages (save and delete) and Stores.
  3. Turn on `oauth/consumer/enable_integration_as_bearer`.
- **Checks:**
  - **Reads:** store views; 5 CMS pages; one page.
  - **Write:** create a CMS page with `active:false`, which returns a 404 on the storefront; read it back; `DELETE` it.
- Docs: <https://developer.adobe.com/commerce/webapi/get-started/authentication/gs-authentication-token/>.

### GitHub / Bitbucket: test repository (test account)
- **Create it:**
  1. A throwaway repository with `main` and `content/*.md`.
  2. **GitHub:** a fine-grained token with Contents and Pull requests set to read and write.
  3. **Bitbucket:** a repository access token with Repositories: Write and Pull requests: Write.
  4. Run with `--environment test_account`.
- **Checks:**
  - **Reads:** the repository and push access; the deploy-branch head; 5 content files.
  - **Write (the article writer's PR flow):**
    1. Create branch `automarket-verify-<stamp>` and commit `automarket-verification/<branch>.md`.
    2. Open a PR titled "Synaprank.ai verification …" and read it back.
    3. **Close** it (GitHub) or **decline** it (Bitbucket).
    4. Delete the branch. The branch is deleted even when a middle step fails.
- **Safety:** the closed PR stays in the history, watchers are notified, and CI may run. That is why the write needs a test repository.

### Custom API and Webhooks / Zapier (test account)
- **Custom API:** use your own test receiver implementing the contract in `custom_api/connector.py`.
  - **Reads:** `/health`; `/pages`; one page.
  - **Write:** one **signed** PUT that sends a page back unchanged, with `"test": true` and an "Synaprank.ai verification ping" note. It then checks that nothing changed.
- **Webhooks / Zapier:** use a Catch Hook Zap (Webhooks by Zapier is premium; it may use one task) or a webhook.site URL.
  - **Read:** an offline address check (HTTPS, catch-hook path), with no call.
  - **Write:** one signed POST `{"event":"connection.test","subject":"Synaprank.ai verification ping","test":true}`. Point it at a test Zap.

---

## Social

### Facebook Pages
- **Create it:**
  1. Create a Meta developer app (<https://developers.facebook.com/apps>) and keep it in Development mode.
  2. Make sure your Facebook account holds an app role. Role holders can grant permissions without App Review.
  3. Meta has temporarily stopped new test users, so create a new, empty Page you admin (facebook.com/pages/create).
- **Checks:**
  - **Reads:** the Page; its 5 newest posts.
  - **Write (test account):** `POST /{page}/feed` with `published=false`; confirm `is_published=false`; `DELETE /{post}`. An unpublished post never appears on the timeline.
- **Cost:** free.
- Docs: <https://developers.facebook.com/docs/graph-api/reference/page/feed>.

### Instagram
- **Create it:** a new Instagram account switched to **Professional**, linked to the test Page above, and the same Meta app with `instagram_basic` and `instagram_content_publish`.
- **Checks:**
  - **Reads:** the account; its 5 newest media.
  - **Write (test account):** create a media **container** from a public JPEG and wait until it reports FINISHED. It is **never published**. Containers can't be deleted through the API; an unpublished one **expires after 24 hours**.
- **Cost:** free.
- Docs: <https://developers.facebook.com/docs/instagram-platform/content-publishing>.

### LinkedIn Pages
- **Create it:**
  1. Create a company page you admin and use only for testing (linkedin.com/company/setup/new). It should have **no followers**.
  2. Create a developer app (<https://www.linkedin.com/developers/apps>) with **Community Management API**. Development tier is fine.
- **Checks:**
  - **Reads:** your role on the page; its 5 newest posts.
  - **Write (test account):** LinkedIn has **no draft or private page post** (the Posts API accepts only PUBLISHED on create). The step **posts publicly and deletes the post immediately**. Its label says so.
- **Cost:** free.
- Docs: <https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api>.

### X (Twitter): costs money
- **Create it:** an X developer account (<https://developer.x.com>), an app with Read and write permission, and a throwaway X account.
- **Pricing:** checked on <https://docs.x.com/x-api/getting-started/pricing>. X is pay-per-use, with no free tier for new sign-ups:
  - creating a post: about **$0.015**
  - deleting a post: about **$0.010**
  - reading a user: about **$0.010**
- **Checks:**
  - **Read:** `GET /2/users/me`, about one cent.
  - **Write (test account, opt-in):** runs only when the connection carries `verifyAllowPaidPost=yes`. It **posts publicly**, then `DELETE /2/tweets/{id}` at once, about $0.025 in total. Without the opt-in the step stops with an explanation of the cost.

### Pinterest: sandbox
- **Create it:**
  1. Create a business account and an app at <https://developers.pinterest.com/apps>. Trial-access apps create only sandbox objects, visible to their creator.
  2. Under the app's **Configure** tab, choose **Generate access token → Sandbox**. The token lasts 30 days.
- **Connection:** paste the token and set **Test account = Sandbox**. The connector then calls `https://api-sandbox.pinterest.com/v5` and refreshes against that host. Sandbox and production tokens are not interchangeable.
- **Checks:**
  - **Reads:** the user account; the chosen board.
  - **Write (test account):** create a **SECRET** board, then one Pin on it; read the Pin; delete the Pin, then the board. The configured board is never touched.
  - A pasted token needs `boards:read_secret`, `boards:write_secret`, `pins:read_secret` and `pins:write_secret` for the write step. The platform's sign-in asks for them. A connection signed in before they were added gets "Sign in again to grant the new permission."
- **Cost:** free.
- Docs: <https://developers.pinterest.com/docs/developer-tools/sandbox/>, <https://developers.pinterest.com/docs/key-concepts/access-tiers/>.

---

## Email and collaboration

### Mailchimp: free plan
- **Create it:**
  1. Sign up at <https://login.mailchimp.com/signup/> on the Free plan.
  2. Create an API key under Profile → Extras → API keys (<https://admin.mailchimp.com/account/api/>), keeping the `-usNN` suffix.
  3. Find the Audience ID under Audience → Settings.
- **Checks:**
  - **Reads:** ping; read the audience (member count only).
  - **Write:** create a **draft** campaign through the agent's own call; read it back as `save`; `DELETE /campaigns/{id}`. It never sends, schedules or test-sends. A test account isn't needed, because a draft is only visible inside Mailchimp.
- **Cost:** free.

### Brevo: free plan
- **Create it:**
  1. Sign up at <https://onboarding.brevo.com/account/register>.
  2. Verify a sender.
  3. Generate a v3 key (`xkeysib-…`) at <https://app.brevo.com/settings/keys/api>.
  4. If authorised IPs are on, allow the runner's IP.
- **Checks:**
  - **Reads:** account (plan type); the sender is active; the configured lists.
  - **Write:** create a draft campaign with no schedule; read it back as `draft`; `DELETE`. Nothing is sent.
- **Cost:** free.

### Klaviyo: free account
- **Create it:** a free account at <https://www.klaviyo.com/sign-up>. A separate test account is recommended (<https://academy.klaviyo.com/en-us/courses/set-up-a-test-account>). Create a private key with full **Templates** access and read access to **Accounts**.
- **Checks:**
  - **Read:** the account.
  - **Write:** save one CODE email template; read it back; `DELETE /api/templates/{id}`. No campaign is created.
- **Cost:** free.
- **Note:** the sign-in now asks for `templates:read` as well. A connection signed in before that may have its read-back refused. The check then says "Sign in again to grant the new permission." The template is still deleted.

### Slack: free workspace or developer sandbox
- **Create it:**
  1. Create a free workspace, or a free Developer Program sandbox (<https://docs.slack.dev/tools/developer-sandboxes>).
  2. Add an app with `chat:write`, `chat:write.public` and `channels:read`, and install it.
  3. Create `#automarket-test`.
  4. Set **Test account** on the connection.
- **Checks:**
  - **Reads:** `auth.test`; `conversations.info` for the channel.
  - **Write (test account only):** post "Synaprank.ai verification ping — safe to ignore", then `chat.delete` it. A live workspace gets the read checks only.
- **Cost:** free.

### SMTP email
- **Create it:** no sign-up. Use any mailbox that allows SMTP submission, such as an app password on a spare mailbox, or Brevo's relay.
- **Check:** connect; secure the connection (STARTTLS on 587, implicit TLS on 465) with the certificate verified; log in; `NOOP`; quit. **Nothing is ever sent**, and there is no write step.

---

## Analytics, search and local (read-only)

| Connector | Account to create | What the checks read | Cost |
|---|---|---|---|
| Google Analytics 4 | A free GA4 property (analytics.google.com → Admin → Create). Enable the Data API and Admin API. | Account summaries; yesterday's sessions; top referrers | Free |
| Search Console | Verify a site you control at search.google.com/search-console | Site list; one day of query stats (25 rows) | Free |
| Bing Webmaster | Add a site at bing.com/webmasters, then generate an API key or sign in | `GetUserSites`; `GetQueryStats` | Free |
| Adobe Analytics | **No free tier.** Needs a licensed org; an OAuth Server-to-Server credential in Adobe Developer Console | Company discovery; up to 10 report suites; two small reports | No cost |
| DataForSEO | Register at app.dataforseo.com (about $1 trial credit) | Free `appendix/user_data`; one live backlinks call with 1 row | **About 2–3 US cents a run** |
| PageSpeed Insights | A Google Cloud key, or none | One run against developers.google.com/speed | Free |
| Google Business Profile | A real, verified listing, plus Business Profile API access approved for the project. **There is no sandbox.** | Accounts and locations; the location; yesterday's impressions; 5 newest reviews | Free |
| Google Places | A Cloud project with billing and Places API (New) | One Text Search, pageSize 1 | About 3.5 US cents each, inside the free monthly allowance for most projects |

Notes on this group:
- **Google Business Profile** has no write step. A review reply is public the moment it is posted.
- **DataForSEO** also offers a free sandbox host (`sandbox.dataforseo.com`). It isn't wired up yet.

---

## CRM (read-only)

### Salesforce: Developer Edition or sandbox
- **Free option, Developer Edition:**
  1. Sign up at <https://developer.salesforce.com/signup>.
  2. Connect with the normal sign-in.
  3. Its address (`*.develop.my.salesforce.com`) is read as a test org.
- **Paid org, sandbox:**
  1. Go to **Setup → Sandboxes → New Sandbox (Developer)**.
  2. Connect with **"This is a sandbox"** ticked, which sets the login URL to `https://test.salesforce.com`. Or use the sandbox's My Domain login (`…--name.sandbox.my.salesforce.com`).
- **How the environment is worked out:**
  - **Sandbox:** the login URL is `test.salesforce.com`, or an address ends in `.sandbox.my.salesforce.com`.
  - **Test account:** a `.develop.` or `.scratch.` My Domain address.
  - **Otherwise:** the optional `accountEnvironment` value.
- **Checks:** org API limits; one `COUNT()` of won opportunities from the last 30 days (the agent's own query); the activity totals query with `LIMIT 5`. Read-only, 3 API calls.

### HubSpot: developer test account
- **Create it:**
  1. In a HubSpot account (free is fine), go to **Development → Testing → Test Accounts → Create developer test account**.
  2. A test account expires after 90 days without API calls.
- **Connection:** connect inside the test account and set **Test account** = "Test account".
- **Checks:** deal pipelines and their won stages; the won-deal search (limit 1); one contact's engagement properties. Read-only.
- Docs: <https://developers.hubspot.com/docs/getting-started/account-types>.

---

## AI models (read-only)
There are no sandboxes. Each vendor just needs an API key with a little credit:
- OpenAI: <https://platform.openai.com/api-keys>
- Anthropic: <https://platform.claude.com/settings/keys>
- Gemini: <https://aistudio.google.com/api-keys>. The free tier works.
- Perplexity: <https://console.perplexity.ai>. Needs prepaid credit.

**Checks:**
- List the models the key can use and confirm the chosen model is there. Perplexity has no models list, so it skips this.
- Send one "Reply with OK" completion capped at **16 output tokens**, with no web search.

**Cost:**
- OpenAI, Anthropic, Gemini: well under a cent.
- Perplexity: about 0.5–1.5 US cents, depending on the model. Don't use `sonar-deep-research` for the check.
- A reasoning model that spends the 16 tokens on thinking still passes; the report says so.
