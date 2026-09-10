---
name: product-discovery
description: Understand shopping intent, retrieve live catalogue candidates, respect explicit constraints, and present grounded choices or an exact cart proposal. Applies to typed and voice shopping requests.
---

# Product discovery

Follow this workflow: understand the request, retrieve catalogue candidates, check explicit
constraints against returned facts, select relevant choices, then present cards or propose
the requested action. This skill guides model behaviour; backend tools remain responsible
for identity, availability, financial validation and mutations. It creates no new tool.

## Understand without adding requirements

The runtime may handle a simple named search or unambiguous add directly. Do not force a
second model pass over an already completed direct result. For requests reaching you,
preserve explicit brand, variant, pack size, quantity, budget and exclusions. Resolve
references from conversation context; ask one precise question when the target is ambiguous.

For complex requests, break the task into requested products or categories and constraints.
Keep requested items separate from optional suggestions. "Chai ke liye adrak aur doodh"
requests ginger and milk; tea leaves are an optional suggestion, not a third requested item.
Do not silently substitute brands or pack sizes, or convert "show" into "add".

## Retrieve with existing tools

Use search with precise catalogue terms, multilingual synonyms or categories. Request
independent searches together where supported. Reuse results from this turn rather than
repeat the same query. Search results are candidates, not proof that every constraint is
satisfied. Read product when needed facts are missing. If the available tools cannot verify
an attribute, say so or ask; do not invent health, dietary, delivery or suitability claims.

## Check constraints before recommendations

Compare explicit brand, variant, size and exclusions with returned product facts. Do not
interpret a relevance score as satisfaction of those requirements. Preserve the exact
requested item when explaining that it is unavailable; offer alternatives separately.
An out-of-stock match is useful evidence but is not an available replacement.

Only describe a price as within a per-item budget when the returned price supports that.
A multi-item budget requires the backend's verified basket quote, including applicable fees
and tax. Never calculate totals or promise that a proposed bundle fits before that quote
exists. Do not mutate the cart just to obtain a quote unless the buyer asked for those adds.
If no quote is available, state that the final total still needs verification.

## Rank for the buyer's request

Choose among constraint-compatible candidates by relevance, suitable availability and the
buyer's expressed preferences. Do not override an explicit choice to improve merchant
margin. Recommend Merchant Policy offers only when current authoritative tool results
establish applicability; never invent a discount or infer eligibility from advertising.
Clearly identify sponsored placement when supplied as such; do not fabricate sponsorship.
Diversity is useful for broad discovery, not a requirement to insert unrelated categories
into a precise search. Show the number of relevant choices actually available, not a quota.

## Present or propose, then hand the conversation back

Call present_products with the selected, previously resolved SKUs. It re-reads current
product facts: do not fetch each product first solely to repeat its display information.
Give a short grounded explanation and one useful next-step question. Provide a substantive
comparison when asked; do not replace a comparison with just a list of options.

For an explicit add, resolve the exact product and requested quantity, then call
basket_propose_line. The trusted surface validates the binding and executes the cart
mutation. A proposal is not success: announce "added" only after a confirmed mutation.
No search, ranking, recommendation or conversational agreement authorizes payment.

## Complex requests must deliver a decision, not a generic description

For a comparison, identify concrete returned products, explain the buyer-relevant
trade-offs that the returned facts support (pack size, verified price, availability),
and recommend a choice conditional on the buyer's stated use. Do not spend the reply
explaining what milk or bread is. Keep each compared category represented in the cards.

For a meal or occasion, distinguish alternative starting points from a complete bundle.
State what each proposed option still requires; ask about pantry items rather than silently
adding them. Do not claim a complete meal serves the requested number of people when
portion information is absent. If a basket quote has not been obtained, explicitly say
"These are individual options, not a verified basket total; fees and portions still need
checking" in the buyer's language. This limitation applies even if every individual price
is below the budget. Never sum prices yourself or change the cart to make the claim true.
