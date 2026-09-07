/**
 * What the storefront does when the answer is not the happy one.
 *
 * This app has no fixture layer and that is deliberate: `playwright.config.ts` says so at
 * length, and the proxy in `src/app/api/backend/[...path]/route.ts` refuses to invent a
 * response when the API is unreachable. The consequence is a property worth testing over
 * and over, because it is the one a well-meaning `catch` destroys: **a screen that could
 * not read something renders the failure, and never a number.** A storefront that fell
 * back to plausible data would show a buyer a price no kernel ever agreed to, and on the
 * approval screen that is not a bug, it is a fabricated consent.
 *
 * So every test below drives a real failure — a 404 the API really sends, a 422 it really
 * sends, a 400 on a cursor it did not issue, a session cookie that fails its own signature,
 * a line the merchant delists while the cart is open — and then asserts two things: the
 * failure is legible in the server's own words, and there is no money on the screen.
 *
 * One exception is called out where it happens. The severed-connection test cuts the
 * network rather than asking the shared API to fall over, because the API is one process
 * serving several suites and stopping it would fail everyone else's run for a reason none
 * of them could see. Cutting the wire substitutes nothing: no response is invented, no
 * body is rewritten, and the condition under test — the storefront cannot reach its own
 * backend — is exactly the real one.
 */
import { expect, test } from "@playwright/test";

import {
  MILK_NAME,
  MILK_SKU,
  addProduct,
  enterStorefront,
  releaseCheckouts,
  visibleSearchBox,
} from "./journey";
import {
  UNREACHABLE,
  currentStock,
  inject,
  mintToken,
  requireApi,
  restore,
} from "./live-api";

let reachable = false;
let token = "";
let listedOnEntry = true;

test.beforeAll(async ({ request }) => {
  reachable = await requireApi(request);
  if (!reachable) return;
  token = await mintToken(request);
  listedOnEntry = (await currentStock(request, token, MILK_SKU)).listed;
});

test.beforeEach(() => {
  test.skip(!reachable, UNREACHABLE);
});

test.afterEach(async ({ page, request }) => {
  await releaseCheckouts(page);
  // The delisting test takes the milk off the shelf. Putting it back here rather than at
  // the end of that test means a failure inside it does not leave every later spec in
  // this suite — and the next person's demo — shopping in a store with no milk.
  if (reachable) {
    await restore(request, token, "AVAILABILITY_SET", MILK_SKU, listedOnEntry);
  }
});

/** A well-formed UUID that names nothing. The API answers 404 for it, not 422. */
const ABSENT_UUID = "01a06fae-0000-7000-8000-000000000000";

/**
 * The page's own error region, scoped to the document rather than to the whole body.
 *
 * `getByRole("alert")` on its own is ambiguous here and only intermittently so, which is
 * the worst way for a selector to be wrong. Next appends `#__next-route-announcer__` —
 * an empty `role="alert"` live region — the first time a route changes on the client, so
 * the same assertion resolved to one element on a cold load and two after any soft
 * navigation earlier in the test. Scoping to `#main` takes the app's own alert and leaves
 * the framework's announcer where it belongs.
 */
function failureIn(page: import("@playwright/test").Page) {
  return page.locator("#main").getByRole("alert");
}

/**
 * Everything on the page that looks like an amount.
 *
 * Expected to be empty on every screen below, which is the sharpest form the "renders the
 * failure rather than invented data" assertion can take: it does not name a figure the
 * test would have to know in advance, it says that no figure of any kind is there to be
 * read. A rupee sign on a screen that could not reach the server came from somewhere, and
 * there is nowhere honest for it to have come from.
 */
function moneyIn(text: string): string[] {
  return [...text.matchAll(/₹\s?[\d,]+(?:\.\d{2})?/g)].map((match) => match[0]);
}

