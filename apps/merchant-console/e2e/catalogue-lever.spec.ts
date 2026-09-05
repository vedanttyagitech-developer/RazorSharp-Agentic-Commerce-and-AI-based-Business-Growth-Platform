/**
 * The catalogue's one lever, and the guard in front of it.
 *
 * This is the only place in the console that changes merchant state, and it changes it in
 * exactly one way: `POST /v1/scenario/injections`, applied by the merchant simulator under
 * its own lock and audited as `SCENARIO_INJECTION` in the same transaction. So two claims
 * are worth proving and neither is provable from a unit test:
 *
 *  1. **The wire carries integer paise.** Not a rupee figure, not a string, not a float.
 *     The request is intercepted and its body inspected, because the bug this guard exists
 *     for was silent: `Number.parseInt("28.50", 10)` is 28, an integrality check on *that*
 *     can never fail, and an operator typing the rupee figure out of habit set ₹0.28 as
 *     the live price with the button still enabled and nothing on screen saying so.
 *  2. **Nothing else is sent.** Every non-GET request the page makes is recorded and the
 *     set must be exactly one injection. A console that also wrote somewhere local, or
 *     that fired the injection twice, would look identical on screen.
 *
 * The refusals are asserted as *refusals before the send*: the button disabled, the entry
 * named as invalid, and -- the part that matters -- no request at all. "The API rejected
 * it" would be a different and much weaker guarantee, because it would mean the console
 * was willing to ask.
 *
 * On touching shared state: the tenant here is also being read by the storefront and agent
 * suites while this runs. So the SKU is chosen from the ones the API reports as not
 * sellable -- nothing can be bought with it, so no checkout can be mid-flight over it --
 * the price is moved by one rupee and put back, and the restore runs in a `finally` against
 * the API directly so a failed assertion cannot leave the catalogue moved. What cannot be
 * put back is the revision counter and the audit trail, and neither should be: an injection
 * that left no trace would be the dishonest kind.
 */
import { expect, test, type Page, type Request } from "@playwright/test";

import { API, platformUnreachable, read, type CataloguePage, type Product } from "./support";

/**
 * The key the API gates `/v1/scenario/` on, for the restore path only.
 *
 * The console keeps this server-side so a browser cannot lift it out of a network panel;
 * a test process is not a browser, and the restore has to work even when the assertion
 * that would have driven the UI has already failed.
 */
const SCENARIO_KEY = process.env.SCENARIO_KEY ?? "local-demo-scenario-key";
const TENANT = process.env.NEXT_PUBLIC_TENANT_SLUG ?? "demo";

let unreachable: string | null = null;

test.beforeAll(async () => {
  unreachable = await platformUnreachable("the catalogue injection suite");
});

test.beforeEach(() => {
  test.skip(unreachable !== null, unreachable ?? "");
});

/**
 * Entries that are not a count of paise, and why each one is dangerous rather than merely
 * wrong. Every one of these parses to a number under some reading, which is the point.
 */
const NOT_PAISE: ReadonlyArray<readonly [string, string]> = [
  ["28.50", "a rupee figure typed out of habit; parseInt truncates it to 28 paise"],
  ["1e3", "exponent notation: Number() reads 1000, a digit-by-digit reader reads nothing"],
  ["300abc", "parseInt stops at the letters and returns 300"],
  ["0", "free is a price the kernel would honour, and never one an operator meant"],
  ["", "an empty field is not a zero"],
  [" ", "whitespace is not a zero either"],
  ["-500", "a negative price parses and is not a price"],
  ["+500", "a leading plus parses and is not a run of digits"],
];

/** Mint an operator session straight against the API, for the restore path. */
async function operatorToken(): Promise<string> {
  const response = await fetch(`${API}/v1/demo/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Scenario-Key": SCENARIO_KEY },
    body: JSON.stringify({ tenant_slug: TENANT, actor_type: "OPERATOR" }),
  });
  expect(response.ok, `could not mint an operator session: ${response.status}`).toBeTruthy();
  return ((await response.json()) as { token: string }).token;
}

/** Put a price back where it was, whatever happened to the assertions above. */
async function setPrice(sku: string, minor: number): Promise<void> {
  const token = await operatorToken();
  const response = await fetch(`${API}/v1/scenario/injections`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      "X-Scenario-Key": SCENARIO_KEY,
    },
    body: JSON.stringify({
      kind: "PRICE_SET",
      sku,
      value: minor,
      note: "e2e restore: putting the price back where the suite found it",
    }),
  });
  // 409 is the API refusing a no-op, which means the price is already what we wanted.
  expect(
    response.ok || response.status === 409,
    `could not restore ${sku} to ${minor} paise: ${response.status} ${await response.text()}`,
  ).toBeTruthy();
}

/**
 * A SKU nothing can be bought with.
 *
 * Chosen from the API's own `available=false` selection rather than named here, so this
 * spec does not hardcode a row out of somebody's seed file, and sorted so two runs pick the
 * same one. Falls back to the last row of the catalogue if the tenant has no unsellable
 * SKU -- the point is to disturb the least-trafficked row available, not to require one.
 */
async function quietProduct(page: Page): Promise<Product> {
  const unsellable = await read<CataloguePage>(
    page,
    "/api/backend/v1/catalogue/products?available=false&limit=50",
  );
  const pool =
    unsellable.products.length > 0
      ? unsellable.products
      : (await read<CataloguePage>(page, "/api/backend/v1/catalogue/products?limit=50")).products;
  expect(pool.length, "this tenant has no catalogue to inject against").toBeGreaterThan(0);
  return [...pool].sort((a, b) => a.sku.localeCompare(b.sku))[pool.length - 1];
}

/** Open the catalogue filtered to the unsellable rows, and unfold one row's editor. */
async function openEditor(page: Page, sku: string): Promise<void> {
  await page.goto("/catalogue");
  await page.getByRole("button", { name: /^not sellable/ }).click();
  const row = page.locator("tbody tr").filter({ hasText: sku }).first();
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: "Inject" }).click();
}

/** Every write this page attempted, in order. */
function recordWrites(page: Page): string[] {
  const writes: string[] = [];
  page.on("request", (request: Request) => {
    if (request.method() !== "GET" && request.url().includes("/api/backend/")) {
      writes.push(`${request.method()} ${request.url().split("/api/backend")[1]}`);
    }
  });
  return writes;
}

test("a price that is not a count of paise is refused before anything is sent", async ({ page }) => {
  const product = await quietProduct(page);
  const writes = recordWrites(page);
  await openEditor(page, product.sku);

  const field = page.getByLabel(`Unit price in paise for ${product.sku}`);
  const apply = page.getByRole("button", { name: "Set price" });
  await expect(apply).toBeEnabled();

  for (const [entry, why] of NOT_PAISE) {
    await field.fill(entry);
    // Disabled, so the request cannot be made -- rather than made and rejected. A console
    // willing to ask is a console that will one day be told yes.
    await expect(apply, `${JSON.stringify(entry)} was accepted: ${why}`).toBeDisabled();
    await expect(field).toHaveAttribute("aria-invalid", "true");
    await expect(page.locator(`#price-echo-${product.sku}`)).toContainText(
      "Digits only, and more than zero. A decimal point is not a paise figure.",
    );
  }

  // And the guard does not latch: a valid entry after an invalid one is accepted, and an
  // invalid one after a valid one is refused again.
  await field.fill(String(product.unit_price_minor));
  await expect(apply).toBeEnabled();
  await field.fill("28.50");
  await expect(apply).toBeDisabled();

  expect(writes, "an entry that was refused still reached the network").toEqual([]);
});

