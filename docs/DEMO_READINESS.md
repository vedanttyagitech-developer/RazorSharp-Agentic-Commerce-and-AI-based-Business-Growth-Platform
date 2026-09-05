# Demo readiness

Written 2026-09-05, late afternoon, against the running stack: API on `:8000` (pid 37509,
started 11:51:39), storefront `:3000`, console `:3001`, database `commerce_dev`. Every claim
below was checked against that stack or against current source; where the two disagree, the
document says which.

One environment fact shapes everything that follows. **The API process is running code from
this morning, before the big merge.** Both front ends run under `next dev` and are current.
So a feature that exists in `packages/` may be invisible on `:8000` — twice today that looked
like a defect and was not. The first line of the fix list is therefore not a code change.

The short version: the platform underneath this product is genuinely good and held up under
adversarial re-testing. The weaknesses are almost entirely on the two screens a judge actually
looks at — the buyer's consent moment and the merchant's money — where the honesty discipline
that governs the kernel has not yet been extended outward. Most of them are one-line fixes.

---

## 1. What works

Lead with these. Do not refactor them today.

**The kernel refuses correctly, under real interference.** During the audit another session
was moving catalogue prices underneath a live checkout. One of the audit's own checkouts came
back `REAPPROVAL_REQUIRED` — refused because the approved version no longer described the
world. Nobody staged that. A system that refuses a stale approval when the drift is *not*
scripted is worth far more than one that refuses on cue.

**Order creation against Razorpay is live.** A real order, `order_TYKx4L0hcGbNNe`, was created
against Razorpay this session, HTTP 200. This is not a fixture. Say it plainly and show it.

**The proof chain verifies link by link.** It was walked end to end and every link held. The
version record is immutable and the approval genuinely binds to specific bytes.

**Grounding of money is airtight.** `packages/agent-runtime/src/agent_runtime/grounding/postcheck.py`
was run against the live source with a hostile input: `"Amul Butter costs Rs 999.00"`, an
amount nothing in the ledger supports, came back `rewritten=True` with the reply stripped. The
agent cannot state a rupee figure the system did not compute. That is the core claim and it is
true.

**Capability denial is real and visible on the wire.** Specialists carry distinct principal
ids and typed tool calls; a tool a specialist does not hold is denied rather than quietly
allowed. This is demonstrable live.

**The refusal path in the growth engine is better than most production systems.**
`packages/commerce-api/src/commerce_api/services/proposals.py:283-286` refuses to describe a
product as a top seller because doing so "would be inventing the one fact that makes the lever
urgent." The system declines to manufacture urgency, in code, with the reasoning written down.

**The reconciliation backend is complete.** `GET /v1/review/reconciliation` returns 39 payment
attempts with `attempts_used`, `attempts_bound` and `next_scheduled_attempt` per row. The data
is there and correct; only the console tab is missing.

**The project already writes honestly about its own gaps.** `docs/STATUS.md` and
`docs/KNOWN_GAPS.md` use exactly the register the rest of the submission needs — ACP, MCP and
platform observability are described in terms of what is wired and what is not. That habit is
an asset in the room; extend it, do not soften it.

---

## 2. What to fix, ranked

Times are honest working estimates for someone who knows the file. Total above the line is
roughly three hours, which is what is left today.

### Do this first, and it is not code

**0. Restart the API. (2 minutes, unblocks two headline features.)**
The process has been up since 11:51:39 and the merge landed at 11:54. Restarting picks up two
things that are currently committed but dead on `:8000`:

- The shopping specialist's basket-write tools. `packages/agent-runtime/src/agent_runtime/capabilities/registry.py:125-133`
  binds `basket_create`, `basket_set_line`, `basket_get`, `present_products`, `present_basket`.
  The live process reports only `basket.read`, `catalog.get_product`, `catalog.search`. So
  "add 2 dahi to my basket" currently searches, reports success, and adds nothing. This is
  RazorAI's single most demo-critical action.
