/**
 * The inspector: one payment attempt, whole, and the only page in this console whose
 * subject arrives in the URL.
 *
 * What makes this page worth testing is not that it renders rows -- it is that the rows and
 * the findings above them have to be the same rows. The API states invariants like "every
 * provider mutation consumed exactly one Execution Grant", and the page's claim to be
 * evidence rather than a verdict rests entirely on a reader being able to recount that
 * against the grants and provider requests printed underneath. So this spec asserts the
 * findings *and* the collections they are computed over, from the same fetched document.
 *
 * The other reason is that an inspector has three genuinely different empty answers and
 * they must not collapse into one another:
 *
 *  - a collection the API returned empty (no webhook has named this attempt),
 *  - a field the API returned null (no provider payment id yet), which is an em dash,
 *  - and an attempt that does not exist, which is a failed read and a problem document.
 *
 * A page that rendered all three as blank space would be useless in exactly the situation
 * it exists for.
 */
import { expect, test, type Page } from "@playwright/test";

import {
  platformUnreachable,
  read,
  rupees,
  type InspectorDocument,
  type OrdersPage,
  type RefundsPage,
} from "./support";

/**
 * The panel carrying a given `<h2>`.
 *
 * By heading rather than by `hasText`, which is a substring match over the whole section:
 * filtering for "Refunds" that way selects the Findings panel, because one of the API's
 * findings is named `refunds_within_capture`. The heading is the only text on a panel that
 * identifies it.
 */
function panelHeaded(page: Page, heading: string) {
  return page
    .locator("section")
    .filter({ has: page.getByRole("heading", { name: heading, exact: true, level: 2 }) });
}

let unreachable: string | null = null;

test.beforeAll(async () => {
  unreachable = await platformUnreachable("the inspector suite");
});

test.beforeEach(() => {
  test.skip(unreachable !== null, unreachable ?? "");
});

/** The collections the document carries, and what the page says when one is empty. */
const COLLECTIONS = [
  { heading: "State history", key: "state_history", empty: "No recorded transition on this attempt yet." },
  { heading: "Execution grants", key: "grants", empty: "No grant was ever issued against this attempt." },
  { heading: "Outbox commands", key: "commands", empty: "No durable command was enqueued for this attempt." },
  {
    heading: "Provider requests",
    key: "provider_requests",
    empty: "No HTTP call to the provider is recorded against this attempt.",
  },
  {
    heading: "Webhook deliveries",
    key: "webhook_deliveries",
    empty: "No webhook naming this attempt's provider ids has been received.",
  },
  {
    heading: "Reconciliation runs",
    key: "reconciliation_runs",
    empty: "Reconciliation has not run against this attempt.",
  },
  { heading: "Refunds", key: "refunds", empty: "No refund has been requested against this attempt." },
] as const;

test("/inspector with no attempt offers the attempts behind recent orders", async ({ page }) => {
  await page.goto("/inspector");
  await expect(page.getByRole("heading", { name: "Inspector", level: 1 })).toBeVisible();

  // Not a bookmark list and not a cache: this is GET /v1/orders performed on the page load.
  const orders = await read<OrdersPage>(page, "/api/backend/v1/orders?limit=10");
  await expect(page.getByText("Attempts behind recent orders · GET /v1/orders")).toBeVisible();

  if (orders.orders.length === 0) {
    await expect(page.getByText("This tenant has no confirmed order yet")).toBeVisible();
  } else {
    // Asserted on the link rather than on the text, because the point of this list is that
    // each entry opens that attempt: an id printed beside a link to a different one would
    // read identically and be the whole bug.
    for (const order of orders.orders) {
      const entry = page.locator(
        `a[href="/inspector?attempt=${encodeURIComponent(order.payment_attempt_id)}"]`,
      );
      await expect(entry).toHaveCount(1);
      await expect(entry).toContainText(order.payment_attempt_id);
      await expect(entry).toContainText(order.state);
      await expect(entry).toContainText(rupees(order.amount.minor, order.amount.currency));
    }
  }
  await expect(page.getByText("READ FAILED")).toHaveCount(0);
});

