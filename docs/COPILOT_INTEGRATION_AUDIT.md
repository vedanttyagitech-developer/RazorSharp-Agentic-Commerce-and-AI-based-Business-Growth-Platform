# Copilot integration verification — 12 September 2026

Verified against the local mounted demo at localhost:8000. This is not a production deployment or an exhaustive certification of every possible natural-language request.

## Changes and evidence

| Surface | Corrected behavior | Verification |
| --- | --- | --- |
| Merchant Command | Copilot drafts refresh the action list; execution refreshes catalogue and insights | Browser prompt created a one-unit milk stock receipt. Request approval → approve exact proposal → execute reached SUCCEEDED. Stock changed 444 → 445 without reloading. A separately approved adjustment restored 444. |
| Merchant conversation | Separate merchant identity/history and business suggestions; no shopping microphone masquerading as merchant voice | Browser merchant history no longer included buyer conversation. Regression checks distinct storage namespaces and session response keeps bearer token private. |
| Merchant progress | Loading follows the real request; Stop aborts transport; uncertain results instruct checking refreshed actions before retrying | Browser displayed “Waiting for the merchant assistant” and Stop while confirmed-sales request was pending. |
| Merchant Orders | Recorded orders, pagination and inspect use merchant APIs | Browser loaded backend order references and their recorded details. Illustrative orders are collapsed and labelled. |
| Operations | Removed local-only Safe Mode toggle and invented counters | Merchant links to the separate operator console. Role guards remain enforced by the backend. |
| Shopping catalogue | Suggestions resolve actual store products | “Find milk and bread” displayed two milk products and two bread products. |
| Shopping cart | Add, absolute correction, removal and ordinal reference use existing bound proposals | Browser added two Amul Taaza packets (₹56 subtotal, ₹85.50 quoted total), corrected to one (₹57.50 total), then removed it. “Doosra wala do packet cart mein jod do” selected displayed second product, Amul Gold, quantity two. |
| Hinglish correction | Added strict parsing for “Nahi, Amul Taaza milk sirf ek packet rakho” | Reproduced original non-action; new API regression and repeated browser test confirmed 2 → 1 with one cart line. Ambiguous and compound payment instructions stay outside this fast path. |
| Voice | Actual recognizer → grounded proposal → speech generation | Synthesized spoken audio sent over the live ticketed voice socket: exact milk-add transcript, matching SKU/quantity proposal and 371,280 audio bytes returned. Browser cart application is separately verified; this socket test alone does not prove a cart write. |
| Payment speech | Verified failure is spoken as failed; UNKNOWN stays unconfirmed | Voice guidance regressions cover PAYMENT_FAILED, INVALIDATED with FAILED attempt, and UNKNOWN despite a client “failed” hint. Frontend tests retain reconciliation/recovery protections. |

## Validation

- 185 frontend tests passed, including new history isolation and stale catalogue response regression tests.
- 688 focused merchant, agent, adaptive-shopping and voice tests passed.
- 26 mounted browser/API boundary tests passed.
- 42 multilingual intent tests passed, including the new real-cart correction regression.
- TypeScript, focused frontend lint, Python lint and mounted production build passed.
- Real browser flows used localhost only. No payment was submitted. Test inventory was restored through the approval workflow; test cart additions were removed.

## Explicit capability limits

- Merchant live voice is not implemented; the console offers typed merchant requests. Shopping voice is connected.
- Storefront placements are previews; WhatsApp remains unavailable. These are labelled, not presented as executed business actions.
- Physical microphone permission/device capture and audible speaker quality were not manually verified. Live generated-audio STT/TTS and automated lifecycle checks are separate evidence.
- No live Razorpay failure was induced in this run. Failure/unknown speech correctness was verified with regression fixtures, not a real charged payment.
- Production capacity, GCP deployment, all browser/device combinations, and exhaustive natural-language coverage are outside this verification.

## Backend capability UI completion

The subsequent implementation connects all 14 audited capability groups:

