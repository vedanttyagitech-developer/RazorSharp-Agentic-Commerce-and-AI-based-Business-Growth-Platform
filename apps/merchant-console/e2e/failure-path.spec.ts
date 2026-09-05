/**
 * The single most important test in this application: what every page shows when it cannot
 * read anything at all.
 *
 * The console's predecessor was deleted for fabricating an engaged Safe Mode out of a
 * request that had failed. That is the failure this file exists to make impossible to
 * reintroduce, and it is not provable from the happy path: a page that renders a plausible
 * zero over a dead upstream looks identical, on a working machine, to a page that renders
 * the truth. It has to be run against a broken console.
 *
 * So these specs address the second server `playwright.config.ts` starts -- the same build,
 * a different process, `COMMERCE_API_URL` pointed at the discard port. Every read through
 * its proxy fails to mint an operator session and comes back as a 503 problem document.
 * Nothing here is stubbed, intercepted or faked: the requests are real, they are attempted,
 * and they genuinely have nowhere to go. A `route.fulfill` would have been easier and would
 * have proved something weaker -- that the console renders a 503 it was handed, rather than
 * that the console produces one when the platform is gone.
 *
 * What is asserted, on each of the six pages, is three claims rather than one:
 *
 *  1. The failure is *visible* -- a panel saying the read failed, carrying the status and
 *     the problem the API gave.
 *  2. No figure survives it. Asserted by scraping every number off the rendered page and
 *     requiring the set to be empty, which is stronger and much less forgiving than
 *     checking that a particular tile is absent: it catches a zero nobody thought to look
 *     for, in a panel nobody remembered was there.
 *  3. It is not blank. A panel that renders nothing at all is its own kind of lie -- an
 *     operator reads an empty queue rather than an unreadable one -- so each page must
 *     still say what it could not read.
 */
import { expect, test, type Page } from "@playwright/test";

import { REFUND_MEANINGS } from "./support";

const DEAD_PORT = Number(process.env.CONSOLE_DEAD_PORT ?? 3102);
const DEAD = `http://127.0.0.1:${DEAD_PORT}`;

/**
 * The six routes, and what each one names as the thing it could not read.
 *
 * `/operations` appears four times because its tabs are four different reads and only the
 * selected one is mounted -- a suite that opened the default tab would leave three
 * unexercised.
 */
const ROUTES = [
  { path: "/", what: "the operating mode", heading: "Overview" },
  { path: "/evidence", what: "the operator session", heading: "Evidence" },
  { path: "/operations?tab=orders", what: "the order list", heading: "Operations" },
  { path: "/operations?tab=refunds", what: "the refund list", heading: "Operations" },
  { path: "/operations?tab=outbox", what: "the durable outbox", heading: "Operations" },
  { path: "/operations?tab=safe-mode", what: "the operating mode", heading: "Operations" },
  { path: "/catalogue", what: "the catalogue page", heading: "Catalogue" },
  { path: "/review", what: "the review queue", heading: "Human review" },
  { path: "/inspector", what: "the recent order list", heading: "Inspector" },
] as const;

/**
 * Every number rendered on the page, ignoring the places a numeral is not a figure.
 *
 * The endpoint paths in the panel subtitles carry a `/v1/`, the problem panels carry the
 * status `503` and the port in the detail sentence, and the console's own chrome is
 * excluded because the header strip and footer are not where an operator reads a queue
 * depth. What is left is the figures: a tile, a count, an amount. On a dead upstream there
 * must not be any.
 */
async function figuresOn(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const text = Array.from(
      document.querySelectorAll("main"),
      (node) => (node as HTMLElement).innerText,
    ).join("\n");
    return (
      text
        .split("\n")
        .map((line) => line.trim())
        // A line that is nothing but a number is a figure: `Figure`, the count tiles and
        // the amount columns all render one in a block of its own. The numerals inside the
        // problem document -- its 503, the port in its detail -- sit inline beside words,
        // so they are never a line on their own and are not caught here. That is the right
        // distinction: the problem document is reporting on itself, not stating a fact
        // about the tenant.
        .filter((line) => /^[₹$€]?[-−+]?[\d,]+(\.\d+)?$/.test(line))
    );
  });
}

/** The console's own account of a failed read, as `ProblemPanel` renders it. */
async function assertProblemPanel(page: Page, what: string): Promise<void> {
  const panel = page.locator('[role="alert"]').filter({ hasText: "READ FAILED" }).first();
  await expect(panel).toBeVisible();
  await expect(panel).toContainText(what);
  // The status and the API's own title, not a friendly sentence over the top of them. An
  // operator staring at this screen is the person who has to tell "the API is down" from
  // "the key is wrong" from "the row does not exist", and those look identical behind
  // "something went wrong".
  await expect(panel).toContainText("503");
  await expect(panel).toContainText("The platform is not reachable");
  await expect(panel).toContainText(
    "Nothing is shown in place of these figures. This console has no fixtures to fall back to.",
  );
}