- The Copilot's proposal branch. `agent_service.py:1251` routes the console's own suggested
  prompt to `_growth_restock`, which renders the proposal card that carries the entire
  "agents propose, humans apply" claim. Live it returns a bare anomaly list.

Restart, then re-test exactly those two before touching anything else. Everything else on this
list was measured against the stale process.

### The six that change what a judge concludes

**1. The normal route to payment dead-ends at a sheet that never opens. (10 minutes.)**
`apps/buyer-web/src/features/basket/basket-view.tsx:92`

Walking the real UI — ADD, basket, Proceed, Approve, Pay, Pay — ends at *"The payment
provider's script could not be loaded."* The mechanism was isolated with an A/B on the same
URL: loading `/checkout/{id}` directly leaves `window.Razorpay` a function; arriving there via
`router.push` from `/basket` leaves it `undefined`. CSP is a property of the document, and a
soft navigation never replaces the document, so the checkout policy computed in
`src/middleware.ts:23` is never applied. The kernel does its job — a genuine Razorpay order is
created — and then the money path stops at a blank error with no cause and no retry.

Fix: in `proceed()`, replace `router.push` with a `window.location.assign` to the same path.
One line, forces a document load, middleware runs. Do **not** widen the base policy; the
scoping rationale in `csp.ts:95-99` is correct. Add "try refreshing this page" to the error
text while you are in there.

**2. The approval screen cannot say what you are buying. (30 minutes.)**
`packages/commerce-api/src/commerce_api/services/checkout_service.py:377`

`POST /baskets/{id}/checkout` returns a full quote with line items. `GET /v1/checkouts/{id}`
returns `approval_card.quote = null`, because it is hardcoded `quote=None`. The storefront only
ever renders the GET. So the screen reads *"Amount you are approving ₹149740.82"* above a panel
headed "What is in it" that explains it was not sent a breakdown.

This is the consent moment the entire product is built around, and it is the one screen that
cannot name what is being consented to. It also makes the strongest claim — an approval binds
exact bytes — impossible to feel, because the bytes are invisible.

Fix: populate `quote` in the read-path `ApprovalCardOut` from the stored version content,
exactly as `approval_card_body` already does on the open path. The version is immutable, so
serving its breakdown re-quotes nothing.

**3. The one number the buyer approves is the only amount with no digit grouping. (1 line.)**
`apps/buyer-web/src/lib/money.ts:45`

`formatMinor` groups through `toLocaleString("en-IN")`. `formatMoney` ignores it and returns
`symbol + display`. Captured off one live basket panel:

```
Items subtotal   ₹1,26,899.00     grouped
Tax on items     ₹22,841.82       grouped
Total            ₹149740.82       not grouped
```

And on the approval screen the button says **Approve ₹149740.82** while the version trail four
inches below says **₹1,49,740.82**. Two renderings of the same number on one screen, at the
moment of consent. An Indian buyer reads the first by lakh grouping and has to count the second
digit by digit.

Fix: `formatMoney` delegates to `formatMinor(money.minor, money.currency, options)`; keep
`display` for the audit trail. Covers every call site.

**4. "Best sellers" is the alphabetical head of the catalogue. FIXED.**
`apps/buyer-web/src/app/page.tsx`

Read live off the rendered homepage: `AASH-STPL-002`, `AASH-STPL-019`, `ACT-SNCK-019`,
`AJWA-COND-012`, then fifteen consecutive `AMUL-DAIRY-*`, ending on a ₹1,26,899 iPhone in a
grocery best-seller rail. `api.products({limit:20})` requests no ranking and nothing in the
platform counts sales per SKU.

This was a claim about other people's behaviour that nothing measured — the softest and
commonest form of buyer pressure, and the platform's own growth engine explicitly refuses to
make this exact claim four directories away.

