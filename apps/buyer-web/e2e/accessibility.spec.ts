/**
 * Specification 29.8, against the running storefront.
 *
 * The claim this file tests is narrow and consequential: **a buyer who cannot use a mouse
 * and cannot see colour must be able to buy something, and must be told when their
 * approval is refused.** Everything below is a way of asking that question of the real
 * screens rather than of a component in isolation.
 *
 * Four properties, in the order they would hurt:
 *
 *  1. **The purchase journey works from the keyboard alone.** Not "the buttons are
 *     focusable" — the whole walk, search to approval, driven only by `Tab` and `Enter`.
 *     A control that can be reached but not operated, or that is skipped by the tab order,
 *     fails here and cannot fail anywhere else.
 *  2. **The changes a buyer must notice are announced.** A payment settles under a person
 *     who is not looking at the pixel that changed. The refusal is `assertive` because it
 *     interrupts; the state banner is `polite` because it accompanies.
 *  3. **Focus is visible.** A keyboard user who cannot see where they are is not being
 *     given a keyboard interface, they are being given a maze.
 *  4. **Colour is never the only signal.** The refusal is the case that matters. If the
 *     only thing separating "approved" from "refused" were a red bar, a buyer who cannot
 *     see red would press Pay again on an order the kernel had already declined. So the
 *     assertions below strip the colour and ask what the text alone says.
 *
 * An axe-core audit was run across the home page, search, a product, the basket, orders,
 * an approval card and a refusal, at both viewports. It found exactly two rule families,
 * and this file's relationship to each is deliberate:
 *
 *  - `scrollable-region-focusable` on the delta table's horizontal scroller. That is a
 *    real defect, it is on the refusal screen, and at 390px it is unconditional: the
 *    table is `min-w-[520px]` inside a viewport 390px wide, so a keyboard-only buyer
 *    could not scroll to the columns that say what changed. It was fixed in
 *    `delta-table.tsx` and is asserted here.
 *  - `color-contrast`, on four design tokens rather than on any component: `--ink-4`
 *    (#828282, 3.74–3.84:1 against the page grounds), `--ink-5` (#999999, 2.77–2.84:1),
 *    `--red` (#e2464c, 4.02:1 on white and 3.68:1 on the red-50 badge ground) and
 *    `--yellow` (#f8cb46, 1.54:1). All four fall short of 4.5:1 at the sizes they are
 *    used. Those live in `src/app/globals.css` and changing them restyles every screen of
 *    the storefront, which is a design decision and not a test's to take, so they are
 *    reported rather than altered. What is asserted here instead is the mitigation that
 *    has to hold whatever the palette does: the load-bearing sentences clear AA on their
 *    own, and nothing anywhere depends on colour to be understood.
 */
import { expect, test, type Locator, type Page } from "@playwright/test";

import {
  type BrowserDecision,
  MILK_NAME,
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
} from "./live-api";

let reachable = false;
let token = "";
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
  await releaseCheckouts(page);
});

test.afterAll(async ({ request }) => {
  if (reachable && priceOnEntry !== null) {
    await restorePrice(request, token, MILK_SKU, priceOnEntry);
  }
});

/* --------------------------------------------------------------- the keyboard */

/** How the focused element identifies itself, the way a screen reader would take it. */
interface Focused {
  tag: string;
  name: string;
  disabled: boolean;
}

async function focused(page: Page): Promise<Focused> {
  return page.evaluate(() => {
    const element = document.activeElement as HTMLElement | null;
    if (!element) return { tag: "none", name: "", disabled: false };
    const name =
      element.getAttribute("aria-label") ??
      (element as HTMLInputElement).placeholder ??
      (element.textContent ?? "").trim().replace(/\s+/g, " ");
    return {
      tag: element.tagName.toLowerCase(),
      name,
      disabled: (element as HTMLButtonElement).disabled === true,
    };
  });
}

/**
 * Tab forward until the focused control is the one wanted, or say what was never reached.
 *
 * Bounded, and the bound is the point. An unbounded loop on a page whose tab order does
 * not contain the control spins until the test times out and reports a timeout, which
 * names nothing; this reports the control that could not be reached and the last twelve
 * stops the keyboard actually visited, which is enough to see whether the control is
 * missing from the order or merely further down it.
 */
async function tabTo(page: Page, wanted: RegExp, description: string, limit = 60): Promise<void> {
  const visited: string[] = [];
  for (let press = 0; press < limit; press += 1) {
    await page.keyboard.press("Tab");
    const where = await focused(page);
    visited.push(`${where.tag}:${where.name.slice(0, 40)}${where.disabled ? " (disabled)" : ""}`);
    if (wanted.test(where.name) && !where.disabled) return;
  }
  throw new Error(
    `${limit} presses of Tab never reached ${description}. The keyboard visited: ${visited
      .slice(-12)
      .join(" -> ")}`,
  );
}

