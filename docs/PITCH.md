# The five-minute pitch

A script to be read aloud over a live screen. Every figure in it was produced by the
running system on 2026-09-05 and is recorded in
[`docs/images/capture-manifest.json`](images/capture-manifest.json). If a rehearsal
produces a different number, the script is wrong and the manifest is right: re-run
`node scripts/capture_screenshots.mjs` and say what the machine said.

**Total: 5 minutes 30 seconds.** The budget is 45 seconds of setup, 90 on the refusal, 90
on the evidence behind it, 60 on the architecture and 45 to close. If a hard five-minute
limit applies, cut the architecture beat to 30 seconds and drop the two paragraphs marked
*optional*. Never cut the refusal.

**Before you start.** The stack running (`make demo`, then both apps). The storefront at
`http://localhost:3000` in the front window. The merchant console at
`http://localhost:3001/evidence` in a second tab. A terminal with the `curl` for beat
three ready to paste. The catalogue reset, so the milk is ₹28.00 — a take that begins with
an already-injected price runs straight to payment and never shows the refusal, and that
is the single easiest way to waste a recording.

---

## Beat one — the problem, not the product (0:00 – 0:45)

**On screen:** the storefront home, then a category page. Scroll once, slowly. Do not
click anything.

> An AI buyer can already do the easy part. It can find the milk, read the price, and put
> it in a basket.
>
> Here is the part it cannot do safely.
>
> Between the moment a buyer says yes and the moment the money moves, the merchant's world
> keeps moving. A price changes. Stock runs out. A delivery fee appears. That gap is
> milliseconds on a good day and minutes on a real one.
>
> Almost every agentic checkout demonstration you will see today ends before that gap
> opens. The assistant says the order is ready, a payment link appears, and the recording
> stops.
>
> We built the thing that happens inside the gap.

*Pause. Change tone. This is where the demonstration starts.*

---

## Beat two — the one screen (0:45 – 2:15)

**On screen:** the basket, then the approval card, then the refusal. This beat is the
submission. Slow down rather than speed up.

**Click:** *Proceed to checkout*.

> This is a basket of two milks and a bag of rice. Five hundred and seventy-nine rupees,
> ninety-five paise. The server computed that. The browser did not.

**On screen:** the approval card. Point at the content hash.

> The buyer is not approving a sentence. They are approving these exact bytes, under this
> hash, for this integer amount.
>
> And read the line under the button. *You are approving this. RazorAI cannot.* The
> assistant that filled this basket holds no capability to approve and none to pay. That
> is not a policy we wrote down. It is a capability it was never issued.

**Click:** *Approve*. Then switch to the terminal and paste the injection.

```bash
curl -sS -X POST $API/v1/scenario/injections \
  -H "$AUTH" -H "X-Scenario-Key: $SK" -H 'Content-Type: application/json' \
  -d '{"kind": "PRICE_SET", "sku": "AMUL-DAIRY-001", "value": 7900}'
```

> The merchant has just raised the milk from twenty-eight rupees to seventy-nine. After
> the buyer approved. Before anything was paid.
>
> That is staged, and it says so. Look at the label the API sends back:
> `SCENARIO_INJECTION`. Every injection is written into the audit under that label and is
> never mixed with organic data. A panel is entitled to ask whether we arranged the
> failure. We did, visibly, on purpose.

**Switch back to the storefront. Click:** *Pay*.

**Hold here. This is the frame.**

> The kernel refuses.
>
> Five hundred and seventy-nine ninety-five, struck through. Six hundred and eighty-one
> ninety-five now. A difference of one hundred and two rupees exactly — two milks, at
> fifty-one rupees more each.
>
> It does not just say no. It names the field that moved, the value the buyer agreed to,
> and the value now. It says *you were not charged*, and it is telling the truth: no
> payment attempt was created and no order exists at Razorpay for this version.
>
> Version one is permanently invalidated. It is never revived. Version two is already
> priced and waiting, with its own policy receipt and its own stock hold, built inside the
> same database transaction as the refusal — so a crash at this exact moment cannot leave a
> checkout that can never be approved.
>
> One more thing, and it is the detail a payments engineer will care about most. **That
> refusal came back as HTTP 200.** Not a four hundred. Not a five hundred. A denial is this
> platform working correctly, and a 4xx would tell every client, retry queue and gateway in
> the chain to try it again as though it were a fault.

*Pause on the screen for two full seconds before moving.*

---

## Beat three — the evidence a payments engineer can check (2:15 – 3:45)

**On screen:** the merchant console at `/evidence`. Scroll to the proof chain.

> That was the buyer's view. This is the merchant's, and it is the same event, read out of
> committed rows.
>
> Four figures. What the buyer approved. What it became. What was captured. And the
> difference.
>
> Look at the third one. It is a dash. Nothing has been captured on this checkout yet, so
> the platform states no captured amount and no difference. It could easily print a number
> here and call it revenue we saved. It does not, because that number would not be true
> yet.
>
> And there, on the retained-revenue response: `controlled_scenario: true`. This is a
> reproducible scenario, not a production lift claim.

**Scroll to the proof chain verdict.**