The rail is now headed **"On the shelf"**, under a caption that names the ordering out loud:
"The first 20 products the merchant's catalogue returns, in SKU order. This shop keeps no
record of what sells, so nothing here is ranked by popularity." The shelf was never the
problem and was kept; only the claim about it went. The caption is checkable against the page
itself — the alphabetical run above is still visible, iPhone included — which is a stronger
position than a quietly renamed heading.

Fix: change the heading to "From the catalogue". That is the whole fix.

**5. The review queue gives an all-clear over ₹85 of stuck money. (20 minutes.)**
`apps/merchant-console/src/app/review/page.tsx:250-252`

`commerce_dev` holds three refunds: PENDING ₹10.00, UNKNOWN ₹25.00, FAILED ₹50.00, aged about
seven hours, `provider_refund_id` null on all three. `audit_events` contains zero
`human_review%` rows, `GET /v1/review/queue` returns `cases: []`, and the page reads: *"an empty
queue is the platform reporting that it settled everything it saw."* That sentence is false in
this database, and it is affirmative rather than merely silent, which is worse — a merchant
reads it and tells three customers their money is on the way.

Fix: state a fact instead of an inference — "No case is open", plus a count of refunds not in a
terminal state. Then, before you present, move the seeded `RECONCILE_REFUND` outbox row's
`available_at` (currently parked at 2026-09-12) to now, so one real case lands in the queue.
That card is the answer to "what happens when you cannot tell whether the money moved," and
right now there is nothing to point at.

**6. Own the three claims the demo cannot back. (20 minutes of editing, no code.)**
All three verified against `commerce_dev`:

- **No live capture has ever happened.** All 8 CONFIRMED orders carry
  `capture_evidence.event_id = "evt_seed_…"` with `channel: VERIFIED_WEBHOOK`. Order *creation*
  is live; capture verification is seeded.
- **No refund has ever reached the provider.** Three refunds, `provider_refund_id` NULL on all
  three. The evidence table currently claims "one verified refund."
- **No model runner is wired.** `agent_runner` is assigned in exactly two test files and
  nowhere in `packages/`, `apps/`, `scripts/` or the Makefile, so `agent_service.py` always
  falls through to `DeterministicRunner()`. Live turns return in ~0.0s with
  `routing_reason: default_shopping`.

A judge who asks "is there a model in this?" or "show me money going back" will get a no, and
finding it themselves retroactively discounts the parts that are excellent — and several are.
Fix the words: the ADK adapter is built and tested, and the honest line is that *the running
demo deliberately uses the deterministic runner, because everything you are about to see must
be true regardless of which model sits in that slot.* That turns the third item into a
strength. Change "ADK traces" in the evidence table to what you can actually show: typed tool
calls, per-specialist principal ids, capability denials on the wire.

### The next tier — take these in order if time holds

**7. One tap too many on `+` strikes out the whole basket. (30 minutes.)**
`basket-view.tsx:137-140`, `use-basket.ts:72-75`. Setting quantity 21 on a 20-unit SKU returns
HTTP 200 with `quote: null` and `code: STALE_CHECKOUT`. The page then computes `dropped` as
every line not present in the (empty) priced set, so **all** lines are marked "No longer
available", the bill panel disappears, names become SKUs, and `unavailableSkus()` discards
`available_units: 20` — the one fact that would fix it. The realistic off-by-one is what
breaks; 999 returns a harmless 422. Fix: when `quote` is null, mark only the SKUs in
`basket.unavailable`, and surface the number — "Only 20 left — reduce to 20?" with a one-tap
button. Never render `code` to a buyer.

**8. `synthetic: true` stamped over committed Postgres rows. (10 minutes.)**
`agent_service.py:~588`. Live payload: `{"kind":"checkout_metrics","synthetic":true,"source":"postgresql","sample_size":1421}`,
and the reply contradicts itself inside one sentence — "Synthetic data… These are counts from
committed rows." The catalogue-health and anomaly payloads are legitimately synthetic; this one
is not. Derive `synthetic` from the source rather than a constant. Provenance discipline is this
product's strongest asset and it breaks at the one place a merchant reads a number.

