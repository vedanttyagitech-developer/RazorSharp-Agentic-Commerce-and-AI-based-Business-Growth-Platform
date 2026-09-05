# Submission

**Razorpay AI Buildathon, Track 1.** An independent project proposal. Not an official
Razorpay, Blinkit, Zepto, Google, OpenAI or NPCI product.

Everything below was measured on 2026-09-05 by running the command printed beside it
against the live stack. Nothing is carried forward from another document. Where the system
declines to state a figure, this page declines to state it too — and section 4 is where
those refusals are collected, because a submission that names its own limits is worth more
than one that does not.

---

## 1. What it is

A multi-tenant agentic commerce platform that makes a quick-commerce merchant discoverable
and transactable by an AI buyer, and that will not let the AI buyer move money.

The whole architecture exists to enforce one sentence:

> **Agents propose; deterministic systems authorize and execute.**

A conversational agent can search the catalogue, read a basket, build a checkout and
explain an order. It cannot approve one and it cannot pay for one. Every money action
passes through a Transaction Assurance Kernel that versions the checkout, binds a canonical
hash to an explicit human approval, fixes the terms in a Policy-at-Sale Receipt, admits
exactly one execution under a single-use Execution Grant, and records the whole thing in a
tamper-evident hash chain.

When the merchant's state changes underneath an approved checkout, the kernel refuses the
stale approval and requires a fresh one on version N+1.

**That refusal, not the shopping, is the product.**

### The storefront is a clone, deliberately

The buyer application is a pixel-faithful clone of Blinkit, built from the live site's own
computed styles rather than eyeballed from a screenshot; the measurements are in
[`BLINKIT_DESIGN_SPEC.md`](BLINKIT_DESIGN_SPEC.md). We are saying so here rather than
letting a judge wonder.

The reason is not decoration. A governed checkout is an unfamiliar idea, and the fastest
way to make an unfamiliar idea legible is to put it inside a shell nobody has to think
about. A viewer who already knows how this storefront works has attention left over for the
one screen where it behaves differently from every other storefront they have used. The
clone is the control variable.

What is ours, and must not be mistaken for Blinkit: RazorAI, the buyer copilot; the trusted
surface where approval and payment happen, drawn deliberately unlike anything the agent
renders; and the refusal screen. Product names and imagery are used for local evaluation
and interface realism only.

---

## 2. What is proven, and how to check it in under a minute

Mint a session first. Every check below uses it.

```bash
export API=http://127.0.0.1:8000
export SK=local-demo-scenario-key
export TOKEN=$(curl -sS -X POST $API/v1/demo/sessions -H 'Content-Type: application/json' \
  -d '{"tenant_slug":"demo","actor_type":"BUYER"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
export AUTH="Authorization: Bearer $TOKEN"
```

### 2.1 The refusal, end to end

**Claim.** A buyer approves ₹579.95. The merchant raises a price. The buyer presses Pay.
The kernel answers HTTP 200 carrying `allowed: false`, names the field that moved, retires
version 1 permanently, and offers version 2 at ₹681.95.

**Check it in one command.** The whole sequence, reproduced from a reset catalogue:

```bash
node scripts/capture_screenshots.mjs
```

It prints the refusal it produced and writes
[`docs/images/capture-manifest.json`](images/capture-manifest.json). The run behind this
page recorded:

```json
"refusal": {
  "checkout_state": "APPROVAL_REQUIRED",
  "current_version": 2,
  "version_1_state": "INVALIDATED",
  "version_1_amount_minor": 57995,
  "version_2_amount_minor": 68195,
  "delta_minor": 10200
}
```

The arithmetic is checkable by hand: two milks at ₹28.00 became two milks at ₹79.00, so
2 × ₹51.00 = ₹102.00, and ₹579.95 + ₹102.00 = ₹681.95. The hero screenshot is
[`06_the_refusal.png`](images/06_the_refusal.png).

**Three details worth more than the headline.**

The refusal is **HTTP 200**. A denial is the platform working correctly, and a 4xx would
tell every client, retry queue and gateway in the chain to try it again as a fault
(ADR 0003 D15).

The response carries `"grant_id": null` and `"payment_attempt_id": null`. Nothing was
created. There is no Razorpay order for version 1 and there never was.

