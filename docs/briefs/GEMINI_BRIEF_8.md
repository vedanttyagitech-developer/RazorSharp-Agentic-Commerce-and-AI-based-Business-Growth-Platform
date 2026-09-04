# Brief 8 for Gemini — turn the Merchant Console into a real product

The console is five thin pages, 1,021 lines, showing figures labelled `SIMULATED`. It is the
weakest surface in the project and it carries the story Razorpay cares about most: what this
platform is worth to a merchant. This brief makes it a product.

---

## STEP ZERO — every session

```bash
cd /Users/vedanttyagi/Desktop/acr-worktrees/gemini-catalogue
pwd && git branch --show-current
git merge --ff-only main
```

`pwd` must be that worktree, branch `gemini/catalogue`. If it shows
`/Users/vedanttyagi/Desktop/Agentic Commerce for Razorpay`, stop and change directory.

## Why this one matters

Razorpay's customers are merchants. The buyer storefront proves the technology; **the
merchant console is where a judge sees the value**. Right now it asserts that value with
invented numbers. Every figure it shows should be derived from real rows in a real database,
and where it cannot be, it must say so.

The single most important screen in the whole console is **retained revenue**: money the
platform preserved by refusing a stale approval. That is step eleven of the demonstration
and the sentence the pitch turns on. It currently reads `₹2,48,920.00` next to the word
`SIMULATED`. Make it real.

## The API is live and has 34 endpoints

`make bootstrap && make seed && make demo` starts PostgreSQL, seeds a demo tenant and runs
the API and worker. Read `docs/DEMO.md` first. Then point the console at
`http://localhost:8000`.

Endpoints you will use:

```
GET  /v1/merchants/{merchant_id}/evidence/retained-revenue?checkout_id=   step 11, the headline
GET  /v1/inspector/payment-attempts/{payment_attempt_id}                  the full forensic document
GET  /v1/checkouts/{checkout_id}/proof                                    the Money Action Proof Chain
GET  /v1/checkouts/{checkout_id}/timeline                                 the action timeline
GET  /v1/checkouts/{checkout_id}/events                                   the same, streamed
GET  /v1/audit/streams/{aggregate_type}/{aggregate_id}/verify             hash-chain verification
GET  /v1/ops/outbox?status=                                               pending, failed, dead commands
POST /v1/ops/outbox/{command_id}/revive                                   operator recovery
GET  /v1/ops/safe-mode        POST /v1/ops/safe-mode                      the kill switch
GET  /v1/orders/{order_id}                                                order with capture evidence
GET  /v1/checkouts/{checkout_id}                                          versions, hashes, attempt
POST /v1/scenario/injections                                              price and stock changes
GET  /v1/config                                                           profile, test mode, reachability
```

Scenario and ops routes need the `X-Scenario-Key` header, which stands in for the merchant
operator surface in P0. If you need an endpoint that does not exist, **write it in
`docs/briefs/REQUESTS_TO_CLAUDE.md` rather than inventing a shape**. I will build it.

## Priority 1 — Retained revenue, made real

Rebuild `/evidence` around the live endpoint. It must answer, from committed rows:

- **What the platform saved.** For each controlled scenario: version N's approved total, the
  total after the merchant's change, what the buyer actually paid on version N+1, and the
  difference. Present it as the arithmetic it is, not a single figure a reader must trust.
- **What was prevented.** Duplicate charges prevented, double refunds prevented, stale
  approvals refused. Each figure links to the checkout that produced it, so a sceptical
  reviewer can click through to the proof chain and check.
- **Verification, visible.** Call the audit verify endpoint and show the result: intact, the
  chain length, and the head sequence. An evidence page that cannot verify its own evidence
  is decoration.

Specification 9.3 lists eighteen revenue metrics. Show the ones you can compute from real
rows: attempted, admitted, authorized, captured, refunded revenue, net retained after
refunds and discount cost, payment-completion rate, refund and cancellation rate, order
value and average order value, and conversion after a price or stock reapproval. **For any
metric you cannot derive from the database, either omit it or label it clearly.** A page
mixing real and invented numbers without distinction is worse than one showing fewer.

## Priority 2 — Operations: orders, refunds, and the review queue

The console has no way to see what is happening. Add:

- **An order list** with state, amount, capture evidence source, and age. Filter by state.
  Click through to an order detail showing its versions, its payment attempt, its refunds
  and its proof chain.
- **A refund view**: refunds in flight, their provider state, and which are reconciling. The
  states that matter and are easy to get wrong are `REFUND_PENDING`, `REFUND_UNKNOWN` and
  `REFUND_FAILED`; show them as genuinely different things, because conflating the first two
  is how a buyer gets refunded twice.
