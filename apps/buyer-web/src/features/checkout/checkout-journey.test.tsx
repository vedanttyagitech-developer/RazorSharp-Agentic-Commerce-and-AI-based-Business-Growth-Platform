/**
 * The journey screen. Sixteen states, four failures, and the two answers that are not errors.
 *
 * `RefusalCard` is tested next door for what it is licensed to *claim*. This file tests
 * the screen that decides whether that card is ever drawn — and, before that, whether the
 * buyer is looking at the state their money is actually in. Those are different failures.
 * A refusal rendered beautifully on the wrong screen is still a lie.
 *
 * Four properties, in the order a defect in them would hurt:
 *
 *  1. **A state renders as itself.** Sixteen states reach this component, five of the
 *     sixteen are routed away to `PaymentPanel`, and the four a live backend actually
 *     produces that a hand-written mock never did — `EXECUTION_PENDING`, `PAYMENT_UNKNOWN`,
 *     `RECONCILING`, `STALE_CAPTURE` — are exactly the four a storefront is tempted to
 *     collapse into one spinner. So every state is driven through the real component and
 *     the page is asserted to name that state's own sentence *and* the server's own raw
 *     string, and never another state's sentence. "Captured against a dead version" drawn
 *     over `PAID` is the worst bug this file can catch.
 *  2. **No figure survives a failed read.** There is no fixture layer in this app. When
 *     `api.checkout` rejects, the strongest assertion available is that the rendered
 *     document contains no `₹` at all — not a stale total, not a zero, nothing. A screen
 *     that produced money after a `catch` would be inventing it.
 *  3. **A failed *refresh* is not a failed read.** The last good read stays on screen and
 *     is labelled as a last good read, because blanking the page loses the buyer's place
 *     and showing it silently claims it is current.
 *  4. **`allowed: false` is a normal answer, from submit and from cancel alike.** Both
 *     routes are HTTP 200 either way. A cancellation the kernel refused has to reach the
 *     buyer as the kernel's own code and sentence; the failure mode it replaces is a
 *     screen that blinks and changes nothing.
 *
 * The fixtures are bodies the live API sent on 2026-09-05. `checkout_APPROVAL_REQUIRED`,
 * `checkout_APPROVED`, the cancel result and `checkout_CANCELLED` are one real checkout
 * walked from open to cancel; the refusal pair is the run captured for
 * `refusal-card.test.tsx` — quote, open, approve version 1, move a price, submit. Where a
 * branch needs a body neither walk reaches, the captured body is reused with the named
 * fields changed and nothing else, and the comment says which. Nothing here was written
 * from the schema alone except the payment handoff, which is marked as such and says why.
 *
 * `PaymentPanel` polls with `window.setTimeout`. The timers are real and are never
 * advanced: the first poll is 2000 ms away, every test here settles and unmounts within a
 * few milliseconds, and `afterEach(cleanup)` runs the effect cleanups that clear the
 * pending timer. No poll ever fires, so no test depends on one.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, within } from "@testing-library/react";

import { ApiError, type Problem } from "@/lib/api/problem";
import {
  CHECKOUT_STATES,
  type ApprovalResult,
  type Checkout,
  type PaymentHandoff,
  type SubmitResult,
} from "@/lib/api/types";

import { CheckoutJourney } from "./checkout-journey";

/* ------------------------------------------------------------------- the client */

/**
 * The API client, not `fetch`.
 *
 * The client parses every response against a zod schema and turns a non-2xx into an
 * `ApiError` around a problem document. Mocking `fetch` would put that parsing inside the
 * unit under test and make a schema change look like a journey bug; mocking the client
 * leaves the journey exactly one thing to get right, which is what it does with the values
 * and the exceptions it is handed.
 *
 * Idempotency keys are minted in sequence rather than as UUIDs so the assertions below can
 * name them. The journey holds a key per logical action, and the point of the tests that
 * read `mock.calls` is which of two calls got the same string.
 */
const mocks = vi.hoisted(() => {
  let minted = 0;
  return {
    api: {
      checkout: vi.fn(),
      approve: vi.fn(),
      approveAndPay: vi.fn(),
      reject: vi.fn(),
      cancel: vi.fn(),
      submitVersion: vi.fn(),
      paymentHandoff: vi.fn(),
      verifyPayment: vi.fn(),
    },
    newIdempotencyKey: vi.fn(() => `key-${(minted += 1)}`),
    resetKeys: () => {
      minted = 0;
    },
  };
});

vi.mock("@/lib/api/client", () => ({
  api: mocks.api,
  newIdempotencyKey: mocks.newIdempotencyKey,
}));

/* ------------------------------------------------------------------ the fixtures */

const CHECKOUT_ID = "01a070e0-df64-7a40-b08e-a69974e53fac";