**9. After you approve, the amount and the clock both disappear. (15 minutes.)**
`approval-card.tsx`, APPROVED state. The button is the bare word "Pay"; no amount appears
outside the greyed version trail, and the countdown that read "14:51 left on this hold" one
screen earlier is gone. The screen before says "Approve ₹149740.82" and the screen after says
"Pay ₹1,49,740.82". Only the commit step is silent about money and time. Carry `total` and the
reservation countdown through.

**10. The approval card never re-reads, so an expired hold still says ACTIVE. (20 minutes.)**
`approval-card.tsx:70-80`. There is no polling at all in the APPROVAL_REQUIRED state — the
payment panel polls, the state banner polls, the approval card runs a client-side `setInterval`
seeded once at mount. The clock clamps at 0:00 rather than running negative, but a server-side
EXPIRED reservation keeps rendering a live countdown and the literal string `state ACTIVE`.
Poll while the card is open; when the reservation is not ACTIVE, stop the clock and say so.

**11. A refused checkout is a loop, and the basket is already gone. (30 minutes.)**
`refusal-card.tsx`, `basket-view.tsx:91`. `proceed()` clears the basket id the instant the
checkout opens — the header flips to "My cart, empty" mid-checkout. After a terminal refusal the
only control offered is "Read this checkout again", which restores a confident Pay button:
expired hold → Pay → refused → read again → Pay. An expired fifteen-minute hold is the likeliest
failure in a quick-commerce demo. Keep the basket id until the checkout can no longer be
abandoned, and replace the re-read control with "Put these items back in my basket".

**12. Searching "paneer" returns aluminium foil. (20 minutes.)**
`packages/merchant-sim/src/merchant_sim/search.py:176-198`. Live: `AMUL-DAIRY-004` scores 150 on
an exact match and is out of stock; `FRES-HHLD-017` (Aluminium Foil) scores 25 on a fuzzy
`"papir"` and appears anyway. The app's own placeholder query returns 80% junk with the one
right answer unavailable. Drop hits below a fraction of the top score whenever an exact match
exists. Also move `catalogue revision 953` out of the buyer-visible header.

**13. The evidence page absorbs price changes made after the checkout died. (20 minutes.)**
`packages/commerce-api/src/commerce_api/services/timeline.py:~825`. The filter is
`event.occurred_at >= head.created_at` with no upper bound. Injections made minutes after a
checkout terminated land inside its Action timeline as bare "Scenario injection." with no SKU
and no deltas, even though the payload carries them. This is the page that proves the platform
refused a stale approval; its credibility rests on every row belonging to this checkout. Bound
at the terminal timestamp and render the SKU and before/after already in hand.

**14. Pin the canonical evidence checkout. (5 minutes.)**
`evidence.py:573` → `proof_chain.py:1128`. With no `checkout_id` the endpoint returns "the
newest checkout whose approved version was later invalidated" — it re-pointed during the audit
to a refusal another session had just created. `docs/PITCH.md` is narrated over ₹579.95 →
₹681.95 and states that only a canonical run matches those numbers. Pin `?checkout_id=` in the
console's `/evidence` link and in the pre-flight checklist, and add "confirm no other process is
driving `:8000`" beside the milk-price check.

**15. Revive is enabled on all 46 outbox rows and inert on 44. (5 minutes.)**
`OutboxTab.tsx:149-158` guards only on `outcome?.running`; DEAD-ness lives in a tooltip. An owner
chasing a stuck refund presses Revive on a DONE `PAYMENT_CREATE_ORDER` and spends the next minute
wondering whether they double-charged someone. The API is safe; the affordance is not. Set
`disabled={command.status !== "DEAD"}` and put the reason in visible text.

