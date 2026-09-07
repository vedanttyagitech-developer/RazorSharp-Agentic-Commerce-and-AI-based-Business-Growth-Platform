/**
 * The checkout states a buyer can actually reach, reached the way a buyer reaches them.
 *
 * The unit suite drives all fourteen through `CheckoutJourney` by handing it a checkout
 * with each state on it, which is the only way to see the four a payment would have to
 * complete for. What it cannot show is that the platform ever produces them, or that the
 * screen a real transition lands on is the screen that state should have. That is this
 * file: three states, each arrived at the way this app makes it reachable — two by pressing
 * the control that causes them, and the last through the app's own proxy, because once the
 * approval has been spent the checkout route offers no control that ends the order — each
 * asserted against the state the server independently says the checkout is in.
 *
 * That last clause is a claim about the app, so it is asserted rather than asserted-in-a
 * -comment: at the moment `cancelThroughTheApp` routes around the missing control it reads
 * the server's own `cancellable` — true, because EXECUTION_PENDING has a CANCELLED edge —
 * and then asserts that the screen is drawing its Pay control and nothing that would end
 * the order. The day the storefront grows a cancel control on that screen, this file fails
 * and the fix is to press it. The gap is in this pass's `appGaps` as well.
 *
 * Three of the fourteen turn out to be reachable without money changing hands:
 * `APPROVAL_REQUIRED`, `EXECUTION_PENDING` and `CANCELLED` — plus, as an assertion rather
 * than a state, the thing they have in common at the end: **a checkout that is over offers
 * no way to pay for it.** A screen that leaves a live Pay button on a finished checkout is
 * offering to spend an approval that no longer exists, and the refusal it would earn is
 * not a defence, because a buyer who pressed it had already been misled.
 *
 * `APPROVED` was the second of those three and is not any more. Approving is one act now:
 * the press records the buyer's consent and hands that exact version to the kernel under a
 * single lock, so a version moves APPROVAL_REQUIRED -> APPROVED -> EXECUTION_PENDING inside
 * the gesture and never rests at APPROVED. The screen APPROVED owns — the card reading
 * "Version N is approved" above a Pay button — therefore belongs to a checkout approved
 * somewhere else, and a spec that asserted it here was asserting a screen no buyer walking
 * this path ever sees. It is covered where it can be covered honestly: the unit suite,
 * against a journey handed that state directly.
 *
 * `REJECTED` was expected to be a fourth and is not. This spec found that out, and the
 * finding turned out to be bigger than one state: there is no `REJECTED` checkout state
 * anywhere — not in the kernel's `CheckoutState`, not in the database CHECK constraint —
 * and the storefront had invented it along with five others while omitting four the
 * platform really writes. The vocabulary is fourteen now, and `CANCELLED` after a decline
 * is simply correct rather than a discrepancy to work around.
 *
 * `AWAITING_PAYMENT`, `PAYMENT_UNKNOWN`, `PAID`, `PAYMENT_FAILED`, `INVALIDATED` and
 * `INVALIDATED_AWAITING_PAYMENT_RESULT` are deliberately absent from this file — though
 * the refusal specs do reach `INVALIDATED` by driving a real price change. The rest
 * require a real Razorpay order and, for most, a real capture, so reaching them from a
 * test would mean either paying with a card or asserting against a fabricated checkout —
 * and a fabricated checkout is the thing this suite exists not to do. They are covered
 * where they can be covered honestly: the unit suite, against captured bodies, named as
 * captured.
 */
import { expect, test } from "@playwright/test";

import {
  type CheckoutRead,
  approveCurrentVersion,
  openCheckoutForMilk,
  readCheckout,
  releaseCheckouts,
} from "./journey";
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

/**
 * Wait for the screen to be showing the state the server already said it was in.
 *
 * The journey helper stops as soon as *a* `role="status"` is visible, and a bare spinner
 * is one: `Button` and the payment panel's own loading card both render `Spinner`, which
 * carries `role="status"` and no text at all. So "a status element exists" is not "the
 * state banner is on screen", and reading the banner the instant the helper returned was
 * reading whatever had rendered first. This waits for the server's own spelling of the
 * state to appear inside a banner, which is the only thing that says the screen has caught
 * up with the write it made — and it waits rather than retries, so a screen that never
 * follows still fails.
 *
 * What it returns is the text that satisfied the poll, not a second reading of the page.
 * Reading twice would let the sentences be asserted against a screen one transition later
 * than the one that was waited for, which is a race the caller could not see.
 */
