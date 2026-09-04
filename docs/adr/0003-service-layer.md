# ADR 0003: The service layer over the Transaction Assurance Kernel

Status: accepted, 2026-09-04. Supersedes nothing; extends PROJECT_SPECIFICATION.md sections
5, 8, 10, 11, 21.4, 24, 26, 28 and 31 with implementation decisions. Where this file and the
specification differ in detail, this file is what is built.

## Context

Six packages exist and are proven (1,988 tests): commerce-domain, platform-db,
transaction-kernel (admission is single-winner under real contention), durable-work,
merchant-sim and payment-adapters. Nothing can be driven over HTTP yet. This layer makes
the eleven-step demonstration (spec 2.5) run end to end on real Razorpay test mode, with
every money invariant kept and every step leaving inspectable evidence.

Three designs were produced independently and judged by two independent reviewers (a
Razorpay payments-engineer lens and a distributed-systems lens). Both chose the
"Razorpay-signal-first" design. Its grafts and the reviewers' corrections are folded in
below.

## Decisions

D1. **In-process kernel.** API mutations run one transaction as the `commerce_kernel`
    role and call kernel functions directly; reads run as `commerce_app`. The worker holds
    a `commerce_worker` engine for the outbox and a `commerce_kernel` engine for kernel
    calls. The physical boundary is the database grant set, enforced by
    `test_import_boundary` style source tests. An RPC split adds nothing a reviewer can see.

D2. **No package cycles.** Dependency direction is fixed:
    `commerce-domain ← platform-db ← transaction-kernel ← {durable-work, payment-adapters,
    merchant-sim} ← {commerce-api, durable-worker}`. The kernel never imports an adapter or
    the outbox. `CaptureEvidence` and `may_fulfil` move to `transaction_kernel.evidence`;
    `payment_adapters.razorpay.fulfilment` re-exports them unchanged.

D3. **payment-adapters stays transport-free.** The `httpx` transport lives in
    `durable_worker.transport`. Only the worker talks to Razorpay.

D4. **Admission hardening.** (a) `approval_id` is verified by
    `approvals.consume_recorded` after the version lock; mismatch, expiry or reuse denies
    `AUTHORITY_INSUFFICIENT`. (b) The `payment_attempts` INSERT runs under a SAVEPOINT;
    SQLSTATE 23505 on the one-non-terminal index becomes an audited
    `CONCURRENT_OPERATION` denial. The library never calls `session.rollback()`.
    (c) On `REAPPROVAL_REQUIRED`, version N's ACTIVE reservation is released (cause
    SUPERSEDED), then N+1 is created with its Policy-at-Sale Receipt and a fresh
    reservation, all inside the admission transaction, so a crash cannot leave an
    unapprovable N+1. (d) `CurrentMerchantState` gains an optional `content` mapping;
    when present it is the full canonical content (D6) and `content_for_hash` returns it.