**16. Refund failures are explained in fixture strings. (20 minutes.)**
`GET /v1/refunds` returns reason codes `seed_not_yet_sent`, `probe timeout`, `probe partial`,
rendered verbatim in monospace as the explanation for why a buyer's money has not moved.
`seed_not_yet_sent` is visibly a fixture name, which tells a merchant the numbers around it may
be fixtures too. Map to merchant sentences and keep the raw code in small type beside it — the
pattern this same screen already uses well for `row_status`.

**17. The small true things. (~1 hour for all of them; each is a few lines.)**

- `RefundsTab.tsx:195` renders `{age_seconds}s old` — live values 24,464 / 25,706 / 25,916.
  Render "6h 47m", keep seconds in the `title`. This is the field that tells a merchant which
  row is urgent, in the least readable unit on the page.
- `header.tsx:121` says 22 items (sum of quantities), `basket-view.tsx:147` says 3 items (line
  count), both visible at once. Label them "3 products · 22 units".
- There is no link to order history anywhere. `/orders` renders fine; the only links to it are
  on a refusal card and on order detail, which is reachable only from `/orders`. Specification
  8.3 opens with "Track order". Add a header entry.
- Empty search says "Try one of the suggestions on the search page." There is no search page and
  no autocomplete. Delete the sentence.
- Checkout timestamps render UTC, the product page renders IST. The same event appears five and a
  half hours apart on two screens, which makes a fifteen-minute hold look hours old. Use IST for
  buyer-facing times.
- Scenario faults are unlabelled in the provider ledger. `provider_requests` has no origin
  column; the sole marker is the string prefix in `transport_error: "ScenarioFault:REFUND_TIMEOUT"`
  sitting against a real `api.razorpay.com` URL. Specification 31.3 requires injections labelled
  and never mixed with organic data — the catalogue page honours this and this table does not.
- `commerce_dev` is one migration behind: `7d2a4b9e1f03` against `commerce_test` at
  `a4e17c93b5d2`. `alembic upgrade head` — two keyset-pagination indexes, no data change. The
  schema you demo should be the schema your tests pass against.
- `catalogue/page.tsx:502` asks a shopkeeper to type paise: the field is prefilled `25500` with
  "Integer paise, as the API stores it." The reasoning (the browser must not compute money) is
  right, but a slipped zero is a ₹2,700 loaf on a live storefront with no confirmation. Take
  rupees, echo the paise integer live beneath the field, send the integer.
- `store.py:180` — the stock pill is on-hand, not available: reservations live in the kernel and
  never decrement the simulator, so a fully reserved product still advertises stock. It errs
  generous, so it is not a dark pattern, but it is an untrue availability claim on the most
  factual-looking element of the card, and it surfaces later as an avoidable checkout failure.
  The product page's "N units at the merchant" is the honest phrasing; the card's bare "12 IN
  STOCK" is not.

---

### The line

**Everything below this line is not happening today. That is a decision, not an oversight.**
Each of these was examined and deliberately deferred, and each has a sentence to say to a judge
who finds it.

**Operator-initiated refunds.** `RefundsTab.tsx:214`. A merchant console that cannot refund is a
real product gap and no shop owner accepts an architectural answer to it. But wiring a
money-moving path through admission and an Execution Grant on deadline day is the single most
dangerous thing on this list. Instead, spend twenty minutes reframing the panel: say the exact
path a customer takes and who to contact when the provider said no, rather than arguing consent
theory at someone who just wants to refund ₹50. *To a judge:* "Refund initiation is the next
grant type; the execution path it would use is the one you just watched authorize a payment."

**Order detail and order search.** "A customer rang about their order" is currently
unanswerable — the Orders table is UUIDs and provider ids with no buyer, no line items, no
lookup. Correctly identified, and a day's work.