/* ----------------------------------- what one press of "Approve to pay" leaves behind */

/**
 * The states one press of "Approve to pay" can leave a checkout in, and the words the
 * state banner owes each of them.
 *
 * Approving is one act. The press records the buyer's consent **and** hands that exact
 * version to the kernel in the same gesture, so a checkout goes APPROVAL_REQUIRED ->
 * APPROVED -> EXECUTION_PENDING without ever resting at APPROVED. `EXECUTION_PENDING` is
 * where the press lands; `AWAITING_PAYMENT` is where a worker spending the grant has
 * already carried it by the time an assertion runs. Which of the two a run sees is a race
 * with that worker rather than a fact about the storefront, so both are spelled out here
 * and an assertion names whichever arrived instead of widening until it says nothing.
 *
 * The sentences are copied out of `state-banner.tsx`, not imported from it. Importing
 * would make every assertion below true by construction — the screen checked against the
 * string it renders — and the claim is that a buyer is told *these* things, so a rewording
 * has to break a test rather than travel with it.
 */
const ADMITTED: Readonly<Record<string, { title: string; money: string }>> = {
  EXECUTION_PENDING: {
    title: "Admitted, order being created",
    money: "Nothing has been charged.",
  },
  AWAITING_PAYMENT: {
    title: "With Razorpay",
    money: "Whether any money has moved is not something this page can tell you",
  },
};

/** The same states as a pattern, so a polled read reports the state it actually found. */
const ADMITTED_STATE = new RegExp(`^(${Object.keys(ADMITTED).join("|")})$`);

