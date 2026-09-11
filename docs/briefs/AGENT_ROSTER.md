# Agent roster — the definitive list

Drawn from `PROJECT_SPECIFICATION.md` section 6. It is the authority for which agents exist,
what each one may hold, and what it may never do; where it and the specification disagree,
this file is what is built.

**Two copilot harnesses. Five specialist agents. Three deterministic services.** Only the
five specialists are models; everything else is code.

All of it now exists, and the gap worth naming is not in the roster but in the tool factory:

| Claim | Count | How to check it |
| --- | ---: | --- |
| Specialist modules | 5 | `ls packages/agent-runtime/src/agent_runtime/specialists/{shopping,checkout,support,growth,case}.py` |
| Prompt files, one per specialist | 5 | `ls packages/agent-runtime/src/agent_runtime/prompts/*.md` |
| Harnesses | 2 | `harness/razorai.py`, `harness/merchant_copilot.py` |
| Deterministic services | 3 | `commerce_api/services/{reconciliation,resolution,human_review}_service.py` |
| Registry A tools — every tool an agent may ever hold | 24 | `python -c "from agent_runtime.capabilities.registry import REGISTRY_A; print(len(REGISTRY_A))"` |
| …of which a factory builder exists for | 19 | the union of the five tool lists below is exactly Registry A; `capabilities/tools.py` builds 19 of them |
| `agent-runtime` tests passing | 624 | `pytest packages/agent-runtime` |

The three roster tools with no builder are `policy_search`, `resolution_evaluate` and
`support_escalate` — the rest of the Support surface. They are **reported, never faked**:
`BoundToolset.unbuilt` names them, a test asserts that an unbuilt tool is never offered to a
model, and the suite raises a warning listing them on every run. A roster entry is a
commitment about authority, not a promise that the tool is wired; the two are deliberately
allowed to differ, and the difference is made loud.

The Case Specialist's two are built. `CaseBackend` in `backends/base.py` is the seam they
needed — a third protocol beside `MerchantBackend`, so that a backend built for a buyer
session cannot reach the review queue at all — and the factory builds `support_case_read`
and `present_case` only against a backend that has it. What was built departed from this sketch in
three places, each recorded beside the implementation.

---

## The shape

**A copilot is a harness, not an agent.** It is ordinary Python: it owns the session, the
tenant, the locale, the modality, the correlation identifier, the transcript, and the
binding of a principal to the tool set that principal may hold. It decides which specialist
runs. It calls no model of its own.

Only the specialists are `LlmAgent`s.

```
RazorAI  (harness, Python)          Merchant Copilot  (harness, Python)
  owns session, tenant, locale,             owns merchant session, tenant,
  modality, correlation, transcript;        correlation, transcript;
  binds principal -> tools;                 binds principal -> tools;
  routes to a specialist                    routes to a specialist
        │                                          │
        ├── Shopping Specialist   (LlmAgent)       ├── Growth Specialist  (LlmAgent)
        ├── Checkout Specialist   (LlmAgent)       └── Case Specialist    (LlmAgent)
        └── Support Specialist    (LlmAgent)
```

**Five agents. Two harnesses.** Specification section 6 counts the two coordinators among
its six internal agents. They are not agents here, and the difference is not cosmetic.

Why the coordinator is not a model:

- **Routing by model is a non-deterministic hop on the money path.** The project's whole
  claim is that everything except proposing is deterministic. Intent routing can be decided
  from what the buyer is looking at and a cheap classifier, and when it is wrong a
  specialist hands back explicitly. A model deciding it costs a call, adds latency, and is
  the one part of the flow that cannot be unit-tested for certainty.
- **Everything section 6.1 asks a coordinator to do is plumbing.** Preserve session, tenant,
  locale, modality and correlation identifiers. Coordinate fallback when speech fails. Keep
  one commerce session across voice and chat. Python does that reliably; a model can drop a
  correlation identifier and be fluent about it.
- **The harness is where authority is granted.** Binding a principal to a tool set is the
  security-critical moment in this design. It belongs in code with tests, not behind a model
  that decides which specialist to delegate to.
