/**
 * The refusal screen. This is the most important test in the app.
 *
 * Everything else on this storefront can be wrong and be embarrassing. This screen can be
 * wrong and be a lie about somebody's money, in either direction: telling a buyer they
 * were not charged when a payment attempt is live, or leaving them to assume they were
 * when the kernel refused before anything was created. Nine codes reach this component and
 * they do not mean the same thing, so the tests are organised by what the card is
 * *licensed to claim* on each of them rather than by what it happens to render.
 *
 * Two properties are checked over and over:
 *
 *  1. Every delta the server sent appears. A refusal that hides a row is a refusal the
 *     buyer cannot check, and the delta list is the entire evidence for "what moved".
 *  2. No figure appears that the decision body does not support. The strongest form of
 *     that assertion is the one used on the Safe Mode case: the rendered card contains no
 *     rupee sign anywhere, because the kernel sent no amounts and there is nothing
 *     honest to draw.
 *
 * The fixtures are the bodies the live API sent on 2026-09-05, captured by walking the
 * documented reproduction: quote a basket, open a checkout, approve version 1, inject a
 * `PRICE_SET`, submit. Where a branch needs a body the reproduction does not reach in one
 * pass — Safe Mode, an unknown code — the captured body is reused with the named fields
 * changed and nothing else, and the comment says which.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";

import type { Checkout, SubmitResult } from "@/lib/api/types";

import { RefusalCard, reasonSentence } from "./refusal-card";

afterEach(cleanup);

/* ------------------------------------------------------------- the fixtures */

/**
 * `POST /v1/checkouts/{id}/versions/1/submit` after AMUL-DAIRY-001 moved from 2800 to
 * 3137 paise. Captured 2026-09-05, HTTP 200. The refusal the platform exists to make
 * legible: no state, no command id, a populated delta list and a successor version.
 */
const REAPPROVAL: SubmitResult = {
  decision_id: "01a06fae-6f57-7986-9148-f9276d668c1e",
  allowed: false,
  code: "REAPPROVAL_REQUIRED",
  outcome: "REAPPROVAL_REQUIRED",
  explanation: "merchant_state_changed_since_approval",
  checkout: {
    checkout_id: "01a06fae-2b52-755b-a664-ea5d66e5bc22",
    version: 1,
    content_hash: "EnxjUFkU8cDvxmBRm3vSgooDR58b5fBK6pqBxS2C-pc",
  },
  deltas: [{ field_path: "total", approved: 8550, current: 9224, reason: "total_changed" }],
  grant_id: null,
  attempt_id: null,
  payment_attempt_id: null,
  next_version: 2,
  correlation_id: "01a06fae-6f4a-7b00-a1a4-b3ad6013d4d2",
};

/** `GET /v1/checkouts/{id}` read back straight after that refusal. Captured 2026-09-05. */
const CHECKOUT_AFTER_REFUSAL: Checkout = {
  checkout_id: "01a06fae-2b52-755b-a664-ea5d66e5bc22",
  basket_id: "01a06fae-0e23-7450-ad32-36006135fec8",
  state: "APPROVAL_REQUIRED",
  current_version: 2,
  versions: [
    {
      version: 1,
      state: "INVALIDATED",
      content_hash: "EnxjUFkU8cDvxmBRm3vSgooDR58b5fBK6pqBxS2C-pc",
      policy_receipt_hash: "YrQ5gD_F6xVbKxYewANcg3UKMLm07aO1S6SjclylIuI",
      amount_minor: 8550,
      currency: "INR",
      created_at: "2026-09-05T03:48:00.461426Z",
      approval: {
        approval_id: "01a06fae-50a8-7e9a-8d45-835f6784755e",
        version: 1,
        content_hash: "EnxjUFkU8cDvxmBRm3vSgooDR58b5fBK6pqBxS2C-pc",
        policy_receipt_hash: "YrQ5gD_F6xVbKxYewANcg3UKMLm07aO1S6SjclylIuI",
        amount_minor: 8550,
        currency: "INR",
        approved_at: "2026-09-05T03:48:10.019162Z",
        expires_at: "2026-09-05T03:58:10.019162Z",
        authority_epoch: 0,
      },
    },
    {
      version: 2,
      state: "APPROVAL_REQUIRED",
      content_hash: "MzFoBqp7Z35dzLDkNoQfYFzkGIgH6G2dHZhmKNGMDvI",
      policy_receipt_hash: "SuUWvKxDTnuYUnGDMWcuTHhf_EECKzxk6WX_Am-VpJM",
      amount_minor: 9224,
      currency: "INR",
      created_at: "2026-09-05T03:48:17.866755Z",
      approval: null,
    },
  ],
  approval_card: {
    checkout_id: "01a06fae-2b52-755b-a664-ea5d66e5bc22",
    version: 2,
    content_hash: "MzFoBqp7Z35dzLDkNoQfYFzkGIgH6G2dHZhmKNGMDvI",
    policy_receipt_id: "01a06fae-6f62-7dd4-80d1-df25b8f1f6bc",
    policy_receipt_hash: "SuUWvKxDTnuYUnGDMWcuTHhf_EECKzxk6WX_Am-VpJM",
    amount_minor: 9224,
    currency: "INR",
    total: { minor: 9224, currency: "INR", display: "92.24" },
    expires_at: "2026-09-05T04:03:17.866755Z",
    reservation: {
      reservation_id: "01a06fae-6f5f-7f96-bade-59fdb78d4604",
      state: "ACTIVE",
      expires_at: "2026-09-05T04:03:17.866755Z",
    },
    quote: null,
    previous_version: 1,
    deltas: [{ field_path: "total", approved: 8550, current: 9224, reason: "total_changed" }],
  },
  attempt: null,
  order_id: null,
  deltas: [{ field_path: "total", approved: 8550, current: 9224, reason: "total_changed" }],
  cancellable: true,
  updated_at: "2026-09-05T03:48:17.866755Z",
};

