/**
 * The merchant console, end to end, against the running platform.
 *
 * Every expectation in this file is fetched, not pasted. The specs call the same endpoints
 * the page calls -- through the console's own `/api/backend` proxy, so they carry the same
 * operator session the browser does -- and assert that what is on the screen is what the
 * API answered. A constant copied out of yesterday's database would make this suite pass
 * on a console rendering a fixture, which is precisely the failure the application is
 * built to make impossible.
 *
 * Three consequences of that, worth stating because they look like weaknesses and are not:
 *
 *  - An empty collection is asserted as an empty collection. If the tenant holds no
 *    orders, the correct screen is the one saying the API answered with counts and this
 *    is an empty result rather than a failed read, and that is what gets asserted. A spec
 *    that demanded rows would be a spec that only passes on a seeded machine.
 *  - Pagination is asserted against `next_cursor`. Where the API offers a next page, the
 *    button is pressed and the second page is compared against the API's own second page;
 *    where it does not, the button must be disabled. The catalogue's 247 products make
 *    the first branch real on any machine.
 *  - Money is compared as a string rendered here from the server's integer paise, by a
 *    different route than the application's formatter takes: `Intl.NumberFormat` over
 *    minor/100 rather than trunc-and-pad. Two implementations agreeing on ₹1,23,456.78 is
 *    a check; one implementation compared against itself is not.
 */
import { expect, test, type Page } from "@playwright/test";

const API = process.env.COMMERCE_API_URL ?? "http://127.0.0.1:8000";

/** Why the suite is skipping, or null when the platform answered. */
let unreachable: string | null = null;

interface Money {
  minor: number;
  currency: string;
  display: string;
}

interface Session {
  merchant_id: string;
  tenant_id: string;
  actor_type: string;
  capabilities: string[];
}

interface Counted {
  counts: Record<string, number>;
  next_cursor: string | null;
  scope: string;
}

interface OrderSummary {
  order_id: string;
  checkout_id: string;
  state: string;
  amount: Money;
  payment_attempt_id: string;
}

interface OrdersPage extends Counted {
  orders: OrderSummary[];
}

interface RefundListItem {
  refund_id: string;
  state: string;
  row_status: string;
  amount: Money;
  captured_minor: number | null;
  currency: string;
}

interface RefundsPage extends Counted {
  refunds: RefundListItem[];
}

interface Product {
  sku: string;
  display_name: string;
  name_en: string;
  category: string;
  unit_price_minor: number;
  unit_price: Money;
  is_listed: boolean;
}

interface CataloguePage {
  products: Product[];
  next_cursor: string | null;
  matched: number;
  counts_by_category: Record<string, number>;
  revision: number;
}

interface SearchResponse {
  normalized_query: string;
  hits: Product[];
  freshness: { source: string; catalogue_revision: number };
}

interface SafeMode {
  mode: string;
  scope: string;
  safe_mode: boolean;
  permitted: Record<string, boolean>;
}

interface OutboxPage {
  counts: Record<string, number>;
}

interface RetainedRevenue {
  checkout_id: string;
  currency: string;
  stale_version: number | null;
  stale_approved_minor: number | null;
  corrected_version: number | null;
  corrected_total_minor: number | null;
  captured_minor: number | null;
  captured_from: string | null;
  difference_minor: number | null;
  direction: string;
  refunded_minor: number;
  net_retained_minor: number | null;
  explanation: string;
}

interface AuditVerification {
  intact: boolean;
  empty: boolean;
  length: number;
  events_verified: number;
  head_seq: number | null;
  code: string;
}

interface ProofChain {
  payment_attempt_id: string | null;
  verdict: {
    tier: string;
    ok: boolean;
    checks: Array<{ name: string; ok: boolean; applicable: boolean }>;
  };
}

/**
 * Render integer paise the way the column should read, by a route independent of the
 * application's own formatter. This is a test asserting an expectation, not a browser
 * deriving a figure: the number itself is the server's, unmodified.
 */
