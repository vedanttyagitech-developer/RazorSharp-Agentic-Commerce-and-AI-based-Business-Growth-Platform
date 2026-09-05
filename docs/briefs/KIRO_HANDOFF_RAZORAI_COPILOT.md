# Handoff: finish the RazorAI copilot (passes 2 and 3)

Written 2026-09-06 for whoever picks this up (Kiro). Everything below is grounded in the tree
at commit `6f4b1c0` on `main`. Read the "What exists" section before editing anything.

## Rules that apply to every step

- Do **not** `git push`. Commit locally, by explicit file path, and say when it is ready.
  The owner triggers the push.
- No sphere, globe, particle or dot animation anywhere, and no `three.js`. No new dependencies.
- Keep every test green and add tests for what you change.
  `apps/buyer-web` has **no** jest-dom: use `toBeTruthy()` / `textContent`, never
  `toBeInTheDocument()`. Call `cleanup()` in `afterEach` where the file already does.
- The lint rule `react-hooks/set-state-in-effect` is on: never call a state setter
  synchronously inside an effect. A `window.setTimeout(..., 0)` with cleanup is the accepted
  pattern. Hooks are always called unconditionally.
- Gates before every commit. Storefront (`cd apps/buyer-web`): `npm run lint`,
  `npm run typecheck`, `npm test`. Python (repo root): `uv run --no-sync ruff format <paths>`,
  `uv run --no-sync ruff check <paths>`, `make types`,
  `uv run --no-sync python -m pytest -p no:cacheprovider -q -o addopts= <test paths>`.
- Servers do not reload Python. After a `packages/voice-runtime` change restart the gateway
  (`set -a; . ./.env; set +a; VOICE_GATEWAY_ALLOWED_ORIGINS=http://localhost:3000
  uv run --no-sync python -m voice_runtime.gateway`, kill the old one by PID first). After a
  `packages/agent-runtime` or `commerce-api` change restart the API the same way
  (`uv run --no-sync uvicorn commerce_api.app:create_app --factory --host 127.0.0.1
  --port 8000 --workers 1`, with `.env` sourced, `SCENARIO_KEY=local-demo-scenario-key`,
  `WEB_CONCURRENCY=1`). Next dev on :3000 hot-reloads the storefront.
- Never kill Python by name: the durable worker matches `durable_worker.main`. Kill by PID.
- The kernel design stays: the model proposes, the buyer presses, the platform executes.
  Every press below is the buyer's, made with a button or with their voice, and every write
  goes through the same requests the shelf and the checkout page already send.

## What exists (commit `6f4b1c0`)

The RazorAI box (`apps/buyer-web/src/features/agent/razorai-panel.tsx`) is the AgentFlow live
view: a dark scene (`bg-[#05070E]/88 backdrop-blur-xl`, indigo radial ground glow, slate
text), a top-left mono state pill (RAZORAI · idle / connecting / listening / thinking /
speaking / text mode), ghost dock and close buttons, ONE conversation and ONE composer.
Voice-first: `apps/buyer-web/src/features/voice/voice-panel.tsx` owns the live session
(`useVoiceSession`, auto-start, mic live from the first tap, muted while she speaks), renders
the voice transcript (`live-transcript.tsx`) while the session is live and the written chat
(`features/agent/message-list.tsx`, with the proposal cards) when it is not. The composer's
edge lights with `edge-glow edge-glow-thinking` while a turn is in flight, `edge-glow-executing`
while she speaks, `edge-glow-listening` while listening. Layout is `"centre"` (over the
shelf) or `"side"`, remembered in localStorage; the box auto-opens only on `/` and not in a tab
where the buyer closed it (`features/agent/launcher.tsx`).

The spoken chain works and must stay intact: each non-deterministic `agent_reply` frame
carries `offer` (one product: `{sku, name, quantity, unit_price}`) and, new in `6f4b1c0`,
`items` (up to five `{sku, name, unit_price, stock_units, available}`, the product the
sentence leads with first). The reducer (`features/voice/transcript.ts`) stores `state.offer`;
a short spoken yes ("yes", "haan", "yes add that", "add two", "le lo"; a "no" anywhere
cancels) sets `state.affirmed` with the spoken count; `VoicePanel` calls `onAffirmed(offer)`;
the panel's `takeOffer()` creates a basket if needed, `api.setLine`, `basket.refresh()`,
`api.openCheckout`, then `window.location.assign('/checkout/{id}?voice=1')`. On that page
the approval card reads itself aloud (`autoRead`), a spoken yes presses Approve through
`features/voice/voice-consent.tsx` (which today creates its own session), the approved
version is auto-submitted, and `features/checkout/payment-panel.tsx` opens the Razorpay
sheet (`autoOpen`). `items` is on the wire but the page does not read it yet.