test("a checkout that does not exist renders the server's refusal to find it, and no figures", async ({
  page,
}) => {
  await page.goto(`/checkout/${ABSENT_UUID}`);

  const failure = failureIn(page);
  await expect(failure).toBeVisible({ timeout: 30_000 });
  await expect(failure.getByText("This checkout could not be read")).toBeVisible();
  // The API's own sentence, not a sentence this app wrote over the top of it.
  await expect(
    failure.getByText("No checkout with that identifier belongs to this session."),
  ).toBeVisible();
  await expect(failure.getByRole("button", { name: "Try again" })).toBeVisible();

  // The one control this screen is entitled to draw, and the whole of what it draws.
  // Asserted before the absences because it is what gives them a meaning: zeroes counted
  // against a `#main` that never rendered, or against role names this app no longer uses,
  // are reassurances about nothing. One button, named as the retry, says the failure
  // branch is on screen and that `getByRole(..., { name })` can still see what is in it.
  const controls = page.locator("#main").getByRole("button");
  await expect(controls).toHaveCount(1);
  await expect(controls).toHaveAccessibleName("Try again");

  // Nothing that could be mistaken for an order. No approval card, no total, no version
  // trail — a screen that drew any of those would be describing a checkout it never read.
  await expect(page.getByRole("heading", { name: "Approve this order" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Version trail" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^Approve/ })).toHaveCount(0);
  // `/^Pay/` rather than the exact word, the same fix states.spec.ts made at the same
  // line: the payment surface's control is named "Pay ₹579.95", so an exact-word check
  // watched for a button this app never draws and would have counted its reassuring zero
  // with the whole payment panel on screen. The panel's heading is named alongside it,
  // because the surface a 404 must not draw is bigger than the button on it, and the
  // heading carries the version number a screen that read no checkout cannot know.
  await expect(page.getByRole("button", { name: /^Pay/ })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: /^Pay for version / })).toHaveCount(0);
  expect(moneyIn(await page.locator("body").innerText())).toEqual([]);
});

test("a malformed checkout id renders the validation failure rather than guessing at one", async ({
  page,
}) => {
  // Not a UUID at all. The API validates the path and answers 422 with the reason, which
  // is a different failure from "not found" and must not be flattened into it.
  await page.goto("/checkout/not-a-uuid");

  const failure = failureIn(page);
  await expect(failure).toBeVisible({ timeout: 30_000 });
  await expect(failure.getByText("This checkout could not be read")).toBeVisible();
  await expect(
    failure.getByText("The request body, query or path did not match the endpoint's schema."),
  ).toBeVisible();
  expect(moneyIn(await page.locator("body").innerText())).toEqual([]);
});

test("an order that does not exist says so, and does not draw a sale", async ({ page }) => {
  await page.goto(`/orders/${ABSENT_UUID}`);

  const failure = failureIn(page);
  await expect(failure).toBeVisible({ timeout: 30_000 });
  await expect(failure.getByText("Could not load this order")).toBeVisible();
  expect(moneyIn(await page.locator("body").innerText())).toEqual([]);
});

test("a product code the catalogue does not carry is admitted to, not invented", async ({
  page,
}) => {
  await page.goto("/p/NOPE-SKU-999");

  await expect(page.getByText("No product with that code")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/nothing under NOPE-SKU-999/)).toBeVisible();
  // No price, and no way to put a thing that does not exist into a cart.
  await expect(page.getByRole("button", { name: /to cart/ })).toHaveCount(0);
  expect(moneyIn(await page.locator("body").innerText())).toEqual([]);
});