/** `GET /v1/checkouts/{id}` on a freshly opened checkout. Captured 2026-09-05 09:23. */
const APPROVAL_REQUIRED: Checkout = {
  checkout_id: CHECKOUT_ID,
  cart_id: "01a070e0-df4b-770a-a096-805a08b646d4",
  state: "APPROVAL_REQUIRED",
  current_version: 1,
  versions: [
    {
      version: 1,
      state: "APPROVAL_REQUIRED",
      content_hash: "MXKNfySnqrmZXgLxA5JWMiQwHeQ3ijE8ybpshFJfsJA",
      policy_receipt_hash: "NaBe7SjhQCQBWQQSV1jRlk2kRZtWcGyuNFG6_Nzg1Zo",
      amount_minor: 57995,
      currency: "INR",
      created_at: "2026-09-05T09:23:00.575554Z",
      approval: null,
    },
  ],
  approval_card: {
    checkout_id: CHECKOUT_ID,
    version: 1,
    content_hash: "MXKNfySnqrmZXgLxA5JWMiQwHeQ3ijE8ybpshFJfsJA",
    policy_receipt_id: "01a070e0-df7d-7cda-b05e-7b506480b3cc",
    policy_receipt_hash: "NaBe7SjhQCQBWQQSV1jRlk2kRZtWcGyuNFG6_Nzg1Zo",
    amount_minor: 57995,
    currency: "INR",
    total: { minor: 57995, currency: "INR", display: "579.95" },
    expires_at: "2026-09-05T09:38:00.575554Z",
    reservation: {
      reservation_id: "01a070e0-df74-720b-868f-15336bb23d7f",
      state: "ACTIVE",
      expires_at: "2026-09-05T09:38:00.575554Z",
    },
    // Null in the real body: the checkout read does not repeat the line breakdown.
    quote: null,
    previous_version: null,
    deltas: [],
  },
  attempt: null,
  order_id: null,
  order_reference: null,
  deltas: [],
  cancellable: true,
  updated_at: "2026-09-05T09:23:00.575554Z",
};

/** The same checkout read back after `POST .../versions/1/approve`. Captured 2026-09-05. */
const APPROVED: Checkout = {
  ...APPROVAL_REQUIRED,
  state: "APPROVED",
  versions: [
    {
      ...APPROVAL_REQUIRED.versions[0],
      state: "APPROVED",
      approval: {
        approval_id: "01a070e0-df90-782e-9f08-c986051cb66f",
        version: 1,
        content_hash: "MXKNfySnqrmZXgLxA5JWMiQwHeQ3ijE8ybpshFJfsJA",
        policy_receipt_hash: "NaBe7SjhQCQBWQQSV1jRlk2kRZtWcGyuNFG6_Nzg1Zo",
        amount_minor: 57995,
        currency: "INR",
        approved_at: "2026-09-05T09:23:00.620621Z",
        expires_at: "2026-09-05T09:33:00.620621Z",
        authority_epoch: 0,
      },
    },
  ],
  // The server drops the card once there is nothing left to consent to.
  approval_card: null,
  updated_at: "2026-09-05T09:23:00.620621Z",
};

/** `POST .../cancel` on that approved checkout, allowed. Captured 2026-09-05. */
const CANCEL_ALLOWED: ApprovalResult = {
  allowed: true,
  code: "OK",
  explanation: "cancelled",
  checkout: {
    checkout_id: CHECKOUT_ID,
    version: 1,
    content_hash: "MXKNfySnqrmZXgLxA5JWMiQwHeQ3ijE8ybpshFJfsJA",
  },
  from_state: "APPROVED",
  grants_revoked: [],
  attempt_expired: null,
  reservation_release: "OK",
};

/** The checkout read back after that cancellation. Captured 2026-09-05. */
const CANCELLED: Checkout = {
  ...APPROVED,
  state: "CANCELLED",
  versions: [{ ...APPROVED.versions[0], state: "CANCELLED" }],
  cancellable: false,
  updated_at: "2026-09-05T09:23:00.640238Z",
};

/**
 * A cancellation the kernel refused.
 *
 * The captured `CANCEL_ALLOWED` body with the four fields the kernel writes on its
 * `deny` path changed and nothing else: `allowed`, `code`, `explanation`, `from_state`.
 * The values are the ones `transaction_kernel.checkouts.cancel` returns when it finds a
 * live payment attempt past `CREATED` while the version still reads cancellable —
 * `PAYMENT_PENDING`, `attempt_in_flight` — which is the one refusal the read model cannot
 * predict, and therefore the one the button is still on screen for. `grants_revoked`,
 * `attempt_expired` and `reservation_release` stay as a refusal sends them: nothing was
 * revoked, expired or released, because nothing was done.
 */
const CANCEL_REFUSED: ApprovalResult = {
  ...CANCEL_ALLOWED,
  allowed: false,
  code: "PAYMENT_PENDING",
  explanation: "attempt_in_flight",
  from_state: "AWAITING_PAYMENT",
  reservation_release: null,
};

/**
 * `POST /v1/checkouts/{id}/versions/1/submit` after a price moved. Captured 2026-09-05,
 * HTTP 200, from the run recorded in `refusal-card.test.tsx`. Its ids belong to that
 * checkout, and so do `REFUSED_READ` and `BEFORE_SUBMIT` below; the three are used
 * together so the refusal tests never mix two runs.
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
const REFUSED_READ: Checkout = {
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
  order_reference: null,
  deltas: [{ field_path: "total", approved: 8550, current: 9224, reason: "total_changed" }],
  cancellable: true,
  updated_at: "2026-09-05T03:48:17.866755Z",
};

/**
 * That same checkout at 03:48:10, the moment after the approval and before the submit.
 *
 * The captured post-refusal read wound back: version 2 did not exist yet, so it is dropped
 * along with the card and the deltas it carried, and version 1 — whose real approval record
 * is in the capture — was `APPROVED`. This is the same reconstruction `refusal-card.test.tsx`
 * calls `untouched`, and it is what the Pay button has to be pressed on.
 */