test("/inspector?attempt= renders the document the API assembled, findings and rows alike", async ({
  page,
}) => {
  const orders = await read<OrdersPage>(page, "/api/backend/v1/orders?limit=1");
  test.skip(
    orders.orders.length === 0,
    "This tenant holds no confirmed order, so there is no payment attempt to inspect.",
  );
  const attemptId = orders.orders[0].payment_attempt_id;

  await page.goto(`/inspector?attempt=${encodeURIComponent(attemptId)}`);
  const document = await read<InspectorDocument>(
    page,
    `/api/backend/v1/inspector/payment-attempts/${encodeURIComponent(attemptId)}`,
  );

  // ------------------------------------------------------------------- the headline
  const attempt = panelHeaded(page, "Attempt");
  await expect(attempt).toContainText(document.state);
  await expect(attempt).toContainText(rupees(document.amount_minor, document.currency));
  await expect(attempt).toContainText(`checkout version ${document.checkout_version}`);
  await expect(attempt).toContainText(document.receipt);
  await expect(attempt).toContainText(document.payment_attempt_id);

  // A provider identifier the API has not got is an em dash, not a blank and not a zero:
  // "the provider has told us nothing" is a fact, and it is the fact this page exists for.
  for (const id of [document.provider_order_id, document.provider_payment_id]) {
    await expect(attempt).toContainText(id ?? "—");
  }

  // --------------------------------------------------------------------- the findings
  const findings = panelHeaded(page, "Findings");
  expect(document.findings.length, "the API stated no invariant over this attempt").toBeGreaterThan(0);
  for (const finding of document.findings) {
    const row = findings.locator("li").filter({ hasText: finding.name });
    await expect(row).toHaveCount(1);
    // The verdict is the API's `ok`, rendered as a word. A finding that failed must not be
    // distinguishable only by colour.
    await expect(row).toContainText(finding.ok ? "holds" : "violated");
    await expect(row).toContainText(finding.detail);
  }

  // ------------------------------------------------------------------ the rows beneath
  // The findings are only evidence if what they were computed over is on the page too.
  for (const collection of COLLECTIONS) {
    const rows = document[collection.key] as Array<Record<string, unknown>>;
    const panel = panelHeaded(page, collection.heading);
    if (rows.length === 0) {
      await expect(panel).toContainText(collection.empty);
      await expect(panel.locator("tbody tr")).toHaveCount(0);
    } else {
      await expect(panel.locator("tbody tr")).toHaveCount(rows.length);
      await expect(panel).not.toContainText(collection.empty);
    }
  }

  // The order row, which is written only from verified capture evidence.
  const order = panelHeaded(page, "Order");
  if (document.order === null) {
    await expect(order).toContainText("No order row exists for this attempt");
    await expect(order).toContainText("the platform has written no sale");
  } else {
    await expect(order.locator("tbody tr")).toHaveCount(1);
    await expect(order).toContainText(String(document.order.order_id));
  }

  await expect(page.getByText("READ FAILED")).toHaveCount(0);
});

test("an attempt the API does not have is a problem document, not an empty inspector", async ({
  page,
}) => {
  // Well-formed and certainly absent: a v7 UUID with a zeroed random suffix.
  const missing = "01a06fff-0000-7000-8000-000000000000";
  const response = await page.request.get(
    `/api/backend/v1/inspector/payment-attempts/${missing}`,
  );
  expect(response.ok(), "this id was supposed to be absent and the API returned it").toBeFalsy();

  await page.goto(`/inspector?attempt=${missing}`);
  const panel = page.locator('[role="alert"]').filter({ hasText: "READ FAILED" });
  await expect(panel).toBeVisible();
  await expect(panel).toContainText("the inspector document");
  // The status the API actually answered with, so an operator can tell "no such attempt"
  // from "the platform is down" -- which is the whole reason the problem document is
  // rendered whole rather than softened into one reassuring sentence.
  await expect(panel).toContainText(String(response.status()));
  await expect(panel).toContainText(
    "Nothing is shown in place of these figures. This console has no fixtures to fall back to.",
  );

  // And none of the document's panels are drawn empty over the failure.
  for (const collection of COLLECTIONS) {
    await expect(page.getByText(collection.empty)).toHaveCount(0);
  }
});

test("a refund row's attempt link opens that attempt in the inspector", async ({ page }) => {
  // The two pages have to agree about what an attempt id is. This is the only place in the
  // console where one screen hands an identifier to another.
  const refunds = await read<RefundsPage>(page, "/api/backend/v1/refunds?limit=25");
  test.skip(
    refunds.refunds.length === 0,
    "This tenant holds no refund, so no row links into the inspector.",
  );

  await page.goto("/operations?tab=refunds");
  // Waited for rather than counted: `count()` does not retry, so asking the instant the
  // navigation resolves is asking before the list has been read.
  const link = page.locator('a[href^="/inspector?attempt="]').first();
  await expect(link).toBeVisible();

  const href = await link.getAttribute("href");
  const attemptId = decodeURIComponent(new URL(href!, "http://localhost").searchParams.get("attempt")!);
  await link.click();

  await expect(page).toHaveURL(new RegExp(`/inspector\\?attempt=`));
  const document = await read<InspectorDocument>(
    page,
    `/api/backend/v1/inspector/payment-attempts/${encodeURIComponent(attemptId)}`,
  );
  // The attempt the inspector opened is the attempt the refund row named. Asserted inside
  // the Attempt panel, because the id is also sitting in the search box above it and
  // "the id is somewhere on the page" is a weaker claim than the one worth making.
  await expect(panelHeaded(page, "Attempt")).toContainText(document.payment_attempt_id);
  await expect(page.getByText("READ FAILED")).toHaveCount(0);
});
