/**
 * The happy path, against the running platform.
 *
 * Nothing here is stubbed and no route is intercepted. The catalogue this spec reads is
 * PostgreSQL through the Commerce API, the cart it writes is a real cart, and the
 * checkout it approves is a real immutable version with a real content hash and a real
 * stock reservation behind it. That is the only way this spec is worth running: the whole
 * claim of the storefront is that it renders what the platform sent and nothing else, and
 * a suite that hands it a fixture cannot tell whether it would.
 *
 * So each assertion about a figure or a name is cross-checked against the API's own
 * answer, fetched separately, rather than against a constant written here.
 */
import { expect, test } from "@playwright/test";

import {
  MILK_NAME,
  MILK_SKU,
  approveCurrentVersion,
  enterStorefront,
  openCheckoutForMilk,
  readCheckout,
  releaseCheckouts,
  visibleSearchBox,
} from "./journey";
import { API_BASE, UNREACHABLE, mintToken, requireApi, rupees } from "./live-api";

let reachable = false;
let token = "";

test.beforeAll(async ({ request }) => {
  reachable = await requireApi(request);
  if (reachable) token = await mintToken(request);
});

test.beforeEach(() => {
  test.skip(!reachable, UNREACHABLE);
});

test.afterEach(async ({ page }) => {
  // A checkout left open holds a unit of the merchant's stock for fifteen minutes.
  // Handing it back is what keeps this suite from refusing its own next run.
  await releaseCheckouts(page);
});

