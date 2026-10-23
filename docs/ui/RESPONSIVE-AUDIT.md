# Responsive layout audit — Synaprank console

Plan for making every route usable from 320px phones through ultrawide desktops, without a new UI framework. Design system remains `industry.css` + `app.css` + `enterprise.css` + `ui.tsx` (see `STYLE-AUDIT.md`).

**Branch:** `cursor/responsive-layout-372b` (off latest `development`). Backend files are out of scope.

## Breakpoints (target)

| Band | Width | Shell behaviour |
| --- | --- | --- |
| Narrow (phone / tablet portrait) | &lt; 900px (also short landscape &lt; 1200×540) | Off-canvas sidebar drawer + hamburger; 1-col grids |
| Medium (tablet landscape / small laptop) | 900–1099px | Collapsible rail; 2-col grids |
| Wide (laptop) | 1100–1919px | Full rail; multi-col |
| Ultrawide | ≥ 1920px | Full rail; content column capped (~1440px) |

Existing `useFitDensity` already maps `narrow` / `medium` / `wide`; media queries must stay aligned to **900px** (not the older 820px strip in `app.css`).

## App shell (all authenticated routes)

| Issue | Fix |
| --- | --- |
| `app.css` still styles narrow as a horizontal nav strip; `enterprise.css` overrides with a drawer — conflicting 820 vs 900 rules | Neutral legacy strip rules; single off-canvas drawer path |
| Assistant dock media at 820px while shell is narrow at 900px | Align dock + page bottom padding to 900px + safe-area |
| Touch targets on topbar icons / nav toggle below 44px on mobile | Raise icon buttons to ≥44px under narrow |
| Ultrawide: page padding grows but content never caps | Cap `.page > *` / page content max-width |
| Body `overflow: hidden` can hide horizontal bleed instead of fixing it | Keep shell overflow hidden; ensure page children use `min-width: 0` + table wrappers |
| Safe-area gaps on dock / toasts / drawers | `env(safe-area-inset-*)` on floaters |

Viewport meta already present: `width=device-width, initial-scale=1, viewport-fit=cover`.

## Route issues

| Route | Issues | Planned fix |
| --- | --- | --- |
| `/login`, `/register`, `/login/mfa`, `/accept-invite` | Auth stack mostly OK via `data-fit-w`; ensure safe-area + full-width inputs | Polish auth padding / min touch targets |
| `/status` | Centered max-width; OK | Minor fluid padding |
| `/dashboard` | KPI grid stays 2-col on phones; charts generally scale | 1-col KPIs on ≤480px; chart containers `min-width: 0` |
| `/onboarding` | Wizard steps may crowd actions | Stack actions; full-width CTAs on narrow |
| `/agents`, `/agents/:slug` | Data-table card-stack path exists; KPI 2-col | Touch targets; ensure table-wrap |
| `/content` | `.campaign-cols` is 2-col with **no** breakpoint; tall preview frames | Stack columns ≤899; reduce preview height |
| `/brand`, `/brand-kit`, `/creative` | Mostly auto-fill; creative advanced tables | Table scroll wrappers; stack splits |
| `/analysis` | Uses `.table-scroll`; OK | Keep; verify forms stack |
| `/seo`, `/offpage`, `/ads`, `/admin` | `.table-scroll` + plain `.table`; `.row-between` no wrap | Wrap headers; ensure scroll wrappers |
| `/visibility`, `/alerts` | `split-1-1` / `grid-tiles` stack ≤1080 | Fluid type/spacing only |
| `/topics` | **Bare table, no overflow wrapper** — primary overflow risk | `.table-scroll` + mobile card/stack rules |
| `/technical` | Card list; OK | Minor |
| `/connectors` | Data-table + catalog grid | Touch targets |
| `/approvals` | `.approval-row` stays row on phones | Stack title / actions |
| `/reports` | Mixed tables | Scroll wrappers |
| `/admin/enterprise`, `/billing`, `/developer`, `/agency`, `/warehouse` | `split-1-1` / tiles | Inherit split reflow; form full-width |
| `/portal` | Portal CSS reflows ≤820 | Align to 900 |
| `/account`, `/account/mfa` | Narrow max-width; OK | Touch / keyboard safe |
| Ask Willy (`AssistantDock`) | Breakpoint mismatch; safe-area | Align + insets |
| Shared: menus, dialogs, drawers, confirms | Dialogs OK width-wise; not always full-screen on phone | Full-bleed dialogs ≤480px; 44px actions |

## Shared component work

1. **Tables:** default `.table` inside `.table-scroll` (or `.table-wrap`); enhance mobile card view for `.data-table[data-label]` and add a generic stacked fallback for plain tables where labels exist.
2. **Grids:** ensure `.split-*`, `.grid-tiles`, `.grid-cards`, `.campaign-cols`, `.kpi-grid` reflow 1 → 2 → N.
3. **Charts:** parent `min-width: 0`; SVG already fluid via viewBox.
4. **Forms / modals:** full-screen / edge-to-edge dialogs on narrow; `100dvh` + keyboard-friendly `max-height`.
5. **A11y:** preserve focus rings; `@media (prefers-reduced-motion: reduce)` on shell transitions; WCAG 1.4.10 reflow (320px, no 2D scroll except tables/maps).

## Verification

- `npm run typecheck` / `lint` / `build` in `frontend/`
- Playwright screenshots at 375 / 768 / 1280 / 1920 (before + after) under `/opt/cursor/artifacts/responsive/`
- Automated check: no document / main horizontal overflow on key routes at 375px

## Implementation order

1. Commit this audit.
2. Shell CSS reconciliation + safe-area + content cap + touch targets.
3. Shared table / grid / dialog / dock CSS.
4. Page-level wrappers (Topics first) and missing reflows (Content, Approvals).
5. Playwright smoke + overflow assertions; screenshots.
6. Rebase onto `development`, open PR, merge to `development` only (never `main`).
