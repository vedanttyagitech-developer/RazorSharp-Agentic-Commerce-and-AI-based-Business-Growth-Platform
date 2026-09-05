/**
 * The walk both specs share: search, add, open a checkout, approve version 1.
 *
 * Kept out of the spec files so that neither of them repeats it, and out of the API
 * helper so that everything here goes through the browser exactly as a buyer would. The
 * one thing it does not do through the browser is read amounts: those come back from
 * `/api/backend/...` using the page's own cookie, so an expectation is never a number the
 * test made up and never a number scraped out of the very element it is checking.
 *
 * Not a test file: Playwright collects `*.spec.ts`, so this is imported, never run.
 */
import { expect, type Page } from "@playwright/test";

export const MILK_SKU = "AMUL-DAIRY-001";
export const MILK_NAME = "Amul Taaza Toned Milk 500 ml";

/**
 * How long one of these steps may take before it is a failure rather than a slow answer.
 *
 * Each is a browser round trip to a Next dev server that may still be compiling the route
 * plus an API call plus a database write, and the two viewport projects run one after the
 * other against a single machine. The default expect timeout is right for asserting on
 * something already rendered and too tight for waiting on all of that.
 */
const SERVER_ROUND_TRIP = 30_000;


/** The shape of `GET /v1/checkouts/{id}`, as far as these specs read it. */
export interface CheckoutRead {
  checkout_id: string;
  state: string;
  current_version: number;
  versions: Array<{ version: number; state: string; amount_minor: number; currency: string }>;
  approval_card: { version: number; amount_minor: number; currency: string } | null;
}

/**
 * The header search box, whichever of the two is on screen.
 *
 * The header renders a wide one and a compact one and hides the wrong one with a media
 * query, so both are in the DOM at every viewport and only one is visible. Selecting the
 * visible one is what lets the same spec run at 390 and at desktop width.
 */
export function visibleSearchBox(page: Page) {
  return page.locator('input[aria-label="Search for products"]:visible').first();
}

/** Read a checkout back through the app's own proxy, with the browser's own session. */
export async function readCheckout(page: Page, checkoutId: string): Promise<CheckoutRead> {
  const response = await page.request.get(
    `/api/backend/v1/checkouts/${encodeURIComponent(checkoutId)}`,
  );
  expect(response.ok(), `the app's proxy could not read checkout ${checkoutId}`).toBe(true);
  return (await response.json()) as CheckoutRead;
}