async function bannerSaying(
  page: import("@playwright/test").Page,
  state: string,
): Promise<string> {
  let seen = "";
  await expect
    .poll(async () => (seen = await bannerText(page)), { timeout: 30_000 })
    .toContain(state);
  return seen;
}

/**
 * Every control the checkout screen itself is offering, by the words written on it.
 *
 * Scoped to `#main`, which is the storefront's own document. RazorAI is mounted outside it
 * in the root layout and its launcher is a button on every route, so a page-wide count of
 * buttons would make "what this screen offers" partly a statement about the copilot's
 * chrome. The copilot is also *allowed* to draw a button that says Pay inside its own
 * panel — that is the point of `TrustedSurface`: the agent may propose anything and is
 * wired to nothing — so the claims below are about the surface that is wired to money.
 *
 * Returned as text rather than counted, so a set that is not what was expected fails
 * naming the controls that were there instead of reporting a number.
 */
async function screenControls(page: import("@playwright/test").Page): Promise<string[]> {
  const labels = await page.locator("#main").getByRole("button").allInnerTexts();
  return labels.map((label) => label.replace(/\s+/g, " ").trim());
}

/**
 * The checkout as the screen reads it, including the one field `CheckoutRead` does not
 * carry.
 *
 * `checkout-journey` draws every control that ends a checkout behind `checkout.cancellable`,
 * and the API computes that field from the state table itself —
 * `can_transition(state, CANCELLED)` — so it is the server's own answer to "could this
 * still be ended?", not the storefront's opinion about it. A spec asserting about those
 * controls has to be able to read the same field the component reads, and `journey.ts`
 * types only what its own helpers use.
 *
 * The field's presence is checked rather than assumed. A read model that stopped sending it
 * would otherwise turn every assertion here into a comparison against `undefined`, which
 * fails for a reason that has nothing to do with what was being asked.
 */
async function readEndability(
  page: import("@playwright/test").Page,
  checkoutId: string,
): Promise<CheckoutRead & { cancellable: boolean }> {
  const read = (await readCheckout(page, checkoutId)) as CheckoutRead & { cancellable?: unknown };
  expect(
    typeof read.cancellable,
    "the checkout read stopped carrying `cancellable`, which is the field the screen draws " +
      "its ending controls from — nothing here can be said about those controls without it",
  ).toBe("boolean");
  return read as CheckoutRead & { cancellable: boolean };
}

/**
 * End the checkout through the app's own proxy, carrying the browser's own session.
 *
 * Every other gesture in this file is a press. This one cannot be: once the approval has
 * been spent there is no control on the checkout route that ends the order — the payment
 * surface draws Pay and nothing else. The kernel is willing, and that is the point: the
 * `CANCELLED` edge runs from DRAFT through EXECUTION_PENDING, and cancelling there
 * releases the reservation and revokes the execution grant the press bought. So the
 * checkout genuinely can reach CANCELLED from where this walk stands; only the button is
 * missing. The gap is reported rather than papered over, and meanwhile the cancellation
 * travels the way `releaseCheckouts` sends its own — this page's cookie, this app's proxy,
 * and the `Origin` a browser would have sent.
 *
 * The gap is also *asserted*, here, before the post that steps over it — because "the app
 * offers no control" is a claim about the app and a claim in a comment is not tested. Two
 * facts make it: the server still says this checkout can be ended, and the screen offers
 * nothing that would end it. `checkout-journey` draws "Cancel this order" in its APPROVED
 * branch and in its terminal branch, and one press now carries a version through APPROVED
 * without resting there, so on the payment screen the control is simply absent. When that
 * changes, this helper fails and the repair is to press the button instead of posting.
 *
 * Called from the payment screen and it says so: the Pay control is counted first, which is
 * what makes the zero beside it a fact about this surface rather than about a page that had
 * not finished rendering.
 *
 * The kernel's answer is read rather than assumed. `POST .../cancel` is HTTP 200 whether
 * it cancelled or refused, so a refusal left unread would surface thirty seconds later as
 * a state that simply never changed, with nothing on screen or in the failure to say why.
 */