test("a cursor the API did not issue is refused, and the refusal reaches the browser unchanged", async ({
  page,
}) => {
  // Driven through the app's own proxy with the browser's own session, because the
  // storefront never puts a cursor in a URL a person can edit: it holds the one the server
  // handed it. What can be checked from here is the layer this app owns — that a keyset
  // cursor the API rejects comes back to the page as the API's own refusal, with nothing
  // substituted and no empty page pretending to be the end of the list.
  await page.goto("/orders");
  await expect(page.getByRole("heading", { name: "Orders" })).toBeVisible({ timeout: 30_000 });

  const response = await page.request.get("/api/backend/v1/orders?limit=5&cursor=zzzz");
  expect(response.status()).toBe(400);
  const problem = (await response.json()) as { title?: string; detail?: string; orders?: unknown };
  expect(problem.title).toBe("Invalid cursor");
  expect(problem.detail).toBe("The cursor is not one this endpoint issued. Start again without one.");
  // The half that matters: a refused page is not answered with rows.
  expect(problem.orders).toBeUndefined();

  // And a cursor the API *did* issue still works, so the assertion above is about the
  // cursor being bad rather than about paging being broken.
  const firstPage = await page.request.get("/api/backend/v1/orders?limit=1");
  expect(firstPage.ok()).toBe(true);
  const body = (await firstPage.json()) as { orders: unknown[]; next_cursor: string | null };
  expect(Array.isArray(body.orders)).toBe(true);
  if (body.next_cursor) {
    const second = await page.request.get(
      `/api/backend/v1/orders?limit=1&cursor=${encodeURIComponent(body.next_cursor)}`,
    );
    expect(second.ok()).toBe(true);
  }
});

test("a session cookie that fails its own signature is discarded, not half-believed", async ({
  page,
  context,
}) => {
  // The browser-visible half of "a 401 after a server restart": the page presents a
  // credential this process will not accept, and the storefront recovers by minting a new
  // anonymous session rather than by failing or, worse, by trusting the identity inside
  // the forgery. `httpOnly` never protected against a cookie an attacker *writes*.
  await page.goto("/");
  // Polled rather than waited for behind a rendered element. The page's own markup appears
  // while the first read is still in flight, and the cookie is set on the response to that
  // read — so asserting on the jar the moment something renders was asking for a credential
  // that had not been issued yet, and reported it as the app not issuing one.
  await expect
    .poll(
      async () => (await context.cookies()).some((cookie) => cookie.name === "acr_session"),
      { timeout: 30_000, message: "the storefront issued no session cookie to tamper with" },
    )
    .toBe(true);
  const session = (await context.cookies()).find((cookie) => cookie.name === "acr_session");
  expect(session).toBeDefined();

  // A payload that parses as JSON and names somebody else, with a tag that cannot verify.
  const forged = `${Buffer.from(
    JSON.stringify({ token: "not-a-real-token", buyer_ref: "somebody-else", capabilities: [] }),
    "utf8",
  ).toString("base64url")}.this-tag-is-not-the-hmac`;
  await context.clearCookies();
  await context.addCookies([{ ...session!, value: forged }]);

  // The store still works, which is the recovery, and the products are real ones.
  await page.goto("/");
  await expect(page.locator("#copilot-composer")).toBeVisible({ timeout: 30_000 });
  // And the catalogue still answers under the session the app minted to replace the
  // forgery. Asserted through a read rather than a heading: static markup renders whether
  // or not the store is reachable, which is exactly the failure this test is about.
  const shelf = await page.request.get("/api/backend/v1/catalogue/products?limit=1");
  expect(shelf.ok(), "the storefront could not read the catalogue after recovering").toBe(true);
  expect(((await shelf.json()) as { products: unknown[] }).products.length).toBeGreaterThan(0);

  const who = await page.request.get("/api/backend/session");
  expect(who.ok()).toBe(true);
  const identity = (await who.json()) as Record<string, unknown>;
  // Not the identity that was smuggled in, and never the bearer token.
  expect(identity.buyer_ref).not.toBe("somebody-else");
  expect(JSON.stringify(identity)).not.toContain("token");
});