- **A sub-agent's capabilities are a subset of its parent's.** With a harness that rule is
  enforced at the moment of binding and is trivially testable. With a root agent it depends
  on the delegation actually happening the way the prompt intended.

What the harness owns, and never an agent:

| Harness (Python) | Specialist (LlmAgent) |
| --- | --- |
| Session, tenant, buyer or merchant identity | The conversation |
| Locale detection and modality | Phrasing, in three languages |
| Correlation and causation identifiers | Which tool to call, and with what arguments |
| Principal, and the tools it may hold | Interpreting a tool result into an explanation |
| Routing to a specialist | Asking a clarifying question |
| The transcript and the tool-call log | Nothing about session, tenant or authority |
| Running the grounding post-check on the reply | |

Specification 6.1's conversational duties — summarising a specialist's result without
altering its authoritative fields, never presenting an interim transcript as confirmed
intent, never summarising away a changed price — survive as **rules the harness enforces**
rather than instructions a coordinator model is asked to follow. A rule enforced by code is
worth more than the same rule written in a prompt.

## How many do we actually need

**For P0 quick commerce, four is the right number**, and the two that matter are the two
that carry the eleven-step demonstration.

| Agent | Needed in P0? | Why |
| --- | --- | --- |
| RazorAI harness | **Yes, first** | Nothing is reachable without it, and it binds the principal to its tools |
| Shopping Specialist | **Yes, first** | Steps 1 and 2 of the demonstration |
| Checkout Specialist | **Yes, first** | Steps 3 to 9, including the refusal |
| Support Specialist | Yes, but later | Needs the Resolution Service before it can quote anything |
| Merchant Copilot harness | Later | The console already shows evidence without conversation |
| Growth Specialist | Later | Proposals are a pitch asset, not a demonstration blocker |
| Case Specialist | **Built, and thin** | P0's queue is read-only, so it presents and explains and decides nothing |

If the deadline bites, ship **Shopping Specialist plus Checkout Specialist under the Buyer
Copilot root**. That is a complete agentic purchase with a governed refusal, which is the
entire Track 1 claim. Support and the merchant pair are additive.

Do not split Discovery from Basket for quick commerce. Browsing and cart-building are one
continuous activity for a grocery buyer, and two agents would hand work back and forth
across a boundary the buyer does not perceive.

## Adapting to another vertical, for example an airline

This is the part worth designing now, because it is the difference between a demo and a
platform, and specification 7.3 already promises it.

**The money path is vertical-neutral.** Product, basket, quote, reservation, checkout
version, approval, order, payment attempt, refund, policy decision, proof chain and audit
event are stable primitives. Nothing in the kernel, the grants, the reservations or the
proof chain knows what is being sold.

That means, moving from quick commerce to an airline:

| Component | Changes? |
| --- | --- |
| Transaction kernel, grants, receipts, proof chain | **No.** Not one line |
| Checkout Specialist agent | **No.** A seat hold is a reservation; a fare is a quote |
| RazorAI harness | **No.** Session, locale and routing are the same shape |
| Support and Case Specialists | Mostly no. Same authority rules; the resolution options differ |
| Shopping Specialist | **Yes.** This is the vertical-specific one |
| Catalogue, inventory, pricing, fulfilment adapters | **Yes.** Typed adapters, per 7.3 |

So onboarding a vertical is a connector plus one agent's tools and prompt, not a rewrite.
For a Zepto-style store, discovery searches SKUs and the basket holds quantities. For an
airline, discovery searches itineraries across dates and fare classes, and the "basket" is
an itinerary plus ancillaries: seats, baggage, meals. Same primitives underneath, different
shape on top.

**Two agents an airline would eventually add**, and neither is needed now:

- **Fare & Ancillary** — seat maps, baggage and meals. In quick-commerce terms this is the
  basket agent, but airline ancillaries are complex enough to deserve their own tools and
  prompt.
- **Itinerary Change** — reschedule and cancel under fare rules. This is Support-adjacent
  but larger, because a change is a new priced transaction rather than a refund, and it
  goes through the same approval and admission path as an original purchase.

