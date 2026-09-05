/**
 * Reproducible submission screenshots, driven against the live stack.
 *
 * Every image under `docs/images/` that this script writes was produced by a browser
 * talking to the Commerce API on :8000 through the two applications, at the moment the
 * script ran. Nothing is composited, retouched or drawn from a fixture, because neither
 * application ships one: an unreachable API renders the problem document rather than a
 * plausible number, so a screenshot that contains a price is a screenshot of a price the
 * server actually produced.
 *
 * The point of the script, rather than a person with a screenshot key, is the refusal.
 * That shot only exists in a window of a few seconds between a buyer approving version 1
 * and the merchant's price moving underneath it, and reproducing it by hand means getting
 * a scenario injection in between two clicks. So the script does it: it resets the
 * merchant catalogue to the seeded baseline, builds the basket through the storefront's
 * own controls, approves version 1, injects the price change over the API, and then
 * presses Pay. What comes back is the screen the whole submission is about.
 *
 * The catalogue lives in the API process's memory (ADR D14) and every session on this
 * machine shares it, so a run states its own numbers rather than trusting the last one's.
 * Whatever the run actually saw is written to `docs/images/capture-manifest.json`, and
 * that file — not a figure typed into prose — is what the documents should quote.
 *
 * Why Node and not Python, in a repository whose scripts are Python: Playwright is already
 * installed for `apps/buyer-web`, along with its Chromium build, and the Python binding is
 * not. Adding it would mutate a virtualenv three other sessions are working in, to gain
 * nothing this file needs.
 *
 *   node scripts/capture_screenshots.mjs               # everything reachable unattended
 *   node scripts/capture_screenshots.mjs --headed      # watch it, and pay by hand
 *   node scripts/capture_screenshots.mjs --pay         # headed, then wait for the capture
 *
 * A payment cannot be completed without a person: Razorpay Standard Checkout wants a card
 * on a hosted page. Unattended, the run stops at the payment handoff and records in the
 * manifest that the captured-order shot was not reachable, which is the truth rather than
 * a gap quietly left out.
 */

import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
const OUT = path.join(ROOT, "docs", "images");

/**
 * Playwright comes from the storefront's own install; this repository adds no second copy.
 *
 * A git worktree has no `node_modules` of its own, and running this from one is the normal
 * case rather than the exception here, so the main checkout is tried as well before the
 * script gives up and says which install is missing.
 */
function loadChromium() {
  const roots = [ROOT];
  try {
    const main = execFileSync("git", ["rev-parse", "--path-format=absolute", "--git-common-dir"], {
      cwd: ROOT,
      encoding: "utf8",
    }).trim();
    if (main.endsWith("/.git")) roots.push(path.dirname(main));
  } catch {
    /* not a git checkout, or no git; the local root is the only candidate */
  }
  const tried = [];
  for (const root of roots) {
    for (const app of ["buyer-web", "merchant-console"]) {
      const anchor = path.join(root, "apps", app, "package.json");
      tried.push(path.relative(ROOT, anchor));
      try {
        return createRequire(anchor)("playwright-core");
      } catch {
        /* try the next anchor */
      }
    }
  }
  throw new Error(
    `Playwright is not installed. Run "npm install" in apps/buyer-web — the same install the ` +
      `storefront needs to run at all — then try again.\nLooked from: ${tried.join(", ")}`,
  );
}

const { chromium } = loadChromium();

const API = process.env.COMMERCE_API_URL ?? "http://127.0.0.1:8000";
const STORE = process.env.STOREFRONT_URL ?? "http://localhost:3000";
const CONSOLE_URL = process.env.CONSOLE_URL ?? "http://localhost:3001";
const SCENARIO_KEY = process.env.SCENARIO_KEY ?? "local-demo-scenario-key";
const TENANT = process.env.TENANT_SLUG ?? "demo";

const HEADED = process.argv.includes("--headed") || process.argv.includes("--pay");
const WAIT_FOR_PAYMENT = process.argv.includes("--pay");
const PAYMENT_WAIT_MS = 240_000;

/** The two SKUs the runbook uses. The rice crosses the free-delivery threshold. */
const MILK = "AMUL-DAIRY-001";
const RICE = "INDI-STPL-001";
const MILK_QTY = 2;
const RICE_QTY = 1;