Version 2 is built inside the same transaction as the refusal, with its own Policy-at-Sale
Receipt and its own reservation, so a crash at that instant cannot leave a checkout that
can never be approved.

### 2.2 The agent cannot approve, by construction

**Claim.** Approval is not a permission the agent is denied. It is a capability that was
never issued to it.

```bash
curl -sS $API/v1/agent/capabilities -H "$AUTH" | python3 -m json.tool
```

The response lists what the agent holds, and then a second list:

```json
"absent_by_construction": ["authority.revoke", "checkout.approve", "checkout.reject"]
```

Each specialist has its own principal — `session:<id>/razorai/shopping`,
`…/razorai/checkout`, `…/razorai/support` — and its own tool set. The shopping specialist
holds `basket.write` and `catalogue.read` and nothing else.

### 2.3 Razorpay test mode, for real

**Claim.** The durable worker has created real orders against `api.razorpay.com`, each
under a single-use grant it consumed *before* the network call.

```bash
psql -d commerce_dev -c "
  select p.provider_id, p.http_status, g.status, (p.request_at - g.consumed_at) as network_after_consume
  from provider_requests p join execution_grants g on g.id = p.grant_id
  where p.url like '%/v1/orders' order by p.request_at desc limit 5"
```

At the time of measurement: **23 order-creation calls, 23 answered HTTP 200, 23 distinct
Razorpay order ids.** The count grows with every demonstration run; what does not change is
that the three numbers are equal. The most recent, from the run that produced this page's
screenshots, is `order_TYDpj0qxDLfmXI` for ₹681.95 under key `rzp_test_TXihheLv4wQW1S`.

Four things in the rows behind that, each a claim rather than a detail:

**Exactly once, provably.** Every `CONSUMED` grant matches exactly one provider request —
no grant with two, no request without one.

```bash
psql -d commerce_dev -At -c "
  select count(*) from (
    select g.id from execution_grants g
    left join provider_requests p on p.grant_id = g.id
    where g.status = 'CONSUMED' group by g.id having count(p.id) <> 1) x"
```

Answer: `0`.

**Consumed before the call, never after.** `request_at − consumed_at` is positive on every
row, between ten milliseconds and three and a half seconds. The grant is spent inside
the committed transaction before the HTTP request goes out, so a crash between the two
loses the money action rather than repeating it. In a payments system, a lost action is
recoverable and a repeated one is not.

**Stale authority is refused, not revived.** Two outbox commands are `DEAD`. Their grants
read `EXPIRED` and `REVOKED`, neither was consumed, and **neither produced a network call**.
The worker declined to spend authority that was no longer live. A dead letter here is the
system being careful.

```bash
psql -d commerce_dev -c "
  select o.status, g.status as grant_status, g.consumed_at,
         (select count(*) from provider_requests p where p.grant_id = g.id) as network_calls
  from outbox_events o left join execution_grants g on g.outbox_command_id = o.id
  where o.status = 'DEAD'"
```

**No credential is ever recorded.** `provider_requests.header_names` reads
`["Accept", "Content-Type"]`. The table keeps the method, the URL, the status, the
provider's identifier and a body digest, and never a secret.

### 2.4 Only the worker talks to the provider

**Claim.** The API process holds no HTTP client for Razorpay at all (ADR 0003). It writes
one command to a durable outbox in the same transaction as the kernel's decision; a
separate worker leases it, spends the grant, and makes the call.

This is why the payment handoff can legitimately be read before the provider order exists
and simply reports `state: CREATED`. Watch the command move:

```bash
curl -sS "$API/v1/ops/outbox" -H "$AUTH" -H "X-Scenario-Key: $SK" | python3 -m json.tool | head -20
```

`PAYMENT_CREATE_ORDER`, `PENDING` → `LEASED` → `DONE`, with `attempts` counting.

### 2.5 A browser is not evidence of payment

**Claim.** The browser's return from Razorpay is recorded as an unverified claim and never
as capture evidence.

`POST /v1/payments/verify` checks the callback's `HMAC(order_id|payment_id)`, records it as
`BROWSER_CALLBACK`, and enqueues a reconciliation. Capture is applied only from Razorpay's
own signed webhook or from the platform fetching the payment directly, through a monotonic
apply, so `CAPTURED` can never regress and a replayed browser return cannot move money
(ADR 0003 D8). The storefront says this on screen, in the payment panel, in those words.

