# RazorSharp

### Agentic commerce for Razorpay — an AI can shop, and cannot spend your money incorrectly.

## ▸ Live — [razorsharp.vedanttyagi.tech](https://razorsharp.vedanttyagi.tech)

Nothing to install. Real Razorpay test mode, both copilots, voice.

| | |
|---|---|
| **Shopping copilot** | **<https://razorsharp.vedanttyagi.tech/shop>** |
| **Merchant Command** | **<https://razorsharp.vedanttyagi.tech/merchant>** |

**Razorpay AI Buildathon 2026 · Track 1 — AI Growth & Agentic Commerce · test mode only, no
live keys, no real money.**

---

## What this is, in ten lines

- **An agent proposes. A person approves. A kernel authorizes.** Three components, three
  capability sets, and the boundaries are enforced in code rather than by convention.
- **A model cannot pay — not because it is checked, but because the capability does not
  exist.** `checkout.approve` and `merchant.action.approve` appear in no registry at all.
- **The agent package cannot even name the kernel.** `transaction-kernel` is absent from its
  dependencies, and an AST walk fails the build if the import comes back.
- **A passing check does not call Razorpay — it mints a one-shot Execution Grant**, bound
  across nine fields, spent exactly once by a physically separate process.
- **The database is the boundary, not the linter.** A role without `UPDATE` on
  `payment_attempts` cannot be bypassed by importing a different module — and a test proves
  it by connecting as that role and being refused.
- **Revocation is an epoch, not a flag.** Revoking a spending permission voids every approval
  the agent is already holding, without writing to a single approval row.
- **An unknown payment outcome is never a failure.** Five HTTP statuses mean "definitely did
  not happen"; everything else becomes reconciliation, because telling you a payment failed
  when it may have succeeded is how somebody pays twice.
- **A browser callback is never capture.** An order row can only be written from a webhook or
  a server-side provider fetch, enforced by a type rather than a comment.
- **The assistant may not say a number the server did not prove**, to the paisa.
- **Any sale can be re-verified end to end, by anyone, over HTTP** — ten links, fifteen named
  checks, recomputed on every request.

**Scale:** 89 HTTP routes across nine surfaces · 14 Python packages, ~97k lines of source and
~83k of tests · a 16.5k-line front end across 100 components and three surfaces.

### 6,455 tests, all passing

Measured at this commit, not carried over — `make gate` and `node --test`, run just now.

| | |
|---|---|
| **Backend** (pytest, 187 files) | **6,261 passing** · 0 failing |
| **Front end** (`node --test`) | **194 passing** · 0 failing |
| **Total** | **6,455 passing** |
| Connecting to a **real PostgreSQL** as restricted roles | **1,822** |
| Adversarial / failure-scenario suites | 34 files |
| Types | mypy `--strict` clean across 278 source files · TypeScript strict clean |
| Lint | ruff clean · oxlint clean |

The number that matters more than the total is **1,822**. Row-level security, `FOR UPDATE`
lock ordering, a partial unique index and twelve real threads racing for one payment cannot be
proven with a mock — they need a real database, real roles and real constraints. **CI fails
the build when one of those tests *skips*, not only when it fails**, because a green run that
proved none of them is worse than a red one.

---

## The incident this was built around

A buyer's UPI attempt fails. They retry on card. The card captures. Forty seconds later the
failure webhook for attempt one arrives, carrying the *first* payment id, asking the platform
to call this order failed.

Most systems would. This one stores the event and declines to apply it —
`failed_report_names_other_payment`
([`payments.py:1372`](packages/transaction-kernel/src/transaction_kernel/payments.py)) —
because a failure naming a payment the attempt no longer holds is evidence about a dead
attempt, not about the buyer's money.

Deduplicating by event id would not have saved you: both webhooks are genuine, distinct, and
correctly ordered by the provider. You have to key it on payment identity.

That is the whole project in one story. An AI shops; a deterministic kernel decides what
happens to money; and the half worth your review is what the kernel **refuses**.

---

## What this is not

Before anything is claimed, four disclosures.

- **The merchant is a simulator.** A deterministic in-process catalogue of 247 products in 10
  categories, with its own pricing and stock. No network, no clock, no database. That
  determinism is what lets a quote stand as evidence — and it means no real shop has been
  integrated.
