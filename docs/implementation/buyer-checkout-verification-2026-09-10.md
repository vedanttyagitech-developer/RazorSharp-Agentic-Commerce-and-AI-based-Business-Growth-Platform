# Buyer checkout integration verification — 2026-09-10

## Changes

- Review uses backend line subtotals, tax, delivery, discounts, total and order reference. Removed the local-only offer simulation and fabricated Policy-at-Sale Receipt/order references.
- Closed-cart checkout recovery reads the existing checkout. In-flight purchases are shown as “Checking your payment”; recovery does not record another approval. Manual resume uses the existing Razorpay order. Reserve attempts are classified through the owned Reserve payment endpoint before offering a manual resume.
- Reserve retry storage cannot leave the paying latch stuck when session storage fails. A stored pending purchase cannot replace a different reviewed checkout. Polls do not overlap. Expired stock reservations offer cancellation and fresh stock/price review, with a server check that no payment attempt exists.
- Manual approval retry keys are saved before network submission. Transport loss and provider UI failure are not described as proof of no charge. Closing review leaves the admitted payment tracked by the backend.
- Checkout voice guidance references a checkout/version, reads it using the buyer credential, and speaks server-authored facts through the existing TTS pipeline. A browser stage cannot assert success or supply an amount. Changed versions require review. Guidance opens no consent window and performs no financial mutation.
- Checkout transcripts route to the trusted checkout UI instead of the Shopping agent. Exiting checkout clears that routing context without closing the voice session.
- Corrected the remaining Reserve explanation of expiry: new permissions are valid until revoked.

## Verified

- 33 Reserve/approve-and-pay API tests with local PostgreSQL, including revocation, capacity, changed-bill refusal and reconciliation.
- 62 focused voice/guidance/pipeline/client tests.
- 21 frontend transport/recovery tests; TypeScript and local production build.
- Real configured STT/model/TTS: milk followed by bread, in one pipeline with resumed listening and grounded product cards. This uses generated speech and a memory transport, not a physical microphone.
- Browser: merchant-wide permission saved with user-selected ₹200 per purchase and ₹5,000 capacity, until revoked.
- Browser: expired stock reservation refused; fresh review preserved selected item; ₹57.50 simulated Reserve debit created order RS-260909-DS26TXM.
- Browser reload: actual order remains CAPTURED and remaining permission capacity is ₹4,942.50.
- Browser: manual test-mode handoff and reload into AWAITING_PAYMENT; corrected same-order resume opens Razorpay’s Test Mode iframe.

## Not verified / external blockers

- Razorpay’s external test iframe remained blank in the in-app browser (no browser console error). No actual test-provider success/failure was completed in that browser run. The manual attempt remains unresolved; no capture was invented or duplicate checkout created to bypass it.
- Physical microphone, speaker audibility and consecutive human-spoken turns remain unverified; user was asked to perform the two-turn check.
- Seven older wire tests skip because they target the removed storefront path. Current frontend transport tests run against the current client; the skipped legacy tests are not counted as passes.
- Merchant remains a simulation; Gmail rebuild and profile/address persistence are outside this task.

## Phone-free Standard Checkout verification

- Official integration documentation supports `hidden.contact: true`: https://razorpay.com/docs/payments/server-integration/nodejs/integration-steps/?preferred-country=IN
- The feature documentation additionally requires contacting Razorpay Support to enable optional phone collection on the account: https://razorpay.com/docs/payments/payment-gateway/features/?preferred-country=IN
- Shared `openRazorpay` sets this option for both initial handoff and existing-order resume, without a fabricated contact prefill.
- Frontend transport suite now passes 24 tests. Three new provider-stub tests verify hidden contact, unchanged order/amount/currency, and dismissal/callback/failure outcomes including a subsequent dismissal. These are adapter tests, not real provider payment completions. TypeScript and diff checks pass.
- Reopened the existing AWAITING_PAYMENT through its resume button. Razorpay's Test Mode iframe again appeared blank. Its URL was `https://api.razorpay.com/v1/checkout/public`, with a visible 1280 x 720 frame, display block and opacity 1. Browser warning/error logs were empty; no application iframe CSS override was found.
- Reloaded after inspection. No second payment was initiated. Account-level phone-free enablement and real hosted payment completion remain unverified; the blank iframe does not establish whether optional contact is enabled.