const BEFORE_SUBMIT: Checkout = {
  ...REFUSED_READ,
  state: "APPROVED",
  current_version: 1,
  versions: [{ ...REFUSED_READ.versions[0], state: "APPROVED" }],
  approval_card: null,
  deltas: [],
  updated_at: "2026-09-05T03:48:10.019162Z",
};

/**
 * An admitted submission.
 *
 * The captured refusal with the six fields an admission answers differently changed and
 * nothing else: `allowed`, `code`, `outcome`, `explanation`, `deltas` and `next_version`.
 * The `grant_id` and `attempt_id` an admission also carries are left null, because the
 * tests that use this one assert which screen is drawn and never read those fields; a
 * value invented for them would be a value some later test could come to believe.
 */
const ADMITTED: SubmitResult = {
  ...REAPPROVAL,
  allowed: true,
  code: "OK",
  outcome: "OK",
  explanation: null,
  deltas: [],
  next_version: null,
};

/**
 * `GET /v1/checkouts/{id}/payment`. Not captured, and deliberately marked as such.
 *
 * Reading a real handoff means opening a real checkout, and every open takes one of a
 * small pool of shared stock reservations that other work is using. So this is assembled
 * from the server's own shape rather than from the wire: the field list and nullability
 * are `PaymentHandoffOut` in `commerce_api.routers.payments`, `merchant_name` is the demo
 * tenant's (`scripts/seed_demo_tenant.py`), and `description` follows the f-string in
 * `payment_service.build_handoff` — `Cart at {merchant_name}`, which names no order
 * because at a handoff there is not one yet. The
 * amount is the captured checkout's, and the provider order id is the one the captured
 * attempt carried. Nothing in this file asserts on these values as evidence of anything;
 * the handoff exists so the payment surface renders at all, which is what lets the five
 * payment states be tested through the real panel instead of around it.
 */
const HANDOFF: PaymentHandoff = {
  checkout_id: CHECKOUT_ID,
  version: 1,
  attempt_id: "01a06fb0-db88-7328-9453-a85fd47c10ab",
  state: "SUBMITTED",
  provider: "razorpay",
  razorpay_key_id: "rzp_test_1DP5mmOlF5G5ag",
  razorpay_order_id: "order_FIXTURE0000001",
  amount_minor: 57995,
  currency: "INR",
  merchant_name: "Demo Grocery Store",
  description: "Cart at Demo Grocery Store",
};

/* ------------------------------------------------------------- problem documents */

/** `GET /v1/checkouts/{unknown}`. Captured 2026-09-05. */
const NOT_FOUND: Problem = {
  type: "about:blank",
  title: "Checkout not found",
  status: 404,
  detail: "No checkout with that identifier belongs to this session.",
  instance: `/v1/checkouts/${CHECKOUT_ID}`,
  checkout_id: CHECKOUT_ID,
};

/** Any endpoint without the bearer the route handler attaches. Captured 2026-09-05. */
const UNAUTHENTICATED: Problem = {
  type: "about:blank",
  title: "Not authenticated",
  status: 401,
  detail: "This endpoint requires an Authorization: Bearer <token> header.",
  instance: "/v1/orders",
};

/** A body the endpoint's schema rejects. Captured 2026-09-05. */
const UNPROCESSABLE: Problem = {
  type: "about:blank",
  title: "Request validation failed",
  status: 422,
  detail: "The request body, query or path did not match the endpoint's schema.",
  instance: `/v1/checkouts/${CHECKOUT_ID}`,
  errors: [
    {
      type: "int_parsing",
      loc: ["body", "quantity"],
      msg: "Input should be a valid integer, unable to parse string as an integer",
      input: "many",
    },
  ],
};

/** What this app's own proxy answers when the API is not listening. Captured 2026-09-05. */
const UNREACHABLE: Problem = {
  type: "about:blank",
  title: "The store is not reachable",
  status: 503,
  detail: "No response from http://127.0.0.1:8000.",
};

/* ---------------------------------------------------------------------- helpers */

/**
 * The fourteen titles, written out here rather than imported from `state-banner`.
 *
 * Importing `stateMeaning` would make this a test of the routing only: the table would be
 * checked against itself and a state whose sentence had been pasted over another's would
 * still pass. Written out, the test is a second opinion on the words, and the assertion
 * "this page names no other state's title" becomes an assertion two states cannot share
 * a sentence.
 */
const TITLES: Record<(typeof CHECKOUT_STATES)[number], string> = {
  DRAFT: "Not priced yet",
  QUOTED: "Priced, not held",
  RESERVED: "Stock held",
  APPROVAL_REQUIRED: "Waiting for you",
  APPROVED: "Approved, not submitted",
  EXECUTION_PENDING: "Admitted, order being created",
  AWAITING_PAYMENT: "With Razorpay",
  PAID: "Paid and recorded",
  PAYMENT_FAILED: "Payment failed",
  PAYMENT_UNKNOWN: "Outcome genuinely unknown",
  INVALIDATED: "Superseded",
  INVALIDATED_AWAITING_PAYMENT_RESULT: "Superseded while a payment may be in flight",
  CANCELLED: "Cancelled",
  EXPIRED: "Expired",
};