test("with the backend unreachable the storefront says so and shows nothing it cannot source", async ({
  page,
}) => {
  // The one place this suite cuts a wire. Nothing is substituted: the request is aborted
  // at the transport, which is what an unreachable API is, and the assertion is about what
  // the app does with no answer at all. Asking the shared API to fall over instead would
  // break three other suites for a reason none of them could diagnose.
  await page.route("**/api/backend/**", (route) => route.abort("connectionfailed"));

  await page.goto(`/checkout/${ABSENT_UUID}`);

  const failure = failureIn(page);
  await expect(failure).toBeVisible({ timeout: 30_000 });
  await expect(failure.getByText("This checkout could not be read")).toBeVisible();
  await expect(failure.getByRole("button", { name: "Try again" })).toBeVisible();
  expect(moneyIn(await page.locator("body").innerText())).toEqual([]);

  // The catalogue is the surface most likely to have a tempting fallback, and it has none.
  await page.unroute("**/api/backend/**");
  await page.route("**/api/backend/**", (route) => route.abort("connectionfailed"));
  await page.goto("/search?q=doodh");
  await expect(page.getByText("The catalogue did not answer")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("link", { name: new RegExp(MILK_NAME) })).toHaveCount(0);
  expect(moneyIn(await page.locator("body").innerText())).toEqual([]);
});

test("a line the merchant delists while the cart is open keeps its place and is not priced", async ({
  page,
  request,
}) => {
  await enterStorefront(page);
  await addProduct(page, "doodh", MILK_NAME, 1);

  await page.goto("/cart");
  await expect(page.getByRole("heading", { name: "Your cart" })).toBeVisible({
    timeout: 30_000,
  });
  // The cart priced normally first, so what follows is a change rather than a store that
  // was always broken.
  await expect(page.getByRole("button", { name: "Proceed to checkout" })).toBeEnabled({
    timeout: 30_000,
  });

  // The merchant takes it off the shelf underneath the buyer.
  const injection = await inject(
    request,
    token,
    "AVAILABILITY_SET",
    MILK_SKU,
    false,
    "e2e: the merchant delists a line mid-session",
  );
  expect(injection.deltas.length).toBeGreaterThan(0);

  await page.reload();
  await expect(page.getByRole("heading", { name: "Your cart" })).toBeVisible({
    timeout: 30_000,
  });

  // The line stays. Vanishing between renders is the failure mode this asserts against:
  // a buyer who put something in a cart is entitled to see it there and be told why it
  // cannot be sold, rather than to find it silently gone.
  await expect(page.getByText(MILK_SKU).first()).toBeVisible({ timeout: 30_000 });

  // And it is not priced, because the merchant returned no price for it. The panel that
  // would have carried the bill carries the refusal instead, counted in the merchant's own
  // arithmetic — one of the one lines in this cart — so a screen that quietly dropped the
  // line and priced the rest could not produce this sentence.
  const notice = page.getByText("This cart has no total yet");
  await expect(notice).toBeVisible();
  await expect(page.getByText("The merchant could not price 1 of these 1 lines")).toBeVisible();
  // And the reason named against this SKU is the one the merchant gave: withdrawn, not run
  // down to zero. The two are different failures with different remedies, and flattening
  // "we no longer sell this" into "we have none today" would send the buyer waiting for a
  // restock that is never coming.
  await expect(page.getByText(/the merchant no longer lists it/)).toBeVisible();
  await expect(page.getByText("No longer available")).toBeVisible();
  await expect(page.getByRole("button", { name: "Proceed to checkout" })).toBeDisabled();

  // And no total was drawn in place of the one the merchant declined to give. The bill
  // panel is rendered only from a quote, so its absence is the absence of every figure it
  // would have carried — subtotal, delivery, tax and total together — rather than of one
  // line of copy that happened to name a discount.
  await expect(page.getByRole("region", { name: "Bill details" })).toHaveCount(0);

  await restore(request, token, "AVAILABILITY_SET", MILK_SKU, listedOnEntry);
  await page.reload();
  await expect(page.getByRole("button", { name: "Proceed to checkout" })).toBeEnabled({
    timeout: 30_000,
  });
});

test("a search that matched nothing says nothing matched, and offers no products", async ({
  page,
}) => {
  await enterStorefront(page);
  await visibleSearchBox(page).fill("zzzqqqxnothing");
  await visibleSearchBox(page).press("Enter");
  await expect(page).toHaveURL(/\/search\?q=zzzqqqxnothing/);

  await expect(page.getByText(/No product matched/)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("button", { name: /to cart/ })).toHaveCount(0);
  expect(moneyIn(await page.locator("#main").innerText())).toEqual([]);
});
