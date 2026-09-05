# Storyboard

The shot list for the five-minute recording. Every cut, what is on screen, the click or
command that produces it, and the words that go over it. The narration here is the same
text as [`PITCH.md`](PITCH.md), broken at the cuts.

Twenty-two shots. Running time 5:30.

Three of them must be perfect. They are marked **★ HERO** and they are shots 9, 10 and 14.
Everything else can be re-recorded around a stumble; those three carry the argument, and a
mistimed one costs the submission more than a whole retake costs the day.

## Before you roll

| Thing | State it must be in | How to check |
| --- | --- | --- |
| The stack | API, worker, both apps up | `curl -sS $API/v1/config` shows `"degraded": []` |
| The catalogue | milk at **₹28.00** | `curl -sS $API/v1/catalogue/products/AMUL-DAIRY-001 -H "$AUTH"` → `unit_price_minor: 2800` |
| The basket | empty | the header pill reads *My Cart*, no count |
| The browser | one window, no dev tools, no bookmarks bar | — |
| The console tab | `http://localhost:3001/evidence`, already loaded | — |
| The terminal | large type, `$API`, `$AUTH` and `$SK` exported, the beat-three `curl` in the buffer | — |

A take that begins with an already-injected price runs straight through to payment and
never shows shots 9 and 10. Reset between takes:

```bash
curl -sS -X POST $API/v1/scenario/injections \
  -H "$AUTH" -H "X-Scenario-Key: $SK" -H 'Content-Type: application/json' \
  -d '{"kind": "CATALOGUE_RESET", "note": "between takes"}'
```

Or let the capture script do the whole sequence and check your framing against its output:

```bash
node scripts/capture_screenshots.mjs
```

Each shot below names the still that script writes, where one exists. Those stills are the
reference frames. If the recording does not look like them, the recording is wrong.

The script's last line says whether the run was **canonical** — version 1 at ₹579.95 and a
difference of exactly ₹102.00. Only a canonical run matches the narration in this file. The
merchant catalogue is process memory shared with every session on the machine, so a run can
be a perfectly valid refusal and still not be the one these words were written over. If it
reports otherwise, run it again rather than re-recording against numbers the script does not
say out loud.

---

## Act one — the problem (0:00 – 0:45)

### Shot 1 · storefront home · 0:00 – 0:12 · 12s
**Screen:** `http://localhost:3000/`, top of the page, no scroll yet.
**Produced by:** open the storefront.
**Reference still:** `docs/images/01_storefront_home.png`
**Motion:** none for the first four seconds. Let it sit.
**Narration:**
> An AI buyer can already do the easy part. It can find the milk, read the price, and put
> it in a basket.

**Note:** the header line reads *Nothing here is delivered — a demonstration of the payment
path*. Do not scroll past it. It is the honesty disclaimer and it belongs in frame.

### Shot 2 · category page · 0:12 – 0:26 · 14s
**Screen:** `http://localhost:3000/c/dairy`.
**Produced by:** click *Dairy & Eggs* in the category rail.
**Reference still:** `docs/images/02_category_dairy.png`
**Motion:** one slow scroll, about half a page, then stop.
**Narration:**
> Here is the part it cannot do safely. Between the moment a buyer says yes and the moment
> the money moves, the merchant's world keeps moving. A price changes. Stock runs out. A
> delivery fee appears.

### Shot 3 · hold on the grid · 0:26 – 0:45 · 19s
**Screen:** the same page, still.
**Produced by:** nothing. Stop moving the mouse.
**Narration:**
> That gap is milliseconds on a good day and minutes on a real one. Almost every agentic
> checkout demonstration you will see today ends before that gap opens. The assistant says
> the order is ready, a payment link appears, and the recording stops.
>
> We built the thing that happens inside the gap.

**Note:** two seconds of silence at the end of this shot. The tone changes here.

---

## Act two — the one screen (0:45 – 2:15)

### Shot 4 · RazorAI, mid-conversation · 0:45 – 1:00 · 15s
**Screen:** the category page with the RazorAI panel open on the right.
**Produced by:** click the *RazorAI* control, bottom right. Type
`mujhe 2 doodh aur ek basmati chawal chahiye`. Send. Wait for the answer.
**Reference still:** `docs/images/03_razorai_panel.png`
**Narration:**
> The assistant reads the merchant's catalogue, not its own memory. It answers in the
> language it was asked in. And under every answer it shows the tool calls it actually
> made — *searched the catalogue, five hits*. Grounding is a property of the answer here,
> not a claim about the model.

**Note:** the panel's footer reads *RazorAI proposes. Approving and paying happen on the
store's own pages, never in this panel.* Keep it in frame. It sets up shot 7.

