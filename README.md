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

## Ten things here that are not the usual demo

Every one is enforced in code and has a test. The file is named so you can check it.

**1. A model cannot pay — not because it is checked, because the capability does not exist.**
`checkout.approve`, `payment.execute`, `payment.capture`, `refund.approve` and
`authority.revoke` have *no row* in the specialist action table. There is nothing to grant
by mistake, no flag to flip, no prompt to jailbreak into one. An AST test walks every source
file in both AI packages and fails if either so much as imports the kernel.

**2. The tool gate refuses by closure identity, not by name.** Hand-build a tool, name it
exactly what the factory names a real one, attach it to the toolset — still refused, because
the gate compares the callable itself with `is` against what the factory produced.

**3. Exactly one payment can win, and it is the database that says so.** A partial unique
index — `uq_payment_attempts_one_non_terminal` — not an application check. Proven with twelve
real threads on twelve real sessions: one winner, eleven losers, one attempt row, one grant,
twelve audit events on an intact hash chain.

**4. Your terms are frozen at approval and re-verified at payment.** A Policy-at-Sale Receipt
hashes the merchant's cancellation, refund, substitution, delivery and fulfilment terms when
you approve. Admission re-derives that hash from the stored document every time — so editing
a term *and* its stored hash together still fails, because the receipt independently names
the tenant, merchant, checkout and version it belongs to.

**5. A price change you would never notice still stops the payment.** `REAPPROVAL_REQUIRED`
runs three comparisons, not one — so a merchant who raises an item and drops the delivery fee
by the same amount, leaving your total identical, is caught. Version N is retired, N+1 is
written, and you are shown the exact fields that moved.

**6. The assistant may not say a number the server did not prove.** An amount is speakable
only if it appears, *to the paisa*, in the set of integer minor units that turn's tools
actually returned. Money sentences are not written by the model at all — approvals, totals,
deltas, expiry and payment outcomes come from versioned templates. Identifiers are never
spoken unless you asked for one.

**7. "Yes" out loud is heard, never recorded as consent.** The voice gateway mints no
principal, holds no capability, and makes exactly three outbound calls — all with your own
bearer token. It reads the approval card from the trusted server, speaks it from a template,
matches your next settled sentence against a closed lexicon, and *reports what it heard*. The
approval itself happens on the screen.

**8. An unknown payment outcome is never turned into a failure.** Only five HTTP statuses are
on the "this definitely did not happen" list. Everything else becomes reconciliation, because
telling you a payment failed when it may have succeeded is how somebody pays twice.

**9. Tenant isolation fails closed.** The RLS predicate is
`tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid` — an unbound
connection matches *nothing* rather than everything. 26 of 29 application tables have RLS
enabled **and FORCED**, so the owning role cannot bypass it either, and no `commerce_*` role
is a superuser or holds `BYPASSRLS`. One `RESTRICTIVE` policy separates who may *write*
evidence from who may *edit* it.

**10. Any sale can be re-verified end to end, by anyone, over HTTP.**
`GET /v1/checkouts/{id}/proof` walks ten links — intent, merchant state, checkout, policy
receipt, approval, kernel decision, grant and command, provider requests, verified evidence,
final state — and returns a verdict with **fifteen named checks**. On a real order in this
repository all fifteen pass: `content_hash_recomputed`, `approval_binds_content`,
`grant_consumed_once`, `every_mutation_consumed_a_grant`, `capture_evidence_is_verified`,
`amounts_agree`, `audit_chain_checkout` and eight more. The audit streams are hash-chained
and verified in the same answer — 19 events, intact, with the head hash returned. Nothing is
taken on trust, including by us.

## Features

**87 HTTP routes across nine surfaces.** Grouped by who uses them.

### Buyer
Grounded discovery over 247 products in 10 categories · durable cart that survives a reload ·
**exact-bill approval** against a content hash · manual payment on Razorpay's own hosted
Checkout · order history and tracking · **look an order up by the number you were shown**
(`RS-260909-XW5G26M`, read case- and hyphen-insensitively) · checkout continuity, so an
unfinished purchase is recoverable rather than orphaned · support cases raised against a real
order · refunds you request but never approve.

### Voice
One WebSocket, live Gemini speech-to-text and synthesis. PCM16 mono in at 16 kHz, out at
24 kHz. **Nine typed degradation frames** — when speech breaks, the wire names which part
broke and typing keeps working. Barge-in, an echo gate, and a closed consent lexicon
(16 affirmative, 21 negative) that is *heard and reported*, never recorded as approval.

### Copilot
Gemini 3.8 Flash on Vertex AI. Three specialists with closed action sets — Shopping (8),
Checkout (7), Support (5). Deterministic routing and deterministic language detection: no
model decides who answers or what language you spoke. Falls back to a deterministic runner
when Vertex is unconfigured, and says so rather than going quiet.

### Reserve Pay
A bounded, revocable licence to spend without asking again: per-purchase and total caps,
merchant-wide coverage (optional selected-product scope), valid until revoked. Revocation is a monotonic epoch that outranks every other
bound. Capacity is defended three times and released at most once. Only the buyer creates or
revokes one; Safe Mode closes this path while leaving a human-present checkout open.

