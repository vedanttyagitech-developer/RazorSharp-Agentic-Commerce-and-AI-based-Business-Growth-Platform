# RazorSharp

**Live: [razorsharp.vedanttyagi.tech](https://razorsharp.vedanttyagi.tech)** — both copilots,
voice, real Razorpay test mode. Nothing to install.

A buyer's UPI attempt fails. They retry on card. The card captures. Forty seconds later the
failure webhook for attempt one arrives, carrying the *first* payment id, asking the platform
to call this order failed.

Most systems would. This one stores the event and declines to apply it —
`failed_report_names_other_payment` ([`payments.py:1372`](packages/transaction-kernel/src/transaction_kernel/payments.py)) —
because a failure naming a payment the attempt no longer holds is evidence about a dead
attempt, not about the buyer's money. Deduplicating by event id would not have saved you:
both webhooks are genuine, distinct, and correctly ordered by the provider. You have to key
it on payment identity.

That is the project. An AI shops; a deterministic kernel decides what happens to money; and
the half worth your review is what the kernel **refuses**.

**Sixty seconds:** open Reserve Pay on the live site, set a budget, authorize, ask the copilot
to buy something, then press **Revoke future spending**. Every approval the agent is still
holding dies at that instant, and the payment path cannot fail to notice.

---

## What this is not

Before anything is claimed, four things are disclosed.

- **The merchant is a simulator.** A deterministic in-process catalogue of 247 products with
  its own pricing and stock. No network, no clock, no database. That determinism is what
  lets a quote stand as evidence — and it means no real shop has been integrated.
- **The Reserve Pay issuer is a simulator**, and the offline verifier says so itself. It
  prints `issuer_kind SIMULATOR`, `live_revocation NOT_CHECKED` and
  `bank_authorization NOT_ESTABLISHED` on the same screen as `signature VALID`, so the output
  cannot be screenshotted into a stronger claim than it makes.
- **Reserve Pay authorizations are tamper-evident, not third-party verifiable.** They are
  signed ES256 over RFC 8785 canonical bytes and verified by a worker process launched
  without the signing key — but the public key is still served by this platform, and a key
  you obtain from the party under audit proves nothing on its own. The honest claim is
  tamper-evidence against a database-plane compromise. Publishing the key at a `.well-known`
  path, as this repo already does for UCP, is what would close it. It is not done.
- **Razorpay is real, in test mode.** Real orders at `api.razorpay.com`, real signature
  verification, real webhooks. No live keys, and live keys are refused at construction.

Where something is simulated, this file says so at the point the claim is made.

---

## Four things to try on the live site

**1. `/shop` — say or type `doodh dhundo`.** The copilot answers in the language you used
(detected deterministically — no model decides) and *proposes* a cart line. You press. The
write is then checked against the world the proposal was built in, and a stale one is refused
`proposal_superseded` rather than executed at a price you never saw.

**2. Approve a bill, then change the shop underneath it.** The kernel refuses the payment and
shows you the exact fields that moved. `REAPPROVAL_REQUIRED` runs three comparisons, not one
— a merchant who raises an item and drops the delivery fee by the same amount, leaving your
total byte-identical, is still caught. Version N is retired, N+1 is written.

**3. Reserve Pay → authorize → revoke.** Described below. This is the one to try if you only
try one.

**4. Look an order up by the number you were shown** — `RS-260909-XW5G26M`, read case- and
hyphen-insensitively, because a person reading a number off a screen should not have to
match punctuation.

---

## When the process moving the money is the attacker

An import-boundary rule is a lint check a determined caller bypasses by importing a different
module. **A role without `UPDATE` on `payment_attempts` cannot be bypassed by importing
anything.**

That sentence is the architecture. Here is the proof rather than the policy:

[`packages/action-executor/tests/test_dwk_import_boundary.py:171`](packages/action-executor/tests/test_dwk_import_boundary.py)
opens a real connection **as the `commerce_worker` Postgres role**, runs
`UPDATE payment_attempts SET status = 'CAPTURED'`, and asserts `"permission denied"` comes
back. Not a mock. Not an assertion about a comment. A credential, a statement, and a refusal
from the database itself.

Five roles are declared; three carry privileges and are used at runtime:

| Role | May |
|---|---|
| `commerce_app` | tenant-scoped reads, non-financial writes |
| `commerce_kernel` | the only role that may write a financial table |
| `commerce_worker` | lease outbox rows — and **not** touch payments |

`commerce_migration` and `commerce_analytics` are declared and hold schema `USAGE` and not one
table privilege, deliberately.

Three consequences worth more than the role list:

- **A hash chain cannot answer the question people think it answers.** It proves nobody edited
  or reordered a stream. It proves nothing about who was *entitled to append*, because the
  chain recomputes over whatever is stored and a row appended at the tail hashes perfectly
  well. So `APPEND_SCOPE` ([`platform_db/roles.py:71`](packages/platform-db/src/platform_db/roles.py))
  is a `RESTRICTIVE` INSERT policy scoping the worker to its own `aggregate_type`. The
  executor's credential physically cannot author an admission row on a checkout stream.
- **The worker may edit the lease but not the instruction.** A column-scoped `UPDATE` grant,
  which is the difference between a process boundary and a containment boundary.
- **The executor refuses to boot on a collapsed boundary**
  ([`action-executor/settings.py:182`](packages/action-executor/src/action_executor/settings.py)).
  Point it at the kernel's credential and it will not start. The separation is not merely
  documented; it is unconfigurable-wrong.

Tenant isolation fails closed in the same spirit. The predicate is
`tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid` — an unbound connection
matches *nothing* rather than everything, because `set_config(..., NULL, true)` stores the
empty string and casting `''` to `uuid` would raise. **29 of 32 application tables have RLS
enabled and FORCED**, so the owning role cannot bypass it either. No `commerce_*` role is a
superuser or holds `BYPASSRLS`. The three exclusions are deliberate and each is asserted by a
test rather than assumed.

---

## Revocation, and why a `REVOKED` flag is not enough

A delegated authority carries one integer, `revocation_epoch`, starting at 0. An approval
records the epoch the authority stood at when consent was given. Revocation does exactly one
thing:

```sql
UPDATE delegated_authorities
   SET revocation_epoch = revocation_epoch + 1, status = 'REVOKED'
 WHERE id = :id AND revocation_epoch = :observed_epoch
RETURNING revocation_epoch
```

The arithmetic is Postgres's, against the locked row. **No public function in the module
accepts an epoch to store** — there is a test asserting that by signature. `revoke()` and
`check_authority()` deliberately take the *same* `SELECT ... FOR UPDATE`: one lock, one
linearization point.

The consequence is the point. **A revocation invalidates every approval the agent is already
holding, without writing to a single approval row.** The epoch is checked first — before
status, before expiry, before merchant, currency, product scope or capacity — and again in
four places: the approval binding at admission, `check_authority`'s own equality test, the
`WHERE` clause of the debit `UPDATE`, and once more by the executor immediately before it
calls the provider.

A flag says *this authority is revoked*. An epoch says *every consent that predates this
moment is void*, and that is the property a delegated authority actually needs.

Capacity is defended three times — an application check, the `UPDATE`'s own `WHERE` clause,
and a `CHECK` constraint — and released at most once. An **unknown** outcome does not release
it: restoring capacity before the provider has confirmed the debit did not happen is how a
buyer gets charged twice.

---

## Five ways this loses your money, and what stops each

**The late failure webhook.** The opening story. `monotonic_apply` never regresses a payment
state, terminal states absorb late evidence, and a `failed` naming a payment the attempt no
longer holds is recorded and not applied.

**The double tap.** Exactly one payment can win, and the *database* says so — a partial unique
index, `uq_payment_attempts_one_non_terminal`, not an application check. Proven with twelve
real threads on twelve real sessions: one winner, eleven losers, one attempt row, one grant,
twelve audit events on an intact hash chain. The `IntegrityError` is identified by SQLSTATE
**plus constraint name**, never message text, so an unrelated violation is never retried as
though it were a race.

**The worker that dies mid-call.** Delivery is **at-least-once; the debit is at-most-once.** A
crashed pod's lease lapses and the command comes back — but the Execution Grant was consumed
and committed in its own transaction *before* Razorpay was called, so the redelivery finds it
`CONSUMED` and stops. The tempting single transaction is the one that double-charges: the
consumption would roll back along with the outcome. The trade is named in the code — this
order can leave a grant spent with no provider order, which is recoverable; the other can
leave a charge with no record, which is not.

**The ambiguous response.** Exactly five HTTP statuses — `{400, 401, 403, 404, 422}` — are on
the "this definitely did not happen" allowlist. Everything else becomes reconciliation.
`PAYMENT_UNKNOWN` is terminal and deliberately excluded from `RETRYABLE`, and there is no
replacement grant, ever: recovery from an uncertain outcome is reconciliation, not reissuance.

**The browser that claims success.** `POST /v1/payments/verify` verifies the HMAC, records
`BROWSER_CALLBACK` evidence, queues reconciliation — and that is *all* it does. An order row
can only ever be written from a webhook or a server-side provider fetch, enforced by a type
rather than a convention: the response hard-codes
`evidence_kind: Literal["BROWSER_CALLBACK"]`.

---

## Every deadline is the database's clock

Payment authority is bounded twice, independently: the grant's own expiry — clamped to **120
seconds** on the payment path, defaulting to 300, with a 900-second ceiling — and the payment
attempt's own window. Both are computed and compared server-side with
`func.now() + make_interval(...)`.

Stated as a threat model rather than a style note: **a pod with a fast clock cannot mint
long-lived authority**, and a pod with a slow one cannot spend expired authority. Outbox
leases and stock reservations are evaluated the same way.

The grant binds nine fields, and the executor rebuilds that binding **from the outbox command
payload, not from the grant row**. There is deliberately no `from_grant` constructor: a
binding read back from the row it is meant to check would always match, and the verification
would be theatre.

---

## What the agents can and cannot name

An agent proposes. A person approves. A kernel authorizes. Three components, three capability
sets, and the boundary is enforced rather than described.

**The agent package cannot name the kernel.** `transaction-kernel` is absent from
[`packages/agent-runtime/pyproject.toml`](packages/agent-runtime/pyproject.toml), and two
tests hold the door shut: an AST walk that catches a kernel import at any depth — inside a
function body, under `TYPE_CHECKING`, behind a `try` — and a `tomllib` parse that catches the
dependency being re-declared. Both assert they actually looked at something first
(`len(OWNED) > 20`), because a glob that silently matched no files would report the door
locked while never having looked at it.

**The capability does not exist to be granted.** `checkout.approve` and
`merchant.action.approve` appear in **no registry at all**. The two absences are the same
claim, on both sides of the marketplace. Registry disjointness is asserted at **import time**,
not test time, and the reason is in the source: a package that cannot be imported cannot be
deployed, whereas a red test can be skipped under deadline pressure.

**The tool gate refuses by closure identity.** Hand-build a tool, name it exactly what the
factory names a real one, attach it — still refused, because the gate compares the callable
itself with `is`. A forged name is a string; object identity cannot be forged.

**The assistant may not say a number the server did not prove.** An amount is speakable only
if it appears, to the paisa, in the set of integer minor units that turn's tools actually
returned. The post-check reads the reply back and drops the sentence — by sentence, not by
word, because patching a number would leave the reasoning around it intact and now wrong. It
also removes demand and popularity claims outright, without consulting the ledger: no tool on
this platform returns a sales rank, so "selling fast" is ungroundable by construction rather
than ungrounded by accident.

**Both copilots, six internal agents.** Buyers see one Commerce Assistant; merchants see one
Merchant Copilot.

| Visible harness | Internal agents |
|---|---|
| Commerce Assistant | Coordinator · Discovery & Basket · Checkout & Order · Customer Support |
| Merchant Copilot | Coordinator · Merchant Operations |

Routing and language detection are deterministic. Gemini on Vertex AI; falls back to a
deterministic runner when Vertex is unconfigured, and says so rather than going quiet.

---

## Voice

One WebSocket, live Gemini speech-to-text and synthesis. PCM16 mono in at 16 kHz, out at
24 kHz, in **100 ms frames cut on the audio thread** — the mic runs in an `AudioWorklet` so a
React render can never delay a frame, because a frame delayed by a render is a gap the
recognizer cannot distinguish from a pause.

The echo gate is half-duplex and **substitutes digital silence rather than withholding
frames**: withholding starves the recognizer and times the stream out, so every gated frame is
replaced by silence of identical length and the cadence never changes. The 0.6 s tail is
released on the *client's* playback report, and the number was measured against the real
recognizer — nothing leaked at 500 ms, "please" leaked at 700 ms — with a unit test asserting
the relationship rather than trusting the comment.

Conversational reference is a closed deterministic grammar, not a model: 24 ordinal forms
across English, Devanagari and romanised Hinglish resolve *"pehla wala do packet jod do"* to
index 0, quantity 2 — and a bare *"pehla wala"* with no verb resolves to **nothing at all**,
because the grammar is full-match and refuses rather than guesses.

**"Yes" out loud is heard, never recorded as consent.** The voice gateway mints no principal,
holds no capability, and makes exactly three outbound calls, all with your own bearer token.
It reads the approval card from the trusted server, speaks it from a template, matches your
next settled sentence against a closed lexicon, and *reports what it heard*. The approval
happens on the screen.

---

## Everything else, briefly

**89 HTTP routes across nine surfaces**, 14 Python packages — roughly 97k lines of source and
83k of tests — and a 16.5k-line front end across 100 components on three surfaces.

**Payment recovery**, the part most demos skip. Reload mid-payment and the same purchase is
offered back: same checkout, same attempt, same grant, same provider order, never a new one.
Lose the backend entirely and the screen says the connection is interrupted and the outcome
is unknown — it claims neither success nor failure, keeps the reference on screen, and
resumes when the API returns.

**The merchant half.** Seven action kinds and twelve states through propose → submit →
approve → execute, with approval bound to the hash the approver actually read. Five
publishable policy families. Insights computed from real order rows, never generated by a
model. **Retained-revenue evidence** per merchant — a recovered sale you can point at rather
than a claimed uplift. A merchant may approve a refund; a merchant may not approve a buyer's
checkout.

**Four protocol surfaces**, each with its own declared conformance boundary.

| | What is implemented | Live routes |
|---|---|---|
| **UCP** | Business and platform profiles at `/.well-known/ucp/` | 2 |
| **AP2** | Human-present mandate flow, v0.2 | via checkout |
| **ACP** | Checkout sessions: create, read, complete, cancel | 4 |
| **MCP** | 13 tools behind OAuth protected-resource discovery | 4 |

Exactly one MCP tool can reach kernel admission, and it still cannot move money. **No tool can
name an amount** — `ArgumentKind` has no monetary member, which is what makes
`order.propose_cancellation` and `refund.propose` honest. Each protocol declares a
`ClaimBoundary` — `LOCAL_CONFORMANCE`, `COMPATIBLE_INTERFACE` or
`PUBLIC_INFORMATION_ALIGNMENT` — stating how far the claim goes instead of asserting
compliance. ACP compatibility is **not** ChatGPT availability, and the code says so.

**Any sale can be re-verified end to end, by anyone, over HTTP.**
`GET /v1/checkouts/{id}/proof` walks ten links and returns a verdict with **fifteen named
checks**, including `content_hash_recomputed`, `grant_consumed_once`,
`every_mutation_consumed_a_grant` and `capture_evidence_is_verified`.

**Demonstrating failure, on purpose.** A scenario surface arms real faults — provider
timeouts, stale captures, transport errors, duplicate submits, expired reservations, webhook
replay. Faults disarm themselves in the same update that consumes them, so a demo cannot
leave one armed.

---

## Real, or simulated

| | |
|---|---|
| **Razorpay payments** | **Real**, test mode. The Action Executor is the only component that mutates at the provider. |
| **Kernel, RLS, grants, audit** | Real. PostgreSQL, real roles, real constraints. |
| **Catalogue and merchant** | Simulated — deterministic, in-process, 247 products. |
| **Reserve Pay issuer** | Simulated, and the verifier prints so. |
| **Delivery, logistics** | Simulated. |

---

## Running it

The live site needs nothing installed. Locally, four processes — the API on **8000**, the
voice gateway on **8100**, the **Action Executor**, and the front end on **3000**:

```sh
scripts/run_demo.sh
```

The front end must be on port 3000; the voice gateway's origin allowlist defaults to exactly
that, so voice fails on any other port while everything else keeps working. Voice needs Google
Cloud credentials; without them the copilot still answers over HTTP and says so.

**With the Action Executor down, checkout stalls forever.** That is by design: the API's role
cannot write a financial table, so `razorpay_order_id` stays honestly `null` until the worker
spends the grant.

### Deployment

One `e2-standard-2` in `asia-south1` running six containers under Compose — Postgres, the
API, the Action Executor, the voice gateway, the front end, and Caddy terminating TLS —
behind Cloudflare. The Cloudflare proxy is **on**: it passed the HTTP-01 challenge through to
the origin, so you get edge protection and end-to-end TLS with Caddy's own certificate on the
origin leg.

Roughly $15 for a judging week. Two stopgap wildcard-DNS hostnames also answer, and neither
should be handed out: both are on filter lists several ad blockers ship by default, so the
page loads and every request it makes is refused `ERR_BLOCKED_BY_CLIENT` — which looks exactly
like a broken backend and is not one.

---

## Verification

```sh
make gate          # lint, then types, then tests -- the order that fails fastest
```

**6,406 automated tests.**

| | |
|---|---|
| Backend (pytest) | **6,212 passing** of 6,214 collected |
| Front end (`node --test`) | **194 passing** |
| Needing a real PostgreSQL | **1,820** — RLS, locking and constraints cannot be faked |
| Adversarial / failure-scenario files | 47 |
| Types | mypy `--strict` clean across **278** source files; TypeScript strict clean |

The two failing backend tests are a known cross-package `conftest` import collision in
`test_sales_assistance.py` — the file passes **15/15** run on its own. A test-harness defect,
not a product one, and it is written here rather than left to be discovered.

In the spirit of the rest of this file: the front-end figure was measured at this commit; the
backend figure is from the last full `make gate` run, which needs a PostgreSQL with migrations
and roles and so is not re-run for a documentation change. Run `make gate` and hold this table
to it.

**CI fails the build when a `db`-marked test *skips*, not only when it fails.** Every central
claim in this file is provable only in a database-backed suite; a green run that proved none
of them is worse than a red one. The gate is narrowed deliberately to `db` and to CI, so an
over-broad rule does not get disabled by an annoyed team.

`voice_live` drives real speech through Gemini and the running API — excluded from the default
run, a separate credentialled check, never a silent offline pass. `razorpay_live` is declared
and **unused**: zero tests carry it.

---

## A record of having been wrong

The most useful thing in a repository is the list of times it corrected itself, with hashes.

- **`ab9ea58` — "Eight claims the README made that turned out not to be true."** Including the
  worst kind: claiming the API physically could not create a Razorpay order because its role
  lacked the privilege, when the real reason was narrower.
- **`7b2c429` — "The merchant copilot was quoting a number the platform refuses to measure."**
  The copilot said *revenue is up 18.4%* beside a panel showing real confirmed order value.
  The backend declines to compute growth on purpose and says so in every insights response.
  The number was deleted rather than sourced, every remaining figure bound to the API, and the
  guard written as a negative test: **no reply may contain a percentage**. The percentage
  guard alone would have missed *"one low-stock product"* — no percent sign — so the count is
  separately asserted against the shelf it was given.
- **`e2c2db8` — "Two claims about the audit chain, one of them false and one unguarded."**
- **The WebAuthn passkey feature was retracted before the deadline**, not shipped. The buyer's
  key was signing a random challenge with the terms bound only by server-side association —
  ceremony-shaped, not proof-shaped. A feature that looks like cryptographic consent and is
  not is worse than no feature.

---

## Decisions

Architecture decision records live in [`docs/adr/`](docs/adr). ADR 0003 is the kernel: denial
as an answer, capability absence, commit-before-send, and why a browser callback is never
evidence. A refusal is a **business outcome, not an exception** — HTTP 200 with
`allowed: false`, the reason, whose action caused it, whether any money moved, and the exact
next step. An error status invites a client to retry a denial as though it were a fault.