function rupees(minor: number, currency = "INR"): string {
  const symbol = currency === "INR" ? "₹" : `${currency} `;
  const body = new Intl.NumberFormat("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(Math.abs(minor) / 100);
  return `${minor < 0 ? "−" : ""}${symbol}${body}`;
}

/** Absent is an em dash and zero is an amount. Never the same string. */
function orDash(minor: number | null, currency = "INR"): string {
  return minor === null ? "—" : rupees(minor, currency);
}

/** A count as the console groups it. */
function count(value: number): string {
  return value.toLocaleString("en-IN");
}

/** Read through the console's own proxy, on the page's operator session. */
async function read<T>(page: Page, path: string): Promise<T> {
  const response = await page.request.get(path);
  expect(response.ok(), `${path} answered ${response.status()}`).toBeTruthy();
  return (await response.json()) as T;
}

test.beforeAll(async () => {
  try {
    const response = await fetch(`${API}/v1/config`);
    if (!response.ok) {
      unreachable = `The Commerce API at ${API} answered ${response.status} to GET /v1/config. Start it with \`make demo\` and re-run.`;
    }
  } catch (cause) {
    unreachable =
      `The Commerce API at ${API} is not reachable (${cause instanceof Error ? cause.message : String(cause)}). ` +
      "These specs read live data and assert nothing without it — start the API with `make demo` and re-run.";
  }
  // The list reporter prints a skipped test's name but not its reason, and a run of eight
  // dashes with no explanation reads like a suite that has been quietly disabled.
  if (unreachable) console.warn(`\nSkipping the merchant console end-to-end suite.\n${unreachable}\n`);
});

test.beforeEach(() => {
  test.skip(unreachable !== null, unreachable ?? "");
});

// ------------------------------------------------------------------------- overview

test("the overview renders the counts the API answered with, and no others", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Overview", level: 1 })).toBeVisible();

  const session = await read<Session>(page, "/api/backend/session");
  expect(session.actor_type).toBe("OPERATOR");

  // ------------------------------------------------------------------- safe mode
  const safeMode = await read<SafeMode>(page, "/api/backend/v1/ops/safe-mode");
  const operating = page.locator("section").filter({ hasText: "Operating mode" });
  await expect(operating).toContainText(safeMode.mode);
  await expect(operating).toContainText(`scope ${safeMode.scope}`);
  for (const capability of ["DELEGATED_DEBIT", "REFUND_EXECUTE", "RECONCILIATION"] as const) {
    // The kernel was asked; the console must render its answer and not an assumption.
    expect(safeMode.permitted).toHaveProperty(capability);
  }
  await expect(operating).toContainText(safeMode.permitted.RECONCILIATION ? "permitted" : "blocked");

  // ---------------------------------------------------------------------- outbox
  const outbox = await read<OutboxPage>(page, "/api/backend/v1/ops/outbox?limit=1");
  const queue = page.locator("section").filter({ hasText: "Durable outbox" });
  for (const [status, value] of Object.entries(outbox.counts)) {
    const tile = queue.locator(`a[href="/operations?tab=outbox&status=${status}"]`);
    await expect(tile).toContainText(status);
    await expect(tile).toContainText(count(value));
  }

  // ------------------------------------------------------------ orders and refunds
  const orders = await read<OrdersPage>(page, "/api/backend/v1/orders?limit=1");
  const ordersPanel = page.locator("section").filter({ hasText: "Orders by state" });
  await expect(ordersPanel).toContainText(`scope ${orders.scope}`);
  for (const [state, value] of Object.entries(orders.counts)) {
    const tile = ordersPanel.locator(`a[href="/operations?tab=orders&status=${state}"]`);
    await expect(tile).toContainText(state);
    // Zero is rendered as zero. A state the API counted must never go missing because it
    // happens to be empty -- "no CANCELLED orders" and "CANCELLED not shown" differ.
    await expect(tile).toContainText(count(value));
  }

  const refunds = await read<RefundsPage>(page, "/api/backend/v1/refunds?limit=1");
  const refundsPanel = page.locator("section").filter({ hasText: "Refunds by state" });
  for (const [state, value] of Object.entries(refunds.counts)) {
    const tile = refundsPanel.locator(`a[href="/operations?tab=refunds&state=${state}"]`);
    await expect(tile).toContainText(state);
    await expect(tile).toContainText(count(value));
  }

  // -------------------------------------------------------------- retained revenue
  const evidence = await page.request.get(
    `/api/backend/v1/merchants/${session.merchant_id}/evidence/retained-revenue`,
  );
  const retainedPanel = page.locator("section").filter({ hasText: "Retained revenue" });
  if (evidence.status() === 404) {
    // Not a failure: the API says this merchant has no refused approval yet, and the
    // console must say so rather than render a zero.
    await expect(retainedPanel).toContainText("NOTHING TO ACCOUNT FOR");
  } else {
    expect(evidence.ok()).toBeTruthy();
    const retained = (await evidence.json()) as RetainedRevenue;
    await expect(retainedPanel).toContainText(
      orDash(retained.stale_approved_minor, retained.currency),
    );
    await expect(retainedPanel).toContainText(
      orDash(retained.corrected_total_minor, retained.currency),
    );
    await expect(retainedPanel).toContainText(orDash(retained.captured_minor, retained.currency));
    await expect(retainedPanel).toContainText(`direction ${retained.direction}`);
    await expect(retainedPanel).toContainText(retained.explanation);
  }

  // Nothing on this screen failed to read. READ FAILED is the chip a ProblemPanel wears,
  // and it is the only place in the console that word appears.
  await expect(page.getByText("READ FAILED")).toHaveCount(0);
});

