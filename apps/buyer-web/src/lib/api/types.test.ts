/**
 * The schemas, against bodies the running API actually sent.
 *
 * Every fixture below was captured by curling `http://127.0.0.1:8000` on **2026-09-05**
 * against the seeded `demo` tenant, and pasted here verbatim apart from the ids and
 * timestamps that are unavoidably per-run. Nothing was hand-written to fit a schema. That
 * matters more here than anywhere else in this suite: these schemas were mirrored from
 * `commerce_api.schemas` by reading, and reading is how six frontend contract bugs got
 * written in the first place. A schema that accepts a shape nobody sends is a schema that
 * has never been checked.
 *
 * Each schema is asserted twice — once that it accepts the real body, and once that it
 * rejects a body that is *plausibly* wrong rather than obviously so. Plausible is the
 * point: `"2800"` instead of `2800`, an absent key where the API sends an explicit
 * `null`, a float where the API sends integer paise. Those are the shapes a redeployed
 * server or a hand-rolled mock would produce, and each of them, if accepted, renders a
 * wrong number inside something a buyer consents to.
 */
import { describe, expect, it } from "vitest";

import {
  ApprovalCardSchema,
  ApprovalResultSchema,
  CartSchema,
  CataloguePageSchema,
  CheckoutSchema,
  DeltaSchema,
  DenialSchema,
  ProductSchema,
  SearchResponseSchema,
  SubmitResultSchema,
  TurnSchema,
} from "./types";

/* ------------------------------------------------------------- the fixtures */

/** `GET /v1/catalogue/search?q=doodh&limit=2`, captured 2026-09-05. */
const SEARCH_DOODH = {
  query: "doodh",
  normalized_query: "doodh",
  locale: "en-IN",
  hits: [
    {
      sku: "AMUL-DAIRY-001",
      display_name: "Amul Taaza Toned Milk 500 ml",
      name_en: "Amul Taaza Toned Milk 500 ml",
      name_hi: "अमूल ताज़ा टोंड दूध 500 मिली",
      category: "dairy",
      unit_label: "500 ml",
      unit_price_minor: 2800,
      unit_price: { minor: 2800, currency: "INR", display: "28.00" },
      currency: "INR",
      tax_bp: 0,
      stock_units: 48,
      is_listed: true,
      is_available: true,
      freshness: {
        source: "merchant-sim:demo-grocery/v1",
        catalogue_revision: 16,
        observed_at: "2026-09-05T03:47:41.529868Z",
      },
      score: 150,
      // The live index really does send the same term twice for this query.
      matched_terms: ["doodh", "doodh"],
    },
  ],
  skus: ["AMUL-DAIRY-001"],
  freshness: {
    source: "merchant-sim:demo-grocery/v1",
    catalogue_revision: 16,
    observed_at: "2026-09-05T03:47:41.527862Z",
  },
};

/** `GET /v1/catalogue/products?limit=1`, captured 2026-09-05, counts trimmed to three. */
const CATALOGUE_PAGE = {
  products: [
    {
      sku: "AASH-STPL-002",
      display_name: "Aashirvaad Shudh Chakki Atta 5 kg",
      name_en: "Aashirvaad Shudh Chakki Atta 5 kg",
      name_hi: "आशीर्वाद शुद्ध चक्की आटा 5 किलो",
      category: "staples",
      unit_label: "5 kg",
      unit_price_minor: 25500,
      unit_price: { minor: 25500, currency: "INR", display: "255.00" },
      currency: "INR",
      tax_bp: 500,
      stock_units: 18,
      is_listed: true,
      is_available: true,
      freshness: {
        source: "merchant-sim:demo-grocery/v1",
        catalogue_revision: 17,
        observed_at: "2026-09-05T03:48:31.790379Z",
      },
    },
  ],
  next_cursor: "AASH-STPL-002",
  limit: 1,
  matched: 247,
  counts_by_category: { staples: 36, snacks: 32, condiments: 24 },
  revision: 17,
};