/** The checkout in the address bar, or a failure saying the walk never got there. */
function checkoutIdOf(page: Page): string {
  const match = /\/checkout\/([^/?#]+)/.exec(page.url());
  if (!match) throw new Error(`not on a checkout route: ${page.url()}`);
  return decodeURIComponent(match[1]);
}

/**
 * The state banner, told apart from the spinners.
 *
 * `Spinner` is itself a `role="status"` — correctly, it is a live region that announces
 * "Loading" — and the payment surface draws one while it reads its handoff, so "the first
 * status on the page" is not reliably the banner. The banner is the status that carries
 * the server's own spelling of the state in a `<code>`, and that is what selects it.
 */
function stateBanner(page: Page): Locator {
  return page.getByRole("status").filter({ has: page.locator("code") }).first();
}

/** The banner as a screen reader would take it: the state it names, and everything it says. */
async function admittedBanner(
  page: Page,
): Promise<{ banner: Locator; state: string; spoken: string }> {
  const banner = stateBanner(page);
  // The panel reads its handoff from the server before it can draw anything, so this is a
  // round trip rather than a render.
  await expect(banner).toBeVisible({ timeout: 30_000 });
  const state = (await banner.locator("code").first().innerText()).trim();
  return { banner, state, spoken: (await banner.innerText()).replace(/\s+/g, " ") };
}

/**
 * Press "Approve to pay" and keep the answer that press was given.
 *
 * The refusal a buyer has to be told about arrives here now. It used to arrive from Pay —
 * approve, then submit, two requests with a window between them — and approving and
 * admitting are one transaction now, so the press that can be refused is this one and
 * `POST .../approve-and-pay` is the response carrying the kernel's reasons. It answers
 * HTTP 200 whether it admitted or refused (ADR 0003 D15), so the status is asserted and
 * the body is read rather than inferred from the screen.
 *
 * Nothing is intercepted or substituted; the response is observed on its way past. It
 * lives here rather than in `journey.ts` because that file is shared and owned centrally.
 */
async function approveAndCaptureDecision(page: Page): Promise<BrowserDecision> {
  const decided = page.waitForResponse(
    (response) =>
      /\/api\/backend\/v1\/checkouts\/[^/]+\/versions\/\d+\/approve-and-pay$/.test(
        new URL(response.url()).pathname,
      ) && response.request().method() === "POST",
    { timeout: 30_000 },
  );
  await page.getByRole("button", { name: /^Approve to pay ₹/ }).click();
  const response = await decided;
  expect(
    response.status(),
    "a kernel decision is HTTP 200 whether it admitted or refused (ADR 0003 D15)",
  ).toBe(200);
  return (await response.json()) as BrowserDecision;
}

/* ------------------------------------------------- proving a walk used no mouse at all */

/**
 * Every trusted pointer press this browser makes, counted across the whole walk.
 *
 * `isTrusted` is the point: it is true only of an event the user agent generated, which is
 * what a `.click()` produces and what a script dispatching its own event never can. The
 * count lives in `sessionStorage` because the walk crosses four documents and a counter on
 * `window` would start again at each one.
 *
 * The zero is written at install time, and that is not bookkeeping. An instrumentation that
 * never installed — an init script that did not run, a browser that refused storage — reads
 * exactly like a walk that never touched a mouse, and a reader that answered `0` to both
 * would make the "no mouse was pressed" assertion unfalsifiable: it would pass hardest
 * precisely when it was measuring nothing. With the zero written on install, a missing key
 * means the counter is absent, `trustedMousePresses` says `null` rather than `0`, and the
 * test refuses to read an absence as evidence.
 */
const MOUSE_PRESSES = "e2e-trusted-mouse-presses";

async function countMousePresses(page: Page): Promise<void> {
  await page.addInitScript((key) => {
    try {
      // Only on the first document of the walk: the later ones must not reset a count.
      if (sessionStorage.getItem(key) === null) sessionStorage.setItem(key, "0");
    } catch {
      // Storage a browser will not give out is not this counter's argument to have — and
      // leaving the key absent is what lets the test tell that apart from a quiet zero.
    }
    window.addEventListener(
      "mousedown",
      (event) => {
        if (!event.isTrusted) return;
        try {
          sessionStorage.setItem(key, String(Number(sessionStorage.getItem(key) ?? "0") + 1));
        } catch {
          // As above. An unwritable store leaves the key exactly as the install found it.
        }
      },
      true,
    );
  }, MOUSE_PRESSES);
}

/** The count, or `null` where there is no instrumentation to have counted anything. */
async function trustedMousePresses(page: Page): Promise<number | null> {
  return page.evaluate((key) => {
    try {
      const raw = sessionStorage.getItem(key);
      return raw === null ? null : Number(raw);
    } catch {
      return null;
    }
  }, MOUSE_PRESSES);
}

/**
 * Wait, bounded, for a control to become operable, and say whether it did.
 *
 * Used where that is a race with a background worker rather than a fact about the
 * storefront, and where both outcomes are real screens a buyer can be sitting in front of:
 * an assertion here would be an assertion about how fast a worker happened to be. A caller
 * may ignore the answer and read the control itself afterwards, which is what keeps a
 * branch and the evidence it asserts on describing the same instant.
 */
async function becomesEnabled(page: Page, control: Locator, budgetMs: number): Promise<boolean> {
  const deadline = Date.now() + budgetMs;
  for (;;) {
    if (await control.isEnabled()) return true;
    if (Date.now() >= deadline) return false;
    await page.waitForTimeout(250);
  }
}

test("a buyer can search, add, open a checkout and approve it with no mouse at all", async ({
  page,
}) => {
  // Installed before the first navigation, because what it counts is every press in the
  // walk and not merely the ones on the last document.
  await countMousePresses(page);

  // The home route is the copilot and draws no header, so the store's search box -- what
  // this walk is about -- lives on the storefront's own pages. The copilot's own keyboard
  // reachability is asserted first, so moving this walk does not quietly drop the home
  // route out of the keyboard suite.
  await page.goto("/");
  const composer = page.locator("#copilot-composer");
  await expect(composer).toBeVisible({ timeout: 30_000 });
  await composer.focus();
  await expect(composer).toBeFocused();

  await page.goto("/search");
  await expect(page.locator('input[aria-label="Search for products"]:visible').first()).toBeVisible({
    timeout: 30_000,
  });

  // The first stop is the skip link, which is what makes the rest of the header optional
  // rather than compulsory for somebody arriving on every page of a store.
  await page.keyboard.press("Tab");
  expect((await focused(page)).name).toBe("Skip to content");

  await tabTo(page, /^Search for products$/, "the header search box");
  await page.keyboard.type("doodh");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/search\?q=doodh/);
  await expect(page.getByRole("heading", { name: /Results for/ })).toBeVisible({
    timeout: 30_000,
  });

  // Adding, from the keyboard, on the card the search returned.
  const add = page.getByRole("button", { name: `Add ${MILK_NAME} to cart` });
  await expect(add).toBeVisible({ timeout: 30_000 });
  await tabTo(page, new RegExp(`^Add ${escapeForRegExp(MILK_NAME)} to cart$`), "the add button");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("link", { name: "My cart, 1 item" })).toBeVisible({
    timeout: 30_000,
  });

  // And the cart, from the keyboard, rather than by typing an address.
  await page.goto("/search?q=doodh");
  await expect(page.getByRole("heading", { name: /Results for/ })).toBeVisible({
    timeout: 30_000,
  });
  await tabTo(page, /^My cart, 1 item$/, "the cart link in the header");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/basket/);
  await expect(page.getByRole("heading", { name: "Your cart" })).toBeVisible({
    timeout: 30_000,
  });

  await expect(page.getByRole("button", { name: "Proceed to checkout" })).toBeEnabled({
    timeout: 30_000,
  });
  await tabTo(page, /^Proceed to checkout$/, "the checkout button");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/checkout\/[^/?#]+/, { timeout: 30_000 });
  await expect(page.getByRole("heading", { name: "Approve this order" })).toBeVisible({
    timeout: 30_000,
  });

  // The approval itself. This is the one press in the journey that a machine is not
  // allowed to make, so it is the one press that most has to work from a keyboard.
  await expect(page.getByRole("button", { name: /^Approve to pay ₹/ })).toBeEnabled({ timeout: 30_000 });
  await tabTo(page, /^Approve to pay ₹/, "the approve button on the trusted surface");
  const approving = checkoutIdOf(page);
  await page.keyboard.press("Enter");

  /*
   * One press, and the journey is past its approval rather than resting on it.
   *
   * "Version N is approved", and the Pay button underneath it, belonged to a checkout left
   * sitting at APPROVED for a second press to spend. There is no such pause now: this press
   * records the consent and hands the version to the kernel in one act, so the walk goes
   * APPROVAL_REQUIRED -> APPROVED -> EXECUTION_PENDING and never stops on the screen those
   * two assertions were reading.
   *
   * So what proves the keyboard *operated* the control is the server, not a heading. A
   * heading can render on a write that failed; a state the kernel wrote cannot, and the
   * only thing that could have moved this checkout out of APPROVAL_REQUIRED is the Enter
   * pressed above.
   */
  await expect
    .poll(async () => (await readCheckout(page, approving)).state, { timeout: 30_000 })
    .toMatch(ADMITTED_STATE);

  // And the screen followed the write it made, in words rather than in a spinner.
  const admitted = await admittedBanner(page);
  expect(
    ADMITTED[admitted.state],
    `the banner names ${admitted.state}, which is not a state one press of "Approve to pay" reaches`,
  ).toBeDefined();
  expect(admitted.spoken).toContain(ADMITTED[admitted.state].title);

  // The next surface the money is on, offered by the store rather than by the agent. It is
  // where the Pay button lives now, so the journey still ends on a control the buyer can
  // see; the approval that would have armed it is gone from the page rather than left
  // sitting there disabled, which is what stops a keyboard user tabbing back to a consent
  // that has already been spent.
  await expect(page.getByLabel("You are paying this. RazorAI cannot.")).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByRole("button", { name: /^Approve to pay ₹/ })).toHaveCount(0);

  /* ------------------------- and the keyboard can still reach the control that is next */

  /*
   * `tabTo(page, /^Pay$/)` used to end this walk, on the APPROVED screen where Pay was the
   * control the approval had just unlocked. That screen is gone — one press spends the
   * approval — and the claim went with it, which left the file whose entire subject is
   * keyboard operability ending its keyboard walk on a visibility check. It is made again
   * here, against the control that replaced it.
   *
   * "Pay ₹X" is `disabled` until a worker has created the provider order
   * (`payment-panel.tsx`), and no browser puts a disabled button in its tab order. So which
   * claim is honest depends on which of two real screens is up when the walk arrives, and
   * both are asserted rather than whichever one happens to be convenient. What is never
   * allowed is the third possibility — no pay control at all — so that is ruled out first,
   * before either branch can be entered.
   */
  const pay = page.getByRole("button", { name: /^Pay ₹/ });
  await expect(
    pay,
    "the admitted screen drew no pay control, so there is nothing here for a keyboard to reach",
  ).toHaveCount(1);

  // Wait for the worker, bounded, and then read the control once. The branch and the facts
  // it asserts have to come from that one read: the worker can finish between any two, and
  // a test that decided on the first read and asserted on the second would fail on the
  // storefront doing exactly the right thing.
  await becomesEnabled(page, pay, 12_000);
  const seat = await pay.evaluate((element) => {
    const button = element as HTMLButtonElement;
    const style = getComputedStyle(button);
    return {
      tabIndex: button.tabIndex,
      disabled: button.disabled,
      keyboardShutOut: button.closest('[aria-hidden="true"], [inert]') !== null,
      painted: style.display !== "none" && style.visibility !== "hidden",
      // The panel's own sentence for the one reason this control is allowed to be dark,
      // read from the surface the control sits on and in the same instant as its state.
      saysWhyItIsDark: (button.closest("section")?.textContent ?? "").includes(
        "waiting for the provider order to exist",
      ),
    };
  });

  if (!seat.disabled) {
    // The provider order exists, so the control is live and the whole claim is available
    // unqualified: the buyer reaches the control that moves their money with Tab alone.
    // `tabTo` refuses a disabled stop, so reaching it is reaching it operable.
    await tabTo(page, /^Pay ₹/, "the pay control on the admitted screen");
  } else {
    /*
     * The worker has not finished, so the control is honestly dark, and demanding it as a
     * tab stop would be demanding a race be won. Asserted instead is every part of
     * reachability that does not depend on the worker, which is not nothing: the control
     * keeps its seat in the tab sequence (`tabIndex` 0 — nothing pushed it out with -1),
     * the keyboard is not shut out of the container it sits in (`inert`, `aria-hidden`),
     * it is painted rather than `display: none`, and the surface says in words why it
     * cannot be pressed — so a keyboard user is waiting rather than stranded, and a button
     * dark for any other reason fails here rather than passing.
     *
     * Every one of those is a claim about one button, and none of them would notice a
     * screen whose tab order was dead. So the tab order of this very screen is then walked,
     * by Tab, to a control that IS operable on it.
     */
    expect(
      seat.tabIndex,
      `the pay control was taken out of the tab sequence with tabindex=${seat.tabIndex}, so enabling it would not make it reachable`,
    ).toBe(0);
    expect(
      seat.keyboardShutOut,
      "the pay control sits inside an aria-hidden or inert container, where the keyboard cannot follow it once it lights up",
    ).toBe(false);
    expect(seat.painted, "the pay control is not painted, so nobody can be waiting for it").toBe(
      true,
    );
    expect(
      seat.saysWhyItIsDark,
      "the pay control is dark and its own surface does not say why: no 'waiting for the provider order to exist' beside it",
    ).toBe(true);
    await tabTo(
      page,
      /^My cart/,
      "the cart link — the control that IS operable on the admitted screen while the pay control waits",
    );
  }

  /* --------------------------------------------- and none of it was done with a mouse */

  /*
   * The walk itself is the claim now: the browser recorded no trusted pointer press
   * anywhere in it. A later edit that reaches for `.click()` has to delete these lines and
   * notice what it is deleting.
   *
   * Read twice, on purpose. The first read asks whether there is a counter at all, because
   * a zero from an instrumentation that never installed is an absence wearing a reading's
   * clothes — and this assertion is worth exactly as much as the counter behind it.
   */
  expect(
    await trustedMousePresses(page),
    "the pointer counter is not installed on this page, so a zero from it would be an absence rather than a reading",
  ).not.toBeNull();
  expect(
    await trustedMousePresses(page),
    "this walk pressed a pointer somewhere, so it is no longer a keyboard-only journey",
  ).toBe(0);

  /*
   * The positive control: the counter is asked to prove it can still count.
   *
   * A zero is only evidence if the thing reporting it is capable of reporting anything
   * else, and a listener that failed to attach — a `mousedown` handler on the wrong
   * document, an init script a navigation dropped — would report this walk's zero just as
   * confidently as the real one. So the walk ends by pressing a pointer deliberately, on
   * the page's own `<h1>`, which is not a control and does nothing when it is clicked, and
   * requiring the counter to have seen exactly that one press. Exactly one, not merely
   * more than none: the count has to have moved from the zero asserted above by the single
   * press made here, which is also what would catch a counter double-counting the walk.
   *
   * Scrolled to the top first so the sticky header cannot be between the pointer and the
   * heading; at scroll zero a sticky header occupies its own place in the flow and the
   * journey's heading sits below it.
   */
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.getByRole("heading", { name: "Checkout", exact: true }).click();
  await expect
    .poll(async () => trustedMousePresses(page), {
      timeout: 5_000,
      message:
        "a deliberate trusted mouse press was not counted, so the zero asserted above was the instrumentation being dead rather than the walk being keyboard-only",
    })
    .toBe(1);
});