/** 2x, so a projector and a PDF both have pixels to spare. */
const SCALE = 2;
const DESKTOP = { width: 1440, height: 1000 };
const CONSOLE_VIEW = { width: 1600, height: 1050 };

/* ------------------------------------------------------------------ the API */

let TOKEN = null;

async function api(method, route, { body, scenario = false, idem = null } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (TOKEN) headers.Authorization = `Bearer ${TOKEN}`;
  if (scenario) headers["X-Scenario-Key"] = SCENARIO_KEY;
  if (idem) headers["Idempotency-Key"] = idem;
  const response = await fetch(API + route, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  const json = text ? JSON.parse(text) : null;
  return { status: response.status, body: json };
}

function key() {
  return `capture-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/**
 * A call that did not answer as expected stops the run.
 *
 * The catalogue is process memory shared with every other session on this machine, so a
 * reset can lose a race and a read can come back a problem document. Left unchecked, that
 * produces the worst possible outcome for this script: a run that finishes, writes fifteen
 * images and a manifest, and quietly documents a refusal over a four-rupee difference
 * nobody meant to demonstrate. Better to stop and say which call went wrong.
 */
function expect(result, what) {
  if (result.status < 200 || result.status >= 300) {
    throw new Error(`${what} answered HTTP ${result.status}: ${JSON.stringify(result.body).slice(0, 300)}`);
  }
  return result.body;
}

/**
 * Read an API route as the *browser's* buyer, through the storefront's own proxy.
 *
 * A checkout belongs to the session that created it, and an operator holding the scenario
 * key still cannot read one — `GET /v1/checkouts/{id}` answers 404 with "No checkout with
 * that identifier belongs to this session." That is the isolation working, so this script
 * asks the way the page does: a same-origin fetch that carries the httpOnly session cookie
 * the proxy set. The script never sees the token, which is the point of the proxy.
 */
async function viaPage(page, route) {
  return page.evaluate(async (r) => {
    const response = await fetch(`/api/backend${r}`, { headers: { Accept: "application/json" } });
    return { status: response.status, body: await response.json().catch(() => null) };
  }, route);
}

const rupees = (minor) =>
  minor === null || minor === undefined ? "—" : `₹${(minor / 100).toFixed(2)}`;

/* ------------------------------------------------------------- the reporting */

const manifest = {
  captured_at: new Date().toISOString(),
  api: API,
  storefront: STORE,
  console: CONSOLE_URL,
  device_scale_factor: SCALE,
  figures: {},
  shots: [],
  not_captured: [],
};

const step = (n, what) => console.log(`\n[${String(n).padStart(2, "0")}] ${what}`);
const note = (what) => console.log(`     ${what}`);

/**
 * Take one shot and record it.
 *
 * A caption is stored beside the file because a screenshot with no provenance is an
 * assertion. The manifest is what a reader checks the images against.
 */
async function shoot(page, name, caption, { fullPage = false, clip = null } = {}) {
  const file = path.join(OUT, `${name}.png`);
  await page.screenshot({ path: file, fullPage, ...(clip ? { clip } : {}) });
  manifest.shots.push({ name, file: path.relative(ROOT, file), caption, url: page.url() });
  note(`→ docs/images/${name}.png — ${caption}`);
}

/** Next's dev overlay is not part of the product and must not be in a submission image. */
const HIDE_DEV_OVERLAY = `nextjs-portal, #nextjs-dev-tools-menu, [data-nextjs-toast] { display: none !important; }`;

/* ------------------------------------------------------ storefront interaction */

async function settle(page, ms = 900) {
  await page.waitForLoadState("networkidle").catch(() => {});
  await page.waitForTimeout(ms);
}

async function open(page, url) {
  await page.goto(url, { waitUntil: "networkidle" });
  await page.addStyleTag({ content: HIDE_DEV_OVERLAY }).catch(() => {});
  await page.waitForTimeout(700);
}

/** Add through the storefront's own controls: ADD the first time, then the stepper. */
async function addToBasket(page, sku, quantity) {
  await open(page, `${STORE}/p/${sku}`);
  const add = page.locator('button[aria-label^="Add "]');
  if (await add.count()) {
    await add.first().click();
    await settle(page, 1100);
  }
  for (let i = 1; i < quantity; i += 1) {
    await page.locator('button[aria-label^="Increase quantity"]').first().click();
    await settle(page, 1100);
  }
}

/* ---------------------------------------------------------------------- main */

async function main() {
  await mkdir(OUT, { recursive: true });

  step(1, "Minting a buyer session and resetting the merchant catalogue");
  const session = await api("POST", "/v1/demo/sessions", {
    body: { tenant_slug: TENANT, actor_type: "BUYER" },
  });
  if (session.status !== 200 && session.status !== 201) {
    throw new Error(`the API would not mint a session (HTTP ${session.status}). Is it running on ${API}?`);
  }
  TOKEN = session.body.token;
  const MERCHANT_ID = session.body.merchant_id;
  manifest.figures.tenant_id = session.body.tenant_id;
  manifest.figures.merchant_id = MERCHANT_ID;

  // The seeded price of the milk, which the runbook quotes. A reset that lands and then
  // loses a race with another session is the one failure worth retrying rather than
  // reporting, because it costs one call and it is the difference between the runbook's
  // numbers and somebody else's.
  const SEEDED_MILK_MINOR = 2800;
  let reset = null;
  let milk = null;
  let rice = null;
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    reset = expect(
      await api("POST", "/v1/scenario/injections", {
        scenario: true,
        body: { kind: "CATALOGUE_RESET", note: "capture_screenshots baseline" },
      }),
      "the catalogue reset",
    );
    milk = expect(await api("GET", `/v1/catalogue/products/${MILK}`), `reading ${MILK}`);
    rice = expect(await api("GET", `/v1/catalogue/products/${RICE}`), `reading ${RICE}`);
    if (milk.unit_price_minor === SEEDED_MILK_MINOR) break;
    if (attempt === 1) note(`${MILK} came back at ${rupees(milk.unit_price_minor)}, not the seeded ${rupees(SEEDED_MILK_MINOR)}; resetting once more`);
  }
  const seededBaseline = milk.unit_price_minor === SEEDED_MILK_MINOR;
  if (!seededBaseline) {
    note(`WARNING: ${MILK} is ${rupees(milk.unit_price_minor)} and the runbook says ${rupees(SEEDED_MILK_MINOR)}. Another session is moving this catalogue. The refusal is still real; the totals will not be the runbook's.`);
  }
  note(`catalogue revision ${reset.revision_before} → ${reset.revision_after}`);
  manifest.figures.baseline = {
    [MILK]: { name: milk.display_name, unit_price_minor: milk.unit_price_minor, quantity: MILK_QTY },
    [RICE]: { name: rice.display_name, unit_price_minor: rice.unit_price_minor, quantity: RICE_QTY },
    catalogue_revision: reset.revision_after,
    matches_runbook_baseline: seededBaseline,
  };
  note(`${milk.display_name} at ${rupees(milk.unit_price_minor)} · ${rice.display_name} at ${rupees(rice.unit_price_minor)}`);

  const browser = await chromium.launch({ headless: !HEADED });
  const context = await browser.newContext({ viewport: DESKTOP, deviceScaleFactor: SCALE });
  const page = await context.newPage();
  const failures = [];
  page.on("pageerror", (error) => failures.push(String(error).slice(0, 200)));

  try {
    step(2, "The storefront, as a buyer arrives at it");
    await open(page, `${STORE}/`);
    await shoot(page, "01_storefront_home", "The storefront home. Every price on it was read from the Commerce API during this page load.");

    step(3, "A category page");
    await open(page, `${STORE}/c/dairy`);
    await shoot(page, "02_category_dairy", `Dairy & Eggs, priced by the merchant at catalogue revision ${reset.revision_after}.`);

    step(4, "RazorAI, mid-conversation, in Hinglish");
    await page.locator('button[aria-label="Open RazorAI"]').click();
    const dialog = page.locator('[role="dialog"]');
    await dialog.waitFor({ timeout: 15_000 });
    await page.locator('input[placeholder*="Ask for something"]').fill(
      "mujhe 2 doodh aur ek basmati chawal chahiye",
    );
    await page.locator('button[aria-label="Send to RazorAI"]').click();
    await page.waitForFunction(
      () => /searched the catalogue|What it actually did/i.test(document.body.innerText),
      null,
      { timeout: 60_000 },
    ).catch(() => note("RazorAI did not report a tool call within a minute; capturing whatever it did say"));
    await page.waitForTimeout(1500);
    manifest.figures.razorai_turn = (await dialog.innerText()).slice(0, 1200);
    await shoot(page, "03_razorai_panel", "RazorAI answers a Hinglish request and shows the tool calls it actually made. It holds no capability to approve or to pay.");
    await page.locator('button[aria-label="Close RazorAI"]').click().catch(() => {});
    await page.waitForTimeout(500);

    step(5, "Building the basket through the storefront's own controls");
    await addToBasket(page, MILK, MILK_QTY);
    await addToBasket(page, RICE, RICE_QTY);
    await open(page, `${STORE}/basket`);
    await settle(page, 1200);
    await shoot(page, "04_basket_quote", "The basket and the merchant's quote. The rice crosses the free-delivery threshold, so the fee and its tax are gone — merchant policy, not a discount an agent invented.", { fullPage: true });

    step(6, "Opening a checkout and reading the approval card");
    await page.getByRole("button", { name: /Proceed to checkout/i }).click();
    await page.waitForURL(/\/checkout\//, { timeout: 30_000 });
    await page.addStyleTag({ content: HIDE_DEV_OVERLAY }).catch(() => {});
    await settle(page, 2200);
    const checkoutId = page.url().split("/checkout/")[1].split(/[?#]/)[0];
    manifest.figures.checkout_id = checkoutId;
    await shoot(page, "05_approval_card_v1", "Version 1's approval card. The buyer approves a hash and an integer amount, not a sentence.", { fullPage: true });

    const beforeApproval = (await viaPage(page, `/v1/checkouts/${checkoutId}`)).body;
    const v1 = beforeApproval.versions.find((v) => v.version === 1);
    manifest.figures.version_1 = {
      amount_minor: v1.amount_minor,
      display: rupees(v1.amount_minor),
      content_hash: v1.content_hash,
      policy_receipt_hash: v1.policy_receipt_hash,
    };
    note(`version 1 is ${rupees(v1.amount_minor)} under hash ${v1.content_hash.slice(0, 12)}…`);

    step(7, "The buyer approves version 1");
    await page.getByRole("button", { name: /^Approve/ }).click();
    await page.waitForFunction(
      () => /Pay|APPROVED/i.test(document.body.innerText),
      null,
      { timeout: 30_000 },
    ).catch(() => {});
    await settle(page, 1800);

    step(8, "The merchant raises the price underneath the approved checkout");
    // The rise is +₹51.00 a unit, over two units, so the difference on screen is always
    // +₹102.00 and the narrator's subtraction holds whatever the baseline turned out to be.
    // On the seeded catalogue this is exactly the runbook's ₹28.00 → ₹79.00. The value has
    // to differ from the current one: the simulator answers 409 to a change that changes
    // nothing, and the current price is re-read rather than assumed because this window is
    // precisely where another session's injection would land.
    const current = expect(await api("GET", `/v1/catalogue/products/${MILK}`), `re-reading ${MILK}`).unit_price_minor;
    const target = current + 5100;
    const injection = expect(
      await api("POST", "/v1/scenario/injections", {
        scenario: true,
        body: { kind: "PRICE_SET", sku: MILK, value: target, note: "capture_screenshots step 5" },
      }),
      "the price injection",
    );
    manifest.figures.injection = {
      sku: MILK,
      label: injection.label,
      deltas: injection.deltas,
      revision_before: injection.revision_before,
      revision_after: injection.revision_after,
      audit_event_id: injection.audit_event_id,
      scenario_run_id: injection.scenario_run_id,
    };
    note(`${MILK} ${rupees(injection.deltas[0].before)} → ${rupees(injection.deltas[0].after)}, labelled ${injection.label}`);

    step(9, "The buyer presses Pay. THE REFUSAL.");
    await page.getByRole("button", { name: /^Pay\b/i }).first().click();
    await page.waitForFunction(
      () => /Refused by the transaction kernel/i.test(document.body.innerText),
      null,
      { timeout: 45_000 },
    );
    await page.addStyleTag({ content: HIDE_DEV_OVERLAY }).catch(() => {});
    await settle(page, 1800);
    await shoot(page, "06_the_refusal", "The kernel refuses the stale approval. HTTP 200 carrying a decision, the exact delta, version 1 retired and version 2 offered.", { fullPage: true });

    const afterRefusal = (await viaPage(page, `/v1/checkouts/${checkoutId}`)).body;
    const r1 = afterRefusal.versions.find((v) => v.version === 1);
    const r2 = afterRefusal.versions.find((v) => v.version === 2);
    manifest.figures.refusal = {
      checkout_state: afterRefusal.state,
      current_version: afterRefusal.current_version,
      version_1_state: r1.state,
      version_1_amount_minor: r1.amount_minor,
      version_2_amount_minor: r2 ? r2.amount_minor : null,
      display: `${rupees(r1.amount_minor)} → ${rupees(r2 ? r2.amount_minor : null)}`,
      delta_minor: r2 ? r2.amount_minor - r1.amount_minor : null,
    };
    note(`version 1 is now ${r1.state}; version ${afterRefusal.current_version} asks ${rupees(r2 && r2.amount_minor)}`);

    step(10, "Fresh approval on version 2, then payment handoff");
    // The refusal offers the successor rather than approving it: consent to version 2 is a
    // separate press on the trusted surface, so this is two clicks and not one.
    await page.getByRole("button", { name: /^Review version/i }).first().click();
    await page.waitForTimeout(2500);
    await page.getByRole("button", { name: /^Approve/ }).first().click();
    await page.waitForTimeout(2500);
    await page.getByRole("button", { name: /^Pay\b/i }).first().click();
    await page.waitForFunction(
      () => /razorpay|order_/i.test(document.body.innerText),
      null,
      { timeout: 60_000 },
    ).catch(() => note("the payment panel did not name a Razorpay order within a minute"));
    await settle(page, 3000);
    await page.addStyleTag({ content: HIDE_DEV_OVERLAY }).catch(() => {});
    await shoot(page, "07_payment_handoff", "The payment handoff. The API never called Razorpay; the durable worker did, holding a single-use grant it had already spent.", { fullPage: true });

    const payment = (await viaPage(page, `/v1/checkouts/${checkoutId}/payment`)).body ?? {};
    manifest.figures.payment = {
      state: payment.state,
      razorpay_order_id: payment.razorpay_order_id ?? null,
      razorpay_key_id: payment.razorpay_key_id ?? null,
      amount_minor: payment.amount_minor ?? null,
      attempt_id: payment.attempt_id ?? null,
    };
    note(`Razorpay order ${payment.razorpay_order_id ?? "(not yet created)"} for ${rupees(payment.amount_minor)}`);

    step(11, "An order and its capture evidence");
    let order = null;
    if (WAIT_FOR_PAYMENT) {
      note(`waiting up to ${PAYMENT_WAIT_MS / 1000}s for a human to complete Razorpay Standard Checkout with a test card`);
      const deadline = Date.now() + PAYMENT_WAIT_MS;
      while (Date.now() < deadline && !order) {
        await new Promise((r) => setTimeout(r, 4000));
        const list = await api("GET", "/v1/orders?limit=5", { scenario: true });
        order = (list.body.orders ?? []).find((o) => o.checkout_id === checkoutId) ?? null;
      }
    } else {
      const list = await api("GET", "/v1/orders?limit=5", { scenario: true });
      order = (list.body.orders ?? [])[0] ?? null;
    }
    if (order) {
      await open(page, `${STORE}/orders/${order.order_id}`);
      await settle(page, 1500);
      await shoot(page, "08_order_capture_evidence", `Order ${order.order_id.slice(0, 8)}… and the evidence its capture was applied from.`, { fullPage: true });
      manifest.figures.order = {
        order_id: order.order_id,
        state: order.state,
        amount_minor: order.amount_minor,
        razorpay_order_id: order.razorpay_order_id,
        razorpay_payment_id: order.razorpay_payment_id,
        capture_evidence: order.capture_evidence,
      };
    } else {
      const why =
        "No order exists on this tenant. A capture needs a card entered on Razorpay's own hosted page, " +
        "which an unattended run cannot do. Re-run with --pay and complete the test payment to capture this shot.";
      note(why);
      manifest.not_captured.push({ shot: "08_order_capture_evidence", reason: why });
      await open(page, `${STORE}/orders`);
      await settle(page, 1200);
      await shoot(page, "08_orders_no_capture_yet", "The orders page, empty and saying so. It renders what the API returned rather than inventing a row.", { fullPage: true });
    }

    /* ------------------------------------------------------------- the console */

    step(12, "The merchant console: the retained-revenue arithmetic");
    const operator = await context.newPage();
    await operator.setViewportSize(CONSOLE_VIEW);
    await open(operator, `${CONSOLE_URL}/evidence`);
    await settle(operator, 2000);
    await shoot(operator, "09_console_retained_revenue", "The merchant's side of the same refusal: four committed rows, and the platform declining to state a difference until a capture is verified.", { fullPage: true });

    const retained = (await api("GET", `/v1/merchants/${MERCHANT_ID}/evidence/retained-revenue?checkout_id=${checkoutId}`, { scenario: true })).body;
    manifest.figures.retained_revenue = retained;

    // The proof chain is the page's strongest panel and the reason a payments engineer can
    // check this rather than believe it, so it gets a frame of its own rather than being a
    // band in the middle of a very tall full-page shot.
    const verdict = operator.getByText(/Proof chain verdict/i).first();
    if (await verdict.count()) {
      await verdict.scrollIntoViewIfNeeded();
      await operator.evaluate(() => window.scrollBy(0, -40));
      await settle(operator, 800);
      await shoot(operator, "09b_console_proof_chain", "The Money Action Proof Chain, link by link: the hash recomputed, the receipt bound to the version, the grant consumed once, and every provider mutation accounted for.");
    } else {
      manifest.not_captured.push({ shot: "09b_console_proof_chain", reason: "no proof-chain panel on /evidence" });
    }

    step(13, "The merchant console: the operations tabs");
    await open(operator, `${CONSOLE_URL}/operations`);
    await settle(operator, 1800);
    await shoot(operator, "10_console_operations_orders", "Operations, orders tab. Counts come from the API, and an empty result says it is empty rather than failing quietly.", { fullPage: true });
    // The tabs are links carrying `?tab=`, so navigating is both shorter and more
    // deterministic than clicking one and hoping the panel has swapped before the shutter.
    for (const [tab, name, caption] of [
      ["refunds", "11_console_operations_refunds", "Refunds, split by state: pending, unknown and failed are three different facts."],
      ["outbox", "12_console_operations_outbox", "The durable outbox. Every Razorpay call this platform has ever made left a row here first."],
      ["safe-mode", "13_console_operations_safe_mode", "The kill switch, asked of the kernel rather than inferred. Refunds are never swept by it."],
    ]) {
      await open(operator, `${CONSOLE_URL}/operations?tab=${tab}`);
      await settle(operator, 1600);
      await shoot(operator, name, caption, { fullPage: true });
    }

    step(14, "The merchant console: the inspector, on this run's own attempt");
    await open(operator, `${CONSOLE_URL}/inspector`);
    const attempt = payment.attempt_id ?? manifest.figures.payment.attempt_id;
    if (attempt) {
      await operator.locator("input").first().fill(attempt);
      await operator.getByRole("button", { name: /^Inspect$/i }).click();
      await settle(operator, 2500);
    }
    await shoot(operator, "14_console_inspector", attempt ? `The whole of payment attempt ${attempt.slice(0, 8)}…, as one document.` : "The inspector, with no attempt loaded.", { fullPage: true });

    const outbox = (await api("GET", "/v1/ops/outbox", { scenario: true })).body;
    const byStatus = {};
    for (const c of outbox.commands ?? []) byStatus[c.status] = (byStatus[c.status] ?? 0) + 1;
    manifest.figures.outbox_by_status = byStatus;

    const audit = (await api("GET", `/v1/audit/streams/checkout/${checkoutId}/verify`, { scenario: true })).body;
    manifest.figures.audit_chain = {
      intact: audit.intact,
      length: audit.length,
      events_verified: audit.events_verified,
      head_hash: audit.head_hash,
    };

    if (failures.length) manifest.page_errors = failures;
  } finally {
    await browser.close();
  }

  await writeFile(
    path.join(OUT, "capture-manifest.json"),
    JSON.stringify(manifest, null, 2) + "\n",
    "utf8",
  );

  console.log(`\n${manifest.shots.length} images written to docs/images/`);
  console.log("Manifest: docs/images/capture-manifest.json");
  if (manifest.not_captured.length) {
    console.log("\nNot captured on this run:");
    for (const gap of manifest.not_captured) console.log(`  - ${gap.shot}: ${gap.reason}`);
  }
  const f = manifest.figures;
  if (f.refusal) {
    console.log(`\nThe refusal this run produced: ${f.refusal.display} (delta ${f.refusal.delta_minor} paise), version 1 ${f.refusal.version_1_state}.`);
  }
}

main().catch((error) => {
  console.error("\nCapture failed:", error.message);
  process.exitCode = 1;
});
