/**
 * The moment the platform exists for, end to end and for real.
 *
 * A buyer is looking at version 1's approval card at one total. Between that card being
 * priced and the press that consents to it, the merchant's price moves — injected here
 * from inside the test, through the same scenario endpoint the demo script uses, against
 * the same catalogue the storefront is reading. The buyer presses "Approve to pay". That
 * press is one act: it records the consent and hands that exact version to the transaction
 * kernel under the lock the approval took. The kernel compares what was approved against
 * what the merchant is selling now, refuses to spend it, and answers HTTP 200 with
 * `allowed: false`.
 *
 * The window used to sit between two presses — approve, then Pay — and this spec used to
 * inject the price into it. That window is gone from the product, not from the platform:
 * `approve-and-pay` moves a version APPROVAL_REQUIRED -> APPROVED -> EXECUTION_PENDING or
 * moves it nowhere at all, so the only gap a merchant's price can move in is the one
 * between the card being drawn and the buyer pressing. That is where the injection goes
 * now, and it is the gap `checkout-journey.tsx` names as the whole point of the call.
 * What is proved is unchanged: an approval is bound to bytes, and bytes that moved cannot
 * be spent.
 *
 * What is asserted is not that a refusal happened — that is the easy half — but that the
 * screen makes it legible: the total that was approved, the total it has become, the
 * difference between them, the field the kernel named, and the sentence that says nothing
 * was charged. Every figure expected below is read back from the server through the app's
 * own proxy, so this spec cannot pass by agreeing with a number the browser invented.
 */
import { expect, test, type Page } from "@playwright/test";

import {
  MILK_SKU,
  openCheckoutForMilk,
  readCheckout,
  releaseCheckouts,
  type BrowserDecision,
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

/**
 * A kernel decision as the submit route answers it, with the two fields that say whether
 * anything was created for it.
 *
 * `BrowserDecision` is `journey.ts`'s shape for the same body and is extended rather than
 * restated. The grant and the attempt are added because "refused" is only worth asserting
 * beside "and nothing now exists that could take this buyer's money".
 */
interface SubmitDecision extends BrowserDecision {
  grant_id: string | null;
  payment_attempt_id: string | null;
}

/**
 * Ask the platform to spend one particular version, and keep the answer it gave.
 *
 * `POST .../versions/{n}/submit` is the kernel's front door — the route the Pay button
 * called when a checkout still rested at `APPROVED`, and still the one thing that turns an
 * approval into a payment attempt. It re-reads the version, the recorded approval and the
 * merchant's state under lock and decides, so submitting a version is how you find out
 * what the *platform* would do rather than what a screen happens to offer.
 *
 * Through `/api/backend/...` rather than at the API directly, so this is the buyer's own
 * session asking: the proxy attaches the token the browser never sees, which makes the
 * refusal that comes back the one this buyer would actually receive rather than one a
 * test's own bearer token produced. `Origin` is supplied because the proxy refuses a write
 * that cannot prove it came from a page of this app and a request context sends no
 * `Sec-Fetch-Site`; the key is fresh every call, because a replayed key returns stored
 * bytes and would prove the idempotency table works rather than that the kernel decided.
 *
 * Nothing is intercepted or substituted, and a non-200 is a failure with the body in it:
 * a kernel decision is HTTP 200 admitted or refused (ADR 0003 D15), so a 4xx here is the
 * proxy or the API failing rather than the kernel answering, and the two must never be
 * allowed to read alike.
 */
async function submitThroughTheProxy(
  page: Page,
  checkoutId: string,
  version: number,
): Promise<SubmitDecision> {
  const response = await page.request.post(
    `/api/backend/v1/checkouts/${encodeURIComponent(checkoutId)}/versions/${version}/submit`,
    {
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": `e2e-submit-${Date.now()}-${Math.round(Math.random() * 1e6)}`,
        Origin: new URL(page.url()).origin,
      },
      data: {},
    },
  );
  const body = await response.text();
  expect(
    response.status(),
    "submitting a version answered something other than a kernel decision: " +
      body.slice(0, 400),
  ).toBe(200);
  return JSON.parse(body) as SubmitDecision;
}