test("every control on the approval screen shows where the keyboard is", async ({ page }) => {
  await openCheckoutForMilk(page);

  /*
   * Reached by pressing Tab, not by calling `.focus()`.
   *
   * The storefront's ring is a `:focus-visible` rule, and `:focus-visible` is a statement
   * about *how* focus arrived: Chromium does not apply it to focus a script moved. A first
   * draft of this test called `element.focus()` and reported "no focus ring on Approve
   * ₹57.50" against a control that rings perfectly well for a person — the test had
   * simulated the one way of focusing that is meant not to draw one.
   */
  const controls = [/^Approve to pay ₹/, /^Reject this version$/, /^My cart/];

  for (const wanted of controls) {
    await page.goto(page.url());
    await expect(page.getByRole("button", { name: /^Approve to pay ₹/ })).toBeEnabled({
      timeout: 30_000,
    });
    await tabTo(page, wanted, `the control matching ${wanted}`);

    const ring = await page.evaluate(() => {
      const element = document.activeElement as HTMLElement;
      const style = getComputedStyle(element);
      return {
        name: element.getAttribute("aria-label") ?? (element.textContent ?? "").trim(),
        outlineStyle: style.outlineStyle,
        outlineWidth: Number.parseFloat(style.outlineWidth),
        outlineColor: style.outlineColor,
        outlineOffset: style.outlineOffset,
      };
    });

    expect(ring.outlineStyle, `no focus ring on ${ring.name}`).not.toBe("none");
    expect(
      ring.outlineWidth,
      `a focus ring of zero width on ${ring.name} is not a focus ring`,
    ).toBeGreaterThan(0);
    // Drawn outside the control rather than over it, so it is visible against a filled
    // button as well as against the page.
    expect(ring.outlineColor, `an invisible focus ring on ${ring.name}`).not.toBe(
      "rgba(0, 0, 0, 0)",
    );
  }
});