Motion classes already defined in `apps/buyer-web/src/app/globals.css`, inside
`@layer components` (utilities must keep winning; never set `position` on the fixed dialog):
`.bubble-enter`, `.card-enter` (honours an inline `animation-delay`), `.cart-line-enter`,
`.cart-pulse` (700 ms one-shot), `.breathing-dot`, `.edge-glow`, `.edge-glow-ring`,
`.edge-glow-thinking`, `.edge-glow-executing`, `.edge-glow-listening`; all collapse to
opacity-only under `prefers-reduced-motion`. Tokens: `--color-primary #4f46e5`,
`--color-accent #4fd9f2`. State colours: idle `#7C8FF5`, connecting `#93A5FF`, listening
`#4FD9F2`, thinking `#B08CFF`, speaking `#FFA14D`, order `#10b981`, reserve `#f59e0b`.

Other seams: `features/basket/use-basket.ts` returns `{ basketId, basket, quantities, names,
loading, error, busySku, add(sku), setQuantity(sku, qty), reload }`; the panel holds it as
`basket`. `primaryImage(sku)` in `lib/product-images.ts`. `<Amount money={...}/>` renders a
`Money {minor, currency, display}`. Types in `lib/api/types.ts`. `lib/security/csp.ts`: the
default policy allows no external script and `frame-src 'none'`; `checkoutPolicy` adds
Razorpay's script/API/frame origins for `/checkout/*` only; `requiresOwnDocument(href)` forces
a document navigation into `/checkout`. The reference for look and feel is
`/Users/vedanttyagi/Desktop/AGENT FLOW/agentflow/apps/web/src/components/live/`
(`LiveView.tsx`, `ChatBox.tsx`) and its `index.css`; the permission-slip look is
`ChatBox.tsx` lines ~139-200 (dark card `rounded-lg border border-white/12 bg-slate-900/85`,
mono uppercase tier badge, bold title, filled "Allow" and ghost "Deny").

## Pass 2 (remaining): items, cart and a stage-aware screen

### 2a. Product cards in the conversation (both paths)

- `features/voice/wire.ts`: `agent_reply` gains optional `items` (zod array of
  `{sku: string, name: string, unit_price: unknown nullable, stock_units: int nullable,
  available: boolean default true}`), matching the gateway field for field.
- `features/voice/transcript.ts`: store `items` on the assistant entry that carried them.
- New `features/agent/product-cards.tsx`: a horizontal, snap-scrolling row of compact dark
  cards: photo (`primaryImage(sku)`, 96 px, rounded, object-cover), name (two lines max),
  price (`<Amount/>`), and a round accent "Add" press calling `onAdd(sku)`. Unavailable items
  show "Out of stock" and no press. Each card has `card-enter` with a staggered inline
  `animation-delay` (`index * 60 ms`).