test("a price that moves before the buyer's press is refused, and the screen says exactly what moved", async ({
  page,
  request,
}) => {
  const checkoutId = await openCheckoutForMilk(page);

  // Read before the press rather than after it, because the press is the thing the
  // merchant's change has to catch. This is version 1 as the merchant priced it and as
  // the card now on screen is showing it.
  const drawn = await readCheckout(page, checkoutId);
  const approvedVersion = drawn.current_version;
  const approvedMinor = drawn.versions.find(
    (version) => version.version === approvedVersion,
  )?.amount_minor;
  expect(approvedMinor, "the checkout carried no amount for the version on screen").toBeDefined();

  // The button is found by the total it carries, so consenting is consent to that figure
  // and to no other. It is also the assertion that the card is showing the server's
  // number: a screen that had quietly redrawn itself at some other price would leave this
  // locator unfindable rather than let the walk approve an amount it never checked.
  const approve = page.getByRole("button", { name: `Approve to pay ${rupees(approvedMinor!)}` });
  await expect(approve).toBeVisible();

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
    "e2e: a merchant price change between the card being drawn and the press",
  );
  expect(injection.afterMinor).not.toBe(injection.beforeMinor);

  // Nothing on this route polls while a version waits to be approved, so the card is
  // still the one that was priced before the injection. That is the point: the buyer
  // presses yes to bytes the merchant has already moved away from.
  await approve.click();

  /* ------------------------------------------- the refusal, as the buyer sees it */

  const refusal = page.getByLabel("The transaction kernel refused this submission");
  // The press is one request that records the consent and asks the kernel to spend it,
  // and the screen re-reads the checkout behind it, so this one wait spans two server
  // round trips rather than a render.
  await expect(
    refusal,
    "the press produced no refusal: either the injected price never reached the merchant " +
      "state the kernel re-reads, or an approval was admitted for a total that had moved",
  ).toBeVisible({ timeout: 30_000 });
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

  const drawn = await readCheckout(page, checkoutId);
  const approvedVersion = drawn.current_version;
  const approvedMinor = drawn.versions.find(
    (version) => version.version === approvedVersion,
  )?.amount_minor;
  expect(approvedMinor, "the checkout carried no amount for the version on screen").toBeDefined();

  const before = await currentPrice(request, token, MILK_SKU);
  const injection = await injectPrice(
    request,
    token,
    MILK_SKU,
    before.unitPriceMinor + 100 + Math.floor(Math.random() * 400),
    "e2e: the superseded version must not be payable",
  );
  expect(injection.afterMinor).not.toBe(injection.beforeMinor);

  // Named by its total, so the consent this walk gives is to the pre-injection figure —
  // the version the kernel is about to refuse — and not to whatever the shop is charging
  // by the time the click lands.
  await page.getByRole("button", { name: `Approve to pay ${rupees(approvedMinor!)}` }).click();
  await expect(page.getByLabel("The transaction kernel refused this submission")).toBeVisible({
    timeout: 30_000,
  });

  const after = await readCheckout(page, checkoutId);
  expect(after.versions.find((version) => version.version === approvedVersion)?.state).toBe(
    "INVALIDATED",
  );

  /* ----------------------- cannot be paid for: asked of the platform, not of the screen */

  // The claim in this test's name is about the platform, so the platform is what gets
  // asked. Version 1 — the one the buyer really did press yes to — is submitted to the
  // kernel through the app's own proxy, on this browser's session, exactly as the Pay
  // button did when there was a screen that drew one. The press is gone from the product;
  // the route is not, and neither is anything else that could reach it: an agent, a curl,
  // a second tab, a client written against the same API.
  //
  // This is the claim that "the storefront draws no Pay button" was standing in for, and
  // it is not the same claim: an absent control is a fact about one screen's markup, it
  // says nothing about what the kernel would do if a submission arrived anyway, and it
  // reads identically on a page that failed to render at all. What is asserted here is a
  // refusal the platform made, in its own words. The screen's half is kept, further down,
  // where it is worth something.
  const refused = await submitThroughTheProxy(page, checkoutId, approvedVersion);
  expect(
    refused.allowed,
    "the kernel agreed to spend an approval it had already invalidated",
  ).toBe(false);
  // The reason, named. A refusal for the wrong reason — a lapsed hold, a missing policy
  // binding, an authority problem — would be a different bug wearing this one's clothes,
  // and `allowed: false` alone cannot tell them apart.
  expect(refused.code).toBe("STALE_CHECKOUT");
  expect(refused.explanation).toBe("version_already_invalidated");
  // Nothing was created for it. No grant for the worker to carry to Razorpay, no attempt
  // to charge against, and no successor: a retired version is refused outright, not
  // superseded a second time, so there is no new version here to be quietly approved.
  expect(refused.grant_id).toBeNull();
  expect(refused.payment_attempt_id).toBeNull();
  expect(refused.next_version).toBeNull();

  // And it cost the checkout nothing. Read back through the proxy rather than assumed,
  // because "nothing happened" is only worth saying about a server that was asked again:
  // the same current version (no version 3), the same state (nothing moved toward paying),
  // and version 1 still retired.
  const unmoved = await readCheckout(page, checkoutId);
  expect(unmoved.current_version).toBe(after.current_version);
  expect(unmoved.state).toBe(after.state);
  expect(unmoved.versions.find((version) => version.version === approvedVersion)?.state).toBe(
    "INVALIDATED",
  );

  // The storefront's half of the same claim, made where it means something. On its own an
  // absent control proves only that this screen declines to draw one, and it is equally
  // absent from a blank page — so it is asserted against a positive control taken first:
  // the refusal surface really is on screen and really does offer the one thing it should,
  // a re-read of the successor. Beside that, no pay control anywhere is a fact about this
  // screen rather than about whether the screen rendered. The regexp is deliberately loose:
  // the surface that pays names its button "Pay ₹X", the one this walk left behind named it
  // "Pay", and neither may be here.
  const review = page.getByRole("button", { name: `Review version ${after.current_version}` });
  await expect(review).toBeVisible();
  await expect(page.getByRole("button", { name: /^Pay/ })).toHaveCount(0);

  // Reading the checkout again lands on the successor's own approval card, at its own
  // amount, with its own content hash, and nothing of the old approval carried over.
  await review.click();
  await expect(page.getByRole("heading", { name: "Approve this order" })).toBeVisible({
    timeout: 30_000,
  });

  const successor = await readCheckout(page, checkoutId);
  const successorMinor = successor.approval_card?.amount_minor;
  expect(successorMinor, "the successor version carried no approval card").toBeDefined();
  // Its own amount: the successor is priced at what the merchant moved to, so consenting
  // to it is a different decision from the one the kernel refused.
  expect(successorMinor).not.toBe(approvedMinor);
  await expect(
    page.getByRole("button", { name: `Approve to pay ${rupees(successorMinor!)}` }),
  ).toBeVisible();

  // The trail keeps the refused version visible: it is the evidence that an approval was
  // refused, and hiding it would leave the screen unable to say what changed.
  await expect(page.getByRole("heading", { name: "Version trail" })).toBeVisible();
  await expect(page.getByText("INVALIDATED", { exact: true })).toBeVisible();

  // "Its own content hash" said out loud rather than left to the comment above it. Each
  // row of the trail labels its chip with the full hash the server sent, so this compares
  // two documents the platform published rather than two strings the test made up: the
  // successor is a new set of bytes, and the approval given for the old ones binds to
  // nothing on this card.
  const hashOnScreen = async (version: number): Promise<string> => {
    const label = await page
      .getByLabel(new RegExp(`^Version ${version} content hash: `))
      .first()
      .getAttribute("aria-label");
    expect(label, `version ${version} showed no content hash in the trail`).not.toBeNull();
    return label!;
  };
  expect(await hashOnScreen(after.current_version)).not.toBe(
    await hashOnScreen(approvedVersion),
  );
});