/* ---------------------------------------------------- what has to be announced */

test("the refusal interrupts and the state banner accompanies, and both say it in words", async ({
  page,
  request,
}) => {
  const checkoutId = await openCheckoutForMilk(page);

  /*
   * The refusal is produced by the approval press itself, because that press *is* the
   * submission. The window this test needs — the merchant's price moving under a card the
   * buyer is looking at — is now the window between the card being drawn and "Approve to
   * pay" being pressed, and the answer to that press is where the kernel says no. Pressing
   * Pay first is not an option: there is no Pay to press until a checkout is admitted, and
   * an admitted checkout has already spent the approval this refusal is about.
   */
  const before = await currentPrice(request, token, MILK_SKU);
  await injectPrice(
    request,
    token,
    MILK_SKU,
    before.unitPriceMinor + 100 + Math.floor(Math.random() * 400),
    "e2e: a refusal a buyer has to be told about",
  );

  const decision = await approveAndCaptureDecision(page);
  expect(decision.allowed).toBe(false);

  const refusal = page.getByLabel("The transaction kernel refused this submission");
  await expect(refusal).toBeVisible({ timeout: 30_000 });
  // Assertive, because the buyer pressed the one control that commits their money and is
  // owed an answer at once rather than whenever the screen reader next draws breath.
  await expect(refusal).toHaveAttribute("aria-live", "assertive");

  /* ------------------------------------------- colour carries none of the meaning */

  // Everything a buyer needs in order to know where they stand, taken as plain text with
  // every colour, border and icon discarded. If this passes on a monochrome reading, a
  // buyer who cannot see red has been told the same thing as one who can.
  const spoken = (await refusal.innerText()).replace(/\s+/g, " ");
  expect(spoken).toContain("Refused by the transaction kernel");
  expect(spoken).toContain(decision.code);
  expect(spoken).toContain("You were not charged.");
  expect(spoken).toContain("Your approval no longer matches what the merchant is selling.");

  // The two totals are distinguished by their captions, not by one of them being struck
  // through in red. A strikethrough is a colour-adjacent signal and it is not the signal.
  expect(spoken).toContain("You approved");
  expect(spoken).toContain("It is now");

  // And the direction of the change is in the character, not only in the colour of it.
  // `+`/`−` survives a monochrome rendering; red and green do not.
  expect(spoken).toMatch(/[+−]₹/);

  /* --------------------------- the sentences a buyer must not miss clear AA on their own */

  const headline = refusal.getByRole("heading", { level: 1 });
  expect(await contrastOf(headline)).toBeGreaterThanOrEqual(4.5);
  const notCharged = refusal.getByText("You were not charged.");
  expect(await contrastOf(notCharged)).toBeGreaterThanOrEqual(4.5);

  const after = await readCheckout(page, checkoutId);
  expect(after.current_version).toBeGreaterThan(1);

  /* ------------------------------------------------ and the accompaniment, also in words */

  /*
   * The other half of the claim: a state banner does **not** interrupt.
   *
   * It cannot be read off the refusal screen, which draws no banner — the refusal is the
   * whole screen — and it cannot be read before the refusal either, because a checkout
   * waiting for its buyer draws the approval card and no banner at all. So it is read where
   * a buyer actually meets one: on the successor version, approved and admitted, which is
   * where the refusal's own button sends them next. "Approved, not submitted" is not that
   * banner any more; one press spends the approval, so the state a buyer is accompanied
   * through is the admitted one.
   */
  await refusal.getByRole("button", { name: /^Review version/ }).click();
  await expect(page.getByRole("heading", { name: "Approve this order" })).toBeVisible({
    timeout: 30_000,
  });
  await approveCurrentVersion(page);

  const { banner, state, spoken: said } = await admittedBanner(page);
  await expect(banner).toHaveAttribute("aria-live", "polite");
  expect(
    ADMITTED[state],
    `the banner names ${state}, which is not a state one press of "Approve to pay" reaches`,
  ).toBeDefined();
  expect(said).toContain(ADMITTED[state].title);
  expect(said).toContain(ADMITTED[state].money);

  // The dot on a state banner is decorative and says so, so nothing is announced twice and
  // nothing is announced only as a shape. Asserted on a screen that has a banner: on the
  // refusal there is none, and the loop that used to run there walked an empty list.
  const dots = banner.locator('[aria-hidden="true"]');
  expect(await dots.count(), "the banner drew no decorative dot to check").toBeGreaterThan(0);
  for (let index = 0; index < (await dots.count()); index += 1) {
    expect((await dots.nth(index).innerText()).trim()).toBe("");
  }
});

