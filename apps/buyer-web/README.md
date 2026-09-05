# buyer-web

Buyer storefront for the governed agentic-commerce platform (Razorpay AI Buildathon, Track 1).
Next.js App Router, React 19, TypeScript, Tailwind CSS 4, zod. No UI kit.

It renders the demonstration of `PROJECT_SPECIFICATION.md` §2.5 end to end: grounded discovery
→ deterministic quote → versioned checkout → trusted approval → merchant change underneath the
approval → exact delta and version N+1 → fresh approval → Razorpay Standard Checkout admitted
exactly once → server-side capture verification → proof chain.

There is one lane. This app has no mock mode, no fixture and no offline fallback, and that is
a design decision rather than an omission: a storefront that quietly substitutes invented data
when the API is unreachable shows a buyer a price no kernel ever agreed to. A read that fails
renders the failure.

## Run

The Commerce API must be running; everything on screen comes from it.

```sh
cd apps/buyer-web
npm install
COMMERCE_API_URL=http://127.0.0.1:8000 npm run dev
```

Then open <http://localhost:3000>. `COMMERCE_API_URL` is read by the server-side route handler
only, defaults to `http://localhost:8000`, and never reaches the browser.

| Command | What it does |
| --- | --- |
| `npm run dev` | Development server on :3000 |
| `npm run build` | Production build (must pass) |
| `npm run start` | Serve the production build |
| `npm run lint` | ESLint (next/core-web-vitals + typescript) |
| `npm run typecheck` | `tsc --noEmit` |
| `npm test` | Vitest unit tests |
| `npm run e2e` | Playwright, against a live API and a live dev server |

The Playwright suite drives the real refusal: it approves a checkout, injects a `PRICE_SET`
through `/v1/scenario/injections`, presses Pay, and asserts the old total, the new total and
the difference against integers it read back from the server itself. It restores the seeded
price afterwards and cancels every checkout it opened, so a run leaves no stock hold behind.

## Where things live

```
src/lib/api/types.ts          zod schemas for every response the app parses
src/lib/api/problem.ts        RFC 9457 problem details -> ApiError
src/lib/api/client.ts         the live fetch client (Idempotency-Key per mutation)
src/lib/money.ts              paise formatting; the one sanctioned subtraction lives here
src/lib/product-images.ts     local artwork lookup for a SKU
src/lib/security/csp.ts       nonce CSP builder; Razorpay origins only on /checkout/*
src/middleware.ts             per-request nonce + CSP (routing only, never authorization)
src/app/api/backend/[...path] same-origin pass-through; mints and holds the session cookie
src/features/checkout/approval-card.tsx   the trusted approval card
src/features/checkout/refusal-card.tsx    the refusal, its deltas and its version trail
src/features/checkout/delta-table.tsx     field-level deltas, money and non-money
src/features/checkout/trusted-surface.tsx the boundary approve/pay/cancel must sit inside
src/features/checkout/payment-panel.tsx   Standard Checkout launch and verification wait
src/features/agent/razorai-panel.tsx      the copilot; holds no money authority
src/features/agent/denial-card.tsx        a capability denial, rendered as the system working
src/features/orders/capture-evidence.tsx  provider evidence as the server recorded it
```

## Invariants the UI keeps

- The browser never computes a total, a tax or a delivery fee. It formats the server's integers.
  The only arithmetic in the app is subtracting two integers the server sent, for a delta row.
- The approval card submits exactly `{content_hash, amount_minor, currency}` as displayed.
- A payment is never marked paid from the Razorpay browser callback. The panel posts the callback
  triple for verification and keeps asking the server until the server says what happened.
- A kernel denial arrives as `200` with a structured decision and is rendered as the platform
  working. Only transport, contract and request failures become `ApiError`.
- Nothing claims more than the response supports. An absent field is rendered as absent, and a
  refusal card that cannot tell whether money moved says exactly that.
- No secret or API key exists in this app. The bearer token lives in a signed `httpOnly` cookie
  that JavaScript never reads and that the browser never receives the contents of.

## Security headers

`next.config.ts` sets HSTS, `Referrer-Policy`, `X-Content-Type-Options`, `X-Frame-Options: DENY`,
`Cross-Origin-Opener-Policy` and `Permissions-Policy`, and `Cache-Control: no-store` on
`/checkout/*`, `/orders/*` and `/basket`. `src/middleware.ts` sets a nonce-based
`Content-Security-Policy` per request, deliberately without `'strict-dynamic'`, allowing
`checkout.razorpay.com` (script) and `api.razorpay.com` (frame/connect) only on `/checkout/*`.
`style-src` allows `'unsafe-inline'` for React's style attributes; scripts do not.

## Accessibility

Keyboard-navigable throughout; state changes are announced through `aria-live` regions; every
money and policy status carries a text label as well as a colour, so colour is never the only
signal; the current journey state carries `aria-current="step"`.
