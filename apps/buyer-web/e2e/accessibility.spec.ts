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
  MILK_NAME,
  MILK_SKU,
  approveCurrentVersion,
  openCheckoutForMilk,
  payAndCaptureDecision,
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

test("a buyer can search, add, open a checkout and approve it with no mouse at all", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Best sellers" })).toBeVisible({
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
  const add = page.getByRole("button", { name: `Add ${MILK_NAME} to basket` });
  await expect(add).toBeVisible({ timeout: 30_000 });
  await tabTo(page, new RegExp(`^Add ${escapeForRegExp(MILK_NAME)} to basket$`), "the add button");
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
  await expect(page.getByRole("heading", { name: "Your basket" })).toBeVisible({
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
  await expect(page.getByRole("button", { name: /^Approve ₹/ })).toBeEnabled({ timeout: 30_000 });
  await tabTo(page, /^Approve ₹/, "the approve button on the trusted surface");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: /^Version \d+ is approved$/ })).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByRole("button", { name: "Pay", exact: true })).toBeEnabled({
    timeout: 30_000,
  });

  // Nothing above used a mouse. Recorded here so that a later edit that reaches for
  // `.click()` has to delete this line and notice what it is deleting.
  await tabTo(page, /^Pay$/, "the pay button");
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
  const controls = [/^Approve ₹/, /^Reject this version$/, /^My cart/];

  for (const wanted of controls) {
    await page.goto(page.url());
    await expect(page.getByRole("button", { name: /^Approve ₹/ })).toBeEnabled({
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
  await approveCurrentVersion(page);

  // The approved state is an accompaniment: it is true, the buyer is looking at it, and
  // it must not talk over anything.
  const banner = page.getByRole("status").filter({ hasText: "Approved, not submitted" });
  await expect(banner).toBeVisible();
  await expect(banner).toHaveAttribute("aria-live", "polite");

  const before = await currentPrice(request, token, MILK_SKU);
  await injectPrice(
    request,
    token,
    MILK_SKU,
    before.unitPriceMinor + 100 + Math.floor(Math.random() * 400),
    "e2e: a refusal a buyer has to be told about",
  );

  const decision = await payAndCaptureDecision(page);
  expect(decision.allowed).toBe(false);

  const refusal = page.getByLabel("The transaction kernel refused this submission");
  await expect(refusal).toBeVisible({ timeout: 30_000 });
  // Assertive, because the buyer pressed Pay and is owed an answer at once rather than
  // whenever the screen reader next draws breath.
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

  // The dot on a state banner is decorative and says so, so nothing is announced twice
  // and nothing is announced only as a shape.
  const dots = page.locator('[role="status"] [aria-hidden="true"]');
  for (let index = 0; index < (await dots.count()); index += 1) {
    expect((await dots.nth(index).innerText()).trim()).toBe("");
  }

  /* --------------------------- the sentences a buyer must not miss clear AA on their own */

  const headline = refusal.getByRole("heading", { level: 1 });
  expect(await contrastOf(headline)).toBeGreaterThanOrEqual(4.5);
  const notCharged = refusal.getByText("You were not charged.");
  expect(await contrastOf(notCharged)).toBeGreaterThanOrEqual(4.5);

  const after = await readCheckout(page, checkoutId);
  expect(after.current_version).toBeGreaterThan(1);
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
    ["/", async () => { await expect(page.getByRole("heading", { name: "Best sellers" })).toBeVisible({ timeout: 30_000 }); }],
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
  await approveCurrentVersion(page);

  const before = await currentPrice(request, token, MILK_SKU);
  await injectPrice(
    request,
    token,
    MILK_SKU,
    before.unitPriceMinor + 100 + Math.floor(Math.random() * 400),
    "e2e: the refusal at 390px",
  );
  await payAndCaptureDecision(page);

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
  await openCheckoutForMilk(page);
  await approveCurrentVersion(page);

  // `role="status"` and a sentence, not a coloured strip with a code in it. The code is
  // there too, verbatim, so an engineer reading over a buyer's shoulder can match the
  // screen to the database — but it is not what carries the meaning.
  const banner = page.getByRole("status").filter({ hasText: "APPROVED" }).first();
  await expect(banner).toBeVisible();
  const spoken = (await banner.innerText()).replace(/\s+/g, " ");
  expect(spoken).toContain("Approved, not submitted");
  expect(spoken).toContain("APPROVED");
  expect(spoken).toContain("no money has moved");
  expect(await contrastOf(banner.getByText("Approved, not submitted"))).toBeGreaterThanOrEqual(4.5);
});

/** A product name is data, not a pattern. */
function escapeForRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