- **The Reserve Pay issuer is a simulator**, and the offline verifier says so itself. It
  prints `issuer_kind SIMULATOR`, `live_revocation NOT_CHECKED` and
  `bank_authorization NOT_ESTABLISHED` on the same screen as `signature VALID`, so the output
  cannot be screenshotted into a stronger claim than it makes.
- **Reserve Pay authorizations are tamper-evident, not third-party verifiable.** They are
  signed ES256 over RFC 8785 canonical bytes and verified by a worker process launched
  without the signing key — but the public key is still served by this platform, and a key
  obtained from the party under audit proves nothing on its own.
- **Razorpay is real, in test mode.** Real orders at `api.razorpay.com`, real signature
  verification, real webhooks. Live keys are refused at construction.

Where something is simulated, this file says so at the point the claim is made.

---

## Track 1 — AI Growth & Agentic Commerce

*"Grow the merchant's revenue, and make them sellable to AI buyers."*

Both halves are built, and **the kernel is what makes the growth half safe**: the same
component that blocks a stale execution is what enables recovery, substitution, reapproval,
payment retry and reorder. Growth is reported as **captured and retained revenue**, never as
orders created, because an order created and never paid is not revenue.

The track asks that every money action be explainable, bounded and gated, with the audit trail
shown and at least one failure handled cleanly.

| The bar | Where it is earned |
|---|---|
| **Explainable** | Every admission returns a named reason from a closed vocabulary, plus whose action caused it, whether money moved, and the exact next step |
| **Bounded** | Reserve Pay's per-purchase and total caps, product scope, and a revocation epoch re-checked in four separate places |
| **Gated** | `admit()` refuses to run outside a transaction and reads only rows it holds locks on; money-moving capabilities exist in no agent registry to be granted |
| **Audit trail, shown** | `GET /v1/checkouts/{id}/proof` — ten links, fifteen named checks, recomputed on every request rather than cached |
| **A failure, handled** | Five of them, each with its mechanism, in *Five ways this loses your money* below |

### Goal one — grow the merchant's revenue

Growth here is not a discount engine and not a nudge. **Every growth feature is a save**, and
each one is pointed at a specific moment where a real checkout loses money today.

| The moment revenue is normally lost | What happens instead | Where |
|---|---|---|
| The price moves while the buyer is deciding, and checkout **fails** | Version N is retired, N+1 is written with the **exact fields that moved**, and the buyer re-approves. A price change becomes a question, not a dead end. | `REAPPROVAL_REQUIRED`, three comparisons |
| The buyer **reloads** mid-payment and starts a second cart | The same purchase is offered back: same checkout, same attempt, same grant, same provider order. Never a duplicate, never an abandonment. | payment recovery |
| The backend blips and the page says **"failed"** | It says the outcome is unknown, keeps the reference on screen, claims neither success nor failure, and resumes when the API returns | `PAYMENT_UNKNOWN` → reconciliation |
| A worker dies and the order is **silently lost** | The command moves to `DEAD` with its payload and correlation id intact, and an operator can **revive** it deliberately | dead-letter revive |
| The buyer walks away mid-checkout and the cart is **orphaned** | Checkout continuity — the unfinished purchase is recoverable, not garbage-collected | `/shop` continuity |
| An item is out of stock and the sale **ends** | Substitution is a publishable policy family, bound into the sale's frozen terms | five policy families |
| Every repeat purchase needs the **same friction again** | Reserve Pay: a bounded, revocable licence to spend without asking again — capped per purchase and in total, scoped to products, revocable in one press | delegated authority |
| A buyer who cannot read English **cannot check out at all** | Grounded discovery and checkout in Hindi, Hinglish and English, by voice or text, with deterministic language detection — and a copilot that may not state a number the server did not prove | voice + copilot |

The claim the kernel makes possible is the unusual one: **the same component that blocks a
stale execution is what enables the save.** A system that cannot tell a stale approval from a
fresh one has only two options when the shop moves — charge the wrong amount, or drop the
sale. Knowing exactly which three fields changed is what lets it offer a third.

