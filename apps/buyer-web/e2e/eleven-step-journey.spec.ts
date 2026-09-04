import { test, expect } from "@playwright/test";

test.describe("Track 1: Eleven-Step Governed Commerce Journey", () => {
  test("walks the complete 11-step journey in mock mode asserting kernel guarantees", async ({
    page,
  }) => {
    // -----------------------------------------------------------------------
    // Step 1: Multilingual Grounded Product Discovery
    // -----------------------------------------------------------------------
    await page.goto("/");
    await expect(page).toHaveTitle(/Zepto|Commerce|Storefront/i);

    // Verify above-the-fold Track 1 Architecture Banner and Mock Mode badge
    await expect(
      page.getByText("“Agents propose; deterministic systems authorize and execute.”").first()
    ).toBeVisible();
    await expect(page.getByText("Mock Mode (Simulated Gateway)").first()).toBeVisible();

    // Search for Indian staple "doodh" (multilingual discovery)
    const searchInput = page.getByRole("searchbox", { name: /search catalogue/i });
    await searchInput.fill("doodh");
    await searchInput.press("Enter");

    // Assert grounded products appear (e.g. Amul Taaza Fresh Toned Milk)
    const milkProduct = page.locator("li").filter({ hasText: /Amul Taaza|Milk|Doodh/i }).first();
    await expect(milkProduct).toBeVisible();

    // -----------------------------------------------------------------------
    // Step 2: Useful Basket Growth Within Merchant Policy
    // -----------------------------------------------------------------------
    // Click ADD button on milk
    const addButton = milkProduct.getByRole("button", { name: /add/i }).first();
    await addButton.click();

    // Verify quantity stepper appears with 1
    const stepper = milkProduct.getByLabel(/1 units|1 pack/i);
    await expect(stepper).toBeVisible();

    // Verify cart indicator or view cart pill updates
    const viewCartLink = page.getByRole("link", { name: /cart/i }).first();
    await expect(viewCartLink).toBeVisible();

    // -----------------------------------------------------------------------
    // Step 3: Checkout Construction
    // -----------------------------------------------------------------------
    await page.goto("/basket");
    await expect(page.getByRole("heading", { name: /Review Your Basket/i })).toBeVisible();

    // Verify bill details rendered with integer paise calculations
    await expect(page.getByRole("heading", { name: /Bill Details/i })).toBeVisible();
    await expect(page.getByText("To Pay")).toBeVisible();

    // Click Proceed to Checkout
    const checkoutCta = page.getByRole("button", { name: /Proceed to Checkout/i });
    await expect(checkoutCta).toBeEnabled();
    await checkoutCta.click();

    // Wait for checkout page navigation
    await page.waitForURL(/\/checkout\/.+/);
    await expect(page.getByRole("heading", { name: /Checkout/i })).toBeVisible();

    // -----------------------------------------------------------------------
    // Step 4: Trusted Approval (Version 1)
    // -----------------------------------------------------------------------
    // Assert approval card is presented with server-confirmed content hash
    const approvalCard = page.getByTestId("approval-card");
    await expect(approvalCard).toBeVisible();
    await expect(page.getByTestId("approval-version")).toHaveText("1");

    const displayedHash = await page.getByTestId("approval-content-hash").innerText();
    expect(displayedHash.length).toBeGreaterThan(0);

    const approveButton = page.getByTestId("approve-button");
    await expect(approveButton).toBeVisible();
    await approveButton.click();

    // Verify approval recorded on Version 1
    await expect(page.getByText("Approval recorded").first()).toBeVisible();
    await expect(page.getByRole("heading", { name: /Approved: version 1/i })).toBeVisible();

    // -----------------------------------------------------------------------
    // Steps 5, 6, 7: Merchant State Changes Underneath & Old Approval Rejected
    // -----------------------------------------------------------------------
    // Submit version 1 to the kernel.
    // In mock mode, the first submit triggers the merchant price/delivery surge (Step 5)
    // and the transaction kernel refuses version 1 and produces Version 2 with exact deltas (Steps 6 & 7).
    const submitV1Button = page.getByRole("button", { name: /Submit version 1 to the kernel/i });
    await expect(submitV1Button).toBeVisible();
    await submitV1Button.click();

    // Assert material delta view is displayed
    const deltaView = page.getByTestId("delta-view");
    await expect(deltaView).toBeVisible();

    // Assert Version 1 is reported as invalidated and Version 2 requires approval
    await expect(
      page.getByRole("heading", { name: /Material delta: version 1 is invalidated; approve version 2/i })
    ).toBeVisible();

    // Assert exact delta fields are rendered
    await expect(deltaView.getByText(/lines\[0\]\.unit_price_minor|PRICE_CHANGED/i).first()).toBeVisible();

    // Assert Version 1 is marked as INVALIDATED in the versions table
    const versionsTable = page.getByRole("table", { name: /Checkout versions/i });
    await expect(versionsTable.getByText("INVALIDATED").first()).toBeVisible();

    // -----------------------------------------------------------------------
    // Step 8: Fresh Approval on Version 2
    // -----------------------------------------------------------------------
    // Now approval card renders for Version 2
    await expect(approvalCard.getByRole("heading", { name: /Approve version 2/i })).toBeVisible();
    await expect(page.getByTestId("approval-version")).toHaveText("2");

    const approveV2Button = page.getByTestId("approve-button");
    await expect(approveV2Button).toBeVisible();
    await approveV2Button.click();

    // Verify Version 2 approval recorded
    await expect(page.getByRole("heading", { name: /Approved: version 2/i })).toBeVisible();

    // -----------------------------------------------------------------------
    // Step 9: Razorpay Payment Execution (Admitted Exactly Once)
    // -----------------------------------------------------------------------
    // Submit version 2 to kernel: kernel admits it and issues Execution Grant
    const submitV2Button = page.getByRole("button", { name: /Submit version 2 to the kernel/i });
    await expect(submitV2Button).toBeVisible();
    await submitV2Button.click();

    // Assert Payment opening state appears
    const payButton = page.getByRole("button", { name: /Pay .* with Razorpay/i });
    await expect(payButton).toBeVisible();

    // Assert NO paid state appears before payment execution
    await expect(page.getByRole("heading", { name: /Order confirmed/i })).not.toBeVisible();

    // Launch simulated Razorpay dialog
    await payButton.click();

    // Assert simulated dialog appears
    await expect(page.getByText("Simulated Razorpay Checkout (mock mode)")).toBeVisible();

    // -----------------------------------------------------------------------
    // Steps 10 & 11: Capture Timeline & Retained Revenue Order Confirmation
    // -----------------------------------------------------------------------
    const simulateSuccessButton = page.getByRole("button", { name: /Pay .* \(simulated success\)/i });
    await expect(simulateSuccessButton).toBeVisible();
    await simulateSuccessButton.click();

    // Verify Order confirmed state arrives from server verification
    await expect(page.getByRole("heading", { name: /Order confirmed/i })).toBeVisible({ timeout: 15000 });
    await expect(page.getByText(/Capture evidence/i).first()).toBeVisible();

    // Verify Order tracking link is present
    const orderLink = page.getByRole("link", { name: /ord_/i }).first();
    await expect(orderLink).toBeVisible();
  });
});
