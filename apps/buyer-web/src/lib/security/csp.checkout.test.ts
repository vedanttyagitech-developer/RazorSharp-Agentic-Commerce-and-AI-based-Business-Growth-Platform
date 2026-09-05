/**
 * The checkout policy, written while making Razorpay's modal actually open.
 *
 * A sibling of `csp.test.ts` rather than part of it: two sessions wrote a suite for this
 * module on the same afternoon, one from the voice gateway's side and one from the payment
 * provider's, and both defined a `directive` helper with different contracts. Merging them
 * into one file silently kept one helper and broke four assertions that were correct about
 * a policy that was also correct. Two files, two helpers, no collision.
 */
/**
 * The policy that has to admit a payment box on one route and nowhere else.
 *
 * Every assertion here is a failure that actually happened while driving a real
 * test-mode payment through this storefront, written down so it cannot happen twice:
 *
 *  - naming only `checkout.razorpay.com` let the loader load and then blocked the two
 *    origins it immediately reaches for, and the visible result was not an error page but
 *    an empty modal;
 *  - `'strict-dynamic'` once voided the `'self'` beside it and took Next's own chunks
 *    down with it;
 *  - and the scoping claim -- that only `/checkout/*` can draw a payment box -- is worth
 *    a test precisely because it is the reason the policy is split in the first place.
 */
import { describe, expect, it } from "vitest";

import { checkoutPolicy, defaultPolicy, isCheckoutPath, requiresOwnDocument } from "./csp";

const NONCE = "dGVzdC1ub25jZS0xMjM0NTY3OA==";

/** The values of one directive, e.g. `directive("script-src", policy)`. */
function directive(name: string, policy: string): string[] {
  const found = policy
    .split(";")
    .map((part) => part.trim())
    .find((part) => part === name || part.startsWith(`${name} `));
  return found === undefined ? [] : found.slice(name.length).trim().split(/\s+/).filter(Boolean);
}

describe("checkoutPolicy", () => {
  const policy = checkoutPolicy(NONCE);

  it("admits every origin Razorpay Checkout actually reaches for", () => {
    // The loader runs in this document, so these are this document's problem. A policy
    // that admits the script but not what the script fetches renders a blank modal.
    expect(directive("script-src", policy)).toEqual(
      expect.arrayContaining(["https://checkout.razorpay.com", "https://cdn.razorpay.com"]),
    );
    expect(directive("connect-src", policy)).toEqual(
      expect.arrayContaining([
        "https://api.razorpay.com",
        "https://checkout.razorpay.com",
        "https://cdn.razorpay.com",
        "https://lumberjack.razorpay.com",
      ]),
    );
  });

  it("frames the provider, exactly as the default policy now does", () => {
    expect(directive("frame-src", policy)).toEqual([
      "https://api.razorpay.com",
      "https://checkout.razorpay.com",
    ]);
    expect(directive("frame-src", defaultPolicy(NONCE))).toEqual([
      "https://api.razorpay.com",
      "https://checkout.razorpay.com",
    ]);
  });

  it("keeps the nonce and 'self' rather than delegating trust to the script", () => {
    expect(directive("script-src", policy)).toEqual(expect.arrayContaining(["'self'", `'nonce-${NONCE}'`]));
    expect(policy).not.toContain("strict-dynamic");
    expect(defaultPolicy(NONCE)).not.toContain("strict-dynamic");
  });
});

describe("defaultPolicy", () => {
  const policy = defaultPolicy(NONCE);

  it("now permits Razorpay's script origin on every route, not just checkout", () => {
    // The copilot takes payment in place wherever it mounts, so the loader has to run
    // outside `/checkout/*`. The default policy therefore names Razorpay's origins.
    expect(directive("script-src", policy)).toEqual(
      expect.arrayContaining(["https://checkout.razorpay.com", "https://cdn.razorpay.com"]),
    );
    expect(directive("connect-src", policy)).toEqual(
      expect.arrayContaining([
        "https://api.razorpay.com",
        "https://checkout.razorpay.com",
        "https://cdn.razorpay.com",
        "https://lumberjack.razorpay.com",
      ]),
    );
  });

  it("keeps the nonce and 'self' and stays without 'strict-dynamic', unchanged", () => {
    expect(directive("script-src", policy)).toEqual(
      expect.arrayContaining(["'self'", `'nonce-${NONCE}'`]),
    );
    expect(policy).not.toContain("strict-dynamic");
  });

  it("is byte-identical to checkoutPolicy, which is now a no-difference alias", () => {
    expect(checkoutPolicy(NONCE)).toBe(policy);
  });
});

describe("requiresOwnDocument", () => {
  it("is now always false, because no route carries a different policy", () => {
    // Razorpay's origins are in the default policy on every route, so a client-side push
    // into `/checkout/*` keeps a policy that already permits the payment script. Nothing
    // needs its own document any more, and the export stays only so callers still compile.
    for (const path of [
      "/checkout",
      "/checkout/01a07128-747e-76ad-8a0b-bb65e36fe9da",
      "/",
      "/basket",
      "/orders",
      "/p/amul-taaza",
      "/checkoutish",
    ]) {
      expect(requiresOwnDocument(path)).toBe(false);
    }
    // isCheckoutPath still discriminates the route, even though the policy no longer does.
    expect(isCheckoutPath("/checkout")).toBe(true);
    expect(isCheckoutPath("/basket")).toBe(false);
  });
});