And growth is **measured, not asserted**: `/v1/merchants/{id}/evidence/retained-revenue`
returns a recovered sale you can point at, computed from real order rows. Reported as
**captured and retained revenue**, never as orders created — an order created and never paid
is not revenue, and counting it would be the kind of number this platform refuses to produce.

### Goal two — make the merchant sellable to AI buyers

Not a chat box bolted onto a storefront. A machine-readable catalogue behind **four protocol
surfaces** — MCP, ACP, UCP and AP2 — so an outside agent can discover, price and transact
without a human driving a browser.

- **Discovery** — UCP business and platform profiles at `/.well-known/ucp/`; MCP exposes 13
  tools behind OAuth protected-resource discovery and minted tokens.
- **Transaction** — ACP checkout sessions (create, read, complete, cancel), signature-verified
  over a length-prefixed canonical string, with replay protection as a database uniqueness
  guarantee rather than an in-memory set.
- **Authorization** — AP2's human-present mandate flow, against Google's own SDK.
- **The safety that makes it sellable at all** — exactly **one** MCP tool can reach kernel
  admission, and it still cannot move money by itself. **No tool can name an amount**:
  `ArgumentKind` has no monetary member, which is what makes `order.propose_cancellation` and
  `refund.propose` honest rather than merely named honestly.

A protocol surface does not get a shortcut: an ACP or MCP caller is admitted by the same
`admit()` path a browser is. Being open to AI buyers does not widen what anyone may spend.

And the merchant is not a spectator to it. **Merchant Command** gives the other side of the
counter its own copilot, a human-approval queue, publishable policy, and insights computed
from real order rows — so the person whose revenue this is can see and steer it.

---

## Four things to try on the live site