// ----------------------------------------------------------------------- operations

test("/operations lists the API's orders, and pages by cursor rather than by index", async ({
  page,
}) => {
  await page.goto("/operations?tab=orders");
  const panel = page.locator("section").filter({ hasText: "GET /v1/orders?status=" });

  const first = await read<OrdersPage>(page, "/api/backend/v1/orders?limit=25");
  const rows = panel.locator("tbody tr");

  if (first.orders.length === 0) {
    await expect(panel).toContainText("No order matches this filter");
    await expect(panel).toContainText("this is an empty result and not a failed read");
    await expect(rows).toHaveCount(0);
  } else {
    await expect(rows).toHaveCount(first.orders.length);
    for (const order of first.orders) {
      // The id is shortened for the eye and complete in the title attribute.
      await expect(panel.locator(`[title="${order.order_id}"]`)).toHaveCount(1);
      await expect(panel).toContainText(order.state);
      await expect(panel).toContainText(order.amount.display);
    }
  }
  await expect(panel).toContainText(`${first.orders.length} orders on this page`);
  await expect(panel).toContainText(`scope ${first.scope}`);

  const next = panel.getByRole("button", { name: "Next →" });
  const previous = panel.getByRole("button", { name: "← Previous" });
  await expect(previous).toBeDisabled();

  if (first.next_cursor === null) {
    await expect(next).toBeDisabled();
    if (first.orders.length > 0) await expect(panel).toContainText("last page");
  } else {
    const second = await read<OrdersPage>(
      page,
      `/api/backend/v1/orders?limit=25&cursor=${encodeURIComponent(first.next_cursor)}`,
    );
    await next.click();
    await expect(rows).toHaveCount(second.orders.length);
    for (const order of second.orders) {
      await expect(panel.locator(`[title="${order.order_id}"]`)).toHaveCount(1);
    }
    // A keyset cursor never repeats a row it has already handed out.
    const firstIds = new Set(first.orders.map((order) => order.order_id));
    for (const order of second.orders) expect(firstIds.has(order.order_id)).toBe(false);

    await expect(previous).toBeEnabled();
    await previous.click();
    await expect(rows).toHaveCount(first.orders.length);
    await expect(previous).toBeDisabled();
  }
});

