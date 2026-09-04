# buyer-web

Buyer storefront for the governed agentic-commerce platform (Razorpay AI Buildathon, Track 1).
Next.js 16.3.4 App Router, React 19, TypeScript, Tailwind CSS 4, zod. No UI kit.

It renders the eleven-step demonstration of `PROJECT_SPECIFICATION.md` §2.5 end to end:
grounded discovery → deterministic quote → versioned checkout → trusted approval → merchant
change underneath the approval → exact delta and version N+1 → fresh approval → Razorpay
Standard Checkout admitted exactly once → server-side capture verification → proof chain.

## Run

```sh
cd apps/buyer-web
npm install

# Mock mode: no backend, the whole journey runs in the browser against the fixture.
NEXT_PUBLIC_API_MODE=mock npm run dev

# Live mode (default): talks to commerce-api through the same-origin route handler.
NEXT_PUBLIC_API_BASE=http://localhost:8000 npm run dev
```

Then open <http://localhost:3000>.

| Command | What it does |
| --- | --- |
| `npm run dev` | Development server |
| `npm run build` | Production build (must pass) |
| `npm run start` | Serve the production build |
| `npm run lint` | ESLint (next/core-web-vitals + typescript) |
| `npm run typecheck` | `tsc --noEmit` |
| `npm test` | Vitest unit tests (problem parsing, approval-card echo, transcript contract, mock journey, SHA-256) |

## Modes

| | `NEXT_PUBLIC_API_MODE=mock` | live (default) |
| --- | --- | --- |
| Data | `src/lib/api/mock.ts`, deterministic, persisted to `sessionStorage` for the tab | FastAPI at `NEXT_PUBLIC_API_BASE` (default `http://localhost:8000`) |
| Session | none needed | `POST /api/session` mints `POST /v1/demo/sessions` server-side and stores the bearer token in an **HttpOnly** cookie; JavaScript never sees it |
| API calls | answered locally | browser → `/api/backend/<path>` (same origin) → route handler attaches `Authorization: Bearer` → FastAPI |
| SSE timeline | in-memory emitter with `Last-Event-ID` resume | `EventSource` on `/api/backend/v1/checkouts/{id}/events`; first connect passes `?last_event_id=` which the handler turns into the `Last-Event-ID` header, reconnects send the real header |
| Razorpay | simulated dialog, `checkout.js` is **not** loaded | `https://checkout.razorpay.com/v1/checkout.js` loaded only on `/checkout/[id]`, opened with `{key, order_id, amount, currency, name, handler}` where `key` is the public key id returned by `GET /v1/checkouts/{id}/payment` |
| Scenario controller | in-page buttons (mock only): payment-unknown fault, invalidate open checkout + late capture, webhook replay | driven from outside the buyer surface with `X-Scenario-Key`; the route handler refuses `/v1/scenario/*` |

The mock's scripted merchant change on the first submit raises the first line's unit price by
₹10 and the delivery fee by ₹20 (when delivery is not free), so the first approval is denied
`REAPPROVAL_REQUIRED` with an exact field-level delta and version 2 is created.

## Where things live

```
src/lib/api/types.ts        zod schemas for every response  (PROVISIONAL, see below)
src/lib/api/problem.ts      RFC 9457 problem details -> ApiError
src/lib/api/client.ts       CommerceClient interface + live fetch client (Idempotency-Key per mutation)
src/lib/api/mock.ts         deterministic mock of the same interface
src/lib/journey.ts          the sixteen spec 8.2 UI states and how they derive from kernel states
src/lib/voice/transcript.ts transcript wire contract (spec 19.5): applyStreamText = incoming || current
src/lib/security/csp.ts     nonce CSP builder; Razorpay origins only on /checkout/*
src/proxy.ts                per-request nonce + CSP (Next 16 proxy; routing only, never authorization)
src/app/api/session         HttpOnly session cookie route handler
src/app/api/backend/[...path]  same-origin pass-through that attaches the bearer token
src/components/approval-card.tsx  trusted approval card + delta view
src/components/razorpay-launcher.tsx  Standard Checkout launch; handler POSTs /v1/payments/verify
src/components/evidence-drawer.tsx    timeline, proof-chain verdict, inspector links
src/components/degradation-banner.tsx visible degradation (spec 19.12)
src/components/voice-panel.tsx        placeholder that renders only the transcript contract
src/features/checkout/checkout-journey.tsx  the state machine page
```

## Invariants the UI keeps

- The browser never computes a total, tax or free-delivery gap; it formats the fee engine's integers.
- The approval card submits exactly `{content_hash, amount_minor, currency}` as displayed (tested).
- A payment is never marked paid from the Razorpay browser callback. The handler posts the callback
  triple to `/v1/payments/verify`, shows "Payment pending verification", and waits for the SSE
  timeline to report `CAPTURED` and an order id.
- Unknown is never turned into failed by a UI timer; expiry is decided by the server clock.
- No secret, API key or `key_secret` exists in this app. `NEXT_PUBLIC_*` carries only the API mode and origin.
- Kernel denials arrive as `200` with a structured decision and are rendered verbatim; only transport,
  contract and request failures become `ApiError`.

## Security headers

`next.config.ts` sets HSTS, `Referrer-Policy`, `X-Content-Type-Options`, `X-Frame-Options: DENY`,
`Cross-Origin-Opener-Policy` and `Permissions-Policy`, and `Cache-Control: no-store` on
`/checkout/*`, `/orders/*` and `/basket`. `src/proxy.ts` sets a nonce-based
`Content-Security-Policy` per request (`frame-ancestors 'none'`, `'strict-dynamic'`), allowing
`checkout.razorpay.com` (script) and `api.razorpay.com` (frame/connect) only on `/checkout/*`.
`style-src` allows `'unsafe-inline'` for Tailwind/React style attributes; scripts do not.

## Provisional

Every response shape in `src/lib/api/types.ts` is marked
`PROVISIONAL: reconcile with OpenAPI after commerce-api lands`. Names follow the existing
packages where they exist (`CheckoutState`, `PaymentState`, `RecoveryCode`, `Delta`,
`KernelDecision`, `Quote.to_checkout_content`); the rest is a best guess against ADR 0003's
endpoint catalogue. Also provisional: the `POST /v1/demo/sessions` request body
(`{actor_type: "BUYER"}`) and response (`{token, session_id, actor_type, expires_at}`), the SSE
event name (`timeline`) and payload, the `?last_event_id=` query convention, and
`GET /v1/config` carrying `degraded[]`/`safe_mode`. When the OpenAPI document lands, replace
`types.ts` with the generated client's types and keep `client.ts` as the thin adapter.

## Accessibility

Keyboard-navigable throughout; state changes are announced through `aria-live` regions; every
money and policy status carries a text label and a glyph, so colour is never the only signal;
tables have captions; the current journey state carries `aria-current="step"`.