/** `PUT /v1/carts/{id}/lines/AMUL-DAIRY-001 {"quantity":2}`, captured 2026-09-05. */
const BASKET_QUOTED = {
  cart_id: "01a06fae-0e23-7450-ad32-36006135fec8",
  lines: [{ sku: "AMUL-DAIRY-001", quantity: 2 }],
  code: "OK",
  quote: {
    currency: "INR",
    lines: [
      {
        sku: "AMUL-DAIRY-001",
        name: "Amul Taaza Toned Milk 500 ml",
        quantity: 2,
        unit_price_minor: 2800,
        subtotal_minor: 5600,
        tax_bp: 0,
        tax_minor: 0,
      },
    ],
    items_subtotal_minor: 5600,
    items_tax_minor: 0,
    delivery_fee_minor: 2500,
    delivery_tax_minor: 450,
    total_minor: 8550,
    total: { minor: 8550, currency: "INR", display: "85.50" },
    free_delivery_applied: false,
    gap_to_free_delivery_minor: 44300,
    source: "merchant-sim:demo-grocery/v1",
    catalogue_revision: 16,
    content_hash: "4hwimFML9XbNCebMyHVRQQmcUI-VwDPWph9xkHYpzJc",
  },
  unavailable: [],
  freshness: {
    source: "merchant-sim:demo-grocery/v1",
    catalogue_revision: 16,
    observed_at: "2026-09-05T03:47:53.042153Z",
  },
  stale: false,
};

/** `POST /v1/carts/{id}/checkout`, captured 2026-09-05. Version 1's approval card. */
const APPROVAL_CARD_V1 = {
  checkout_id: "01a06fae-2b52-755b-a664-ea5d66e5bc22",
  version: 1,
  content_hash: "EnxjUFkU8cDvxmBRm3vSgooDR58b5fBK6pqBxS2C-pc",
  policy_receipt_id: "01a06fae-2b61-733c-927c-06306c2913ec",
  policy_receipt_hash: "YrQ5gD_F6xVbKxYewANcg3UKMLm07aO1S6SjclylIuI",
  amount_minor: 8550,
  currency: "INR",
  total: { minor: 8550, currency: "INR", display: "85.50" },
  expires_at: "2026-09-05T04:03:00.461426Z",
  reservation: {
    reservation_id: "01a06fae-2b5c-7474-a352-f9168749e06b",
    state: "ACTIVE",
    expires_at: "2026-09-05T04:03:00.461426Z",
  },
  quote: BASKET_QUOTED.quote,
  previous_version: null,
  deltas: [],
};

/** `POST /v1/checkouts/{id}/versions/1/approve`, captured 2026-09-05. */
const APPROVE_RESULT = {
  checkout: {
    checkout_id: "01a06fae-2b52-755b-a664-ea5d66e5bc22",
    version: 1,
    content_hash: "EnxjUFkU8cDvxmBRm3vSgooDR58b5fBK6pqBxS2C-pc",
  },
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
  state: "APPROVED",
};

/**
 * `POST /v1/checkouts/{id}/versions/1/submit` after a `PRICE_SET` injection moved
 * AMUL-DAIRY-001 from 2800 to 3137 paise. Captured 2026-09-05. **HTTP 200.**
 *
 * This is the refusal the whole platform turns on, and it arrives with no `state` and no
 * `command_id` at all, because a refused submission enqueued no command and moved no
 * version. Requiring either of those would make this body fail at the boundary.
 */
const SUBMIT_REFUSAL = {
  decision_id: "01a06fae-6f57-7986-9148-f9276d668c1e",
  allowed: false,
  code: "REAPPROVAL_REQUIRED",
  explanation: "merchant_state_changed_since_approval",
  checkout: {
    checkout_id: "01a06fae-2b52-755b-a664-ea5d66e5bc22",
    version: 1,
    content_hash: "EnxjUFkU8cDvxmBRm3vSgooDR58b5fBK6pqBxS2C-pc",
  },
  deltas: [{ field_path: "total", approved: 8550, current: 9224, reason: "total_changed" }],
  grant_id: null,
  payment_attempt_id: null,
  next_version: 2,
  correlation_id: "01a06fae-6f4a-7b00-a1a4-b3ad6013d4d2",
  outcome: "REAPPROVAL_REQUIRED",
  attempt_id: null,
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
    // The successor card really does arrive with a null quote.
    quote: null,
    previous_version: 1,
    deltas: [{ field_path: "total", approved: 8550, current: 9224, reason: "total_changed" }],
  },
};

