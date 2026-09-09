# Frontend/backend integration audit — 10 September 2026

Scope: `apps/razorsharp-concept`, its two API bridges and their commerce/voice backend counterparts. Source audit plus bounded runtime checks; not an exhaustive security audit or a complete end-to-end certification. Existing uncommitted agent-runtime, order and listing changes were left intact.

## Runtime evidence

### Follow-up integration checkpoint

### Durable-cart wiring checkpoint

The shopping surface now restores the backend cart, serializes button/voice changes through one writer, and renders returned quantities and quote totals. Unknown write outcomes retain the same absolute quantity and idempotency key for retry. Review reuses that cart rather than copying it into another cart. Initial commerce requests share one session bootstrap to avoid independently minting buyers in the same page.

Buyer agent turns without explicit cart context resolve that buyer's open cart, scoped to tenant and merchant. HTTP and voice proposal projection preserve the original cart ID, absolute quantity and expected-state binding. The Kernel still decides whether a checked-out cart can be edited; this change adds no financial authority.

Validation: 32 buyer-agent tests, 34 voice offer/client tests and 19 frontend transport/controller tests passed; TypeScript and production build passed. A live isolated buyer restored quantity two through the frontend bridge, received a model proposal for quantity three on the same cart, and applied it with its binding. Test items were removed without creating checkout or payment. Browser reload interaction, simultaneous tabs, physical microphone and payment recovery remain to be exercised in the later UI validation step. No claim of full seven-step completion.

The gap table below records the initial audit, not the current completion state. Since that audit:

- Typed shopping now calls the HTTP agent endpoint when no voice socket is connected; it does not replay an accepted socket turn. Structured backend products and explicit basket proposals drive the response.
- Buyer order history, order evidence and support submissions use backend records. Merchant support and action approval/execution use a separate authenticated local demo bridge. Buyer support remains escalation, without buyer refund approval or execution.
- Merchant Policy editing creates Controller publication proposals. The action workflow uses backend revisions and hashes.
- Merchant overview/growth now reads `/v1/merchant/insights`, scoped by both tenant and merchant, grouped by currency. It shows confirmed order value before refunds, not profit or campaign-attributed growth. Local frontend login, insights read and logout each returned 200 after the API restart.
- The two-turn generated-speech integration test passed against configured speech services and the local API in one session. A browser rendered five milk products and entered Listening. Physical microphone multi-turn success remains unverified.
- Fifteen focused frontend transport tests pass; TypeScript and the production build pass. Two database-backed insights tests verify recorded amounts and buyer denial. These checks do not establish comprehensive tenant isolation or a complete end-to-end release gate.

Still open: durable cart continuity across voice/manual/reload paths, merchant order scope before wiring its list, merchant conversational agent wiring, campaign delivery, account persistence, and broader payment/recovery/browser testing. Local previews in other merchant sections must not be presented as connected workflows.

The sandbox shell could not connect to localhost even though processes were listening. Outside that sandbox, frontend `/`, backend `/healthz`, backend `/v1/config` and frontend `/api/commerce/catalogue/products?limit=1` returned HTTP 200. This was tool isolation, not evidence of a server outage. No payment, refund, campaign send or merchant mutation was executed during this audit.

## Verified transport fixes

1. Voice ticket startup: a missing buyer cookie returned 409; an expired session could return 401. The voice client now asks the existing commerce bridge to initialise/recover the session through `carts/current`, then retries the ticket once. Identity minting stays in the commerce bridge. Infrastructure failures are not retried by this recovery path.
2. Reserve history: `Promise.allSettled` previously discarded every rejected lookup, presenting auth/server outages as an empty or incomplete purchase history. Only 404 is now treated as a non-Reserve order; other errors reach the existing error display.
3. Unknown mutation outcome: shared commerce transport no longer claims “Nothing was charged” when fetch fails. A server may have committed before the connection failed; the message now asks users to check status before retrying.
4. Voice ticket bridge now refuses cross-origin writes before requesting a ticket.

Nine focused mocked transport tests pass in `apps/razorsharp-concept/tests/integration-transport.test.mjs`. These verify recovery ordering, error preservation and origin rejection, not live gateway audio. TypeScript and production build pass.

## Confirmed integration gaps