- `live-transcript.tsx`: render `<ProductCards items onAdd/>` under the assistant bubble that
  carried items. `VoicePanel` accepts `onAdd(sku)` and passes it through; the panel passes
  `basket.add` (the shelf's own write).
- `message-list.tsx`: render the same cards for a razorai message whose `turn.structured` is
  kind `products` (`hits[]`) or kind `product` (the product's fields spread flat), above any
  proposal card. Keep ProposalCard, DenialCard and ToolChip rendering.
- Tests: `product-cards.test.tsx`; assertions in the transcript and message-list tests.

### 2b. The live cart strip

- New `features/agent/cart-strip.tsx`, mounted in the panel between the conversation and the
  composer, reading the panel's `basket`: when there are lines, a scrolling row of chips
  (28 px thumb, name, quantity with − / + presses calling `setQuantity(sku, qty ± 1)`),
  the total (`<Amount/>` inside a span that gets `cart-pulse` for 700 ms whenever
  `total.minor` changes; use a keyed re-render or a timer with cleanup, never a synchronous
  setState in an effect), and a "Checkout" press that opens the checkout the way the panel's
  existing `confirmCheckout` / `api.openCheckout` does (until pass 3 lands it may navigate to
  `/checkout/{id}`, with `?voice=1` when the voice session is live). Empty basket: one quiet
  line "Your cart is empty. Ask for something, or say yes to an offer." New lines get
  `cart-line-enter`. Dark styling throughout (slate text, `bg-white/[0.06]`,
  `border-white/10`, `rounded-xl`).
- Test: `cart-strip.test.tsx`.

### 2c. The screen changes with the order stage

- New `features/agent/use-order-stage.ts`: derive `{ stage, checkout }` where `stage` is
  `discover | cart | approve | pay | order | reserve`. Inputs: `usePathname()` (mock
  `next/navigation` the way sibling tests do), `basket` (lines → at least `cart`), the panel's
  inline checkout when pass 3 exists, and on `/checkout/{id}` a poll of `api.checkout(id)`
  every 2.5 s while on that page (stop on unmount; timer-based; no synchronous setState):
  `APPROVAL_REQUIRED → approve`; `APPROVED | EXECUTION_PENDING | AWAITING_PAYMENT |
  PAYMENT_UNKNOWN → pay`; `PAID → order`; `/orders/{id} → order`; `/reserve-pay → reserve`;
  otherwise `cart` if lines else `discover`.
- New `features/agent/stage-rail.tsx`: under the pill header, five steps Discover · Cart ·
  Approve · Pay · Order (a sixth chip "Reserve Pay · simulator" only in the reserve stage),
  mono uppercase labels with a dot; the active dot breathes in the stage colour, past steps
  dimmed and ticked, a thin progress line filling to the active step with a 400 ms transition.
  Add `.stage-*` rules inside `@layer components`; reduced motion honoured.
- New `features/agent/stage-scene.tsx`, between the rail and the conversation, one or two lines
  per stage: discover "Ask for anything, or say what you need"; cart "Ready? Say checkout, or
  press Checkout"; approve: the checkout's version and amount plus "Say yes to approve";
  pay "Approved. Razorpay's sheet is next" with the Razorpay order id when known; order
  "Paid. Track it on the order page" linking `/orders/{id}`; reserve "Reserve Pay is a labelled
  simulator; nothing moves money" linking the page's controls. No forms here.
- Placement: on `/checkout/*` and `/reserve-pay` force the `"side"` layout regardless of the
  stored preference so the page's trusted controls are never covered; restore the preference
  elsewhere; the manual toggle keeps working. The launcher also auto-opens, docked, when
  arriving at `/checkout/{id}?voice=1` (the spoken flow continuing); keep home-only auto-open
  and the per-tab dismissal otherwise.
- Tests: `stage-rail.test.tsx`, `use-order-stage.test.ts`.

## Pass 3: permission at every step, approval and payment inside the box

### 3a. Let Razorpay load where the copilot runs (owner's decision, recorded)

- `lib/security/csp.ts`: move Razorpay's script and frame origins into the default policy
  (keep `'strict-dynamic'` + nonce semantics and every other directive exactly as they are),
  keep `checkoutPolicy` as a no-difference alias so callers compile, make
  `requiresOwnDocument` return `false` (keep the export; document why). Update `csp.test.ts`
  to pin the new policy. Add a short KNOWN_GAPS item: Razorpay's loader now runs on every
  route (was `/checkout/*` only) so the copilot can take payment in place; the least-privilege
  posture of ADR 0003 D8's surface is narrower by one origin; revisit by scoping the policy to
  routes that mount the copilot if that set ever shrinks.

### 3b. One voice session for everything

- `voice-consent.tsx` gains an optional `session` prop (the controller
  `use-voice-session.ts` returns). When given, it uses it instead of creating its own
  (hooks stay unconditional: an option that makes the internal `useVoiceSession` inert, or an
  inner component). `VoicePanel` gains `onSession(controller)`, called once. Result: one
  microphone, one socket, even with the approval card embedded in the box.

### 3c. Permission slips

- New `features/agent/permission-slip.tsx`: `{tier: LOW|MEDIUM|HIGH, title, detail?,
  allowLabel, onAllow, onDeny, busy?}` in the reference look; tier badge colours LOW emerald,
  MEDIUM amber, HIGH rose; Enter allows, Escape denies. Test it (mouse and keyboard).
- The panel keeps a pending-permission state `{kind: add | checkout | pay, ...}`. A spoken yes
  to an offer, or an Add press on a card, no longer writes immediately: it shows a MEDIUM slip
  "Add {qty} × {name} for {price} to your cart?". Allow → `api.setLine` (the existing write),
  the cart strip updates, then a MEDIUM slip "Open checkout for {basket total}?". Allow →
  `api.openCheckout` → the inline checkout journey (3d) shows the approval card: the HIGH step
  is the card itself. Once approved and auto-submitted and the checkout is `AWAITING_PAYMENT`
  with a `razorpay_order_id`: a HIGH slip "Pay {amount} with Razorpay?". Allow → the embedded
  PaymentPanel's `pay()` opens the sheet in this page, no navigation.
- Spoken words: while a slip is pending, `transcript.affirmed` is Allow for that slip and must
  NOT also take the offer; a spoken "no"/"nahi"/"mat" is Deny. Deny on add writes nothing; on
  checkout stays in cart; on pay stays approved with Pay available on the strip.
- The stage scene shows the pending slip's one-line summary.

### 3d. The approval and the payment in the box

- `checkout-journey.tsx` gains `embedded?`, `voiceFlow?` (overrides the `?voice=1` read),
  `session?` (threaded to ApprovalCard → VoiceConsent), `onState?(checkout)`. Embedded, it
  renders the approval surfaces (the card with spoken consent + Approve/Reject, refusals and
  superseded versions, the "submitting" banner) and the PaymentPanel inside the box, with the
  panel's opening deferred until the pay slip is allowed. The non-embedded checkout page stays
  byte-for-byte as it is (`autoRead`, auto-submit, `autoOpen`).
- `razorai-panel.tsx`: hold `checkoutId` state. `takeOffer` and the checkout slip's Allow set it
  instead of navigating. `checkout-proposal-card.tsx` gains optional `onOpened(checkoutId)`;
  with it the panel opens the inline journey; without it the card navigates as today. Render
  `<CheckoutJourney embedded voiceFlow={voiceLive} session={controller} checkoutId
  onState={...}/>` in the conversation area, framed as the trusted surface it is (keep the
  card's own "You are approving this. RazorAI cannot." label; add a mono TRUSTED SURFACE label
  and a `#B08CFF` edge ring). Clear `checkoutId` on cancelled / rejected / expired.
- `use-order-stage.ts` takes the inline checkout so the rail shows Approve while the card is up,
  Pay once submitted, Order when paid.

### Verification checklist (refute each by reading code; fix what survives)

a. Every step asks: add, checkout and pay each show a slip; nothing is written or opened before
   Allow or a spoken yes routed to that slip.
b. A spoken yes while a slip is pending allows that slip and does not also take the offer; a
   spoken no denies.
c. Exactly one voice session exists with the approval card embedded.
d. The embedded card's spoken yes uses the same `consent_recognised` matching (hash, amount,
   currency, version) and the same approve request as the page.
e. The pay slip's Allow calls `pay()`, and Razorpay's loader can load on the home route under
   the new CSP; nonce and strict-dynamic semantics unchanged; other directives unchanged.
f. The checkout page variant is unchanged.
g. `takeOffer` no longer navigates; the proposal card navigates only without `onOpened`.
h. The rail follows the stage: discover / cart / approve / pay / order / reserve, and the box is
   docked to the side on `/checkout/*` and `/reserve-pay`.
i. Cards render on both paths with photos and an Add press that is `basket.add`; the cart strip
   shows lines, ± steppers, a pulsing total and Checkout.
j. Hooks unconditional; no synchronous setState in effects; no new dependencies; no sphere,
   globe, dots or three.js; dark styling throughout the new components; reduced motion
   collapses every animation; nothing sets `position` on the fixed dialog.
k. KNOWN_GAPS records the CSP widening.

Commit locally by explicit path when each pass is green. Do not push.
