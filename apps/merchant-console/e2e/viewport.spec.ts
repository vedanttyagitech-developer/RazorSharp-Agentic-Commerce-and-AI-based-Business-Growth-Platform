/**
 * The console below 1280px, because a judge will open it beside the storefront.
 *
 * The failure this guards against is specific and easy to reintroduce: a table of eight
 * columns that widens the *page* rather than scrolling inside its own box. The symptom is
 * not a missing column -- it is a body that scrolls sideways, which drags the header strip
 * and the navigation off the screen and leaves the reader dragging the whole console
 * around to read one figure. `TableWrap` exists to prevent exactly that, so the test
 * asserts the mechanism as well as the outcome: no page-level overflow, *and* every table
 * that is wider than its container sits inside something that scrolls.
 *
 * Asserting only "the page does not scroll sideways" would pass on a console that had
 * fixed the overflow by clipping the table, which loses the columns instead of the layout.
 * So the columns are counted too.
 *
 * 1279 is the interesting width because it is one pixel under the layout's own max-width;
 * 768 and 430 are there because a stakeholder will open this on a tablet or a phone at some
 * point and "not broken" should still mean readable.
 */
import { expect, test, type Page } from "@playwright/test";

import { platformUnreachable, REFUND_MEANINGS as MEANINGS } from "./support";

const WIDTHS = [1279, 1024, 900, 768, 430] as const;

const ROUTES = [
  { path: "/", heading: "Overview" },
  { path: "/evidence", heading: "Evidence" },
  { path: "/operations?tab=orders", heading: "Operations" },
  { path: "/operations?tab=refunds", heading: "Operations" },
  { path: "/operations?tab=outbox", heading: "Operations" },
  { path: "/operations?tab=safe-mode", heading: "Operations" },
  { path: "/catalogue", heading: "Catalogue" },
  { path: "/review", heading: "Human review" },
  { path: "/inspector", heading: "Inspector" },
] as const;

let unreachable: string | null = null;

test.beforeAll(async () => {
  unreachable = await platformUnreachable("the narrow-viewport suite");
});

test.beforeEach(() => {
  test.skip(unreachable !== null, unreachable ?? "");
});

/** How far the document scrolls sideways. Anything above zero is the bug. */
async function overflow(page: Page): Promise<number> {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
}

/**
 * Tables that are wider than the box they are in, and whether that box scrolls.
 *
 * A table wider than its container is fine and expected -- eight columns of identifiers do
 * not fit in 430px and should not be made to. What matters is that the overflow is absorbed
 * by an ancestor with `overflow-x: auto` rather than pushed out into the page.
 */
async function unscrollableWideTables(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const offenders: string[] = [];
    document.querySelectorAll("table").forEach((table, index) => {
      let node: HTMLElement | null = table.parentElement;
      let scrolls = false;
      while (node && node !== document.body) {
        const overflowX = getComputedStyle(node).overflowX;
        if (overflowX === "auto" || overflowX === "scroll") {
          scrolls = true;
          break;
        }
        node = node.parentElement;
      }
      const wider = table.scrollWidth > (table.parentElement?.clientWidth ?? 0) + 1;
      if (wider && !scrolls) offenders.push(`table ${index} (${table.scrollWidth}px) has no scrolling ancestor`);
    });
    return offenders;
  });
}

/**
 * Every element that sticks out past the right edge of the viewport with nothing absorbing
 * it, outermost first.
 *
 * The page-level `scrollWidth` check says *that* the layout broke; this says *what* broke
 * it, which is the difference between a failure someone can fix and a failure someone
 * reruns. It generalises the table check below to any element, because the thing most
 * likely to widen this console is not a table -- it is a long unbroken token. A checkout
 * id, a SKU, or a `proposal_id` like `prp_SjQFIcRnk9GZpa6jvkngT1` is 26 characters with no
 * break opportunity, and a mono span holding one without `overflow-wrap` will push its
 * container through the viewport at 430px. The console has a `.break-id` class
 * (`overflow-wrap: anywhere`) for exactly this, and the interesting question is which of
 * the places that render an identifier forgot to use it.
 *
 * An ancestor with `overflow-x` of `auto`, `scroll` or `hidden` absorbs the overflow and is
 * not a fault -- that is `TableWrap` and the copilot's `<pre>` blocks doing their job.
 * Descendants of an offender are dropped, so one wide element reports once rather than
 * once per cell inside it.
 */