Neither belongs in P0. They are listed so the boundary is drawn in the right place today:
**keep everything vertical-specific inside Shopping Specialist and the adapters, and let
nothing vertical-specific leak into Checkout Specialist.** If a checkout agent ever needs to
know it is selling groceries, the abstraction has failed.

Deterministic services behind Support, which are **code, not agents**: the Reconciliation
Service, the Resolution Service and the Human Review queue. An agent may read their output
and explain it. An agent may never do their job.

---

## 1. RazorAI — the root agent (spec 6.1)

Owns the buyer conversation across text and voice.

- Detects intent and routes to discovery, checkout or support.
- Preserves session, tenant, locale, modality and correlation identifiers across turns.
- Summarises other agents' results **without changing their authoritative fields**.
- Coordinates fallback to text when voice fails.

Hard rules:
- A sub-agent receives no capability the coordinator does not itself hold.
- Never present an interim speech transcript as confirmed intent.
- Never summarise away a material difference in price, fee, item, quantity, delivery or
  refund. If a number changed, the buyer sees it.
- Never claim payment success from conversation state. Payment success comes from verified
  provider evidence, never from what the conversation believes.

## 2. Shopping Specialist (spec 6.2)

The shopping agent: search, compare, recommend, and build the cart.

Tools: `catalog.search`, `catalog.get_product`, `inventory.check`, `basket.create`,
`basket.update`, `quote.request`, `reservation.request`.

Hard rules:
- Every proposed item resolves to an active catalogue identifier in the same tenant and
  store. No invented products.
- Results carry source, freshness and serviceability.
- A product description is untrusted data. It cannot create an instruction or a tool call.
- **The fee engine computes the free-delivery gap; the agent only phrases the nudge.** The
  agent performs no arithmetic on money.
- Cross-sell must satisfy merchant margin, category, inventory, budget and discount rules.
- Reorders are always revalidated for stock and price and need a fresh approval.
- No pressure, no fabricated scarcity, no hidden fees.

## 3. Checkout Specialist (spec 6.3)

Orchestrates the verified checkout lifecycle and its recovery paths.

Tools: `quote.request`, `reservation.request`, `checkout.submit_for_approval`,
`checkout.submit_approved`, `order.track`, `order.propose_cancel`, `refund.propose`.

Hard rules:
- It may submit an **already-approved** version. It can never approve, pay, refund or
  revoke; those are trusted-surface actions the human performs.
- It explains revalidation, price and stock changes, payment state and order state.
- It proposes cancellation or refund; it never executes one.
- On a refusal it renders **every** delta and says version N is invalidated and version N+1
  needs approval. This is the demonstration's hero moment.

## 4. Support Specialist (spec 6.4.4)

Post-purchase, over verified state only.

Tools: `order.track`, `checkout.read`, `policy.search`, `resolution.evaluate`,
`support.escalate`, `support.case.read`.

Authority, and this matters more here than anywhere:
- Razorpay is authoritative for payment and refund state.
- The merchant connector is authoritative for fulfilment state.
- The Resolution Service is authoritative for what is owed.
- The **Policy-at-Sale Receipt** is authoritative for which rules apply. A merchant who
  tightened their refund rule yesterday cannot retroactively narrow a sale made last week.

Hard rules:
- **Never state an amount that did not come from a resolution plan or a verified provider
  record. The agent performs no arithmetic on money.**
- Never promise a refund before kernel admission and provider confirmation.
- Cash refund stays available whenever store credit is offered.
- Never ask for card details, a UPI PIN, a one-time password, an API key or a private key.
- On escalation it gives the case reference, states what has been verified and what happens
  next, and does not predict an outcome or promise a timeline beyond the recorded target.

## 5. Merchant Copilot — the root agent (spec 6.5)

One coherent merchant-facing assistant.

- Routes onboarding and configuration questions to deterministic services.
- Routes catalogue, inventory, policy, operations and growth work to Merchant Operations.
- Presents proposals and evidence without applying sensitive changes itself.