D5. **Lock order** extends the kernel's: `platform_operating_modes → checkout_versions →
    reservations → delegated_authorities → payment_attempts → execution_grants → refunds`.
    Every writer that touches both a checkout version and its attempt locates the attempt
    with a plain SELECT, locks `checkout_versions FOR UPDATE`, then
    `payment_attempts FOR UPDATE`. Cancel and provider-evidence application therefore
    cannot deadlock.

D6. **Canonical checkout content** is owned by `transaction_kernel.checkout_content`
    (builder + validator + a frozen regression vector test). merchant-sim builds content
    only through it. The approval hash contract does not depend on the simulator.

D7. **Webhook receiver.** One Razorpay test account per process. Route
    `POST /webhooks/razorpay/{tenant_slug}`. Order of operations is fixed: read raw bytes
    (256 KiB cap) → verify HMAC with the webhook secret, constant time → resolve tenant
    from the slug → claim an inbox row (dedupe on `x-razorpay-event-id`, else a body
    fingerprint) → enqueue `APPLY_WEBHOOK_EVENT` → 200. JSON is parsed only after
    verification. The worker's apply step confirms the referenced order belongs to that
    tenant before touching state.

D8. **Browser callback is never capture evidence.** `POST /v1/payments/verify` checks
    ownership, verifies `HMAC(order_id|payment_id)`, records `BROWSER_CALLBACK`, and
    enqueues `RECONCILE_PAYMENT`. Capture is applied only from `PROVIDER_FETCH` or
    `WEBHOOK` evidence through `payments.apply_provider_evidence`, which uses
    `states.monotonic_apply` so `CAPTURED` never regresses.

D9. **Duplicate submit.** Same `Idempotency-Key` replays the stored response with an
    `Idempotent-Replayed: true` header. Same key, different payload → 422 problem. A
    concurrent second submit with a different key returns 200 with
    `outcome: DUPLICATE_OPERATION` and the winner's attempt id; the buyer sees the one
    live attempt, which is the two-tabs demonstration.

D10. **Refund grants** are bound per refund row (the grant binding carries `refund_id`),
    so a second partial refund on one attempt can be admitted after the first is
    consumed. The one-non-terminal-attempt-per-checkout index is unchanged.

D11. **Scenario controller** runs in the API process under `X-Scenario-Key`; its routes do
    not exist in the production profile. Every injection writes a `SCENARIO_INJECTION`
    audit row and is never mixed with organic data.

D12. **Testing.** Real PostgreSQL as NOSUPERUSER NOBYPASSRLS roles, never Testcontainers.
    Markers `db`, `e2e`, `razorpay_live`. CI applies migrations and bootstraps roles, and
    fails if any `db` test is skipped. Test file basenames are unique across the whole
    repository (pytest prepend import mode).

D13. **Constants.** Webhook body limit 256 KiB; outbox lease 60 s; provider transport
    timeout 20 s; grant TTL 300 s; reservation TTL 900 s; approval TTL 600 s;
    reconciliation bounded to 6 attempts with exponential backoff before `ESCALATED`.

D14. **One API process.** The merchant simulator's state lives in the API process, so
    settings refuse `WEB_CONCURRENCY > 1`. Documented demo restriction; GKE runs one
    replica of the API and one of the worker.

D15. **Errors** are RFC 9457 problem details. `RecoveryCode` maps to HTTP status in one
    table in `commerce_api.errors`; kernel denials are 200 with the structured decision,
    never 4xx, because a denial is the system working.

## Endpoint catalogue (contract for all build units)

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| POST | /v1/demo/sessions | none (dev/demo profile only) | Mint a pseudonymous buyer or agent session; returns bearer token |
| GET | /v1/catalogue/search | session | Grounded discovery with freshness |
| GET | /v1/catalogue/products/{sku} | session | Product detail with live availability |
| POST | /v1/baskets | session + Idempotency-Key | Create basket |
| PUT | /v1/baskets/{id}/lines/{sku} | session + Idempotency-Key | Set line quantity; returns deterministic quote |
| GET | /v1/baskets/{id} | session | Re-quote; shows staleness |
| POST | /v1/baskets/{id}/checkout | session + Idempotency-Key | Version 1 + receipt + reservation; returns approval card |
| GET | /v1/checkouts/{id} | session (owner) | Head, versions, approval card, attempt summary |
| POST | /v1/checkouts/{id}/versions/{v}/approve | session (owner) + key | Trusted approval; body echoes hash + amount |
| POST | /v1/checkouts/{id}/versions/{v}/reject | session (owner) + key | Reject; release reservation |
| POST | /v1/checkouts/{id}/versions/{v}/submit | session + key | Kernel admission; decision verbatim |
| POST | /v1/checkouts/{id}/cancel | session (owner) + key | Cancel within policy |
| GET | /v1/checkouts/{id}/payment | session (owner) | Payment handoff: order id, key id, state |
| POST | /v1/payments/verify | session (owner) + key | Client-return verification (D8) |
| POST | /webhooks/razorpay/{tenant_slug} | raw-body HMAC | Webhook inbox (D7) |
| GET | /v1/orders/{id} | session (owner) | Order status with capture evidence |
| POST | /v1/orders/{id}/refunds | session (owner) + key | Buyer-confirmed refund |
| GET | /v1/checkouts/{id}/timeline | session or scenario key | Action timeline |
| GET | /v1/checkouts/{id}/events | session | SSE timeline, resumes from Last-Event-ID |
| GET | /v1/checkouts/{id}/proof | session or scenario key | Money Action Proof Chain + verifier |
| GET | /v1/inspector/payment-attempts/{id} | scenario key or owner | Inspector document |
| GET | /v1/audit/streams/{type}/{id}/verify | scenario key or owner | Hash-chain verification |
| GET | /v1/merchants/{id}/evidence/retained-revenue | scenario key | Step 11 evidence |
| POST | /v1/scenario/injections | scenario key | Merchant state change (step 5) |
| POST | /v1/scenario/reservations/{id}/{v}/expire | scenario key | Reservation expiry by DB clock |
| POST | /v1/scenario/webhooks/{inbox_id}/replay | scenario key | Replay stored raw webhook |
| POST | /v1/scenario/duplicate-submit | scenario key | Concurrent submit race |
| POST | /v1/scenario/faults | scenario key | Arm worker-side fault |
| POST | /v1/scenario/checkouts/{id}/invalidate-open | scenario key | Late-capture scenario |
| POST/GET | /v1/ops/safe-mode | scenario key / session | Kill switch |
| GET/POST | /v1/ops/outbox, /v1/ops/outbox/{id}/revive | scenario key | Operator view |
| GET | /healthz, /v1/config | none | Liveness; redacted runtime facts |

## Consequences

The API process connects as the kernel role for mutations. The answer to "why" is D1: the
transaction executes only kernel functions plus idempotency bookkeeping, and the app role
physically cannot write a financial table, which the tests prove.