- **The human-review queue**, read-only. Each case with its blocking reason code, its
  redacted timeline, its proof-chain reference, and the verified provider state at the moment
  of escalation. **P0 ships the queue and the evidence, not a resolution workflow** — no
  assign button, no decision recording, no "resolve" action. A button that does nothing is
  worse than no button. Say plainly that a reviewer acts outside this surface today.
- **The outbox view**, from the ops endpoints: pending, failed and dead commands, with the
  revive action, which does exist.

## Priority 3 — Catalogue management over 247 products

`/catalogue` currently lists a handful. It should be a working merchant tool:

- Search, filter by category, sort by price or stock. Two hundred and forty-seven rows needs
  pagination or virtualisation; a table that janks at 247 rows will jank on camera.
- Per product: price in rupees rendered from integer paise, stock, listed or delisted, tax
  basis points, and the category tiles it appears under.
- **Price and stock changes go through the scenario injection endpoint**, which is the real
  merchant-state path the kernel revalidates against. Changing a price here and watching a
  buyer's approved checkout get refused in the storefront is the most persuasive thing you
  can build this week. Wire it so that demonstration is two windows side by side.
- Never let the console write a price directly to a database. It proposes through the
  endpoint; the platform decides.

## Priority 4 — Onboarding, with the twelve steps that exist

`/onboarding` is one page against a specification with twelve configuration steps (7.1):
tenant and merchant identity, admin verification, business profile and locales, stores and
service areas, catalogue connector, field mapping, currency and rounding, fulfilment zones
and fees, reservation TTL and substitution policy, discounts and margin floors, cancellation
and refund policy, and approval and delegated-authority policy.

Build it as a real multi-step flow with saved progress. It closes a P0 acceptance criterion:
*a second tenant configured without code changes*. Where a step needs a backend that does not
exist, render the form, persist locally, and mark it clearly as not yet wired.

## Priority 5 — The Protocol Inspector, for the reviewer

`/inspector` is 97 lines. The inspector endpoint returns a complete forensic document:
attempt state history, grant lifecycle with consumption time, the outbox command and its
attempts, every provider request redacted, every webhook delivery with dedupe status,
reconciliation runs, the order and its refunds.

Render it as something a payments engineer would enjoy reading. This is the page that proves
the claims to the person most able to check them.

## Rules

1. **Proposals never self-apply.** A growth proposal shows its lever, metric, policy gate,
   evidence source and reversibility, and a human presses apply. Specification 6.6.
2. **Synthetic data stays labelled** until it comes from the database. You already do this;
   keep doing it. An unlabelled invented figure on a payments submission discredits every
   real one beside it.
3. **Money is integer paise**, formatted for display only. Never compute a total in the
   browser.
4. **The console never writes a financial table.** It reads, and it proposes through
   endpoints.
5. **No secret in frontend code.** The scenario key comes from the server side, as the
   storefront's session token does.
6. **Mobile matters less here** than on the storefront, but the console must not be broken
   below 1280px. A judge may open it on a laptop beside the storefront.

## Boundaries

Yours: `apps/merchant-console/**`, `apps/buyer-web/src/{app,components,features}/**` except
`app/api/**`, `apps/buyer-web/public/**`, `agent_runtime/prompts/**`, and `merchant_sim`'s
`catalogue.py`, `search.py`, `textfold.py`, `grounding.py`.

Not yours: `packages/transaction-kernel`, `platform-db`, `payment-adapters`, `durable-work`,
`commerce-api`, `durable-worker`, `commerce-domain`; the whole of `agent-runtime` except
`prompts/` (I am writing `harness/`, `core/`, `specialists/`, `capabilities/`, `grounding/`,
`runtime_adk/` right now); `merchant_sim`'s `fees.py`, `policy.py`, `store.py`,
`kernel_adapter.py`, `injection.py`; `lib/api/{client,types,problem}.ts`; `lib/security/**`;
`conftest.py`, `pyproject.toml`, `uv.lock`, `.github/**`, `infra/**`, `docs/adr/**`,
`PROJECT_SPECIFICATION.md`, `docs/DEMO.md`, `docs/STATUS.md`.

The console needs its own typed API client. Build it at
`apps/merchant-console/src/lib/api/` — it is yours. Do not import from the storefront's
`lib/api/`, which is mine and shaped for the buyer.

## Gate

```bash
cd apps/merchant-console && npm run lint && npm run typecheck && npm run build
cd ../buyer-web && npm run lint && npm run typecheck && npm run test && npm run build && npm run e2e
```

Add unit tests for anything that formats money or computes a derived metric, and a Playwright
walk of the console's main path if time allows.

## Reporting

Append to `docs/briefs/GEMINI_REPORT.md` with real command output. Say specifically **which
figures are now drawn from the live API and which are still simulated**. That distinction is
the whole point of this brief, and getting it wrong is the kind of thing a judge finds by
clicking one link.