/**
 * The captured checkout in one state.
 *
 * `APPROVAL_REQUIRED` and `APPROVED` are the two the capture holds; every other state is
 * the captured `APPROVED` read with the single `state` field changed, which is the whole
 * of what the server would send differently for the purposes of this screen. The version
 * trail therefore still reports version 1 as `APPROVED` underneath, and that is left
 * alone deliberately: it keeps the raw string the banner prints distinguishable from the
 * raw strings the trail prints.
 */
function checkoutInState(state: string): Checkout {
  if (state === "APPROVAL_REQUIRED") return APPROVAL_REQUIRED;
  return { ...APPROVED, state };
}

/**
 * Let the promises this render started settle, inside `act`.
 *
 * Every mocked value is already resolved, so a fixed number of turns is enough and
 * nothing here waits on a timer: a poll would need 2000 ms and never gets it.
 */
async function settle(): Promise<void> {
  await act(async () => {});
  await act(async () => {});
  await act(async () => {});
}

async function press(name: string | RegExp): Promise<void> {
  const button = screen.getByRole("button", { name });
  await act(async () => {
    button.click();
  });
  await settle();
}

function renderJourney(checkoutId = CHECKOUT_ID) {
  return render(<CheckoutJourney checkoutId={checkoutId} />);
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.resetKeys();
  mocks.api.paymentHandoff.mockResolvedValue(HANDOFF);
});

afterEach(cleanup);

/**
 * Everything the screen says about the state it is in, with the version trail left out.
 *
 * The trail lists every past version by its own state, so its text legitimately contains
 * other states' names; including it would make "this screen names no other state" false on
 * every screen for a reason that has nothing to do with the claim being tested.
 */
function withoutVersionTrail(container: HTMLElement): string {
  const copy = container.cloneNode(true) as HTMLElement;
  copy.querySelector('[aria-label="Every version of this checkout"]')?.remove();
  return copy.textContent ?? "";
}

/* --------------------------------------------- every state renders as that state */

describe("the fourteen states", () => {
  it("names each state's own sentence and the server's own string, and no other state's", async () => {
    for (const state of CHECKOUT_STATES) {
      mocks.api.checkout.mockResolvedValue(checkoutInState(state));
      const { container } = renderJourney();
      await settle();
      const page = container.textContent ?? "";
      /*
       * The exclusion check reads everything except the version trail.
       *
       * The trail's own prose begins "Superseded versions are kept and shown" — and
       * `INVALIDATED`'s title is "Superseded", so a page-wide check reported every state as
       * rendering INVALIDATED's title. That is a real substring and not a real collision:
       * the trail is describing its own contents, not claiming a state.
       *
       * Reading the state banner instead would have been simpler and wrong, because
       * `APPROVAL_REQUIRED` has no banner — the approval card *is* the state — and a check
       * that skipped the one screen a buyer consents on would have been the least useful
       * place to save two lines.
       */
      const claimed = withoutVersionTrail(container);

      // The sentence a person reads.
      expect(page, `${state} did not name itself`).toContain(TITLES[state]);
      // The vocabulary the server owns, printed rather than paraphrased away. This is
      // what a support conversation and a log line have in common.
      expect(page, `${state} did not print the raw state`).toContain(state);
      expect(claimed, `${state} did not name itself outside the trail`).toContain(
        TITLES[state],
      );
      for (const other of CHECKOUT_STATES) {
        if (other === state) continue;
        /*
         * One pair cannot be separated by substring and must not be: `INVALIDATED` is
         * "Superseded" and `INVALIDATED_AWAITING_PAYMENT_RESULT` is "Superseded while a
         * payment may be in flight", so the shorter title is a strict prefix of the longer
         * one. That is the copy being deliberately related rather than confused — the
         * longer sentence is unambiguous to anyone reading past the first word — and a
         * test that demanded otherwise would be arguing with the wording rather than
         * checking the routing. Skipped by the containment relation rather than by naming
         * the pair, so a future title that swallows another is skipped for a stated reason
         * instead of failing mysteriously.
         */
        if (TITLES[state].includes(TITLES[other])) continue;
        expect(claimed, `${state} rendered ${other}'s title`).not.toContain(TITLES[other]);
      }

      cleanup();
    }
  });

  it("puts every payment state on the payment surface, not the approval surface", async () => {
    // These are the states where a provider order exists or is being created. The screen
    // that matters for them is the one that says what has and has not been confirmed —
    // and never an approve button, because the approval was spent to get here.
    //
    // The list is four now and was five, and two of the five never existed: the set used to
    // route `SUBMITTED`, `PAYMENT_PENDING` and `RECONCILING`, none of which a checkout can
    // hold, while omitting `AWAITING_PAYMENT`, which is on the happy path.
    for (const state of [
      "EXECUTION_PENDING",
      "AWAITING_PAYMENT",
      "PAYMENT_UNKNOWN",
      "INVALIDATED_AWAITING_PAYMENT_RESULT",
    ]) {
      mocks.api.checkout.mockResolvedValue(checkoutInState(state));
      renderJourney();
      await settle();

      expect(screen.getByText("Pay for version 1"), `${state} drew no payment surface`).toBeDefined();
      expect(
        screen.getByText("Your browser coming back is not proof that you paid."),
      ).toBeDefined();
      expect(screen.queryByRole("button", { name: /^Approve/ })).toBeNull();

      cleanup();
    }
  });

  it("draws the approval card on APPROVAL_REQUIRED and the paid evidence on PAID", async () => {
    mocks.api.checkout.mockResolvedValue(APPROVAL_REQUIRED);
    renderJourney();
    await settle();
    // The captured card: version 1, ₹579.95, and the hash the consent binds to.
    expect(screen.getByRole("button", { name: "Approve to pay ₹579.95" })).toBeDefined();
    expect(
      within(screen.getByLabelText("What this approval is bound to")).getByLabelText(
        "Content hash: MXKNfySnqrmZXgLxA5JWMiQwHeQ3ijE8ybpshFJfsJA",
      ),
    ).toBeDefined();
    cleanup();

    // PAID is the only state that may point at an order, and it points at the order the
    // server named rather than at one derived from the checkout id.
    mocks.api.checkout.mockResolvedValue({
      ...checkoutInState("PAID"),
      order_id: "01a06fb1-2c44-7f0e-9d61-6b1d0a2e7f55",
      order_reference: "RS-260905-TESTREF",
    } satisfies Checkout);
    renderJourney();
    await settle();
    const link = screen.getByRole("link", { name: /See the order and its evidence/ });
    expect(link.getAttribute("href")).toBe("/orders/01a06fb1-2c44-7f0e-9d61-6b1d0a2e7f55");
  });

  it("routes AWAITING_PAYMENT to the payment surface, which it did not used to do", async () => {
    // The state every buyer who reaches Razorpay Checkout passes through. The vocabulary
    // omitted it, so it fell through to the terminal branch: no payment panel, and a
    // banner rendering the enum name because no sentence existed for it. Sending it
    // anywhere but the payment panel leaves a buyer mid-payment on a screen with no
    // payment on it.
    mocks.api.checkout.mockResolvedValue(checkoutInState("AWAITING_PAYMENT"));
    const { container } = renderJourney();
    await settle();

    expect(screen.getByText("Pay for version 1")).toBeDefined();
    expect(container.textContent).toContain("With Razorpay");
    expect(container.textContent).toContain("AWAITING_PAYMENT");
    // And it says the thing that makes the state safe to show: being here is not evidence.
    expect(container.textContent).toContain("an order is not a payment");
  });

  it("renders a state this build has never heard of as itself rather than white-screening", async () => {
    // The deployed kernel writes states the shared vocabulary does not list. A storefront
    // that crashed on one would take a buyer's payment page down for a string.
    mocks.api.checkout.mockResolvedValue(checkoutInState("SOME_FUTURE_STATE"));
    const { container } = renderJourney();
    await settle();

    expect(container.textContent).toContain("SOME_FUTURE_STATE");
    expect(screen.getByText(/does not have a description for this state/)).toBeDefined();
  });
});

