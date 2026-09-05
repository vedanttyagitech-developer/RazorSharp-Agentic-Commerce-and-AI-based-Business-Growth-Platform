# Required failure scenarios

Specification section 30 lists the failures this platform must be shown to survive. This
document is the evidence index for that list. For each one it records three things,
because a scenario is only demonstrated when all three are true:

1. **the money invariant** — nothing is lost, nothing is duplicated;
2. **what a user actually sees** — a state a person can act on, in words;
3. **where the degradation shows** — the project's rule is that silent degradation is a
   defect, so a failure that leaves no visible trace counts as unhandled even when the
   money is safe.

Every row names a test that drives the scenario. Where a scenario cannot be driven
honestly today, it is listed under [Not demonstrable today](#not-demonstrable-today) with
what would be needed, rather than given a mock that proves nothing.

Two conventions from ADR 0003 govern how these read:

- **A kernel denial is HTTP 200 carrying a decision** (D15), never a 4xx. A test asserting
  4xx for a refusal is asserting the wrong thing. A 4xx or 5xx here means the request was
  malformed or the platform was unable to consider it at all — a different event, and the
  distinction is load-bearing in the database-outage row.
- **Money is integer minor units.** No float touches an amount anywhere in these paths.

---

## The scenario controller

Most of these are driven through `POST /v1/scenario/*`
(`packages/commerce-api/src/commerce_api/routers/scenario.py`), which is absent from the
production profile and gated on `X-Scenario-Key`. It arms real conditions rather than
faking outcomes:

| Lever | What it actually does |
| --- | --- |
| `POST /v1/scenario/injections` | Changes merchant state under the registry lock and writes a labelled `SCENARIO_INJECTION` audit row, so a price move is visibly an injection |
| `POST /v1/scenario/reservations/{id}/{v}/expire` | Moves the reservation's *deadline*, not its status, so the kernel's own expiry predicate refuses it |
| `POST /v1/scenario/webhooks/{id}/replay` | Re-posts the stored raw bytes with the original signature and event id |
| `POST /v1/scenario/duplicate-submit` | Two connections, two transactions, one barrier — a real race |
| `POST /v1/scenario/faults` | Arms one single-use worker-side fault; the fault fires *instead of* the provider call, never alongside it |
| `POST /v1/scenario/checkouts/{id}/invalidate-open` | Invalidates a checkout whose payment surface is still open, to set up a late capture |

`scenario_faults` rows are never read in the production profile
(`WorkerSettings.scenario_faults_enabled`). A fault that could fire against live
credentials would be a way to make a real payment call disappear.

### Injectability, checked live

Every lever below was driven against a running API on an isolated database
(`commerce_fail`, port 8090) rather than read from the source. What each returned:

| Spec 31.3 lever | Live result |
| --- | --- |
| Stock decrement | `201` — `STOCK_SET`, delta `stock_units 48 → 0`, one labelled audit row |
| Price/fee change | `201`, then submit `200 allowed=false code=REAPPROVAL_REQUIRED next_version=2` |
| Reservation expiry | `200`, then submit `200 allowed=false code=RESERVATION_EXPIRED` |
| Duplicate request | `201` then `200` with `Idempotent-Replayed: true` and the same basket id |
| Duplicate/out-of-order webhook | `200`, `duplicate_confirmed=true`, count `0 → 1`, `signature_reverified=true` — the stored raw bytes still verify |
| Payment timeout/unknown | `201` — `CREATE_ORDER_TIMEOUT` armed |
| Late capture | `200` — `AWAITING_PAYMENT → INVALIDATED_AWAITING_PAYMENT_RESULT` |
| Refund timeout | `201` — `REFUND_TIMEOUT` armed |
| **LLM/STT/TTS failure** | **no lever exists.** Both enums are closed and reject every spelling. The *responses* are implemented and tested (see below); only the injection is absent |

Two things a runbook needs and did not say:

- **Late capture needs the worker.** `invalidate-open` requires `AWAITING_PAYMENT`, and a
  checkout only reaches it when the durable worker has recorded the provider order.
  Against an API with no worker running it answers `409 illegal_transition:
  EXECUTION_PENDING -> INVALIDATED_AWAITING_PAYMENT_RESULT`, which reads like a broken
  lever and is not one. Start the worker, wait for the state, then throw the lever.
- **Concurrent checkout returns both answers**: `admitted_count=1`, one grant, codes
  `['OK', 'DUPLICATE_OPERATION']`.

Every denial above is an HTTP 200 carrying `allowed: false` (ADR 0003 D15), and every
amount is an integer: the checkout above priced at `amount_minor=8550`, an `int`.

---

## Section 30, row by row

### Hallucinated product

An agent proposes a SKU that does not exist.

- **Money:** nothing moves; a proposal is not a basket line.
- **User sees:** the unknown SKU is dropped and grounded alternatives are offered with a
  notice. `GET /v1/catalogue/products/{sku}` for an unknown SKU is a 404 problem.
- **Visible:** the rewritten sentence carries a notice in the buyer's language; the
  ungrounded SKU never enters provenance.
- **Evidence:** `agent-runtime/tests/test_ar_grounding.py::test_ungrounded_sku_is_rewritten_with_a_notice_and_alternatives`;
  `agent-runtime/tests/test_ar_injection.py::test_injected_sku_never_enters_provenance`;
  `commerce-api/tests/test_capi_agent.py::test_turn_proposes_a_basket_line_only_for_a_sku_a_tool_returned`.

### Malicious product description

Merchant-supplied text tries to instruct the model.

- **Money:** none; the path never reaches a money verb.
- **User sees:** the product, without the injected instruction having any effect.
- **Visible:** the scanner names the matched pattern **without copying the payload** into
  the log, so an operator learns an injection was seen without the log becoming a delivery
  vector for it.
- **Evidence:** `agent-runtime/tests/test_ar_injection.py::test_injection_in_merchant_text_causes_no_tool_call`,
  `::test_scanner_names_the_pattern_without_copying_the_payload`;
  `agent-runtime/tests/test_ar_grounding.py::test_payloads_fence_merchant_text_and_carry_the_notice`.

### Stale inventory

The catalogue moves between quote and checkout.

- **Money:** the quote is recomputed from the fee engine; a total whose components do not
  support it is refused on the wire.
- **User sees:** the basket is marked stale rather than priced, and a sold-out line is
  named.
- **Visible:** staleness is a field on the quote, not an absence.
- **Evidence:** `commerce-api/tests/test_capi_journey.py::test_basket_quote_is_deterministic_and_flags_staleness`;
  `agent-runtime/tests/test_ar_backends.py::test_a_sold_out_line_makes_the_basket_stale_not_priced`,
  `::test_a_quote_refuses_a_total_its_components_do_not_support`.

### Price / fee change under an approved checkout

- **Money:** version N is invalidated and N+1 created with its own Policy-at-Sale Receipt
  and its own hold. **No payment attempt is created**, so there is nothing to reconcile.
- **User sees:** a `REAPPROVAL_REQUIRED` decision listing the exact deltas and naming
  `next_version`. Re-approving N+1 then succeeds.
- **Visible:** the deltas are in the decision body; the injection that caused them is a
  labelled row on the timeline, distinguishable from organic movement.
- **Evidence:** `commerce-api/tests/test_capi_journey.py::test_a_price_change_denies_with_deltas_and_creates_version_two`,
  `::test_reapproving_version_two_then_succeeds`,
  `::test_submitting_version_one_after_supersede_is_refused`.

### Duplicate order

The same `Idempotency-Key` arrives twice.

- **Money:** one admission. The second call replays the stored response body rather than
  admitting again.
- **User sees:** the original result, byte for byte.
- **Visible:** the `Idempotent-Replayed` response header. A different payload under the
  same key is a 422 rather than a silent overwrite.
- **Evidence:** `commerce-api/tests/test_capi_foundation.py::test_the_same_key_and_payload_replays_the_stored_response`,
  `::test_the_same_key_with_a_different_payload_is_422`;
  `commerce-api/tests/test_capi_journey.py::test_the_same_key_replays_instead_of_admitting_twice`.

### Concurrent checkout

Two tabs submit the same version at the same instant.

- **Money:** exactly one Execution Grant, by construction — a partial unique index in
  PostgreSQL, not application logic.
- **User sees:** the winner gets the grant; the loser gets a `CONCURRENT_OPERATION`
  decision (a 200) and the current state.
- **Visible:** both outcomes are returned side by side by the scenario lever, so the
  loser's refusal is shown rather than described.
- **Evidence:** `commerce-api/tests/test_capi_journey.py::test_two_concurrent_submits_produce_one_winner`;
  `commerce-api/tests/test_capi_scenario.py::test_two_concurrent_submits_produce_exactly_one_grant`;
  `transaction-kernel/tests/test_grants.py::test_two_concurrent_issues_for_one_attempt_leave_exactly_one_grant`.

### Expired reservation / approval

- **Money:** payment is blocked; the hold is released and must be re-taken.
- **User sees:** a `RESERVATION_EXPIRED` decision, and a re-reservation path.
- **Visible:** the expiry is on the reservation row and in the decision; the scenario lever
  moves the deadline, so the kernel refuses on its own clock rather than on a status a
  test wrote.
- **Evidence:** `commerce-api/tests/test_capi_scenario.py::test_reservation_expiry_makes_a_later_admission_deny`;
  `transaction-kernel/tests/test_reservations.py`.

### Revoked authority

- **Money:** denied under the locked epoch; no grant is issued.
- **User sees:** an authority denial naming the epoch.
- **Visible:** the admission decision records it; the audit chain carries it.
- **Evidence:** `transaction-kernel/tests/test_authority.py`, `test_admission.py`
  (revocation/admission race).

### Discount-limit violation

**Enforced structurally; not demonstrable live.** Working this row honestly turns up
something worth stating rather than dressing up: **the Demo Grocery Store runs no discounts
at all.** `content_from_quote` emits `discount_minor: 0` unconditionally and the merchant's
policy set carries a `DISCOUNT` policy whose terms are `{"allowed": False}`. There is no
discount engine, so there is no limit to violate and no request to deny — which is a
stronger guarantee than denying one on request, and a weaker demonstration.

- **Money:** no discount can enter a total. `discount_minor` is a canonical *hashed* field,
  so a document carrying one is a different document with a different hash, and approval
  compares hashes. The content builder also recomputes the arithmetic, so "fix the total to
  match" fails too.
- **User sees:** the Policy-at-Sale Receipt says discounts are not allowed. That is the
  "cite policy" the specification asks for, and it is captured at approval rather than
  looked up later, so a merchant enabling discounts afterwards cannot change what this
  buyer was told.
- **Visible:** the policy is in the receipt for the version, with its own policy version.
- **Evidence:** `merchant-sim/tests/test_fs_discount_limit.py` (5 tests, written for this
  row); `merchant-sim/tests/test_ms_kernel_adapter.py`;
  `transaction-kernel/tests/test_tk_checkout_content.py::test_delivery_fee_and_discount_enter_the_total`.
- **What would be needed to demonstrate it:** a discount or promotion engine in the
  merchant simulator with a configurable cap, and a scenario lever that requests a discount
  beyond it. Until that exists, this row should be reported as *enforced* and not as
  *demonstrated*.

### Duplicate / out-of-order webhook

- **Money:** stored once. Application is monotonic: a late `authorized` arriving after a
  `captured` does not regress the attempt.
- **User sees:** no change on redelivery — the order stays where it was.
- **Visible:** the inbox's `duplicate_count` increments and the replay lever reports
  `duplicate_confirmed` with the counts before and after, plus a re-verification of the
  stored signature against the stored raw bytes.
- **Evidence:** `durable-worker/tests/test_dwk_webhook.py::test_a_redelivered_command_for_the_same_row_changes_nothing`,
  `::test_a_late_authorized_after_a_capture_does_not_regress_the_attempt`;
  `durable-worker/tests/test_dwk_create_order.py::test_a_second_delivery_calls_the_transport_zero_times`;
  `commerce-api/tests/test_capi_scenario.py::test_the_replay_hands_the_receiver_the_exact_bytes_that_were_signed`,
  `::test_replaying_a_stored_webhook_is_recognised_as_a_duplicate`.

### LLM failure

The model times out, is misconfigured, or the tenant's budget is exhausted.

- **Money:** none. No approval, checkout, payment or refund state changes from a model
  failure — the model has no money verb to call.
- **User sees:** a deterministic fallback that names the actions available and **makes no
  money claim**. Budget exhaustion is a denial, not a silent downgrade.
- **Visible:** a misconfiguration is loud rather than a fallback, so "the model is not
  wired up" never masquerades as "the model declined".
- **Evidence:** `agent-runtime/tests/test_ar_harness.py::test_timeout_renders_fallback_and_leaves_state_unchanged`,
  `::test_misconfiguration_is_loud_not_a_fallback`;
  `agent-runtime/tests/test_ar_messages.py::test_fallback_is_non_empty_and_makes_no_money_claim`;
  `agent-runtime/tests/test_ar_capabilities.py::test_budget_exhausted_denies`;
  `commerce-api/tests/test_capi_agent.py::test_a_turn_leaves_every_financial_table_untouched`.

### Unknown payment (provider unreachable, or answers after the timeout)

The nastiest case in payments: the request was aborted locally but may have been committed
upstream. Driven for real with `POST /v1/scenario/faults` armed `CREATE_ORDER_TIMEOUT`.

- **Money:** the attempt becomes `UNKNOWN`, never `FAILED`. The grant was consumed and
  committed *before* the send, so a redelivered command finds it `CONSUMED` and takes the
  reconciliation path instead of sending a second `POST /v1/orders`. Reconciliation looks
  the order up by the stable receipt and settles from provider truth. Bounded at six
  rounds (ADR D13), then escalation — never a silent loop, never a blind retry.
- **User sees:** payment pending verification, with no second charge and no second order.
- **Visible:** the `provider_requests` row carries a transport error rather than a
  fabricated HTTP status, and for an injected fault it says
  `ScenarioFault:CREATE_ORDER_TIMEOUT` — so a reviewer can tell a demo injection from a
  real timeout without reading code.
- **Evidence:** `durable-worker/tests/test_dwk_create_order.py::test_a_timeout_leaves_the_attempt_unknown_and_enqueues_reconciliation`,
  `::test_grant_is_consumed_and_committed_before_the_transport_is_called`,
  `::test_a_second_delivery_calls_the_transport_zero_times`;
  `durable-work/tests/test_outbox.py::test_an_unknown_payment_outcome_is_buried_immediately_not_retried`;
  `durable-worker/tests/test_dwk_reconcile.py::test_a_lost_create_is_found_by_receipt_and_settled_from_provider_truth`,
  `::test_the_sixth_round_escalates_instead_of_scheduling_a_seventh`;
  `durable-worker/tests/test_dwk_create_order.py::test_an_order_for_a_different_amount_escalates_and_does_not_reconcile`.

### Capture on an invalid checkout version

> **The refund half is not wired.** Driven end to end, a late capture on an invalidated
> checkout writes **zero** `orders` (fulfilment blocked, as required) and **zero**
> `refunds`, with no `REFUND_EXECUTE` command enqueued. The kernel primitive
> `admit_stale_capture_refund` exists and is unit-tested, but nothing in the application
> calls it — the only mention outside the kernel and its tests is a docstring in
> `scenario_service.invalidate_open_checkout` promising that "exactly one automatic refund
> is admitted". `apply_provider_evidence` classifies the capture and stops. Report the
> fulfilment block as proven and the automatic refund as implemented-but-unconnected.

- **Money:** no fulfilment, and the reservation is deliberately kept: stock that may in
  fact have been paid for must not be resold before the late capture is resolved.
- **User sees:** no order, and a refund in flight.
- **Visible:** a `STALE_CAPTURE` transition on the checkout with the reason recorded.
- **Evidence:** `commerce-api/tests/test_capi_scenario.py::test_invalidate_open_refuses_a_checkout_with_no_payment_open`
  and the `invalidate-open` lever; `transaction-kernel/tests/test_tk_payments.py`,
  `test_tk_refunds.py`.

### Missing item after capture

- **Money:** partial or full refund under policy; the amount originates in a resolution
  plan, never in model output.
- **User sees:** the refund and its amount, with the policy that produced it.
- **Visible:** the refund row's wire state and the resolution plan are both readable.
- **Evidence:** `commerce-api/tests/test_capi_listing.py::test_a_partial_settled_refund_is_partially_refunded`,
  `::test_refunds_list_reports_wire_state_and_counts`.

### Unknown refund

- **Money:** a duplicate refund is blocked. A refund timeout is `REFUND_UNKNOWN` and
  reconciles; a refund the provider never made is verified absent rather than assumed
  either way.
- **User sees:** refund pending verification; a second request while one is in flight is a
  200 denial, not a second refund.
- **Visible:** the reconciliation round is recorded with its number and decision.
- **Evidence:** `durable-worker/tests/test_dwk_refund.py::test_a_timeout_is_refund_unknown_and_enqueues_reconciliation`,
  `::test_a_second_delivery_never_sends_a_second_refund`;
  `durable-worker/tests/test_dwk_refund.py::test_a_refund_the_provider_never_made_is_verified_absent`;
  `commerce-api/tests/test_capi_payments.py::test_a_second_refund_while_one_is_in_flight_is_a_200_denial`.

### Connector failure

The merchant catalogue/inventory/pricing connector is unavailable.

- **Money:** no stock, price, fee or fulfilment state is invented. Quote and revalidation
  pause; affected checkouts do not admit.
- **User sees:** browsing data marked stale where that is safe; the checkout path stops
  rather than quoting a number nobody can stand behind.
- **Visible:** an unreachable upstream is a 503 problem document, never a fabricated
  answer. The console's proxy has the same rule: "a console that painted a plausible queue
  depth over a failed request would be worse than no console."
- **Evidence:** `agent-runtime/tests/test_ar_backends.py::test_transport_failure_is_a_503_problem`,
  `::test_rfc9457_problem_becomes_a_structured_backend_error`,
  `::test_a_shape_violation_is_a_contract_error_not_a_key_error`.

---

## Beyond section 30

Three infrastructure failures that section 30 does not name but section 23.3 requires, and
that a demonstration is otherwise likely to skip.

### The database is unreachable for a period, then returns

Driven for real: the API's database traffic runs through a TCP forwarder that is stopped
mid-test, so pooled connections have their sockets closed underneath them exactly as they
would if Cloud SQL went away. The same `Engine` object — asserted to be the same object —
serves traffic again afterwards.

- **Money:** nothing moves. A submit during the outage creates no payment attempt, no
  grant and no outbox command, and the counts are identical before and after. One submit
  after recovery produces exactly one of each.
- **User sees:** the storefront still loads (`/healthz` answers from process state, so
  Kubernetes does not restart the pod mid-payment). The submit comes back as an RFC 9457
  problem with **no decision in it** — an outage is not a denial, and answering 200 with a
  decision-shaped body would tell a buyer the platform considered their checkout when it
  never saw it. The problem body discloses no connection string.
- **Visible:** `GET /v1/config` answers 200 with `database.reachable: false` and a
  `degraded` entry naming the component and the consequence. Safe Mode is separately
  reported as *unknown* rather than as `false`, because "the kill switch is off" and
  "nobody can tell you whether the kill switch is off" are different facts.
- **Evidence:** `commerce-api/tests/test_fs_database_unavailable.py` (5 tests).

### The worker dies mid-command, holding a lease

- **Money:** the command is redelivered, not lost, and not executed twice — the grant was
  consumed before the send, so the redelivery reconciles instead of re-sending.
- **User sees:** nothing; this is the case the architecture is built to make invisible,
  and that is the correct outcome because no state was left wrong.
- **Visible:** the lease lapses on the database clock and the row is re-leased. A worker
  that comes back cannot complete a command that was reassigned. A command whose attempts
  are exhausted is buried with an audit row, never dropped.
- **Evidence:** `durable-work/tests/test_outbox.py::test_a_crashed_workers_command_is_re_leased_once_the_deadline_passes`,
  `::test_a_lapsed_lease_cannot_be_completed_even_when_nobody_took_over`,
  `::test_the_superseded_worker_cannot_complete_the_reassigned_command`,
  `::test_a_worker_that_dies_on_its_last_attempt_is_reaped_not_stranded`,
  `::test_concurrent_workers_partition_the_queue_without_overlap`;
  `durable-worker/tests/test_dwk_loop.py::test_the_lease_is_committed_before_the_handler_runs`.

### Authority lapses between issue and use

A grant expires, or Safe Mode is thrown, while the command is already in the outbox.

**This is the row most likely to be misread, so it is stated plainly.** There is a `DEAD`
outbox row in the development database from an expired grant, carrying
`AUTHORITY_INSUFFICIENT`. **The worker refusing it is correct behaviour, not a bug.** The
tests exist so that nobody converts it into a retry.

- **Money:** zero provider requests. The grant is not consumed; the payment attempt keeps
  whatever state it had. An incident is not evidence that a charge failed, so nothing is
  rewritten.
- **User sees:** the approved checkout version and its reservation survive — the
  reservation stays `CONSUMED`, so the stock is not returned to the shelf for somebody else
  to buy while an operator decides. Recovery is a re-admission of the same approved bytes,
  which re-checks reservation, approval and authority epoch from scratch. Leaving Safe Mode
  does not resurrect a revoked grant, and that is affordable precisely because the buyer
  has lost nothing but time.
- **Visible:** `AUTHORITY_INSUFFICIENT` is **not** retryable, so the command is buried on
  the *first* attempt rather than after eight quiet retries — a scheduled retry would have
  hidden the stop behind a delay. Two audit rows on two streams: `worker.grant_refused` on
  the checkout (naming the grant, the attempt and the reason) and `outbox.dead_letter` on
  the command (carrying the terminal code and the full payload, so `revive` can return it
  to the queue after re-admission). Burial is not deletion.
- **Safe Mode's asymmetry, which is deliberate:** the activation sweep names `RESERVE_DEBIT`
  alone (`DELEGATED_GRANT_OPERATIONS`). A **human-present Standard Checkout already in the
  outbox runs to completion during an incident** — specification 34 requires Safe Mode to
  block delegated and Reserve Pay execution "while preserving human-present checkout and
  recovery". Cancelling a checkout a person has already approved makes nobody safer; it
  strands them holding a reservation and no order, over an incident that had nothing to do
  with them. Refund grants are in `NEVER_SWEPT` for the same reason, and that holds against
  the `revoke_operations` override, not only against its default.
- **Evidence:** `durable-worker/tests/test_fs_authority_lapses_midflight.py` (7 tests);
  `transaction-kernel/tests/test_grants.py::test_safe_mode_revokes_unused_grants_and_leaves_consumed_ones`;
  `transaction-kernel/tests/test_safe_mode.py::test_refund_grants_are_never_swept_by_activation`,
  `::test_leaving_does_not_resurrect_revoked_grants`,
  `::test_the_sweep_cannot_be_pointed_at_refund_grants_by_its_caller`;
  `commerce-api/tests/test_capi_scenario.py::test_safe_mode_blocks_a_delegated_debit_and_keeps_the_buyer_whole`.

---

## Not demonstrable today

Listed rather than mocked. A mock of any of these would prove that the mock behaves, which
is not the claim section 30 asks for.

Two different things are collected here and the difference matters when quoting a status:
**implemented and tested but not injectable** (LLM, STT, TTS -- the response is real and
proven, only the live lever is missing) versus **not implemented at all** (multi-region,
which the specification itself refuses to claim). Reporting the first group as a gap
understates the platform; reporting it as demonstrated overstates it.

### LLM / STT / TTS failure — tested, not injectable

Both rows have a deterministic response and a test. Neither has a lever, and that is the
whole of the gap.

**STT failure.** `voice_runtime.pipeline` degrades explicitly -- its own comment reads
"Silent degradation is a defect (19.12): every degraded path is a frame" -- and emits a
`degradation` frame carrying `kind`, `text_input_available` and
`transaction_state_changed`. `test_an_stt_failure_leaves_typing_working_and_says_no_state_changed`
asserts exactly section 30's mandated response: `kind == "stt_unavailable"`,
`text_input_available is True`, `transaction_state_changed is False`, and a typed turn
still runs. `test_typed_input_runs_a_turn_while_speech_is_unavailable` and
`test_exhausted_reconnects_degrade_visibly` cover the reconnect path.

**TTS failure.** `test_tts_failure_leaves_the_text_visible` asserts the reply text is still
delivered, the degradation frame says `tts_failed`, and `transport.audio_chunks() == []` --
the buyer reads the exact deterministic text rather than hearing a fabricated one.

**LLM failure** is covered above (harness fallback, state unchanged, budget denial).

What is missing for all three is the *injection*. Specification 31.3 lists
"LLM/STT/TTS failure" as one of nine required scenario-controller injections and it is the
only bullet with no lever: `FaultKind` and `InjectionKind` are closed enums covering the
other eight, and every spelling of a model or speech fault is refused 422. So these rows
can be asserted in a test and cannot be shown to a judge on demand.

The shape of the missing lever is not the shape of the existing ones, which is why it is
not a five-line addition. Every current fault is a *worker-side provider timeout*, claimed
from `scenario_faults` and consumed before a network call. A model failure happens inside
the API process during a turn, and a speech failure inside the voice gateway; neither is a
Razorpay call and neither is worker-consumed, so either `scenario_faults` grows a
non-worker consumer or these get their own store.

### The two fault enums disagree

Independently of the missing lever, the fault vocabulary the API advertises and the one the
worker implements are not the same set, and each has one member the other lacks:

| Kind | `commerce_api.services.scenario_service.FaultKind` | `durable_worker.faults.FaultKind` | Effect |
| --- | --- | --- | --- |
| `CREATE_ORDER_TIMEOUT` | arms | consumed in `handlers/create_order.py` | works |
| `REFUND_TIMEOUT` | arms | consumed in `handlers/refund.py` | works |
| `PAYMENT_FETCH_TIMEOUT` | **arms (201)** | **no consumer** | **dead lever** |
| `RECONCILE_FETCH_TIMEOUT` | **refuses (422)** | **consumed in `handlers/reconcile.py`** | **unreachable fault** |

Verified live against a running API, and by enumerating every `claim_fault` call site in
the worker.

Both directions cost something. Arming `PAYMENT_FETCH_TIMEOUT` returns `201` with
`armed: true` and then nothing ever happens -- an operator demonstrating a payment-fetch
timeout would watch a normal payment succeed while a row sits armed forever. And
`RECONCILE_FETCH_TIMEOUT` is the one the worker's own docstring says exists "so the
bounded-attempts path (ADR D13) can be shown ending in an escalation rather than in a
silent loop" -- which is precisely the demonstration that cannot currently be started,
because the controller will not arm it.

This is a two-line vocabulary fix in `scenario_service.py`, and it should be made in the
same pass as the LLM/STT/TTS lever rather than separately, since both edit that enum.

### Secret Manager / signing service unavailable

Section 23.3 requires that the affected Razorpay or protocol operation be disabled and
alerted, with no unsigned mandate and no missing-secret fallback.

**What the platform can already show:** a missing secret is loud rather than silently
absent — `commerce_api.settings` refuses a live key outside production, requires both role
URLs, and reports `scenario_routes_enabled: false` when the scenario key is absent, so the
scenario controller 404s rather than 401s. `infra/docker/node-entrypoint.mjs` and
`entrypoint.py` both refuse to export an empty secret file and say so on stderr.

**What would be needed:** a running deployment against a real Secret Manager, with the
secret's IAM binding removed mid-flight, and an assertion that the affected operation is
disabled and an alert fires. This cannot be driven from the test suite because the failure
is in the CSI mount, outside the process. Validating that the mounts are *declared*
correctly is what `scripts/validate_infra.sh` step 5 does; that is a different and weaker
claim, and is stated as such.

### Real-audio voice evidence never runs in CI

Six tests in `packages/voice-runtime/tests/test_voice_real_audio.py` skip with
`GOOGLE_CLOUD_PROJECT is not set`. No workflow in `.github/workflows/` sets that variable,
so they skip on every CI run and on any laptop without it.

That is correct behaviour for an optional credential -- the repository-wide rule in
`conftest.py` deliberately enforces only `db`-marked skips, because failing a run for a
missing optional credential punishes the honest path. But specification 35's evidence table
asks for a "Rotation-under-speech real-audio test" and "real-audio echo and barge-in tests"
before those rows may be claimed, and a run of the suite does not provide them. Anyone
filling in that table should either run the suite with `GOOGLE_CLOUD_PROJECT` set and quote
that run, or mark those rows unproven. `4518 passed, 6 skipped` is not evidence for them.

### Multi-region failover

Section 21.9 already refuses this claim: the architecture is region-aware and **initially
deployed in one region**. There is no second region to fail over to, and no test here
should suggest otherwise.

---

## Running these

```bash
export PATH="$HOME/.local/bin:$PATH"
uv run --no-sync pytest packages/ -o addopts="--strict-markers" -k "fs_ or scenario or dwk_ or safe_mode"
```

The two files written specifically for this document:

```bash
uv run --no-sync pytest packages/commerce-api/tests/test_fs_database_unavailable.py -o addopts="--strict-markers"
uv run --no-sync pytest packages/durable-worker/tests/test_fs_authority_lapses_midflight.py -o addopts="--strict-markers"
```

Both need a database with migrations applied and test roles bootstrapped; see
`docs/WORKSTREAMS.md` for the four `DATABASE_URL_TEST_*` overrides a worktree must set.
Run them in separate invocations: each package's `tests/` directory carries its own
`conftest.py` and pytest's prepend import mode gives one file per name.