## Active-tree takeover audit

- Backend working directory is the main project checkout, branch `main`; baseline commit `d4e1846`. The separate frontend repository is `apps/razorsharp-concept`, branch `main`, baseline `468ad69`. Do not stage that nested repository as a parent gitlink.
- All 13 other registered backend worktrees were clean. Six are ancestors of main. Seven have one side-only commit each: incomplete agent/voice runtime checkpoints or explicitly historical leftovers (scenario faults, proposal tests, old buyer-web dependencies, and launch configuration). None of those seven commits changes the current refund authorization boundary. No historical worktree was merged or deleted.
- Commit `bc1763f` removed typed/voice direct refund initiation from the old buyer UI. It did not remove the buyer-authorized HTTP refund endpoint. The distinction is verified from the commit and current source, rather than inferred from a change summary.
- Current source still grants `refund.request` to BUYER and uses it for `POST /v1/orders/{order_id}/refunds`. Merchant capabilities do not grant financial execution. This conflicts with the settled buyer-escalation / merchant-approval product flow and remains an explicit pending change; existing worker safety does not repair this API authorization mismatch.
- Fresh isolated runs: 23 payment API tests passed; 23 refund/webhook worker tests passed. The initial combined invocation failed collection because both packages import an unqualified `conftest`; the successful isolated runs are the evidence, not the failed combined invocation. These tests use test fixtures and do not establish hosted Razorpay payment completion.

## Merchant-approved refund implementation (supersedes pending authorization finding)

- Buyer sessions no longer receive `refund.request`. Buyer support-case open/read uses `support.case.open`; an existing buyer session carrying the retired name is normalized to escalation-only access when authenticated. The financial endpoint requires `merchant.refund.approve` and an actual MERCHANT actor owning the checkout's merchant.
- `GET /v1/orders/{id}/refundable` supports the owning merchant and returns the Kernel balance plus a canonical `approval_hash` binding the order, immutable checkout content, Policy-at-Sale Receipt hash, balance, currency and refund-window state. Buyer reads remain read-only.
- `POST /v1/orders/{id}/refunds` requires explicit `amount_minor`, `case_id`, `reason` and `approval_hash`. The support case must belong to the same order and merchant, and its reason must match. The Kernel's lock-backed balance is reread before admission; a stale review is HTTP 409. No price/refund arithmetic or financial admission logic was replaced in the Kernel.
- The exact approved request is recorded through existing idempotency machinery. Same-key replay returns the original refund without another command; changed payload is refused. Existing refund grants, outbox execution, provider evidence and unknown-outcome reconciliation remain in use. Marking a case resolved does not itself initiate a refund.
- Merchant support UI now has a separate refund review, explicit amount approval and status read. Unknown transport outcomes preserve the approval and key across reloads for same-request retry. The merchant proxy only adds order-specific review/refund endpoints and still requires its own authenticated cookie and same-origin mutations.
- A simulated Reserve provider payment (`sim_pay_...`) is explicitly refused by this Razorpay refund admission path. This does not implement simulated Reserve refunds.
- Corrected the adapter's provider header to **`X-Refund-Idempotency`**, the current documented header: https://razorpay.com/docs/api/refunds/normal-refunds-idempotent/?preferred-country=IN . Existing stored refund keys are preserved. An explicit test asserts the literal documented header, not only the shared constant.
- Validation: 102 tests across payment, review, listing, support reads, order isolation, merchant identity and order duration passed; a subsequent 29-test payment run added and passed the legacy-buyer-session regression (103 distinct tests across that group). Another 51 capability/adversarial tests, 55 refund-adapter tests, 23 worker/webhook tests and 26 frontend tests passed. TypeScript, Ruff, diff checks and frontend production build passed.
- Restarted the API and action worker from this checkout with test-mode credentials. Live API smoke: BUYER refund attempt returned 403; unreviewed MERCHANT refund returned 422. Read-only authenticated Razorpay Orders, Payments and Refunds collection requests each returned HTTP 200.
- Browser smoke reached Customer Support Desk and verified the unauthenticated merchant gate. Authenticated UI approval and an actual captured Razorpay payment/refund round-trip remain unverified. The previously blank hosted Checkout remains a separate blocker; no unrelated provider payment was refunded to manufacture a successful demo.
# Manual payment and recovery retest — 2026-09-10