/* ------------------------------------------- a failed read invents nothing at all */

describe("a read that failed", () => {
  const problems: Array<[string, Problem]> = [
    ["404", NOT_FOUND],
    ["401", UNAUTHENTICATED],
    ["422", UNPROCESSABLE],
    ["503", UNREACHABLE],
  ];

  for (const [label, problem] of problems) {
    it(`says the server's own sentence on a ${label} and draws no money at all`, async () => {
      mocks.api.checkout.mockRejectedValue(new ApiError(problem));
      const { container } = renderJourney();
      await settle();

      expect(screen.getByText("This checkout could not be read")).toBeDefined();
      // The server's sentence, not one written here. `humanMessage` prefers `detail`.
      expect(screen.getByText(problem.detail as string)).toBeDefined();
      // The assertion this whole file exists for. There is no fixture layer in this app,
      // and a total drawn after a catch is a total nobody computed.
      expect(container.textContent).not.toContain("₹");
      // A dead end is not an answer: the read is offered again. And it is the only thing
      // offered — a screen that could not read the checkout cannot licence a payment on it.
      expect(screen.getByRole("button", { name: "Try again" })).toBeDefined();
      expect(screen.getAllByRole("button")).toHaveLength(1);
    });
  }

  it("re-reads when the retry is pressed, and shows what comes back", async () => {
    mocks.api.checkout.mockRejectedValueOnce(new ApiError(UNREACHABLE));
    mocks.api.checkout.mockResolvedValue(APPROVED);
    renderJourney();
    await settle();

    await press("Try again");

    expect(mocks.api.checkout).toHaveBeenCalledTimes(2);
    expect(screen.getByText("Version 1 is approved")).toBeDefined();
    expect(screen.queryByText("This checkout could not be read")).toBeNull();
  });
});

/* ------------------------------------- a failed refresh is not a failed first read */