Hard rules:
- Merchant instructions are lower-precedence data and cannot change system invariants.
- Tenant comes from the merchant's authenticated server session, never from the message.
- Sensitive policy changes need ETag/If-Match and merchant-admin authorisation.
- **Money approval is never a merchant-configurable switch that can be turned off.**
- Synthetic analysis is visibly labelled.

## 6. Growth Specialist (spec 6.6)

Analysis and growth proposals over deterministic data.

Tools: `merchant.catalogue_health.read`, `merchant.inventory_anomalies.read`,
`merchant.checkout_metrics.read`, `merchant.growth_proposal.create`.

Hard rules:
- Read-only by default.
- **A proposal cannot directly change price, stock, discount, fee, campaign budget, refund
  rule or financial authority.** It proposes; a human applies.
- Recommendations cite source window, sample size, and whether the data is synthetic.
- A discount recommendation must show gross revenue, discount cost, and net captured and
  retained revenue.
- Never infer cross-merchant benchmarks from data it does not have.

---

## The three services that are not agents

Each one decides money, which is why none of them is a model.

- **Reconciliation Service** (6.4.1): leases work with `FOR UPDATE SKIP LOCKED`, queries the
  provider by authoritative identifiers only, applies transitions monotonically from
  verified evidence, records one row per attempt, and escalates after bounded attempts. A
  capture is never regressed. Unknown is never turned into failed because something timed
  out.
- **Resolution Service** (6.4.2): decides what is owed, from the order, the verified payment
  state and the **Policy-at-Sale Receipt**. Total refundable never exceeds captured minus
  refunds already issued or pending. A plan is immutable and expires; an expired plan is
  re-evaluated, never reused.
- **Human Review** (6.4.3): one case per escalation, carrying the reason code, a redacted
  timeline, the proof-chain reference and the verified provider state. In P0 the case is
  created and shown in a read-only queue. **No human resolves it during the demonstration.**

---

## Why a prompt cannot grant authority

The prompt files and the enforcement machinery are separate on purpose, and the separation
is what makes the prompts safe to iterate on.

**A prompt cannot grant a capability**, because every tool call is checked against the
principal's granted capabilities at the tool layer. A prompt that asks for something
forbidden is denied by machinery however it is worded. Prompt text is therefore the one part
of this system where a mistake is a quality problem rather than a security problem — which is
exactly why it is kept out of the layer where a mistake would be neither.

Attaching a tool to an agent is the moment authority is granted. So one rule carries the
whole arrangement: **an agent's tools come only from the factory in `capabilities/tools.py`.**
Never construct a tool directly. The factory is what applies the gate, and a hand-built tool
bypasses it silently. A test enforces this rather than a sentence in a document.

The corollary is what the fence exists for. A prompt file may not restate the fence label —
the notice is one paragraph the loader injects, once, so that a prompt cannot weaken the
boundary by paraphrasing it. ADR 0004 §1.1 has the mechanism.

## The order this was built in, and where it stopped

1. **Shopping Specialist and Checkout Specialist**, which between them carry the eleven-step
   demonstration. Both are complete: every tool on their two lists has a factory builder.
2. **RazorAI**, so the two above are reachable from one conversation.
3. **Merchant Copilot and the Growth Specialist.** Complete — all four merchant reads build.
4. **Case.** Complete: `CaseBackend` reaches `human_review_service` through the API's
   existing review routes, and both case tools build against any backend carrying it.
5. **Support.** The module, the prompt and the roster entry exist; `policy_search`,
   `resolution_evaluate` and `support_escalate` have no builder yet, because they need the
   Resolution Service and the Policy-at-Sale Receipt reached the way the queue now is.

The ordering was chosen so that the thing being demonstrated was reachable first. It also
means the incomplete surface is the post-purchase one: nothing on the money path is waiting
on a tool that does not exist, and a support agent that cannot yet quote a resolution says
so rather than guessing at one. Of the three still missing, two would change state, which
is the honest reason they are last rather than an accident of sequencing.