test("/operations lists refunds with the three states kept apart", async ({ page }) => {
  await page.goto("/operations?tab=refunds");
  const panel = page.locator("section").filter({ hasText: "GET /v1/refunds?state=" });

  const first = await read<RefundsPage>(page, "/api/backend/v1/refunds?limit=25");
  const rows = panel.locator("tbody tr");

  // The triage tiles are rendered from `counts` whatever the page holds, so they are the
  // one part of this screen that is asserted even on an empty tenant.
  for (const state of ["REFUND_PENDING", "REFUND_UNKNOWN", "REFUND_FAILED"] as const) {
    expect(first.counts).toHaveProperty(state);
    await expect(panel).toContainText(state);
  }
  await expect(panel).toContainText("in flight, do not retry");
  await expect(panel).toContainText("reconciliation owns this row, not an operator");
  await expect(panel).toContainText("the provider said no — this refund did not happen");

  if (first.refunds.length === 0) {
    await expect(panel).toContainText("No refund matches this filter");
    await expect(rows).toHaveCount(0);
  } else {
    await expect(rows).toHaveCount(first.refunds.length);
    for (const refund of first.refunds) {
      const row = panel.locator("tbody tr").filter({ has: page.locator(`[title="${refund.refund_id}"]`) });
      await expect(row).toContainText(refund.state);
      await expect(row).toContainText(`row_status ${refund.row_status}`);
      await expect(row).toContainText(refund.amount.display);
      // `captured_minor` is null when the refund has no order row, and that is an em dash
      // with a warning beside it, not a zero.
      await expect(row).toContainText(orDash(refund.captured_minor, refund.currency));
    }
  }
  await expect(panel).toContainText(`${first.refunds.length} refunds on this page`);

  const next = panel.getByRole("button", { name: "Next →" });
  if (first.next_cursor === null) {
    await expect(next).toBeDisabled();
  } else {
    const second = await read<RefundsPage>(
      page,
      `/api/backend/v1/refunds?limit=25&cursor=${encodeURIComponent(first.next_cursor)}`,
    );
    await next.click();
    await expect(rows).toHaveCount(second.refunds.length);
  }
});

test("a filter chip re-asks the API rather than narrowing the rows in the browser", async ({
  page,
}) => {
  await page.goto("/operations?tab=orders");
  const panel = page.locator("section").filter({ hasText: "GET /v1/orders?status=" });
  await expect(panel.getByRole("button", { name: /^all/ })).toHaveAttribute("aria-pressed", "true");

  const all = await read<OrdersPage>(page, "/api/backend/v1/orders?limit=25");
  const state = "CONFIRMED";
  const filtered = await read<OrdersPage>(page, `/api/backend/v1/orders?limit=25&status=${state}`);

  const request = page.waitForRequest(
    (candidate) =>
      candidate.url().includes("/api/backend/v1/orders") && candidate.url().includes(`status=${state}`),
  );
  await panel.getByRole("button", { name: new RegExp(`^${state}`) }).click();
  await request;

  await expect(panel.locator("tbody tr")).toHaveCount(filtered.orders.length);
  await expect(panel).toContainText(`${filtered.orders.length} orders on this page`);
  // The counts are computed across the scope, so they do not move when a filter narrows
  // the rows: a queue depth read off a filtered page is the wrong number.
  for (const [name, value] of Object.entries(all.counts)) {
    await expect(panel.getByRole("button", { name: new RegExp(`^${name}`) })).toContainText(
      count(value),
    );
  }
});

// ------------------------------------------------------------------------ catalogue

