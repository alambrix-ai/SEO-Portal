# AutoMarket AI — console

React 18 + TypeScript + Vite. Eleven screens over the platform API, drawn in
the **Industry** design system: steel-blue on a light technical ground, Barlow
Condensed over Barlow, and every card framed as a wireframe object with `+`
registration marks at its corners.

## Layout

```
src/
├── main.tsx              providers and mount
├── App.tsx               routing and route guards
├── api/
│   ├── client.ts         one HTTP client: token refresh, error shape
│   └── types.ts          the API contract, typed
├── auth/AuthContext.tsx  session state and the access map
├── components/
│   ├── ui.tsx            Blueprint frame, tiles, tags, dialog, fields
│   ├── CodeEntry.tsx     the one-time-code step, shared by sign-in and sign-up
│   ├── NotificationBell.tsx  the header bell, its panel and its unseen dot
│   ├── icons.tsx         thin-stroke line icons
│   └── Toasts.tsx        notifications, driven by the API's own wording
├── hooks/useResource.ts  loading, cancellation, reload
├── layout/AppShell.tsx   sidebar, header, page outlet
├── pages/                one file per screen
└── styles/
    ├── industry.css      the design system (tokens + components)
    └── app.css           console layout, built only from those tokens
```

## Running it

```bash
npm install
npm run dev
```

The dev server proxies `/api` and `/health` to `http://127.0.0.1:8000`, so the
browser stays on one origin and CORS behaves as it will in production. Override
with `VITE_PROXY_TARGET`, or point at a deployed API with `VITE_API_BASE_URL`.

For the free Render static deploy, set `VITE_API_BASE_URL` to your API's
`/api/v1` URL at build time (see [`../RENDER_DEPLOY.md`](../RENDER_DEPLOY.md)).

```bash
npm run typecheck   # tsc --noEmit
npm run build       # typecheck, then bundle to dist/
```

## Five things worth knowing

**Lists are ordered by how much attention something wants, and the server
decides.** Errored agents first, then running, then configured-but-stopped,
then untouched; connected connectors before the rest, with an unhealthy
connection above a working one. Catalogue order is right for a catalogue and
wrong for a fleet somebody is running. The dashboard follows from the same
idea: it lists what is *live* rather than the head of the list, and says
plainly when that is nothing.

**Every card has one skeleton: identity, state, actions.** The slack goes
between state and actions and nowhere else, so eleven agents with wildly
different amounts to report still line their controls up. This is worth
knowing because the obvious thing does not work: the design system gives
`.card-body` `flex: 1`, so the *description* absorbs the spare height — a card
with little to say pushes its description open and sits its content at the
bottom, while a busy one sits at the top. Same grid, two rhythms. `.agent-state`
takes the slack instead.

**A card shows only what is true of it yet.** An agent nobody has set up has
no status worth reporting — its state is "paused", its next run is a dash and
its metric line is a placeholder — so the card collapses to what it is and a
Configure button. The status tags, the metric line, the autonomy switch and
Run now appear once the agent is live or has been configured. Connectors work
the same way against `connected`: a "Not connected" tag beside a button that
says Connect is the same fact stated twice, and twenty-five of them read as a
wall of status rather than a catalogue. The gate is a payload field in both
cases, and the API is tested to arrive genuinely empty rather than with
placeholder state the card would faithfully render.

**Signing in is two steps, and the screen does not know why.** There is no
password field anywhere in the console. `LoginPage` asks for an address, calls
`/auth/request-code`, and then shows `CodeEntry`. Note that it shows the code
step *even for an address with no account* — the API's answer is deliberately
identical either way, and a console that skipped ahead only for real customers
would leak exactly what the API refuses to. The code length, its lifetime and
the resend gap all come from `/auth/policy`, so the countdowns match what the
server will actually do rather than a second copy of the rules.

**Permissions come from the server.** The session payload carries the access
map from the same RBAC table the endpoints enforce, so the navigation a user
sees and the requests they are allowed to make are the same decision. A module
a role cannot view is rendered as a locked, non-clickable item — visible, so
the product's shape is legible, but not a dead link.

**Labels come from the server too.** Status words, relative timestamps,
countdowns and toast messages are produced by the API, next to the rule that
decided them. Two screens showing the same object cannot describe it
differently, and changing a rule does not mean changing its wording in two
places.

**Token refresh is shared.** Access tokens are short-lived. On a 401 the client
redeems the refresh token once and replays the request; concurrent 401s all
wait on that same refresh. Without that, a screen loading several endpoints at
once would spend several refresh tokens and trip the backend's reuse detection,
signing the user out.

## Design system

`styles/industry.css` is taken unmodified from the project's design system.
Everything in `app.css` is expressed in its tokens — `var(--color-accent)`,
`var(--space-3)`, `var(--font-heading)` — so retuning that one file retunes the
whole console. The `Blueprint` component in `components/ui.tsx` is the frame
every card wears; nothing hand-writes the four corner marks.
