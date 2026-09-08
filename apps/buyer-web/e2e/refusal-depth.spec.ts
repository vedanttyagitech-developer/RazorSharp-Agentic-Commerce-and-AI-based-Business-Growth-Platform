/**
 * The refusal, past the one case the demo shows.
 *
 * `refusal.spec.ts` proves the headline: a price moves out from under a card, the kernel
 * refuses, and the screen says what moved. That is one shape of one refusal, and a
 * storefront that only renders that shape correctly is a storefront that has been tested
 * against the script rather than against the platform. Four more shapes reach the same
 * component and each one has a way of going quietly wrong:
 *
 *  - **Twice in a row.** The second refusal must be about the second pair of versions. A
 *    card that kept comparing against the original approval would show a buyer a
 *    difference they already dealt with, and hide the one they are being asked about now.
 *  - **A price that falls.** The kernel refuses a cheaper order too, because it is still
 *    not the order that was approved. The difference then has to read as less, not more —
 *    a screen that renders every delta in the colour and the sign of a rise is telling
 *    half the buyers of a refusal the opposite of what happened.
 *  - **Stock rather than price.** The merchant sends two deltas here, not one, and the
 *    second is not money at all. The row for it has to render a value that is a word.
 *  - **A hold that lapsed.** `RESERVATION_EXPIRED` carries no successor version and no
 *    deltas whatsoever, so every part of the card that exists to describe a re-pricing has
 *    to stand down. This is the branch where a card that assumed a successor drew
 *    "version 1 → version 1" and called the approval invalidated by itself.
 *
 * **Where the refusal now arrives.** Approving is one act: the press records the buyer's
 * consent and hands that exact version to the kernel in the same transaction
 * (`POST .../versions/{n}/approve-and-pay`), so a version never rests at `APPROVED` with
 * nothing spending it and there is no second press to refuse. The window a refusal lives
 * in is therefore between the card being drawn and the buyer pressing, and every merchant
 * move below is made in that window rather than after an approval. Nothing about what is
 * being proved changed: the kernel still compares the bytes that were consented to against
 * what the merchant is selling now, still refuses, and still has to be legible about it.
 * What changed is which response carries the decision, and these tests read it off the
 * press that actually produced it.
 *
 * And underneath all four, the property that makes a refusal checkable at all: **the
 * screen shows every delta the server sent and nothing it did not.** That is asserted
 * against the press's own response body, captured as it arrives in the browser, rather
 * than against a re-read of the checkout — the card renders the decision's list, so a
 * re-read would be a second opinion standing in for the evidence.
 *
 * Nothing here is stubbed. The prices and the stock move through the same scenario
 * endpoint the demo runbook uses, and everything the merchant is left holding is put back.
 */
import { expect, test, type Page } from "@playwright/test";

import {
  type BrowserDecision,
  MILK_NAME,
  MILK_SKU,
  RICE_NAME,
  openCheckoutForMilk,
  openCheckoutForTwoProducts,
  readCheckout,
  releaseCheckouts,
} from "./journey";
import {
  UNREACHABLE,
  currentPrice,
  currentStock,
  expireReservation,
  inject,
  injectPrice,
  mintToken,
  requireApi,
  restore,
  restorePrice,
  rupees,
  signedRupees,
} from "./live-api";

let reachable = false;
let token = "";

/**
 * What the merchant held when this file started, restored once at the end.
 *
 * Read up front and never re-read, for the reason `refusal.spec.ts` sets out about
 * prices: each test in here moves the merchant, so restoring the "before" of any one of
 * them would leave the catalogue at whatever another test last set, and a suite run twice
 * would ratchet the seeded values away from the shape the demo needs.
 */
let priceOnEntry: number | null = null;
let stockOnEntry: number | null = null;

test.beforeAll(async ({ request }) => {
  reachable = await requireApi(request);
  if (!reachable) return;
  token = await mintToken(request);
  const held = await currentStock(request, token, MILK_SKU);
  priceOnEntry = held.unitPriceMinor;
  stockOnEntry = held.stockUnits;
});

