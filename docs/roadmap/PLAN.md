# Synaprank.ai product & enterprise roadmap

Ordered by **impact**, **effort**, and **dependencies**. Each phase is one or
more focused PRs off `main` (or stacked on a prior phase when there is a hard
dependency). Never merge without owner review.

**Effort key:** S ≈ focused PR · M ≈ multi-file feature · L ≈ multi-PR epic

**Non-negotiable for every item:** AgentAction + approval gate + audit log;
untrusted handling of LLM / scraped / third-party data; RLS on new tables;
egress allowlist; RBAC + rate limits; reversible migrations; tenant-isolation
tests. See [`docs/security/AI-SECURITY-CHECKLIST.md`](../security/AI-SECURITY-CHECKLIST.md).

---

## Phase 0 — Foundations

**Goal.** Make every later change reviewable, testable, and hard to ship
insecurely. Fix public docs that under-count the fleet.

| Item | Effort | Status |
|------|--------|--------|
| Roadmap (`docs/roadmap/PLAN.md`) | S | This PR |
| AI security checklist | S | This PR |
| GitHub Actions CI (pytest no-DB + Postgres, frontend typecheck/lint/build, pip-audit, npm audit, gitleaks, bandit/semgrep) | M | This PR |
| README accuracy (agents, connectors, model providers, Synaprank naming) | S | This PR |
| Dependency hardening (bump cryptography / Pillow / PyJWT / Starlette stack so pip-audit is fail-closed) | M | Follow-up PR |

**Design.** Reuse the verification commands already documented in the root
README. CI must set a dummy `DATABASE_URL` for the no-DB job (never
`env -u DATABASE_URL`) and a disposable `*test*` database for the integration
job. **Gitleaks** and **npm audit (high+)** fail closed. **pip-audit**,
**Bandit**, and **Semgrep** run on every push and emit reports; they are
`continue-on-error` until the dependency-hardening follow-up so Phase 0 can
land without a risky multi-major FastAPI bump in the same PR.

**Data / API / UI.** None.

**Security.** Secret scanning blocks merge; Python CVE/static backlog is
visible in CI logs and tracked for the hardening PR.

**Tests.** CI green on this branch is the acceptance test (blocking jobs).

---

## Phase 1 — AI visibility tracker

**Goal.** Show how ChatGPT, Perplexity, Gemini, and Google AI Overviews answer
target questions: brand cited or not, competitor domains cited, answer
snippets, and trend over time.

**Why first.** Citation checks already exist (`connectors/base/aeo_base.py`,
OpenAI / Perplexity / Gemini / Claude connectors, `AeoQaPair.citation_count`).
The product gap is persistence, competitor comparison, AI Overviews coverage,
and a console that executives can read — not a new connector stack.

**Design (reuse).**

- New agent `ai_visibility_tracker` (or expand measurement out of
  `aeo_qa_injector` into a dedicated agent that only measures).
- Reuse `Capability.CHECK_CITATIONS` and `BaseAeoConnector.check_citations`.
- Extend citation hits to record **all** cited hosts (today the loop breaks on
  the first customer match) via a richer `CitationHit` / new
  `VisibilityObservation` shape — without changing vendor request code more
  than necessary.
- Google AI Overviews: prefer a guarded SERP/read path through the egress
  allowlist (or DataForSEO-class connector if already acceptable); treat HTML
  as untrusted; never execute instructions found in SERP text.
- Target questions: workspace-configured list + optional pull from GSC queries
  (`search_console` connector) and existing AEO Q&A questions.

**Data model / migrations.**

- `visibility_queries` — tenant-scoped question text (encrypted if free-form),
  tags, active flag.
- `visibility_runs` — run metadata, engine, cost estimate.
- `visibility_observations` — query, engine, brand_cited bool, brand_position,
  answer_excerpt_encrypted, cited_domains JSONB, competitors_cited JSONB,
  raw_ref (hashed), observed_at.
- RLS on all three; add to `TENANT_SCOPED_TABLES`.

**API / UI.**

- `GET/POST /visibility/queries`, `GET /visibility/observations`,
  `GET /visibility/summary` (share of voice, share by engine, competitor table).