Reserve authorization is an ES256/P-256 signed **simulator** artifact, binding the buyer,
tenant, merchant, currency and exact spending limits. The Kernel verifies it on creation
and debit admission; the executor checks it again before sending. Immutable
`verified_authority_proofs` rows enforce unique proof IDs/nonces and remain linked to the
original bounds. Revocation and allocation accounting remain live database controls;
an unknown debit keeps its allocation held until a confirmed outcome arrives.

For a local setup, run `.venv/bin/python scripts/configure_reserve_signer.py` once before
starting `scripts/run_demo.sh`. This writes a stable, dedicated key to ignored `.env`
without printing it. API: `RESERVE_PROVIDER_SIGNING_JWK` (private) and
`RESERVE_PROVIDER_VERIFICATION_JWKS` (public). Executor: public verification set only;
the demo launcher removes its private signing key. Keep old public keys in `keys` on
rotation; list compromised key IDs in `revoked_kids` and restart the services. New debits
then fail closed; already accepted/unknown debits still reconcile. Never reuse AP2 keys.
The signing migration retires unsigned permissions with an audit event; buyers must
explicitly authorize a new permission. No historical permission is silently signed.

The UI exposes verification state and the authorization digest. This proves simulator
issued bounds, **not a bank funds block, NPCI signature or UAP certification**.


#### Demo approval and independent verification

Reserve permissions are created after explicit confirmation of the reviewed limits
by the authenticated demo buyer. No passkey, Touch ID or device PIN is required.
The signed simulator authorization binds buyer, merchant, currency, scope and limits.
This is demo session confirmation, not device-verified consent or a bank mandate.
Idempotent retries return the same permission. Kernel limits, revocation and
single-use execution checks remain enforced.

Previously issued version-two proofs remain verifiable as historical evidence.
The retired WebAuthn tables and migration are retained for that history; no enrollment
or assertion endpoints remain active.

In Reserve Pay, expand **Authorization evidence** to download the signed proof and public
verification keys. Keep/pin the public key set through a trusted channel; downloading an
attacker's key alongside their artifact establishes no trust. Run the separate verifier:

```bash
.venv/bin/python scripts/verify_reserve_authorization.py "/path/to/reserve-authorization.json" \
  --trusted-jwks "/path/to/trusted-reserve-public-keys.json"
```

It uses `jwcrypto` and `rfc8785`, with no application imports, database, private key or
network access. It checks the ES256 simulator signature, canonical payload and the signed
consent/terms digests. `ISSUER_ATTESTED_AND_HASH_BOUND` describes consent evidence, not
independent biometric attestation. Live revocation, remaining capacity and bank
funds authorization are explicitly not established by offline verification. The Kernel
continues to enforce the current authority state when a debit is admitted/executed.

### Merchant
Seven action kinds and twelve states through propose → submit → approve → execute, with
approval bound to the hash the approver actually read. Five publishable policy families
(cancellation, refund, return, substitution, fulfilment). Insights computed from real order
rows over a 1–90 day window, never generated. A merchant may approve a refund; a merchant may
not approve a buyer's checkout.

### Evidence
The ten-link **proof chain** with its fifteen checks · a checkout **timeline** and raw event
stream · **hash-chain verification** for any audit stream
(`/v1/audit/streams/{type}/{id}/verify`) · **retained-revenue evidence** per merchant · a
**payment-attempt inspector** and a protocol-interaction inspector for forensics · a
payment acknowledgement a buyer can keep.

### Operations
Metrics · outbox visibility · **dead-letter revive**, so a command whose grant expired can be
brought back deliberately rather than lost · Safe Mode read and write · a human **review
queue** and a **reconciliation queue** for outcomes a machine should not decide alone.

### Protocols
**MCP** — 13 tools behind OAuth protected-resource discovery and minted tokens; exactly one
can reach kernel admission. **ACP** — five routes, signature-verified over a length-prefixed
canonical string, with replay protection as a database uniqueness guarantee rather than an
in-memory set. **UCP** and **AP2** — published profiles, AP2 against Google's real SDK pinned
to an exact commit. Each protocol is pinned as data with an explicit `ClaimBoundary` —
`LOCAL_CONFORMANCE`, `COMPATIBLE_INTERFACE` or `PUBLIC_INFORMATION_ALIGNMENT` — so the
conformance matrix at `/v1/protocols/conformance` states how far the claim actually goes
instead of asserting compliance.

