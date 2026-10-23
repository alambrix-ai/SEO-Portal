# AI security & privacy checklist — Synaprank.ai

Use this on every feature PR. Prefer stopping and explaining a conflict over
weakening an existing control.

References: [OWASP Top 10 for LLM Applications](https://owasp.org/www-project-top-10-for-large-language-model-applications/),
[OWASP Agentic AI Top 10](https://genai.owasp.org/),
[OWASP ASVS](https://owasp.org/www-project-application-security-verification-standard/),
[MITRE ATLAS](https://atlas.mitre.org/),
[NIST AI RMF](https://www.nist.gov/itl/ai-risk-management-framework),
Dishant’s secure-agents kit (`standards/` in
[claude-code-secure-agents](https://github.com/thakurdishanttt/claude-code-secure-agents)).

Platform anchors (do not bypass):

| Control | Location |
|---------|----------|
| Approval gate / autonomy | `orchestration/runner.py`, workspace `full` / `hybrid` / `human` |
| AgentAction + audit | `agents/base/contracts.py`, append-only `audit_log` |
| Untrusted LLM input fencing | `llm/untrusted.py` |
| Egress / SSRF guard | `core/egress.py`, `core/netguard.py` |
| Per-workspace AES-256-GCM | `core/crypto.py`, `services/encryption.py` |
| RLS | `db/rls.py`, `TENANT_SCOPED_TABLES` |
| RBAC | `core/rbac.py` |

---

## 1. Pre-merge gate (every PR)

- [ ] External side effects go through `AgentAction` and the runner; no direct
      connector writes from API handlers “for convenience”.
- [ ] Workspace autonomy, per-agent human override, and daily caps still apply.
- [ ] New tables with `tenant_id` are in `TENANT_SCOPED_TABLES` (or explicitly
      justified in `RLS_EXEMPT_TABLES`) with FORCE RLS; migration reversible.
- [ ] Secrets use workspace DEKs; nothing secret in logs, prompts, or client
      responses.
- [ ] Outbound HTTP uses the egress client; destinations allowlisted; no
      redirect-following that skips re-validation.
- [ ] Endpoints: RBAC permission, Pydantic validation, rate limiting; dangerous
      ops need an explicit permission.
- [ ] LLM / scraped / competitor / ad / third-party text wrapped with
      `untrusted.wrap` / `wrap_fields` and `SYSTEM_RULE` in the system prompt.
- [ ] Structured model output validated against a schema before use.
- [ ] Unit + integration + **tenant-isolation** tests added; existing suite
      still passes.
- [ ] Jobs idempotent; retries use backoff; structured logs without PII.

---

## 2. OWASP LLM Top 10 → Synaprank controls

| OWASP LLM risk | What we do |
|----------------|------------|
| LLM01 Prompt injection | Fence untrusted data; never follow instructions inside fetched content; separate system vs data |
| LLM02 Sensitive info disclosure | Encrypt at rest; redact PII before LLM where feasible; no secrets in prompts; response hygiene tests |
| LLM03 Supply chain | Pin deps; CI `pip-audit` / `npm audit`; model/connectors are explicit allowlists |
| LLM04 Data / model poisoning | Treat vendor and crawl data as untrusted; validate outputs; no training on customer data in-product |
| LLM05 Improper output handling | Schema validation; HTML/markdown sanitised on console; no raw HTML from models |
| LLM06 Excessive agency | Approval gate; impact levels; daily caps; capability-scoped connectors |
| LLM07 System prompt leakage | Do not echo system prompts; tests for internal disclosure |
| LLM08 Vector / embedding weakness | Tenant-scoped indexes only; no cross-tenant retrieval (when RAG ships) |
| LLM09 Misinformation | Ground Ask Willy in tools; label estimates; no fabricated metrics |
| LLM10 unbounded consumption | Token/field caps (`untrusted` hard max); per-agent cost caps; rate limits |

---

## 3. OWASP Agentic AI Top 10 (mapping)

| Theme | Synaprank expectation |
|-------|------------------------|
| Agent goal / instruction hijack | Untrusted fencing; runner is the only apply path |
| Tool misuse / over-privilege | Capabilities on connectors; `writes_via` verification; RBAC |
| Privilege compromise | RLS + per-org keys; no shared DEKs |
| Memory / context poisoning | Chat memory tenant-bound; wrap retrieved memory |
| Insecure inter-agent messaging | Agents do not call each other with free-form prompts; shared DB is tenant-pinned |
| Cascading hallucination | Structured outputs; human approval on HIGH impact |
| Human-in-the-loop bypass | Autonomy settings cannot be overridden by model output |
| Cost / resource abuse | Daily action caps; query caps on citation checks; metering (Phase 13) |
| Supply-chain / rogue skills | Auto-discovery only from in-repo packages; no remote agent install |
| Audit gaps | Append-only audit log; every apply audited |

---

## 4. ASVS-oriented API & session checks

- [ ] Authentication remains possession-based (email code / future SSO) with
      lockout, single-use codes, refresh-token reuse detection.
- [ ] Authorisation checked server-side per route; UI hiding is not security.
- [ ] Input validated (length, charset, URL allowlists); file uploads
      constrained (see Creative Studio SVG path).
- [ ] Security headers / CSP preserved (nginx + frontend guards tests).
- [ ] Error messages do not leak internals (`test_error_hygiene`,
      `test_no_internal_disclosure`).

---

## 5. MITRE ATLAS (selected techniques)

When designing a feature, note residual risk for:

| ATLAS-style technique | Mitigation in this codebase |
|----------------------|-----------------------------|
| Prompt injection / jailbreak | `llm/untrusted.py`, system rule, no instruction-following from data |
| LLM plugin / tool compromise | Capability interfaces; egress SSRF guard |
| Exfiltration via prompts | Data minimisation; redaction; field caps |
| Adversarial web content | Scrape/SERP treated as untrusted; egress allowlist |
| Credential theft from agents | Encrypted connector secrets; never log credentials |
| Cost flooding | Caps, rate limits, idempotent jobs |

Document any new ATLAS-relevant surface in the PR description.

---

## 6. NIST AI RMF (lightweight)

| Function | PR expectation |
|----------|----------------|
| **Govern** | Feature listed on roadmap; owner approves merge to `main` |
| **Map** | Data classes touched (customer content, credentials, model I/O) named |
| **Measure** | Tests + optional eval fixtures for grounded answers |
| **Manage** | Rollback via reversible migration; feature flags where risky |

---

## 7. Feature-specific prompts

### Agents that write to CMS, ads, or social

- [ ] `Impact` correctly HIGH vs LOW for hybrid mode  
- [ ] `apply` is pure given payload (no second model call on approve)  
- [ ] `writes_via` declared for vendor writes  

### Citation / visibility / competitive / ads library

- [ ] All third-party text fenced  
- [ ] Query volume capped  
- [ ] Competitor lists are customer-configured  

### Ask Willy / RAG

- [ ] Tools are allowlisted server functions, not free SQL  
- [ ] Answers cite internal sources  
- [ ] Cross-tenant retrieval tests fail closed  

### Enterprise SSO / SCIM / public API

- [ ] Tokens encrypted; webhook HMAC verified  
- [ ] No RLS bypass for “parent” agency — use memberships  

---

## 8. Stop-ship conflicts

If a feature needs any of the following, **stop** and explain in the PR
instead of shipping a workaround:

1. Writing to a vendor without `AgentAction` / runner  
2. Disabling RLS, FORCE RLS, or audit append-only triggers  
3. Following redirects or fetching URLs without egress validation  
4. Putting connector secrets or DEKs into prompts or client bundles  
5. Trusting model output as code, SQL, or shell  
6. Sharing one encryption key or tenant context across organisations  

---

## 9. PR template blurb (copy into description)

```text
AI security checklist: reviewed against docs/security/AI-SECURITY-CHECKLIST.md
- [ ] Approval gate respected
- [ ] Untrusted data fenced
- [ ] RLS + tenant-isolation tests
- [ ] Egress allowlist
- [ ] RBAC + validation + rate limit
- [ ] No weakened existing control
```