async function overflowingElements(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const limit = document.documentElement.clientWidth;
    const offenders: Element[] = [];

    for (const element of Array.from(document.querySelectorAll("body *"))) {
      const box = element.getBoundingClientRect();
      if (box.width === 0 && box.height === 0) continue;
      if (box.right <= limit + 1) continue;

      let node = element.parentElement;
      let absorbed = false;
      while (node && node !== document.body) {
        const overflowX = getComputedStyle(node).overflowX;
        if (overflowX === "auto" || overflowX === "scroll" || overflowX === "hidden") {
          absorbed = true;
          break;
        }
        node = node.parentElement;
      }
      if (!absorbed) offenders.push(element);
    }

    // Outermost only: an element whose ancestor is already reported adds nothing.
    return offenders
      .filter((element) => !offenders.some((other) => other !== element && other.contains(element)))
      .map((element) => {
        const box = element.getBoundingClientRect();
        const classes = element.className && typeof element.className === "string"
          ? `.${element.className.split(/\s+/).slice(0, 3).join(".")}`
          : "";
        const text = (element.textContent ?? "").replace(/\s+/g, " ").trim().slice(0, 60);
        return `<${element.tagName.toLowerCase()}${classes}> right=${Math.round(box.right)} > ${limit} — "${text}"`;
      });
  });
}

