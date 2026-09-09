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