### Shot 5 · the basket · 1:00 – 1:12 · 12s
**Screen:** `http://localhost:3000/basket`, bill details visible.
**Produced by:** add two milks and one rice, then open the basket.
**Reference still:** `docs/images/04_basket_quote.png`
**Narration:**
> Two milks and a bag of rice. Five hundred and seventy-nine rupees, ninety-five paise. The
> server computed that. The browser did not.

**Note:** *Delivery fee — Free* is on screen because the rice crossed the merchant's
threshold. Worth a half-sentence if the take is running short of its budget, and the first
thing to cut if it is running long.

### Shot 6 · the approval card · 1:12 – 1:26 · 14s
**Screen:** `/checkout/<id>`, version 1's card, the *What it is bound to* block visible.
**Produced by:** click *Proceed to checkout*.
**Reference still:** `docs/images/05_approval_card_v1.png`
**Motion:** cursor rests on the content hash. Do not click.
**Narration:**
> The buyer is not approving a sentence. They are approving these exact bytes, under this
> hash, for this integer amount.

### Shot 7 · the trusted surface · 1:26 – 1:38 · 12s
**Screen:** same card, scrolled so the black-bordered approval block fills the frame.
**Produced by:** scroll down one block.
**Narration:**
> Read the line under the button. *You are approving this. RazorAI cannot.* The assistant
> that filled this basket holds no capability to approve and none to pay. That is not a
> policy we wrote down. It is a capability it was never issued.

**Note:** the trusted surface is visually distinct from everything the agent renders — a
hard border and its own colour. That difference is the design doing the argument's work,
so frame the whole block rather than cropping to the button.

### Shot 8 · the injection · 1:38 – 1:55 · 17s
**Screen:** the terminal, full frame.
**Produced by:** click *Approve* on the storefront first, confirm it says APPROVED, then
cut to the terminal and paste:

```bash
curl -sS -X POST $API/v1/scenario/injections \
  -H "$AUTH" -H "X-Scenario-Key: $SK" -H 'Content-Type: application/json' \
  -d '{"kind": "PRICE_SET", "sku": "AMUL-DAIRY-001", "value": 7900}'
```

**Narration:**
> The merchant has just raised the milk from twenty-eight rupees to seventy-nine. After the
> buyer approved. Before anything was paid.
>
> That is staged, and it says so. Look at the label: `SCENARIO_INJECTION`. Every injection
> is written into the audit under that label and is never mixed with organic data. A panel
> is entitled to ask whether we arranged the failure. We did, visibly, on purpose.

**Note:** the response also carries `revision_before`, `revision_after`, an
`audit_event_id` and a `scenario_run_id`. Do not read them out. They are there for anyone
who pauses the video.

### Shot 9 · ★ HERO · the refusal, first read · 1:55 – 2:05 · 10s
**Screen:** the refusal card, top third: the red badge, the headline, the
*You were not charged* paragraph, and the version trail line.
**Produced by:** cut back to the storefront and click *Pay*.
**Reference still:** `docs/images/06_the_refusal.png`
**Motion:** **none at all.** No scroll, no cursor movement, nothing.
**Narration:**
> The kernel refuses.

**Note:** one full second of silence after *refuses* before the next line starts. This is
the frame the room remembers. If the card renders with a scrollbar mid-animation, cut and
retake.

### Shot 10 · ★ HERO · the arithmetic · 2:05 – 2:22 · 17s
**Screen:** the delta block, held. `₹579.95` struck through, an arrow, `₹681.95`,
`+₹102.00` in red, and the *What changed* table beneath it naming `total` and
`total_changed`.
**Produced by:** nothing. The same page. Hold.
**Narration:**
> Five hundred and seventy-nine ninety-five, struck through. Six hundred and eighty-one
> ninety-five now. A difference of one hundred and two rupees exactly — two milks, at
> fifty-one rupees more each.
>
> It does not just say no. It names the field that moved, the value the buyer agreed to,
> and the value now. It says *you were not charged*, and it is telling the truth: no
> payment attempt was created and no order exists at Razorpay for this version.

**Note:** say the subtraction out loud. Two times fifty-one is one hundred and two. It is
what turns a number on a screen into a number the room has checked.

### Shot 11 · ★ HERO · the version trail · 2:22 – 2:38 · 16s
**Screen:** scroll to the bottom of the refusal page. Both rows in frame: `v1 INVALIDATED
₹579.95` with its approval timestamp, `v2 APPROVAL_REQUIRED ₹681.95`.
**Produced by:** one slow scroll to the version trail.
**Narration:**
> Version one is permanently invalidated. It is never revived. Version two is already priced
> and waiting, with its own policy receipt and its own stock hold, built inside the same
> database transaction as the refusal — so a crash at this exact moment cannot leave a
> checkout that can never be approved.

