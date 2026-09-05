/**
 * The human-review queue, which nothing had ever driven.
 *
 * Two things make this page worth its own file.
 *
 * The first is that it is gated differently from everything else. `/v1/review` requires
 * the scenario key at the router, and the console's proxy attaches that key only to an
 * allowlist of path prefixes. Review was missing from that list once and every request
 * answered 401 while the API was perfectly healthy -- the queue rendering "could not be
 * read" with nothing wrong upstream. So the first test asserts the status code through the
 * proxy and says, when it is 401, exactly which list to go and look at. A test that only
 * checked "the page shows an error panel" would have passed against that bug, because an
 * error panel is what the bug produced.
 *
 * The second is that this page is deliberately read-only, and the absence of a control is
 * a claim that has to be tested like any other. P0 ships the queue and the evidence, not a
 * resolution workflow: an assign button that did nothing would tell a reviewer the case was
 * settled here when it was not, which is worse than no button. Absence is only provable by
 * enumerating what is on the page, so that is what the last test does rather than looking
 * for three names it expects to be missing.
 *
 * The queue is empty on a seeded tenant and the honest thing is to say so: an empty queue
 * is the platform reporting that reconciliation settled everything it saw. Every test here
 * asserts the API's own answer, so the same file covers a populated queue on a machine
 * where one has been driven in -- see the report accompanying this suite for why this
 * suite does not drive one in itself.
 */
import { expect, test } from "@playwright/test";

import { count, platformUnreachable, read, rupees, type Queue } from "./support";

let unreachable: string | null = null;

test.beforeAll(async () => {
  unreachable = await platformUnreachable("the human-review queue suite");
});

test.beforeEach(() => {
  test.skip(unreachable !== null, unreachable ?? "");
});

test("the queue is readable through the proxy, which must attach the scenario key to it", async ({
  page,
}) => {
  const response = await page.request.get("/api/backend/v1/review/queue?limit=50");
  expect(
    response.status(),
    "GET /v1/review/queue answered 401 through this console's proxy. The API gates that " +
      "router on the scenario key and the proxy attaches it only to the prefixes in " +
      "SCENARIO_KEY_PATHS (src/app/api/backend/[...path]/route.ts). Check that `v1/review/` " +
      "is still in that list -- an allowlist is the right shape, but it is a list, and a " +
      "list is a thing that falls behind.",
  ).not.toBe(401);
  expect(response.ok(), `GET /v1/review/queue answered ${response.status()}`).toBeTruthy();
});

test("/review renders the queue the API answered with", async ({ page }) => {
  await page.goto("/review");
  await expect(page.getByRole("heading", { name: "Human review", level: 1 })).toBeVisible();

  const queue = await read<Queue>(page, "/api/backend/v1/review/queue?limit=50");

  // Nothing on this page failed to read. READ FAILED is the chip a ProblemPanel wears and
  // it is the only place in the console that phrase appears.
  await expect(page.getByText("READ FAILED")).toHaveCount(0);
  await expect(page.getByRole("alert")).toHaveCount(0);

  await expect(page.getByText(`scope ${queue.scope} · showing up to ${queue.limit}`)).toBeVisible();

  if (queue.cases.length === 0) {
    // An empty queue is a fact about the platform, not a blank screen: reconciliation
    // opens a case only when it cannot settle a difference itself, so this sentence is
    // the API's answer rendered rather than an absence of one.
    await expect(
      page.getByText("Nothing has been escalated for a person to decide"),
    ).toBeVisible();
    await expect(
      page.getByText("an empty queue is the platform reporting that it settled everything it saw"),
    ).toBeVisible();
    return;
  }

  // A populated queue: every case the API sent is on the screen, with the facts a reviewer
  // triages on. Reached on a machine where a case has been escalated.
  for (const row of queue.cases) {
    const card = page.getByRole("button", { name: new RegExp(row.reason_code) }).first();
    await expect(card).toContainText(row.priority);
    await expect(card).toContainText(row.reason_family);
    await expect(card).toContainText(`opened by ${row.opened_by}`);
    await expect(card).toContainText(
      `${row.detections} detection${row.detections === 1 ? "" : "s"}`,
    );
    // Exposure is money, so absent must not render as zero.
    if (row.monetary_exposure) {
      await expect(card).toContainText(
        `${rupees(row.monetary_exposure.minor, row.monetary_exposure.currency)} at stake`,
      );
    } else {
      await expect(card).toContainText("no monetary exposure recorded");
    }
  }
});

test("the priority chips carry the counts the API sent, and no priority it did not", async ({
  page,
}) => {
  await page.goto("/review");
  const queue = await read<Queue>(page, "/api/backend/v1/review/queue?limit=50");

  const chips = page.locator("main span").filter({ hasText: /^P\d+ · [\d,]+$/ });
  await expect(chips).toHaveCount(Object.keys(queue.priority_counts).length);

  // Every priority the API counted is shown, zero included: "no P1 cases" and "P1 not
  // shown" are different claims about a queue a person is working down.
  for (const [priority, value] of Object.entries(queue.priority_counts)) {
    await expect(chips.filter({ hasText: `${priority} · ${count(value)}` })).toHaveCount(1);
  }

  // And nothing else. A chip reading "P4 · 0" over a platform whose Priority enum has
  // three members is a figure that was not read from the API in this page load -- the one
  // thing this console is built never to render. It is also the more dangerous direction
  // of the two: a reviewer reads it as a tier that exists and is quiet.
  const shown = await chips.allTextContents();
  const invented = shown
    .map((text) => text.split(" · ")[0].trim())
    .filter((priority) => !(priority in queue.priority_counts));
  expect(
    invented,
    "these priorities are rendered from a hardcoded list rather than from priority_counts",
  ).toEqual([]);
});

test("the queue offers no assign, no decide and no resolve", async ({ page }) => {
  await page.goto("/review");
  const queue = await read<Queue>(page, "/api/backend/v1/review/queue?limit=50");

  // Stated on the page, because a reviewer who does not know where a case is settled will
  // look for the control until they find something that looks like one.
  await expect(
    page.getByText("Nothing here resolves a case: a reviewer acts elsewhere"),
  ).toBeVisible();

  // Enumerated rather than searched for by name. Asking whether a button called "Assign"
  // exists proves nothing about a button called "Take", so the test reads every control on
  // the page and requires each one to be a case card and nothing else.
  const controls = page.locator("main button, main input, main select, main textarea");
  const names = await controls.evaluateAll((nodes) =>
    nodes.map((node) => (node.textContent ?? "").replace(/\s+/g, " ").trim()),
  );
  // The only buttons this page renders are the case cards, one per case the API sent.
  expect(names).toHaveLength(queue.cases.length);

  // No form to submit and no route to a mutation, whatever a control might be labelled.
  await expect(page.locator("main form")).toHaveCount(0);

  const writes: string[] = [];
  page.on("request", (request) => {
    if (request.method() !== "GET" && request.url().includes("/api/backend/")) {
      writes.push(`${request.method()} ${request.url()}`);
    }
  });
  if (queue.cases.length > 0) {
    // Open a case and read its evidence. Selecting is the only interaction this surface
    // has, and it must still not write.
    await page.getByRole("button", { name: new RegExp(queue.cases[0].reason_code) }).first().click();
    await expect(page.getByText("Blocked on a person")).toBeVisible();
    await expect(
      page.getByText("Nothing on this screen resolves it, assigns it or records a decision"),
    ).toBeVisible();
  }
  expect(writes, "the review surface performed a write").toEqual([]);
});
