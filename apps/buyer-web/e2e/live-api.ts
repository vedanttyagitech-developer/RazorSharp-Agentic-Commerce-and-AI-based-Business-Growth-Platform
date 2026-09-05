/**
 * What the end-to-end suite needs from the Commerce API directly, rather than through
 * the browser.
 *
 * The storefront never holds a bearer token: it talks to its own origin at
 * `/api/backend/...` and the route handler attaches the credential. That is right for the
 * app and useless for a test that has to reach past the browser to move the merchant's
 * prices, so this module mints its own operator-side session and talks to
 * `http://127.0.0.1:8000` as a second actor. It is deliberately the *only* place in the
 * suite that does; every assertion about what a buyer sees is made against the page, and
 * every number those assertions expect is read back through the app's own proxy with the
 * browser's own cookie, so the test never states a figure the platform did not send.
 *
 * Not a test file: Playwright collects `*.spec.ts`, so this is imported, never run.
 */
import type { APIRequestContext } from "@playwright/test";

export const API_BASE = process.env.COMMERCE_API_URL ?? "http://127.0.0.1:8000";
export const SCENARIO_KEY = process.env.SCENARIO_KEY ?? "local-demo-scenario-key";
export const TENANT_SLUG = process.env.NEXT_PUBLIC_TENANT_SLUG ?? "demo";

/** Said once, in full, wherever the suite skips. A skipped test has to explain itself. */
export const UNREACHABLE =
  `The Commerce API at ${API_BASE} did not answer. This suite drives the real platform — ` +
  "it opens real checkouts and moves real merchant prices — and there is nothing honest " +
  "for it to assert against a storefront with no server behind it. Start the API " +
  "(and its worker) and run again.";

/**
 * Whether the API is up.
 *
 * `/healthz` rather than a business route, so a tenant that has not been seeded fails
 * later with a message about seeding rather than here with a message about the network.
 */
export async function apiIsReachable(request: APIRequestContext): Promise<boolean> {
  try {
    const response = await request.get(`${API_BASE}/healthz`, { timeout: 5000 });
    return response.ok();
  } catch {
    return false;
  }
}

/**
 * The same check, said out loud when it fails.
 *
 * `test.skip` records its reason where only a JSON or HTML reporter shows it, and the
 * list reporter this suite runs under prints a bare dash. A person who started the suite
 * with the API down would then see five silent dashes and no clue why, which is the
 * obscure failure this is here to prevent, so the reason is written to the console once
 * per spec file whether or not the reporter would have carried it.
 */
export async function requireApi(request: APIRequestContext): Promise<boolean> {
  const up = await apiIsReachable(request);
  if (!up) console.warn(`\n  SKIPPING every test in this file. ${UNREACHABLE}\n`);
  return up;
}

/** A buyer session token for the seeded tenant. The browser gets its own, separately. */
export async function mintToken(request: APIRequestContext): Promise<string> {
  const response = await request.post(`${API_BASE}/v1/demo/sessions`, {
    data: { tenant_slug: TENANT_SLUG, actor_type: "BUYER" },
  });
  if (!response.ok()) {
    throw new Error(`could not mint a demo session: ${response.status()} ${await response.text()}`);
  }
  const body = (await response.json()) as { token?: unknown };
  if (typeof body.token !== "string") throw new Error("the demo session carried no token");
  return body.token;
}

interface CataloguePrice {
  sku: string;
  unitPriceMinor: number;
  displayName: string;
}