**Note:** the strikethrough on the `1` in the *Version 1 → 2* strip is the detail to point
at, not the badge.

### Shot 12 · the status code · 2:38 – 2:52 · 14s
**Screen:** the *Why it refused* block. Reason key, decision id, correlation id.
**Produced by:** scroll back up one block.
**Narration:**
> One more thing, and it is the detail a payments engineer will care about most. That
> refusal came back as HTTP 200. Not a four hundred. Not a five hundred. A denial is this
> platform working correctly, and a 4xx would tell every client, retry queue and gateway in
> the chain to try it again as though it were a fault.

**Note:** two full seconds on this frame before cutting to the console.

---

## Act three — the evidence (2:52 – 4:00)

### Shot 13 · the arithmetic, merchant side · 2:52 – 3:14 · 22s
**Screen:** the console at `/evidence`, top: the four rows of *The arithmetic*.
**Produced by:** switch tabs. The page is already loaded; reload it so the read is fresh.
**Reference still:** `docs/images/09_console_retained_revenue.png`
**Narration:**
> That was the buyer's view. This is the merchant's, and it is the same event, read out of
> committed rows.
>
> Four figures. What the buyer approved. What it became. What was captured. And the
> difference.
>
> Look at the third one. It is a dash. Nothing has been captured on this checkout yet, so
> the platform states no captured amount and no difference. It could easily print a number
> here and call it revenue we saved. It does not, because that number would not be true yet.

**Note:** the `INJECTED` and `UNSETTLED` chips in the Provenance block below are worth a
glance if the take has room. The response field is `controlled_scenario: true` — a
reproducible scenario, not a production lift claim. Say that phrase if nothing else from
this block.

### Shot 14 · ★ HERO · the proof chain · 3:14 – 3:38 · 24s
**Screen:** the *Proof chain verdict* panel. `HOLDS`, `tier EXECUTED`, and the checks
listed beneath it.
**Produced by:** scroll to it. Do not click.
**Reference still:** `docs/images/09b_console_proof_chain.png`
**Narration:**
> This is the Money Action Proof Chain, and it is fifteen separate checks rather than one
> green tick.
>
> The content hash, recomputed from the stored bytes rather than trusted. The policy
> receipt, bound to the version. The approval, bound to the content. The grant, bound to
> the decision. And this one — `grant_consumed_once`: one grant, one provider request
> recorded against it. `every_mutation_consumed_a_grant`: one provider mutation, zero
> without a grant.

**Note:** one of the rows reads `n/a — capture_evidence_is_verified: no order has been
confirmed`. Leave it in frame. A verifier that returns `n/a` where it cannot check is worth
more than one that returns green everywhere, and a judge who spots it will trust the rest
more.

### Shot 15 · *optional* · the database underneath · 3:38 – 3:56 · 18s
**Screen:** the terminal, `psql` output.
**Produced by:**

```bash
psql -d commerce_dev -c "
  select p.provider_id, g.status, (p.request_at - g.consumed_at) as network_after_consume
  from provider_requests p join execution_grants g on g.id = p.grant_id
  where p.url like '%/v1/orders' order by p.request_at desc limit 4"
```

**Narration:**
> Every call this system has made to `api.razorpay.com` came back with an order id —
> twenty-three of them at the time of writing, and the count only goes up. Every consumed
> grant matches exactly one network call: no grant with two, no call without one.
>
> And the ordering is the point. The grant is spent inside the committed transaction
> *before* the request goes out. So a crash between the two loses the money action rather
> than repeating it. In a payments system, losing an action is recoverable and repeating one
> is not.

**Note:** cut this shot first if the recording is long. Shot 14 already makes the claim;
this one only shows the rows behind it.

### Shot 16 · the timeline · 3:56 – 4:08 · 12s
**Screen:** the console's *Action timeline*, scrolled so the `SCENARIO_INJECTION` row and
the `admission.denied` row beneath it are both visible.
**Produced by:** scroll down.
**Narration:**
> Every step is here in order, hash-chained. The scenario injection is in the middle,
> flagged. The kernel's denial is a row as durable as its approval — what was refused is
> evidenced exactly as well as what was allowed.

---

## Act four — the architecture (4:08 – 4:55)

### Shot 17 · the diagram · 4:08 – 4:22 · 14s
**Screen:** `docs/images/architecture.svg`, full frame.
**Produced by:** open the file, or a slide holding it.
**Narration:**
> One sentence holds the whole architecture. Agents propose. Deterministic systems authorize
> and execute.

**Note:** the money path is emerald and the agent's reach is rose. Say which is which; the
colours carry the point faster than the boxes do.

### Shot 18 · the capabilities response · 4:22 – 4:38 · 16s
**Screen:** the terminal.
**Produced by:**

