# flight-sim — a second commerce vertical, stood down

**Status: stopped on 5 September 2026, deliberately, before integration.** The domain model
here is finished and verified. The storefront, the API wiring, the agent prompts and the
console tenant switcher were never written. Nothing in this package is reachable from the
running demo, and nothing outside this package was changed.

It is kept because the *question* it answers is worth having on the record: does this
platform generalise past a grocery list, and if not, exactly where does it stop?

## What was measured

Live research against `goindigo.in` on 5 September 2026, DEL→BOM for 6 September. Measured
values, not impressions:

| | |
|---|---|
| Brand blue | `#000099` — CTA fills, wordmark, route pill |
| Ink / muted / faint | `#25304B` / `#4B5772` / `#7A85A0` |
| Type | Poppins (SemiBold 361 uses, Regular 252, Medium 47); BauhausStd-Medium for display |
| Sizes | 16px base, 14px secondary, 12px small, 44px display |
| Radius | 12px dominant, then 16px and 32px; pills at 100–120px |
| Shadow | `rgba(76,93,158,0.08) 0 -12px 24px` — blue-tinted, never grey |

The fare tray for 6E 6470 offered four economy tiers — Lite ₹6,057, Saver ₹6,529, Flexi
Plus ₹6,844, UpFront ₹9,154 — differing by check-in baggage (0/15/15/20 kg), change rights
(standard/standard/partial/zero beyond 72h) and perks. "View Details" broke Lite down as
**base airfare ₹4,395 + taxes and fees ₹1,662 = ₹6,057**, with a disclaimer naming a
non-refundable convenience fee of up to ₹499 per passenger per segment.

`fares.py` reproduces all four of those totals to within a rupee, checked by assertions at
import. That is the thing worth keeping: the numbers are not invented.

## The mapping that worked

The kernel's canonical content shape is closed — `LINE_KEYS` is exactly
`{sku, name, quantity, unit_minor, line_minor, tax_minor}`. Flights land on it with no
kernel change at all:

| Kernel key | Flight meaning |
|---|---|
| `sku` | `6E2134-20260912-SAVER` — one flight × date × fare family |
| `quantity` | **passengers** — and `reservations.py` derives held inventory from exactly this |
| `unit_minor` | base airfare per passenger |
| `tax_minor` | GST plus flat statutory charges for the departure airport |
| `delivery_fee_minor` | the convenience fee |

The strongest finding is that **a seat hold is not analogous to a stock reservation; it is
the same database row**. `merchant_sim.SimMerchantStateSource` satisfies admission step 8
for an airline *unmodified*, because "re-quote the approved lines against current state" is
already vertical-neutral. `checkout_specialist`'s own docstring anticipated this: *"a seat
hold is a reservation and a fare is a quote."*

## The one seam it needs — and does not have

`MerchantStore.__init__` seeds from the module-level grocery `CATALOGUE` and resolves SKUs
against the module-level `PRODUCTS_BY_SKU`. Loading flight products into a store therefore
requires one strictly-additive change: a `catalogue=` constructor argument defaulting to the
grocery fixture, plus `self._by_sku` instead of the module global.

That change was made (36 lines), verified green (`merchant-sim` 167 passed), and then
**reverted** when this work was stood down. Without it, `projection.products_for` is a
correct projection with nowhere to put its output. Everything else in this package works
standalone.

## Where flights genuinely strain this architecture

This is the part worth reading. Five things, in order of how much they matter.

**1. `delivery_fee_minor` is a grocery name on a hashed document.** The convenience fee maps
onto it semantically without a wrinkle — both are a non-item charge added at checkout — but
the *key* says "delivery". It cannot be renamed: it is inside the hashed content document,
so a rename means bumping `CONTENT_VERSION`, which invalidates every stored approval,
receipt and grant. The platform's second vertical is therefore permanently reading a
grocery word. That is a real cost of freezing the content shape early, and it is the right
trade, but it should be named rather than discovered later.

**2. Tax is not a rate, and the content shape assumes it is.** Indian airfare tax is 5% GST
*plus flat per-passenger statutory charges* (UDF, security fee) that do not scale with the
fare. The fee engine takes one `tax_bp` per line and computes `(base × bp + 5000) // 10000`.
Flat charges cannot be written as a rate in general. This package escapes by computing the
rate *per SKU* — legitimate, because each SKU has exactly one known base fare, and it
reproduces the measured tax to the paisa — but it is an escape, not a fit. Any vertical with
a genuine per-unit flat fee (booking fees, deposits, per-item levies) will hit the same wall
and may not have a per-SKU escape available.

**3. Discovery does not generalise; only pricing and inventory do.** A grocery buyer asks by
token and gets a ranked list. A flight buyer asks by origin, destination and date and gets
an exact set. Token search over fare SKUs would rank "6E 2134 Saver" above "6E 2134 Lite"
for reasons no traveller would accept. `search.py` is the one module here that had to be
written from scratch, and the lesson is that **a vertical's shape lives in its discovery**,
not in its money.

**4. The agent roster is closed, and that is load-bearing in both directions.** `ACTIONS` in
`specialists/_spec.py` is a frozen table and `AgentRole` is a closed enum pinned by
`test_five_specialists_two_surfaces`. Flight search needs no new action — it is
`catalog.search`; picking a fare is `basket.update` — so the closure is *vindicated*. But it
also means a second vertical cannot get its own specialist without editing the registry, the
harness, routing, the prompt loader and that test. The vertical belongs in the **prompt and
the catalogue adapter**, not in a new agent role. That is a good constraint, arrived at
uncomfortably.

**5. `Category` is closed and fully populated by a test.** `test_catalogue.py` asserts the
grocery catalogue uses every member of `Category`, so a second vertical cannot add one
without breaking a grocery test. This package brings its own `FlightCategory` StrEnum, which
works because the value is only ever consumed as a string — but it means the type hint on
`Product.category` is now a polite fiction, carried with a `type: ignore`. A tag vocabulary
that a second tenant cannot extend is a small design smell worth knowing about.

## What is not modelled, and was never going to be

Seat maps, meals, round trips, multi-city, loyalty (BluChips), interline and codeshare,
timezone-correct international arrival, and cabin classes above economy. The fixture holds
one carrier because a multi-carrier fixture needs interline settlement rules this project
makes no claim about.

## Provenance of `network-partial.json`

146 directed city pairs and 548 departures across India's twenty main cities, produced by a
six-agent fan-out with three adversarial verification lenses per batch (time arithmetic,
fare-versus-sector, identity and equipment). **Synthetic**, modelled on IndiGo's real
network shape and calibrated against the one real fare breakdown above. It was never
scraped and must never be presented as live schedule data.