/** The same route on an admitted submission. Captured 2026-09-05. */
const SUBMIT_ADMITTED = {
  decision_id: "01a06fb0-db88-76ea-8ff7-f5da35c8589e",
  allowed: true,
  code: "OK",
  explanation: "admitted",
  checkout: {
    checkout_id: "01a06fae-2b52-755b-a664-ea5d66e5bc22",
    version: 2,
    content_hash: "MzFoBqp7Z35dzLDkNoQfYFzkGIgH6G2dHZhmKNGMDvI",
  },
  deltas: [],
  grant_id: "01a06fb0-db8a-7a91-9f00-f279db217b8f",
  payment_attempt_id: "01a06fb0-db88-7328-9453-a85fd47c10ab",
  next_version: null,
  correlation_id: "01a06fb0-db7f-7633-aefa-d36d9cfd5f37",
  outcome: "OK",
  attempt_id: "01a06fb0-db88-7328-9453-a85fd47c10ab",
  command_id: "01a06fb0-db95-712e-9c6e-678efff78644",
  state: "EXECUTION_PENDING",
};

/**
 * Submitting the same version a second time. Captured 2026-09-05.
 *
 * Two nulls in here are the reason `CheckoutRefSchema.content_hash` and
 * `SubmitResultSchema.decision_id` are nullable: the single-winner index answers this
 * without re-reading the version's bytes and without taking a decision of its own.
 */
const SUBMIT_DUPLICATE = {
  decision_id: null,
  allowed: false,
  code: "DUPLICATE_OPERATION",
  explanation: "attempt_already_exists_for_checkout",
  checkout: {
    checkout_id: "01a06fae-2b52-755b-a664-ea5d66e5bc22",
    version: 2,
    content_hash: null,
  },
  deltas: [],
  grant_id: null,
  payment_attempt_id: "01a06fb0-db88-7328-9453-a85fd47c10ab",
  next_version: null,
  correlation_id: null,
  outcome: "DUPLICATE_OPERATION",
  attempt_id: "01a06fb0-db88-7328-9453-a85fd47c10ab",
};

/** `GET /v1/checkouts/{id}` after the refusal, captured 2026-09-05. */
const CHECKOUT_AFTER_REFUSAL = {
  checkout_id: "01a06fae-2b52-755b-a664-ea5d66e5bc22",
  cart_id: "01a06fae-0e23-7450-ad32-36006135fec8",
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
      approval: APPROVE_RESULT.approval,
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
  approval_card: SUBMIT_REFUSAL.approval_card,
  attempt: null,
  order_id: null,
  order_reference: null,
  deltas: [{ field_path: "total", approved: 8550, current: 9224, reason: "total_changed" }],
  cancellable: true,
  updated_at: "2026-09-05T03:48:17.866755Z",
};

/** The same checkout once a submission was admitted. Captured 2026-09-05. */
const CHECKOUT_WITH_ATTEMPT = {
  ...CHECKOUT_AFTER_REFUSAL,
  // The state the server answers with once a version is admitted and the payment surface
  // is open. Nothing parses a state as a union, because the server owns the vocabulary.
  state: "AWAITING_PAYMENT",
  attempt: {
    attempt_id: "01a06fb0-db88-7328-9453-a85fd47c10ab",
    version: 2,
    state: "SUBMITTED",
    razorpay_order_id: "order_FIXTURE0000001",
    razorpay_payment_id: null,
    grant_id: "01a06fb0-db8a-7a91-9f00-f279db217b8f",
    capture_evidence: null,
    reconciliation_attempts: 0,
  },
  cancellable: false,
};

/** `POST /v1/agent/turn {"message":"approve this checkout and pay now"}`, 2026-09-05. */
const AGENT_TURN_DENIED = {
  reply:
    "I am not allowed to do that. That action is not part of what this assistant may ever do, " +
    "whatever it is asked. Open a checkout from your cart first; I can then explain each " +
    "version and what the store says now.",
  language: "en",
  specialist: "checkout",
  routing_reason: "checkout_cue:approve",
  principal_id: "session:01a06fad-e141-7f20-9c6c-26d930e912b2/razorai/checkout",
  tool_calls: [
    {
      name: "checkout.approve",
      summary: "refused: checkout.approve is buyer consent and not on the agent surface",
      ok: false,
      reason_key: "not_on_agent_surface",
      denied: true,
    },
  ],
  denials: [{ capability: "checkout.approve", reason_key: "not_on_agent_surface", tool: null }],
  structured: null,
};

