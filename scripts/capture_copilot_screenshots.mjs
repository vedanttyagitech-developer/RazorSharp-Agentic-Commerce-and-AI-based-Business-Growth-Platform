/**
 * Screenshots of the copilot, taken by driving it the way a buyer does.
 *
 * Separate from `capture_screenshots.mjs`, which photographs the storefront pages and
 * resets the demo catalogue on the way in. This one resets nothing: it opens a fresh
 * session, talks to the shop, and photographs what comes back. Anything it captures is
 * therefore a real screen produced by the real stack, which is the only kind of screenshot
 * worth putting in a README -- a mocked one is a drawing of a claim.
 *
 * Run with the stack up:
 *
 *     node scripts/capture_copilot_screenshots.mjs
 *
 * `--headed` shows the browser. `--only=home,store` captures a subset while iterating.
 */

import { createRequire } from "node:module";
import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = resolve(HERE, "..", "docs", "images");
const BASE = process.env.COPILOT_BASE_URL ?? "http://localhost:3000";
const HEADED = process.argv.includes("--headed");
const ONLY = (process.argv.find((arg) => arg.startsWith("--only=")) ?? "").slice(7);

/** Wide enough for the conversation and the cart to sit side by side, as the demo is run. */
const VIEWPORT = { width: 1280, height: 900 };

/**
 * Playwright, resolved from the storefront's own install.
 *
 * It is a dependency of `apps/buyer-web` rather than of this repository root, so a plain
 * require from `scripts/` finds nothing. Anchoring on the app's `package.json` is what
 * `capture_screenshots.mjs` does, for the same reason.
 */
function loadChromium() {
  const anchor = resolve(HERE, "..", "apps", "buyer-web", "package.json");
  const require = createRequire(anchor);
  try {
    return require("playwright-core");
  } catch {
    return require("playwright");
  }
}

/**
 * Say something to the copilot, exactly as the composer does.
 *
 * Through the input's own React setter and the form's submit, rather than by calling into
 * the app: a capture that bypassed the composer could photograph a state a buyer cannot
 * actually reach.
 */
async function say(page, text) {
  await page.evaluate((sentence) => {
    const input = document.getElementById("copilot-composer");
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype,
      "value",
    ).set;
    setter.call(input, sentence);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.closest("form").requestSubmit();
  }, text);
}

/** Wait for a sentence to appear in the transcript, or give up and say so. */
async function waitForReply(page, pattern, timeout = 30_000) {
  try {
    await page.waitForFunction(
      (source) => new RegExp(source).test(document.body.innerText),
      pattern.source,
      { timeout },
    );
    return true;
  } catch {
    return false;
  }
}

/** Wait until a region actually contains something, not merely until a reply mentioned it. */
async function settled(page, selector, pattern, timeout = 30_000) {
  try {
    await page.waitForFunction(
      ([sel, source]) => {
        const region = document.querySelector(sel);
        return region !== null && new RegExp(source).test(region.innerText);
      },
      [selector, pattern.source],
      { timeout },
    );
    return true;
  } catch {
    return false;
  }
}

/**
 * Wait until the copilot is listening again.
 *
 * `send` refuses while a turn is in flight, so a capture that types its next sentence
 * straight after the last one silently loses it -- which is exactly how the approval shot
 * went missing: the shop was still finding words for the add.
 */
async function idle(page, timeout = 30_000) {
  try {
    await page.waitForFunction(
      () => document.querySelector('li[aria-label="Working on it"]') === null,
      undefined,
      { timeout },
    );
    return true;
  } catch {
    return false;
  }
}

async function shoot(page, name, note) {
  const file = resolve(OUT, `${name}.png`);
  await page.screenshot({ path: file });
  console.log(`  ${name}.png — ${note}`);
  return { name, note };
}

async function main() {
  const wanted = ONLY ? new Set(ONLY.split(",")) : null;
  const want = (name) => wanted === null || wanted.has(name);

  await mkdir(OUT, { recursive: true });
  const { chromium } = loadChromium();
  const browser = await chromium.launch({ headless: !HEADED });
  const captured = [];
  try {
    const context = await browser.newContext({ viewport: VIEWPORT });
    const page = await context.newPage();

    await page.goto(BASE, { waitUntil: "networkidle" });
    await page.waitForSelector("#copilot-composer", { timeout: 30_000 });

    if (want("home")) {
      captured.push(
        await shoot(page, "20_copilot_home", "the copilot at rest, before anything is asked"),
      );
    }

    if (want("store")) {
      await page.getByRole("button", { name: "Store" }).click();
      await page.waitForTimeout(1_500);
      captured.push(await shoot(page, "21_copilot_store", "the shelf, inside the copilot"));

      // The first product tile, which opens rather than adds.
      await page.locator("section[aria-label='The store'] li button").first().click();
      await page.waitForTimeout(800);
      captured.push(
        await shoot(page, "22_copilot_product", "a product as the merchant records it"),
      );
      await page.getByRole("button", { name: "Close the store" }).click();
      await page.waitForTimeout(500);
    }

    if (want("cart")) {
      await idle(page);
      await say(page, "add amul gold full cream milk");
      const asked = await waitForReply(page, /How many would you like/);
      if (!asked) console.log("  (the shop did not ask for a quantity; capturing anyway)");
      captured.push(
        await shoot(page, "23_copilot_quantity", "the shop asks how many, rather than assuming"),
      );
      await idle(page);
      await say(page, "2");
      // The cart column, not the transcript: the sentence arrives before the write settles,
      // and a screenshot taken on the sentence catches a card still saying ADDING over an
      // empty cart. What this photograph is for is the cart having the thing in it.
      await settled(page, "aside", /Amul Gold Full Cream Milk/);
      await page.waitForTimeout(600);
      captured.push(
        await shoot(page, "24_copilot_cart", "the cart beside the conversation, with its ledger"),
      );
    }

    if (want("approve")) {
      await idle(page, 40_000);
      await say(page, "proceed to checkout");
      const arrived = await waitForReply(page, /Approve to pay/, 60_000);
      await page.waitForTimeout(1_200);
      if (arrived) {
        captured.push(
          await shoot(page, "25_copilot_approval", "one confirmation, on the store's own surface"),
        );
      } else {
        // Say what happened rather than skipping quietly: a capture that silently drops a
        // screenshot leaves a README describing a screen nobody checked.
        const tail = await page.evaluate(() =>
          [...document.querySelectorAll('ol[role="log"] > li')]
            .slice(-2)
            .map((entry) => entry.innerText.replace(/\n+/g, " ").slice(0, 200)),
        );
        console.log("  (no approval card appeared) last said:", JSON.stringify(tail));
        await shoot(page, "25_copilot_approval_MISSING", "diagnostic: what was on screen instead");
      }
    }

    await writeFile(
      resolve(OUT, "copilot-capture-manifest.json"),
      `${JSON.stringify(
        {
          base_url: BASE,
          viewport_css_px: VIEWPORT,
          captured,
          note: "Driven through the composer against a running stack. Nothing here is mocked.",
        },
        null,
        2,
      )}\n`,
    );
  } finally {
    await browser.close();
  }
  console.log(`\n${captured.length} screenshots written to docs/images/`);
}

await main();