/**
 * The WCAG 2.1 contrast ratio of an element's own text against what is behind it.
 *
 * Computed in the page rather than read off a design document, because the value that
 * matters is the one that actually rendered: a token can be correct and be overridden, and
 * an opacity applied to a parent changes the answer without changing any colour anybody
 * wrote down. Backgrounds are walked upward until an opaque one is found, which is what
 * the browser does when it paints.
 */
async function contrastOf(locator: Locator): Promise<number> {
  return locator.first().evaluate((element) => {
    const parse = (value: string): [number, number, number, number] => {
      const parts = value.match(/[\d.]+/g)?.map(Number) ?? [];
      return [parts[0] ?? 0, parts[1] ?? 0, parts[2] ?? 0, parts[3] ?? 1];
    };
    const channel = (value: number): number => {
      const scaled = value / 255;
      return scaled <= 0.03928 ? scaled / 12.92 : ((scaled + 0.055) / 1.055) ** 2.4;
    };
    const luminance = ([r, g, b]: number[]): number =>
      0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);

    const foreground = parse(getComputedStyle(element).color);
    let node: HTMLElement | null = element as HTMLElement;
    let background: [number, number, number, number] = [255, 255, 255, 1];
    while (node) {
      const candidate = parse(getComputedStyle(node).backgroundColor);
      if (candidate[3] > 0) {
        background = candidate;
        break;
      }
      node = node.parentElement;
    }
    const light = Math.max(luminance(foreground), luminance(background));
    const dark = Math.min(luminance(foreground), luminance(background));
    return (light + 0.05) / (dark + 0.05);
  });
}