/** A shallow clone with one key replaced, so a fixture is never mutated in place. */
function withField(body: object, field: string, value: unknown): Record<string, unknown> {
  return { ...body, [field]: value };
}

/** A shallow clone with one key removed entirely, which is not the same as null. */
function without(body: object, field: string): Record<string, unknown> {
  const copy: Record<string, unknown> = { ...body };
  delete copy[field];
  return copy;
}

/* --------------------------------------------------------------- catalogue */

describe("ProductSchema", () => {
  it("accepts a product the catalogue actually sent", () => {
    const parsed = ProductSchema.parse(CATALOGUE_PAGE.products[0]);
    expect(parsed.unit_price.minor).toBe(25500);
    expect(parsed.unit_price.display).toBe("255.00");
    expect(parsed.tax_bp).toBe(500);
  });

  it("rejects a price sent as a decimal string", () => {
    const wrong = withField(CATALOGUE_PAGE.products[0], "unit_price_minor", "25500");
    expect(ProductSchema.safeParse(wrong).success).toBe(false);
  });

  it("rejects a price in rupees rather than integer paise", () => {
    // 255.0 is the shape a service that thinks in major units would send, and it is the
    // one that has to fail loudly: accepted, it renders ₹2.55 beside an ADD button.
    const wrong = withField(CATALOGUE_PAGE.products[0], "unit_price_minor", 255.0);
    expect(ProductSchema.safeParse(wrong).success).toBe(true);
    const fractional = withField(CATALOGUE_PAGE.products[0], "unit_price_minor", 255.5);
    expect(ProductSchema.safeParse(fractional).success).toBe(false);
  });

  it("rejects a product whose `unit_price` object is missing", () => {
    expect(ProductSchema.safeParse(without(CATALOGUE_PAGE.products[0], "unit_price")).success).toBe(
      false,
    );
  });

  it("rejects a product that does not say whether it is available", () => {
    // Absent would default to falsy in a cast, which draws "Not available" over a product
    // the merchant is selling.
    expect(
      ProductSchema.safeParse(without(CATALOGUE_PAGE.products[0], "is_available")).success,
    ).toBe(false);
    expect(ProductSchema.safeParse(without(CATALOGUE_PAGE.products[0], "is_listed")).success).toBe(
      false,
    );
  });

  it("rejects a product with no freshness stamp", () => {
    expect(ProductSchema.safeParse(without(CATALOGUE_PAGE.products[0], "freshness")).success).toBe(
      false,
    );
  });
});

describe("CataloguePageSchema", () => {
  it("accepts the page the catalogue actually sent", () => {
    const parsed = CataloguePageSchema.parse(CATALOGUE_PAGE);
    expect(parsed.matched).toBe(247);
    expect(parsed.counts_by_category.staples).toBe(36);
    expect(parsed.next_cursor).toBe("AASH-STPL-002");
  });

  it("accepts a null cursor on the last page but rejects an absent one", () => {
    expect(CataloguePageSchema.safeParse(withField(CATALOGUE_PAGE, "next_cursor", null)).success).toBe(
      true,
    );
    expect(CataloguePageSchema.safeParse(without(CATALOGUE_PAGE, "next_cursor")).success).toBe(false);
  });

  it("rejects category counts sent as strings", () => {
    const wrong = withField(CATALOGUE_PAGE, "counts_by_category", { staples: "36" });
    expect(CataloguePageSchema.safeParse(wrong).success).toBe(false);
  });
});

describe("SearchResponseSchema", () => {
  it("accepts the response the index actually sent for `doodh`", () => {
    const parsed = SearchResponseSchema.parse(SEARCH_DOODH);
    expect(parsed.hits[0].sku).toBe("AMUL-DAIRY-001");
    expect(parsed.hits[0].matched_terms).toEqual(["doodh", "doodh"]);
    expect(parsed.hits[0].score).toBe(150);
    expect(parsed.freshness.catalogue_revision).toBe(16);
  });

  it("rejects a hit with no matched terms, because the chips would then be invented", () => {
    const wrong = withField(SEARCH_DOODH, "hits", [without(SEARCH_DOODH.hits[0], "matched_terms")]);
    expect(SearchResponseSchema.safeParse(wrong).success).toBe(false);
  });

  it("rejects a score sent as a float", () => {
    const wrong = withField(SEARCH_DOODH, "hits", [
      withField(SEARCH_DOODH.hits[0], "score", 150.5),
    ]);
    expect(SearchResponseSchema.safeParse(wrong).success).toBe(false);
  });
});

