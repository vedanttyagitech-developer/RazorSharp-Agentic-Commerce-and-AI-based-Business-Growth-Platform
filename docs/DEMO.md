# Running the Governed Agentic Commerce Demonstration

This guide walks through reproducing the **Track 1** core demonstration of the Governed Agentic Commerce Platform.

> **Architecture Thesis**: *“Agents propose; deterministic systems authorize and execute.”*

---

## 1. Quickstart: Mock Mode (Zero Setup, Browser Only)

Mock mode runs the complete frontend, multilingual search catalogue, and the deterministic Transaction Assurance Kernel simulation entirely inside Next.js without requiring PostgreSQL or external credentials.

### Step 1: Start the Storefront
```bash
cd apps/buyer-web
npm install
NEXT_PUBLIC_API_MODE=mock npm run dev
```

Open [http://localhost:3000](http://localhost:3000) in your browser (or in Chrome DevTools responsive mobile mode at **390px**).

### Step 2: Start the Merchant Console (Optional, Port 3001)
```bash
cd apps/merchant-console
npm run dev
```

Open [http://localhost:3001](http://localhost:3001) to view the Merchant Retained Revenue Dashboard and Refusal Evidence Ledger.

---

## 2. Walking the Eleven-Step Demonstration

Follow these steps directly in the browser to experience the complete governed commerce lifecycle:

1. **Multilingual Grounded Discovery**:
   - In the search bar on [http://localhost:3000](http://localhost:3000), search for `doodh` or `milk` or Devanagari `दूध`.
   - Observe grounded product hits with integer paise pricing (`₹28.00` for Amul Taaza 500ml).
2. **Useful Basket Growth**:
   - Click **ADD** on Amul Taaza Milk. Notice the quantity stepper expands.
   - The header cart badge and bottom floating pill update to 1 Item.
3. **Checkout Construction**:
   - Click the **Cart** link or go to `/basket`.
   - Review the deterministic fee breakdown (Items Subtotal, Delivery Partner Fee, GST).
   - Click **Proceed to Checkout →**.
4. **Trusted Approval**:
   - On the `/checkout/[id]` page, review the **ApprovalCard**.
   - Note the exact server-confirmed JCS SHA-256 Content Hash, Policy Receipt Hash, and integer paise total.
   - Click **Approve ₹340.00 for version 1**.
   - The card records the approval bound cryptographically to that specific hash.
5. **Merchant State Changes Underneath Approved Checkout**:
   - Click **Submit version 1 to the kernel**.
   - In mock mode (or via merchant surge injection), the merchant pricing moves underneath the approved checkout.
6. **The Old Approval is Rejected**:
   - The transaction kernel refuses to execute against the stale approval (`STALE_APPROVAL_REFUSED` / `REAPPROVAL_REQUIRED`).
   - Version 1 is permanently marked **INVALIDATED**.
7. **Exact Delta Shown & Version 2 Created**:
   - A **Material Delta View** highlights the exact field-level price shifts (e.g. unit price increased by ₹10.00, delivery fee increased by ₹20.00).
8. **Fresh Approval on Version 2**:
   - A fresh **ApprovalCard** for Version 2 is rendered with the new content hash and revised total.
   - Click **Approve for version 2**.
9. **Razorpay Payment Execution (Admitted Exactly Once)**:
   - Click **Submit version 2 to the kernel**.
   - The kernel validates that merchant state matches the approved facts, consumes the approval once, and issues an **Execution Grant**.
   - Click **Pay with Razorpay**.
10. **Money Action Proof Chain**:
    - In mock mode, the simulated payment dialog returns the `{razorpay_order_id, razorpay_payment_id, razorpay_signature}` callback.
    - Notice that browser return is NOT treated as capture proof: the page remains in *Payment pending verification* until provider capture evidence arrives.
11. **Merchant Retained Revenue Evidence**:
    - Capture evidence is recorded into the PostgreSQL event timeline.
    - The order status transitions to **Order Confirmed** with full audit logs and links to the order tracking page.

---

## 3. Experiencing the AI Shopping Agent

Click the floating **✨ Ask Zepto AI** button at the bottom-right of the storefront:
- **Natural Language Shopping**: Try typing in Hindi, Hinglish, or English (e.g. `2 packet doodh add kar do`).
- **Tool Transparency**: Watch the agent execute labeled deterministic tools (`search_catalogue`, `modify_basket`, `propose_checkout`).
- **Hero Moment Simulation**: Click the quick prompt **⚡ "Simulate Price Shift Refusal"** to immediately witness the transaction kernel reject stale facts and render the interactive **Refusal Hero Card** with before-and-after price deltas.

---

## 4. Automated End-to-End Test (Playwright)

To run the automated test that programmatically walks the whole eleven-step journey across both Desktop Chrome and the **390px iPhone Viewport**:

```bash
cd apps/buyer-web
npm run e2e
```

---

## 5. Live Mode Verification (PostgreSQL & Commerce API)

When running against the full backend stack:

```bash
# 1. Start PostgreSQL 16
# 2. Run database migrations
uv run --package platform-db alembic upgrade head

# 3. Start Commerce API
uv run python -m commerce_api

# 4. Start Storefront against live API
cd apps/buyer-web
NEXT_PUBLIC_API_MODE=live npm run dev
```
