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
// `Response` is aliased because `lib.dom` puts a different `Response` in scope: the one
// wanted here is the network response Playwright hands a `waitForResponse` predicate.
import { expect, type Locator, type Page, type Response as HttpResponse } from "@playwright/test";

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
 * A storefront route with the store's own header on it.
 *
 * `/` is the copilot now, and `components/header.tsx` returns null there on purpose --
 * the conversation draws its own header, and two would stack a second cart icon over the
 * first. So a walk that begins by searching has to begin on a storefront page, and the
 * search route is the least surprising one: it is where the search box sends you anyway.
 *
 * This is a navigation change, not a coverage change. Nothing was being asserted about
 * the home page here; what these helpers prove -- the card the index returned, the count
 * the server summed, the total the merchant priced, the card the kernel froze -- is
 * asserted below and unchanged. The copilot home has its own specs.
 */
const STOREFRONT_ENTRY = "/search";

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

/**
 * Land on the storefront with its header, and prove the search box is really there.
 *
 * The assertion is the point: without it a header that stopped rendering would surface
 * as a fill() timing out thirty seconds later inside whatever step happened to run first.
 */
export async function enterStorefront(page: Page): Promise<void> {
  await page.goto(STOREFRONT_ENTRY);
  await expect(visibleSearchBox(page)).toBeVisible({ timeout: SERVER_ROUND_TRIP });
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
 * next `POST /v1/carts/{id}/checkout` came back
 * `reservation_refused: no hold could be taken for version 1: CONCURRENT_OPERATION`.
 * From the browser that reads as a checkout that simply never opened — the failure looked
 * like a hung navigation and was really a suite competing with its own past runs.
 *
 * So the suite gives back what it can. Nothing about this is a workaround: a reservation
 * is scarce by design, and a test that walks away holding one is a test that has changed
 * the merchant it was measuring.
 */
const openedCheckouts: string[] = [];

/** What became of one checkout's stock hold when the suite tried to give it back. */
export interface HoldRelease {
  checkoutId: string;
  /** True only where the kernel actually ended the checkout and let the hold go. */
  released: boolean;
  /** The kernel's own words when it would not, or why the attempt never reached it. */
  detail: string;
}

/**
 * Ask the kernel to end every checkout this test opened, and report which holds actually
 * came back.
 *
 * This used to fire the cancellations and read none of the answers, which stopped being
 * honest the moment approving became one press. `POST .../cancel` is HTTP 200 whether the
 * kernel cancelled or refused, so a discarded response makes a refusal indistinguishable
 * from a release: every approving test now leaves its checkout admitted, and a checkout
 * whose payment attempt has moved past CREATED is one the kernel will not abandon. That is
 * the platform working — it is the same refusal this suite has a spec for — and it means a
 * fifteen-minute hold can survive the run. Silence about that reads to the next run as a
 * `reservation_refused` from nowhere, which is precisely the failure this cleanup exists to
 * prevent, so what could not be released is now named.
 *
 * It is a report and not an assertion, deliberately: it is called from `afterEach`, and
 * failing a passing test because the kernel correctly declined to cancel would be inventing
 * a bug out of the platform doing its job. Nothing here throws and nothing here asserts.
 * The caller gets the outcomes if it wants them, and anything still held is written to the
 * run's output whether or not anyone asked.
 *
 * A refused checkout is not queued for a second attempt. The refusal is a definite answer
 * about a hold that is now the expiry's business, and re-asking would only collect it
 * again. The proxy refuses a write that cannot prove it came from a page of this app, so
 * the `Origin` a browser would have sent is supplied explicitly.
 */
export async function releaseCheckouts(page: Page): Promise<HoldRelease[]> {
  const origin = appOrigin(page);
  const outcomes: HoldRelease[] = [];
  while (openedCheckouts.length > 0) {
    const id = openedCheckouts.pop();
    if (!id) continue;
    outcomes.push(await endCheckout(page, id, origin));
  }

  const held = outcomes.filter((outcome) => !outcome.released);
  if (held.length > 0) {
    // Warned rather than thrown, and warned every time rather than once: the cost is paid
    // by whatever runs next against these SKUs, which is usually a different file.
    console.warn(
      `\n  [e2e] ${held.length} of ${outcomes.length} checkout(s) opened by this test still ` +
        "hold merchant stock. Each hold expires on its own in about fifteen minutes; until it " +
        "does, the SKUs behind it have one fewer hold available to the rest of the suite, and " +
        "a later `reservation_refused: CONCURRENT_OPERATION` is this rather than a broken app. " +
        held.map((outcome) => `${outcome.checkoutId} — ${outcome.detail}`).join("; ") +
        "\n",
    );
  }
  return outcomes;
}

/**
 * One cancellation, and the kernel's own answer to it rather than the fact of having asked.
 *
 * Everything is caught and turned into an outcome. This runs after a test has already
 * finished, and a cleanup that threw would replace whatever the test found with a failure
 * about the tidying up.
 */
async function endCheckout(page: Page, id: string, origin: string): Promise<HoldRelease> {
  try {
    const response = await page.request.post(
      `/api/backend/v1/checkouts/${encodeURIComponent(id)}/cancel`,
      {
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": `e2e-cancel-${id}`,
          Origin: origin,
        },
        data: { reason: "e2e_cleanup" },
      },
    );
    if (!response.ok()) {
      return {
        checkoutId: id,
        released: false,
        detail: `the app's proxy answered ${response.status()}, so the kernel may never have been asked`,
      };
    }
    // `allowed` is absent from a successful cancellation and present-and-false on a refused
    // one, so the refusal is the case that is tested for; anything else on a 200 is a
    // checkout that ended. `from_state` is the kernel's own reading of where it was when it
    // said no, which is the fact that says whether this hold was worth expecting back.
    const verdict = (await response.json()) as {
      allowed?: boolean;
      code?: string;
      from_state?: string | null;
    };
    if (verdict.allowed === false) {
      return {
        checkoutId: id,
        released: false,
        detail:
          `the kernel refused to cancel it: ${verdict.code ?? "no code sent"}, read as ` +
          `${verdict.from_state ?? "an unstated state"} when it refused`,
      };
    }
    return { checkoutId: id, released: true, detail: "cancelled, and the hold is back" };
  } catch (cause) {
    return {
      checkoutId: id,
      released: false,
      detail: `the cancellation never got an answer: ${cause instanceof Error ? cause.message : String(cause)}`,
    };
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
 * The states a checkout has reached once its approval was accepted and spent, and the
 * title the state banner owes each of them.
 *
 * The button reads "Approve to pay" because the press is one act: it records the buyer's
 * consent and hands that exact version to the kernel in the same gesture. APPROVED is
 * passed through rather than rested on, so the card that says "Version N is approved" is
 * for a checkout approved somewhere else -- and a helper that waited for it waited for a
 * screen this walk never reaches. Which of the four a run finds is a race with the worker
 * spending the grant rather than a fact about the storefront, so all four are named and an
 * assertion says which one arrived instead of widening until it says nothing.
 *
 * The titles are copied out of `state-banner.tsx`, not imported from it. Importing would
 * make every assertion that uses them true by construction -- the screen checked against
 * the string it renders -- and the claim is that a buyer is told *this*, so a rewording
 * has to break a test rather than travel with it.
 *
 * The state spellings are the opposite case, and that is why the set below is exported.
 * They are the kernel's vocabulary, not this storefront's: every spec has to agree with
 * the kernel about them, and three specs keeping private copies is how a fifth admitted
 * state gets added to one of them and to none of the others.
 */
export const ADMITTED_BANNER_TITLES: Readonly<Record<string, string>> = {
  EXECUTION_PENDING: "Admitted, order being created",
  AWAITING_PAYMENT: "With Razorpay",
  INVALIDATED_AWAITING_PAYMENT_RESULT: "Superseded while a payment may be in flight",
  PAID: "Paid and recorded",
};

/**
 * The same four states, as the set every membership test here asks.
 *
 * Derived from the titles rather than written out a second time, so no state can be
 * admitted here without somebody having said what the buyer is shown when it arrives.
 */
export const ADMITTED_STATES: ReadonlySet<string> = new Set(Object.keys(ADMITTED_BANNER_TITLES));

/**
 * The state banner, told apart from the spinners that share its role.
 *
 * `Spinner` is a `role="status"` carrying an aria-label and no text at all -- correctly, it
 * is a live region announcing "Loading" -- and `Button` draws one inside itself while a
 * press is in flight, while `PaymentPanel` draws one on its own while it reads the payment
 * handoff. So the first `[role="status"]` on a checkout screen is routinely a spinner, and
 * "a status element is visible" is satisfied by a screen that has said nothing yet.
 *
 * The banner is the status that carries the server's own spelling of the state in a
 * `<code>`, and that is what selects it. Exported because the same trick was being
 * hand-rolled in a spec, and a selector that encodes an app fact is worth stating once.
 */
export function stateBanner(page: Page): Locator {
  return page.getByRole("status").filter({ has: page.locator("code") });
}

/**
 * Search for `doodh`, add the milk, and land on a checkout showing version 1's card.
 *
 * Every step waits on something only a server could have produced -- the card the search
 * index returned, the cart count the provider summed from a cart the API sent, the
 * priced total on the cart page, the approval card the merchant froze. None of those
 * appear before a response has come back, which is what makes this walk a test of the
 * platform rather than of the browser's optimism. Where the screen deliberately runs
 * ahead of the server, that is called out at the line rather than trusted.
 */
export async function openCheckoutForMilk(page: Page): Promise<string> {
  await enterStorefront(page);

  await visibleSearchBox(page).fill("doodh");
  await visibleSearchBox(page).press("Enter");
  // `toHaveURL` rather than `waitForURL`: every navigation from here is a client-side
  // push, and waiting on a `load` event that a soft navigation never fires again turns a
  // slow response into a bare "timed out waiting for navigation" with nothing in it.
  await expect(page).toHaveURL(/\/search\?q=doodh/);

  const add = page.getByRole("button", { name: `Add ${MILK_NAME} to cart` });
  await expect(add).toBeVisible({ timeout: SERVER_ROUND_TRIP });
  await add.click();

  // The stepper is NOT the signal. Quantity is the one number this storefront lets the
  // browser have an opinion about — `useCart` publishes the wanted count optimistically
  // and dims the money until the server answers — so "1 in cart" appears before
  // `POST /v1/carts` has even been sent. Navigating on it aborted the write in flight
  // and landed on an empty cart, intermittently and for no visible reason.
  //
  // The cart pill's count is the honest one: `itemCount` is only ever published by the
  // provider's `refresh`, which sums the quantities on a cart the server returned. So
  // this line waits for a round trip, and the one above only for the intent.
  await expect(page.getByLabel("1 in cart").first()).toBeVisible({ timeout: SERVER_ROUND_TRIP });
  await expect(page.getByRole("link", { name: "My cart, 1 item" })).toBeVisible({
    timeout: SERVER_ROUND_TRIP,
  });

  await page.goto("/cart");
  // The cart read has to have landed before the button means anything: with no cart
  // this page renders its empty state, and asserting on a control that is simply
  // not there reports a missing element rather than the read that did not arrive.
  await expect(page.getByRole("heading", { name: "Your cart" })).toBeVisible({
    timeout: SERVER_ROUND_TRIP,
  });
  const proceed = page.getByRole("button", { name: "Proceed to checkout" });
  await expect(proceed).toBeEnabled({ timeout: SERVER_ROUND_TRIP });
  await proceed.click();

  // A checkout the merchant declined to open renders as a notice and no navigation. Said
  // out loud here, because the alternative is a URL timeout that names nothing.
  await expect(
    page.getByText("Checkout could not be opened"),
    "the merchant refused to open a checkout for this cart",
  ).toHaveCount(0);
  await expect(page).toHaveURL(/\/checkout\/[^/?#]+/, { timeout: SERVER_ROUND_TRIP });
  await expect(page.getByRole("heading", { name: "Approve this order" })).toBeVisible({
    timeout: SERVER_ROUND_TRIP,
  });

  const checkoutId = checkoutIdFrom(page.url());
  openedCheckouts.push(checkoutId);
  return checkoutId;
}

export async function approveCurrentVersion(page: Page): Promise<void> {
  const approving = checkoutIdFrom(page.url());
  await page.getByRole("button", { name: /^Approve to pay ₹/ }).click();

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
  const refused = page.locator("#main").getByRole("alert");
  let outcome = "";
  try {
    // The server is the arbiter, not the screen. A heading can render before a write has
    // landed and can survive one that failed; the checkout's state cannot. The refusal is
    // still read off the page, because the reason is a sentence the card owns and the
    // state alone would not say which of several reasons applied.
    await expect
      .poll(
        async () => {
          if (await refused.first().isVisible()) return (outcome = "refused");
          const read = await readCheckout(page, approving);
          return (outcome = ADMITTED_STATES.has(read.state) ? "admitted" : read.state);
        },
        { timeout: SERVER_ROUND_TRIP },
      )
      .toMatch(/^(admitted|refused)$/);
  } catch {
    // Neither outcome arrived, which is the one case the two-way wait above cannot
    // explain on its own. Ask the three questions that separate the possibilities --
    // did the press register, did the server act, and where is the checkout now -- and
    // put the answers in the failure, because a second identical timeout would teach
    // nobody anything a first one had not.
    throw new Error(await stuckApproving(page));
  }
  if (outcome === "refused") {
    throw new Error(
      "the approval was not accepted, and the card said why: " +
        (await refused.first().innerText()).replace(/\s+/g, " ") +
        " — this is what a merchant state change between opening a checkout and approving " +
        "it looks like from the browser.",
    );
  }

  /*
   * And now the screen's own account of where the checkout is, which is a separate claim
   * from the server's and the one this helper exists to leave standing.
   *
   * No Pay button is waited for. Pay belongs to a checkout resting at APPROVED, and this
   * press already spent the approval -- the kernel issued its grant and the worker is
   * creating the provider order. Waiting for a control the flow has moved past is what
   * reported five successful approvals as failures.
   *
   * What replaced it was `[role="status"]` being visible, and that proved nothing: the
   * payment surface renders a bare spinner while it reads its handoff, `Spinner` *is* a
   * `role="status"` with no text, so the first status on screen is a spinner at exactly the
   * moment this line runs. It passed on a screen that had not yet said a word about the
   * checkout, and it would have gone on passing if the panel had never rendered the state
   * at all.
   *
   * So both halves of the banner are required. The code alone is not enough either:
   * `stateMeaning` falls back to echoing an unrecognised state as its own title, so a state
   * this storefront has no words for renders code and title identically and a check on the
   * code would call that a pass. The banner has to name a state only an admitted approval
   * reaches AND carry that state's own title.
   *
   * Read as one poll rather than two assertions because the worker moves the checkout
   * EXECUTION_PENDING -> AWAITING_PAYMENT underneath this: a title compared against a code
   * read a moment earlier would fail on a screen that was telling the truth both times.
   */
  const banner = stateBanner(page);
  await expect
    .poll(
      async () => {
        // `count`, `allTextContents` and the `count` below all answer from the current DOM
        // without waiting, so the poll owns the timeout and one slow read cannot eat it.
        if ((await banner.count()) === 0) return "no state banner is on screen";
        const named = ((await banner.locator("code").allTextContents())[0] ?? "").trim();
        if (!ADMITTED_STATES.has(named)) return `the banner names ${named || "no state at all"}`;
        const title = ADMITTED_BANNER_TITLES[named];
        const said = await banner.getByText(title, { exact: true }).count();
        return said === 1 ? "admitted" : `the banner names ${named} but does not say "${title}"`;
      },
      {
        timeout: SERVER_ROUND_TRIP,
        message:
          "the server admitted this approval and the screen never said so: the state banner " +
          "has to name a state only an admitted approval reaches and carry that state's own " +
          "words. `no state banner is on screen` means the approval card is still up — that " +
          "screen draws no banner — or the payment panel never got past its handoff read; " +
          "either way it is the storefront failing to follow a write it made",
      },
    )
    .toBe("admitted");
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

  const add = page.getByRole("button", { name: `Add ${productName} to cart` });
  await expect(add).toBeVisible({ timeout: SERVER_ROUND_TRIP });
  await add.click();

  const pill =
    expectedCount === 1 ? "My cart, 1 item" : `My cart, ${expectedCount} items`;
  await expect(page.getByRole("link", { name: pill })).toBeVisible({ timeout: SERVER_ROUND_TRIP });
}

/**
 * A checkout over two products, so a refusal can be asked what it says about a cart
 * where only one line moved.
 *
 * The kernel's answer to that turns out to be one `total` delta rather than a delta per
 * line, which is precisely why a spec has to be written against it: an assertion that
 * expected a row per changed product would have been asserting a design nobody built.
 */
export async function openCheckoutForTwoProducts(page: Page): Promise<string> {
  await enterStorefront(page);
  await addProduct(page, "doodh", MILK_NAME, 1);
  await addProduct(page, "basmati", RICE_NAME, 2);

  await page.goto("/cart");
  await expect(page.getByRole("heading", { name: "Your cart" })).toBeVisible({
    timeout: SERVER_ROUND_TRIP,
  });
  const proceed = page.getByRole("button", { name: "Proceed to checkout" });
  await expect(proceed).toBeEnabled({ timeout: SERVER_ROUND_TRIP });
  await proceed.click();

  await expect(
    page.getByText("Checkout could not be opened"),
    "the merchant refused to open a checkout for this cart",
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
  // `exact`, because this is the bare "Pay" the APPROVED screen draws. The payment panel's
  // control is "Pay ₹X" -- a different button on a different screen -- and a loose name
  // would match whichever of them happened to be there.
  await page.getByRole("button", { name: "Pay", exact: true }).click();
  return decisionFrom(await submission, "submit");
}

/**
 * Press "Approve to pay" and keep the answer the kernel gave that press.
 *
 * The refusal a buyer has to be told about arrives here now. It used to arrive from Pay --
 * approve, then submit, two requests with a window between them -- and approving and
 * admitting are one transaction now, so the press that can be refused is this one and
 * `POST .../versions/{n}/approve-and-pay` is the response carrying the kernel's reasons. A
 * wait armed for `submit` waits for a request nobody makes: thirty seconds of nothing, and
 * not a word about the refusal already rendered on screen.
 *
 * It lives here rather than in a spec because three of them want it. Same reasoning as
 * `payAndCaptureDecision` above, including the reason this is a capture rather than a click
 * followed by a re-read: the response body is the only evidence of what the server actually
 * sent this browser, and reading the checkout back afterwards gives the read model's
 * recomputation of the same comparison, which is a second opinion. The card renders
 * `decision.deltas`, so a spec checking the read model's list could pass while the card
 * dropped a row the kernel had really sent.
 *
 * The decision is returned unjudged, because a refusal is a normal answer to this press and
 * is the whole subject of the refusal specs. `approveCurrentVersion` is the helper for the
 * other case: it presses the same button and insists the approval was admitted.
 *
 * Nothing is intercepted or substituted. The response is observed on its way past.
 */
export async function approveAndCaptureDecision(page: Page): Promise<BrowserDecision> {
  const decided = page.waitForResponse(
    (response) =>
      /\/api\/backend\/v1\/checkouts\/[^/]+\/versions\/\d+\/approve-and-pay$/.test(
        new URL(response.url()).pathname,
      ) && response.request().method() === "POST",
    { timeout: SERVER_ROUND_TRIP },
  );
  await page.getByRole("button", { name: /^Approve to pay ₹/ }).click();
  return decisionFrom(await decided, "approve-and-pay");
}

/**
 * The kernel's verdict, read off the response that carried it.
 *
 * Shared by both captures because the one thing true of every decision this platform makes
 * is true of both routes: HTTP 200 whether it admitted or refused (ADR 0003 D15). So the
 * status is asserted to be 200 -- a 4xx or 5xx here is a broken route, not a refusal -- and
 * the verdict is read out of the body.
 *
 * `allowed` is checked for being a boolean rather than assumed. `SubmitResultSchema`
 * requires it, and that is exactly why its absence has to be caught at the boundary: every
 * caller branches on it, `undefined` is falsy, and a body that had lost the field would be
 * read by all of them as a refusal the kernel never made -- a refusal spec passing on
 * evidence of the opposite of what it claims.
 */
async function decisionFrom(response: HttpResponse, route: string): Promise<BrowserDecision> {
  expect(
    response.status(),
    `a kernel decision is HTTP 200 whether it admitted or refused (ADR 0003 D15); ${route} answered`,
  ).toBe(200);
  const decision = (await response.json()) as BrowserDecision;
  expect(
    typeof decision.allowed,
    `${route} answered 200 with no boolean \`allowed\`, so there is no verdict in it to read`,
  ).toBe("boolean");
  return decision;
}

/**
 * Why an approval produced neither a confirmation nor an error.
 *
 * Two facts, reported as facts. `aria-busy` says whether a press is in flight *now*; it
 * does not say whether one ever happened, because the journey clears it in a `finally`, so
 * an unset value covers both "never pressed" and "pressed and finished". An earlier version
 * of this helper drew the first conclusion from it and was wrong the first time it fired --
 * it announced a hydration race on a run where the server had recorded the approval, which
 * only a press that reached React could have produced.
 *
 * The server's own view of the checkout is the fact that settles it, so the two are
 * reported side by side and the reading is left to whoever is looking. The combination that
 * matters is a checkout the server calls APPROVED underneath a card still asking to
 * approve it: that is the screen failing to follow a write it made, and it is worth more
 * than any guess this function could offer about why.
 *
 * Everything here is best effort and nothing here asserts. It runs only on a path that has
 * already failed, and a diagnostic that could itself throw would replace the failure being
 * explained with one about the explaining.
 */
async function stuckApproving(page: Page): Promise<string> {
  const facts: string[] = [
    "the approval produced neither a confirmation nor an error within " +
      `${SERVER_ROUND_TRIP / 1000}s`,
  ];

  // Asked first, because it separates stuck from merely slow and the answer decays: the
  // page keeps rendering while this function runs, so a heading that appears now but not
  // before the deadline means the budget was too tight, not that the screen never followed.
  try {
    const arrived = await page
      .getByRole("heading", { name: /^Version \d+ is approved$/ })
      .first()
      .isVisible();
    facts.push(
      arrived
        ? "the confirmation is on screen NOW, moments after the deadline — this run was slow rather than stuck, and the budget is what to question"
        : "the confirmation is still not on screen",
    );
  } catch {
    facts.push("the confirmation could not be looked for");
  }

  // The decision the platform accepted but this screen could not read back. It is the
  // journey's own third outcome and it is the honest one, so finding it here is the
  // storefront working rather than failing -- but it still means the read never converged.
  try {
    if (await page.getByText("Your decision was recorded").first().isVisible()) {
      facts.push(
        "the screen is showing 'your decision was recorded, but this page has not been able " +
          "to read it back yet' — the bounded confirmation ran out, which is the storefront " +
          "telling the truth about a read that did not converge",
      );
    }
  } catch {
    // Older builds have no such notice; its absence is not a fact worth reporting.
  }

  try {
    const button = page.getByRole("button", { name: /^Approve to pay ₹/ }).first();
    if ((await button.count()) === 0) {
      facts.push("the approve button is no longer on the page");
    } else {
      const busy = await button.getAttribute("aria-busy");
      const disabled = await button.isDisabled();
      facts.push(
        `the approve button is still on screen, aria-busy=${busy ?? "unset"}, disabled=${disabled}` +
          (busy !== null || disabled
            ? " — so a press is still in flight and the round trip is what did not finish"
            : " — so nothing is in flight, which means either the press never reached React or it completed and the screen did not move on"),
      );
    }
  } catch {
    facts.push("the approve button could not be inspected");
  }

  try {
    const id = /\/checkout\/([^/?#]+)/.exec(page.url())?.[1];
    if (id) {
      const response = await page.request.get(
        `/api/backend/v1/checkouts/${encodeURIComponent(decodeURIComponent(id))}`,
      );
      if (response.ok()) {
        const body = (await response.json()) as CheckoutRead;
        facts.push(
          `the server says this checkout is ${body.state} at version ${body.current_version}` +
            (ADMITTED_STATES.has(body.state) || body.state === "APPROVED"
              ? " — the approval WAS recorded and, past APPROVED, spent: the press reached the kernel and the screen simply did not follow the write it made; this is the storefront's bug, not the platform's"
              : " — so nothing was approved and the press did not reach the server"),
        );
      } else {
        facts.push(`reading the checkout back answered ${response.status()}`);
      }
    }
  } catch {
    facts.push("the checkout could not be read back");
  }

  return facts.join("; ");
}