test.describe("every page over a dead upstream", () => {
  test.use({ baseURL: DEAD });

  test("the broken console really is broken, and says so in a problem document", async ({
    page,
  }) => {
    // Proving the premise before relying on it. If this server could reach an API, every
    // other test in this file would be asserting nothing at all -- and would still pass.
    const response = await page.request.get("/api/backend/v1/config");
    expect(
      response.status(),
      "the failure-path server reached an API. It is supposed to be pointed at a dead " +
        "port by playwright.config.ts; check CONSOLE_DEAD_PORT and the server's env.",
    ).toBe(503);
    const problem = await response.json();
    expect(problem.title).toBe("The platform is not reachable");
    expect(response.headers()["content-type"]).toContain("application/problem+json");
  });

  for (const route of ROUTES) {
    test(`${route.path} renders the failure rather than a figure`, async ({ page }) => {
      await page.goto(route.path);

      // Not blank: the page frame is still there and still names itself.
      await expect(page.getByRole("heading", { name: route.heading, level: 1 })).toBeVisible();

      await assertProblemPanel(page, route.what);

      // And not a number anywhere an operator would read one.
      const figures = await figuresOn(page);
      expect(
        figures,
        `${route.path} rendered figures over a failed read: ${JSON.stringify(figures)}`,
      ).toEqual([]);
    });
  }

  test("the header strip says the platform is unreachable rather than printing defaults", async ({
    page,
  }) => {
    await page.goto("/");
    // "development / razorpay test / db reachable / mode NORMAL" printed over a failed
    // `GET /v1/config` would be four confident claims about a platform this process cannot
    // reach, and the last of them is a kill switch drawn as an assurance.
    await expect(page.getByText("platform unreachable · 503")).toBeVisible();
    for (const claim of ["mode NORMAL", "SAFE MODE", "db reachable", "db unreachable"]) {
      await expect(page.getByText(claim, { exact: true })).toHaveCount(0);
    }
  });

  test("an empty collection and an unreadable one never look the same", async ({ page }) => {
    // The distinction the whole file turns on. `/operations?tab=refunds` says one thing
    // when the API answers with an empty list and a different thing when it does not
    // answer; the sentence for the first must not appear over the second.
    await page.goto("/operations?tab=refunds");
    await assertProblemPanel(page, "the refund list");
    await expect(page.getByText("No refund matches this filter")).toHaveCount(0);
    await expect(
      page.getByText("The API answered with counts, so this is an empty result"),
    ).toHaveCount(0);

    // The three triage tiles still name the three states -- that vocabulary is this
    // console's own and does not depend on a read -- but the figure on each must be an em
    // dash. A zero here is the exact shape of the bug this file exists for: it reads as
    // "no refunds are pending" when the truth is "nobody knows how many are".
    for (const meaning of Object.values(REFUND_MEANINGS)) {
      const tile = page.getByText(meaning).locator("..");
      await expect(tile).toContainText("—");
    }
    // The state names survive because they are this console's vocabulary rather than a
    // read: each appears twice, once as a filter chip and once on its tile.
    for (const state of Object.keys(REFUND_MEANINGS)) {
      expect(await page.getByText(state, { exact: true }).count()).toBeGreaterThan(0);
    }

    await page.goto("/review");
    await assertProblemPanel(page, "the review queue");
    await expect(page.getByText("Nothing has been escalated")).toHaveCount(0);
    await expect(page.locator("main span").filter({ hasText: /^P\d+ · / })).toHaveCount(0);

    await page.goto("/catalogue");
    await assertProblemPanel(page, "the catalogue page");
    await expect(page.getByText("No product matches this filter")).toHaveCount(0);
    await expect(page.getByText(/matched by this filter/)).toHaveCount(0);
    await expect(page.getByText(/^revision /)).toHaveCount(0);
  });

  test("a failed read offers the operator a way to ask again", async ({ page }) => {
    await page.goto("/");
    // The panel is a dead end otherwise: the operator's next question is "is it back yet",
    // and the answer must not be "reload the whole console and lose where you were".
    await assertProblemPanel(page, "the operating mode");
    // `toBeVisible` rather than a bare `count()`, which does not retry: reading the count
    // the instant the navigation resolves asks the question before the read has failed.
    const retry = page.getByRole("button", { name: "Read again" });
    await expect(retry.first()).toBeVisible();
    await expect(retry.first()).toBeEnabled();
  });
});