test("the price field reads the entry back as money before it is sent", async ({ page }) => {
  const product = await quietProduct(page);
  await openEditor(page, product.sku);

  const field = page.getByLabel(`Unit price in paise for ${product.sku}`);
  const echo = page.locator(`#price-echo-${product.sku}`);

  // The field opens on the server's own integer, unconverted.
  await expect(field).toHaveValue(String(product.unit_price_minor));

  // 2850 paise is ₹28.50, and 28.50 typed into a paise field is not. Both readings are on
  // screen beside the button, in the notation the row above uses, because that is the only
  // place the difference is obvious to someone in a hurry.
  await field.fill("2850");
  await expect(echo).toContainText("Will set ₹28.50");
  await field.fill("28.50");
  await expect(echo).toContainText("A decimal point is not a paise figure.");
});

test("a price change goes out as integer paise, through the injection endpoint and nothing else", async ({
  page,
}) => {
  const product = await quietProduct(page);
  const original = product.unit_price_minor;
  const target = original + 100;

  const writes = recordWrites(page);
  try {
    await openEditor(page, product.sku);
    await page.getByLabel(`Unit price in paise for ${product.sku}`).fill(String(target));

    const sent = page.waitForRequest(
      (request) =>
        request.method() === "POST" && request.url().includes("/api/backend/v1/scenario/injections"),
    );
    await page.getByRole("button", { name: "Set price" }).click();
    const request = await sent;

    // ------------------------------------------------------- what went over the wire
    const body = request.postDataJSON() as { kind: string; sku: string; value: unknown };
    expect(body.kind).toBe("PRICE_SET");
    expect(body.sku).toBe(product.sku);
    expect(body.value).toBe(target);
    expect(
      Number.isInteger(body.value),
      `value went out as ${JSON.stringify(body.value)}, which is not an integer`,
    ).toBe(true);
    expect(typeof body.value, "value went out as a string").toBe("number");
    // Belt and braces on the raw bytes: no decimal point and no exponent reached the API,
    // whatever JSON.parse would have been willing to read back.
    expect(String(request.postData()).match(/"value"\s*:\s*(-?[\d.eE+]+)/)?.[1]).toBe(
      String(target),
    );

    // ------------------------------------------------------ what came back, rendered
    const applied = page.locator("section").filter({ hasText: "Injections applied from this console" });
    await expect(applied).toBeVisible();
    await expect(applied).toContainText("SCENARIO_INJECTION");
    await expect(applied).toContainText("PRICE_SET");
    await expect(applied).toContainText(product.sku);
    // The delta the API computed, not one the browser worked out.
    await expect(applied).toContainText(`unit_price_minor:`);
    await expect(applied).toContainText(String(original));
    await expect(applied).toContainText(String(target));
    await expect(applied).toContainText(/revision \d+ → \d+/);

    // ------------------------------------------------------------- and it really landed
    const after = await read<CataloguePage>(
      page,
      `/api/backend/v1/catalogue/products?available=false&limit=50`,
    );
    const row = after.products.find((candidate) => candidate.sku === product.sku);
    expect(row?.unit_price_minor, "the API did not take the price this console sent").toBe(target);

    // Exactly one write, to exactly one endpoint. Nothing local, nothing duplicated.
    expect(writes).toEqual(["POST /v1/scenario/injections"]);
  } finally {
    await setPrice(product.sku, original);
  }

  // The restore is asserted too: a suite that quietly left the catalogue moved would be
  // handing the next reader of this tenant a figure this file invented.
  const restored = await read<CataloguePage>(
    page,
    "/api/backend/v1/catalogue/products?available=false&limit=50",
  );
  expect(
    restored.products.find((candidate) => candidate.sku === product.sku)?.unit_price_minor,
  ).toBe(original);
});