/* --------------------------------------------------------------- 390 pixels wide */

test("nothing on the buying journey overflows a 390px screen sideways", async ({ page }) => {
  // The suite's mobile project is already 390 wide, so this runs at both widths on
  // purpose: a page that scrolls sideways at 390 and not at 1280 is a mobile bug, and one
  // that does it at both is a layout bug. Either way the assertion is the same sentence.
  const screens: Array<[string, () => Promise<void>]> = [
    // The home route is the copilot, so its readiness signal is the composer rather than a
    // shelf heading. The assertion this list exists for is the sideways-scroll one below,
    // and the copilot has to pass it exactly like the document pages do -- an application
    // that owns the whole viewport is the easiest place to overflow one.
    ["/", async () => { await expect(page.locator("#copilot-composer")).toBeVisible({ timeout: 30_000 }); }],
    ["/search?q=doodh", async () => { await expect(page.getByRole("heading", { name: /Results for/ })).toBeVisible({ timeout: 30_000 }); }],
    ["/orders", async () => { await expect(page.getByRole("heading", { name: "Orders" })).toBeVisible({ timeout: 30_000 }); }],
    ["/basket", async () => { await expect(page.locator("main")).toBeVisible({ timeout: 30_000 }); }],
  ];

  for (const [url, ready] of screens) {
    await page.goto(url);
    await ready();
    const overflow = await page.evaluate(() => ({
      scroll: document.documentElement.scrollWidth,
      client: document.documentElement.clientWidth,
    }));
    expect(
      overflow.scroll,
      `${url} scrolls sideways: the document is ${overflow.scroll}px wide inside ${overflow.client}px`,
    ).toBeLessThanOrEqual(overflow.client + 1);
  }
});