describe("a refresh that failed after a good read", () => {
  it("keeps the last good read on screen and says it is a last good read", async () => {
    mocks.api.checkout.mockResolvedValueOnce(checkoutInState("RESERVED"));
    mocks.api.checkout.mockRejectedValue(new ApiError(UNREACHABLE));
    const { container } = renderJourney();
    await settle();

    await press("Read this checkout again");

    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("The last refresh failed:");
    expect(alert.textContent).toContain("No response from http://127.0.0.1:8000.");
    // The claim the screen must not make is that what is below is current.
    expect(alert.textContent).toContain(
      "The information below is the last good read, not necessarily what is true now.",
    );

    // And the read itself is still there: not blanked, not replaced by an error page.
    expect(container.textContent).toContain("Stock held");
    expect(container.textContent).toContain("RESERVED");
    expect(within(screen.getByLabelText("Every version of this checkout")).getByText("₹579.95")).toBeDefined();
  });

  it("clears the notice when a later refresh succeeds", async () => {
    mocks.api.checkout.mockResolvedValueOnce(checkoutInState("RESERVED"));
    mocks.api.checkout.mockRejectedValueOnce(new ApiError(UNREACHABLE));
    mocks.api.checkout.mockResolvedValue(checkoutInState("RESERVED"));
    renderJourney();
    await settle();

    await press("Read this checkout again");
    expect(screen.getByRole("alert")).toBeDefined();

    await press("Read this checkout again");
    // A stale failure notice over a good read is the same lie in the other direction.
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

/* ------------------------------------------------ the two answers that are not errors */

describe("a submission the kernel refused", () => {
  it("routes allowed:false to the refusal card and never to the new approval card", async () => {
    mocks.api.checkout.mockResolvedValueOnce(BEFORE_SUBMIT);
    mocks.api.checkout.mockResolvedValue(REFUSED_READ);
    mocks.api.submitVersion.mockResolvedValue(REAPPROVAL);
    renderJourney(BEFORE_SUBMIT.checkout_id);
    await settle();

    await press("Pay");

    expect(screen.getByLabelText("The transaction kernel refused this submission")).toBeDefined();
    expect(screen.getByText("REAPPROVAL_REQUIRED")).toBeDefined();
    // The re-read came back as APPROVAL_REQUIRED carrying version 2's card. Drawing that
    // card here would put "Approve ₹92.24" in front of a buyer who has not been told why
    // the ₹85.50 they already approved was refused.
    expect(screen.queryByRole("button", { name: /^Approve/ })).toBeNull();
    expect(screen.queryByLabelText("What this approval is bound to")).toBeNull();
    expect(screen.getByRole("button", { name: "Review version 2" })).toBeDefined();
  });

  it("draws no refusal at all when the kernel admitted the submission", async () => {
    mocks.api.checkout.mockResolvedValueOnce(BEFORE_SUBMIT);
    mocks.api.checkout.mockResolvedValue({ ...BEFORE_SUBMIT, state: "EXECUTION_PENDING" });
    mocks.api.submitVersion.mockResolvedValue(ADMITTED);
    renderJourney(BEFORE_SUBMIT.checkout_id);
    await settle();

    await press("Pay");

    expect(screen.queryByLabelText("The transaction kernel refused this submission")).toBeNull();
    expect(screen.getByText("Admitted, order being created")).toBeDefined();
    expect(screen.getByText("Pay for version 1")).toBeDefined();
  });

  it("re-reads after an admitted submit until the kernel's new state is visible", async () => {
    // The session dependency commits after the response is written, so the first re-read
    // can land before the transition. A live run showed the Approve surface, with a live Pay
    // button, over a checkout the kernel had already admitted.
    mocks.api.checkout.mockResolvedValueOnce(BEFORE_SUBMIT);
    mocks.api.checkout.mockResolvedValueOnce(BEFORE_SUBMIT);
    mocks.api.checkout.mockResolvedValueOnce(BEFORE_SUBMIT);
    mocks.api.checkout.mockResolvedValue({ ...BEFORE_SUBMIT, state: "EXECUTION_PENDING" });
    mocks.api.submitVersion.mockResolvedValue(ADMITTED);
    renderJourney(BEFORE_SUBMIT.checkout_id);
    await settle();

    await press("Pay");

    expect(await screen.findByText("Admitted, order being created", {}, { timeout: 4000 })).toBeDefined();
    expect(screen.queryByText(/could not yet see the payment order/)).toBeNull();
    expect(mocks.api.checkout.mock.calls.length).toBeGreaterThanOrEqual(3);
  });

  it("says so when an admitted submit never becomes visible, and does not invite a second approval", async () => {
    mocks.api.checkout.mockResolvedValue(BEFORE_SUBMIT);
    mocks.api.submitVersion.mockResolvedValue(ADMITTED);
    renderJourney(BEFORE_SUBMIT.checkout_id);
    await settle();

    await press("Pay");

    expect(await screen.findByText(/could not yet see the payment order/, {}, { timeout: 6000 })).toBeDefined();
    expect(screen.queryByText("Admitted, order being created")).toBeNull();
  });

  it("leaves the refusal on screen when the re-read that follows it fails", async () => {
    // The decision is the evidence. Losing it because the next GET timed out would leave
    // the buyer with a screen that never explained why their approval was not spent.
    mocks.api.checkout.mockResolvedValueOnce(BEFORE_SUBMIT);
    mocks.api.checkout.mockRejectedValue(new ApiError(UNREACHABLE));
    mocks.api.submitVersion.mockResolvedValue(REAPPROVAL);
    renderJourney(BEFORE_SUBMIT.checkout_id);
    await settle();

    await press("Pay");

    expect(screen.getByLabelText("The transaction kernel refused this submission")).toBeDefined();
    expect(
      screen.getByText("The decision was recorded but this page could not re-read the checkout."),
    ).toBeDefined();
  });
});

describe("a cancellation the kernel refused", () => {
  it("renders the kernel's verdict rather than blinking and changing nothing", async () => {
    mocks.api.checkout.mockResolvedValue(APPROVED);
    mocks.api.cancel.mockResolvedValue(CANCEL_REFUSED);
    renderJourney();
    await settle();

    await press("Cancel this order");

    const verdict = screen.getByLabelText("Your cancellation was refused");
    // The buyer pressed a button expecting the order to end, so this interrupts.
    expect(verdict.getAttribute("aria-live")).toBe("assertive");
    expect(within(verdict).getByText("Cancellation refused")).toBeDefined();
    // The kernel's own three fields, in the kernel's own words.
    expect(within(verdict).getByText("PAYMENT_PENDING")).toBeDefined();
    expect(
      within(verdict).getByText(/A payment attempt for this checkout is already in flight/),
    ).toBeDefined();
    expect(within(verdict).getByText(/This checkout was not cancelled\./)).toBeDefined();
    // `from_state` is the kernel's reading of where the checkout was when it refused, and
    // it is worth more than a reassurance: it is the state the cancellation broke against.
    expect(within(verdict).getByText("AWAITING_PAYMENT")).toBeDefined();

    // And the screen did not quietly adopt the refusal's state as the checkout's state.
    expect(screen.getByText("Version 1 is approved")).toBeDefined();
  });

  it("re-reads the checkout after a refused cancellation instead of asserting a state", async () => {
    mocks.api.checkout.mockResolvedValue(APPROVED);
    mocks.api.cancel.mockResolvedValue(CANCEL_REFUSED);
    renderJourney();
    await settle();

    await press("Cancel this order");

    // Once on mount, once after the refusal: where the checkout stands now is the
    // server's answer, not the refusal's `from_state`.
    expect(mocks.api.checkout).toHaveBeenCalledTimes(2);
  });

  it("shows no verdict when the kernel performed the cancellation", async () => {
    mocks.api.checkout.mockResolvedValueOnce(APPROVED);
    mocks.api.checkout.mockResolvedValue(CANCELLED);
    mocks.api.cancel.mockResolvedValue(CANCEL_ALLOWED);
    renderJourney();
    await settle();

    await press("Cancel this order");

    expect(screen.queryByLabelText("Your cancellation was refused")).toBeNull();
    expect(screen.getByText("Cancelled")).toBeDefined();
    expect(screen.getByText(/Any hold on stock has been released and nothing was charged/)).toBeDefined();
  });
});

/* ---------------------------------------------------------------- idempotency keys */

describe("the idempotency keys", () => {
  it("replays the same key when an approve is retried after a failure", async () => {
    // The button is one act now -- approve and admit in a single transaction -- so this
    // watches `approveAndPay`. What it is protecting has not changed: a first attempt whose
    // response was lost may already have recorded the buyer's consent, so the retry must be
    // the same request or the buyer consents twice.
    mocks.api.checkout.mockResolvedValue(APPROVAL_REQUIRED);
    mocks.api.approveAndPay.mockRejectedValueOnce(new ApiError(UNREACHABLE));
    mocks.api.approveAndPay.mockResolvedValue({ allowed: true, decision_id: null, code: "ADMITTED" });
    renderJourney();
    await settle();

    await press(/^Approve/);
    expect(screen.getByRole("alert").textContent).toContain("No response from http://127.0.0.1:8000.");

    await press(/^Approve/);

    expect(mocks.api.approveAndPay).toHaveBeenCalledTimes(2);
    const [first, second] = mocks.api.approveAndPay.mock.calls;
    // Named rather than merely compared: two calls that both sent no key at all would
    // satisfy an equality check and would be the bug this test is here to catch.
    expect(first[1]).toBe("key-1");
    // The first attempt may have reached the server and recorded an approval whose
    // response was lost. The retry has to be the same request, or the buyer consents twice.
    expect(second[1]).toBe("key-1");
    // And the card is echoed back whole, so the server compares the bytes that were on
    // screen rather than trusting an id and a version.
    expect(first[0]).toBe(APPROVAL_REQUIRED.approval_card);
  });

  it("replays the same key when a submit is retried after a failure", async () => {
    mocks.api.checkout.mockResolvedValue(BEFORE_SUBMIT);
    mocks.api.submitVersion.mockRejectedValueOnce(new ApiError(UNREACHABLE));
    mocks.api.submitVersion.mockResolvedValue(REAPPROVAL);
    renderJourney(BEFORE_SUBMIT.checkout_id);
    await settle();

    await press("Pay");
    await press("Pay");

    expect(mocks.api.submitVersion).toHaveBeenCalledTimes(2);
    const [first, second] = mocks.api.submitVersion.mock.calls;
    expect(first[2]).toBe("key-1");
    expect(second[2]).toBe("key-1");
  });

  it("spends the key once a submit has been answered, so a second press is a second intention", async () => {
    // The kernel answered; the GET that followed did not. The screen keeps the last good
    // read — APPROVED — so Pay is still there, and pressing it again is a new submission
    // the kernel must be free to refuse as a duplicate rather than a replay it must repeat.
    mocks.api.checkout.mockResolvedValueOnce(BEFORE_SUBMIT);
    mocks.api.checkout.mockRejectedValue(new ApiError(UNREACHABLE));
    mocks.api.submitVersion.mockResolvedValue(ADMITTED);
    renderJourney(BEFORE_SUBMIT.checkout_id);
    await settle();

    await press("Pay");
    expect(
      screen.getByText("The decision was recorded but this page could not re-read the checkout."),
    ).toBeDefined();

    await press("Pay");

    expect(mocks.api.submitVersion).toHaveBeenCalledTimes(2);
    const [first, second] = mocks.api.submitVersion.mock.calls;
    expect(first[2]).toBe("key-1");
    expect(second[2]).toBe("key-2");
    // Both submissions named the version the buyer approved, not a version derived here.
    expect(first[1]).toBe(1);
    expect(second[1]).toBe(1);
  });

  it("spends the key once a cancellation has been answered, refusal included", async () => {
    mocks.api.checkout.mockResolvedValue(APPROVED);
    mocks.api.cancel.mockResolvedValue(CANCEL_REFUSED);
    renderJourney();
    await settle();

    await press("Cancel this order");
    await press("Cancel this order");

    expect(mocks.api.cancel).toHaveBeenCalledTimes(2);
    const [first, second] = mocks.api.cancel.mock.calls;
    // A refusal is a completed operation with a definite answer. A later press is a new
    // intention against a checkout that may have moved, not a retry of a lost response.
    expect(first[2]).toBe("key-1");
    expect(second[2]).toBe("key-2");
  });
});

/* -------------------------------------------------------------- the version trail */

describe("the version trail", () => {
  it("renders every version the server sent, superseded ones included", async () => {
    mocks.api.checkout.mockResolvedValue(REFUSED_READ);
    renderJourney(REFUSED_READ.checkout_id);
    await settle();

    const trail = within(screen.getByLabelText("Every version of this checkout"));
    const rows = trail.getAllByRole("listitem");
    expect(rows).toHaveLength(2);

    // Version 1 is the evidence that an approval was made and then invalidated. Hiding it
    // would leave this screen unable to say what changed.
    expect(within(rows[0]).getByText("v1")).toBeDefined();
    expect(within(rows[0]).getByText("INVALIDATED")).toBeDefined();
    expect(within(rows[0]).getByText("₹85.50")).toBeDefined();
    // The approval's own instant, printed without a locale so the server and the browser
    // cannot disagree about what it says.
    expect(within(rows[0]).getByText("approved 2026-09-05 03:48:10 UTC")).toBeDefined();

    // Version 2 carries its own amount. The two must not be drawn with one figure.
    expect(within(rows[1]).getByText("v2")).toBeDefined();
    expect(within(rows[1]).getByText("APPROVAL_REQUIRED")).toBeDefined();
    expect(within(rows[1]).getByText("₹92.24")).toBeDefined();
    expect(within(rows[1]).queryByText(/^approved /)).toBeNull();
  });

  it("names each version's content hash, which is what an approval binds to", async () => {
    mocks.api.checkout.mockResolvedValue(REFUSED_READ);
    renderJourney(REFUSED_READ.checkout_id);
    await settle();

    const trail = within(screen.getByLabelText("Every version of this checkout"));
    expect(
      trail.getByLabelText("Version 1 content hash: EnxjUFkU8cDvxmBRm3vSgooDR58b5fBK6pqBxS2C-pc"),
    ).toBeDefined();
    expect(
      trail.getByLabelText("Version 2 content hash: MzFoBqp7Z35dzLDkNoQfYFzkGIgH6G2dHZhmKNGMDvI"),
    ).toBeDefined();
  });
});

/* ------------------------------------------------------ accessibility (spec 29.8) */

describe("what a screen reader is told", () => {
  it("announces the state banner politely, with both the sentence and the raw state", async () => {
    mocks.api.checkout.mockResolvedValue(APPROVED);
    renderJourney();
    await settle();

    // The state changes underneath a buyer who is watching a payment settle. A change
    // they cannot see is a change they cannot act on.
    const banner = screen.getByRole("status");
    expect(banner.getAttribute("aria-live")).toBe("polite");
    expect(within(banner).getByText("Approved, not submitted")).toBeDefined();
    expect(within(banner).getByText("APPROVED")).toBeDefined();
  });

  it("announces an action failure as an alert carrying the server's sentence", async () => {
    mocks.api.checkout.mockResolvedValue(APPROVED);
    mocks.api.submitVersion.mockRejectedValue(new ApiError(NOT_FOUND));
    renderJourney();
    await settle();

    await press("Pay");

    const alert = screen.getByRole("alert");
    expect(alert.textContent).toBe("No checkout with that identifier belongs to this session.");
  });

  it("marks the first read as busy rather than drawing an empty checkout", async () => {
    // A pending read must not look like a checkout with nothing in it.
    mocks.api.checkout.mockReturnValue(new Promise(() => {}));
    const { container } = renderJourney();

    const loading = screen.getByLabelText("Loading this checkout");
    expect(loading.getAttribute("aria-busy")).toBe("true");
    expect(container.textContent).not.toContain("₹");
  });
});