/**
 * Submitting the already-admitted version a second time. Captured 2026-09-05.
 *
 * `decision_id` and the ref's `content_hash` are both null in the real body: the
 * single-winner index answered without taking a decision of its own.
 */
const DUPLICATE: SubmitResult = {
  decision_id: null,
  allowed: false,
  code: "DUPLICATE_OPERATION",
  outcome: "DUPLICATE_OPERATION",
  explanation: "attempt_already_exists_for_checkout",
  checkout: {
    checkout_id: "01a06fae-2b52-755b-a664-ea5d66e5bc22",
    version: 2,
    content_hash: null,
  },
  deltas: [],
  grant_id: null,
  attempt_id: "01a06fb0-db88-7328-9453-a85fd47c10ab",
  payment_attempt_id: "01a06fb0-db88-7328-9453-a85fd47c10ab",
  next_version: null,
  correlation_id: null,
};

/** The same checkout once a submission had been admitted. Captured 2026-09-05. */
const CHECKOUT_WITH_ATTEMPT: Checkout = {
  ...CHECKOUT_AFTER_REFUSAL,
  state: "AWAITING_PAYMENT",
  attempt: {
    attempt_id: "01a06fb0-db88-7328-9453-a85fd47c10ab",
    version: 2,
    state: "SUBMITTED",
    razorpay_order_id: "order_TYDHez32NF91qq",
    razorpay_payment_id: null,
    grant_id: "01a06fb0-db8a-7a91-9f00-f279db217b8f",
    capture_evidence: null,
    reconciliation_attempts: 0,
  },
  cancellable: false,
};

const noop = () => undefined;

/* --------------------------------------- every delta the server sent appears */

