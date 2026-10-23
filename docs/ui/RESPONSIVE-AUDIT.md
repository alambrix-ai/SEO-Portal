# Responsive layout audit — Synaprank console

Plan for making every route usable from 320px phones through ultrawide desktops, without a new UI framework. Design system remains `industry.css` + `app.css` + `enterprise.css` + `ui.tsx` (see `STYLE-AUDIT.md`).

**Branch:** `cursor/responsive-layout-372b` (off latest `development`). Backend files are out of scope.

## Breakpoints (target)

| Band | Width | Shell behaviour |
| --- | --- | --- |
| Narrow (phone / tablet) | &lt; 1024px (also short landscape &lt; 1200×540) | Off-canvas sidebar drawer + hamburger; 1-col grids |
| Medium (small laptop) | 1024–1279px | Collapsible rail; 2-col grids |
| Wide (laptop) | 1280–1919px | Full rail; multi-col |
| Ultrawide | ≥ 1920px | Full rail; content column capped (~1440px) |

`useFitDensity` and CSS media queries must stay aligned (no leftover **820px** horizontal-nav strip in `app.css`).

### P0 — Dishant iPhone Overview (prod, ~390px Chrome)

| Symptom | Root cause | Fix |
| --- | --- | --- |
| Content only ~left 55%; right side empty cream background | `.dash-board` uses `minmax(340px, 1fr)` so the grid is ~700px wide; `.page { overflow-x: hidden }` clips the rest — looks like a half-width page | Mobile-first 1-col grids; never use 340px track mins below desktop |
| “Website visits” card clipped vertically; Ask Willy still visible | Shell/`overflow: hidden` + short scrollport; page bottom padding fights the dock | Full-width `.app-main`, `100dvh` shell, scrollable `.page`, safe-area bottom padding |
| Suspected desktop sidebar width reserved | Conflicting `app.css` narrow/`820px` rules (`position: static`, horizontal strip) vs enterprise drawer | Neutralize legacy rules; fixed drawer **must not** consume flex width; `.app-main { width: 100% }` |

Viewport meta is already in `index.html` (`viewport-fit=cover`); keep it and verify it ships in the built `dist/index.html`.

## App shell (all authenticated routes)

| Issue | Fix |
| --- | --- |
| `app.css` still styles narrow as a horizontal nav strip; `enterprise.css` overrides with a drawer — conflicting 820 vs 900 rules | Neutral legacy strip rules; single off-canvas drawer path through tablet |
| Assistant dock media at 820px while shell is narrow | Align dock + page bottom padding to shell breakpoint + safe-area |
| Touch targets on topbar icons / nav toggle below 44px on mobile | Raise icon buttons to ≥44px under narrow |
| Ultrawide: page padding grows but content never caps | Cap page content max-width (~1440px) |
| Body `overflow: hidden` can hide horizontal bleed instead of fixing it | Keep shell overflow hidden; children `min-width: 0` + table wrappers; no page-level clip of wide grids |
| Safe-area gaps on dock / toasts / drawers | `env(safe-area-inset-*)` on floaters |

## Route issues

| Route | Issues | Planned fix |
| --- | --- | --- |
| `/login`, `/register`, `/login/mfa`, `/accept-invite` | Auth stack mostly OK via `data-fit-w`; ensure safe-area + full-width inputs | Polish auth padding / min touch targets |
| `/status` | Centered max-width; OK | Minor fluid padding |
| `/dashboard` (**P0**) | Half-width clip from `dash-board` 340px track; KPI/stat grids; vertical clip | Mobile-first stacks; full-bleed main; verify at 375 & 390 |
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