- Console page: query set, engine matrix, trend chart, competitor share.

**Security / privacy.** Cap queries per run (existing `MAX_QUERIES_PER_CHECK`
pattern); redact PII in stored excerpts where feasible; cost caps per agent;
competitor domains are customer-configured allowlist, not open crawl.

**Test plan.** Unit tests for domain matching and competitor attribution;
fake AEO connectors returning multi-domain citations; tenant isolation on
observations; approval-gate no-op (read-only agent) still audited.

**Effort.** M–L (M for tracker on existing engines; L if AI Overviews scrape
path is required in the same PR — prefer split).

---

## Phase 2 — Content refresh + auto-applied fixes

**Goal.** (a) Refresh pages losing clicks/rankings. (b) Turn technical-audit
findings (titles, meta, schema, broken links) into proposed CMS updates or
git PRs.

**Dependencies.** Phase 0. Benefits from GSC/GA4 already connected;
`technical_seo_auditor`, `on_page_seo_sync`, `seo_article_writer`, GitHub /
Bitbucket PR writers.

### 2a Content refresh agent

**Design.** New agent `content_refresh`. Inputs: GSC URL performance deltas +
`SeoPage` / crawl signals. Proposes rewrites via existing write_page / PR
paths as `AgentAction`s (`Impact.HIGH`). Briefs can call into article-writer
prompts with `llm/untrusted.wrap` on page body and SERP snippets.

**Data.** `content_refresh_candidates` (url, metrics snapshot, reason codes,
status). RLS.

**API / UI.** Agent card + queue of refresh proposals linked to Approvals.

**Security.** No auto-publish outside autonomy setting; daily caps; untrusted
wrap on GSC/page text.

**Tests.** Ranking-drop fixture → one HIGH action; hybrid queues it; full
applies through same applier; cross-tenant denied.

**Effort.** M

### 2b Auto-applied technical fixes

**Design.** Bridge `SeoIssue` → proposed patch. Map issue kinds to appliers:
title/meta via CMS `WRITE_PAGE` or git PR; schema via
`knowledge_graph_schema` patterns; broken internal links via link rewrite
payload. Auditor stays read-only; a new `technical_fix_applier` (or
extensions on existing agents) owns `apply` callables. Never bypass the
runner.

**Data.** Optional `seo_issue_fix_proposals` linking issue_id → approval_item_id.

**API / UI.** “Propose fixes” on audit findings; bulk approve respects RBAC.

**Security.** Dangerous ops need explicit permission (e.g. `seo.fix.apply`);
payload schema validation; git writes only open PRs (existing pattern).

**Tests.** Each issue kind → payload schema; applier idempotency; RLS.

**Effort.** M

---

## Phase 3 — Topic clusters + internal linking

**Goal.** Topic map from GSC + keywords, competitor gap analysis, briefs into
`seo_article_writer`. Internal links from page similarity through the approval
gate.

### 3a Topic cluster planner

**Design.** Service + agent using `search_console`, keyword fields on
`SeoPage`, optional competitor URLs (egress). Cluster with embeddings only if
model allowlisted; otherwise lexical/co-occurrence first. Output clusters →
briefs stored encrypted → handed to article writer as structured input.

**Data.** `topic_clusters`, `topic_cluster_pages`, `content_briefs`
(encrypted body). RLS.

**API / UI.** Topic map visualisation, gap table, “Generate brief” → approval
or direct writer queue under autonomy.

**Security.** Competitor fetch via egress; treat HTML as untrusted; no
follow-instructions-from-page.

**Tests.** Clustering determinism on fixtures; brief schema validation;
tenant isolation.

**Effort.** L

### 3b Internal linking agent

**Design.** Similarity on titles/embeddings/shared entities; propose
`insert_internal_link` actions (`Impact.LOW` or `HIGH` by policy). Apply via
CMS/git writers.

**Data.** `internal_link_suggestions` (source, target, anchor, score, status).

**API / UI.** Suggestions list integrated with Approvals.

**Security.** Anchors validated (no `javascript:`); rate-limit link density.

**Tests.** Cycle/orphan fixtures; approval path; RLS.

