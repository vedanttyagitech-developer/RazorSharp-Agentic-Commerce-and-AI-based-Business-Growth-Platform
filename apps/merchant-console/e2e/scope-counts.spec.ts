/**
 * Counts span the scope, never the page.
 *
 * `GET /v1/orders` and `GET /v1/refunds` each return a page of rows *and* a `counts` map
 * computed across the whole tenant. Those are different questions and the console must
 * never answer the second with the first: an operator reading a queue depth off a truncated
 * or filtered list is an operator about to draw the wrong conclusion about how much work is
 * outstanding.
 *
 * The trap is that on a small tenant the two answers coincide. Eight orders at a page size
 * of twenty-five means "counted across the tenant" and "counted off the page" both say
 * eight, and a test that only compared the chip to the API on an unfiltered page would pass
 * against a console that derived its counts from the rows.
 *
 * So the assertions here are the ones that come apart:
 *
 *  - **Filter to a state and the counts must not move.** A page showing only CONFIRMED rows
 *    still reports how many CANCELLED there are. Derive from rows and every other chip
 *    drops to zero.
 *  - **Filter to a state with no rows at all.** The page is then empty and every count must
 *    still be the tenant's. This is the killer: a row-derived implementation reports zero
 *    for everything, including the states that do have rows.
 *  - **Shrink the page below the tenant.** Ask for one row and the counts must still
 *    describe all of them.
 */
import { expect, test, type Page } from "@playwright/test";

import {
  count,
  platformUnreachable,
  read,
  refundTile,
  REFUND_MEANINGS,
  type CataloguePage,
  type OrdersPage,
  type RefundsPage,
} from "./support";

let unreachable: string | null = null;

test.beforeAll(async () => {
  unreachable = await platformUnreachable("the scope-counts suite");
});

test.beforeEach(() => {
  test.skip(unreachable !== null, unreachable ?? "");
});

/** The filter chip for one state, which carries that state's count beside its name. */
function chip(page: Page, state: string) {
  return page.getByRole("button", { name: new RegExp(`^${state}`) });
}

/** Every count the API reported, asserted on the chips, whatever the page is showing. */
async function assertChipsCarry(page: Page, counts: Record<string, number>): Promise<void> {
  for (const [state, value] of Object.entries(counts)) {
    await expect(
      chip(page, state),
      `the ${state} chip lost its tenant-wide count of ${value}`,
    ).toContainText(count(value));
  }
}

test("order counts survive a filter that empties the page", async ({ page }) => {
  await page.goto("/operations?tab=orders");
  const all = await read<OrdersPage>(page, "/api/backend/v1/orders?limit=25");
  await assertChipsCarry(page, all.counts);

  // A state the tenant genuinely has none of. If none exists the tenant is too full for
  // this test to say anything, which is worth saying out loud rather than passing quietly.
  const empty = Object.entries(all.counts).find(([, value]) => value === 0)?.[0];
  const populated = Object.entries(all.counts).filter(([, value]) => value > 0);
  test.skip(
    empty === undefined || populated.length === 0,
    "This tenant has no empty order state and no populated one at the same time, so " +
      "filtering cannot separate a scope count from a page count here.",
  );

  await chip(page, empty!).click();
  const filtered = await read<OrdersPage>(page, `/api/backend/v1/orders?limit=25&status=${empty}`);
  expect(filtered.orders.length, `${empty} was supposed to be empty`).toBe(0);

  // The page is now empty and says so honestly.
  await expect(page.locator("tbody tr")).toHaveCount(0);
  await expect(page.getByText("No order matches this filter")).toBeVisible();
  await expect(page.getByText("this is an empty result and not a failed read")).toBeVisible();

  // And every count is still the tenant's. Derived from the rows, these would all be zero.
  await assertChipsCarry(page, all.counts);
  for (const [state, value] of populated) {
    await expect(
      chip(page, state),
      `${state} reads zero on a page filtered to ${empty}, so the counts are coming from the rows`,
    ).toContainText(count(value));
  }
});