/* ------------------------------------------------------------------ cart */

describe("CartSchema", () => {
  it("accepts the cart the API actually sent after one line was set", () => {
    const parsed = CartSchema.parse(BASKET_QUOTED);
    expect(parsed.quote?.total_minor).toBe(8550);
    expect(parsed.quote?.total.display).toBe("85.50");
    expect(parsed.quote?.gap_to_free_delivery_minor).toBe(44300);
    expect(parsed.stale).toBe(false);
  });

  it("accepts an unpriced cart whose quote is explicitly null", () => {
    expect(CartSchema.safeParse(withField(BASKET_QUOTED, "quote", null)).success).toBe(true);
  });

  it("rejects a cart whose quote key is absent rather than null", () => {
    // Nullable is not optional. A missing quote and a quote the merchant could not
    // produce are different facts, and only one of them is a shape this app understands.
    expect(CartSchema.safeParse(without(BASKET_QUOTED, "quote")).success).toBe(false);
  });

  it("rejects a quote whose total is only a display string", () => {
    const quote = without(BASKET_QUOTED.quote, "total_minor");
    expect(CartSchema.safeParse(withField(BASKET_QUOTED, "quote", quote)).success).toBe(false);
  });

  it("rejects a quote with no content hash to bind an approval to", () => {
    const quote = without(BASKET_QUOTED.quote, "content_hash");
    expect(CartSchema.safeParse(withField(BASKET_QUOTED, "quote", quote)).success).toBe(false);
  });
});

/* ---------------------------------------------------------------- approval */

describe("ApprovalCardSchema", () => {
  it("accepts the card `POST /v1/carts/{id}/checkout` actually answered with", () => {
    const parsed = ApprovalCardSchema.parse(APPROVAL_CARD_V1);
    expect(parsed.version).toBe(1);
    expect(parsed.amount_minor).toBe(8550);
    expect(parsed.content_hash).toBe("EnxjUFkU8cDvxmBRm3vSgooDR58b5fBK6pqBxS2C-pc");
    expect(parsed.reservation?.state).toBe("ACTIVE");
    expect(parsed.previous_version).toBeNull();
  });

  it("accepts the successor card, whose quote really is null", () => {
    const parsed = ApprovalCardSchema.parse(SUBMIT_REFUSAL.approval_card);
    expect(parsed.quote).toBeNull();
    expect(parsed.previous_version).toBe(1);
    expect(parsed.amount_minor).toBe(9224);
  });

  it("rejects a card with no content hash: there would be nothing to consent to", () => {
    expect(ApprovalCardSchema.safeParse(without(APPROVAL_CARD_V1, "content_hash")).success).toBe(
      false,
    );
  });

  it("rejects a card whose amount disagrees with itself in type", () => {
    expect(
      ApprovalCardSchema.safeParse(withField(APPROVAL_CARD_V1, "amount_minor", "8550")).success,
    ).toBe(false);
    expect(
      ApprovalCardSchema.safeParse(withField(APPROVAL_CARD_V1, "amount_minor", 85.5)).success,
    ).toBe(false);
  });

  it("rejects a card whose reservation key is absent rather than null", () => {
    expect(ApprovalCardSchema.safeParse(without(APPROVAL_CARD_V1, "reservation")).success).toBe(
      false,
    );
    expect(
      ApprovalCardSchema.safeParse(withField(APPROVAL_CARD_V1, "reservation", null)).success,
    ).toBe(true);
  });

  it("rejects a card with no deltas array, so the successor cannot hide what moved", () => {
    expect(ApprovalCardSchema.safeParse(without(APPROVAL_CARD_V1, "deltas")).success).toBe(false);
  });
});