/** What the merchant is charging for a SKU right now, in integer paise. */
export async function currentPrice(
  request: APIRequestContext,
  token: string,
  sku: string,
): Promise<CataloguePrice> {
  const response = await request.get(`${API_BASE}/v1/catalogue/search`, {
    params: { q: sku, limit: 5 },
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok()) throw new Error(`catalogue search failed: ${response.status()}`);
  const body = (await response.json()) as {
    hits: Array<{ sku: string; unit_price_minor: number; display_name: string }>;
  };
  const hit = body.hits.find((candidate) => candidate.sku === sku);
  if (!hit) throw new Error(`the catalogue has no ${sku}; is the demo tenant seeded?`);
  return { sku, unitPriceMinor: hit.unit_price_minor, displayName: hit.display_name };
}

export interface PriceInjection {
  beforeMinor: number;
  afterMinor: number;
  revisionAfter: number;
}

/**
 * Move a SKU's price, the way the demo script does.
 *
 * The endpoint answers 409 when the value it is handed is the one already in force, so
 * the new price is derived from the price the merchant is charging at this instant rather
 * than from a constant: two runs of this suite an hour apart must not collide, and nor
 * must the two viewport projects within one run.
 */
export async function injectPrice(
  request: APIRequestContext,
  token: string,
  sku: string,
  valueMinor: number,
  note: string,
): Promise<PriceInjection> {
  const response = await request.post(`${API_BASE}/v1/scenario/injections`, {
    headers: {
      Authorization: `Bearer ${token}`,
      "X-Scenario-Key": SCENARIO_KEY,
      "Idempotency-Key": `e2e-${Date.now()}-${Math.random().toString(36).slice(2)}`,
    },
    data: { kind: "PRICE_SET", sku, value: valueMinor, note },
  });
  if (!response.ok()) {
    throw new Error(`the price injection was refused: ${response.status()} ${await response.text()}`);
  }
  const body = (await response.json()) as {
    deltas: Array<{ field: string; before: number; after: number }>;
    revision_after: number;
  };
  const moved = body.deltas.find((delta) => delta.field === "unit_price_minor");
  if (!moved) throw new Error("the injection reported no unit price change");
  return { beforeMinor: moved.before, afterMinor: moved.after, revisionAfter: body.revision_after };
}

/**
 * Put a price back where the suite found it, best effort.
 *
 * The seeded catalogue is shared with whoever runs the demo next, and a suite that walks
 * away leaving milk permanently dearer has changed the thing it was measuring. A failure
 * to restore is swallowed on purpose: it is tidying, not an assertion, and turning it
 * into one would fail runs for a reason no reader would connect to the test.
 */
export async function restorePrice(
  request: APIRequestContext,
  token: string,
  sku: string,
  valueMinor: number,
): Promise<void> {
  try {
    await injectPrice(request, token, sku, valueMinor, "e2e teardown: restoring the seeded price");
  } catch {
    // Nothing to do about it, and nothing worth failing a green run over.
  }
}

/**
 * Integer paise as this storefront renders them: `9224` -> `₹92.24`.
 *
 * Written out here rather than imported from `src/lib/money.ts` on purpose. A test that
 * formats its expectations with the code under test asserts only that the code agrees
 * with itself; the unit suite pins the formatter exhaustively, and this second, separate
 * spelling is what makes the end-to-end assertion an independent check of the figure.
 */
export function rupees(minor: number): string {
  const negative = minor < 0;
  const absolute = Math.abs(minor);
  const units = (absolute - (absolute % 100)) / 100;
  const paise = absolute % 100;
  const grouped = new Intl.NumberFormat("en-IN", { useGrouping: true }).format(units);
  return `${negative ? "-" : ""}₹${grouped}.${String(paise).padStart(2, "0")}`;
}

/** `+₹6.74` / `−₹6.74`, with the true minus sign the refusal card uses. */
export function signedRupees(minor: number): string {
  if (minor === 0) return rupees(0);
  return `${minor > 0 ? "+" : "−"}${rupees(Math.abs(minor))}`;
}

/* ------------------------------------------- the merchant's other two levers */

/**
 * What the merchant holds and is charging for a SKU right now.
 *
 * `currentPrice` above answers the same question for money alone and is left as it is,
 * because two specs already read it and a widened return would make them say `.price`
 * where they say what they mean today.
 */
export async function currentStock(
  request: APIRequestContext,
  token: string,
  sku: string,
): Promise<{ unitPriceMinor: number; stockUnits: number; listed: boolean }> {
  const response = await request.get(`${API_BASE}/v1/catalogue/search`, {
    params: { q: sku, limit: 5 },
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok()) throw new Error(`catalogue search failed: ${response.status()}`);
  const body = (await response.json()) as {
    hits: Array<{ sku: string; unit_price_minor: number; stock_units: number; is_listed: boolean }>;
  };
  const hit = body.hits.find((candidate) => candidate.sku === sku);
  if (!hit) throw new Error(`the catalogue has no ${sku}; is the demo tenant seeded?`);
  return { unitPriceMinor: hit.unit_price_minor, stockUnits: hit.stock_units, listed: hit.is_listed };
}

/** One merchant-state injection of any kind, with the deltas it reported. */
export interface Injection {
  deltas: Array<{ field: string; before: unknown; after: unknown }>;
  revisionAfter: number;
}

/**
 * Move something about a SKU that is not its price.
 *
 * `PRICE_SET` keeps its own function above because a price injection has a return type
 * two specs already destructure. This one is the general form, and it is deliberately
 * not folded into that: the endpoint answers 409 when handed the value already in force,
 * so every caller has to read the current value first, and a shared helper that hid that
 * would invite a spec to set a constant and fail on its second run.
 */
export async function inject(
  request: APIRequestContext,
  token: string,
  kind: "STOCK_SET" | "AVAILABILITY_SET" | "SELL_OUT",
  sku: string,
  value: number | boolean | null,
  note: string,
): Promise<Injection> {
  const response = await request.post(`${API_BASE}/v1/scenario/injections`, {
    headers: {
      Authorization: `Bearer ${token}`,
      "X-Scenario-Key": SCENARIO_KEY,
      "Idempotency-Key": `e2e-${Date.now()}-${Math.random().toString(36).slice(2)}`,
    },
    data: value === null ? { kind, sku, note } : { kind, sku, value, note },
  });
  if (!response.ok()) {
    throw new Error(
      `the ${kind} injection was refused: ${response.status()} ${await response.text()}`,
    );
  }
  const body = (await response.json()) as {
    deltas: Array<{ field: string; before: unknown; after: unknown }>;
    revision_after: number;
  };
  return { deltas: body.deltas, revisionAfter: body.revision_after };
}

/**
 * Put a merchant-state value back, best effort and never an assertion.
 *
 * Same reasoning as `restorePrice`: the seeded catalogue is shared with whoever runs the
 * demo next, and a suite that walks away having sold out the milk has changed the thing
 * it was measuring. A 409 here means the value is already what it should be, which is
 * the outcome this function wanted, so it is swallowed along with everything else.
 */
export async function restore(
  request: APIRequestContext,
  token: string,
  kind: "STOCK_SET" | "AVAILABILITY_SET",
  sku: string,
  value: number | boolean,
): Promise<void> {
  try {
    await inject(request, token, kind, sku, value, "e2e teardown: restoring the seeded value");
  } catch {
    // Tidying, not an assertion. Failing a green run here would name nothing a reader could act on.
  }
}

/**
 * End a version's stock hold now, the way the troubleshooting table tells an operator to.
 *
 * Two specs need this for opposite reasons. One drives `RESERVATION_EXPIRED` deliberately,
 * because a hold that lapses between approval and payment is a refusal the storefront has
 * to render and no amount of waiting would produce it inside a test. The other uses it as
 * cleanup for a checkout the kernel would not cancel, so the suite hands the stock back
 * rather than leaving it held for fifteen minutes and refusing its own next run.
 */
export async function expireReservation(
  request: APIRequestContext,
  token: string,
  checkoutId: string,
  version: number,
): Promise<void> {
  await request.post(
    `${API_BASE}/v1/scenario/reservations/${encodeURIComponent(checkoutId)}/${version}/expire`,
    {
      headers: {
        Authorization: `Bearer ${token}`,
        "X-Scenario-Key": SCENARIO_KEY,
        "Idempotency-Key": `e2e-expire-${Date.now()}-${Math.random().toString(36).slice(2)}`,
      },
      data: {},
    },
  );
}