1. Comparison selects two products from live conversation results.
2. Typed and voice support proposals retain the owned order identifier and open an explicit review handoff.
3. Merchant drafts support editing, rejection and withdrawal; terminal failures can create a fresh draft requiring new approval. UNKNOWN cannot be blindly retried.
4. Listing and unlisting have typed proposal controls.
5. Offer start/end have discount and time fields, current-offer context and exact target checks. Execution is immediate; dates do not schedule activation/expiry.
6. Merchant action cards show actors, timestamps, exact hashes and applied before/after evidence.
7. Merchant support shows ownership and per-case handling notes.
8. Reserve permission review supports explicit selected-product scope.
9. Owned orders expose Policy-at-Sale Receipt and resolution evaluation; merchant reads retain merchant ownership checks.
10. Operator evidence explorer connects payment inspection, audit verification, retained revenue and checkout proof reads.
11. DEAD-command revival requires review and the exact original command identifier.
12. Metrics show actual process exposition with filtering, not invented business counters.
13. Protocol configuration and order interaction evidence have dedicated read panels.
14. Merchant/support queues have offset pagination; operator queues now also support Previous/Next pagination and backend-supported filters.

Merchant Policy drafts and actions broadcast the shared refresh event. Listing/offer execution remains behind exact approval. A newly discovered offer-end mismatch now fails without stopping a different running offer. Illustrative buyer practice orders are collapsed below recorded orders.

### Additional validation

- 191 frontend tests passed; 666 focused backend and voice tests passed (final expanded run).
- Browser verified merchant draft edit, submit and reject without publishing the listing.
- Browser verified refund-request handoff to its actual order, verified sale binding, resolution evaluation and truthful empty protocol-interaction state.
- Browser verified actual protocol pins, live metric series and payment inspector records.
- DEAD-command revival is covered by boundary tests with a stubbed worker call; no live command or payment was revived for this test.
- Local build and type/lint checks are recorded separately; no GCP deployment or physical microphone test is implied.

Final browser checks also verified the live comparison pair (Amul Taaza ₹28, 444 units; Amul Gold ₹73, 298 units), an intact audit chain, retained-revenue UNSETTLED evidence, selected-product Reserve review, and immediate Merchant Policy draft-list refresh. The new publication test draft was withdrawn; existing published terms remain unchanged. Replacement Reserve authorization is disabled while a live permission remains.

During the last browser pass the external reasoning service was unavailable for the multi-product request “Find milk and bread”; the UI displayed that limitation and did not change the cart. A single-product “milk” request returned live catalogue records. A subsequent fix added a bounded model-independent path for literal multi-product searches; the browser then returned two milk and two bread products for “Find milk and bread”. Complex constraints still retain reasoning and do not silently degrade to broad keyword results. This is not a successful model-driven multi-product test.

## Final remaining-gap pass

- Operator reconciliation, review and execution queues now expose exact `has_more` and offset pagination. Stable creation/id ordering separates pages. Reconciliation's findings filter applies within each scanned page; the UI keeps Next enabled when older attempts exist even if that page contains no findings. Page-scoped counts remain distinct from tenant-wide outbox counts.
- Literal multi-product searches (up to four searches, including Hindi/Hinglish) use the existing authorized catalogue executor without a model round. Budget, dietary exclusion, comparison and financial requests remain outside that shortcut. Displayed results fit the existing ordinal-reference context.
- Catalogue availability/listing flags now reach product cards and comparisons. Unavailable/unlisted items cannot be added through those controls; backend cart validation remains authoritative.
- Operator rows display readable provider status and observation time; execution status counts use labelled cards.
- Browser verified “Find milk and bread” returns four real catalogue products with an unchanged empty cart, reconciliation Page 2 loads older attempt identifiers, and current product facts load from the detail endpoint.
- Full Commerce API + voice suite: 1,437 tests passed. Final targeted discovery regression and 191 frontend tests also passed; TypeScript, focused lint and mounted build passed.
- `caffeinate -i -t 14400` was started for this work, and macOS reported its idle-system-sleep assertion active. It is time bounded and does not override closing the lid. No GCP deployment or payment was performed.
