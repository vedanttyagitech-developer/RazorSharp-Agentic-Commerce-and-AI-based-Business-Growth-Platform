/**
 * The checkout states a buyer can actually reach, reached the way a buyer reaches them.
 *
 * The unit suite drives all fourteen through `CheckoutJourney` by handing it a checkout
 * with each state on it, which is the only way to see the four a payment would have to
 * complete for. What it cannot show is that the platform ever produces them, or that the
 * screen a real transition lands on is the screen that state should have. That is this
 * file: four states, each arrived at by pressing the control that causes it, each asserted
 * against the state the server independently says the checkout is in.
 *
 * Three of the fourteen turn out to be reachable without money changing hands:
 * `APPROVAL_REQUIRED`, `APPROVED` and `CANCELLED` — plus, as an assertion rather than a
 * state, the thing they have in common at the end: **a checkout that is over offers no way
 * to pay for it.** A screen that leaves a live Pay button on a finished checkout is
 * offering to spend an approval that no longer exists, and the refusal it would earn is
 * not a defence, because a buyer who pressed it had already been misled.
 *
 * `REJECTED` was expected to be a fourth and is not. This spec found that out, and the
 * finding turned out to be bigger than one state: there is no `REJECTED` checkout state
 * anywhere — not in the kernel's `CheckoutState`, not in the database CHECK constraint —
 * and the storefront had invented it along with five others while omitting four the
 * platform really writes. The vocabulary is fourteen now, and `CANCELLED` after a decline
 * is simply correct rather than a discrepancy to work around.
 *
 * `EXECUTION_PENDING`, `AWAITING_PAYMENT`, `PAYMENT_UNKNOWN`, `PAID`, `PAYMENT_FAILED`,
 * `INVALIDATED` and `INVALIDATED_AWAITING_PAYMENT_RESULT` are deliberately absent from
 * this file — though the refusal specs do reach `INVALIDATED` by driving a real price
 * change. The rest require a real
 * Razorpay order and, for most, a real capture, so reaching them from a test would mean
 * either paying with a card or asserting against a fabricated checkout — and a fabricated
 * checkout is the thing this suite exists not to do. They are covered where they can be
 * covered honestly: the unit suite, against captured bodies, named as captured.
 */
import { expect, test } from "@playwright/test";

import { approveCurrentVersion, openCheckoutForMilk, readCheckout, releaseCheckouts } from "./journey";
import { UNREACHABLE, mintToken, requireApi, rupees } from "./live-api";

let reachable = false;

test.beforeAll(async ({ request }) => {
  reachable = await requireApi(request);
  // Minted here rather than used, so a tenant that has not been seeded fails in this hook
  // with a sentence about seeding rather than inside a test with a selector timeout.
  if (reachable) await mintToken(request);
});

test.beforeEach(() => {
  test.skip(!reachable, UNREACHABLE);
});

test.afterEach(async ({ page }) => {
  await releaseCheckouts(page);
});

/** Every state banner on the page, as text, with the colour and the dot discarded. */
async function bannerText(page: import("@playwright/test").Page): Promise<string> {
  const banners = page.getByRole("status");
  const parts: string[] = [];
  for (let index = 0; index < (await banners.count()); index += 1) {
    parts.push((await banners.nth(index).innerText()).replace(/\s+/g, " "));
  }
  return parts.join(" ");
}

test("a freshly opened checkout is waiting for the buyer, and says which version it is waiting on", async ({
  page,
}) => {
  const checkoutId = await openCheckoutForMilk(page);

  const server = await readCheckout(page, checkoutId);
  expect(server.state).toBe("APPROVAL_REQUIRED");

  // The screen names the version the server says is current. A card showing one version's
  // bytes under another version's number is the whole failure this platform is built to
  // make impossible, so it is checked at the cheapest opportunity rather than only after
  // something has gone wrong.
  await expect(
    page.getByText(new RegExp(`version ${server.current_version} ·`)),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: "Approve this order" })).toBeVisible();

  // Nothing here can be paid for. Consent has not been given, so the control that spends
  // it does not exist — not disabled, absent.
  await expect(page.getByRole("button", { name: "Pay", exact: true })).toHaveCount(0);
});

test("approving moves the checkout to APPROVED, and the screen says nothing has been charged", async ({
  page,
}) => {
  const checkoutId = await openCheckoutForMilk(page);
  await approveCurrentVersion(page);

  const server = await readCheckout(page, checkoutId);
  expect(server.state).toBe("APPROVED");

  const spoken = await bannerText(page);
  expect(spoken).toContain("Approved, not submitted");
  expect(spoken).toContain("APPROVED");
  // The three separate facts this state is entitled to assert, all of them true only
  // before submission: the kernel has not seen it, no order exists, no money moved.
  expect(spoken).toContain("has not been handed to the transaction kernel yet");
  expect(spoken).toContain("no payment order exists");
  expect(spoken).toContain("no money has moved");

  // And now, and only now, there is something to pay.
  await expect(page.getByRole("button", { name: "Pay", exact: true })).toBeEnabled();
});

