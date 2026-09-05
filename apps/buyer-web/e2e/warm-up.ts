/**
 * Compile every route the suite touches before the suite starts timing anything.
 *
 * A Next dev server compiles a route on its first request. `webServer.url` in the config
 * waits for the root to answer and nothing else, so the first spec to visit `/search`,
 * `/basket`, a checkout, or the `/api/backend` proxy paid for that route's compilation
 * inside its own assertion — and that assertion's timeout is sized for a server round trip,
 * not for a bundler.
 *
 * It became worth fixing when `reuseExistingServer` was turned off. That change is right —
 * a suite that quietly reuses whichever checkout bound the port first proves nothing about
 * the branch that produced it — but it means every run now starts a cold server, so what
 * used to be a once-a-day cost is paid on every run. On a busy machine that is the
 * difference between a slow test and a failed one.
 *
 * This is not a wait-for-good-luck. It makes no assertion, and it deliberately does not
 * fail the run: a route that will not compile fails loudly in the spec that needs it, with
 * that spec's own context, which is a better failure than one thrown out of a setup hook
 * that no test owns. All this does is move a known, irrelevant cost out of the measurement.
 *
 * Nothing here is stubbed and nothing is cached for the specs to read. Warming a route
 * leaves no state behind: these are reads, and the two writes the suite makes on a checkout
 * are made by the specs themselves.
 */
import { request, type FullConfig } from "@playwright/test";

/**
 * The routes the suite visits, and one that only exists to compile the proxy.
 *
 * The checkout route is warmed against a UUID that names nothing, so it compiles and then
 * renders its own "could not be read" state rather than opening a real checkout and taking
 * a stock reservation the suite would have to give back.
 */
const ROUTES = [
  "/",
  "/search?q=doodh",
  "/basket",
  "/orders",
  "/p/AMUL-DAIRY-001",
  "/checkout/01a06fae-0000-7000-8000-000000000000",
  "/api/backend/v1/catalogue/products?limit=1",
];

export default async function warmUp(config: FullConfig): Promise<void> {
  const baseURL =
    config.projects[0]?.use?.baseURL ?? process.env.BASE_URL ?? "http://localhost:3000";

  const context = await request.newContext({ baseURL });
  const started = Date.now();
  const slow: string[] = [];

  for (const route of ROUTES) {
    const at = Date.now();
    try {
      await context.get(route, { timeout: 120_000 });
    } catch {
      // Left for the spec that actually needs this route to report, in its own words.
      continue;
    }
    const took = Date.now() - at;
    if (took > 3_000) slow.push(`${route} ${(took / 1000).toFixed(1)}s`);
  }

  await context.dispose();

  // Said out loud, because a warm-up that took thirty seconds is worth knowing about when
  // reading the timings underneath it, and silence would hide the one thing this file is
  // in a position to observe.
  const total = ((Date.now() - started) / 1000).toFixed(1);
  console.log(
    `  warmed ${ROUTES.length} routes in ${total}s` +
      (slow.length > 0 ? ` (slowest: ${slow.join(", ")})` : ""),
  );
}