for (const width of WIDTHS) {
  test.describe(`at ${width}px`, () => {
    test.use({ viewport: { width, height: 900 } });

    test("no page scrolls sideways, and every wide table scrolls inside itself", async ({
      page,
    }) => {
      for (const route of ROUTES) {
        await page.goto(route.path);
        // Waited for by content rather than by a timer: a page still on its loading state
        // has nothing wide on it yet and would pass this test by being empty.
        await expect(page.getByRole("heading", { name: route.heading, level: 1 })).toBeVisible();
        await expect(page.getByText(/reading \/v1\/config/)).toHaveCount(0);

        expect(
          await overflow(page),
          `${route.path} at ${width}px scrolls the page sideways, which drags the navigation off screen`,
        ).toBeLessThanOrEqual(1);

        expect(await unscrollableWideTables(page), `${route.path} at ${width}px`).toEqual([]);

        // And nothing else sticks out either -- named, so a failure says which element.
        expect(
          await overflowingElements(page),
          `${route.path} at ${width}px has elements past the right edge with nothing absorbing them`,
        ).toEqual([]);
      }
    });

    test("the navigation and the platform strip stay on screen", async ({ page }) => {
      await page.goto("/");
      await expect(page.getByRole("heading", { name: "Overview", level: 1 })).toBeVisible();

      // Every section still reachable. A console whose navigation wrapped off the viewport
      // would be one a judge could not get out of.
      //
      // The sections are read off the shell rather than listed here. A hardcoded list would
      // keep passing after a section was added to the navigation and never checked at any
      // width -- which is the same shape as the bug this suite found on the review queue,
      // and it would be poor form to reintroduce it in the test that found it.
      const nav = page.getByRole("navigation", { name: "Console sections" });
      const links = nav.getByRole("link");
      const sections = await links.allInnerTexts();
      expect(sections.length, "the shell renders no navigation at all").toBeGreaterThan(0);

      for (const [index, section] of sections.entries()) {
        const link = links.nth(index);
        await expect(link).toBeVisible();
        const box = await link.boundingBox();
        expect(box, `${section} has no box at ${width}px`).not.toBeNull();
        expect(box!.x, `${section} sits off the left edge at ${width}px`).toBeGreaterThanOrEqual(0);
        expect(box!.x + box!.width, `${section} runs off the right edge at ${width}px`).toBeLessThanOrEqual(
          width + 1,
        );
      }

      // The live platform facts are the reason to have the strip at all.
      await expect(page.getByText("db reachable")).toBeVisible();
    });

    test("the copilot dock opens without pushing the page sideways", async ({ page }) => {
      // The dock is `fixed right-0 bottom-0 w-full max-w-[620px]`, so closed it is a
      // launcher and proves almost nothing. Open is the surface that can break a layout,
      // and 430px is where a `min-w-` or an unbroken identifier would show.
      await page.goto("/");
      await expect(page.getByRole("heading", { name: "Overview", level: 1 })).toBeVisible();

      const opener = page.getByRole("button", { name: "Show the conversation" });
      await expect(opener, "the copilot dock is not on this page").toBeVisible();
      await opener.click();
      await expect(page.getByRole("button", { name: "Hide the conversation" })).toBeVisible();

      expect(
        await overflow(page),
        `the open copilot dock scrolls the page sideways at ${width}px`,
      ).toBeLessThanOrEqual(1);
      expect(
        await overflowingElements(page),
        `the open copilot dock puts elements past the right edge at ${width}px`,
      ).toEqual([]);

      // The dock must stay inside the viewport itself, not merely fail to scroll the body.
      // By attribute, not by role/label: `getByLabel` also matches the controls inside
      // the dock whose own names contain "Merchant Copilot".
      const dock = page.locator('[aria-label="Merchant Copilot"]');
      const box = await dock.boundingBox();
      expect(box, `the open dock has no box at ${width}px`).not.toBeNull();
      expect(box!.x, `the dock starts off the left edge at ${width}px`).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width, `the dock runs off the right edge at ${width}px`).toBeLessThanOrEqual(
        width + 1,
      );

      // An identifier with no break opportunity, typed into the composer. `proposal_id`
      // values look like this and are 26 characters; the console has `.break-id`
      // (`overflow-wrap: anywhere`) for them, and the question is whether every path that
      // renders one uses it. This covers the input path deterministically, without firing
      // a turn -- see the report for why the reply path is not covered here.
      await page.locator("#copilot-composer").fill("prp_SjQFIcRnk9GZpa6jvkngT1");
      expect(
        await overflow(page),
        `a long identifier in the composer scrolls the page sideways at ${width}px`,
      ).toBeLessThanOrEqual(1);
      expect(await overflowingElements(page)).toEqual([]);
    });

    test("a reply carrying an unbroken identifier does not widen the page", async ({ page }) => {
      /**
       * The gap the composer test could not reach: a reply is rendered by a different
       * element from the one an operator types into, and `whitespace-pre-wrap` wraps only
       * at break *opportunities*, which an identifier does not offer.
       *
       * Two things measured here that are worth stating, because both correct an
       * assumption I made and then repeated:
       *
       *  - **The symptom is clipping, not a wide page.** The dock panel carries
       *    `overflow-hidden`, so a token that will not wrap is cut off inside the bubble
       *    rather than pushed into the layout. A test watching only the viewport passes
       *    just as happily with the wrapping removed -- I wrote that test first, and it
       *    would have shipped the bug it was written for.
       *  - **The token has to be long enough to matter.** At 430px the bubble is 378px, so
       *    a 26-character `proposal_id` and even a 43-character receipt hash fit with or
       *    without the wrapping class. The guard only becomes observable past about 43
       *    characters, which is why the reply below carries a deliberately longer one. No
       *    identifier this platform currently mints is that long, so `break-words` here is
       *    protective rather than load-bearing today -- and a test using a realistic id
       *    would assert nothing at all.
       *
       * This is the one place in the suite that fulfils a request instead of letting it
       * reach the platform, and the distinction from `failure-path.spec.ts` is worth
       * stating rather than assuming. There, stubbing was refused because the claim was
       * "the console *produces* a 503 when the platform is gone", and a fulfilled 503
       * would have proved only that it renders one it was handed. Here the claim is
       * geometry: given a reply of this shape, does the layout hold. The provenance of the
       * text is irrelevant to that, and the alternative -- a real turn -- is an LLM round
       * trip that is non-deterministic and writes a proposal into shared tenant state.
       *
       * A browser measuring the real box is also strictly stronger than the unit test
       * beside this one, which can only assert that the class is present because jsdom
       * cannot lay anything out.
       */
      // A real proposal id, and a token past the width at which wrapping starts to matter.
      const IDENTIFIER = "prp_SjQFIcRnk9GZpa6jvkngT1";
      const LONG = `${IDENTIFIER}${"x".repeat(60)}`;
      await page.route("**/api/backend/v1/merchant/agent/turn", (route) =>
        route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            reply: `The proposal is ${LONG} and it restocks AMUL-DAIRY-004.`,
            language: "en",
            specialist: "growth",
            routing_reason: "catalogue question",
            principal_id: "operator",
            tool_calls: [],
            denials: [],
            structured: null,
          }),
        }),
      );

      await page.goto("/");
      await page.getByRole("button", { name: "Show the conversation" }).click();

      // The question is the identifier itself, so the user bubble is stressed too -- it
      // carries `max-w-[85%]` and is the narrower of the two.
      await page.locator("#copilot-composer").fill(IDENTIFIER);
      await page.getByRole("button", { name: "Send to the Merchant Copilot" }).click();

      // Both bubbles on screen before measuring: an empty transcript would pass trivially.
      await expect(page.getByText(`The proposal is ${LONG}`)).toBeVisible();

      // The page does not widen -- but on its own that assertion is worthless here, and
      // finding out why was the useful part. The dock panel carries `overflow-hidden`, so
      // an identifier that refuses to wrap is *clipped* rather than pushed into the page.
      // A test that only watched the viewport passed just as happily with the wrapping
      // removed, which makes it a test that would have shipped the bug.
      expect(await overflow(page)).toBeLessThanOrEqual(1);
      expect(await overflowingElements(page)).toEqual([]);

      // So the real assertion is that no bubble is wider than the box drawn around it.
      // `scrollWidth > clientWidth` on a element nothing can scroll means text the
      // merchant cannot read -- here, the back half of the identifier they asked about.
      const clipped = await page.evaluate(() => {
        const dock = document.querySelector('[aria-label="Merchant Copilot"]');
        if (!dock) return ["the copilot dock is not on this page"];
        return Array.from(dock.querySelectorAll("p, span, li, dd, div"))
          .filter((element) => {
            // A screen-reader-only label is a 1px box by construction and is meant to
            // overflow itself. Skipping degenerate boxes rather than the `sr-only` class,
            // so a different visually-hidden technique is skipped too.
            if (element.clientWidth <= 1 || element.clientHeight <= 1) return false;
            const overflowX = getComputedStyle(element).overflowX;
            // `auto`/`scroll` is reachable by scrolling and is not clipping -- that is the
            // proposal card's <pre> blocks doing their job.
            if (overflowX === "auto" || overflowX === "scroll") return false;
            return element.scrollWidth > element.clientWidth + 1;
          })
          .map((element) => {
            const text = (element.textContent ?? "").replace(/\s+/g, " ").trim().slice(0, 50);
            return `<${element.tagName.toLowerCase()}> ${element.scrollWidth}px in ${element.clientWidth}px — "${text}"`;
          });
      });
      expect(
        clipped,
        `text is cut off inside the copilot dock at ${width}px — an identifier that will not wrap`,
      ).toEqual([]);
    });

    test("the figures an operator came for are still readable", async ({ page }) => {
      // Narrow must mean reflowed, never truncated: a count that has been clipped to fit is
      // a wrong number, and this console would rather wrap than round.
      await page.goto("/operations?tab=refunds");
      await expect(page.getByRole("heading", { name: "Operations", level: 1 })).toBeVisible();

      // The meaning sentence is what carries the instruction; if it has been dropped to save
      // width then colour is doing the work alone, which is the one thing the refund
      // vocabulary must never rely on.
      //
      // `.first()` because each sentence legitimately appears twice on a populated tenant --
      // once on the triage tile and once on the row it describes -- and which of the two
      // has painted first is a race this test has no business asserting on.
      for (const meaning of Object.values(MEANINGS)) {
        const tile = page.getByText(meaning).first();
        await expect(tile).toBeVisible();
      }
      for (const state of Object.keys(MEANINGS)) {
        await expect(page.getByText(state, { exact: true }).first()).toBeVisible();
      }
    });
  });
}