describe("ApprovalResultSchema", () => {
  it("accepts the body `.../approve` actually answered with", () => {
    const parsed = ApprovalResultSchema.parse(APPROVE_RESULT);
    expect(parsed.state).toBe("APPROVED");
    expect(parsed.approval?.amount_minor).toBe(8550);
    expect(parsed.checkout?.version).toBe(1);
  });

  it("accepts the sibling shapes the three routes send, which share only `checkout`", () => {
    // Reject sends a state and no approval; cancel sends a verdict and no state.
    // The state it sends is CANCELLED. Declining a version ends the checkout; there is no
    // REJECTED checkout state anywhere in the platform, and this fixture used to assert
    // one, which is the same mistake `CHECKOUT_STATES` used to make.
    const rejected = { checkout: APPROVE_RESULT.checkout, state: "CANCELLED", from_state: "APPROVAL_REQUIRED" };
    expect(ApprovalResultSchema.safeParse(rejected).success).toBe(true);
    expect(ApprovalResultSchema.parse(rejected).state).toBe("CANCELLED");
    const refusedCancel = {
      checkout: APPROVE_RESULT.checkout,
      allowed: false,
      code: "DUPLICATE_OPERATION",
      explanation: "attempt_already_exists_for_checkout",
    };
    const parsed = ApprovalResultSchema.parse(refusedCancel);
    expect(parsed.allowed).toBe(false);
    expect(parsed.state).toBeUndefined();
  });

  it("rejects a body with no checkout at all", () => {
    expect(ApprovalResultSchema.safeParse(without(APPROVE_RESULT, "checkout")).success).toBe(false);
  });
});

/* --------------------------------------------------------------- the refusal */

describe("SubmitResultSchema", () => {
  it("accepts the refusal, which is a normal 200 body carrying no state and no command", () => {
    const parsed = SubmitResultSchema.parse(SUBMIT_REFUSAL);
    expect(parsed.allowed).toBe(false);
    expect(parsed.code).toBe("REAPPROVAL_REQUIRED");
    expect(parsed.explanation).toBe("merchant_state_changed_since_approval");
    expect(parsed.next_version).toBe(2);
    expect(parsed.deltas).toHaveLength(1);
    expect(parsed.deltas[0].approved).toBe(8550);
    expect(parsed.deltas[0].current).toBe(9224);
    expect(parsed.state).toBeUndefined();
    expect(parsed.command_id).toBeUndefined();
  });

  it("accepts the admitted submission, which does carry a state and a command", () => {
    const parsed = SubmitResultSchema.parse(SUBMIT_ADMITTED);
    expect(parsed.allowed).toBe(true);
    expect(parsed.state).toBe("EXECUTION_PENDING");
    expect(parsed.command_id).toBe("01a06fb0-db95-712e-9c6e-678efff78644");
    expect(parsed.next_version).toBeNull();
  });

  it("accepts the duplicate answer, whose decision id and content hash are both null", () => {
    const parsed = SubmitResultSchema.parse(SUBMIT_DUPLICATE);
    expect(parsed.decision_id).toBeNull();
    expect(parsed.checkout?.content_hash).toBeNull();
    expect(parsed.attempt_id).toBe("01a06fb0-db88-7328-9453-a85fd47c10ab");
    expect(parsed.deltas).toEqual([]);
  });

  it("rejects `allowed` sent as the string \"false\"", () => {
    // The single most expensive plausible-but-wrong shape on this platform: truthy, and
    // it would take a buyer straight past the refusal into a payment surface.
    expect(SubmitResultSchema.safeParse(withField(SUBMIT_REFUSAL, "allowed", "false")).success).toBe(
      false,
    );
  });

  it("rejects a decision with no deltas array, which would silently hide what moved", () => {
    expect(SubmitResultSchema.safeParse(without(SUBMIT_REFUSAL, "deltas")).success).toBe(false);
  });

  it("rejects a `next_version` sent as a string", () => {
    expect(
      SubmitResultSchema.safeParse(withField(SUBMIT_REFUSAL, "next_version", "2")).success,
    ).toBe(false);
  });

  it("keeps fields the server adds that this app has not been taught yet", () => {
    const extended = { ...SUBMIT_REFUSAL, some_future_field: { nested: true } };
    const parsed = SubmitResultSchema.parse(extended);
    expect(parsed).toHaveProperty("some_future_field");
  });
});

describe("DeltaSchema", () => {
  it("accepts the money delta the kernel sent", () => {
    const parsed = DeltaSchema.parse(SUBMIT_REFUSAL.deltas[0]);
    expect(parsed.field_path).toBe("total");
    expect(parsed.approved).toBe(8550);
    expect(parsed.reason).toBe("total_changed");
  });

  it("accepts a delta whose sides are not money, because a path can carry anything", () => {
    const parsed = DeltaSchema.parse({
      field_path: "lines[AMUL-DAIRY-001].is_available",
      approved: true,
      current: false,
      reason: null,
    });
    expect(parsed.current).toBe(false);
    expect(parsed.reason).toBeNull();
  });

  it("rejects a delta with no field path to name what moved", () => {
    expect(
      DeltaSchema.safeParse({ approved: 8550, current: 9224, reason: "total_changed" }).success,
    ).toBe(false);
  });
});