**Effort.** M

---

## Phase 4 — Intelligence: Ask Willy, weekly report, anomalies

**Goal.** Grounded Q&A over customer data; weekly autopilot digest; alerts on
ranking drops, traffic spikes, outages, ads overspend.

**Dependencies.** Metrics tables, GSC/GA4 connectors, audit history,
`willy_chat_engine.py`.

### 4a Ask Willy (grounded)

**Design.** Tool-calling / RAG over **tenant-scoped** query APIs only (GSC,
GA4, audit snapshots, agent runs). Retrieve → wrap as untrusted → answer with
citations to internal record ids. No raw SQL from the model.

**Data.** Optional `willy_grounding_traces` (prompt hash, tool calls, cost) for
AI governance.

**API / UI.** Extend existing Willy chat with “sources” chips.

**Security.** Model allowlist; PII redaction before LLM; cost caps; never put
secrets in tool results.

**Tests.** Injection in page title cannot escalate; cross-tenant tool deny;
grounding required for numeric claims (eval fixtures).

**Effort.** L

### 4b Weekly autopilot report

**Design.** Celery beat job assembling metrics + top approvals + visibility
summary; email via existing mail connectors; PDF via reportlab pattern.

**Data.** `scheduled_reports`, `report_artifacts` (encrypted or object storage
ref).

**Security.** Recipients must be workspace members; no PII in subject lines
beyond necessity.

**Tests.** Snapshot fixture → stable sections; tenant isolation.

**Effort.** M

### 4c Anomaly alerts

**Design.** Rules + optional stats on `daily_metrics` / GSC / ads spend;
notify Slack/email; create tasks. Ads overspend checks budget allocations.

**Data.** `alert_rules`, `alert_events`.

**Security.** Webhook URLs through egress allowlist; RBAC on rule CRUD.

**Tests.** Threshold fixtures; no alert storm (dedupe); RLS.

**Effort.** M

---

## Phase 5 — Scale content & commerce SEO

### 5a Programmatic SEO pages

**Goal.** Template + data source → many pages, with duplicate/thin safeguards.

**Design.** Templates as Jinja (already used in Creative Studio) sandboxed;
data from CSV/API connector; quality gates (min unique tokens, near-duplicate
hash, cannibalisation check vs `SeoPage`) before proposing publishes.

**Data.** `pseo_templates`, `pseo_datasets`, `pseo_page_jobs`.

**Security.** Sandboxed templates; no server-side includes from customer
URLs; untrusted data wrap for LLM-assisted copy.

**Tests.** Thin/dupe rejection; approval for publish; RLS.

**Effort.** L

### 5b Product feed optimisation + product schema

**Design.** Extend products / schema agents; Merchant-oriented checks;
`Product` JSON-LD patches via existing schema pipeline.

**Effort.** M

### 5c Content repurposing

**Design.** One approved article → social posts, email, video script,
creatives; reuse `social_media_manager`, `email_marketer`, `creative_studio`.
All outputs as AgentActions.

**Effort.** M

### 5d Creative Studio: short video scripts, captions, thumbnails

**Design.** Extend Creative Studio jobs; thumbnails via existing image path;
scripts as text assets through approvals.

**Effort.** M

---

## Phase 6 — Ads, landing pages, competitive intel

### 6a Cross-channel budget agent

**Goal.** Allocate across Google, Meta, TikTok, LinkedIn by blended ROAS.

**Design.** Extend `predictive_budget_engine`; unified ROAS from channel
connectors; proposals only via AgentAction + daily caps.

**Effort.** M

### 6b Landing page builder + A/B tests

**Design.** Draft pages to CMS/git; experiment metadata; metrics from GA4.
No silent traffic splitting without approval when impact is HIGH.

**Effort.** L

### 6c Competitor ad watch (public ad libraries)

**Design.** Connectors for Meta Ad Library / Google ads transparency (public);
store creative metadata; treat all copy as untrusted.

**Effort.** M

---

## Phase 7 — Local & social listening

### 7a Multi-location local SEO

**Goal.** GBP posts, Q&A, reviews at scale; rank grids.