describe("it renders every delta the server sent", () => {
  it("draws the captured refusal's one delta as a row, in the kernel's own path", () => {
    render(
      <RefusalCard
        decision={REAPPROVAL}
        checkout={CHECKOUT_AFTER_REFUSAL}
        approvedVersion={1}
        onReview={noop}
      />,
    );

    const changed = screen.getByLabelText("What changed");
    expect(within(changed).getByText("total", { selector: "code" })).toBeDefined();
    expect(within(changed).getByText("₹85.50")).toBeDefined();
    expect(within(changed).getByText("₹92.24")).toBeDefined();
    expect(within(changed).getByText("+₹6.74")).toBeDefined();
    expect(within(changed).getByText("total_changed", { selector: "code" })).toBeDefined();
  });

  it("draws a row for each of them when the kernel listed several", () => {
    const many: SubmitResult = {
      ...REAPPROVAL,
      deltas: [
        { field_path: "total", approved: 8550, current: 9224, reason: "total_changed" },
        {
          field_path: "lines[AMUL-DAIRY-001].unit_price_minor",
          approved: 2800,
          current: 3137,
          reason: "price_changed",
        },
        {
          field_path: "lines[AMUL-DAIRY-001].is_available",
          approved: true,
          current: false,
          reason: "stock_exhausted",
        },
        { field_path: "catalogue_revision", approved: 16, current: 17, reason: null },
      ],
    };
    render(
      <RefusalCard
        decision={many}
        checkout={CHECKOUT_AFTER_REFUSAL}
        approvedVersion={1}
        onReview={noop}
      />,
    );

    const changed = screen.getByLabelText("What changed");
    for (const delta of many.deltas) {
      expect(within(changed).getByText(delta.field_path, { selector: "code" })).toBeDefined();
    }
    // Four rows plus the header row. Nothing was collapsed and nothing was dropped.
    expect(within(changed).getAllByRole("row")).toHaveLength(5);
  });

  it("falls back to the read model's deltas only when the decision carried none", () => {
    const withoutDeltas: SubmitResult = { ...REAPPROVAL, deltas: [] };
    render(
      <RefusalCard
        decision={withoutDeltas}
        checkout={CHECKOUT_AFTER_REFUSAL}
        approvedVersion={1}
        onReview={noop}
      />,
    );

    const changed = screen.getByLabelText("What changed");
    expect(within(changed).getByText("total", { selector: "code" })).toBeDefined();
    expect(within(changed).getByText("+₹6.74")).toBeDefined();
  });

  it("names a product from the quote, and prints the SKU when the quote did not name it", () => {
    const named: Checkout = {
      ...CHECKOUT_AFTER_REFUSAL,
      approval_card: CHECKOUT_AFTER_REFUSAL.approval_card && {
        ...CHECKOUT_AFTER_REFUSAL.approval_card,
        quote: {
          currency: "INR",
          lines: [
            {
              sku: "AMUL-DAIRY-001",
              name: "Amul Taaza Toned Milk 500 ml",
              quantity: 2,
              unit_price_minor: 3137,
              subtotal_minor: 6274,
              tax_bp: 0,
              tax_minor: 0,
            },
          ],
          items_subtotal_minor: 6274,
          items_tax_minor: 0,
          delivery_fee_minor: 2500,
          delivery_tax_minor: 450,
          total_minor: 9224,
          total: { minor: 9224, currency: "INR", display: "92.24" },
          free_delivery_applied: false,
          gap_to_free_delivery_minor: 43626,
          source: "merchant-sim:demo-grocery/v1",
          catalogue_revision: 17,
          content_hash: "MzFoBqp7Z35dzLDkNoQfYFzkGIgH6G2dHZhmKNGMDvI",
        },
      },
    };
    const decision: SubmitResult = {
      ...REAPPROVAL,
      deltas: [
        {
          field_path: "lines[AMUL-DAIRY-001].unit_price_minor",
          approved: 2800,
          current: 3137,
          reason: null,
        },
        { field_path: "lines[MAGGI-SNKS-001].quantity", approved: 1, current: 0, reason: null },
      ],
    };
    render(
      <RefusalCard decision={decision} checkout={named} approvedVersion={1} onReview={noop} />,
    );

    expect(screen.getByText("Amul Taaza Toned Milk 500 ml — unit price")).toBeDefined();
    expect(screen.getByText("MAGGI-SNKS-001 — quantity")).toBeDefined();
  });
});

/* ------------------------------------------------- the totals, and only them */