**Takings and funnel on the Overview.** The data exists: the Copilot answered 1,421 checkouts,
1,198 CANCELLED, 8 PAID — a 0.6% conversion that no screen mentions. A takings strip is the
highest-value missing merchant feature. Not today.

**A reconciliation tab in the console.** `GET /v1/review/reconciliation` already answers the
merchant's most valuable question — which payments are in flight and unresolved — and the front
end throws it away; `grep reconcil` across `apps/merchant-console/src/lib/api/client.ts` returns
nothing. A read-only tab is about two hours. If it is cut, item 5's copy fix **must** land,
because that sentence is currently covering for this absence.

**Voice.** `POST localhost:3000/api/voice/tickets` returns 404; no route exists under
`apps/buyer-web/src/app/api/`, and `connect-src 'self'` would block the gateway anyway.
`voice-runtime` is real and tested; nothing serves the socket. **Hide the microphone for the
demo** and use the line `docs/KNOWN_GAPS.md` already writes for you. Do not attempt the
WebSocket proxy today.

**One real capture and one real refund.** This would upgrade the two weakest evidence rows to
the strongest. It needs a tunnel for the webhook plus an operator entering their own test card.
If an hour appears, it is the highest-value hour available. If not, item 6's wording covers it
honestly.

### If only six things get done

1. Restart the API — unlocks two headline features, costs nothing.
2. `window.location.assign` in `proceed()` — one line; without it the payments demo fails at
   payment.
3. `formatMoney` delegates to `formatMinor` — one line; fixes the approve button.
4. ~~Rename "Best sellers"~~ — done. The rail is "On the shelf" and says what orders it; the
   last fabricated claim on the buyer surface is gone.
5. Populate `quote` on the checkout read path — thirty minutes; lets the hero screen state what
   is being approved.
6. Fix the review page's empty-state sentence — twenty minutes; stops the console giving an
   all-clear over stuck money.

Ninety minutes. It repairs the money path, makes the consent screen honest, and removes both
things a payments company would call a dark pattern.

---

## 3. The honest-growth position

The claim to defend is: **this system raises merchant revenue without pressuring buyers.** The
audit found one live exception and one structural gap; both are now closed, and both are left
written up below rather than deleted, because what was found and what was done about it is the
evidence for the claim. Here is what was checked and what was found, so the claim can survive
questioning rather than merely be asserted.

### Where the discipline is real and provable

The growth engine refuses to invent urgency, in code, with the reasoning committed.
`packages/commerce-api/src/commerce_api/services/proposals.py:283-286` declines to describe a
product as a top seller because that "would be inventing the one fact that makes the lever
urgent." That is a system choosing a weaker pitch over an unfounded one.

The free-delivery nudge is arithmetic, not persuasion. The gap is server-computed, and
`policy.py:73-92` refuses to report a gap at all when no delivery fee applies — the nudge cannot
exist unless the saving is real. Both the spend and the saving sit on the same panel. That is
emphasis, and it is honest.

The buyer surface refuses claims it cannot back. `promo-banners.tsx` will not name a discount it
cannot substantiate, and the storefront header says outright: "Nothing here is delivered. A
demonstration of the payment path." This is a codebase that has already deleted a class of claim
on purpose. It is worth saying that in the room, because it is unusual.

Approvals are not confirm-shamed. `approval-card.tsx:351` and `checkout-journey.tsx:433` were
checked directly: Approve and Decline are the same size, both visible, neutrally worded, with no
"no thanks, I don't want to save money" pattern anywhere. Decline is styled red, which is a
defensible hierarchy choice rather than a dark pattern; the only issue is that the same action
is called Cancel on one screen and Reject on another, which is cosmetic.

Growth levers require a human to apply them. The proposal card is a proposal; nothing in the
growth path executes on its own.

### The one live dark pattern — closed