### Demonstrating failure, on purpose
The scenario controller exists so the hard paths can be shown rather than described:
**nine merchant-state injections** (price, stock, availability, delivery fee, free-delivery
threshold, offer start and end, catalogue reset) and **five armable faults**
(`CREATE_ORDER_TIMEOUT`, `RECONCILE_FETCH_TIMEOUT`, `REFUND_TIMEOUT`, `LLM_FAILURE`,
`TTS_FAILURE`), plus duplicate-submit, forced reservation expiry, open-version invalidation
and webhook replay. Every injection is labelled `SCENARIO_INJECTION` in the audit stream, so
a demonstrated failure can never be mistaken for an organic one.

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
| **Razorpay payments** | **Real**, test mode. The Action Executor creates real orders at `api.razorpay.com` under an Execution Grant. The API additionally makes a read-only provider fetch to show how a payment was made. |
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
action-executor        spends Execution Grants; the only component that MUTATES at Razorpay
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

26 of the 29 application tables carry row-level security, and it is `FORCED` — the owning
role cannot bypass it either, and no `commerce_*` role is a superuser or holds `BYPASSRLS`
(0 of them, checked against the live cluster). The three exemptions are deliberate and
asserted by a test: `tenants` holds no tenant data, `platform_operating_modes` has a nullable
tenant because the global switch belongs to none, and `api_sessions` cannot require a tenant
because resolving a bearer token is the step that *discovers* one. The thirtieth table is
`alembic_version`, which is migration bookkeeping rather than application data.

## Running it

Python 3.14, uv, PostgreSQL, Node 22.13+. Start from `.env.example` and keep credentials out
of Git. It is a starting point rather than a complete file: the three role URLs the stack
actually runs on (`DATABASE_URL_APP`, `_KERNEL`, `_WORKER`) and `GOOGLE_CLOUD_LOCATION` are
not in it, and `make bootstrap` creates three databases — `commerce_dev`, `commerce_test`
and `commerce_dev_adk`, the copilot's conversation store, kept out of the commerce database
on purpose.

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
Executor**, and the front end. Creating a provider order is a *durable command*, not part of
a request: admission issues an Execution Grant, the grant goes to the outbox, and the
executor spends it. The handoff endpoint is a read and honestly answers
`razorpay_order_id: null` until that has happened — so with the executor down, checkout
stalls forever waiting for an order nothing will create.

The front end must be on **port 3000**: the voice gateway's origin allowlist defaults to
exactly `http://localhost:3000` and `http://127.0.0.1:3000`, so voice fails on any other
port while everything else keeps working.

Voice additionally needs Google Cloud credentials; without them the copilot still answers
over HTTP and says so. See [`apps/razorsharp-concept/README.md`](apps/razorsharp-concept/README.md)
for the front end. [`docs/DEPLOY.md`](docs/DEPLOY.md) is **stale**: it describes deploying
`buyer-web` and `merchant-console`, two apps that no longer exist, and names the current
front end nowhere.

## Verification

```sh
make gate          # lint, then types, then tests -- the order that fails fastest
```

That is the **Python** gate. The front end's own checks are separate: `npx tsc --noEmit`,
`npm run lint` and `node --test tests/` inside `apps/razorsharp-concept`.

CI runs formatting, lint, strict Python types, per-package backend tests, frontend
TypeScript checks, frontend regressions and the production build. Current test counts
come from the run artifacts rather than a manually maintained number here.

Database-backed tests exercise restricted roles, RLS, single-winner admission and
single-use grants. Adversarial tests cover changed approvals, replay, revocation and
provider-failure recovery. Live provider and microphone checks remain separate from
these deterministic suites.

Markers keep the default run honest rather than convenient:

- `db` — needs PostgreSQL with migrations and roles. CI **fails if these skip**, because
  RLS, single-winner admission and grant-spend-once are proven only in database-backed
  suites, and a green run that proved none of them is worse than a red one.
- `voice_live` — drives real speech through Gemini and the running API. Excluded from the
  default run; a separate credentialled check, never a silent offline pass.

Two things that number hides, said here rather than discovered later:

- **The 21 skips are not environmental.** They are the voice wire-contract tests, and they
  skip with *"the storefront is not present in this checkout"* — they read
  `apps/buyer-web`, a front end that was deleted. That guard held the server's frame
  contract against the client's; against the current front end it holds nothing.
- **`razorpay_live` is declared and unused.** Zero tests carry it. The marker exists in
  `pyproject.toml` and in the Makefile's exclusion flag, and nothing is behind it.

## Decisions

Five ADRs in [`docs/adr/`](docs/adr/) record why things are the way they are, and
[`docs/KNOWN_GAPS.md`](docs/KNOWN_GAPS.md) records what is not built. Documents that asserted
a *current state* were deliberately deleted; the ones that record a *decision* stayed.

This repository is the development and test-mode implementation.


#### Hybrid speech rendering

The voice gateway uses Sulafat on both GCP providers. Chirp 3 HD renders quick replies,
cart/action facts, numeric statements and deterministic checkout guidance. Gemini Live
renders longer non-numeric advice with comparison/recommendation cues, after the existing
speech grounding checks. Ambiguous replies default to Chirp. The decision is made once
per reply, never mid-sentence. Both paths share the same PCM playback and interruption
state machine. Gemini failures before the first audio fall back to Chirp; partial audio
is never replayed. Chirp failures on financial speech do not fall back to generative audio.
Native audio remains probabilistic; matching voice names do not guarantee identical prosody.