### 2.6 The Money Action Proof Chain

**Claim.** One document links the whole transaction, and it is fifteen independent checks
rather than one green tick.

```bash
curl -sS "$API/v1/checkouts/<checkout_id>/proof" -H "$AUTH" -H "X-Scenario-Key: $SK" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d["verdict"]["checks"]))'
```

The checks are: `content_hash_recomputed`, `receipt_bound_to_version`,
`approval_binds_content`, `decision_names_version`, `grant_binds_decision`,
`grant_consumed_once`, `command_carries_grant`, `every_mutation_consumed_a_grant`,
`capture_evidence_is_verified`, `amounts_agree`, `tenant_and_merchant_correlate`,
`evidence_in_order`, `final_state_consistent`, `audit_chain_checkout` and
`audit_chain_payment_attempt`.

On the demonstration checkout, `capture_evidence_is_verified` returns **n/a** — no order
has been confirmed. A verifier that answers *not applicable* where it cannot check is worth
more than one that answers green everywhere. It is rendered in the console at
[`09b_console_proof_chain.png`](images/09b_console_proof_chain.png).

### 2.7 The audit is hash-chained and verifiable

```bash
curl -sS "$API/v1/audit/streams/checkout/<checkout_id>/verify" -H "$AUTH" -H "X-Scenario-Key: $SK"
```

The demonstration checkout returns `intact: true`, `length: 12`, `events_verified: 12` and
a head hash. Editing one field, deleting one row or swapping two breaks every link after
it. The scenario injection appears in the stream labelled `SCENARIO_INJECTION` — staged
data is marked as staged, never mixed with organic data.

### 2.8 Tenant and buyer isolation

A checkout belongs to the session that created it. An operator holding the scenario key
still cannot read another buyer's checkout:

```bash
curl -sS "$API/v1/checkouts/<someone-elses-id>" -H "$AUTH" -H "X-Scenario-Key: $SK"
# 404 — "No checkout with that identifier belongs to this session."
```

Underneath, isolation is forced row-level security in PostgreSQL, and the test suite that
proves it connects as a `NOSUPERUSER NOBYPASSRLS` role and asserts both flags before
running — because the same suite run as the database owner would pass while proving
nothing.

### 2.9 Neither application ships a fixture

**Claim.** Every figure on every screen was read from the API during that page load. An
unreachable API renders the failure rather than a plausible number.

Stop the API and reload either app. The storefront says the store is not reachable. The
console renders the problem document where a number would be. There is no mock mode and its
absence is deliberate: a storefront that falls back to invented data shows a buyer a price
no kernel ever agreed to, which on a payments submission is worse than an honest error,
because every real figure beside it becomes unverifiable.

### 2.10 The suite

```bash
uv run --no-sync python -m pytest packages -o addopts="" -q
```

**3,334 passed in 62.84s.**

| Package | Tests | Source |
| --- | ---: | ---: |
| `transaction-kernel` | 1,730 | 13,527 lines |
| `agent-runtime` | 492 | 9,414 lines |
| `payment-adapters` | 298 | 3,550 lines |
| `platform-db` | 232 | 1,496 lines |
| `merchant-sim` | 167 | 5,537 lines |
| `commerce-api` | 157 | 14,029 lines |
| `durable-work` | 130 | 1,538 lines |
| `commerce-domain` | 65 | 407 lines |
| `durable-worker` | 63 | 3,180 lines |

`uv run --no-sync mypy packages/*/src` → no issues in 148 source files.
`uv run --no-sync ruff check packages apps` → all checks passed.
`npm test` in `apps/buyer-web` → 135 tests in 6 files.
`npm test` in `apps/merchant-console` → 50 tests in 4 files.

The API serves 41 routes. The catalogue holds 247 products across 10 categories, with 319
local WebP images and no external image origin.

---

## 3. What is deliberately not built, and why

A submission that names its own limits is worth more than one that does not. These are
choices, not oversights, and each has a reason.

### Autonomous Reserve Pay is held in Safe Mode

Specification section 14 describes delegated, human-absent payment under a mandate. The
kernel's authority model supports it: delegated authorities, bounded scopes, revocation
epochs and a Safe Mode kill switch are all built and tested.

