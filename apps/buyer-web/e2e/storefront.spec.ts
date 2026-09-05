/**
 * The happy path, against the running platform.
 *
 * Nothing here is stubbed and no route is intercepted. The catalogue this spec reads is
 * PostgreSQL through the Commerce API, the basket it writes is a real basket, and the
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

test("the home page renders products the catalogue actually holds", async ({ page, request }) => {
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
  await expect(page.getByRole("heading", { name: "Best sellers" })).toBeVisible();

  // The same products, by name and by price, and the price is the server's integer paise
  // rendered by an independent formatter in this suite.
  const first = page1.products[0];
  const card = page.getByRole("link", { name: new RegExp(escapeForRegExp(first.display_name)) }).first();
  await expect(card).toBeVisible();
  await expect(card).toHaveAttribute("href", `/p/${first.sku}`);
  await expect(page.getByText(compactRupees(first.unit_price.minor), { exact: true }).first()).toBeVisible();

  // Nothing on this page promises a delivery time, because no catalogue response carries
  // one. This is the assertion that would have caught the "8 MINS" pill.
  await expect(page.getByText(/\b\d+\s*MINS?\b/i)).toHaveCount(0);
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
    hits: Array<{ sku: string; display_name: string; matched_terms: string[] }>;
    normalized_query: string;
    freshness: { catalogue_revision: number };
  };
  expect(results.hits.length).toBeGreaterThan(0);
  expect(results.hits.some((hit) => hit.sku === MILK_SKU)).toBe(true);

  await page.goto("/");
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

  await expect(page.getByRole("link", { name: new RegExp(escapeForRegExp(MILK_NAME)) }).first()).toBeVisible();

  // The chips are the grounding: the index says these terms did the work, and the screen
  // shows those terms rather than a guess about why the row came back.
  const chips = page.getByLabel("Matched on").first();
  await expect(chips).toBeVisible();
  const milkTerms = results.hits.find((hit) => hit.sku === MILK_SKU)?.matched_terms ?? [];
  expect(milkTerms.length).toBeGreaterThan(0);
  await expect(chips.getByText(milkTerms[0], { exact: true }).first()).toBeVisible();
});

test("a buyer can fill a basket, open a checkout and approve the exact version on screen", async ({
  page,
}) => {
  const checkoutId = await openCheckoutForMilk(page);

  const opened = await readCheckout(page, checkoutId);
  expect(opened.state).toBe("APPROVAL_REQUIRED");
  expect(opened.current_version).toBe(1);
  const card = opened.approval_card;
  expect(card, "the checkout answered with no approval card to consent to").not.toBeNull();
  const amountMinor = card!.amount_minor;

  // The button names the amount, and the amount is the server's. This is the figure the
  // approval binds to, so it is the one assertion on this screen that has to be exact.
  await expect(page.getByRole("button", { name: `Approve ${rupees(amountMinor)}` })).toBeVisible();
  await expect(page.getByLabel(/You are approving this. RazorAI cannot./)).toBeVisible();

  await approveCurrentVersion(page);

  const approved = await readCheckout(page, checkoutId);
  expect(approved.state).toBe("APPROVED");
  expect(approved.versions.find((version) => version.version === 1)?.amount_minor).toBe(amountMinor);

  // The version trail keeps the approved version and its amount on screen afterwards.
  await expect(page.getByRole("heading", { name: "Version trail" })).toBeVisible();
  await expect(page.getByText("v1", { exact: true })).toBeVisible();
  await expect(page.getByText(rupees(amountMinor), { exact: true }).first()).toBeVisible();

  // Paying is a control on the store's own trusted surface, never in the agent panel.
  const trusted = page.getByLabel("You are paying this. RazorAI cannot.");
  await expect(trusted).toBeVisible();
  await expect(trusted.getByRole("button", { name: "Pay", exact: true })).toBeVisible();
});

/** The grid's compact form: `.00` dropped only when the amount is exact rupees. */
function compactRupees(minor: number): string {
  const full = rupees(minor);
  return full.endsWith(".00") ? full.slice(0, -3) : full;
}

/** A product name can contain regex metacharacters; it is data, not a pattern. */
function escapeForRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