test("/catalogue pages the whole merchant catalogue by cursor", async ({ page }) => {
  await page.goto("/catalogue");
  await expect(page.getByRole("heading", { name: "Catalogue", level: 1 })).toBeVisible();

  const first = await read<CataloguePage>(page, "/api/backend/v1/catalogue/products?limit=50");
  await expect(page.getByText(`revision ${first.revision}`, { exact: true })).toBeVisible();
  await expect(
    page.getByText(`${count(first.matched)} matched by this filter`, { exact: true }),
  ).toBeVisible();

  const panel = page.locator("section").filter({ hasText: "GET /v1/catalogue/products?category=" });
  const rows = panel.locator("tbody tr");
  await expect(rows).toHaveCount(first.products.length);

  for (const product of first.products.slice(0, 5)) {
    const row = rows.filter({ hasText: product.sku });
    await expect(row).toContainText(product.display_name);
    // The row states the server's own display string and the integer it came from, and
    // never a figure this browser converted between them.
    await expect(row).toContainText(`₹${product.unit_price.display}`);
    await expect(row).toContainText(`${product.unit_price_minor} paise`);
    await expect(row).toContainText(product.is_listed ? "listed" : "delisted");
  }

  // The "all categories" chip is counted from counts_by_category, which the API computes
  // over the whole catalogue -- not from `matched`, which counts this filter's selection.
  const total = Object.values(first.counts_by_category).reduce((sum, value) => sum + value, 0);
  await expect(page.getByRole("button", { name: /^all categories/ })).toContainText(count(total));

  expect(first.next_cursor, "a 247-product catalogue must page at limit=50").not.toBeNull();
  const second = await read<CataloguePage>(
    page,
    `/api/backend/v1/catalogue/products?limit=50&cursor=${encodeURIComponent(first.next_cursor!)}`,
  );
  await panel.getByRole("button", { name: "Next →" }).click();
  await expect(rows).toHaveCount(second.products.length);
  await expect(rows.first()).toContainText(second.products[0].sku);

  const firstSkus = new Set(first.products.map((product) => product.sku));
  for (const product of second.products) expect(firstSkus.has(product.sku)).toBe(false);

  await panel.getByRole("button", { name: "← Previous" }).click();
  await expect(rows.first()).toContainText(first.products[0].sku);
});

test("/catalogue filters by category through the API", async ({ page }) => {
  await page.goto("/catalogue");
  const first = await read<CataloguePage>(page, "/api/backend/v1/catalogue/products?limit=50");

  // Pick a category the API actually counted, rather than one this spec assumes exists.
  const [category, expected] = Object.entries(first.counts_by_category)
    .filter(([, value]) => value > 0)
    .sort((a, b) => a[1] - b[1])[0];
  const filtered = await read<CataloguePage>(
    page,
    `/api/backend/v1/catalogue/products?limit=50&category=${encodeURIComponent(category)}`,
  );
  expect(filtered.matched).toBe(expected);

  await page.getByRole("button", { name: new RegExp(`^${category}`) }).click();
  const panel = page.locator("section").filter({ hasText: "GET /v1/catalogue/products?category=" });
  await expect(panel.locator("tbody tr")).toHaveCount(filtered.products.length);
  await expect(
    page.getByText(`${count(filtered.matched)} matched by this filter`, { exact: true }),
  ).toBeVisible();
  for (const product of filtered.products.slice(0, 5)) {
    await expect(panel.locator("tbody tr").filter({ hasText: product.sku })).toContainText(category);
  }
});

test("/catalogue searches the merchant's index rather than the loaded page", async ({ page }) => {
  await page.goto("/catalogue");
  const first = await read<CataloguePage>(page, "/api/backend/v1/catalogue/products?limit=50");

  // A term taken from live data, so the query is meaningful on any seeded machine.
  const term = first.products[0].name_en.split(" ")[0];
  const expected = await read<SearchResponse>(
    page,
    `/api/backend/v1/catalogue/search?q=${encodeURIComponent(term)}&limit=50`,
  );

  await page.getByRole("searchbox").fill(term);
  await page.getByRole("button", { name: "Search" }).click();

  const panel = page
    .locator("section")
    .filter({ hasText: "GET /v1/catalogue/search — ranked, with the terms that matched" });
  await expect(panel).toBeVisible();
  const rows = panel.locator("tbody tr");

  if (expected.hits.length === 0) {
    await expect(panel).toContainText("The index returned no hit for that query");
  } else {
    await expect(rows).toHaveCount(expected.hits.length);
    await expect(panel).toContainText(`${expected.hits.length} hits for`);
    await expect(panel).toContainText(expected.normalized_query);
    await expect(panel).toContainText(expected.freshness.source);
    await expect(panel).toContainText(`revision ${expected.freshness.catalogue_revision}`);
    for (const hit of expected.hits.slice(0, 5)) {
      await expect(rows.filter({ hasText: hit.sku })).toContainText(hit.display_name);
    }
    // The ranked index can return more than the fifty-row page held, and it is a
    // different question from browsing, which the page says out loud.
    await expect(page.getByText(/Showing ranked hits from GET \/v1\/catalogue\/search/)).toBeVisible();
  }
});