test("refund counts survive a filter that narrows the page to one row", async ({ page }) => {
  await page.goto("/operations?tab=refunds");
  const all = await read<RefundsPage>(page, "/api/backend/v1/refunds?limit=25");

  const populated = Object.entries(all.counts).filter(([, value]) => value > 0);
  test.skip(populated.length < 2, "This tenant holds fewer than two refund states to tell apart.");

  await assertChipsCarry(page, all.counts);

  // Narrow to one state. The rows shrink to that state's own count; the chips must not.
  const [chosen, chosenCount] = populated[0];
  await chip(page, chosen).click();
  const filtered = await read<RefundsPage>(page, `/api/backend/v1/refunds?limit=25&state=${chosen}`);
  await expect(page.locator("tbody tr")).toHaveCount(filtered.refunds.length);
  expect(filtered.refunds.length).toBe(chosenCount);

  await assertChipsCarry(page, all.counts);
  // Specifically: the states that are not on screen at all still report their rows.
  for (const [state, value] of populated.filter(([name]) => name !== chosen)) {
    await expect(page.locator("tbody tr").filter({ hasText: state })).toHaveCount(0);
    await expect(
      chip(page, state),
      `${state} has no row on this page and its chip lost the count of ${value}`,
    ).toContainText(count(value));
  }

  // The three triage tiles are read from the same map and must not move either.
  for (const state of Object.keys(REFUND_MEANINGS)) {
    if (!(state in all.counts)) continue;
    await expect(refundTile(page, state)).toContainText(String(all.counts[state]));
  }
});

test("the overview's tiles are the tenant's counts, read from a one-row page", async ({ page }) => {
  // The overview asks for `limit=1` precisely because it wants the counts and not the rows.
  // If the counts were derived from what came back, every tile on the operator's first
  // screen would read one or zero.
  await page.goto("/");
  const orders = await read<OrdersPage>(page, "/api/backend/v1/orders?limit=1");
  const refunds = await read<RefundsPage>(page, "/api/backend/v1/refunds?limit=1");

  expect(orders.orders.length, "limit=1 returned more than one row").toBeLessThanOrEqual(1);

  const ordersPanel = page.locator("section").filter({ hasText: "Orders by state" });
  for (const [state, value] of Object.entries(orders.counts)) {
    await expect(
      ordersPanel.locator(`a[href="/operations?tab=orders&status=${state}"]`),
      `the overview's ${state} tile does not carry the tenant's count`,
    ).toContainText(count(value));
  }

  const refundsPanel = page.locator("section").filter({ hasText: "Refunds by state" });
  for (const [state, value] of Object.entries(refunds.counts)) {
    await expect(
      refundsPanel.locator(`a[href="/operations?tab=refunds&state=${state}"]`),
    ).toContainText(count(value));
  }

  // The total across the tiles must exceed what one row could account for, or this test is
  // not distinguishing the two implementations on this tenant and should say so.
  const total = Object.values(orders.counts).reduce((sum, value) => sum + value, 0);
  expect(
    total,
    "this tenant holds at most one order, so a page-derived count would look identical here",
  ).toBeGreaterThan(1);
});

test("the catalogue's category chips count the catalogue, not the loaded page", async ({ page }) => {
  // The same distinction one page over: `counts_by_category` is computed over the whole
  // catalogue whatever filter is in force, while `matched` counts the current selection.
  // The chip standing for "no category filter" has to use the first, or choosing a category
  // makes it report that category's size.
  await page.goto("/catalogue");
  const first = await read<CataloguePage>(
    page,
    "/api/backend/v1/catalogue/products?limit=50",
  );
  const total = Object.values(first.counts_by_category).reduce((sum, value) => sum + value, 0);
  expect(total, "this catalogue has no products to count").toBeGreaterThan(first.products.length);

  const all = page.getByRole("button", { name: /^all categories/ });
  await expect(all).toContainText(count(total));

  const [category] = Object.entries(first.counts_by_category)
    .filter(([, value]) => value > 0)
    .sort((a, b) => a[1] - b[1])[0];
  await page.getByRole("button", { name: new RegExp(`^${category}`) }).click();
  const filtered = await read<CataloguePage>(
    page,
    `/api/backend/v1/catalogue/products?limit=50&category=${encodeURIComponent(category)}`,
  );
  // `exact` because the pager below repeats the same figure in a longer sentence.
  await expect(
    page.getByText(`${count(filtered.matched)} matched by this filter`, { exact: true }),
  ).toBeVisible();
  // Still the whole catalogue, after the filter narrowed the rows.
  await expect(
    all,
    "the 'all categories' chip is counting the filter's selection rather than the catalogue",
  ).toContainText(count(total));
});
