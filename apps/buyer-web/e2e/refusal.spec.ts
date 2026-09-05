/**
 * The moment the platform exists for, end to end and for real.
 *
 * A buyer approves version 1 at one total. Between that press and the next one, the
 * merchant's price moves — injected here from inside the test, through the same scenario
 * endpoint the demo script uses, against the same catalogue the storefront is reading.
 * The buyer presses Pay. The kernel compares the approval against what the merchant is
 * selling now, refuses to spend it, and answers HTTP 200 with `allowed: false`.
 *
 * What is asserted is not that a refusal happened — that is the easy half — but that the
 * screen makes it legible: the total that was approved, the total it has become, the
 * difference between them, the field the kernel named, and the sentence that says nothing
 * was charged. Every figure expected below is read back from the server through the app's
 * own proxy, so this spec cannot pass by agreeing with a number the browser invented.
 */
import { expect, test } from "@playwright/test";

import {
  MILK_SKU,
  approveCurrentVersion,
  openCheckoutForMilk,
  readCheckout,
  releaseCheckouts,
} from "./journey";
import {
  UNREACHABLE,
  currentPrice,
  injectPrice,
  mintToken,
  requireApi,
  restorePrice,
  rupees,
  signedRupees,
} from "./live-api";

let reachable = false;
let token = "";
/**
 * What the merchant was charging when this file started running.
 *
 * Read once, up front, and put back once at the end — not remembered from the first
 * injection. Each test in here moves the price, so restoring the `before` of one of them
 * would leave the catalogue at whatever the others last set, and a suite run twice a day
 * would ratchet the seeded price upward until the demo stopped resembling a grocery shop.
 */
let priceOnEntry: number | null = null;

test.beforeAll(async ({ request }) => {
  reachable = await requireApi(request);
  if (!reachable) return;
  token = await mintToken(request);
  priceOnEntry = (await currentPrice(request, token, MILK_SKU)).unitPriceMinor;
});

test.beforeEach(() => {
  test.skip(!reachable, UNREACHABLE);
});

test.afterEach(async ({ page }) => {
  // A checkout left open holds a unit of the merchant's stock for fifteen minutes.
  // Handing it back is what keeps this suite from refusing its own next run.
  await releaseCheckouts(page);
});

test.afterAll(async ({ request }) => {
  if (reachable && priceOnEntry !== null) {
    await restorePrice(request, token, MILK_SKU, priceOnEntry);
  }
});

