# Synaprank.ai — 10-minute walkthrough video

Script, shot list and prep for a screen recording in your own voice.
Speaking pace ≈ 140 words a minute; the spoken text below is ≈ 1,400 words.

---

## Before you record (30–45 minutes of prep)

Every screen in the video should show real data. Do these first:

1. **Database up to date** — in `SEO-Portal/backend`: `alembic upgrade head`, then restart the backend.
2. **A clean demo workspace** — use Alambrix.ai (or create "Demo"). Sign in as yourself (portal admin, so the Platform screens show).
3. **Brand profile** — Content → Brand profile → enter `alambrix.ai` → Create profile. Wait for it to finish.
4. **Connect what works today** (Automation → Integrations):
   - **Anthropic Claude** (or OpenAI) — your API key; pick the model from the dropdown.
   - **Site Crawler** — just the website address.
   - **WordPress** — if you have a test site with https.
   - *Optional:* set up **Sign in with Google** in Platform → Sign-in providers and connect Google Analytics / Search Console, so the one-click sign-in can be shown live.
5. **Run a few agents once** so their results exist: On-Page SEO Sync → Technical SEO Auditor → AEO Q&A Injector → SEO Article Writer (Run now on each, a few minutes apart).
6. **Run a Site analysis** of alambrix.ai (Optimization → Site analysis), ~3 minutes. Run it a second time later if you want the "What changed" tab to have content.
7. **Browser** — Chrome, 1920×1080, zoom 100%, bookmarks bar hidden, notifications off (Windows: Focus assist on), close other tabs.
8. **Rehearse once** with the script open on a second screen or phone.

> Be accurate on camera: ad platforms, social networks and CRMs show a **Beta** label until they pass a live check. Say "in Beta" for those rather than demonstrating money-moving actions live.

---

## How to record (easiest options)

- **Loom** (recommended): free, records screen + your microphone + optional webcam bubble, gives a shareable link for your manager. loom.com → install → "Screen + Cam" or "Screen only" → pick your microphone → record.
- **OBS Studio** (free, more control): add a *Display Capture* and *Audio Input Capture* (your mic), set Output → Recording to MP4, 1080p, 30 fps.
- **Microphone**: a headset or earphones mic beats the laptop mic. Record in a quiet room; do a 10-second test and listen back.
- **Tip**: record in 3–4 parts (pause between scenes) and join them, rather than one perfect 10-minute take. Loom and OBS both allow pausing.

---

## Script and shot list

Each scene: **On screen** (what to click) and **Say** (read aloud, in your own words where you like).

### 1. Opening — 0:00–0:45
**On screen:** Overview (dashboard).

**Say:**
"Hi, this is a walkthrough of Synaprank.ai — our AI marketing platform. In the next ten minutes I'll show how a business connects its tools in a few clicks, how our AI agents work on their website, search, content, ads and local presence, and how every change stays under the customer's control. Everything you see is the working portal."

### 2. Overview and getting started — 0:45–1:30
**On screen:** Point at the "Get started" steps, the activity panel, the left menu.

**Say:**
"This is the Overview. New customers get a short checklist: create the brand profile, connect their tools, switch on agents. The left menu follows that journey — Content, Optimization, Automation, Reports and Administration. Agents that are working show here live, so you always know what is running and what is waiting for you."

### 3. Brand profile — 1:30–2:15
**On screen:** Content → Brand profile. Show the filled profile; scroll through fields; open the Versions tab.

**Say:**
"Everything starts with the brand profile. You paste your website address and the AI reads your site and writes down what you sell, who you sell to, your tone of voice, your keywords and your locations. You can correct anything. Every content agent writes from this, so articles, ads and posts sound like the business — not like a template. Every change is kept as a version, and you can restore an older one."

### 4. Integrations — 2:15–3:45
**On screen:** Automation → Integrations. Scroll categories. Open **Google Analytics** (show Sign in with Google), then **OpenAI or Claude** (show the 3-step guide and model dropdown), then **Site Crawler** (just an address). Point at a **Beta** pill.

**Say:**
"Integrations are how the platform reaches your tools — Google Analytics and Search Console, Google and Meta Ads, LinkedIn, WordPress, Shopify, HubSpot, Mailchimp, Slack and more — almost forty in total.
We built this for marketers, not developers. For Google, Facebook, LinkedIn and most others you just click 'Sign in with Google', allow access and pick your property from a list — no keys, no tokens.
For tools that only give a key, like OpenAI or Claude, we show three simple steps with a button straight to the right page, check the key as soon as it's pasted, and then show the models from your own account in a dropdown.
And the simplest one: Site Crawler — just type your website address and the SEO agents can read every page.
You'll see a Beta label on some integrations. That's deliberate: an integration only loses it after it passes a live check against a real account."