test.beforeEach(() => {
  test.skip(!reachable, UNREACHABLE);
});

test.afterEach(async ({ page, request }) => {
  // Two different debts, settled in the same place. The checkouts hold stock for fifteen
  // minutes; the stock injection holds it for as long as nobody puts it back, and a test
  // that failed halfway through the stock case would otherwise leave the milk sold out
  // and every later test unable to open a checkout at all.
  await releaseCheckouts(page);
  if (stockOnEntry !== null) {
    await restore(request, token, "STOCK_SET", MILK_SKU, stockOnEntry);
  }
});

test.afterAll(async ({ request }) => {
  if (!reachable) return;
  if (priceOnEntry !== null) await restorePrice(request, token, MILK_SKU, priceOnEntry);
  if (stockOnEntry !== null) await restore(request, token, "STOCK_SET", MILK_SKU, stockOnEntry);
});

/** A price nobody has set before, derived from what is charged now: 409 means no change. */
function freshPriceAbove(current: number): number {
  return current + 100 + Math.floor(Math.random() * 400);
}

/**
 * Press "Approve to pay" and keep the answer the kernel gave that press.
 *
 * `journey.ts` has `payAndCaptureDecision`, which waits on `POST .../submit` and presses
 * Pay. Both halves of that belong to a checkout resting at `APPROVED` — a rest this walk
 * no longer has. The approve press is one act now, so the submission the kernel refuses is
 * made by the press itself, on `approve-and-pay`, and a wait armed for `submit` waits for
 * a request nobody makes. That is precisely how these five tests failed: thirty seconds of
 * nothing, and not one word about the refusal that had already been rendered.
 *
 * Everything else is `payAndCaptureDecision`'s reasoning unchanged, and it is the reason
 * this is a capture rather than a click followed by a re-read. The response body is the
 * only evidence of what the server actually sent this browser; reading the checkout back
 * afterwards gives the read model's recomputation of the same comparison, which is a
 * second opinion. The card renders `decision.deltas`, so a spec that checked the read
 * model's list could pass while the card dropped a row the kernel had really sent.
 *
 * Nothing is intercepted or substituted. The response is observed on its way past.
 */
async function approveAndCaptureDecision(page: Page): Promise<BrowserDecision> {
  const consent = page.waitForResponse(
    (response) =>
      /\/api\/backend\/v1\/checkouts\/[^/]+\/versions\/\d+\/approve-and-pay$/.test(
        new URL(response.url()).pathname,
      ) && response.request().method() === "POST",
    { timeout: 30_000 },
  );
  await page.getByRole("button", { name: /^Approve to pay ₹/ }).click();
  const response = await consent;
  // A non-200 is the API refusing before the kernel decided, and the problem document says
  // which of the three refusals it was. Reading it into the failure is the difference
  // between "expected 200, received 409" and a sentence naming the cause -- the same reason
  // stuckApproving() exists a few functions away.
  if (response.status() !== 200) {
    const body = await response.text();
    let said = body.slice(0, 300);
    try {
      const problem = JSON.parse(body) as { title?: string; detail?: string };
      said = `${problem.title ?? "untitled"} — ${problem.detail ?? "no detail"}`;
    } catch {
      // Not a problem document; the raw first bytes are more use than nothing.
    }
    throw new Error(
      `a kernel decision is HTTP 200 whether it admitted or refused (ADR 0003 D15), but ` +
        `${new URL(response.url()).pathname} answered ${response.status()}: ${said}`,
    );
  }
  return (await response.json()) as BrowserDecision;
}

/**
 * The version on screen and the amount its card is asking for, taken from the server.
 *
 * Read before the press rather than after it. The press spends the version in the same
 * gesture that consents to it, so by the time there is a decision to read, the version the
 * buyer approved has already been invalidated and a successor priced; the figure this
 * needs is the one the buyer is about to agree to, and that only exists beforehand. The
 * button assertion is what ties the two together: the card on screen names the amount the
 * server named, so a spec that later says "you approved ₹X" is not saying it about a
 * number the browser had to itself.
 */