async function cancelThroughTheApp(
  page: import("@playwright/test").Page,
  checkoutId: string,
): Promise<void> {
  const before = await readEndability(page, checkoutId);
  expect(
    before.cancellable,
    `the server no longer says this checkout can be ended: it reads ${before.state}. ` +
      "AWAITING_PAYMENT has no CANCELLED edge, so a provider order was created while this " +
      "walk stood on the payment screen — that is a different test from the one being run",
  ).toBe(true);
  await expect(
    page.locator("#main").getByRole("button", { name: /^Pay ₹/ }),
    "the payment screen is not up, so the absence asserted next would be about nothing",
  ).toHaveCount(1);
  await expect(
    page.locator("#main").getByRole("button", { name: "Cancel this order" }),
    "the payment screen has grown a control that ends the order — press it, and delete " +
      "the workaround this helper exists to be",
  ).toHaveCount(0);

  const response = await page.request.post(
    `/api/backend/v1/checkouts/${encodeURIComponent(checkoutId)}/cancel`,
    {
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": `e2e-states-cancel-${checkoutId}`,
        Origin: new URL(page.url()).origin,
      },
      data: { reason: "buyer_cancelled" },
    },
  );
  expect(response.ok(), "the app's proxy would not carry the cancellation").toBe(true);
  const verdict = (await response.json()) as {
    allowed?: boolean;
    code?: string;
    from_state?: string | null;
  };
  expect(
    verdict.allowed !== false,
    `the kernel refused to cancel this checkout: ${verdict.code ?? "no code sent"}, read as ` +
      `${verdict.from_state ?? "an unstated state"} when it refused — from EXECUTION_PENDING ` +
      "that means a payment attempt was already past CREATED, which is a different test",
  ).toBe(true);
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
  //
  // `/^Pay/` and not the exact word "Pay", which is what stood here: the control this
  // screen must not be offering is the payment panel's, and that one is named with its
  // amount — "Pay ₹579.95" — so an exact-word check passed over the very button the line
  // exists to watch for and would have gone on passing after the panel appeared.
  //
  // And the zero is measured against a screen that demonstrably draws controls. This one
  // offers three, read as the whole set rather than looked for one at a time: a fourth
  // control appearing on the screen where a buyer decides about money is worth failing over
  // whatever it turns out to say, and a set nobody counted cannot be said to be missing one.
  expect(
    await screenControls(page),
    "the approval screen is not offering the controls it should",
  ).toEqual([
    // RazorAI reads the card aloud and listens for a yes. Matched rather than quoted
    // because the label flips to "Read it again" once a session is running.
    expect.stringMatching(/^(Or say it aloud|Read it again)$/),
    expect.stringMatching(/^Approve to pay ₹/),
    "Reject this version",
  ]);
  await expect(page.getByRole("button", { name: /^Pay/ })).toHaveCount(0);
});

test("approving admits the checkout in the same press, and the screen says nothing has been charged", async ({
  page,
}) => {
  const checkoutId = await openCheckoutForMilk(page);
  await approveCurrentVersion(page);

  // One press, two transitions. The endpoint records the consent and hands that exact
  // version to the kernel under one lock, so the version passes through APPROVED inside the
  // gesture and comes to rest at EXECUTION_PENDING. Finding it at APPROVED here would mean
  // the admission half of the press did not happen and a grant nobody spent was left for a
  // sweeper — which is precisely the window this endpoint was rebuilt to close.
  const server = await readCheckout(page, checkoutId);
  expect(server.state).toBe("EXECUTION_PENDING");

  const spoken = await bannerSaying(page, "EXECUTION_PENDING");
  expect(spoken).toContain("Admitted, order being created");
  // The three separate facts this state is entitled to assert. They are not APPROVED's
  // three — the kernel has seen this one, and saying it had not would be the screen lying
  // about where the buyer's consent now is — but the one a buyer is standing there for is
  // the same fact, said in the same breath: nothing has been charged.
  expect(spoken).toContain("Setting up your payment. Nothing has been charged.");
  expect(spoken).toContain("issued a single-use execution grant");
  expect(spoken).toContain("No payment page exists yet");
  expect(spoken).toContain("nothing has been charged, and the grant cannot be spent a second time");

  // The consent was spent by the press, so there is nothing left to consent to. A screen
  // still offering the button would be inviting a second approval of one thing, against a
  // version that already carries the first.
  await expect(page.getByRole("button", { name: /^Approve to pay ₹/ })).toHaveCount(0);

  // And what stands in its place is the payment surface for the version the server calls
  // current — this replaces the old "and now, and only now, there is something to pay",
  // which waited for a bare Pay button on a checkout resting at APPROVED that this walk no
  // longer passes through. The control here is the panel's own, named with the amount, and
  // it is deliberately dark: the grant is out, the worker has not created the provider
  // order, and the banner above has just said that no payment page exists yet. A live Pay
  // button at EXECUTION_PENDING would be offering a payment page that does not exist.
  await expect(
    page.getByRole("heading", { name: `Pay for version ${server.current_version}` }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: /^Pay ₹/ })).toBeDisabled();
});

