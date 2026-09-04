# Brief 3 for Gemini — make both apps real

Brief 2 is merged. This is the next workload, and it is the heaviest so far.

---

## STEP ZERO — every session

```bash
cd /Users/vedanttyagi/Desktop/acr-worktrees/gemini-catalogue
pwd && git branch --show-current
git merge --ff-only main
```

`pwd` must be the worktree above and the branch must be `gemini/catalogue`. The merge
brings in Claude's work. If it fails, stop and say so.

## What changed since your last brief

**The backend is now real.** Claude's HTTP API and durable worker exist: roughly 11,600
lines of API and 3,200 of worker. The eleven-step demonstration has been walked live
against a running server with curl, including the moment that matters — a price change
injected underneath an approved checkout, the submit refused with HTTP 200 carrying
`allowed: false`, `REAPPROVAL_REQUIRED`, a delta of `57995 → 68195`, and `next_version: 2`.

So the mock is no longer the only truth. **Your apps must now work against the live API**,
and that is the theme of this brief.

Two notes on your last delivery. The Playwright suite asserts the deltas, the invalidation
and the capture, but never checks that the approval card submits exactly the content hash
it displayed — that is the single most important guarantee in the product and it needs a
test. And `docs/DEMO.md` was written by both of us because I assigned it to my own agent
and pointed you at it in the same breath; that was my mistake, the longer executed version
was kept, and your mock-mode quickstart was folded into it as section zero.

## Priority 1 — run both apps against the live backend

This is the whole job. Until now everything you built was proven against a mock that
always answers instantly and never fails.

1. **Get it running.** From the repository root: `make bootstrap && make seed && make demo`
   starts PostgreSQL migrations, seeds a demo tenant and merchant, and runs the API and
   worker together. Read `docs/DEMO.md` first; it has the exact commands and a
   troubleshooting section. Then start the storefront with `NEXT_PUBLIC_API_MODE=live` and
   `NEXT_PUBLIC_API_BASE=http://localhost:8000`.
2. **Walk all eleven steps in the browser against the live API** and fix every place the
   UI breaks, stalls, or shows something untrue. Expect to find real problems: a live
   backend is slower than a mock, returns RFC 9457 problem documents instead of thrown
   errors, and produces intermediate states the mock never emitted.
3. **The states the mock never showed you.** `EXECUTION_PENDING` while the worker is
   creating the Razorpay order, `AWAITING_PAYMENT`, `PAYMENT_UNKNOWN` during
   reconciliation, `RECONCILING`, and `STALE_CAPTURE`. Specification 8.2 lists sixteen
   required states; make each one reachable and visually distinct against the real server.
4. **Server-sent events for real.** The timeline endpoint streams; the storefront must
   consume it, resume from `Last-Event-ID` after a dropped connection, and flip the payment
   screen from pending to captured without polling. Test it by killing the API mid-stream
   and restarting it.
5. **Every error path.** Turn off PostgreSQL and load a page. Send a mutation without an
   idempotency key. Submit an approval with a stale hash. Each must produce something a
   human understands, built from the problem document the server returned, never a blank
   screen or a raw status code.
6. **Do not change the API contract to make the UI easier.** `lib/api/client.ts`,
   `types.ts` and `problem.ts` are Claude's. If a response shape genuinely does not match
   what the server sends, write it in `docs/briefs/REQUESTS_TO_CLAUDE.md` with the endpoint,
   what you expected and what arrived. That file is how the two halves stay coherent.

## Priority 2 — the merchant console on live data

The console currently renders figures labelled `SIMULATED` and `MOCK`, which was the honest
thing to do. Now make them real.

- Point the dashboard, the evidence ledger and the inspector at the live endpoints. Ask
  Claude in `REQUESTS_TO_CLAUDE.md` for any endpoint you need that does not exist yet
  rather than inventing a shape.
- The retained-revenue view is step eleven of the demonstration and the number the pitch
  turns on. When it is drawn from live data, remove the `SIMULATED` label. While any part
  of it is still illustrative, keep the label. A judge who finds an unlabelled invented
  figure on a payments submission will discount everything else on the page.
- The price-surge control should call the real scenario endpoint, so the console can
  trigger the refusal the storefront then displays. That is a strong thing to show live.

## Priority 3 — pin the guarantee that matters

Add to the Playwright suite, in mock mode where it is deterministic:

- The approval card posts **exactly** the `content_hash`, `amount_minor` and `currency` it
  rendered. Read the values out of the DOM, intercept the outgoing request, and assert they
  are identical. A stale closure here authorises a payment for the wrong amount, and no
  current test would catch it.
- No paid or confirmed state ever appears before the timeline reports capture.
- A second submit of an already-admitted checkout does not produce a second payment
  attempt.
- Run the suite at 390px as well as desktop, as you already do.

## Priority 4 — make the apps feel finished

- **Loading and empty states everywhere.** Skeletons rather than layout jumps, and a real
  empty state for a search with no results, an empty basket, and a merchant with no orders.
- **Optimistic updates with honest rollback** on basket changes, so the phone feels quick
  without ever showing a quantity the server did not accept.
- **Offline and slow-network behaviour.** Throttle to slow 3G in devtools and fix what
  becomes unusable. Quick commerce is used on phones with bad signal.
- **The agent panel in live mode.** It currently talks to a mock. Wire it to whatever
  endpoint Claude exposes; if none exists yet, keep it on the mock, make the boundary
  obvious in the UI, and note it in your report.

## Priority 5 — only when the above genuinely works

- Merchant onboarding, specification section 7.
- Hindi and Hinglish rendering through the whole agent conversation, not only search.
- A short screen-recording of the eleven steps against the live API, for the pitch.

## Boundaries, unchanged

Never touch: `packages/transaction-kernel`, `packages/platform-db`,
`packages/payment-adapters`, `packages/durable-work`, `packages/commerce-api`,
`packages/durable-worker`, `packages/commerce-domain`; `merchant_sim`'s `fees.py`,
`policy.py`, `store.py`, `kernel_adapter.py`, `injection.py`; `lib/api/client.ts`,
`types.ts`, `problem.ts`; `lib/security/**`; `app/api/**`; `conftest.py`, `pyproject.toml`,
`uv.lock`, `.github/**`, `infra/**`, `docs/adr/**`, `PROJECT_SPECIFICATION.md`,
`docs/DEMO.md`, `docs/STATUS.md`.

`mock.ts`: the `FIXTURE` array is yours, the contract functions are not.

## Gate before every commit

```bash
cd apps/buyer-web && npm run lint && npm run typecheck && npm run test && npm run build && npm run e2e
cd ../merchant-console && npm run lint && npm run build
export PATH="$HOME/.local/bin:$PATH"
uv run --no-sync python -m pytest packages/merchant-sim -o addopts="" -q
```

Commit small, on your branch only. Never merge, rebase or push.

## Reporting

Append to `docs/briefs/GEMINI_REPORT.md` with real command output. For this brief
especially, say plainly **which screens you actually exercised against the live API** and
which are still only proven against the mock. That distinction is the most useful thing you
can tell me, and an overstated claim here costs more than an admitted gap: if the live path
is broken and the report says otherwise, it will be discovered while the demonstration is
being recorded.