| Priority | Feature | Current frontend | Backend / boundary | Required completion |
|---|---|---|---|---|
| P0 | Typed fallback when voice service fails | Shopping `send` uses `voice.say`; typing shares the gateway dependency | `/v1/agent/turn` exists and is buyer-proxied | Implement an explicit HTTP fallback with conversation identity and ambiguous-outcome handling. Do not automatically resend an acknowledged socket turn. Current “typing still works” copy overstates independent availability. |
| P0 | Support escalation | `components/support-cases.tsx` stores cases and demo resolutions in localStorage | Buyer order support-case routes exist; `/v1/support/cases` is an operator queue requiring scenario key and capabilities | Wire buyer submissions to their order; add a separately authenticated merchant queue. Never put scenario/operator credentials into the buyer bridge. |
| P0 | Merchant approvals | `app/merchant/page.tsx` changes local action state and sample revisions | `/v1/merchant/actions` provides draft/read/edit/submit/approve/reject/cancel/execute | Separate merchant session/bridge and typed action responses; use canonical backend hashes and execution results. A buyer cookie cannot become merchant authority. |
| P1 | Buyer order history and tracking | Shop uses an in-memory order snapshot and sample timeline | `commerce.orders.list/read` and checkout timeline/events routes exist | Restore actual buyer orders on load, track by persistent IDs, consume backend status; retain unknown/pending state. |
| P1 | Proof surface | Shared `Proof` component renders illustrative fixed events; other Kernel UI may exist separately | `/v1/checkouts/{id}/proof` and committed-row verifier exist | Wire each order's proof action to its actual checkout; show verifier tiers, missing evidence and recomputation failures. Audit every entry point, not just one screen. |
| P1 | Cart continuity | UI basket is local; authoritative bill creates/synchronises a backend cart at review | Backend supports `carts/current`, line writes, checkout recovery | Decide and implement a single durable cart lifecycle; reconcile reload, voice proposals and manual additions without duplicate writes. |
| P1 | Merchant Policy editing | Preview action UI | `/v1/merchant/policy` exists | Bind editor and publication to merchant permissions and versioning, then surface checkout reapproval deltas. |
| P1 | Merchant insights and orders | Sample dashboard/order records coexist with live catalogue reads | Source contains merchant/domain routes, but no proof all displayed metrics have a corresponding aggregate endpoint | Inventory each metric; wire only to authorised deterministic reads. Do not replace sample numbers with model-generated numbers. |
| P1 | Protocol/config status | Homepage pins/status are static code-grounded explanations | `/v1/protocols`, `/v1/config`, inspector surfaces exist | Add appropriately scoped runtime status reads; maintain public claim boundaries. Do not proxy privileged inspector endpoints indiscriminately. |
| P2 | Gmail campaigns | Subject/audience/approval/result are a simulated local workflow | No production delivery integration was established by this audit | Verify backend connector availability before wiring; require audience snapshot, approval, dedupe and provider evidence. No external messages authorised in this audit. |
| P2 | Profile, address and storefront campaign | Browser-local settings and campaign preview | Persistence/service coverage not yet verified | Establish validated backend contracts before claiming cross-device/account persistence. |

## Additional risks requiring validation

- Cold-start parallel requests may race to mint different demo buyers when no identity cookie exists. A single canonical session bootstrap should precede protected feature loads. The one-retry voice recovery is not proof this concurrency risk is eliminated.
- The voice WebSocket URL is derived from `VOICE_GATEWAY_URL`. A server-internal host is not necessarily browser-reachable in deployment; verify public WSS routing, origin allowlist and TLS separately.
- Buyer bridge currently allowlists `/support/cases`, but the target router is operator-gated. It cannot supply a buyer-wide case list merely by forwarding that path.
- Reserve history currently starts from 25 confirmed orders; pending attempts and older pages are not a complete history. The UI comment already acknowledges unsettled attempts. Add pagination/recovery rather than implying completeness.
- Reserve transport still has a random-key default. Review call sites before removing it: stable keys must represent logical actions, including retries, rather than being generated per transport attempt.
- A successful source build does not validate model credentials, gateway speech, payment callbacks, worker reconciliation, concurrent approval changes or browser rendering.

## Completion sequence

1. Session bootstrap and a single truthful shopping conversation controller for voice/text.
2. Durable order/cart restoration and actual evidence/timeline entry points.
3. Buyer support escalation plus separately authorised merchant support queue.
4. Merchant action and Merchant Policy integration using existing controller contracts.
5. Deterministic merchant metrics and runtime protocol/status views.
6. Governed campaign integration after connector availability is verified.

Each slice needs contract tests plus a live happy path, refusal/error path, reload/recovery path and tenant/role isolation check. Kernel financial semantics, buyer authority and existing payment controls must not be relaxed to make a screen work.