async function versionOnScreen(
  page: Page,
  checkoutId: string,
): Promise<{ version: number; minor: number }> {
  const read = await readCheckout(page, checkoutId);
  const version = read.current_version;
  const minor = read.versions.find((entry) => entry.version === version)?.amount_minor;
  expect(minor, "the checkout carried no amount for the version it is asking about").toBeDefined();
  await expect(
    page.getByRole("button", { name: `Approve to pay ${rupees(minor!)}` }),
    "the card on screen is not asking for the amount the server holds for this version",
  ).toBeVisible({ timeout: 30_000 });
  return { version, minor: minor! };
}

test("a second price change under a second card is refused against the second version, not the first", async ({
  page,
  request,
}) => {
  const checkoutId = await openCheckoutForMilk(page);
  const first = await versionOnScreen(page, checkoutId);
  const v1 = first.version;
  const v1Minor = first.minor;

  const before = await currentPrice(request, token, MILK_SKU);
  await injectPrice(
    request,
    token,
    MILK_SKU,
    freshPriceAbove(before.unitPriceMinor),
    "e2e: the first of two price changes",
  );

  const firstDecision = await approveAndCaptureDecision(page);
  expect(firstDecision.allowed).toBe(false);
  expect(firstDecision.next_version).not.toBeNull();

  const refusal = page.getByLabel("The transaction kernel refused this submission");
  await expect(refusal).toBeVisible({ timeout: 30_000 });

  /* ------------------------------------ consent to the successor, then move the price again */

  const v2 = firstDecision.next_version!;
  await page.getByRole("button", { name: `Review version ${v2}` }).click();
  await expect(page.getByRole("heading", { name: "Approve this order" })).toBeVisible({
    timeout: 30_000,
  });

  const second = await versionOnScreen(page, checkoutId);
  expect(second.version, "reviewing the successor did not land on the successor").toBe(v2);
  const v2Minor = second.minor;
  expect(v2Minor).not.toBe(v1Minor);

  const between = await currentPrice(request, token, MILK_SKU);
  await injectPrice(
    request,
    token,
    MILK_SKU,
    freshPriceAbove(between.unitPriceMinor),
    "e2e: the second of two price changes",
  );

  const secondDecision = await approveAndCaptureDecision(page);
  expect(secondDecision.allowed).toBe(false);
  const v3 = secondDecision.next_version;
  expect(v3, "the second refusal created no successor version").not.toBeNull();
  expect(v3!).toBeGreaterThan(v2);

  /* ------------------------------ the second refusal is about the second pair of versions */

  await expect(refusal).toBeVisible({ timeout: 30_000 });
  const trail = refusal.getByLabel(
    "The version that was refused and the version that replaces it",
  );
  await expect(
    trail.getByText(`Version ${v2} is permanently invalidated.`, { exact: false }),
  ).toBeVisible();

  const after = await readCheckout(page, checkoutId);
  const v3Minor = after.versions.find((version) => version.version === v3)?.amount_minor;
  expect(v3Minor, "the second successor carried no amount").toBeDefined();

  // The two figures on screen are version 2's and version 3's. Version 1's amount is the
  // one a card comparing against the original approval would have drawn, and it must not
  // be either of them — which is what makes this assertion able to fail.
  const totals = refusal.getByLabel("The total you approved against the total now");
  await expect(totals.getByText(rupees(v2Minor), { exact: true })).toBeVisible();
  await expect(totals.getByText(rupees(v3Minor!), { exact: true })).toBeVisible();
  await expect(
    totals.getByText(signedRupees(v3Minor! - v2Minor), { exact: true }),
  ).toBeVisible();

  // The trail below keeps all three, because the evidence of two refusals is two dead
  // versions and hiding either would leave the screen unable to say what happened.
  await expect(page.getByRole("heading", { name: "Version trail" })).toBeVisible();
  for (const version of [v1, v2, v3!]) {
    await expect(page.getByText(`v${version}`, { exact: true })).toBeVisible();
  }
  expect(after.versions.find((version) => version.version === v1)?.state).toBe("INVALIDATED");
  expect(after.versions.find((version) => version.version === v2)?.state).toBe("INVALIDATED");
});