test("the refusal fits a 390px screen, and its delta table can be read from a keyboard", async ({
  page,
  request,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });

  await openCheckoutForMilk(page);

  // The price has to move while version 1's card is still on screen, because the press
  // that can be refused is the approval: it records the consent and hands it to the kernel
  // in one act, so there is no approved-but-unspent checkout to change the price under.
  // Same window, same refusal, one request earlier in the journey.
  const before = await currentPrice(request, token, MILK_SKU);
  await injectPrice(
    request,
    token,
    MILK_SKU,
    before.unitPriceMinor + 100 + Math.floor(Math.random() * 400),
    "e2e: the refusal at 390px",
  );
  const decision = await approveAndCaptureDecision(page);
  expect(
    decision.allowed,
    "the merchant's price moved under the card and the kernel admitted it anyway, so there is no refusal here to measure",
  ).toBe(false);

  const refusal = page.getByLabel("The transaction kernel refused this submission");
  await expect(refusal).toBeVisible({ timeout: 30_000 });

  // The page does not scroll sideways even though the table inside it must.
  const overflow = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
  expect(
    overflow.scroll,
    "the refusal screen pushes the whole page sideways at 390px",
  ).toBeLessThanOrEqual(overflow.client + 1);

  /*
   * The table's own scroller, which axe flagged and which is the reason this test exists.
   * At 390px the table is wider than the screen by construction (`min-w-[520px]`), so a
   * container that is not focusable is a container a keyboard-only buyer cannot scroll —
   * and what they cannot scroll to is the column saying what the merchant changed. That is
   * the evidence for the refusal, on the screen whose entire job is to present it.
   */
  const scroller = page.getByRole("region", {
    name: "The changed fields, as a table that scrolls sideways",
  });
  await expect(scroller).toBeVisible();
  const geometry = await scroller.evaluate((element) => ({
    scrollWidth: element.scrollWidth,
    clientWidth: element.clientWidth,
    tabIndex: (element as HTMLElement).tabIndex,
    role: element.getAttribute("role"),
    label: element.getAttribute("aria-label"),
  }));
  expect(
    geometry.scrollWidth,
    "the delta table did not overflow at 390px, so this test is no longer asserting anything",
  ).toBeGreaterThan(geometry.clientWidth);
  expect(geometry.tabIndex, "the delta table's scroller cannot be reached by keyboard").toBe(0);
  expect(geometry.role).toBe("region");
  expect(geometry.label).not.toBeNull();

  // Reachable, and then actually scrollable with the arrow keys once it has focus.
  await scroller.focus();
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("ArrowRight");
  await expect
    .poll(async () => scroller.evaluate((element) => element.scrollLeft), { timeout: 5_000 })
    .toBeGreaterThan(0);

  /*
   * The control a person has to hit with a thumb is big enough to hit.
   *
   * The bar asserted is WCAG 2.2's 2.5.8 Target Size (Minimum), which is 24 by 24 CSS
   * pixels and is the AA requirement. It is not the figure most people quote: 44 by 44 is
   * 2.5.5 Target Size (Enhanced), which is AAA, and the platform guidelines echo it.
   *
   * This button measures 42 by design rather than by accident. `size="lg"` is `h-12`,
   * three rem, and `globals.css` sets the root font to 14px rather than 16, so every rem
   * in the storefront is 12.5% smaller than the Tailwind defaults assume. That is a single
   * decision affecting every control on every screen, and it leaves the primary button two
   * pixels short of the AAA figure. Reported rather than asserted, because moving it is a
   * change to the storefront's whole type scale and not a test's to make.
   */
  const review = refusal.getByRole("button", { name: /^Review version/ });
  const box = await review.boundingBox();
  expect(box, "the refusal offered no way forward at 390px").not.toBeNull();
  expect(
    box!.height,
    `the primary control is ${box!.height}px tall at 390px, under the 24px WCAG 2.5.8 minimum`,
  ).toBeGreaterThanOrEqual(24);
  expect(box!.width).toBeGreaterThanOrEqual(24);
});

test("the state banner names the state in words at every width", async ({ page }) => {
  const checkoutId = await openCheckoutForMilk(page);
  await approveCurrentVersion(page);

  /*
   * `role="status"` and a sentence, not a coloured strip with a code in it.
   *
   * The state a buyer meets after approving is no longer APPROVED. One press records the
   * consent and spends it, so the banner here is the admitted one — EXECUTION_PENDING, or
   * AWAITING_PAYMENT if the worker has already created the provider order. Which arrived is
   * read off the banner's own code and the words are then required to be *that* state's,
   * because a banner showing one state's code over another state's sentence is precisely
   * the failure this test exists to catch, and a test that accepted either sentence for
   * either code would not catch it.
   */
  const { banner, state, spoken } = await admittedBanner(page);
  // The code is there, verbatim, so an engineer reading over a buyer's shoulder can match
  // the screen to the database. Verbatim is the assertion: it is the kernel's own spelling
  // of a state this press reaches, not a label the storefront chose or prettified.
  expect(
    Object.keys(ADMITTED),
    `the banner names ${state}, which is not a state one press of "Approve to pay" reaches: ${spoken}`,
  ).toContain(state);
  const words = ADMITTED[state];
  // And the meaning is carried by the words either side of it: what the state is, and what
  // is true of the buyer's money while it lasts.
  expect(spoken).toContain(words.title);
  expect(spoken).toContain(words.money);

  // The server agrees this checkout is past its approval and spending it, so the banner is
  // describing this checkout rather than a state the screen invented. Exact equality with
  // the code above is deliberately not asserted: the panel stops re-reading once the
  // provider order exists, so a screen still saying EXECUTION_PENDING while the server has
  // moved to AWAITING_PAYMENT is a lag between two true readings, not a lie.
  const server = await readCheckout(page, checkoutId);
  expect(
    Object.keys(ADMITTED),
    `the checkout is ${server.state} after the approval was pressed`,
  ).toContain(server.state);

  expect(await contrastOf(banner.getByText(words.title))).toBeGreaterThanOrEqual(4.5);
});

/** A product name is data, not a pattern. */
function escapeForRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
