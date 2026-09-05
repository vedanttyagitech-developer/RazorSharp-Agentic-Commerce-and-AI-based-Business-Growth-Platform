/**
 * The three refund states, against the tenant that holds exactly one of each.
 *
 * REFUND_PENDING, REFUND_UNKNOWN and REFUND_FAILED are three different facts about money.
 * Pending means the provider was asked and has not answered: the money is in flight and the
 * correct action is to wait. Unknown means the answer was lost: the money may or may not
 * have moved, and reconciliation owns the row rather than an operator. Failed means the
 * provider said no. An operator who reads either of the first two as the third retries the
 * refund, and the buyer is refunded twice.
 *
 * The unit suite already proves the component keeps them apart when handed three fixtures.
 * What it cannot prove is that the *running platform* and this console agree about which
 * row is which, so this file reads the live rows and asserts each one renders as its own
 * state -- which is the version of the claim that would actually have caught a mapping
 * error between the API's `state` and the console's vocabulary.
 *
 * "Different" is asserted as three separate claims, because each on its own can be
 * satisfied by a screen that still misleads:
 *
 *  1. Three different colours, so a glance down the column separates them.
 *  2. Three different sentences saying what the state means and who owns the row --
 *     because colour is not available to every reader, does not survive a greyscale
 *     projector or a printed page, and carries no instruction even when it is visible.
 *  3. The state's own name as text, so a screenshot of one row is unambiguous with no
 *     legend beside it.
 *
 * Claim 2 is the one that matters most and the one a redesign is most likely to drop.
 */
import { expect, test } from "@playwright/test";

import {
  orDash,
  platformUnreachable,
  read,
  refundTile,
  REFUND_MEANINGS as MEANINGS,
  rupees,
  type RefundsPage,
} from "./support";

const CRITICAL = Object.keys(MEANINGS);

let unreachable: string | null = null;

test.beforeAll(async () => {
  unreachable = await platformUnreachable("the refund vocabulary suite");
});

test.beforeEach(() => {
  test.skip(unreachable !== null, unreachable ?? "");
});

test("each live refund renders as its own state, with the sentence that owns it", async ({
  page,
}) => {
  await page.goto("/operations?tab=refunds");
  const refunds = await read<RefundsPage>(page, "/api/backend/v1/refunds?limit=25");
  test.skip(refunds.refunds.length === 0, "This tenant holds no refund to render.");

  for (const refund of refunds.refunds) {
    const row = page.locator("tbody tr").filter({ has: page.locator(`[title="${refund.refund_id}"]`) });
    await expect(row).toHaveCount(1);

    // The wire state, as text. Not inferred from the row status, not abbreviated.
    await expect(row).toContainText(refund.state);
    // `row_status` beside it, because they are different columns and an operator
    // reconciling against the database needs the second one.
    await expect(row).toContainText(`row_status ${refund.row_status}`);
    // The amount as the server stated it.
    await expect(row).toContainText(rupees(refund.amount.minor, refund.amount.currency));
    // What was captured, or an em dash. A refund with no order row behind it has no
    // captured amount, and rendering that as ₹0.00 would assert a fact nobody established.
    await expect(row).toContainText(orDash(refund.captured_minor, refund.currency));

    // And the sentence, on the row itself rather than only in a legend at the top.
    if (refund.state in MEANINGS) {
      await expect(row).toContainText(MEANINGS[refund.state]);
    }
  }
});

test("the tenant's one of each is three visibly different things", async ({ page }) => {
  await page.goto("/operations?tab=refunds");
  const refunds = await read<RefundsPage>(page, "/api/backend/v1/refunds?limit=25");

  const present = CRITICAL.filter((state) => (refunds.counts[state] ?? 0) > 0);
  test.skip(
    present.length < 2,
    `This tenant holds ${present.length} of the three critical refund states, so there is ` +
      "nothing to tell apart. Seed one of each to exercise this.",
  );

  // The triage tiles: one per state, each carrying its name, its meaning and its count.
  const tiles = CRITICAL.map((state) => refundTile(page, state));

  const colours: string[] = [];
  for (const [index, state] of CRITICAL.entries()) {
    const tile = tiles[index];
    await expect(tile).toContainText(state);
    await expect(tile).toContainText(MEANINGS[state]);
    await expect(tile).toContainText(String(refunds.counts[state] ?? 0));
    colours.push(
      await tile
        .locator("span")
        .first()
        .evaluate((node) => getComputedStyle(node).color),
    );
  }

  // 1. Three colours, all different.
  expect(new Set(colours).size, `two of the three states share a colour: ${colours.join(", ")}`).toBe(3);

  // 2. Three sentences, all different, and none of them a paraphrase that lost the
  //    instruction. Checked as set size rather than pairwise so a fourth state added later
  //    cannot quietly reuse one.
  expect(new Set(Object.values(MEANINGS)).size).toBe(3);

  // 3. And the names themselves, so colour is never the only carrier. Asserted by stripping
  //    every colour off the page and requiring the three to still be tellable apart.
  await page.emulateMedia({ forcedColors: "active" });
  for (const state of CRITICAL) {
    await expect(page.getByText(state, { exact: true }).first()).toBeVisible();
    await expect(page.getByText(MEANINGS[state]).first()).toBeVisible();
  }
});

test("pending and unknown never share a description, however the page is read", async ({
  page,
}) => {
  // The specific conflation that refunds a buyer twice. Both states mean "there is no
  // completed refund here", and only one of them is safe to leave alone and wait on -- so
  // the page must not describe them with the same words.
  await page.goto("/operations?tab=refunds");

  // Read back off the rendered page rather than compared as constants: two constants this
  // file declared differently are trivially different, and that would prove nothing about
  // what an operator is looking at.
  const pending = (await refundTile(page, "REFUND_PENDING").innerText()).replace(/\s+/g, " ").trim();
  const unknown = (await refundTile(page, "REFUND_UNKNOWN").innerText()).replace(/\s+/g, " ").trim();

  expect(pending, "the two tiles render the same text").not.toBe(unknown);
  expect(pending).toContain("REFUND_PENDING");
  expect(unknown).toContain("REFUND_UNKNOWN");
  // One says wait; the other says this row is not yours to act on. Those are different
  // instructions and losing either is how the retry happens.
  expect(pending, "the pending tile no longer says not to retry").toContain("do not retry");
  expect(unknown, "the unknown tile no longer says who owns the row").toContain(
    "reconciliation owns this row",
  );
  expect(unknown, "the unknown tile is telling an operator to wait, which is the pending advice").not.toContain(
    "do not retry",
  );
});