test("the home route offers the merchant real products, and invents no delivery time", async ({ page, request }) => {
  const answered = await request.get(`${API_BASE}/v1/catalogue/products`, {
    params: { limit: 20 },
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(answered.ok()).toBe(true);
  const page1 = (await answered.json()) as {
    products: Array<{ sku: string; display_name: string; unit_price: { minor: number } }>;
    matched: number;
  };
  expect(page1.products.length).toBeGreaterThan(0);

  await page.goto("/");
  await expect(page.locator("#copilot-composer")).toBeVisible({ timeout: 30_000 });

  // The weakest scan in this file, kept only because it costs nothing: before the shelf is
  // opened there is no product on this screen at all, so an absence here is satisfied by an
  // absence of products as easily as by an absence of invented promises. The pill this
  // guard exists for was drawn on product tiles, so the scans that can actually fail are
  // the ones below and in the search spec, each made only after that surface has been shown
  // to have drawn the merchant's products.
  await expect(page.getByText(DELIVERY_TIME)).toHaveCount(0);

  // The shelf is one press away inside the copilot rather than underneath it, so that is
  // where the "these are the merchant's real products" assertion now lives.
  const first = page1.products[0];
  await page.getByRole("button", { name: "Store" }).click();
  const shelf = page.locator('section[aria-label="The store"]');
  await expect(shelf).toBeVisible({ timeout: 30_000 });

  // The tile names the product and its price in one accessible name, both of them the
  // server's own -- a stronger statement than the old card made, because a screen reader
  // and a sighted buyer are told the same two facts from one source.
  const tile = shelf
    .getByRole("button", { name: new RegExp(`^${escapeForRegExp(first.display_name)}`) })
    .first();
  await expect(tile).toBeVisible({ timeout: 30_000 });

  // And the price as pixels, rendered from the server's integer paise by a formatter this
  // suite owns rather than by the one the application uses.
  //
  // Two decimal places, always. The tile draws `<Amount money={item.unit_price} />` with no
  // `whole` prop, so `formatMoney` keeps the paise: `₹255.00`, not the storefront grid's
  // compact `₹255`. Read off the tile located above rather than off the whole shelf, which
  // makes this "that product's tile carries that product's price" rather than "some tile
  // somewhere shows that number" — a claim a second product at the same price satisfied.
  await expect(
    tile.getByText(rupees(first.unit_price.minor), { exact: true }).first(),
  ).toBeVisible();

  // The shelf, scanned for the invented delivery time, on the surface that draws tiles.
  //
  // An absence proves nothing over an empty panel, so the tiles are counted first and the
  // count is held against the catalogue: the sheet asks the same unfiltered listing for 40
  // rows that this test asked for 20 of, so it cannot honestly draw fewer tiles than the
  // API returned rows. That makes the line below "no delivery time on any of these N
  // products" rather than "no delivery time on whatever happened to be rendered".
  const tiles = shelf.getByRole("listitem");
  expect(
    await tiles.count(),
    "the shelf drew fewer tiles than the catalogue returned rows, so the scan below would " +
      "have been looking at a shelf that never loaded",
  ).toBeGreaterThanOrEqual(page1.products.length);
  await expect(shelf.getByText(DELIVERY_TIME)).toHaveCount(0);

  // Where the old `href` assertion went, now that there is no href to read.
  //
  // The tile is a button: it opens the product inside the sheet instead of navigating, so
  // "this tile leads to that product" is settled by pressing it and reading back the
  // identity the shelf resolves -- the merchant's own product code, which the sheet holds
  // and this test did not supply. Going to `/p/{sku}` built from the API response, as this
  // test did for a while, asks nothing of the shelf at all: it would pass over a tile
  // wired to the wrong row, which is exactly the regression the href assertion caught.
  await tile.click();
  await expect(
    shelf.getByRole("heading", { name: first.display_name, exact: true }),
  ).toBeVisible({ timeout: 30_000 });
  await expect(shelf.getByText(first.sku, { exact: true })).toBeVisible();
  await expect(
    shelf.getByText(rupees(first.unit_price.minor), { exact: true }).first(),
  ).toBeVisible();
  // The product view is the second surface in the sheet that draws a product, and it is
  // drawn from the row the tile carried, so it is scanned too.
  await expect(shelf.getByText(DELIVERY_TIME)).toHaveCount(0);

  // The product's own page: what a link from outside lands on. Not a claim about the
  // shelf -- the tile settled that above -- but about the route resolving a SKU to the
  // merchant's own row. Name, code and price, all three the server's, and then the same
  // scan, because a product page is a surface that draws a product.
  await page.goto(`/p/${encodeURIComponent(first.sku)}`);
  await expect(
    page.getByRole("heading", { name: first.display_name, exact: true, level: 1 }),
  ).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(`SKU ${first.sku}`)).toBeVisible();
  await expect(page.getByText(rupees(first.unit_price.minor)).first()).toBeVisible();
  await expect(page.getByText(DELIVERY_TIME)).toHaveCount(0);
});

test("searching for `doodh` returns the merchant's hits, with the terms that matched", async ({
  page,
  request,
}) => {
  const answered = await request.get(`${API_BASE}/v1/catalogue/search`, {
    params: { q: "doodh", limit: 40 },
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(answered.ok()).toBe(true);
  const results = (await answered.json()) as {
    hits: Array<{
      sku: string;
      display_name: string;
      matched_terms: string[];
      stock_units: number;
      is_available: boolean;
    }>;
    normalized_query: string;
    freshness: { catalogue_revision: number };
  };
  expect(results.hits.length).toBeGreaterThan(0);
  const milkHit = results.hits.find((hit) => hit.sku === MILK_SKU);
  expect(milkHit, "the index no longer returns the milk every spec in this suite walks through")
    .toBeDefined();

  await enterStorefront(page);
  await visibleSearchBox(page).fill("doodh");
  await visibleSearchBox(page).press("Enter");
  await page.waitForURL(/\/search\?q=doodh/);

  await expect(page.getByRole("heading", { name: /Results for/ })).toBeVisible();
  // The count, the normalisation and the catalogue revision the index answered with —
  // all three come off the response, so a screen that made any of them up disagrees here.
  await expect(
    page.getByText(
      new RegExp(
        `${results.hits.length} products? · read as .${escapeForRegExp(results.normalized_query)}.`,
      ),
    ),
  ).toBeVisible();
  await expect(
    page.getByText(new RegExp(`catalogue revision ${results.freshness.catalogue_revision}`)),
  ).toBeVisible();

  const milkCard = page.getByRole("link", { name: new RegExp(escapeForRegExp(MILK_NAME)) }).first();
  await expect(milkCard).toBeVisible();

  // This grid is where the "8 MINS" pill actually lived: `ProductCard` keeps the pill's
  // corner and now prints the one fact the merchant's catalogue really sends about how
  // quickly a product runs out. So the guard belongs here as well as on the copilot's
  // shelf, and here it comes with a positive control rather than only a precondition --
  // the index says the merchant is holding units, so the card must draw a badge in that
  // corner, and a scan finding no delivery time in a card known to be rendering its pill
  // is a scan that looked at something.
  expect(
    milkHit!.is_available,
    "the merchant is not selling this milk right now, so its card draws no pill and the " +
      "control below would be asserting the wrong thing",
  ).toBe(true);
  expect(
    milkHit!.stock_units,
    "the merchant holds none of this milk, so its card draws no pill",
  ).toBeGreaterThan(0);
  await expect(milkCard.getByText(/^\d+ IN STOCK$/)).toHaveCount(1);
  await expect(milkCard.getByText(DELIVERY_TIME)).toHaveCount(0);

  // The chips are the grounding: the index says these terms did the work, and the screen
  // shows those terms rather than a guess about why the row came back.
  const chips = page.getByLabel("Matched on").first();
  await expect(chips).toBeVisible();
  const milkTerms = milkHit!.matched_terms;
  expect(milkTerms.length).toBeGreaterThan(0);
  await expect(chips.getByText(milkTerms[0], { exact: true }).first()).toBeVisible();
});

test("a buyer can fill a cart, open a checkout and approve the exact version on screen", async ({
  page,
}) => {
  const checkoutId = await openCheckoutForMilk(page);

  const opened = await readCheckout(page, checkoutId);
  expect(opened.state).toBe("APPROVAL_REQUIRED");
  expect(opened.current_version).toBe(1);
  const card = opened.approval_card;
  expect(card, "the checkout answered with no approval card to consent to").not.toBeNull();
  const amountMinor = card!.amount_minor;

  // The bytes this consent will be nailed to, taken from the same response as the amount.
  // Checked for shape before anything is concluded from it: `journey.ts` does not declare
  // this field, so it arrives through a cast, and a cast that quietly produced `undefined`
  // would let every comparison below succeed by comparing nothing with nothing.
  const bound = opened as unknown as BoundCheckout;
  const approvedHash = bound.approval_card?.content_hash ?? "";
  expect(
    approvedHash,
    "the approval card arrived without the content hash it binds consent to",
  ).toMatch(CONTENT_HASH);

  // The button names the amount, and the amount is the server's. This is the figure the
  // approval binds to, so it is the one assertion on this screen that has to be exact.
  //
  // It reads "Approve to pay" rather than "Approve" because the press is one act and the
  // label says so before it is pressed: consent is recorded and handed to the kernel in the
  // same gesture. The amount is still the whole point of the assertion.
  await expect(
    page.getByRole("button", { name: `Approve to pay ${rupees(amountMinor)}` }),
  ).toBeVisible();
  await expect(page.getByLabel(/You are approving this. RazorAI cannot./)).toBeVisible();

  // And the bytes are on the card, not only in the response: the buyer is shown the hash
  // before they press anything. Located by its accessible name rather than by sight,
  // because the evidence sits inside a closed `<details>` -- present in the DOM and read
  // out to a screen reader that asks for it. The claim is that the card carries this exact
  // hash, which is what makes "the exact version on screen" mean something below.
  await expect(page.locator(`[aria-label="Content hash: ${approvedHash}"]`)).toHaveCount(1);

  await approveCurrentVersion(page);

  // One press, and the checkout is past its approval rather than resting on it. APPROVED is
  // passed through in the same act, so this walk never observes it and asserting on it was
  // asserting a pause the platform does not take.
  //
  // Two states rather than one because the worker is already spending the grant while this
  // line runs: it has the create-order command in the outbox (EXECUTION_PENDING) or the
  // provider order back (AWAITING_PAYMENT). Those are the only two an approval that was
  // admitted and spent reaches on this walk. PAID needs a payment nobody has made yet, and
  // INVALIDATED_AWAITING_PAYMENT_RESULT means version 1 was invalidated after its payment
  // surface opened -- the opposite of the thing this test is named for -- so a run that
  // ends there has to fail here rather than be admitted by a wide enough set.
  const admitted = await readCheckout(page, checkoutId);
  expect(
    SPENT_STATES,
    `the checkout is ${admitted.state} after the approval was pressed`,
  ).toContain(admitted.state);

  // What "approve the exact version on screen" actually claims, said as three facts that
  // have to agree: the version carries a recorded approval at all; that approval names the
  // bytes the card displayed above; and those are the bytes of version 1's own immutable
  // document, computed by the server rather than echoed back from the browser. A press
  // that consented to a different version, or an API that stored whatever hash it was
  // handed, breaks one of the three.
  const admittedBound = admitted as unknown as BoundCheckout;
  const v1 = admittedBound.versions.find((version) => version.version === 1);
  expect(v1, "the checkout came back with no version 1 to have approved").toBeDefined();
  expect(v1!.amount_minor).toBe(amountMinor);
  // `?? null` because a field that never arrived is `undefined`, and `undefined` passes
  // `not.toBeNull()` — the check meant to catch a missing approval would have been the
  // thing that let it through.
  const record = v1!.approval ?? null;
  expect(
    record,
    "version 1 carries no approval record: nothing was recorded against the bytes on screen",
  ).not.toBeNull();
  expect(record!.version).toBe(1);
  expect(
    record!.content_hash,
    "the approval was recorded against different bytes than the card showed",
  ).toBe(approvedHash);
  expect(
    v1!.content_hash,
    "the bytes the approval bound are not version 1's own",
  ).toBe(approvedHash);
  expect(record!.amount_minor).toBe(amountMinor);

  // The version trail keeps the approved version, its amount and its hash on screen
  // afterwards, and stamps v1 with the approval record the server returned beside it —
  // which is the screen saying in its own words what the read above says, from a timestamp
  // and a hash it was sent rather than ones it drew.
  await expect(page.getByRole("heading", { name: "Version trail" })).toBeVisible();
  await expect(page.getByText("v1", { exact: true })).toBeVisible();
  await expect(page.getByText(rupees(amountMinor), { exact: true }).first()).toBeVisible();
  await expect(
    page.locator(`[aria-label="Version 1 content hash: ${approvedHash}"]`),
  ).toHaveCount(1);
  await expect(
    page.getByText(/^approved \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC$/).first(),
  ).toBeVisible();

  // Paying is still a control on the store's own trusted surface, never in the agent panel.
  // The surface carrying that label is the payment panel's now rather than a second screen
  // the buyer has to press through to reach: approving carried the checkout onto it, so the
  // frame is one press away instead of two and the claim about where money moves is
  // unchanged.
  //
  // Its button names the amount too, and that is worth asserting rather than dropping: the
  // figure on the pay control is the handoff's, and requiring it to equal the approval
  // card's is what says the amount was carried rather than recomputed on the way.
  const trusted = page.getByLabel("You are paying this. RazorAI cannot.");
  // The panel reads its handoff before it can draw this, which is a server round trip.
  await expect(trusted).toBeVisible({ timeout: 30_000 });
  await expect(trusted.getByRole("button", { name: `Pay ${rupees(amountMinor)}` })).toBeVisible();
});

/**
 * Where a checkout is once its approval has been accepted and spent, on this walk.
 *
 * Deliberately narrower than the set `journey.ts` polls with, which is right for a helper
 * that must also serve a spec driving a payment or an invalidation and wrong here: this
 * test presses approve and then looks at the payment panel, so PAID would mean money moved
 * without anyone paying and INVALIDATED_AWAITING_PAYMENT_RESULT would mean the version the
 * buyer approved was invalidated underneath them. Both are failures of what this test
 * claims, so neither is listed. Spelled out here rather than imported for the same reason
 * `rupees` is spelled apart from the application's formatter: a spec that shares its
 * expectation with the helper it is checking is only agreeing with itself.
 */
const SPENT_STATES = ["EXECUTION_PENDING", "AWAITING_PAYMENT"];

/**
 * A canonical content hash: base64url SHA-256 with the padding stripped, 43 characters
 * (`commerce_domain.hashing`). Matched before any comparison is drawn from a hash, so a
 * field that arrived missing or empty fails as itself instead of agreeing with another
 * missing field.
 */
const CONTENT_HASH = /^[A-Za-z0-9_-]{43}$/;

/**
 * The parts of `GET /v1/checkouts/{id}` that `journey.ts`'s `CheckoutRead` does not
 * declare, described here rather than widened there.
 *
 * The server sends all of it — `ApprovalCardOut.content_hash` and
 * `VersionSummaryOut.approval` in the Commerce API's schemas — and `CheckoutRead` is
 * shared by every spec in this suite, while this is the only one that reads the bytes.
 * Nothing is asserted through this description without a shape check first: a cast is a
 * claim about a payload, not a guarantee about one.
 */
interface BoundCheckout {
  approval_card: { content_hash: string } | null;
  versions: Array<{
    version: number;
    amount_minor: number;
    content_hash: string;
    approval: { version: number; content_hash: string; amount_minor: number } | null;
  }>;
}

/**
 * A delivery time in minutes, in the two forms this storefront has actually invented one
 * in: the pill's `8 MINS` and the header note's `Delivery in 8 minutes`.
 *
 * Both were promises about the buyer's own order that no response supports. The only
 * fulfilment figure `ProductOut` carries is `delivery_promise_days`, in whole days, and no
 * surface here renders even that, so any number of minutes on any of these screens was
 * drawn rather than read.
 *
 * The one form deliberately left out is the singular "minute": this merchant's own
 * catalogue sells "Maggi 2-Minute Masala Noodles", and a guard that failed the suite over
 * a product's name would be deleted by the first person it stopped. That is a real hole —
 * a pill reading "8 Minute" would walk through it — and it is a narrower hole than the
 * false positive it avoids. `MINS?\b` does not match "Minute" either, for the same reason
 * and by the same word boundary.
 */
const DELIVERY_TIME = /\b\d+\s*(?:mins?|minutes)\b/i;

/** A product name can contain regex metacharacters; it is data, not a pattern. */
function escapeForRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