**1. [`/shop`](https://razorsharp.vedanttyagi.tech/shop) — say or type `doodh dhundo`.**
The copilot answers in the language you used — detected deterministically, no model decides —
and *proposes* a cart line. You press. The write is then checked against the world the
proposal was built in, and a stale one is refused `proposal_superseded` rather than executed
at a price you never saw.

**2. Approve a bill, then change the shop underneath it.** The kernel refuses the payment and
shows you the exact fields that moved. `REAPPROVAL_REQUIRED` runs three comparisons, not one —
a merchant who raises an item and drops the delivery fee by the same amount, leaving your
total byte-identical, is still caught. Version N is retired, N+1 is written.

**3. Reserve Pay → authorize → revoke.** Set a budget, authorize, ask the copilot to buy
something, then press **Revoke future spending**. Every approval the agent is still holding
dies at that instant, and the payment path cannot fail to notice. This is the one to try if
you only try one.

**4. [`/merchant`](https://razorsharp.vedanttyagi.tech/merchant) — Merchant Command.** Live
insights computed from real order rows, publishable policy families, the human-approval queue,
and a refund a merchant *may* approve. A merchant may approve a refund; a merchant may not
approve a buyer's checkout.

Look an order up by the number you were shown — `RS-260909-XW5G26M`, read case- and
hyphen-insensitively, because a person reading a number off a screen should not have to match
punctuation.

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

**A hash chain cannot answer the question people think it answers.** It proves nobody edited
or reordered a stream. It proves nothing about who was *entitled to append*, because the chain
recomputes over whatever is stored and a row appended at the tail hashes perfectly well. So
`APPEND_SCOPE` ([`platform_db/roles.py:71`](packages/platform-db/src/platform_db/roles.py)) is
a `RESTRICTIVE` INSERT policy scoping the worker to its own `aggregate_type`. The executor's
credential physically cannot author an admission row on a checkout stream.

**The worker may edit the lease but not the instruction.** A column-scoped `UPDATE` grant,
which is the difference between a process boundary and a containment boundary.

**The executor refuses to boot on a collapsed boundary**
([`action-executor/settings.py:182`](packages/action-executor/src/action_executor/settings.py)).
Point it at the kernel's credential and it will not start. The separation is not merely
documented; it is unconfigurable-wrong.

### Tenancy

Tenant isolation fails closed in the same spirit. The predicate is
`tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid` — an unbound connection
matches *nothing* rather than everything, because `set_config(..., NULL, true)` stores the
empty string and casting `''` to `uuid` would raise. **29 of 32 application tables have RLS
enabled and FORCED**, so the owning role cannot bypass it either. No `commerce_*` role is a
superuser or holds `BYPASSRLS`. The three exclusions are deliberate and each is asserted by a
test rather than assumed.

Safety is the default rather than the outcome of remembering: a test creates a throwaway table
inside a rolled-back transaction, runs the real generators over it, and asserts RLS came out
enabled and forced with privileges exactly `{'SELECT'}` — so a future table whose writers
nobody declared is read-only rather than open.

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

### Reserve Pay in full

A bounded, revocable licence to spend without asking again: per-purchase cap, total cap,
optional product scope, and an expiry. Only the buyer creates or revokes one — it is never an
agent tool, and a test asserts no such tool exists in the registry at all. Safe Mode closes
this path while leaving a human-present checkout open.

Capacity is defended three times — an application check, the `UPDATE`'s own `WHERE` clause,
and a `CHECK` constraint — and released at most once. An **unknown** outcome does not release
it: restoring capacity before the provider has confirmed the debit did not happen is how a
buyer gets charged twice.

Each authorization is signed ES256 over RFC 8785 canonical bytes, stored in an INSERT-only
table, welded to the authority by a composite foreign key, and re-verified at four call
sites — including by the executor, which runs with the signing key removed from its
environment.

---

## Five ways this loses your money, and what stops each

**1. The late failure webhook.** The opening story. `monotonic_apply` never regresses a payment
state, terminal states absorb late evidence, and a `failed` naming a payment the attempt no
longer holds is recorded and not applied.

**2. The double tap.** Exactly one payment can win, and the *database* says so — a partial
unique index, `uq_payment_attempts_one_non_terminal`, not an application check. Proven with
twelve real threads on twelve real sessions: one winner, eleven losers, one attempt row, one
grant, twelve audit events on an intact hash chain. The `IntegrityError` is identified by
SQLSTATE **plus constraint name**, never message text, so an unrelated violation is never
retried as though it were a race.

**3. The worker that dies mid-call.** Delivery is **at-least-once; the debit is at-most-once.**
A crashed pod's lease lapses and the command comes back — but the Execution Grant was consumed
and committed in its own transaction *before* Razorpay was called, so the redelivery finds it
`CONSUMED` and stops. The tempting single transaction is the one that double-charges: the
consumption would roll back along with the outcome. The trade is named in the code — this
order can leave a grant spent with no provider order, which is recoverable; the other can
leave a charge with no record, which is not.

**4. The ambiguous response.** Exactly five HTTP statuses — `{400, 401, 403, 404, 422}` — are
on the "this definitely did not happen" allowlist. Everything else becomes reconciliation.
`PAYMENT_UNKNOWN` is terminal and deliberately excluded from `RETRYABLE`, and there is no
replacement grant, ever: recovery from an uncertain outcome is reconciliation, not reissuance.

**5. The browser that claims success.** `POST /v1/payments/verify` verifies the HMAC, records
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

### What the kernel actually checks

`admit()` runs inside one transaction, against a fixed lock order exported as data, and trusts
nothing the caller passed it:

```
 4   can this actor submit this operation at all?
 5   Safe Mode, before anything delegated is accepted
 6   lock the checkout, confirm the version is current
6a   the buyer's own approval, re-read from the database
 7   reservation, against the database clock
8-10 re-read merchant truth, compare with what was approved
10+  authority, locked in the same transaction
12   exactly one winner
```

A refusal is a **business outcome, not an exception**: HTTP 200 with `allowed: false`, the
reason, whose action caused it, whether any money moved, and the exact next step. An error
status invites a client to retry a denial as though it were a fault.

---

## What the agents can and cannot name

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
this platform returns a sales rank, so *"selling fast"* is ungroundable by construction rather
than ungrounded by accident.

**Every identifier the model supplies is percent-encoded before it enters a URL path**,
because a SKU carrying `/`, `..`, `?` or `#` used to be spliced in raw.

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

---

## Voice

One WebSocket, live Gemini speech-to-text and synthesis. PCM16 mono in at 16 kHz, out at
24 kHz, in **100 ms frames cut on the audio thread** — the mic runs in an `AudioWorklet` so a
React render can never delay a frame, because a frame delayed by a render is a gap the
recognizer cannot distinguish from a pause. The worklet does drift-corrected resampling with
a fractional carry across blocks, and clamps before int16 scaling so an out-of-range sample
clips rather than wrapping into a click the recognizer hears as a consonant.

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

**Typed degradation frames** — when speech breaks, the wire names which part broke and typing
keeps working. Typed and spoken turns share one context, and the **latest intent wins**: ask
for milk, then type *"actually, bread"*, and the milk turn is dropped rather than answered
late.

**"Yes" out loud is heard, never recorded as consent.** The voice gateway mints no principal,
holds no capability, and makes exactly three outbound calls, all with your own bearer token.
It reads the approval card from the trusted server, speaks it from a template, matches your
next settled sentence against a closed lexicon, and *reports what it heard*. The approval
happens on the screen.

---

## The buyer surface

Grounded discovery over 247 products in 10 categories · a durable cart that survives a reload ·
**exact-bill approval** against a content hash · manual payment on Razorpay's own hosted
Checkout · order history and tracking · checkout continuity, so an unfinished purchase is
recoverable rather than orphaned · support cases raised against a real order · refunds you
request but never approve.

### Payment recovery

Reload the page mid-payment and the same purchase is offered back to you: **same checkout,
same payment attempt, same Execution Grant, same provider order** — never a new one.

Lose the backend entirely and the screen says the connection is interrupted and the outcome is
unknown. It claims neither success nor failure, keeps the payment reference on screen, and
resumes on its own when the API returns. A provider surface that loads blank has an explicit
**"Return to payment status"** escape that frees the page without starting a second payment and
leaves the order resumable.

**Why this is a growth feature and not a reliability one.** A payment interrupted at the
provider is, in most systems, an abandoned cart — the buyer sees an error, does not know
whether they were charged, and does not try again. The merchant loses the sale twice over:
once to the failure, once to the doubt.

Here the purchase is an object with a life, not a request that succeeded or didn't. The
checkout, the attempt, the grant and the provider order are all still addressable, so the buyer
returns to *the same purchase* rather than starting a second one. Three things follow, and each
is revenue:

- **No duplicate charge to refund.** A second attempt means a second order at the provider, and
  a refund, and a support case. Resuming the same attempt means none of those exist.
- **No abandonment from doubt.** The screen never tells a buyer their payment failed when it
  may have succeeded, because `PAYMENT_UNKNOWN` is a real state and reconciliation settles it.
  A buyer who is told the truth waits; a buyer who is told "failed" leaves.
- **No silent loss on the merchant's side.** A command whose worker died moves to `DEAD` with
  its payload intact and is revivable. The sale is recoverable by a person rather than gone.

---

## Merchant Command — the growth half

[**razorsharp.vedanttyagi.tech/merchant**](https://razorsharp.vedanttyagi.tech/merchant)

The merchant side is not a read-only dashboard. It is the other counter: its own copilot, its
own approval queue, its own policy surface, and insights computed from the same order rows the
buyer side writes.

### What a merchant can actually do

**Propose → submit → approve → execute.** Seven action kinds move through twelve states at
`/v1/merchant/actions/...`, and approval is bound to **the hash of the change the approver
actually read** — so a change edited between being shown and being approved cannot be executed
on the strength of the earlier look.

**Five publishable policy families** — cancellation, refund, return, substitution, fulfilment.
These are not settings; they are documents that get **frozen into a sale at approval time** as
a Policy-at-Sale Receipt. A merchant who changes a refund window tomorrow does not
retroactively change the terms of a sale made today, and admission re-derives the hash from the
stored document every time to prove it.

**A refund a merchant may approve** — and a buyer's checkout they may not. The asymmetry is
enforced in the same registry that keeps the models out: `merchant.action.approve` exists in no
agent registry, exactly as `checkout.approve` does not. Both absences are the same claim.

**A human-approval queue** for what a machine should not decide alone, and a reconciliation
queue for outcomes nobody can be certain of yet.

### Insights a merchant can act on, and none they cannot trust

Computed from real order rows over a 1–90 day window — never generated by a model. Every
response carries its own definition, verbatim:

> Confirmed order value before refunds; not net revenue, profit, or campaign-attributed growth.
> Currencies are never combined.

And the copilot standing beside those numbers **may not state a figure the platform declines to
compute**. It once said *revenue is up 18.4%* next to a panel of real money; the growth
percentage was deleted rather than sourced, every remaining figure was bound to the API, and
the guard was written as a negative test — *no reply may contain a percentage at all*. A
merchant copilot that answers "I don't have that" is worth more than one that answers
confidently and wrongly, because a merchant will act on what it says.

### Growth, reported as something you can point at

**Retained-revenue evidence** per merchant at `/v1/merchants/{id}/evidence/retained-revenue` —
a recovered sale with the identifiers that prove it, not a claimed uplift.

The distinction this platform holds to: **captured and retained revenue, never orders created.**
An order created and never paid is not revenue, and a system that counts it is flattering its
own operator. Value waiting on a human approval is reported on its own line and never folded
into revenue — counting it would claim income nobody collected, and hiding it would score a
gate that did its job as a lost sale.

---

## Evidence and operations

**Evidence** — the ten-link proof chain with its fifteen named checks, including
`content_hash_recomputed`, `grant_consumed_once`, `every_mutation_consumed_a_grant`,
`capture_evidence_is_verified` and `amounts_agree` · a checkout timeline and raw event stream ·
hash-chain verification for any audit stream (`/v1/audit/streams/{type}/{id}/verify`) · a
payment-attempt inspector and a protocol-interaction inspector for forensics · a payment
acknowledgement a buyer can keep, which refuses to render without verified capture evidence.

**Operations** — metrics · outbox visibility · **dead-letter revive**, so a command whose grant
expired can be brought back deliberately rather than lost · Safe Mode read and write · a human
**review queue** and a **reconciliation queue** for outcomes a machine should not decide alone.

Outbox exhaustion is a state, not a deletion: a command out of attempts moves to `DEAD`, keeps
its payload and correlation id, and stays queryable, alertable and revivable. Dropping the
message instead would convert a visible incident into an invisible one.

**Demonstrating failure, on purpose** — a scenario surface arms real faults: provider timeouts,
stale captures, transport errors, duplicate submits, expired reservations, webhook replay.
Faults disarm themselves in the same update that consumes them, so a demo cannot leave one
armed.

---

## Protocols — four, not one

Roughly 8,700 lines across four protocol surfaces, each with its own declared conformance
boundary.

| | What is implemented | Live routes |
|---|---|---|
| **UCP** | Business and platform profiles at `/.well-known/ucp/` | 2 |
| **AP2** | Human-present mandate flow, v0.2 | via checkout |
| **ACP** | Checkout sessions: create, read, complete, cancel | 4 |
| **MCP** | 13 tools behind OAuth protected-resource discovery and minted tokens | 4 |

**Exactly one MCP tool can reach kernel admission**, and it still cannot move money by itself.
**No tool can name an amount**: `ArgumentKind` has no monetary member, which is what makes
`order.propose_cancellation` and `refund.propose` honest. ACP is signature-verified over a
length-prefixed canonical string, with replay protection as a database uniqueness guarantee
rather than an in-memory set.

A **Protocol Inspector** (`/v1/inspector/protocols/{id}`) and a conformance endpoint
(`/v1/protocols/conformance`) let a reviewer read what each surface actually claims. Each
protocol declares a `ClaimBoundary` — `LOCAL_CONFORMANCE`, `COMPATIBLE_INTERFACE` or
`PUBLIC_INFORMATION_ALIGNMENT` — stating how far the claim goes instead of asserting
compliance. ACP compatibility is **not** ChatGPT availability, and the code says so.

Protocol surfaces do not bypass the kernel: an ACP or MCP caller is admitted by the same
`admit()` path a browser is.

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

## Layout

```
packages/                14 Python packages
  transaction-kernel       admission, grants, authority, refunds, audit
  commerce-api             89 HTTP routes: buyer, merchant, operator, protocol
  action-executor          the only component that calls the payment provider
  durable-work             outbox, leasing (FOR UPDATE SKIP LOCKED), fencing tokens
  agent-runtime            specialists, capability gate, grounding ledger
  voice-runtime            gateway, STT/TTS, degradation frames, consent lexicon
  commerce-protocols       MCP, ACP, UCP, AP2
  platform-db              schema, RLS, roles
  merchant-sim             catalogue, pricing, stock
  merchant-adapter         the connector boundary
  merchant-controller      merchant actions and policy
  payment-adapters         the Razorpay adapter
  commerce-domain          the vocabulary everything else hashes and compares
  platform-observability   timing and instruments

apps/razorsharp-concept  the front end: 100 components, 25 lib modules, 16.5k lines
infra/gce/               the deployment that actually runs
docs/                    ADRs, threat model, security review, failure scenarios
```

---

## Running it

The live site needs nothing installed. Locally, four processes — the API on **8000**, the voice
gateway on **8100**, the **Action Executor**, and the front end on **3000**:

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

One `e2-standard-2` in `asia-south1` running six containers under Compose — Postgres, the API,
the Action Executor, the voice gateway, the front end, and Caddy terminating TLS — behind
Cloudflare. The Cloudflare proxy is **on**: it passed the HTTP-01 challenge through to the
origin, so you get edge protection and end-to-end TLS with Caddy's own certificate on the
origin leg. Roughly $15 for a judging week.

---

## Verification

```sh
make gate          # lint, then types, then tests -- the order that fails fastest
```

**6,455 automated tests, all passing.** Both halves measured at this commit.

| | |
|---|---|
| Backend (pytest, 187 files) | **6,261 passing** · 0 failing · 0 skipped db suites |
| Front end (`node --test`) | **194 passing** · 0 failing |
| Needing a real PostgreSQL | **1,822** — RLS, locking and constraints cannot be faked |
| Adversarial / failure-scenario suites | 34 files |
| Types | mypy `--strict` clean across **278** source files; TypeScript strict clean |
| Lint | ruff clean across `packages/`, `scripts/`; oxlint clean |

Run `make gate` and hold this table to it. The backend half needs a PostgreSQL with migrations
and roles; `REQUIRE_DB=1` is what makes a missing one a failure instead of a quiet skip.

**CI fails the build when a `db`-marked test *skips*, not only when it fails.** Every central
claim in this file is provable only in a database-backed suite; a green run that proved none of
them is worse than a red one. The gate is narrowed deliberately to `db` and to CI, so an
over-broad rule does not get disabled by an annoyed team.

`voice_live` drives real speech through Gemini and the running API — excluded from the default
run, a separate credentialled check, never a silent offline pass.

### Verified live, not just unit-tested

Run against real Razorpay test-mode APIs, with the identifiers recorded:

- **Reload mid-payment → recover → complete.** Same checkout, same payment attempt, same
  Execution Grant, same provider order — one attempt row, one grant, one order.
- **Delayed confirmation.** With the Action Executor paused, a completed provider payment
  recorded `BROWSER_CALLBACK` and the attempt stayed `SUBMITTED` with **zero** order rows.
  Releasing the worker produced a real `PROVIDER_FETCH` capture and **exactly one** order.
- **Bounded reconciliation.** 34 empty provider status probes at 5.7× the reconciliation bound
  did not escalate a buyer who was still paying.
- **Backend outage mid-recovery.** The UI reported an interrupted connection and an unknown
  outcome, claimed neither success nor failure, and resumed on the same attempt.

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
  guard written as a negative test: **no reply may contain a percentage**. The percentage guard
  alone would have missed *"one low-stock product"* — no percent sign — so the count is
  separately asserted against the shelf it was given.
- **`e2c2db8` — "Two claims about the audit chain, one of them false and one unguarded."**
- **The WebAuthn passkey feature was retracted before the deadline**, not shipped. The buyer's
  key was signing a random challenge with the terms bound only by server-side association —
  ceremony-shaped, not proof-shaped. A feature that looks like cryptographic consent and is not
  is worse than no feature.

---

## Decisions

Architecture decision records live in [`docs/adr/`](docs/adr). ADR 0003 is the kernel: denial
as an answer, capability absence, commit-before-send, and why a browser callback is never
evidence.

Also in [`docs/`](docs): a threat model, an adversarial pass over the agent layer recording
what broke and what held, a security review run against the platform as it runs, and the
failure-scenario evidence index.

**MIT licensed.** Test mode only — no live keys, no real money.