test("a price that falls is refused too, and the difference reads as less rather than more", async ({
  page,
  request,
}) => {
  const checkoutId = await openCheckoutForMilk(page);
  const { version: approvedVersion, minor: approvedMinor } = await versionOnScreen(
    page,
    checkoutId,
  );

  // Downward, and far enough below that the total cannot land back on the approved one.
  const before = await currentPrice(request, token, MILK_SKU);
  const injection = await injectPrice(
    request,
    token,
    MILK_SKU,
    before.unitPriceMinor - 100 - Math.floor(Math.random() * 300),
    "e2e: a merchant price cut between the card being drawn and the press",
  );
  expect(injection.afterMinor).toBeLessThan(injection.beforeMinor);

  const decision = await approveAndCaptureDecision(page);

  // The whole point of the case: cheaper is still not what was approved.
  expect(
    decision.allowed,
    "the kernel admitted a version the buyer never approved because it happened to be cheaper",
  ).toBe(false);

  const refusal = page.getByLabel("The transaction kernel refused this submission");
  await expect(refusal).toBeVisible({ timeout: 30_000 });
  await expect(refusal.getByText("You were not charged.")).toBeVisible();

  const after = await readCheckout(page, checkoutId);
  expect(after.current_version, "the refusal priced no successor to compare against").toBeGreaterThan(
    approvedVersion,
  );
  const currentMinor = after.versions.find(
    (version) => version.version === after.current_version,
  )?.amount_minor;
  expect(currentMinor).toBeDefined();
  expect(currentMinor).toBeLessThan(approvedMinor);

  const totals = refusal.getByLabel("The total you approved against the total now");
  await expect(totals.getByText(rupees(approvedMinor), { exact: true })).toBeVisible();
  await expect(totals.getByText(rupees(currentMinor!), { exact: true })).toBeVisible();

  // A true minus sign, not a hyphen, and not a plus. `signedRupees` in this suite spells
  // the sign independently of `formatDelta` in the app, so agreeing here is agreement
  // between two implementations rather than one implementation with itself.
  const difference = signedRupees(currentMinor! - approvedMinor);
  expect(difference.startsWith("−"), "the suite's own formatter did not sign a fall").toBe(true);
  await expect(totals.getByText(difference, { exact: true })).toBeVisible();
  // And the rise's spelling of the same magnitude is nowhere on the screen.
  await expect(
    refusal.getByText(signedRupees(approvedMinor - currentMinor!), { exact: true }),
  ).toHaveCount(0);

  // The fall appears on every component it moved, not once. A single-line cart priced down
  // moves the unit price, the line total, the subtotal and the order total by the same
  // amount, and the kernel now names each of them -- so the count is a fact about the
  // comparison rather than an artefact of it having compared only the sum.
  const changed = refusal.getByLabel("What changed");
  const fallenRows = changed.getByText(difference, { exact: true });
  await expect(fallenRows.first()).toBeVisible();
  expect(
    await fallenRows.count(),
    "a cart-wide fall must be shown on each component it moved",
  ).toBeGreaterThan(1);

  // And the line it moved on is named, so the buyer is told which item changed rather
  // than only that the sum did.
  await expect(changed.getByText(MILK_NAME, { exact: false }).first()).toBeVisible();
});