describe("the two totals", () => {
  it("shows the approved total struck through, the current total, and the difference", () => {
    render(
      <RefusalCard
        decision={REAPPROVAL}
        checkout={CHECKOUT_AFTER_REFUSAL}
        approvedVersion={1}
        onReview={noop}
      />,
    );

    const totals = screen.getByLabelText("The total you approved against the total now");
    expect(within(totals).getByText("You approved")).toBeDefined();
    expect(within(totals).getByText("₹85.50")).toBeDefined();
    expect(within(totals).getByText("It is now")).toBeDefined();
    expect(within(totals).getByText("₹92.24")).toBeDefined();
    expect(within(totals).getByText("+₹6.74")).toBeDefined();
  });

  it("takes both figures from the decision's own delta, not from the read model", () => {
    // The checkout says 8550 → 9224; the decision says otherwise. The decision is the
    // primary evidence for what was refused, and the card must not average the two.
    const disagreeing: SubmitResult = {
      ...REAPPROVAL,
      deltas: [{ field_path: "total", approved: 8550, current: 12000, reason: "total_changed" }],
    };
    render(
      <RefusalCard
        decision={disagreeing}
        checkout={CHECKOUT_AFTER_REFUSAL}
        approvedVersion={1}
        onReview={noop}
      />,
    );

    const totals = screen.getByLabelText("The total you approved against the total now");
    expect(within(totals).getByText("₹120.00")).toBeDefined();
    expect(within(totals).getByText("+₹34.50")).toBeDefined();
  });

  it("draws no difference figure when the two totals are the same", () => {
    const unchanged: SubmitResult = {
      ...REAPPROVAL,
      deltas: [{ field_path: "total", approved: 8550, current: 8550, reason: null }],
    };
    render(
      <RefusalCard
        decision={unchanged}
        checkout={CHECKOUT_AFTER_REFUSAL}
        approvedVersion={1}
        onReview={noop}
      />,
    );

    const totals = screen.getByLabelText("The total you approved against the total now");
    expect(within(totals).queryByText("Difference")).toBeNull();
  });

  it("draws no totals at all when there is no second total to put beside the first", () => {
    // A refusal with no total delta and no successor version. Comparing the approved
    // version against itself would invent a change out of a comparison with nothing.
    const noSuccessor: SubmitResult = { ...REAPPROVAL, deltas: [], next_version: null };
    const noNewVersion: Checkout = {
      ...CHECKOUT_AFTER_REFUSAL,
      current_version: 1,
      versions: [CHECKOUT_AFTER_REFUSAL.versions[0]],
      approval_card: null,
      deltas: [],
    };
    render(
      <RefusalCard
        decision={noSuccessor}
        checkout={noNewVersion}
        approvedVersion={1}
        onReview={noop}
      />,
    );

    expect(screen.queryByLabelText("The total you approved against the total now")).toBeNull();
  });
});

/* ------------------------------- what the card is licensed to say about money */

