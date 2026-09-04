import { test, expect } from "@playwright/test";

test.describe("Merchant Console Comprehensive End-to-End Suite", () => {
  test("1. Dashboard loads and renders retained revenue figure", async ({ page }) => {
    await page.goto("/");
    await expect(page).toHaveTitle(/Merchant Console/i);

    // Headline and Retained Revenue card
    await expect(page.getByText("Zepto Merchant Control Console")).toBeVisible();
    await expect(page.getByText(/Retained Revenue/i).first()).toBeVisible();
    await expect(page.getByText("₹102.00").first()).toBeVisible();

    // Verify presence of proof breakdown link
    await expect(page.getByText(/Audit Breakdown/i).first()).toBeVisible();
  });

  test("2. /evidence shows exact arithmetic and cryptographic audit verification", async ({ page }) => {
    await page.goto("/evidence");

    // Version N approved vs corrected vs difference
    await expect(page.getByText("The Preserved Margin Arithmetic")).toBeVisible();
    await expect(page.getByText("₹108.25").first()).toBeVisible();
    await expect(page.getByText("₹151.85").first()).toBeVisible();
    await expect(page.getByText("+₹43.60").first()).toBeVisible();

    // Cryptographic audit stream verification
    await expect(page.getByText("Cryptographic Audit Chain Verification").first()).toBeVisible();
    await expect(page.getByText(/INTACT · ZERO BREAKS/i).first()).toBeVisible();
    await expect(page.getByText(/14 entries/i).first()).toBeVisible();
  });

  test("3. /operations renders all four refund states as visibly different", async ({ page }) => {
    await page.goto("/operations");

    // Click Refund State Tracker tab
    await page.getByRole("button", { name: /Refund State Tracker/i }).click();

    // Locate the 4 distinct refund state elements in the table
    const pendingBadge = page.locator("[data-refund-state=\"REFUND_PENDING\"]").first();
    const unknownBadge = page.locator("[data-refund-state=\"REFUND_UNKNOWN\"]").first();
    const failedBadge = page.locator("[data-refund-state=\"REFUND_FAILED\"]").first();
    const processedBadge = page.locator("[data-refund-state=\"PROCESSED\"]").first();

    await expect(pendingBadge).toBeVisible();
    await expect(unknownBadge).toBeVisible();
    await expect(failedBadge).toBeVisible();
    await expect(processedBadge).toBeVisible();

    // Assert that REFUND_PENDING and REFUND_UNKNOWN have strictly distinct classes to prevent conflation
    const pendingClass = await pendingBadge.getAttribute("class");
    const unknownClass = await unknownBadge.getAttribute("class");
    expect(pendingClass).not.toEqual(unknownClass);
    expect(pendingClass).toContain("amber");
    expect(unknownClass).toContain("purple");
  });

  test("4. /catalogue filters, searches by Hindi synonym, and paginates without collapsing", async ({ page }) => {
    await page.goto("/catalogue");

    // Verify initial table renders products
    await expect(page.getByText("GRO-DAIRY-001").first()).toBeVisible();

    // Search by Hindi synonym "दूध" (doodh / milk)
    const searchInput = page.getByPlaceholder(/Search by SKU, English name, Hindi synonym/i);
    await searchInput.fill("दूध");

    // Verify Amul milk products appear in filtered results
    await expect(page.getByText("Amul Taaza Toned Milk 500 ml").first()).toBeVisible();

    // Clear search and test pagination
    await searchInput.fill("");
    await expect(page.getByText("Page 1 of 10").first()).toBeVisible();

    const nextBtn = page.getByRole("button", { name: "Next", exact: true });
    await expect(nextBtn).toBeEnabled();
    await nextBtn.click();

    // Verify page incremented to Page 2 without table collapse
    await expect(page.getByText("Page 2 of 10").first()).toBeVisible();
    const table = page.locator("table");
    await expect(table).toBeVisible();
    const rowCount = await table.locator("tbody tr").count();
    expect(rowCount).toBeGreaterThan(0);
  });

  test("5. Scenario lever posts to /v1/scenario/injections endpoint", async ({ page }) => {
    await page.goto("/catalogue");

    // Intercept outgoing POST request to scenario injection endpoint
    const postPromise = page.waitForRequest(
      (req) => req.url().includes("/scenario/injections") && req.method() === "POST",
      { timeout: 10000 }
    );

    // Dynamically retrieve SKU of the first product in the table
    const firstRow = page.locator("tbody tr").first();
    const targetSku = (await firstRow.locator("td").first().locator("span").first().textContent())?.trim();
    expect(targetSku).toBeTruthy();

    // Click "Sell Out" scenario lever on the first product row
    const sellOutBtn = firstRow.getByRole("button", { name: "Sell Out" });
    await sellOutBtn.click();

    // Assert that the request was made over the wire (not just a cosmetic toast)
    const interceptedRequest = await postPromise;
    expect(interceptedRequest.method()).toBe("POST");

    const postData = interceptedRequest.postDataJSON();
    expect(postData.kind).toBe("SELL_OUT");
    expect(postData.sku).toBe(targetSku);
  });

  test("6. Every simulated surface displays its honest SIMULATED · MOCK badge", async ({ page }) => {
    // Check Operations Page badges
    await page.goto("/operations");

    // Orders tab badge
    const ordersBadge = page.locator("[data-testid=\"badge-orders\"]");
    await expect(ordersBadge).toBeVisible();
    await expect(ordersBadge).toHaveText(/SIMULATED · MOCK|LIVE · COMMITTED/);

    // Refunds tab badge
    await page.getByRole("button", { name: /Refund State Tracker/i }).click();
    const refundsBadge = page.locator("[data-testid=\"badge-refunds\"]");
    await expect(refundsBadge).toBeVisible();
    await expect(refundsBadge).toHaveText(/SIMULATED · MOCK|LIVE · COMMITTED/);

    // Review Queue tab badge
    await page.getByRole("button", { name: /Human Review Queue/i }).click();
    const reviewBadge = page.locator("[data-testid=\"badge-review-queue\"]");
    await expect(reviewBadge).toBeVisible();
    await expect(reviewBadge).toHaveText(/SIMULATED · MOCK/);

    // Outbox tab badge
    await page.getByRole("button", { name: /Outbox & Durable Work/i }).click();
    const outboxBadge = page.locator("[data-testid=\"badge-outbox\"]");
    await expect(outboxBadge).toBeVisible();
    await expect(outboxBadge).toHaveText(/SIMULATED · MOCK|LIVE · COMMITTED/);

    // Check Catalogue Page badge
    await page.goto("/catalogue");
    const catalogueBadge = page.locator("[data-testid=\"badge-catalogue\"]");
    await expect(catalogueBadge).toBeVisible();
    await expect(catalogueBadge).toHaveText(/SIMULATED · MOCK/);

    // Check Onboarding Page badge and honest notice
    await page.goto("/onboarding");
    const onboardingBadge = page.locator("[data-testid=\"badge-onboarding\"]");
    await expect(onboardingBadge).toBeVisible();
    await expect(onboardingBadge).toHaveText(/SIMULATED · MOCK/);

    await expect(page.getByText("LOCAL STORAGE ONLY · NO BACKEND TENANT CREATED YET")).toBeVisible();
  });
});