It is switched off on purpose.

Shipping a machine that pays without a human present, and calling it safe, is the exact
claim this project argues you should not make on the strength of an architecture diagram.
The controls that would make it defensible — the mandate cryptography, the revocation path
under live load, the reconciliation of an action nobody watched — are not proven yet.
`GET /v1/ops/safe-mode` answers with what the kernel actually permits rather than what a
configuration file asserts, and the console shows it. When Reserve Pay is proven, that
switch is where it turns on.

### Reconciliation and resolution are bounded, not autonomous

The bounded-attempt policy is fixed in ADR 0003 D13: six attempts, exponential backoff,
then `ESCALATED`. A payment whose outcome is genuinely unknown escalates to a human queue
rather than retrying forever or guessing. The queue and its evidence exist; a full operator
action interface does not, and this page does not imply live human operations.

### Deployment is validated offline and has never been applied

`infra/` holds Terraform, a Kustomize base with dev and demo overlays, and three
Dockerfiles. `./scripts/validate_infra.sh` passes: `terraform fmt` and offline validate,
`kubeconform` strict with zero skipped schemas, all three images build. The script itself
reports the pending operations. **Nothing has touched a real GCP project.** Region-aware by
design; single region deployed is not a claim we can make, because none is.

### One API process

ADR 0003 D14: the merchant simulator's state lives in the API process, so settings refuse
`WEB_CONCURRENCY > 1`. This is a demonstration restriction and it is stated rather than
hidden. A real merchant adapter reads a real catalogue and the restriction goes away.

### JCS canonicalizes integers only

RFC 8785's hardest requirement is ECMAScript number serialization. This domain has no
non-integer numbers — money is integer minor units, and quantities, versions and epoch
timestamps are integers. A float reaching canonicalization is a bug, most likely money that
escaped the `Money` type, so it raises rather than rounds. Enabling floats means
implementing ECMAScript number serialization and proving it against the official RFC 8785
vectors first, and we have not done that.

---

## 4. The honest boundaries

### No end-to-end capture has been demonstrated

The platform has created **thirty** real Razorpay test-mode orders under single-use grants,
every one answering HTTP 200. It has not carried one through to a captured payment, because
capture needs a card typed into Razorpay's own hosted page and no unattended run can do
that. Every screen that would show a captured amount for the demonstration checkout shows a
dash instead.

Eight order rows do exist on the demonstration tenant, and they are worth naming rather than
letting a judge find them and wonder. **All eight are seeded**, and every one carries a
`capture_evidence.event_id` beginning `evt_seed_` and a payment id Razorpay has never issued
— development fixtures, not captures:

```sql
select count(*) total,
       count(*) filter (where capture_evidence->>'event_id' like 'evt_seed_%') seeded
from orders;   --  8 | 8
```

`capture_screenshots.mjs` refuses to photograph them: the order shot is taken only for the
run's own checkout, and evidence whose event id begins `evt_seed_` is rejected even then.

The check that settles it is the inbox. `webhook_inbox` holds **2** rows, and neither is a
capture: both come from the webhook forgery test — one forged `evt_attack_*` and one
`evt_genuine_*` — and both resolved `apply_status = IGNORED`, `apply_reason =
attempt_not_found`. No webhook has ever applied capture evidence in this system, so no
`"channel": "VERIFIED_WEBHOOK"` in it came from one. `docs/KNOWN_GAPS.md` tracks removing
the rows.

The retained-revenue endpoint is the clearest example. It reports:

```json
"stale_approved_minor": 57995,
"corrected_total_minor": 68195,
"captured_minor": null,
"difference_minor": null,
"direction": "UNSETTLED",
"controlled_scenario": true,
"explanation": "Version 1's approval was invalidated, but no capture has been verified yet, so no difference can be stated."
```

It could print ₹102.00 and call it revenue retained. It does not, because that is not what
`difference_minor` means: that field is captured minus approved, and nothing has been
captured. The behaviour under a real capture is proven by 52 payment tests and 59 capture-
evidence tests in the kernel, against fixtures, not against a live captured order.

`controlled_scenario: true` is on every response. This is a reproducible scenario, not a
production revenue-lift claim.

### The protocol layer and voice, re-measured