describe("what it claims about money", () => {
  it("says the buyer was not charged only when the kernel created nothing at all", () => {
    render(
      <RefusalCard
        decision={REAPPROVAL}
        checkout={CHECKOUT_AFTER_REFUSAL}
        approvedVersion={1}
        onReview={noop}
      />,
    );

    // Licensed on this one: REAPPROVAL_REQUIRED predates the attempt insert, the decision
    // named no attempt, and the checkout read back carries none.
    expect(screen.getByText("You were not charged.")).toBeDefined();
    expect(screen.queryByLabelText(/recorded about the payment attempt/)).toBeNull();
  });

  it("refuses to say it when an attempt for this checkout is already live", () => {
    render(
      <RefusalCard
        decision={DUPLICATE}
        checkout={CHECKOUT_WITH_ATTEMPT}
        approvedVersion={2}
        onReview={noop}
      />,
    );

    expect(screen.queryByText("You were not charged.")).toBeNull();
    expect(
      screen.getByText("This screen cannot tell you whether money has moved on the first attempt."),
    ).toBeDefined();
    expect(screen.getByText(/refused so that one order cannot be paid for twice/)).toBeDefined();
  });

  it("refuses to say it when the code created nothing but an attempt is on record anyway", () => {
    // The code's own branch created nothing; the checkout read back still carries an
    // attempt somebody else's submission made. The wider claim is not the card's to make.
    render(
      <RefusalCard
        decision={REAPPROVAL}
        checkout={CHECKOUT_WITH_ATTEMPT}
        approvedVersion={1}
        onReview={noop}
      />,
    );

    expect(screen.queryByText("You were not charged.")).toBeNull();
    expect(
      screen.getByText("This screen cannot tell you whether money has moved on it."),
    ).toBeDefined();
  });

  it("reports a null capture as a null capture, not as a failed payment", () => {
    render(
      <RefusalCard
        decision={DUPLICATE}
        checkout={CHECKOUT_WITH_ATTEMPT}
        approvedVersion={2}
        onReview={noop}
      />,
    );

    const evidence = screen.getByLabelText("What the platform has recorded about the payment attempt");
    expect(
      within(evidence).getByText(/no capture has been applied to this attempt/),
    ).toBeDefined();
    expect(within(evidence).getByText(/not the same as the payment having failed/)).toBeDefined();
    // The attempt's own facts, printed as they arrived.
    expect(within(evidence).getByText("SUBMITTED")).toBeDefined();
    expect(within(evidence).getByText("order_TYDHez32NF91qq")).toBeDefined();
    // No Razorpay payment id was recorded, and that is said rather than left blank.
    expect(within(evidence).getAllByText("none recorded").length).toBeGreaterThan(0);
  });

  it("names an attempt id the refusal gave when the checkout read back carries none", () => {
    const noAttemptOnRead: Checkout = { ...CHECKOUT_AFTER_REFUSAL, attempt: null };
    render(
      <RefusalCard
        decision={DUPLICATE}
        checkout={noAttemptOnRead}
        approvedVersion={2}
        onReview={noop}
      />,
    );

    const evidence = screen.getByLabelText("What the platform has recorded about the payment attempt");
    expect(within(evidence).getByText(/carried no attempt object/)).toBeDefined();
    expect(
      within(evidence).getByLabelText(/Payment attempt id: 01a06fb0-db88-7328-9453-a85fd47c10ab/),
    ).toBeDefined();
  });

  it("says when the refusal named a different attempt from the one now on the checkout", () => {
    const moved: SubmitResult = {
      ...DUPLICATE,
      attempt_id: "01a06fb0-0000-0000-0000-000000000000",
      payment_attempt_id: "01a06fb0-0000-0000-0000-000000000000",
    };
    render(
      <RefusalCard
        decision={moved}
        checkout={CHECKOUT_WITH_ATTEMPT}
        approvedVersion={2}
        onReview={noop}
      />,
    );

    expect(
      screen.getByText("a different attempt from the one on the checkout now"),
    ).toBeDefined();
  });

  it("draws no amount anywhere on a refusal that carried none", () => {
    // Safe Mode: the captured refusal body with the code, explanation, deltas and
    // successor changed to what `SAFE_MODE_ACTIVE` sends, and nothing else touched. The
    // approval is untouched, the platform has stopped, and no figure moved — so there is
    // no honest number on this screen at all.
    const safeMode: SubmitResult = {
      ...REAPPROVAL,
      code: "SAFE_MODE_ACTIVE",
      outcome: "SAFE_MODE_ACTIVE",
      explanation: "safe_mode_blocks_operation",
      deltas: [],
      next_version: null,
    };
    const untouched: Checkout = {
      ...CHECKOUT_AFTER_REFUSAL,
      state: "APPROVED",
      current_version: 1,
      versions: [CHECKOUT_AFTER_REFUSAL.versions[0]],
      approval_card: null,
      deltas: [],
    };
    const { container } = render(
      <RefusalCard
        decision={safeMode}
        checkout={untouched}
        approvedVersion={1}
        onReview={noop}
      />,
    );

    expect(container.textContent).not.toContain("₹");
    expect(screen.getByText("The platform is refusing to move money right now.")).toBeDefined();
    expect(screen.getByText(/Safe Mode is switched on/)).toBeDefined();
    expect(screen.getByText("safe_mode_blocks_operation")).toBeDefined();
    expect(screen.getByText("You were not charged.")).toBeDefined();
  });

  it("claims nothing at all on a code it has no settled sentence for", () => {
    const unknown: SubmitResult = {
      ...REAPPROVAL,
      code: "SOME_FUTURE_CODE",
      outcome: "SOME_FUTURE_CODE",
      explanation: "a_reason_this_app_has_never_seen",
      deltas: [],
      next_version: null,
    };
    const untouched: Checkout = {
      ...CHECKOUT_AFTER_REFUSAL,
      current_version: 1,
      versions: [CHECKOUT_AFTER_REFUSAL.versions[0]],
      approval_card: null,
      deltas: [],
    };
    render(
      <RefusalCard decision={unknown} checkout={untouched} approvedVersion={1} onReview={noop} />,
    );

    expect(screen.queryByText("You were not charged.")).toBeNull();
    expect(screen.getByText(/no settled sentence for/)).toBeDefined();
    // The kernel's words are shown as they arrived rather than replaced with a guess.
    expect(screen.getByText("SOME_FUTURE_CODE")).toBeDefined();
    expect(screen.getByText("a_reason_this_app_has_never_seen")).toBeDefined();
    expect(screen.getByText(/does not have a sentence for/)).toBeDefined();
  });
});

/* ------------------------------------------------------ the version it names */

describe("the version trail", () => {
  it("names the invalidated version and its replacement when there is one", () => {
    render(
      <RefusalCard
        decision={REAPPROVAL}
        checkout={CHECKOUT_AFTER_REFUSAL}
        approvedVersion={1}
        onReview={noop}
      />,
    );

    const trail = screen.getByLabelText("The version that was refused and the version that replaces it");
    expect(within(trail).getByText("1")).toBeDefined();
    expect(within(trail).getByText("2")).toBeDefined();
    expect(within(trail).getByText(/Version 1 is permanently invalidated/)).toBeDefined();
  });

  it("says no successor was created rather than pointing at the version it refused", () => {
    const noSuccessor: SubmitResult = { ...DUPLICATE, next_version: null };
    render(
      <RefusalCard
        decision={noSuccessor}
        checkout={CHECKOUT_WITH_ATTEMPT}
        approvedVersion={2}
        onReview={noop}
      />,
    );

    expect(
      screen.queryByLabelText("The version that was refused and the version that replaces it"),
    ).toBeNull();
    const section = screen.getByLabelText("What happened to the version you approved");
    expect(within(section).getByText(/No superseding version was created/)).toBeDefined();
  });
});

