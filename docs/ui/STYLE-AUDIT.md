# UI style audit — Synaprank console

Audit of `frontend/` routes against the Industry design system (`industry.css` + `app.css` + shared `ui.tsx` primitives). Goal: every page, tab, modal, form, table, empty, loading and error state matches well-styled screens (SEO, Technical SEO, Integrations).

## Design system (reference)

| Layer | Path | Use |
| --- | --- | --- |
| Tokens | `styles/industry.css` | colours, Fraunces/Figtree, `.btn*`, `.input`, `.card`, `.blueprint` |
| Layout | `styles/app.css` | `.grid-tiles`, `.split-1-1`, `.stack` / `.stack-sm`, tables, auth shell |
| Enterprise | `styles/enterprise.css` | `.page-header`, `.list-row`, denser panels |
| Primitives | `components/ui.tsx` | `PageHeader`, `SectionHeading`, `StatTile`, `Field`, `EmptyState`, `Loading`, `ErrorState`, `Blueprint`, `Tag` |

**Contract for feature UIs:** `PageHeader` → optional tabs → `Loading` / `ErrorState` / `EmptyState` → `grid-tiles` / `split-*` / `.table` → buttons as `btn btn-primary|secondary|ghost` → form controls as `input` (including `textarea.input`).

No dark theme tokens today (light Industry theme only). Responsive shell already uses `data-fit-w` / `data-fit-h`.

## Broken → fixed

### P0 — AI Visibility (`/visibility`) — Dishant report

| Issue | Fix |
| --- | --- |
| Stat cards stacked full-width (`stat-row` undefined) | `grid-tiles` |
| Two columns missing (`grid-2` undefined) | `split-1-1` |
| Native unstyled buttons (`btn primary` / `btn ghost`) | `btn btn-primary` / `btn btn-ghost btn-sm` |
| Native unstyled textareas | `className="input"` + `Field` with `htmlFor` |
| Headings flush / weak hierarchy | `SectionHeading` + descriptions; `spaced` between lower sections |
| Lists without row chrome | `.list-row` / `.list-row-main` / `.list-row-side` |
| Breadcrumb missing | `TITLES['/visibility']` in `AppShell` |

Screenshots: `ai_visibility_before.png`, `ai_visibility_after.png`.

### P0 — MFA

| Page | Broken | Fixed |
| --- | --- | --- |
| `/login/mfa` | Bare `auth-card`, `className="error"`, no stage/Willy shell | Mirrors `LoginPage`: `auth-login-shell`, stage, `auth-error`, `input-login`, `btn-block` |
| `/account/mfa` | Inline max-width, `stack gap-md`, bare `<code>` / `<ul>`, `className="error"` | `Blueprint` cards, `SectionHeading`, `input` / recovery list rows, `EmptyState` when done |

### P1 — New feature surfaces (API existed; UI missing or thin)

| Route | What was wrong | What we shipped |
| --- | --- | --- |
| `/topics` | No UI | Topic clusters + briefs with `StatTile`, table, `EmptyState` |
| `/alerts` | No UI | Rules / events / create form with design-system forms |
| `/admin/enterprise` | No UI | SSO list, custom roles, IP allowlist, SCIM mint, scoped approvals |
| `/admin/billing` | Seat meter only on Team page | Catalog, account, usage, entitlements |
| `/admin/developer` | No UI | API tokens + webhooks |
| `/admin/agency` | No UI | Create agency + white-label form |
| `/admin/warehouse` | No UI | Export jobs + enqueue form |
| `/status` | No public route | Public status page (no tenant data) |
| Ask Willy grounding | `sources` ignored | Source chips under assistant replies |
| Agent icons | 15+ new slugs → generic | Mapped to area icons |

### P2 — Consistency sweeps

| Area | Change |
| --- | --- |
| Account | MFA card + sign-out use `Blueprint` / `auth-error` |
| Approvals | View switch uses `section-heading` instead of inline margin |
| App shell | Nav + breadcrumbs for all new routes |
| CSS helpers | `ul.stack` / `ul.stack-sm` list reset; split grids get bottom margin |

## Still agent-shell only (intentional)

These backends run as agents; they keep using `/agents/:slug` + Approvals rather than a dedicated workspace page. Icons + labels were added so the Agents grid does not look unfinished:

content refresh, auto-applied fixes, internal linking, programmatic SEO, product feed optimizer, content repurposer, cross-channel budget, landing-page A/B, competitor ad watch, multi-location local SEO, social listening, playbooks.

Weekly autopilot report remains scheduler/email-only (no HTTP route to wire).

## Checks

- `npm run typecheck`
- `npm run lint`
- `npm run build`

## Security constraints (unchanged)

- No `dangerouslySetInnerHTML` for untrusted content
- CSP unchanged (no inline-script / `unsafe-*` relaxations)
- No new third-party CDNs