An earlier version of this page reported all five of these as having no source at all. That
was true when written and is no longer true, and leaving it would have understated the
repository to anyone checking. Re-measured 2026-09-05:

| Layer | Measured | Command |
| --- | --- | --- |
| UCP 2026-08-25 | 6 source modules | `ls packages/commerce-protocols/src/commerce_protocols/ucp/*.py` |
| ACP 2026-04-17 | 5 source modules | same, `acp/` |
| MCP | 5 source modules | same, `mcp/` |
| AP2 v0.2 | 7 source modules; 5 packages import `jwcrypto` | same, `ap2/`; `grep -rl jwcrypto packages/*/src` → 5 |
| `commerce-protocols` suite | 367 passing | `pytest packages/commerce-protocols` |
| Realtime voice | 36 source modules, 233 passing, 6 skipped | `pytest packages/voice-runtime`; the skips need `GOOGLE_CLOUD_PROJECT` |

Two things this table does **not** claim. ACP and MCP are complete, tested libraries that
are **not mounted over HTTP** — neither is reachable by an external client, and
`docs/KNOWN_GAPS.md` records what mounting them needs. And AP2 and UCP sign with keys this
deployment is configured with, not keys it generates: a signature made before a restart
still verifies after one, demonstrated end to end on 2026-09-05. A deployment given no key
publishes no profile at all — the well-known documents answer 404 — rather than minting one
that stops verifying its own past evidence when the process exits.

The rule that produced the original table still stands, in the direction that matters: what
must not happen is a protocol claimed here that is not in the repository. Read the
repository rather than this table if you are checking after the fact.

### The scenario is staged, and says so

The price change that triggers the refusal is injected through a scenario controller. Every
injection is written to the audit under the label `SCENARIO_INJECTION`, carries an explicit
before-and-after, and appears in the timeline flagged. A panel is entitled to ask whether we
arranged the failure. We did, visibly, on purpose — a refusal that cannot be reproduced on
demand cannot be demonstrated, and one that hides its staging cannot be trusted.

### One narrow, deliberate exception to the isolation claim

Twenty of the twenty-four database tables carry `FORCE ROW LEVEL SECURITY`. The four
without are `alembic_version`, `tenants`, `platform_operating_modes` and `api_sessions`, and
only the last is a judgement call worth stating in public. `api_sessions` has a
`tenant_id NOT NULL` column but no policy, because resolving a bearer token is how a request
*learns* its tenant, and that lookup must work before any tenant is bound. It is deliberate
and it is tested. The only thing standing between one tenant and another tenant's session
row is that the lookup key is an unguessable 64-character token hash under a unique
constraint. That is a real, narrow exception, and it belongs here rather than averaged into
"forced RLS everywhere".

### Nothing here is fulfilled, and no money is real

No order leaves a shelf. Every payment runs against Razorpay in test mode; a `rzp_live_`
key is refused outside an explicit production profile with a named approval. Prices, stock
and totals come from a simulated merchant catalogue.

---

## 5. Where to look

| You want | Open |
| --- | --- |
| To run it yourself, from a clean machine | [`DEMO.md`](DEMO.md) |
| What is verified, and by which test | [`STATUS.md`](STATUS.md) |
| The five-minute script | [`PITCH.md`](PITCH.md) |
| The shot list for the recording | [`STORYBOARD.md`](STORYBOARD.md) |
| The screenshots, and the figures behind them | [`images/`](images/), [`capture-manifest.json`](images/capture-manifest.json) |
| Why the service layer is shaped this way | [`adr/0003-service-layer.md`](adr/0003-service-layer.md) |
| Why the agent layer is shaped this way | [`adr/0004-agent-runtime-blueprint.md`](adr/0004-agent-runtime-blueprint.md) |
| The storefront's visual contract | [`BLINKIT_DESIGN_SPEC.md`](BLINKIT_DESIGN_SPEC.md) |
| The full specification | [`../PROJECT_SPECIFICATION.md`](../PROJECT_SPECIFICATION.md) |

## 6. The claim, stated once

Most agentic commerce demonstrations show a machine that can buy things.

This one shows a machine that cannot — until a person has agreed to the exact number it is
about to spend, and the merchant's world has not moved since they agreed.

The refusal is the product.
