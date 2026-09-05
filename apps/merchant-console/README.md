# Merchant Console

The operations instrument for the agentic commerce platform. A separate Next.js app from
the buyer storefront, deliberately unlike it: a dark, dense console rather than a shop, so
the two read as two products when they sit side by side on a demo screen.

```
npm install
npm run dev      # http://localhost:3001
```

It needs the Commerce API running (`make demo` at the repository root) and the same
scenario key that API was started with.

| Variable                 | Default                     | What it is                                          |
| ------------------------ | --------------------------- | --------------------------------------------------- |
| `COMMERCE_API_URL`       | `http://localhost:8000`     | The API this console proxies to.                     |
| `NEXT_PUBLIC_TENANT_SLUG`| `demo`                      | The tenant the operator session is minted against.   |
| `SCENARIO_KEY`           | `local-demo-scenario-key`   | The operator credential. Never reaches the browser.  |

## The one rule

**Every figure on every page is read from the API during that page load, or it is not
shown.** There are no fixtures in this application, no mock fallbacks, and no cached
last-good values. A panel whose read fails renders the RFC 9457 problem document — status,
title, detail and every extension member — in place of the numbers it could not get. A
panel whose endpoint does not exist yet says so and names what is missing.

That rule is enforced structurally rather than by discipline:

- `src/lib/api/client.ts` throws on every failure and has nothing to substitute.
- `src/lib/useRead.ts` ties `data` and `error` to the exact inputs that produced them, so a
  failed read leaves no figures behind and a filter change does not leave the previous
  filter's rows under a new heading.
- The Safe Mode switch never paints an engaged state. What it shows is always the mode the
  API last returned; a write that fails renders the failure and leaves the read state
  exactly as it was.

## Credentials

`src/app/api/backend/[...path]/route.ts` is the only place this app holds one, and it holds
two. It mints an **OPERATOR** session — which the API refuses to anyone not already holding
the scenario key — keeps the bearer token in an `httpOnly` cookie, and attaches both the
token and `X-Scenario-Key` to every forwarded request. The key widens each read from the
caller's own rows to the whole tenant, which is what separates a console from a buyer's
order history, and it must never reach the browser.

## Pages

| Route         | What it reads                                                                              |
| ------------- | ------------------------------------------------------------------------------------------ |
| `/`           | Safe mode, outbox counts, order and refund counts by state, the retained-revenue headline.  |
| `/evidence`   | Retained revenue as arithmetic, chain verification, the proof-chain verdict, the timeline.  |
| `/operations` | Orders, refunds, the durable outbox with revive, and the kill switch.                       |
| `/catalogue`  | 247 products, searched and filtered server-side; changes go through scenario injections.    |
| `/inspector`  | One payment attempt, whole: grants, commands, provider calls, webhooks, reconciliation.     |

Money is an integer count of paise from the server throughout. Nothing in this app adds,
multiplies or rounds a price, and the catalogue editor takes paise as an integer for that
reason: a rupee field would mean a conversion here, and a price this console computed could
disagree with the one the kernel revalidates at admission.