### 5. Agents — 3:45–5:15
**On screen:** Automation → Agents. Show the groups. Open **SEO Article Writer** setup: Step 1 "What it needs" (connector dropdowns, AI model), Step 2 settings, Step 3 summary. Close. Open an agent that ran: show the live run timeline and the **Results** tab.

**Say:**
"Agents are the heart of the product. Each one does one job — fix technical SEO problems, rewrite weak pages, add question-and-answer sections so ChatGPT and Gemini can quote you, add schema, write articles, build search campaigns, post to social, write emails, reply to Google reviews, track your Google Maps ranking, and manage ad budgets.
Setting one up takes three steps. First, what it needs — and here the user chooses which connected tool the agent uses, and which AI model writes. Where it makes sense you can pick several, for example several websites for the SEO agents. Then a few plain settings, then a summary.
When an agent runs you see it step by step, live, and every agent has a Results tab with exactly what it found and what it changed."

### 6. Approvals and safety — 5:15–6:15
**On screen:** Approvals page (a waiting item, its before/after). Then Team & Access → Spending agents (practice mode, limits, Stop all agents). Point at the practice badge on an ads agent.

**Say:**
"Nothing goes live behind the customer's back. Changes can wait here in Approvals, with a clear before-and-after, and the workspace decides how much the agents may do on their own.
For anything that spends money we added extra safety. Spending agents start in practice mode — they read real data and plan real changes, but send nothing, and they tell you what they would have done this week. There are daily limits on budget moved and campaigns created, anything over a limit waits for a person, and there's a 'Stop all agents' button. Ad connections that haven't been verified never change anything automatically."

### 7. Content — 6:15–7:00
**On screen:** Content → Content library. Open an article preview, then show the tabs for search campaigns, social posts and emails; show Download options and Approve & publish.

**Say:**
"Everything the content agents write lands in the Content library — SEO articles, Google search campaigns, social posts with AI images, and email campaigns. You preview it exactly as it will look, edit it, and approve it. Approved content is sent to WordPress or your CMS as a draft, posted to your social accounts on schedule, or created as a draft campaign — or you can simply download it."

### 8. Optimization screens and history — 7:00–7:45
**On screen:** SEO & AEO (pages and scores, Score history tab), Technical SEO (findings, History tab), Off-Page & PR, Paid Media.

**Say:**
"The Optimization screens show the results in one place: page scores and rewrites, technical issues with the fix for each, backlink and PR opportunities, and paid media performance. Each one keeps a history — so you can see, run by run, what was fixed, what's new and how the score moved."

### 9. Site analysis and reports — 7:45–8:45
**On screen:** Optimization → Site analysis. Open the latest report; scroll the score, priorities, keyword table, ad previews. Click **Download PDF report**, then **Interactive report (HTML)**. Show the history table and "What changed".

**Say:**
"Site analysis is a full audit of any website in about three minutes: technical health, speed, AI-search readiness, local presence and tracking — every check measured, with the evidence — plus a strategy, keywords, ready-to-use ads, posts and emails written from the site's own content.
It downloads as a professional PDF report or an interactive web report, and every run is kept, so you can show a customer exactly what improved since last month. The same analysis powers the free check on our website, which is how new customers will discover us."

### 10. AI Assistant — 8:45–9:15
**On screen:** Open the Assistant dock; ask "Which agents should I switch on for a hair salon?"

**Say:**
"And if a user isn't sure where to start, the AI Assistant recommends which tools to connect and which agents to switch on for their business, and can set them up."

### 11. Platform administration — 9:15–9:45
**On screen:** Platform (Portal Admin) → tabs Workspaces, Catalogue, Analyses, Sign-in providers, Verification.

**Say:**
"Finally, our own admin side. We manage every customer workspace, switch features on or off, see every analysis — including the ones from the website — set up each sign-in provider once for all customers, and run live verification checks on every integration. There's also a platform-wide emergency stop."

### 12. Close — 9:45–10:00
**On screen:** Back to Overview.

**Say:**
"So that's Synaprank.ai: connect in a few clicks, let the agents do the work, and stay in control of every change. Next steps on our side are verifying the remaining integrations against live test accounts and onboarding our first customers. Thanks for watching."

---

## If you run short or long

- **Short on time:** shorten scene 8 to one sentence and skip scene 10.
- **Running long:** in scene 4, show only Google sign-in and Site Crawler.
- **Something fails on camera:** pause the recording, fix or skip it, and continue — Loom/OBS pauses join seamlessly.