/** The checkout id in the address bar, or a failure that says the journey never got there. */
function checkoutIdFrom(url: string): string {
  const match = /\/checkout\/([^/?#]+)/.exec(url);
  if (!match) throw new Error(`not on a checkout route: ${url}`);
  return decodeURIComponent(match[1]);
}

/* -------------------------------------------- giving the stock back afterwards */

/**
 * Every checkout this file has opened and not yet ended.
 *
 * Opening a checkout takes a hold on the merchant's stock for fifteen minutes, and a
 * checkout left sitting in `APPROVAL_REQUIRED` keeps it. Running this suite a few times
 * inside that window therefore exhausted the live holds available on one SKU, and the
 * next `POST /v1/baskets/{id}/checkout` came back
 * `reservation_refused: no hold could be taken for version 1: CONCURRENT_OPERATION`.
 * From the browser that reads as a checkout that simply never opened — the failure looked
 * like a hung navigation and was really a suite competing with its own past runs.
 *
 * So the suite gives back what it takes. Nothing about this is a workaround: a reservation
 * is scarce by design, and a test that walks away holding one is a test that has changed
 * the merchant it was measuring.
 */
const openedCheckouts: string[] = [];

/**
 * End every checkout this test opened, so its stock hold is released now rather than in
 * fifteen minutes.
 *
 * Written to be called from `afterEach` and never to fail a test. A checkout the kernel
 * declines to cancel — one whose payment is already in flight — is exactly the refusal
 * this platform exists to make, and it is not this cleanup's business to argue with it.
 * The proxy refuses a write that cannot prove it came from a page of this app, so the
 * `Origin` a browser would have sent is supplied explicitly.
 */
export async function releaseCheckouts(page: Page): Promise<void> {
  const origin = appOrigin(page);
  while (openedCheckouts.length > 0) {
    const id = openedCheckouts.pop();
    if (!id) continue;
    try {
      await page.request.post(`/api/backend/v1/checkouts/${encodeURIComponent(id)}/cancel`, {
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": `e2e-cancel-${id}`,
          Origin: origin,
        },
        data: { reason: "e2e_cleanup" },
      });
    } catch {
      // Best effort. The hold expires on its own; this only makes it sooner.
    }
  }
}

function appOrigin(page: Page): string {
  try {
    const origin = new URL(page.url()).origin;
    return origin === "null" ? "http://localhost:3000" : origin;
  } catch {
    return "http://localhost:3000";
  }
}

/**
 * Search for `doodh`, add the milk, and land on a checkout showing version 1's card.
 *
 * Every step waits on something only a server could have produced -- the card the search
 * index returned, the cart count the provider summed from a basket the API sent, the
 * priced total on the basket page, the approval card the merchant froze. None of those
 * appear before a response has come back, which is what makes this walk a test of the
 * platform rather than of the browser's optimism. Where the screen deliberately runs
 * ahead of the server, that is called out at the line rather than trusted.
 */
export async function openCheckoutForMilk(page: Page): Promise<string> {
  await page.goto("/");

  await visibleSearchBox(page).fill("doodh");
  await visibleSearchBox(page).press("Enter");
  // `toHaveURL` rather than `waitForURL`: every navigation from here is a client-side
  // push, and waiting on a `load` event that a soft navigation never fires again turns a
  // slow response into a bare "timed out waiting for navigation" with nothing in it.
  await expect(page).toHaveURL(/\/search\?q=doodh/);

  const add = page.getByRole("button", { name: `Add ${MILK_NAME} to basket` });
  await expect(add).toBeVisible({ timeout: SERVER_ROUND_TRIP });
  await add.click();

  // The stepper is NOT the signal. Quantity is the one number this storefront lets the
  // browser have an opinion about — `useBasket` publishes the wanted count optimistically
  // and dims the money until the server answers — so "1 in basket" appears before
  // `POST /v1/baskets` has even been sent. Navigating on it aborted the write in flight
  // and landed on an empty basket, intermittently and for no visible reason.
  //
  // The cart pill's count is the honest one: `itemCount` is only ever published by the
  // provider's `refresh`, which sums the quantities on a basket the server returned. So
  // this line waits for a round trip, and the one above only for the intent.
  await expect(page.getByLabel("1 in basket").first()).toBeVisible({ timeout: SERVER_ROUND_TRIP });
  await expect(page.getByRole("link", { name: "My cart, 1 item" })).toBeVisible({
    timeout: SERVER_ROUND_TRIP,
  });

  await page.goto("/basket");
  // The basket read has to have landed before the button means anything: with no basket
  // this page renders "Your basket is empty", and asserting on a control that is simply
  // not there reports a missing element rather than the read that did not arrive.
  await expect(page.getByRole("heading", { name: "Your basket" })).toBeVisible({
    timeout: SERVER_ROUND_TRIP,
  });
  const proceed = page.getByRole("button", { name: "Proceed to checkout" });
  await expect(proceed).toBeEnabled({ timeout: SERVER_ROUND_TRIP });
  await proceed.click();

  // A checkout the merchant declined to open renders as a notice and no navigation. Said
  // out loud here, because the alternative is a URL timeout that names nothing.
  await expect(
    page.getByText("Checkout could not be opened"),
    "the merchant refused to open a checkout for this basket",
  ).toHaveCount(0);
  await expect(page).toHaveURL(/\/checkout\/[^/?#]+/, { timeout: SERVER_ROUND_TRIP });
  await expect(page.getByRole("heading", { name: "Approve this order" })).toBeVisible({
    timeout: SERVER_ROUND_TRIP,
  });

  const checkoutId = checkoutIdFrom(page.url());
  openedCheckouts.push(checkoutId);
  return checkoutId;
}

/**
 * Approve the version currently on the card, and wait for the server to say it happened.
 *
 * The approve button carries the amount, so it is found by its role and a prefix rather
 * than by an exact label: the label contains a figure the merchant chose and this helper
 * has no business predicting it.
 */
export async function approveCurrentVersion(page: Page): Promise<void> {
  await page.getByRole("button", { name: /^Approve ₹/ }).click();

  /*
   * Wait for either answer, and read the unhappy one out loud.
   *
   * The card has two outcomes and this used to wait for one of them. When the server
   * declines an approval the card renders the reason in its own error slot and the success
   * heading never arrives, so the only thing the suite reported was a thirty-second
   * timeout on `Version N is approved`: true, useless, and indistinguishable from the app
   * hanging.
   *
   * That is not a hypothetical. One full run of this suite in seven failed at this step
   * and produced exactly that message, and it has not recurred in sixty further runs of
   * the same spec or in four full runs since — so what actually happened is unknown, and
   * the reason it is unknown is that the assertion threw away the only evidence on screen.
   * The seeded tenant is shared with three other suites hammering the same API, and the
   * step times here drifted from nine seconds to sixteen while they ran, which is a hint
   * and not a diagnosis.
   *
   * So this does not retry and does not widen a timeout. It makes the next occurrence
   * self-explaining by putting the card's own words in the failure.
   */
  const approved = page.getByRole("heading", { name: /^Version \d+ is approved$/ });
  const refused = page.locator("#main").getByRole("alert");
  await expect(approved.or(refused).first()).toBeVisible({ timeout: SERVER_ROUND_TRIP });
  if (!(await approved.isVisible())) {
    throw new Error(
      "the approval was not accepted, and the card said why: " +
        (await refused.first().innerText()).replace(/\s+/g, " ") +
        " — this is what a merchant state change between opening a checkout and approving " +
        "it looks like from the browser.",
    );
  }

  await expect(page.getByRole("button", { name: "Pay", exact: true })).toBeEnabled({
    timeout: SERVER_ROUND_TRIP,
  });
}

/* ----------------------------------------- baskets with more than one line in them */

export const RICE_SKU = "INDI-STPL-001";
export const RICE_NAME = "India Gate Classic Basmati Rice 5 kg";

/**
 * Search for a term and add the named product, through the browser, once.
 *
 * Pulled out of `openCheckoutForMilk` rather than copied from it, because a refusal on a
 * checkout with several lines needs the same walk twice with different products and the
 * interesting assertion is about the refusal, not about the adding.
 *
 * `expectedCount` is the cart pill's count after this add, and waiting on it is what makes
 * the step a round trip rather than an optimistic render. The stepper is deliberately not
 * used as the signal for the reason set out on `openCheckoutForMilk`.
 */
export async function addProduct(
  page: Page,
  query: string,
  productName: string,
  expectedCount: number,
): Promise<void> {
  await visibleSearchBox(page).fill(query);
  await visibleSearchBox(page).press("Enter");
  await expect(page).toHaveURL(new RegExp(`/search\\?q=${encodeURIComponent(query)}`));

  const add = page.getByRole("button", { name: `Add ${productName} to basket` });
  await expect(add).toBeVisible({ timeout: SERVER_ROUND_TRIP });
  await add.click();

  const pill =
    expectedCount === 1 ? "My cart, 1 item" : `My cart, ${expectedCount} items`;
  await expect(page.getByRole("link", { name: pill })).toBeVisible({ timeout: SERVER_ROUND_TRIP });
}

/**
 * A checkout over two products, so a refusal can be asked what it says about a basket
 * where only one line moved.
 *
 * The kernel's answer to that turns out to be one `total` delta rather than a delta per
 * line, which is precisely why a spec has to be written against it: an assertion that
 * expected a row per changed product would have been asserting a design nobody built.
 */
export async function openCheckoutForTwoProducts(page: Page): Promise<string> {
  await page.goto("/");
  await addProduct(page, "doodh", MILK_NAME, 1);
  await addProduct(page, "basmati", RICE_NAME, 2);

  await page.goto("/basket");
  await expect(page.getByRole("heading", { name: "Your basket" })).toBeVisible({
    timeout: SERVER_ROUND_TRIP,
  });
  const proceed = page.getByRole("button", { name: "Proceed to checkout" });
  await expect(proceed).toBeEnabled({ timeout: SERVER_ROUND_TRIP });
  await proceed.click();

  await expect(
    page.getByText("Checkout could not be opened"),
    "the merchant refused to open a checkout for this basket",
  ).toHaveCount(0);
  await expect(page).toHaveURL(/\/checkout\/[^/?#]+/, { timeout: SERVER_ROUND_TRIP });
  await expect(page.getByRole("heading", { name: "Approve this order" })).toBeVisible({
    timeout: SERVER_ROUND_TRIP,
  });

  const checkoutId = checkoutIdFrom(page.url());
  openedCheckouts.push(checkoutId);
  return checkoutId;
}

/* --------------------------------------------- the decision the browser was handed */

/** The kernel's answer as it arrived in this browser, not as the test imagined it. */
export interface BrowserDecision {
  allowed: boolean;
  code: string;
  explanation: string | null;
  next_version: number | null;
  deltas: Array<{ field_path: string; approved: unknown; current: unknown; reason: string | null }>;
}

/**
 * Press Pay and keep the submission's own response body.
 *
 * This is the only way to assert that the refusal screen shows *every delta the server
 * sent and nothing it did not*. Reading the checkout back afterwards gives the read
 * model's recomputation of the same comparison, which is a second opinion rather than the
 * evidence: the card renders `decision.deltas` when the decision carried any, so a spec
 * that checked the read model's list could pass while the card dropped a row the kernel
 * had actually sent.
 *
 * Nothing is intercepted or substituted. The response is observed on its way past.
 */
export async function payAndCaptureDecision(page: Page): Promise<BrowserDecision> {
  const submission = page.waitForResponse(
    (response) =>
      /\/api\/backend\/v1\/checkouts\/[^/]+\/versions\/\d+\/submit$/.test(new URL(response.url()).pathname) &&
      response.request().method() === "POST",
    { timeout: SERVER_ROUND_TRIP },
  );
  await page.getByRole("button", { name: "Pay", exact: true }).click();
  const response = await submission;
  expect(
    response.status(),
    "a kernel decision is HTTP 200 whether it admitted or refused (ADR 0003 D15)",
  ).toBe(200);
  return (await response.json()) as BrowserDecision;
}