**"Best sellers" on the storefront homepage was fabricated social proof.**
`apps/buyer-web/src/app/page.tsx`. Nothing in the platform counts sales per SKU; the rail is
`api.products({limit:20})`, which is the alphabetical head of the catalogue. It was a claim
about what other shoppers chose, above the fold, that no measurement supported — exactly the
claim `proposals.py` refuses to make. The rail is now "On the shelf" and prints its own
ordering rule beneath the heading, so the section states what it is instead of what it wished
it were.

Two other candidates were examined and **rejected** as dark patterns, deliberately: the red
Decline button (a hierarchy choice, both options equally available) and the free-delivery nudge
(the arithmetic is honest, server-side, and suppressed when the saving is not real). A third —
the "12 IN STOCK" pill overstating availability because reservations do not decrement the
simulator (`store.py:180`) — is a factual error but errs *generous*: it never manufactures
scarcity. It belongs on the bug list, not this one.

### The structural gap, and it is the more interesting answer — closed

**The grounding fence guarded money but not manipulation.** It now guards both.
`packages/agent-runtime/src/agent_runtime/grounding/postcheck.py`. Run against live source, the
same inputs as before:

| Input to `verify_reply` | Was | Now |
| --- | --- | --- |
| `"Amul Butter costs Rs 999.00"` (ungrounded amount) | `rewritten=True` — stripped | unchanged |
| `"Hurry! almost gone, only 2 left and 14 people bought it in the last hour."` | passed verbatim | `rewritten=True` — nothing survives |
| `"This is our best seller, 500 people bought it today. Selling fast!"` | passed verbatim | `rewritten=True` — nothing survives |

Fabricated scarcity and fabricated social proof used to be invisible, because `_AMOUNT` requires
a currency marker and there was no unit-count or pressure-phrase rule. The only thing standing
between the assistant and those two sentences was one line of prose in
`shopping_specialist.md:25` — a request, not an enforcement.

Two rules close it, and they are deliberately different in kind, because the claims are.

**Rule 4, unit counts, checked against the ledger.** `GroundingLedger.stock_counts` now records
every shelf figure a merchant read returned this turn — `ProductCard.stock_units` from search and
product reads, `UnavailableLine.available_units` from a basket that could not be priced — kept
apart from `amounts_minor`, so a price of 200 paise can never ground a claim that 200 units
remain. `_STOCK_CLAIM` reads remaining-counts out of the prose in all three scripts, digits and
spelled-out numbers alike (a digits-only check is one rewording away from "only two left"), and a
count no read returned takes its sentence with it. Scarcity is not banned; inventing it is. It is
turn-scoped like the money rule, because stock moves under a basket exactly as price moves under
a checkout, so last turn's count is not evidence for this turn's sentence.

**Rule 5, sales pressure, dropped without consulting the ledger.** "Best seller", "selling fast",
"trending", "14 people bought it in the last hour", "jaldi kijiye". No tool on this platform
returns a demand signal, a sales rank or a buyer count, so no ledger entry could ever make one of
these true — they are ungroundable by construction rather than ungrounded by accident. A closed
lexical vocabulary, so what is removed stays reviewable and nothing else goes with it.

One deliberate refinement between the two: scarcity with the *number left out* — "almost gone",
"running low", "last few" — is gated rather than banned. A shelf really can be nearly empty, and
a merchant asking which lines need restocking is owed the phrase, so it is allowed when a read
this turn came back at or under `LOW_STOCK_UNITS` and dropped when none did. Banning it outright
would silence a true sentence about a real anomaly, which is its own kind of dishonesty.

Both are recorded as the correction `sales_pressure_removed`, kept apart from
`ungrounded_sentences_dropped` because a reviewer reading a trace is asking a different question:
not "did the model get a number wrong" but "did it try to pressure the buyer".

Ten tests in `packages/agent-runtime/tests/test_ar_grounding.py`, each broken once and watched go
red — including both directions of the scarcity gate, since a check that silences honest merchant
reporting has failed as surely as one that lets a fabrication through.