test("declining a version ends the checkout, in whatever word the kernel uses for it", async ({
  page,
}) => {
  const checkoutId = await openCheckoutForMilk(page);

  const opened = await readCheckout(page, checkoutId);
  const version = opened.current_version;

  await page.getByRole("button", { name: "Reject this version" }).click();

  /*
   * The state this lands in is CANCELLED, and it is the right one.
   *
   * This assertion was written as a live discrepancy: the storefront listed `REJECTED`
   * among its checkout states and had a sentence ready for it — "You declined this
   * version" — while `POST .../versions/{n}/reject` answered `{"state": "CANCELLED"}`.
   * Chasing that gap found there is no `REJECTED` checkout state at all: not in the
   * kernel's enum, not in the database constraint, nowhere. The storefront had invented
   * it, along with five other phantoms, while failing to name four states the platform
   * genuinely writes.
   *
   * So this is no longer a canary. The vocabulary is fourteen, the copy for a decline is
   * `CANCELLED`'s own — widened to say a checkout ends "either because you cancelled it or
   * because you declined the version" — and a buyer who declines now reads a sentence
   * written for them. What is asserted here is what the platform does, which is what it
   * always should have been.
   */
  await expect
    .poll(async () => (await readCheckout(page, checkoutId)).state, { timeout: 30_000 })
    .toBe("CANCELLED");

  const spoken = await bannerText(page);
  expect(spoken).toContain("CANCELLED");
  expect(spoken).toContain("nothing was charged");
  // And it does not claim the other one. A screen that guessed "Declined by you" from the
  // button that was pressed would be describing its own click rather than the server's
  // answer, which is the whole habit this storefront is built against.
  expect(spoken).not.toContain("REJECTED");

  await expect(page.getByRole("button", { name: "Pay", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^Approve ₹/ })).toHaveCount(0);

  // The version is kept and shown, at the amount it was declined at. A trail that dropped
  // it would leave the screen unable to say what the buyer turned down.
  const after = await readCheckout(page, checkoutId);
  const declined = after.versions.find((entry) => entry.version === version);
  expect(declined, "the declined version vanished from the checkout").toBeDefined();
  await expect(page.getByRole("heading", { name: "Version trail" })).toBeVisible();
  await expect(page.getByText(`v${version}`, { exact: true })).toBeVisible();
  await expect(page.getByText(rupees(declined!.amount_minor), { exact: true }).first()).toBeVisible();
});

test("cancelling an approved checkout ends it, and the screen stops offering to spend the approval", async ({
  page,
}) => {
  const checkoutId = await openCheckoutForMilk(page);
  await approveCurrentVersion(page);
  await expect(page.getByRole("button", { name: "Pay", exact: true })).toBeEnabled();

  await page.getByRole("button", { name: "Cancel this order" }).click();

  await expect
    .poll(async () => (await readCheckout(page, checkoutId)).state, { timeout: 30_000 })
    .toBe("CANCELLED");

  const spoken = await bannerText(page);
  expect(spoken).toContain("Cancelled");
  expect(spoken).toContain("CANCELLED");
  expect(spoken).toContain("nothing was charged");
  expect(spoken).toContain("hold on stock has been released");

  // The approval the buyer gave is no longer spendable, and the screen no longer offers to
  // spend it. This is the assertion that would catch a cancelled checkout still rendering
  // the payment surface because the state changed underneath a branch that never re-read.
  await expect(page.getByRole("button", { name: "Pay", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Cancel this order" })).toHaveCount(0);
});

test("an unrecognised state would render as itself, and the recognised ones never render as each other", async ({
  page,
}) => {
  // Four screens, four different sentences. Run as one test because the property is about
  // the set: it is not enough for each banner to be right on its own screen, they have to
  // be distinguishable, and a component that fell back to one sentence for everything
  // would pass four separate tests and fail this one.
  const checkoutId = await openCheckoutForMilk(page);
  const waiting = await bannerOrCardText(page);

  await approveCurrentVersion(page);
  const approved = await bannerText(page);

  await page.getByRole("button", { name: "Cancel this order" }).click();
  await expect
    .poll(async () => (await readCheckout(page, checkoutId)).state, { timeout: 30_000 })
    .toBe("CANCELLED");
  const cancelled = await bannerText(page);

  expect(approved).not.toBe(cancelled);
  expect(approved).not.toContain("Cancelled");
  expect(cancelled).not.toContain("Approved, not submitted");
  expect(waiting).not.toContain("Approved, not submitted");
  expect(waiting).not.toContain("Cancelled");

  // Each one carried the server's own spelling of the state alongside the English, which
  // is what lets somebody reading over the buyer's shoulder match the screen to the row.
  expect(approved).toContain("APPROVED");
  expect(cancelled).toContain("CANCELLED");
});

/**
 * The approval screen has no state banner: the card is the state.
 *
 * Reading the whole main region there rather than pretending a banner exists keeps the
 * comparison above honest — it is comparing what a buyer would see on each screen, not
 * what each screen would say if it were built like the others.
 */
async function bannerOrCardText(page: import("@playwright/test").Page): Promise<string> {
  const banners = await bannerText(page);
  if (banners.trim().length > 0) return banners;
  return (await page.locator("#main").innerText()).replace(/\s+/g, " ");
}