// ------------------------------------------------------------------------- evidence

test("/evidence shows the retained-revenue arithmetic and verifies its own chains", async ({
  page,
}) => {
  await page.goto("/evidence");
  await expect(page.getByRole("heading", { name: "Evidence", level: 1 })).toBeVisible();

  const session = await read<Session>(page, "/api/backend/session");
  const response = await page.request.get(
    `/api/backend/v1/merchants/${session.merchant_id}/evidence/retained-revenue`,
  );
  if (response.status() === 404) {
    // The endpoint answers 404 rather than a zero, and the page must carry that distinction
    // rather than paint an arithmetic over nothing.
    await expect(page.getByText("NOTHING TO ACCOUNT FOR")).toBeVisible();
    return;
  }
  expect(response.ok()).toBeTruthy();
  const retained = (await response.json()) as RetainedRevenue;

  const arithmetic = page.locator("section").filter({ hasText: "The arithmetic" });
  await expect(arithmetic).toContainText(
    `Version ${retained.stale_version ?? "N"} — approved total`,
  );
  await expect(arithmetic).toContainText(orDash(retained.stale_approved_minor, retained.currency));
  await expect(arithmetic).toContainText(orDash(retained.corrected_total_minor, retained.currency));
  await expect(arithmetic).toContainText(orDash(retained.captured_minor, retained.currency));
  await expect(arithmetic).toContainText(orDash(retained.difference_minor, retained.currency));
  await expect(arithmetic).toContainText(orDash(retained.net_retained_minor, retained.currency));

  // The one figure the browser derives: corrected minus approved, signed, and not the
  // server's `difference_minor`, which is captured minus approved.
  const movement =
    retained.stale_approved_minor !== null && retained.corrected_total_minor !== null
      ? retained.corrected_total_minor - retained.stale_approved_minor
      : null;
  const rendered =
    movement === null
      ? "—"
      : movement === 0
        ? rupees(0, retained.currency)
        : `${movement > 0 ? "+" : "−"}${rupees(Math.abs(movement), retained.currency)}`;
  const requote = page.getByText("Re-quote movement · subtracted in this browser").locator("..");
  await expect(requote).toContainText(rendered);

  // Provenance, and the API's own account of what happened.
  await expect(page.getByText(retained.explanation)).toBeVisible();
  await expect(page.locator("section").filter({ hasText: "Provenance" })).toContainText(
    retained.direction,
  );

  // ------------------------------------------------------------- chain verification
  const checkoutChain = await read<AuditVerification>(
    page,
    `/api/backend/v1/audit/streams/checkout/${encodeURIComponent(retained.checkout_id)}/verify`,
  );
  const chains = page.locator("section").filter({ hasText: "Chain verification" });
  await expect(chains).toContainText(checkoutChain.intact ? "INTACT" : "BROKEN");
  await expect(chains).toContainText(checkoutChain.code);
  await expect(chains).toContainText(String(checkoutChain.length));
  await expect(chains).toContainText(String(checkoutChain.events_verified));

  const proof = await read<ProofChain>(
    page,
    `/api/backend/v1/checkouts/${encodeURIComponent(retained.checkout_id)}/proof`,
  );
  await expect(chains).toContainText(proof.verdict.ok ? "HOLDS" : "BROKEN");
  await expect(chains).toContainText(`tier ${proof.verdict.tier}`);
  for (const check of proof.verdict.checks) {
    await expect(chains).toContainText(check.name);
  }
  if (proof.payment_attempt_id) {
    const attemptChain = await read<AuditVerification>(
      page,
      `/api/backend/v1/audit/streams/payment_attempt/${encodeURIComponent(proof.payment_attempt_id)}/verify`,
    );
    await expect(chains).toContainText(attemptChain.code);
  } else {
    await expect(chains).toContainText("No payment attempt on this checkout yet.");
  }

  await expect(page.getByText("READ FAILED")).toHaveCount(0);
});