test("a price change between approval and payment is refused, and the screen says exactly what moved", async ({
  page,
  request,
}) => {
  const checkoutId = await openCheckoutForMilk(page);
  await approveCurrentVersion(page);

  const approved = await readCheckout(page, checkoutId);
  const approvedVersion = approved.current_version;
  const approvedMinor = approved.versions.find(
    (version) => version.version === approvedVersion,
  )?.amount_minor;
  expect(approvedMinor, "the checkout carried no amount for the version just approved").toBeDefined();

  // Move the merchant's price to something nobody has set before. Derived from what the
  // merchant is charging this second, because setting the price already in force answers
  // 409 and because two runs of this suite must not collide.
  const before = await currentPrice(request, token, MILK_SKU);
  const target = before.unitPriceMinor + 100 + Math.floor(Math.random() * 400);
  const injection = await injectPrice(
    request,
    token,
    MILK_SKU,
    target,
    "e2e: a merchant price change between approval and payment",
  );
  expect(injection.afterMinor).not.toBe(injection.beforeMinor);

  await page.getByRole("button", { name: "Pay", exact: true }).click();

  /* ------------------------------------------- the refusal, as the buyer sees it */

  const refusal = page.getByLabel("The transaction kernel refused this submission");
  // The press is a kernel admission plus a re-read of the checkout, so this one wait
  // spans two server round trips rather than a render.
  await expect(refusal).toBeVisible({ timeout: 30_000 });
  await expect(refusal).toHaveAttribute("aria-live", "assertive");
  await expect(refusal.getByText("Refused by the transaction kernel")).toBeVisible();
  await expect(refusal.getByText("REAPPROVAL_REQUIRED", { exact: true })).toBeVisible();
  await expect(
    refusal.getByText("merchant_state_changed_since_approval", { exact: true }),
  ).toBeVisible();

  // The refusal is not an error. Nothing was created, and the card is licensed to say so
  // only because the kernel decided before the attempt insert.
  await expect(refusal.getByText("You were not charged.")).toBeVisible();

  /* ------------------------ the two totals and the difference, against the server */

  // Read back after the refusal: the kernel has invalidated the approved version and
  // priced a successor, and these are the two integers the screen is claiming.
  const after = await readCheckout(page, checkoutId);
  expect(after.current_version).toBeGreaterThan(approvedVersion);
  const currentMinor = after.versions.find(
    (version) => version.version === after.current_version,
  )?.amount_minor;
  expect(currentMinor, "the checkout carried no amount for the superseding version").toBeDefined();
  expect(currentMinor).not.toBe(approvedMinor);

  const totals = refusal.getByLabel("The total you approved against the total now");
  await expect(totals).toBeVisible();
  await expect(totals.getByText("You approved")).toBeVisible();
  await expect(totals.getByText(rupees(approvedMinor!), { exact: true })).toBeVisible();
  await expect(totals.getByText("It is now")).toBeVisible();
  await expect(totals.getByText(rupees(currentMinor!), { exact: true })).toBeVisible();
  await expect(totals.getByText("Difference")).toBeVisible();
  await expect(
    totals.getByText(signedRupees(currentMinor! - approvedMinor!), { exact: true }),
  ).toBeVisible();

  /* ------------------------------------------------ the field the kernel named */

  const changed = refusal.getByLabel("What changed");
  await expect(changed).toBeVisible();
  await expect(changed.getByRole("row")).not.toHaveCount(0);
  await expect(changed.getByText("total", { exact: true })).toBeVisible();
  await expect(changed.getByText(rupees(approvedMinor!), { exact: true })).toBeVisible();
  await expect(changed.getByText(rupees(currentMinor!), { exact: true })).toBeVisible();

  /* ----------------------------------------------- the version, and what is next */

  const trail = refusal.getByLabel("The version that was refused and the version that replaces it");
  await expect(trail).toBeVisible();
  await expect(
    trail.getByText(`Version ${approvedVersion} is permanently invalidated.`, { exact: false }),
  ).toBeVisible();

  // Consent to the successor is given on the successor's own card. This button re-reads
  // the checkout and is labelled as the read it performs, not as an approval.
  const surface = refusal.getByLabel("Nothing happens until you approve again. RazorAI cannot.");
  await expect(surface).toBeVisible();
  await expect(
    surface.getByRole("button", { name: `Review version ${after.current_version}` }),
  ).toBeVisible();
  await expect(refusal.getByRole("button", { name: /^Approve/ })).toHaveCount(0);
});

test("the superseded version cannot be paid for, and the successor asks for its own consent", async ({
  page,
  request,
}) => {
  const checkoutId = await openCheckoutForMilk(page);
  await approveCurrentVersion(page);

  const approved = await readCheckout(page, checkoutId);
  const approvedVersion = approved.current_version;

  const before = await currentPrice(request, token, MILK_SKU);
  const injection = await injectPrice(
    request,
    token,
    MILK_SKU,
    before.unitPriceMinor + 100 + Math.floor(Math.random() * 400),
    "e2e: the superseded version must not be payable",
  );
  expect(injection.afterMinor).not.toBe(injection.beforeMinor);

  await page.getByRole("button", { name: "Pay", exact: true }).click();
  await expect(page.getByLabel("The transaction kernel refused this submission")).toBeVisible({
    timeout: 30_000,
  });

  const after = await readCheckout(page, checkoutId);
  expect(after.versions.find((version) => version.version === approvedVersion)?.state).toBe(
    "INVALIDATED",
  );

  // Reading the checkout again lands on the successor's own approval card, at its own
  // amount, with its own content hash, and nothing of the old approval carried over.
  await page.getByRole("button", { name: `Review version ${after.current_version}` }).click();
  await expect(page.getByRole("heading", { name: "Approve this order" })).toBeVisible({
    timeout: 30_000,
  });

  const successor = await readCheckout(page, checkoutId);
  const successorMinor = successor.approval_card?.amount_minor;
  expect(successorMinor, "the successor version carried no approval card").toBeDefined();
  await expect(page.getByRole("button", { name: `Approve ${rupees(successorMinor!)}` })).toBeVisible();

  // The trail keeps the refused version visible: it is the evidence that an approval was
  // refused, and hiding it would leave the screen unable to say what changed.
  await expect(page.getByRole("heading", { name: "Version trail" })).toBeVisible();
  await expect(page.getByText("INVALIDATED", { exact: true })).toBeVisible();
});