```bash
curl -sS $API/v1/agent/capabilities -H "$AUTH" | python3 -m json.tool
```

**Narration:**
> Ask the API what the agent can do and it answers with a list — and then a second list,
> called `absent_by_construction`: approve, reject, revoke authority. Those are not
> permissions the agent has been denied. They are capabilities that were never issued to it,
> so there is no path to abuse and no prompt that talks its way past one.

**Note:** this is the shortest shot that proves the thesis, and it fits on one screen. If a
panel asks *how do you stop prompt injection reaching the money*, this is the answer.

### Shot 19 · the kernel, in words · 4:38 – 4:48 · 10s
**Screen:** the architecture diagram again, or the refusal card held.
**Narration:**
> Every money action goes through the Transaction Assurance Kernel. It versions the
> checkout. It binds a canonical hash to an explicit human approval. It fixes the terms in a
> Policy-at-Sale Receipt. It admits exactly one execution under a single-use grant. And it
> writes the whole thing into a hash chain.

### Shot 20 · the payment handoff · 4:48 – 5:05 · 17s
**Screen:** `/checkout/<id>` after approving version 2. The Razorpay order id, the amount,
and the paragraph headed *Your browser coming back is not proof that you paid*.
**Produced by:** click *Review version 2*, *Approve*, then *Pay*.
**Reference still:** `docs/images/07_payment_handoff.png`
**Narration:**
> The API process holds no HTTP client for Razorpay at all. It writes one command to a
> durable outbox, in the same transaction as the decision. A separate worker picks it up,
> spends the grant, and makes the call.
>
> And read this line. *Your browser coming back is not proof that you paid.* Capture is
> applied only from Razorpay's own signed webhook or from the platform fetching the payment
> directly — through a monotonic apply, so a captured state can never regress.

**Note:** a real `order_...` id is on screen. That is a genuine Razorpay test-mode order
and it is fine to show. It is also the only shot where a live identifier appears, so do not
crop it out to make the frame tidier.

---

## Act five — the close (5:05 – 5:30)

### Shot 21 · the refusal, returned to · 5:05 – 5:22 · 17s
**Screen:** the refusal card again, the delta block centred. Same framing as shot 10.
**Produced by:** browser back, or a still.
**Narration:**
> The storefront is a deliberate, pixel-faithful clone of a quick-commerce app. That is on
> purpose. The shell has to be completely familiar so that the one unfamiliar thing in it —
> a checkout that refuses an approval it no longer trusts — is the only thing you have to
> think about.
>
> What is honestly not built, we have written down. The protocol layer is in progress. Voice
> is in progress. Autonomous Reserve Pay is held in Safe Mode, deliberately.

### Shot 22 · the last frame · 5:22 – 5:30 · 8s
**Screen:** the refusal, held, no motion.
**Narration:**
> Most agentic commerce demonstrations show you a machine that can buy things.
>
> This one shows you a machine that *cannot* — until a person has agreed to the exact number
> it is about to spend.
>
> That refusal is the product.

**Note:** hold two seconds after the last word before the cut. Do not fade to a logo.

---

## If a shot goes wrong mid-take

| What happened | Why | What to do |
| --- | --- | --- |
| *Pay* went through instead of refusing | the price injection was already in effect from the last take | `CATALOGUE_RESET`, start again from shot 5 |
| The injection answered `409` | the milk is already at ₹79.00 | reset, or inject a different value; the simulator refuses a change that changes nothing |
| A scenario call answered `401` | `X-Scenario-Key` sent without the bearer token | both headers, every time |
| A mutation answered `422` naming `Idempotency-Key` | the header is missing | add it, unique per logical action |
| A response you expected to be new carries `Idempotent-Replayed: true` | the key was reused | new key |
| The storefront says the store is not reachable | the API is down | start it — there is no fixture to fall back to, and that is deliberate |
| The grid shows skeletons forever and nothing is clickable | the Content-Security-Policy is blocking Next's own chunks | `src/lib/security/csp.ts` must not carry `'strict-dynamic'` |
| A code change to the storefront has no effect | an orphaned `next-server` still holds the port | `lsof -ti:3000 \| xargs kill -9`, `rm -rf .next`, start again |

The fuller list is in [`DEMO.md`](DEMO.md) §5.

## What cannot be recorded unattended

Shot 20 ends at the handoff. A capture needs a card typed into Razorpay's own hosted page,
so `capture_screenshots.mjs` stops there too and records the gap in its manifest rather than
leaving it out. If the recording is going to show a captured order, a person completes the
test payment between shots 20 and 21, and the order detail becomes a twenty-third shot.

Doing it live is the better choice if the network cooperates, and the honest alternative is
to say on camera that the capture path is proven by the seventeen test-mode orders in the
provider log rather than by this one take.