- Actual in-app browser: Razorpay Test Mode now renders its contact form and the correct ₹57.50 amount. The previous blank iframe did not reproduce. It still requests a mobile number despite `hidden.contact: true`; no account-level optional-contact enablement is claimed.
- Returned from provider checkout without a payment and refreshed `/shop`: cart restored, review recovered `AWAITING_PAYMENT`, and the same Razorpay checkout could be resumed. No successful order was fabricated.
- Temporarily stopped the local API and explicitly requested resume: the UI displayed `Commerce backend is unavailable. No payment confirmation received.` After restarting the API, polling cleared the error and resume opened the provider again.
- Read-only DB check after reload/outage/resume: checkout `01a08870-1f7a-7833-a46d-f2963db19200` still has exactly one attempt, `01a08870-d410-72ee-b445-1e0695d58861`, with provider order `order_Ta7C5DSpczvp5y`, amount 5750, status SUBMITTED. Razorpay order-payments GET returned HTTP 200 and zero payments.
- Frontend regression suite: 32 passing, including added delayed confirmation, interrupted settlement preserving pending approval, backend terminal failure, and ignoring another cart's confirmed order. Provider event tests use a scripted Checkout constructor; they are not hosted payment success/failure evidence.
- Payment API suite: 29 passing. Worker reconciliation/webhook suites: 29 passing, using an isolated real database and scripted provider responses. These include late provider evidence and duplicate-delivery protections.
- Still blocked at hosted contact entry: real test-mode success, decline and delayed capture through the complete browser flow. Asked the user to enter contact on the provider surface; no phone data was invented and no real payment was made.

## Captured Razorpay test payment and processed partial refund

- Hosted Test Mode netbanking completed: provider payment `pay_Ta8kjxVOd9jadF`, provider order `order_Ta7C5DSpczvp5y`, INR 57.50, captured=true. Backend order `01a088c5-ae65-797a-b4eb-b6ab63caf1c2`, reference `RS-260910-2EWNWHD`, was confirmed from PROVIDER_FETCH after the browser callback. The browser displayed the payment acknowledgement, correct payment IDs, netbanking and Test Mode label. Download acknowledgement was clicked.
- Buyer case `01a088c7-7bee-7ddb-a8f6-a8a9888b38f4` was opened through the buyer API using the store-supported `item_damaged` reason. Merchant session fetched the current approval hash and approved exactly INR 10 through the refund endpoint. This was an API-level merchant approval test, not an authenticated Merchant Command browser test.
- Provider refund `rfnd_Ta8o6TKt5V4YlS` returned processed for INR 10. Its initial POST returned pending, exposing a worker gap: pending results had no reconciliation follow-up. Added bounded reconciliation for pending results and exact GET /refunds/{id} verification when the refund ID is known. Payment aggregate refund totals cannot identify an individual refund.
- Recovered the existing pending test refund through Kernel record_refund_result plus an outbox reconciliation command. No second refund POST was sent. Updated worker completed `refund_verified_processed`; buyer history displayed PARTIALLY_REFUNDED.
- Corrected buyer support reason values, recovered-order propagation to the parent confirmation UI, stale checkout assistant guidance, acknowledgement currency formatting, and misleading no-capture wording after provider load failures.
- 34 frontend tests and 38 worker refund/reconciliation/webhook tests pass; TypeScript and frontend production build pass. Kernel implementation unchanged.
- Intermittent provider blank rendering still has no confirmed root cause. A distinct hosted decline and deliberate refresh during bank processing are not yet verified; earlier API outage/reload recovery and scripted failure tests must not be mistaken for those.

### Hosted decline result

- Separate INR 57.50 test checkout `01a088d1-2cf0-728f-8b52-b3383dd8849e`, attempt `01a088d1-e54e-7476-a3bd-02e50cfcf42d`, provider order `order_Ta905rvwB985LG`.
- Razorpay payment `pay_Ta92CjyGXEzlzE` returned status failed, captured=false, error_reason payment_failed. Hosted UI reported bank decline; manual UI showed the provider failure description.
- FAIL: local attempt stayed SUBMITTED because failed browser events do not schedule provider reconciliation, unlike signed successful callbacks. With no local webhook delivery this leaves verification waiting and safe retry unresolved. Do not label the failure/retry vertical path complete.