/* -------------------------------------------------------- consent and register */

describe("the register and the controls", () => {
  it("announces itself as a refusal by the kernel rather than as a page error", () => {
    render(
      <RefusalCard
        decision={REAPPROVAL}
        checkout={CHECKOUT_AFTER_REFUSAL}
        approvedVersion={1}
        onReview={noop}
      />,
    );

    const card = screen.getByLabelText("The transaction kernel refused this submission");
    expect(card.getAttribute("aria-live")).toBe("assertive");
    expect(screen.getByText("Refused by the transaction kernel")).toBeDefined();
    expect(screen.getByText("REAPPROVAL_REQUIRED")).toBeDefined();
    expect(screen.getByText("Approve the new version to continue")).toBeDefined();
  });

  it("labels its button as the read it performs, never as an approval", () => {
    const onReview = vi.fn();
    render(
      <RefusalCard
        decision={REAPPROVAL}
        checkout={CHECKOUT_AFTER_REFUSAL}
        approvedVersion={1}
        onReview={onReview}
      />,
    );

    // Consent to version 2 is given on version 2's own card. This press only re-reads.
    const button = screen.getByRole("button", { name: "Review version 2" });
    button.click();
    expect(onReview).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: /^Approve/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Pay/ })).toBeNull();
  });

  it("keeps every control inside the trusted surface, out of the agent's reach", () => {
    render(
      <RefusalCard
        decision={REAPPROVAL}
        checkout={CHECKOUT_AFTER_REFUSAL}
        approvedVersion={1}
        onReview={noop}
        onCancel={noop}
      />,
    );

    const surface = screen.getByLabelText("Nothing happens until you approve again. RazorAI cannot.");
    const inSurface = within(surface).getAllByRole("button");
    expect(inSurface).toHaveLength(2);
    expect(screen.getAllByRole("button")).toHaveLength(inSurface.length);
  });

  it("offers no cancellation when the checkout says it is not cancellable", () => {
    render(
      <RefusalCard
        decision={REAPPROVAL}
        checkout={CHECKOUT_AFTER_REFUSAL}
        approvedVersion={1}
        onReview={noop}
      />,
    );
    expect(screen.queryByRole("button", { name: /Cancel this order instead/ })).toBeNull();
  });

  it("says 'none sent' rather than inventing a reason when the kernel sent none", () => {
    const silent: SubmitResult = { ...REAPPROVAL, explanation: null, decision_id: null };
    render(
      <RefusalCard
        decision={silent}
        checkout={CHECKOUT_AFTER_REFUSAL}
        approvedVersion={1}
        onReview={noop}
      />,
    );

    expect(screen.getByText("none sent")).toBeDefined();
    expect(screen.getByText("none — no admission ran, so no decision was taken")).toBeDefined();
  });
});

/**
 * The vocabulary is shared between two screens, so a sentence in it may name a fact and
 * must not name an outcome.
 *
 * `payment_surface_open` is the key both screens can receive: it refuses a submission from
 * the checkout screen and a cancellation from the order screen. This was found by driving
 * it rather than by reading it -- a real cancellation was refused on the live stack at
 * HTTP 200 (checkout 01a071e7-9452-71c9-b85e-fdb3c317ba5e, code PAYMENT_PENDING, kernel
 * state AWAITING_PAYMENT) and the panel told a buyer who had pressed "Cancel this order"
 * that "a second submission was refused" -- an event they had not caused, on a screen with
 * no submit button.
 */
describe("the reason vocabulary both screens read", () => {
  it("explains payment_surface_open without claiming which request was refused", () => {
    const sentence = reasonSentence("payment_surface_open");

    // The fact is the shared part and has to survive.
    expect(sentence).toContain("payment surface");

    // The outcome is the per-screen part. Naming a submission here puts the checkout
    // screen's event on the order screen's cancel panel, which is where it read as a lie.
    expect(sentence).not.toMatch(/submission|submitted|cancelled/i);
  });
});
