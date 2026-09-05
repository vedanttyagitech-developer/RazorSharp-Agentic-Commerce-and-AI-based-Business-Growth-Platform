import { test, expect } from "@playwright/test";
import path from "path";

test.describe("Visual Asset Capture", () => {
  test("captures high-resolution submission screenshots", async ({ page }) => {
    const imagesDir = path.resolve(process.cwd(), "../../docs/images");

    // 1. Storefront Home Desktop
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto("/");
    await page.waitForLoadState("networkidle");
    await expect(page.getByText("“Agents propose; deterministic systems authorize and execute.”").first()).toBeVisible();

    await page.screenshot({
      path: path.join(imagesDir, "01_storefront_home.webp"),
      type: "webp",
      quality: 90,
      fullPage: false,
    });

    // 2. Mobile 390px Storefront
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/");
    await page.waitForLoadState("networkidle");

    await page.screenshot({
      path: path.join(imagesDir, "04_mobile_storefront_390.webp"),
      type: "webp",
      quality: 90,
      fullPage: false,
    });

    // 3. Agent Panel Mid-Conversation with Tool Chips Visible
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto("/");
    await page.waitForLoadState("networkidle");

    const openAgentBtn = page.getByRole("button", { name: /Open AI Shopping Assistant/i });
    await openAgentBtn.click();
    const agentDialog = page.getByRole("dialog", { name: /AI Shopping Assistant/i });
    await expect(agentDialog).toBeVisible();

    // 1. Add doodh prompt
    const doodhPrompt = agentDialog.getByRole("button", { name: /2 packet doodh add karo/i });
    await doodhPrompt.click();
    await expect(page.getByText(/Searched catalogue/i).first()).toBeVisible({ timeout: 5000 });

    // 2. Propose checkout prompt
    const proposePrompt = agentDialog.getByRole("button", { name: /Propose checkout/i });
    await proposePrompt.click();
    await expect(page.getByText(/Drafted Checkout Proposal/i).first()).toBeVisible({ timeout: 5000 });
    await page.waitForTimeout(600);

    await page.screenshot({
      path: path.join(imagesDir, "02_agent_panel.webp"),
      type: "webp",
      quality: 90,
      fullPage: false,
    });

    // 4. Refusal Hero Card Showing Price Change Caught Underneath
    const refusalPrompt = page.getByRole("button", { name: /Simulate Price Shift Refusal/i });
    await refusalPrompt.click();

    await expect(page.getByRole("alert", { name: /Kernel Price Protection Refusal/i })).toBeVisible();
    await expect(page.getByText("Hero Moment · Kernel Guard")).toBeVisible();
    await page.waitForTimeout(600);

    await page.screenshot({
      path: path.join(imagesDir, "03_refusal_hero_card.webp"),
      type: "webp",
      quality: 90,
      fullPage: false,
    });

    // 5. Category View Blinkit Adapted
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto('/cn/null/cid/332/1102');
    await page.waitForLoadState('networkidle');
    await expect(page.getByText('Soft Drinks').first()).toBeVisible();

    await page.screenshot({
      path: path.join(imagesDir, '05_blinkit_category_view.webp'),
      type: 'webp',
      quality: 90,
      fullPage: false,
    });

    // 6. Click ADD on first available product to showcase green stepper
    const addBtn = page.getByRole('button', { name: /Add Coca-Cola/i }).first();
    if (await addBtn.isVisible()) {
      await addBtn.click();
      await page.waitForTimeout(500);
      await page.screenshot({
        path: path.join(imagesDir, '06_blinkit_category_view_added.webp'),
        type: 'webp',
        quality: 90,
        fullPage: false,
      });
    }
  });
});