### The position, stated

Revenue growth here comes from levers that are structurally incapable of pressuring a buyer:
restocking what sold out, repricing against real cost, recovering a checkout whose price changed
rather than letting it be abandoned, and refusing to execute any of it without a human. The
system's discipline about unfounded claims is written into the growth engine itself, and the
enforcement of it now reaches money, unit counts and pressure alike: the reply post-check drops a
fabricated shelf count for the same structural reason it drops a fabricated total, and drops a
popularity claim because nothing this platform can read could ever support one. On the buyer
surface, the last heading that asserted more than the catalogue knows now prints its own ordering
rule instead.

The honest statement of what remains is narrower, and worth making precisely: the pressure
vocabulary is a closed lexical list, so it catches the phrasings it names rather than every
paraphrase a model might invent. That is a bounded, reviewable limit rather than an open hole,
and the count rule behind it is not lexical at all — it is checked against what a tool actually
returned.

That is a stronger answer than "we have no dark patterns," and it is true.

---

## 4. What to lead with, and the question you will be asked

### The demo

Lead with the refusal, not the happy path. The happy path is a checkout; every entrant has one.
The refusal is the product.

1. **Build a basket and open a checkout.** The approval card names the exact version and the
   exact amount. (With fix 2 in, it names the line items too, which is what makes the next beat
   land.)
2. **Change the price underneath it** from the merchant console — a lever a human applies.
3. **Approve anyway.** The kernel refuses: `REAPPROVAL_REQUIRED`. The approval bound specific
   bytes and those bytes no longer describe the world. Say plainly that this happened to you
   unscripted during testing, because another process moved a price mid-run. That is the most
   credible sentence available.
4. **Open the evidence page** and walk the proof chain link by link — pinned to the canonical
   `checkout_id`, per fix 14.
5. **Show a real Razorpay order id**, created live. `order_TYKx4L0hcGbNNe` was created this
   session against Razorpay, HTTP 200.
6. **Close on the console:** a proposal the growth engine generated, and a human applying it.
   Agents propose; deterministic systems authorize and execute.

Drive up to the payment sheet and stop. Do not enter card details.

### The sharpest question a payments engineer will ask

> **"You said an agent can't move money without authorization. Show me what actually enforces
> that — because from here it looks like your agent just calls your own API, and 'the agent
> asked nicely' is not a control."**

The honest answer:

The enforcement is not in the agent and not in the prompt. Three things sit between a proposal
and a movement of money, and each is a separate mechanism:

**One — capability binding.** Each specialist holds a distinct principal id and a fixed tool set
bound at registry level (`capabilities/registry.py`). A tool a specialist does not hold is denied
at the boundary, on the wire, and the denial is observable in the trace. The agent cannot reach
for a capability it was not granted, regardless of what it decides to do.

**Two — version binding.** An approval is a signature over an immutable version record, not over
an intent. If any input to that version changes — price, stock, fees — the kernel refuses at
authorization time. It refused during this audit because a price moved underneath a live
checkout that nobody staged.

**Three — the execution grant.** Execution is a separate, typed, human-gated step. The agent
produces a proposal; a human applies it; the kernel executes it. There is no path where the
model's output is the thing that moves money.

Then volunteer the weak point before it is found: **capture verification in this demo is
seeded.** Order creation against Razorpay is live and can be shown. Webhook-verified capture is
not, because it needs a public tunnel that was not stood up. The verification path is built and
tested; what you are seeing on this laptop is a seeded event. Saying that first is worth more
than being caught at it, and it costs nothing — the control being demonstrated is the
authorization gate, and that gate is real.

If they push on the model: the reasoning layer is pluggable, the ADK adapter is built and tested,
and the running demo deliberately uses the deterministic runner — because everything they have
just watched must be true regardless of which model sits in that slot. That is the actual thesis
of the product, and it is better as a stated choice than as an omission someone discovers.
