# Reserve Pay: current UI-to-backend contract

This implementation connects the concept frontend to the existing commerce API and
Action Executor. **The provider is simulated. No NPCI UAP, bank mandate, fund block or
real debit is performed.** Production API submission and simulator execution are disabled.
The separate manual Razorpay frontend remains a visual preview; its existing backend
payment and refund paths are retained.

## Ownership

- **Trusted buyer surface:** selects product scope, limit and validity; displays the
  server-generated bill; records the buyer's explicit choice of Reserve Pay. Composer
  text can select and confirm the reviewed Reserve bill. The current voice component
  remains a voice preview; real STT/TTS integration is not claimed by this change.
- **Commerce API:** authenticates the buyer and merchant, validates selected catalogue
  SKUs, persists permission through Kernel functions, composes approval/admission, and
  returns authoritative state. No new LLM capability is added.
- **Transaction Trust Kernel:** enforces exact approval-content/amount/action binding,
  Policy-at-Sale Receipt verification, reservation and merchant-state revalidation,
  selected-product/buyer/merchant authority scope, per-purchase and cumulative capacity,
  expiry, live revocation epoch, Safe Mode and single-use grants. It owns allocation
  settlement, using the persisted payment state rather than a UI boolean.
- **Action Executor:** consumes the exact command's grant; rechecks authority expiry and
  revocation before simulator acceptance; observes provider evidence and creates the
  order through the existing Kernel payment lifecycle. It never creates an order with
  a direct table insert.
- **Merchant Action Controller Command:** does not approve buyer authority or execute
  this financial path. Its published Merchant Policy changes remain subject to the
  existing checkout revalidation and Policy-at-Sale Receipt boundaries.

This is the user-present, AI-assisted checkout flow requested by the UI. It is not a
claim that an unattended model may manufacture buyer approval or spend outside the
selected products. No forged protocol authority proof is introduced.

## Persisted identities and bounds

`delegated_authorities` stores selected SKU IDs (`allowed_skus`) and
`per_purchase_limit_minor`, alongside the existing buyer, merchant, total capacity,
expiry, status and revocation epoch. New public setup always requires selected SKUs and
both limits. Legacy internal authorities retain nullable-bound compatibility.

Setup accepts 1–100 unique known SKUs, ₹1–₹5,000 per purchase, total capacity at least
the purchase limit, and 1–30 days validity (UI: seven days). Total capacity is an
**authorization ceiling**, not a wallet or evidence of bank funds. Multiple permissions
are independent; they do not replace or replenish one another silently.

`payment_attempts` records the admitted authority ID/epoch and allocation state:

| Allocation | Meaning |
| --- | --- |
| `HELD` | Admitted, queued, processing or uncertain; capacity is unavailable |
| `SPENT` | Capture or stale-capture confirmed; capacity remains consumed |
| `RELEASED` | Definitive failure; allocation restored exactly once |

A failed debit never reactivates a revoked authority. A refund does not automatically
replenish delegated spending capacity. Historical audit envelopes and hashes are unchanged.
New permission events are written to a real `authority` audit stream; financial execution
and allocation events remain on the purchase's `checkout` stream.

## HTTP contract