test("a stock change rather than a price change is refused, and both deltas the merchant sent are shown", async ({
  page,
  request,
}) => {
  const checkoutId = await openCheckoutForMilk(page);
  // The amount is not read back here, but the card still has to be the server's: this is
  // the one test where the total never moves, so a card showing some other version's money
  // would leave every assertion below true of a screen nobody could have consented from.
  await versionOnScreen(page, checkoutId);

  // Below what this checkout holds, so the merchant cannot price the approved line at all.
  // The price is left exactly where it was: this test is about the other lever.
  const priceBefore = (await currentPrice(request, token, MILK_SKU)).unitPriceMinor;
  const injection = await inject(
    request,
    token,
    "STOCK_SET",
    MILK_SKU,
    0,
    "e2e: the merchant sells out while the buyer is reading the card",
  );
  expect(injection.deltas.some((delta) => delta.field === "stock_units")).toBe(true);

  const decision = await approveAndCaptureDecision(page);
  expect(decision.allowed).toBe(false);

  const refusal = page.getByLabel("The transaction kernel refused this submission");
  await expect(refusal).toBeVisible({ timeout: 30_000 });
  await expect(refusal.getByText("You were not charged.")).toBeVisible();

  // The merchant did not change the price, and the screen must not suggest it did.
  expect((await currentPrice(request, token, MILK_SKU)).unitPriceMinor).toBe(priceBefore);

  /* ------------------------------- every delta the kernel sent, and nothing it did not */

  const changed = refusal.getByLabel("What changed");
  await expect(changed).toBeVisible();
  const rows = changed.getByRole("row");
  // One header row plus one row per delta the browser was actually handed.
  await expect(rows).toHaveCount(decision.deltas.length + 1);

  for (const delta of decision.deltas) {
    await expect(
      changed.getByText(delta.field_path, { exact: true }),
      `the refusal dropped the delta the kernel sent for ${delta.field_path}`,
    ).toBeVisible();
  }

  // An availability delta is not money, and its row has to render the words the merchant
  // used. A table that only knew how to draw rupees would print nothing here, or worse,
  // print a number.
  const availability = decision.deltas.find((delta) => delta.field_path === "line_items");
  expect(
    availability,
    "the merchant reported no availability delta for a sell-out; the rest of this test assumes one",
  ).toBeDefined();
  await expect(changed.getByText("Items in this order", { exact: true })).toBeVisible();

  await restore(request, token, "STOCK_SET", MILK_SKU, stockOnEntry ?? 48);
});

test("a refusal on a cart of several lines shows exactly the deltas the kernel sent for it", async ({
  page,
  request,
}) => {
  const checkoutId = await openCheckoutForTwoProducts(page);
  const { minor: approvedMinor } = await versionOnScreen(page, checkoutId);

  // Only the milk moves. The rice is untouched, and the assertion below is that the screen
  // does not invent a row for it.
  const before = await currentPrice(request, token, MILK_SKU);
  await injectPrice(
    request,
    token,
    MILK_SKU,
    freshPriceAbove(before.unitPriceMinor),
    "e2e: one line of several moves",
  );

  const decision = await approveAndCaptureDecision(page);
  expect(decision.allowed).toBe(false);

  const refusal = page.getByLabel("The transaction kernel refused this submission");
  await expect(refusal).toBeVisible({ timeout: 30_000 });

  const changed = refusal.getByLabel("What changed");
  await expect(changed).toBeVisible();

  // The header row, plus the kernel's list, exactly. Both halves matter: a missing row is
  // evidence withheld from the buyer, and an extra row is a claim about a line that did
  // not move.
  await expect(changed.getByRole("row")).toHaveCount(decision.deltas.length + 1);

  // The kernel compares the whole document now, not only the total, so a cart-wide price
  // change is answered with the components that moved and the line they moved on. The
  // milk's name is therefore expected on this table -- the table resolves a `lines[SKU]`
  // path to the display name the quote carried -- and its presence is the assertion that
  // the buyer is told *which* item changed rather than only that the sum did.
  await expect(changed.getByText(MILK_NAME, { exact: false }).first()).toBeVisible();

  // And the line that did not move is still not described as having moved. The rice's
  // name appears on this checkout's quote, so its absence here is the guard against a
  // comparator that refuses too much: over-reporting is the other way to be wrong, and
  // the harder one to notice, because a refusal always looks like the system working.
  await expect(changed.getByText(RICE_NAME, { exact: false })).toHaveCount(0);

  const after = await readCheckout(page, checkoutId);
  const currentMinor = after.versions.find(
    (version) => version.version === after.current_version,
  )?.amount_minor;
  const totals = refusal.getByLabel("The total you approved against the total now");
  await expect(totals.getByText(rupees(approvedMinor), { exact: true })).toBeVisible();
  await expect(totals.getByText(rupees(currentMinor!), { exact: true })).toBeVisible();
});

