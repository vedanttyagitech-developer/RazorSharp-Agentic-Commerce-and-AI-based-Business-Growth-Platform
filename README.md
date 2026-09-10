# RazorSharp

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

## What the kernel actually checks

`admit()` runs inside one database transaction and refuses at any of these, in order
(`packages/transaction-kernel/src/transaction_kernel/admission.py`):

| # | Check | Refuses when |
|---|---|---|
| 4 | Actor capability | the acting party may not submit this operation at all |
| 5 | Safe Mode | an operator has paused financial admissions |
| 3+6 | Version currency | the approved bytes are not the stored bytes, or a newer version exists |
| 6a | Recorded consent | the buyer's approval is missing, expired, or already spent |
| 6 | Policy binding | the sale's frozen terms cannot be verified |
| 7 | Stock hold | the reservation has expired or been released |
| 8–10 | Merchant truth | price, fee or availability moved since approval |
| 10 | Spending authority | a Reserve Pay permission does not cover this purchase |
| 12 | Exactly one winner | a live payment attempt already exists for this checkout |

A refusal arrives as **HTTP 200 with `allowed: false`**, never a 4xx — a denial is the
system working, and an error status invites a client to retry it as though it were a fault
(ADR 0003 D15).

## Real, or simulated

The single most important thing to understand about this repository:

| Part | Status |
|---|---|
| **Razorpay payments** | **Real**, test mode. The Action Executor calls `api.razorpay.com`; the dev database holds 41 orders and 159 attempts carrying a genuine provider order id. |
| **Transaction Trust Kernel** | Real. Every admission, grant, reservation and receipt is a real row with real locking. |
| **Buyer copilot & voice** | Real. Gemini via Vertex AI, live speech-to-text and synthesis. Needs cloud credentials. |
| **Merchant** | **Simulated.** `merchant-sim` is a deterministic in-memory stand-in for a merchant connector — no database, no network, no clock. That determinism is what lets a quote stand as evidence. |
| **Reserve Pay** | **Mixed.** The delegated authority, its bounds, admission and the executor path are real; the *provider* is a server-side simulator. Not NPCI UAP certification or integration. |
| **Gmail campaigns** | Simulated. No live delivery integration. |

Capture is never inferred from the browser. A browser callback is recorded as
`BROWSER_CALLBACK` evidence and nothing more; an order row is written only from `WEBHOOK` or
`PROVIDER_FETCH` evidence (ADR 0003 D8).

## Layout

Fourteen Python packages under `packages/`, and the front end under `apps/`.

```
transaction-kernel     the only component that may authorize money movement
commerce-domain        the vocabulary everything else hashes and compares
commerce-api           87 HTTP routes: buyer, merchant, operator, protocol
agent-runtime          the copilot: specialists, capabilities, grounding, ADK adapter
voice-runtime          the voice gateway: STT, TTS, the wire, the speech guard
action-executor        spends Execution Grants; the only caller of Razorpay
durable-work           the outbox and command vocabulary
platform-db            schema, roles, RLS, migrations
merchant-sim           the deterministic merchant stand-in
merchant-adapter       the connector boundary
merchant-controller    merchant actions and policy
payment-adapters       the Razorpay adapter
commerce-protocols     MCP, ACP, UCP, AP2
platform-observability timing and instruments

apps/razorsharp-concept  the buyer and merchant front end (123 TS/TSX files)
```

Protocol surfaces do not bypass the kernel: an ACP or MCP caller is admitted by the same
`admit()` path a browser is.

## Tenancy

26 of 30 tables carry row-level security, and it is `FORCED` — the owning role cannot bypass
it either. The four exemptions are deliberate and asserted by a test: `tenants` and
`alembic_version` hold no tenant data, `platform_operating_modes` has a nullable tenant
because the global switch has none, and `api_sessions` cannot require a tenant because
resolving a bearer token is the step that *discovers* one.

## Running it

Python 3.14, uv, PostgreSQL, Node 22.13+. Copy `.env.example` and keep credentials out of Git.

```sh
uv sync --all-extras --dev
make bootstrap     # databases, roles, migrations
make seed          # the demo tenant and its catalogue
make demo          # API :8000, voice gateway :8100, Action Executor
```

Then the front end, in another terminal:

```sh
cd apps/razorsharp-concept && npm ci && npm run dev -- --port 3000
```

**Four processes must be running**, not three: the API, the voice gateway, the **Action
Executor**, and the front end. The API physically cannot create a Razorpay order — its
database role has no write on financial tables — so with the executor down, checkout stalls
waiting for a provider order that will never appear.

Voice additionally needs Google Cloud credentials; without them the copilot still answers
over HTTP and says so. See [`apps/razorsharp-concept/README.md`](apps/razorsharp-concept/README.md)
for the front end and [`docs/DEPLOY.md`](docs/DEPLOY.md) for deployment.

## Verification

```sh
make gate          # lint, then types, then tests -- the order that fails fastest
```

At the current commit: **5,944 tests passing**, 21 skipped, mypy `--strict` clean across 258
source files, ruff clean. Roughly 94k lines of source and 82k lines of tests.

Markers keep the default run honest rather than convenient:

- `db` — needs PostgreSQL with migrations and roles. CI **fails if these skip**, because
  RLS, single-winner admission and grant-spend-once are proven only in database-backed
  suites, and a green run that proved none of them is worse than a red one.
- `voice_live` — drives real speech through Gemini and the running API.
- `razorpay_live` — calls Razorpay test mode over the network.

The last two are excluded from the default run and are separate credentialled checks, never
silently treated as offline passes.

## Decisions

Five ADRs in [`docs/adr/`](docs/adr/) record why things are the way they are, and
[`docs/KNOWN_GAPS.md`](docs/KNOWN_GAPS.md) records what is not built. Documents that asserted
a *current state* were deliberately deleted; the ones that record a *decision* stayed.

This repository is the development and test-mode implementation.