All mutations require `Idempotency-Key`. A repeated key with the same payload replays;
a different payload with the same key is refused. Tenant, buyer and merchant identities
come from the session, not request bodies.

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/reserve/authorities` | Create bounded selected-product permission |
| `GET /v1/reserve/authorities` | List the authenticated buyer's merchant permissions |
| `GET /v1/reserve/authorities/{id}` | Read current limits, expiry, epoch and capacity |
| `POST /v1/reserve/authorities/{id}/revoke` | Revoke future spending; replay-safe |
| `POST /v1/reserve/checkouts/{id}/versions/{v}/pay` | Submit the exact reviewed bill |
| `GET /v1/reserve/payments/{attempt_id}` | Read financial state, allocation and order ID |
| `POST /v1/reserve/simulator/{attempt_id}` | Protected demo outcome fixture, never buyer/model capability |

The payment body carries `authority_id`, `authority_epoch`, `content_hash`,
`amount_minor` and `currency`. The Kernel derives purchased SKUs and the buyer from
stored checkout state, not from an extra caller-supplied list. Recorded approval binds
both the financial operation `RESERVE_DEBIT` and the selected authority ID/epoch.

The simulator endpoint additionally requires the scenario key, is disabled in production,
and is not exposed by the frontend bridge. It queues reconciliation; it does not mark
an order paid from a browser callback. Default simulator outcome is `captured`.

## Exact checkout and mid-flight changes

1. The UI builds a real cart from its selected items and obtains a frozen approval card.
2. It displays the backend's item quantities, prices, taxes, fees, discount and total.
   The earlier visual storefront prices are not treated as authoritative.
3. The buyer confirms this reviewed bill, using the button or composer choice.
4. The approval and admission run in one transaction. The approval names the exact
   content hash, amount, operation and authority epoch.
5. Admission revalidates merchant truth. Relevant price/fee/offer/item changes return
   `REAPPROVAL_REQUIRED` and a successor card. The UI shows the change and asks again.
   This applies to a lower price or new offer too; no silent substitution is approved.
6. Out-of-scope products, insufficient capacity, expired/revoked permission or invalid
   approval are refused without an admitted debit. Sold-out inventory is not bypassed.
7. On admission, allocation, the single-winner payment attempt, Execution Grant,
   consumed approval and typed outbox command commit together.
8. A later merchant edit cannot erase an already accepted provider debit. Its exact
   admitted purchase remains the transaction identity; uncertain money is reconciled.

A reservation can legitimately fail in the seeded development database when old holds
or consumed reservations exhaust stock. The integration does not clear those rows or
reset paid inventory to make a demonstration pass.

## Durable execution and recovery

`RESERVE_DEBIT` and `RESERVE_RECONCILE` are distinct typed outbox commands. Existing
serialized command versions and manual/refund payloads are unchanged. Reserve grants
use the existing Kernel `Operation.RESERVE_DEBIT`; no merchant operation is added there.

The executor validates command checkout/version/hash/amount against the admitted
attempt. A malformed command must not release a legitimate allocation. Immediately
before first execution it checks authority expiry and epoch. An exhausted authority may
finish its already allocated debit; it cannot admit another one.

For this **local simulator**, grant consumption and provider acceptance are in one DB
transaction. A crash rolls both back or preserves both. Redelivery after acceptance
reads the durable outcome; it does not consume another grant or debit again. This is a
simulator-specific boundary, **not the protocol for a future network provider**. That
adapter must commit send intent/grant consumption before the network call and recover
by an idempotent provider reference after uncertain delivery.

An `unknown` fixture creates the Kernel's `UNKNOWN` state and keeps capacity `HELD`.
Reconciliation performs up to six delayed reads. If still unknown, it remains unresolved;
no fabricated failure or capacity release occurs. A protected later fixture update can
queue another reconciliation read. A confirmed result proceeds through
`RECONCILING`, recovered provider identity and normal provider-evidence application.
Revocation after acceptance does not suppress a later capture or refund owed to a buyer.

Provider requests use `reserve-simulator.invalid`, `SIMULATED_NO_NETWORK` and `sim_*`
identifiers. Evidence explicitly records simulated provider status. The proof-chain
lookup includes Reserve grants and retains the real audit/grant verification path.

## Frontend recovery and honesty

The local `/api/commerce/*` bridge uses an HttpOnly, SameSite buyer-session cookie and an
allowlist of routes. It never exposes the scenario key or provider credentials. It is
disabled in production unless explicitly enabled as a local-demo bridge. Its backend
still refuses simulated submission under the production profile.

Setup and checkout read the same persisted permissions. A pending request's card,
permission, idempotency key and attempt reference are retained in session storage for
recovery; these are recovery hints, not authority. A lost response retries the same
request identity. An expired session is not silently replaced during an uncertain write.

After submission, the UI polls backend status. Only `CAPTURED` plus a backend order ID
shows order confirmation. A timer or voice animation cannot create a successful order.
Unknown outcomes keep method switching blocked. Current manual-payment visual-preview
state remains separately labelled.

## Local configuration and verification

Existing local services: `bash scripts/run_demo.sh`. The concept frontend runs on 3100.
Apply the two additive migrations before starting the changed API/executor:
`f1a6c9d82b40` then `a72e890c14d6`.

Frontend server variables, if defaults differ:

- `COMMERCE_API_URL`: defaults to `http://127.0.0.1:8000`.
- `COMMERCE_TENANT_SLUG`: defaults to `demo`.
- `RESERVE_LOCAL_DEMO=true`: only for an explicitly local production-build preview.

Run each Python package's tests separately: their `conftest` modules share a module
name and combining package test paths can cause import collisions.

Focused coverage includes selected products, purchase limits, cumulative capacity,
owner/agent refusal, expiry, changed-price reapproval, request replay/conflict, queued
revocation, definitive failure release, unknown-to-captured/failed reconciliation,
executor redelivery, and Reserve grant/provider evidence in the proof-chain endpoint.
Manual approval/payment, manual executor/refund, authority/admission and outbox command
regressions are also run. Browser interaction/voice recording is not part of these
checks; this change does not claim real STT, live NPCI UAP or bank integration.