test("declining a version ends the checkout, in whatever word the kernel uses for it", async ({
  page,
}) => {
  const checkoutId = await openCheckoutForMilk(page);

  const opened = await readCheckout(page, checkoutId);
  const version = opened.current_version;

  // What the screen offers before the decline, read as the whole set. Without it the
  // absences asserted after the press would be statements about a screen whose controls
  // were never counted — and every one of them would read the same on a page that had
  // failed to render at all.
  expect(
    await screenControls(page),
    "the approval screen is not offering the controls it should",
  ).toEqual([
    expect.stringMatching(/^(Or say it aloud|Read it again)$/),
    expect.stringMatching(/^Approve to pay ₹/),
    "Reject this version",
  ]);

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

  // Every way of answering is gone — the spoken yes and the written one as much as the
  // refusal that was pressed — and nothing has taken their place. Measured against the
  // three counted before the press, so the empty set is a disappearance rather than a claim
  // about a screen that might never have had anything on it.
  await expect
    .poll(async () => await screenControls(page), {
      message: "a declined checkout is still offering something to press",
      timeout: 30_000,
    })
    .toEqual([]);
  // Asked for by name as well, because the two things a declined checkout must never grow
  // back are a second chance to consent and a way to pay. `/^Pay/` and not the exact word
  // "Pay", which is what stood here: the payment panel names its control with the amount
  // ("Pay ₹579.95"), so the exact-word check passed over precisely the button whose
  // appearance on an ended checkout would matter.
  await expect(page.getByRole("button", { name: /^Approve to pay ₹/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^Pay/ })).toHaveCount(0);

  // The version is kept and shown, at the amount it was declined at. A trail that dropped
  // it would leave the screen unable to say what the buyer turned down.
  const after = await readCheckout(page, checkoutId);
  const declined = after.versions.find((entry) => entry.version === version);
  expect(declined, "the declined version vanished from the checkout").toBeDefined();
  await expect(page.getByRole("heading", { name: "Version trail" })).toBeVisible();
  await expect(page.getByText(`v${version}`, { exact: true })).toBeVisible();
  await expect(page.getByText(rupees(declined!.amount_minor), { exact: true }).first()).toBeVisible();
});

/*
 * The name says "ended through the app's proxy" rather than "cancelling", because no buyer
 * pressed anything here and a test name is a claim like any other. It used to read
 * "cancelling an admitted checkout ends it", written when the cancellation was
 * `getByRole("button", { name: "Cancel this order" }).click()` on a checkout resting at
 * APPROVED. One press now carries a version through APPROVED to EXECUTION_PENDING, and the
 * payment screen it comes to rest on draws no such control, so the press became a POST and
 * the name went on describing a gesture the test no longer makes.
 *
 * What is left is still worth a test, and it is what the name now claims: a checkout ended
 * underneath a live payment surface is followed by that surface going away, on its own,
 * with nobody reloading. The missing control is asserted as a gap inside
 * `cancelThroughTheApp` rather than described here, and it is in `appGaps`.
 */