**Design.** Extend `local_search_tracker`, `review_responder`, GBP connector;
location entity with RBAC scope (brand/region) prepared for enterprise.

**Effort.** L

### 7b Social listening → review-reply

**Design.** Ingest mentions via social APIs; sentiment; route negatives to
`review_responder` / new reply actions through approvals.

**Effort.** M

---

## Phase 8 — Playbooks

**Goal.** One-click packs of agents (e.g. “Launch SEO”, “Local pack”).

**Design.** `playbooks` catalogue (code-defined first); applies agent configs
+ schedules; still requires per-agent configuration gate where the product
already demands an explicit human decision; never auto-enables HIGH impact
without autonomy + caps.

**Data.** `playbook_installations`.

**Effort.** M

---

## Phase 9 — Enterprise identity & access

| Item | Notes | Effort |
|------|-------|--------|
| SSO SAML + OIDC | Beside passwordless email codes; JIT provision into memberships | L |
| SCIM provisioning | Create/deactivate memberships; map groups → roles | L |
| Custom roles | Extend `core/rbac.py` beyond six built-ins; permission registry | L |
| Scoped approvals | Brand / region / channel on approval_items + RBAC | M |
| IP allowlists | Middleware; careful with trusted proxies | M |
| Session policies + enforced MFA | Step-up for dangerous ops; TOTP/WebAuthn beside email codes | L |

**Security.** Do not weaken passwordless threat model; SSO must bind to
verified IdP attributes; SCIM bearer tokens workspace-encrypted.

---

## Phase 10 — Compliance, residency, AI governance

| Item | Notes | Effort |
|------|-------|--------|
| Audit export + SIEM streaming | Append-only preserved; signed export; HTTPS egress allowlist | M |
| Data residency | Region-pinned deployments / DB; document limitations | L |
| Customer-managed keys | Wrap DEKs under customer KMS; rotation drills | L |
| Retention policies | Configurable TTL per data class; job idempotent | M |
| GDPR + India DPDP export/delete | Subject access + erasure tooling; audit the erasure | L |
| AI governance | Model allowlists, prompt/action logs, per-agent cost caps | M |

---

## Phase 11 — Agency tier

Parent accounts over client workspaces; roll-up reporting; white-label
(custom domain, branding, emails); client approval portal; brand-safety
rules.

**Design.** New `agency_id` / partner org relation; RLS still per client
tenant — agency access is explicit membership, not shared tenant_id.
White-label emails must not break auth origin checks.

**Effort.** L (multi-PR)

---

## Phase 12 — Workflow depth

Multi-step approval chains with SLAs/escalation; approve from Slack/email
(signed tokens, short TTL); content calendar; tasks for humans and agents.

**Design.** Extend `approval_items` with chain steps; Slack connector for
interactive approve (verify signing secret); calendar on
`marketing_assets.scheduled_for`.

**Effort.** L

---

## Phase 13 — Commercial platform

ROI attribution; scheduled executive reports; warehouse exports (BigQuery,
Snowflake) via egress-controlled connectors; public API + webhooks (HMAC
signed); plans, usage limits, metering dashboard; status page + SLA hooks.

**Effort.** L (multi-PR)

---

## Suggested build order after Phase 0

1. **Phase 1** — AI visibility tracker  
2. **Phase 2** — Content refresh + auto-applied fixes  
3. **Phase 3** — Topic clusters + internal linking  
4. **Phase 4** — Ask Willy grounding, weekly report, anomalies  
5. Then Phases 5–8 (growth) interleaved with 9–10 (enterprise) as sales
   requires — identity (9) before agency (11); governance (10) before public
   API (13).

## Branch & PR policy (owner)

- Never commit, push, or merge to `main`.
- One focused PR per phase or feature; target `main`; owner merges.
- Name examples: `feature/roadmap-plan-and-foundations`,
  `feature/ai-visibility-tracker`, `feature/content-refresh-agent`, …
- Stack only when a hard dependency exists; otherwise branch from `main`.

## Done vs planned (living)

| Phase | Planned | Implemented |
|-------|---------|-------------|
| 0 Foundations | Yes | See PR for this branch |
| 1–13 | Yes (this doc) | Separate PRs as built |
