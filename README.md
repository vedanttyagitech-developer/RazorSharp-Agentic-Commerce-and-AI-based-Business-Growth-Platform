# RazorSharp

### 🔗 Live demo — **[vedanttyagi.tech](https://vedanttyagi.tech)**

> **Status: deploying.** The infrastructure is written and the application runs end to end
> locally against real Razorpay test-mode APIs. The public host is not serving yet — see
> [Deployment status](#deployment-status) for exactly what is and is not done. This README
> states nothing it has not checked.

---

Agentic commerce for Razorpay. A buyer talks or types to a copilot that finds things and
*proposes* actions; a **Transaction Trust Kernel** is the only component permitted to
authorize money movement, and it re-checks every proposal against the buyer's own recorded
consent, the exact bill they saw, and the merchant's live state before a rupee moves.

The interesting claim is not that an AI can shop. It is that an AI can shop **and be unable
to spend your money incorrectly**, and that the platform can prove which of the two happened.

## The idea in one paragraph

An agent proposes. A person approves. A kernel authorizes. Those are three different
components with three different capability sets, and the boundaries between them are
enforced in code, not by convention. A model can search the catalogue and suggest a cart; it
cannot approve a bill, cannot mint spending authority, and cannot write a financial table.
When it proposes something and the shop's price has moved since you agreed, the kernel
refuses the payment and tells you exactly what changed.

---

## Twelve things here that are not the usual demo

Every one is enforced in code and has a test. The file is named so you can check it.

**1. A model cannot pay — not because it is checked, because the capability does not exist.**
`checkout.approve`, `payment.execute`, `payment.capture`, `refund.approve` and
`authority.revoke` have *no row* in the specialist action table. There is nothing to grant
by mistake, no flag to flip, no prompt to jailbreak into one. An AST test walks every source
file in both AI packages and fails if either so much as imports the kernel.

**2. The tool gate refuses by closure identity, not by name.** Hand-build a tool, name it
exactly what the factory names a real one, attach it to the toolset — still refused, because
the gate compares the callable itself with `is` against what the factory produced. A forged
name is a string; object identity cannot be forged.

**3. Exactly one payment can win, and it is the database that says so.** A partial unique
index — `uq_payment_attempts_one_non_terminal` — not an application check. Proven with twelve
real threads on twelve real sessions: one winner, eleven losers, one attempt row, one grant,
twelve audit events on an intact hash chain.

**4. The Execution Grant is spent and committed *before* the provider is called.** Duplicate
charges come from one ordering mistake: calling the payment provider before the record is
durable. Here single-use authority is consumed and committed first, so a worker that dies
mid-flight replays into `GrantAlreadyConsumedError` — which is a *resume* path, not a
failure. The trade is named in the code: this order can leave a grant spent with no provider
order, which is recoverable; the other order can leave a charge with no record, which is not.

**5. Your terms are frozen at approval and re-verified at payment.** A Policy-at-Sale Receipt
hashes the merchant's cancellation, refund, substitution, delivery and fulfilment terms when
you approve. Admission re-derives that hash from the stored document every time — so editing
a term *and* its stored hash together still fails, because the receipt independently names
the tenant, merchant, checkout and version it belongs to.

**6. A price change you would never notice still stops the payment.** `REAPPROVAL_REQUIRED`
runs three comparisons, not one — so a merchant who raises an item and drops the delivery fee
by the same amount, leaving your total identical, is caught. Version N is retired, N+1 is
written, and you are shown the exact fields that moved.

**7. The assistant may not say a number the server did not prove.** An amount is speakable
only if it appears, *to the paisa*, in the set of integer minor units that turn's tools
actually returned. Money sentences are not written by the model at all — approvals, totals,
deltas, expiry and payment outcomes come from versioned templates. Identifiers are never
spoken unless you asked for one.

**8. "Yes" out loud is heard, never recorded as consent.** The voice gateway mints no
principal, holds no capability, and makes exactly three outbound calls — all with your own
bearer token. It reads the approval card from the trusted server, speaks it from a template,
matches your next settled sentence against a closed lexicon, and *reports what it heard*. The
approval itself happens on the screen.

**9. An unknown payment outcome is never turned into a failure.** Exactly five HTTP statuses
— `{400, 401, 403, 404, 422}` — are on the "this definitely did not happen" allowlist.
Everything else becomes reconciliation, because telling you a payment failed when it may
have succeeded is how somebody pays twice.

**10. A browser callback is never capture.** `POST /v1/payments/verify` verifies the HMAC,
records `BROWSER_CALLBACK` evidence and queues reconciliation — and that is *all* it does.
`apply_provider_evidence` refuses browser-callback evidence at its source gate, so an order
row can only ever be written from a webhook or a server-side provider fetch. This is enforced
by a type, not a convention: the verify response hard-codes
`evidence_kind: Literal["BROWSER_CALLBACK"]`.

**11. Tenant isolation fails closed.** The RLS predicate is
`tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid` — an unbound
connection matches *nothing* rather than everything. **29 of 32 application tables have RLS
enabled and FORCED**, so the owning role cannot bypass it either, and no `commerce_*` role is
a superuser or holds `BYPASSRLS`. One `RESTRICTIVE` policy separates who may *write* evidence
from who may *edit* it.

**12. Any sale can be re-verified end to end, by anyone, over HTTP.**
`GET /v1/checkouts/{id}/proof` walks ten links — intent, merchant state, checkout, policy
receipt, approval, kernel decision, grant and command, provider requests, verified evidence,
final state — and returns a verdict with **fifteen named checks**, including
`content_hash_recomputed`, `grant_consumed_once`, `every_mutation_consumed_a_grant`,
`capture_evidence_is_verified` and `amounts_agree`. The audit streams are hash-chained and
verified in the same answer. Nothing is taken on trust, including by us.

---

## Features in full

**89 HTTP routes across nine surfaces**, backed by **14 Python packages — roughly 97k lines
of source and 83k of tests** — and a 16.5k-line front end.

### The front end

Not a thin demo shell. **16,486 lines of TypeScript/TSX** across **100 components** and 25
library modules, on three surfaces — the platform home `/`, the shopping copilot `/shop`,
and the merchant workspace `/merchant`.

**Buyer** — a conversational shopping surface (`shopping-showcase`, `discovery-cards`,
`product-comparison`) that flows into an exact-bill review (`order-review`), manual Razorpay
checkout (`manual-checkout`, `checkout-payment`, `payment-window`), and the recovery surfaces
most demos never build: `checkout-recovery`, `previous-payments`, `continuity` and
`payment-acknowledgement`. Live order tracking in `live-orders`. Reserve Pay has its own
consent and checkout flow (`reserve-pay`, `reserve-checkout`).

**Merchant** — `live-merchant` with real insights computed from order rows
(`live-merchant-insights`), publishable policy families (`live-merchant-policy`), and the
refund path a merchant *may* approve (`merchant-refund`, `refund-approval-panel`).

**The kernel, made visible** — `transaction-kernel` renders a refusal the way the kernel
actually answered it: which check stopped it, whose action it was about, whether a second
payment is in flight, exactly which fields moved, and the identifiers that follow the
decision through the audit stream. It also renders **four timing spans** — you deciding, the
kernel admitting, waiting for a worker, paying at Razorpay — marking the single span a
payment provider can see. `trust-boundaries` draws the capability walls.

**Voice** — `voice-session` is the largest component in the app (514 lines), with
`voice-wave` for live audio and `copilot-status` for degradation, over a typed wire
(`lib/voice/wire.ts`) that a Python test holds the server's frame contract against.

**Designed, not templated** — `statue-canvas`, `ascii-field`, `possibility-scene`,
`campaign-artwork`, `identity-hero`, `impact-deck`, `scramble-title` and a motion system
(`motion`, `response-motion`) carry the visual language across 1,325 lines of hand-written
CSS.

Front-end checks are separate from the Python gate: TypeScript strict, oxlint, and
**175 tests** under `node --test`.

### Buyer
Grounded discovery over **247 products in 10 categories** · durable cart that survives a
reload · **exact-bill approval** against a content hash · manual payment on Razorpay's own
hosted Checkout · order history and tracking · **look an order up by the number you were
shown** (`RS-260910-P0G7K7F`, read case- and hyphen-insensitively) · checkout continuity, so
an unfinished purchase is recoverable rather than orphaned · support cases raised against a
real order · refunds you request but never approve.

### Payment recovery
The part most demos skip. Reload the page mid-payment and the same purchase is offered back
to you — **same checkout, same payment attempt, same Execution Grant, same provider order**,
never a new one. Lose the backend entirely and the screen says the connection is interrupted
and the outcome is unknown; it claims neither success nor failure, keeps the payment
reference on screen, and resumes on its own when the API returns. A provider surface that
loads blank has an explicit **"Return to payment status"** escape that frees the page without
starting a second payment and leaves the order resumable.

### Voice
One WebSocket, live Gemini speech-to-text and synthesis. PCM16 mono in at 16 kHz, out at
24 kHz. **Typed degradation frames** — when speech breaks, the wire names which part
broke and typing keeps working. Barge-in, an echo gate, and a closed consent lexicon that is
*heard and reported*, never recorded as approval.
Typed and spoken turns share one context, and the **latest intent wins**: ask for milk, then
type "actually, bread", and the milk turn is dropped rather than answered late.

### Two copilots, six internal agents
Gemini on Vertex AI. Buyers see one **Commerce Assistant**; merchants see one **Merchant
Copilot**. Six agents collaborate inside those two harnesses:

| Visible harness | Internal agents |
|---|---|
| Commerce Assistant | Coordinator · Discovery & Basket · Checkout & Order · Customer Support |
| Merchant Copilot | Coordinator · Merchant Operations |

Every one has a closed, enumerated action set. Routing and language detection are
**deterministic** — no model decides who answers or what language you spoke. Falls back to a
deterministic runner when Vertex is unconfigured, and says so rather than going quiet.

### Reserve Pay
A bounded, revocable licence to spend without asking again: per-purchase and total caps,
optional product scope, expiry. Revocation is a monotonic epoch that outranks every other
bound. Capacity is defended three times and released at most once. Only the buyer creates or
revokes one; Safe Mode closes this path while leaving a human-present checkout open.

### Merchant — the growth half
Track 1 asks for revenue growth *or* an AI buyer completing a transaction. This does both,
and the kernel is what makes the growth half safe: the same component that blocks a stale
execution is what enables recovery, substitution, reapproval, payment retry and reorder.

> Growth is measured as **captured and retained revenue**, not merely orders created.

Seven action kinds and twelve states through propose → submit → approve → execute
(`/v1/merchant/actions/...`), with approval bound to the hash the approver actually read.
Five publishable policy families (cancellation, refund, return, substitution, fulfilment).
**Insights computed from real order rows** over a 1–90 day window, never generated by a
model. **Retained-revenue evidence** per merchant at
`/v1/merchants/{id}/evidence/retained-revenue` — a recovered sale you can point at rather
than a claimed uplift. A merchant may approve a refund; a merchant may not approve a buyer's
checkout.

### Evidence
The ten-link **proof chain** with its fifteen checks · a checkout **timeline** and raw event
stream · **hash-chain verification** for any audit stream
(`/v1/audit/streams/{type}/{id}/verify`) · **retained-revenue evidence** per merchant · a
**payment-attempt inspector** and a protocol-interaction inspector for forensics · a payment
acknowledgement a buyer can keep, which refuses to render without verified capture evidence.

### Operations
Metrics · outbox visibility · **dead-letter revive**, so a command whose grant expired can be
brought back deliberately rather than lost · Safe Mode read and write · a human **review
queue** and a **reconciliation queue** for outcomes a machine should not decide alone.

### Protocols — four, not one
Roughly 8,700 lines across four protocol surfaces, each with its own conformance boundary.

| | What is implemented | Live routes |
|---|---|---|
| **UCP** | Business and platform profiles at `/.well-known/ucp/` | 2 |
| **AP2** | Human-present mandate flow, v0.2 | via checkout |
| **ACP** | Checkout sessions: create, read, complete, cancel | 4 |
| **MCP** | 13 tools behind OAuth protected-resource discovery and minted tokens | 4 |

**Exactly one MCP tool can reach kernel admission**, and it still cannot move money by
itself. No tool can name an amount: `ArgumentKind` has no monetary member, which is what
makes `order.propose_cancellation` and `refund.propose` honest. ACP is signature-verified
over a length-prefixed canonical string, with replay protection as a database uniqueness
guarantee rather than an in-memory set.

A **Protocol Inspector** (`/v1/inspector/protocols/{id}`) and a conformance endpoint
(`/v1/protocols/conformance`) let a reviewer read what each surface actually claims. Each
protocol declares a `ClaimBoundary` — `LOCAL_CONFORMANCE`, `COMPATIBLE_INTERFACE` or
`PUBLIC_INFORMATION_ALIGNMENT` — stating how far the claim goes instead of asserting
compliance. ACP compatibility is **not** ChatGPT availability, and the code says so.

### Demonstrating failure, on purpose
A scenario surface arms **real** faults — provider timeouts, stale captures, transport errors,
duplicate submits, expired reservations, webhook replay — so the recovery paths can be shown
rather than described. Faults disarm themselves in the same update that consumes them, so a
demo cannot leave one armed. Dedicated failure-scenario suites cover authority lapsing
mid-flight, an unavailable merchant connector, a discount limit and the database going away.

---

## What the kernel actually checks

`admit()` runs inside one transaction and trusts nothing the caller passed it:

```
 4  can this actor submit this operation at all?
 5  Safe Mode, before anything delegated is accepted
 6  lock the checkout, confirm the version is current
6a  the buyer's own approval, re-read from the database
 7  reservation, against the database clock
8-10 re-read merchant truth, compare with what was approved
10+ authority, locked in the same transaction
12  exactly one winner
```

A refusal is a **business outcome, not an exception**: HTTP 200 with `allowed: false`, the
reason, whose action caused it, whether any money moved, and the exact next step.

## Real, or simulated

| | |
|---|---|
| **Razorpay payments** | **Real**, test mode. The Action Executor creates real orders at `api.razorpay.com` under an Execution Grant. |
| **Kernel, RLS, grants, audit** | Real. PostgreSQL, real roles, real constraints. |
| **Catalogue and merchant** | Simulated: a deterministic in-process store with 247 products. |
| **Delivery, logistics** | Simulated. |

## Layout

```
packages/          14 Python packages
  transaction-kernel     admission, grants, authority, refunds, audit
  commerce-api           89 HTTP routes: buyer, merchant, operator, protocol
  action-executor        the only component that calls the payment provider
  durable-work           outbox, leasing (FOR UPDATE SKIP LOCKED), fencing tokens
  agent-runtime          specialists, capability gate, grounding ledger
  voice-runtime          gateway, STT/TTS, degradation frames, consent lexicon
  commerce-protocols     MCP, ACP
  platform-db            schema, RLS, roles
  merchant-sim           catalogue, pricing, stock
  ...
apps/razorsharp-concept  the front end: 100 components, 25 lib modules, 16.5k lines
infra/                   Terraform (GKE, Cloud SQL) and Kubernetes manifests
docs/pitch-video/        the 5-minute pitch, as a Remotion composition
```

## Running it

Four processes: the API on **8000**, the voice gateway on **8100**, the **Action Executor**,
and the front end on **3000**.

```sh
scripts/run_demo.sh
```

The front end must be on **port 3000** — the voice gateway's origin allowlist defaults to
exactly that, so voice fails on any other port while everything else keeps working. Voice
additionally needs Google Cloud credentials; without them the copilot still answers over HTTP
and says so.

**With the Action Executor down, checkout stalls forever.** That is by design: the API's role
cannot write a financial table, so `razorpay_order_id` stays honestly `null` until the worker
spends the grant.

## Verification

```sh
make gate          # lint, then types, then tests -- the order that fails fastest
```

**6,389 automated tests.** Measured on this commit, not carried over:

| | |
|---|---|
| Backend (pytest) | **6,212 passing** of 6,214 collected |
| Front end (`node --test`) | **175 passing** |
| Needing a real PostgreSQL | **1,820** — RLS, locking and constraints cannot be faked |
| Adversarial / failure-scenario files | 47 |
| Types | mypy `--strict` clean across **278** source files; TypeScript strict clean |

The two failing backend tests are a known cross-package `conftest` import collision in
`test_sales_assistance.py` — the file passes **15/15** run on its own. It is a test-harness
defect, not a product one, and it is written here rather than left to be discovered.

Two numbers worth more than the total: **1,820 tests connect to a real PostgreSQL**, because
row-level security, `FOR UPDATE` ordering and a partial unique index cannot be proven any
other way — and the adversarial suites are written to *attack* the thing they cover rather
than confirm it.

Markers keep the default run honest rather than convenient:

- `db` — needs PostgreSQL with migrations and roles. CI **fails if these skip**, because RLS,
  single-winner admission and grant-spend-once are proven only in database-backed suites, and
  a green run that proved none of them is worse than a red one.
- `voice_live` — drives real speech through Gemini and the running API. Excluded from the
  default run; a separate credentialled check, never a silent offline pass.

Two things worth stating plainly:

- **`razorpay_live` is declared and unused.** Zero tests carry it. The marker exists in
  `pyproject.toml` and in the Makefile's exclusion flag, and nothing is behind it.
- The voice wire-contract test reads `apps/razorsharp-concept/lib/voice/wire.ts` and holds
  the server's frame contract against the front end that actually ships.

### Verified live, not just unit-tested

Run against real Razorpay test-mode APIs, with the identifiers recorded:

- **Reload mid-payment → recover → complete.** Same checkout, same payment attempt, same
  Execution Grant, same provider order — one attempt row, one grant, one order.
- **Delayed confirmation.** With the Action Executor paused, a completed provider payment
  recorded `BROWSER_CALLBACK` and the attempt stayed `SUBMITTED` with **zero** order rows.
  Releasing the worker produced a real `PROVIDER_FETCH` capture and **exactly one** order.
- **Bounded reconciliation.** 34 empty provider status probes at 5.7× the reconciliation
  bound did not escalate a buyer who was still paying.
- **Backend outage mid-recovery.** The UI reported an interrupted connection and an unknown
  outcome, claimed neither success nor failure, and resumed on the same attempt.

## Deployment status

Stated honestly, because a README that overclaims is worse than one that admits a gap.

| | |
|---|---|
| Terraform (GKE Autopilot, Cloud SQL, Artifact Registry, Secret Manager, Workload Identity) | **Written and schema-validated** |
| Kubernetes manifests — API and Action Executor | **Written** |
| Container image — `commerce-api` | **Written** |
| Container image — `action-executor` | **Written** |
| Container image — `voice-gateway` | **Built and run-verified** — 353 MB, `/healthz` 200, non-root uid 10001 |
| Front end image | **Not needed** — it builds a Cloudflare Worker (`dist/server/wrangler.json`), so it deploys with `wrangler deploy` rather than a container |
| Action Executor `/healthz` | **Not done** — a liveness probe points at a server that does not exist yet |
| Kubernetes manifests — voice gateway | **Not done** — no Deployment, Service or ingress route |
| Public host + managed certificate | **Not done** — needs the above, then a DNS `A` record |

### The shape this is heading for

The front end is a Cloudflare Worker and the DNS is already on Cloudflare, so the natural
split is: **front end on Cloudflare Workers, API and worker on GKE**, with the Worker's
`COMMERCE_API_URL` pointing at the GKE ingress. The front end proxies every buyer call
server-side through `/api/commerce/*`, so nothing about that boundary changes.

The voice gateway is the one service that must be a long-lived Pod rather than a per-request
runtime, and the reason is in the code, not in preference: the ticket store is in-memory and
process-local while minting and redemption arrive as two separate connections, and the socket
loop is deliberately unbounded so a session outlives the provider's stream limit.

When the DNS record is finally created it must be **DNS-only (grey cloud)**. A Google-managed
certificate validates by resolving the name straight to the load balancer, and Cloudflare's
proxy would intercept that; the proxy also closes idle WebSockets at 100 s on Free and Pro,
which this gateway has no heartbeat for.

`docs/DEPLOY.md` is **stale**: it describes deploying two front ends that were deleted.

## Tenancy

Every application table carries `tenant_id`. RLS is enabled **and FORCED** on 29 of 32, so
the owning role cannot bypass it. The three exclusions are deliberate and each is asserted by
a test rather than assumed.

## Decisions

Architecture decision records live in [`docs/adr/`](docs/adr). ADR 0003 is the kernel:
denial as an answer, capability absence, commit-before-send, and why a browser callback is
never evidence.