> This is the Money Action Proof Chain, and it is fifteen separate checks rather than one
> green tick.
>
> The content hash, recomputed from the stored bytes rather than trusted. The policy
> receipt, bound to the version. The approval, bound to the content. The grant, bound to
> the decision. And this one — `grant_consumed_once`: one grant, one provider request
> recorded against it.
>
> `every_mutation_consumed_a_grant`: one provider mutation, zero without a grant.

*Optional, if the room is technical:*

> That claim is checkable underneath, in the database, and it is the strongest thing here.
> Seventeen calls have been made to `api.razorpay.com` by this system. Seventeen order ids
> came back. Every consumed grant matches exactly one network call — no grant with two, no
> call without one.
>
> And the ordering is the point. The grant is spent inside the committed transaction
> *before* the request goes out; the timestamps show the network call landing between a
> quarter of a second and three seconds afterwards, never before. So a crash between the
> two loses the money action rather than repeating it. In a payments system, losing an
> action is recoverable and repeating one is not.

**Scroll to the action timeline.**

> Every step is here in order, hash-chained. The scenario injection is in the middle,
> flagged. The kernel's denial is a row as durable as its approval — what was refused is
> evidenced exactly as well as what was allowed.

---

## Beat four — how it is built (3:45 – 4:45)

**On screen:** the architecture diagram, then the payment handoff screen.

> One sentence holds the whole architecture.
>
> **Agents propose. Deterministic systems authorize and execute.**
>
> The conversational agent can search the catalogue, read a basket and build a checkout.
> Ask the API what it can do and it answers with a list — and then a second list, called
> `absent_by_construction`: approve, reject, revoke authority. Those are not permissions
> the agent has been denied. They are capabilities that were never issued to it, so there
> is no path to abuse and no prompt that talks its way past one.
>
> Every money action goes through the Transaction Assurance Kernel. It versions the
> checkout. It binds a canonical hash to an explicit human approval. It fixes the terms in a
> Policy-at-Sale Receipt, so the sale can be explained months later against the rules that
> were actually in force. It admits exactly one execution under a single-use grant. And it
> writes the whole thing into a hash chain.

**Point at the payment handoff screen.**

> The API process holds no HTTP client for Razorpay at all. It cannot call the provider even
> if it wanted to. It writes one command to a durable outbox, in the same transaction as the
> decision. A separate worker picks that command up, spends the grant, and makes the call.
>
> And read this line, because it is the one that took the longest to get right: **your
> browser coming back is not proof that you paid.** When Razorpay returns the buyer here,
> that return is recorded as a claim and nothing more. Capture is applied only from
> Razorpay's own signed webhook or from the platform fetching the payment directly — through
> a monotonic apply, so a captured state can never regress and a replayed browser return
> cannot move money.

---

## Beat five — the close (4:45 – 5:30)

**On screen:** the refusal, again. End on it.

> The storefront is a deliberate, pixel-faithful clone of a quick-commerce app. That is on
> purpose. The shell has to be completely familiar so that the one unfamiliar thing in it —
> a checkout that refuses an approval it no longer trusts — is the only thing you have to
> think about.
>
> What is honestly not built, we have written down. The protocol layer is in progress.
> Voice is in progress. Autonomous Reserve Pay is held in Safe Mode, deliberately, because
> we are not going to ship a machine that pays without a human while claiming it is safe.
>
> What is built runs. Two hundred and forty-seven products. Three thousand three hundred and
> thirty-four backend tests, green in sixty-three seconds. Seventeen real Razorpay test-mode
> orders. And a kernel that has never once let a stale approval through.
>
> Most agentic commerce demonstrations show you a machine that can buy things.
>
> This one shows you a machine that *cannot* — until a person has agreed to the exact number
> it is about to spend.
>
> That refusal is the product.

---

## The figures this script quotes

Re-measured on 2026-09-05 against the running stack. Nothing here is copied from another
document.

| Claim in the script | Where it came from |
| --- | --- |
| ₹579.95 approved, ₹681.95 after, +₹102.00 | `capture-manifest.json` → `figures.refusal`; the run that wrote `06_the_refusal.png` |
| Milk ₹28.00 → ₹79.00, two units | `figures.injection.deltas` — `unit_price_minor` 2800 → 7900 |
| HTTP 200, `REAPPROVAL_REQUIRED` | `POST /v1/checkouts/{id}/versions/1/submit`, this run |
| Version 1 `INVALIDATED`, version 2 waiting | `figures.refusal.version_1_state`, `current_version: 2` |
| Seventeen calls to `api.razorpay.com`, all 200 | `select count(*), count(*) filter (where http_status=200), count(distinct provider_id) from provider_requests where url like '%/v1/orders'` → 17, 17, 17 |
| Every consumed grant matches exactly one call | the same table joined to `execution_grants`; grants with a count other than 1 → **0** |
| The grant is spent before the call | `p.request_at - g.consumed_at` across every row → between 0.26s and 3.41s, never negative |
| `absent_by_construction` | `GET /v1/agent/capabilities` → `["authority.revoke", "checkout.approve", "checkout.reject"]` |
| 247 products | `GET /v1/catalogue/products` → `matched: 247` across 10 categories |
| 3,334 backend tests in 62.84s | `uv run --no-sync python -m pytest packages -o addopts="" -q` |

Two figures the script deliberately does **not** quote, because the system will not state
them: captured revenue and net retained revenue. Nothing has been captured on the
demonstration checkout, so both read as a dash. Saying so out loud is worth more than a
number that would have to be walked back.