test("a checkout ended while its payment screen is up — through the app's proxy, because that screen offers a buyer no control that ends one — takes the payment surface off screen", async ({
  page,
}) => {
  const checkoutId = await openCheckoutForMilk(page);
  await approveCurrentVersion(page);

  // The precondition, which used to be a Pay button waiting to be enabled on a checkout
  // resting at APPROVED. That checkout no longer exists: the press spent the approval, so
  // what is on screen is the payment surface for the grant it bought. Asserted as the panel
  // being drawn for this version and carrying its own Pay control, because the whole point
  // of the assertions below is that this surface goes away, and a surface that was never
  // there could not prove that.
  const admitted = await readCheckout(page, checkoutId);
  expect(admitted.state).toBe("EXECUTION_PENDING");
  await expect(
    page.getByRole("heading", { name: `Pay for version ${admitted.current_version}` }),
  ).toBeVisible();
  // Read as the whole control set, not as one button looked for: what the assertions after
  // the cancellation say is that this set empties, and a set nobody measured cannot be
  // shown to have emptied. One control, named with the amount — no voice control here,
  // because a spoken yes approves and never pays, and no way to end the order, which is the
  // gap `cancelThroughTheApp` asserts on its way past.
  expect(
    await screenControls(page),
    "the payment screen is not offering the one control it should",
  ).toEqual([expect.stringMatching(/^Pay ₹/)]);

  await cancelThroughTheApp(page, checkoutId);

  await expect
    .poll(async () => (await readCheckout(page, checkoutId)).state, { timeout: 30_000 })
    .toBe("CANCELLED");

  // The screen follows on its own, with nothing reloaded and nobody pressing anything. That
  // is the assertion, not a wait for one: while the provider order does not exist the panel
  // is re-reading the checkout every couple of seconds, and this is what catches a screen
  // that kept the payment surface up because the state changed underneath a branch that
  // never looked again.
  const spoken = await bannerSaying(page, "CANCELLED");
  expect(spoken).toContain("Cancelled");
  expect(spoken).toContain("nothing was charged");
  expect(spoken).toContain("hold on stock has been released");

  // The grant the approval bought was revoked when the kernel cancelled the version, and
  // the screen no longer offers to spend it. `/^Pay/` rather than the exact word: the
  // control on the payment surface is named "Pay ₹579.95", so an assertion against the bare
  // word would have passed over the very button this test exists to see disappear.
  await expect(page.getByRole("button", { name: /^Pay/ })).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: `Pay for version ${admitted.current_version}` }),
  ).toHaveCount(0);

  // And nothing has taken its place: the screen offers a buyer nothing at all to press.
  //
  // This line used to assert that no "Cancel this order" button was on screen, under the
  // heading that nothing offers to end an order that has already ended — and it had no
  // before-state. `checkout-journey` draws that control in its APPROVED branch and in its
  // terminal branch, and this walk stood on the payment panel, which draws neither, so the
  // count was zero before the cancellation as surely as after it and would have been zero
  // on a blank page. What is asserted instead is the set: one control a moment ago, named
  // Pay, and none now. The banner sentences above are this screen's positive control — the
  // page is alive and rendering the checkout the server calls ended.
  await expect
    .poll(async () => await screenControls(page), {
      message: "an ended checkout is still offering something to press",
      timeout: 30_000,
    })
    .toEqual([]);

  // And the server agrees about why there is nothing to press, which is the half of this a
  // screenshot could not settle: CANCELLED has no outgoing edge, so the read model computes
  // `cancellable: false`, and the journey draws its ending surface only for a checkout the
  // server still calls cancellable. A screen with no controls over a server that still
  // thought this order could be ended would look identical to this one and be a bug.
  const ended = await readEndability(page, checkoutId);
  expect(
    ended.cancellable,
    "the server still says an ended checkout can be ended, so the empty screen above is " +
      "hiding a live capability rather than reflecting a closed one",
  ).toBe(false);
});

test("an unrecognised state would render as itself, and the recognised ones never render as each other", async ({
  page,
}) => {
  // Three screens, three different sentences. Run as one test because the property is about
  // the set: it is not enough for each banner to be right on its own screen, they have to
  // be distinguishable, and a component that fell back to one sentence for everything
  // would pass three separate tests and fail this one.
  //
  // The middle screen used to be APPROVED's and is EXECUTION_PENDING's, because approving
  // admits in the same press. That is the harder version of this property rather than a
  // softer one: "Approved, not submitted" and "Admitted, order being created" are one press
  // apart and both are about a checkout nobody has paid for, so a screen that blurred them
  // would be telling a buyer their consent is still sitting on this machine when the kernel
  // is already holding it.
  const checkoutId = await openCheckoutForMilk(page);
  const waiting = await bannerOrCardText(page);

  await approveCurrentVersion(page);
  const admitted = await bannerSaying(page, "EXECUTION_PENDING");

  // Ended through the proxy rather than by a press, for the reason set out on
  // `cancelThroughTheApp`: after the approval there is no control on this route that ends
  // the order. That helper asserts the gap where it stands — the server calls this checkout
  // cancellable, the screen offers only Pay — so this test's third screen is reached past a
  // missing button that has been shown to be missing, rather than past an assumption. What
  // the screen does with the state afterwards is still the screen's own doing, which is
  // what this test reads.
  await cancelThroughTheApp(page, checkoutId);
  await expect
    .poll(async () => (await readCheckout(page, checkoutId)).state, { timeout: 30_000 })
    .toBe("CANCELLED");
  const cancelled = await bannerSaying(page, "CANCELLED");

  expect(admitted).not.toBe(cancelled);
  expect(admitted).not.toContain("Cancelled");
  expect(cancelled).not.toContain("Admitted, order being created");
  expect(waiting).not.toContain("Admitted, order being created");
  expect(waiting).not.toContain("Cancelled");

  // Each one carried the server's own spelling of the state alongside the English, which
  // is what lets somebody reading over the buyer's shoulder match the screen to the row.
  expect(admitted).toContain("EXECUTION_PENDING");
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