/* ---------------------------------------------------------------- checkout */

describe("CheckoutSchema", () => {
  it("accepts the checkout read back after the refusal", () => {
    const parsed = CheckoutSchema.parse(CHECKOUT_AFTER_REFUSAL);
    expect(parsed.current_version).toBe(2);
    expect(parsed.versions).toHaveLength(2);
    expect(parsed.versions[0].state).toBe("INVALIDATED");
    expect(parsed.versions[0].amount_minor).toBe(8550);
    expect(parsed.versions[1].amount_minor).toBe(9224);
    expect(parsed.versions[1].approval).toBeNull();
    expect(parsed.attempt).toBeNull();
    expect(parsed.order_id).toBeNull();
  });

  it("accepts the checkout once an attempt exists, in a state outside the sixteen", () => {
    const parsed = CheckoutSchema.parse(CHECKOUT_WITH_ATTEMPT);
    expect(parsed.state).toBe("AWAITING_PAYMENT");
    expect(parsed.attempt?.razorpay_order_id).toBe("order_FIXTURE0000001");
    expect(parsed.attempt?.razorpay_payment_id).toBeNull();
    expect(parsed.attempt?.capture_evidence).toBeNull();
    expect(parsed.cancellable).toBe(false);
  });

  it("rejects a checkout whose attempt key is absent rather than null", () => {
    expect(CheckoutSchema.safeParse(without(CHECKOUT_AFTER_REFUSAL, "attempt")).success).toBe(false);
  });

  it("rejects an attempt whose capture evidence key is absent", () => {
    // Absent would be read as "no capture", and this is the one field that decides
    // whether the platform believes money moved. It has to be sent, even as null.
    const attempt = without(CHECKOUT_WITH_ATTEMPT.attempt, "capture_evidence");
    expect(
      CheckoutSchema.safeParse(withField(CHECKOUT_WITH_ATTEMPT, "attempt", attempt)).success,
    ).toBe(false);
  });

  it("rejects a version summary with no content hash", () => {
    const versions = [without(CHECKOUT_AFTER_REFUSAL.versions[0], "content_hash")];
    expect(
      CheckoutSchema.safeParse(withField(CHECKOUT_AFTER_REFUSAL, "versions", versions)).success,
    ).toBe(false);
  });

  it("rejects a checkout whose state is a number", () => {
    expect(CheckoutSchema.safeParse(withField(CHECKOUT_AFTER_REFUSAL, "state", 4)).success).toBe(
      false,
    );
  });
});

/* ----------------------------------------------------------------- RazorAI */

describe("TurnSchema and DenialSchema", () => {
  it("accepts the turn the agent actually answered with when asked to approve", () => {
    const parsed = TurnSchema.parse(AGENT_TURN_DENIED);
    expect(parsed.denials).toHaveLength(1);
    expect(parsed.denials[0].capability).toBe("checkout.approve");
    expect(parsed.denials[0].reason_key).toBe("not_on_agent_surface");
    expect(parsed.tool_calls[0].ok).toBe(false);
    expect(parsed.structured).toBeNull();
  });

  it("keeps the extra keys the harness sends on a denial", () => {
    const parsed = DenialSchema.parse(AGENT_TURN_DENIED.denials[0]);
    expect(parsed).toHaveProperty("tool", null);
  });

  it("rejects a denial that names no reason", () => {
    expect(DenialSchema.safeParse({ capability: "checkout.approve" }).success).toBe(false);
  });

  it("rejects a turn whose denials arrive as a single object rather than a list", () => {
    const wrong = withField(AGENT_TURN_DENIED, "denials", AGENT_TURN_DENIED.denials[0]);
    expect(TurnSchema.safeParse(wrong).success).toBe(false);
  });

  it("rejects a turn with no reply text", () => {
    expect(TurnSchema.safeParse(without(AGENT_TURN_DENIED, "reply")).success).toBe(false);
  });
});