test("a hold that lapsed is refused with no successor, and the card claims none", async ({
  page,
  request,
}) => {
  const checkoutId = await openCheckoutForMilk(page);
  const { version: approvedVersion } = await versionOnScreen(page, checkoutId);

  // The runbook's own instrument, and it has to be reached for before the press rather
  // than after it: consent and admission are one gesture now, so there is no pause between
  // them for a hold to lapse in. The lapse belongs where a buyer meets it — while the card
  // is on screen and being read — and a fifteen-minute hold cannot be waited out inside a
  // test, so it is ended here instead of waited for.
  await expireReservation(request, token, checkoutId, approvedVersion);

  const decision = await approveAndCaptureDecision(page);
  expect(decision.allowed).toBe(false);
  expect(decision.code).toBe("RESERVATION_EXPIRED");
  expect(decision.explanation).toBe("reservation_not_valid");
  // The two facts the rest of this test turns on, taken from the wire rather than assumed.
  expect(decision.next_version).toBeNull();
  expect(decision.deltas).toHaveLength(0);

  const refusal = page.getByLabel("The transaction kernel refused this submission");
  await expect(refusal).toBeVisible({ timeout: 30_000 });

  await expect(refusal.getByText("RESERVATION_EXPIRED", { exact: true })).toBeVisible();
  await expect(refusal.getByText("The hold on the stock ran out")).toBeVisible();
  await expect(
    refusal.getByText("The stock this order was holding is no longer held."),
  ).toBeVisible();
  await expect(refusal.getByText("You were not charged.")).toBeVisible();

  /* ------------------------------- nothing was re-priced, so nothing is drawn as if it was */

  // No successor version means no version arrow and no "already priced and waiting".
  await expect(
    refusal.getByLabel("The version that was refused and the version that replaces it"),
  ).toHaveCount(0);
  const stood = refusal.getByLabel("What happened to the version you approved");
  await expect(stood).toBeVisible();
  await expect(stood.getByText("No superseding version was created.", { exact: false })).toBeVisible();

  // No deltas means no comparison of two totals. This is the assertion that would have
  // caught a card drawing "₹57.50 → ₹57.50" out of a version compared with itself.
  await expect(
    refusal.getByLabel("The total you approved against the total now"),
  ).toHaveCount(0);
  await expect(refusal.getByLabel("What changed")).toHaveCount(0);

  // And the button says what pressing it does, which here is a read and not an approval.
  const surface = refusal.getByLabel("Only you decide what happens next. RazorAI cannot.");
  await expect(surface).toBeVisible();
  await expect(surface.getByRole("button", { name: "Read this checkout again" })).toBeVisible();
  await expect(refusal.getByRole("button", { name: /^Review version/ })).toHaveCount(0);
  await expect(refusal.getByRole("button", { name: /^Approve/ })).toHaveCount(0);

  // The consent the buyer gave is recorded and unspent. One press records the approval and
  // submits it in the same transaction, so a refusal here is the second half failing after
  // the first half succeeded: the version really is APPROVED, at the version the buyer was
  // looking at, and the kernel declined to spend it rather than retiring it. A screen — or
  // a platform — that took the refusal as a reason to throw the approval away would fail
  // here, and so would one that superseded a version nothing had been re-priced for.
  const after = await readCheckout(page, checkoutId);
  expect(after.current_version).toBe(approvedVersion);
  expect(after.versions.find((version) => version.version === approvedVersion)?.state).toBe(
    "APPROVED",
  );
  // The hold is already gone, so `afterEach`'s cancellation is only tidying the row.
});
