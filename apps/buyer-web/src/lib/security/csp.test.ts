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

  it("frames the provider, which the default policy refuses outright", () => {
    expect(directive("frame-src", policy)).toEqual([
      "https://api.razorpay.com",
      "https://checkout.razorpay.com",
    ]);
    expect(directive("frame-src", defaultPolicy(NONCE))).toEqual(["'none'"]);
  });

  it("keeps the nonce and 'self' rather than delegating trust to the script", () => {
    expect(directive("script-src", policy)).toEqual(expect.arrayContaining(["'self'", `'nonce-${NONCE}'`]));
    expect(policy).not.toContain("strict-dynamic");
    expect(defaultPolicy(NONCE)).not.toContain("strict-dynamic");
  });
});

describe("defaultPolicy", () => {
  it("names no Razorpay origin anywhere, which is the whole point of splitting the policy", () => {
    expect(defaultPolicy(NONCE)).not.toContain("razorpay.com");
  });
});

describe("requiresOwnDocument", () => {
  it("is true exactly where the policy differs", () => {
    // A Content-Security-Policy belongs to a document, so a route carrying its own policy
    // has to be *entered* as one. Reaching `/checkout/x` by a client-side push keeps the
    // previous page's policy and the payment script is refused on arrival.
    for (const path of ["/checkout", "/checkout/01a07128-747e-76ad-8a0b-bb65e36fe9da"]) {
      expect(requiresOwnDocument(path)).toBe(true);
      expect(isCheckoutPath(path)).toBe(true);
    }
    for (const path of ["/", "/basket", "/orders", "/p/amul-taaza", "/checkoutish"]) {
      expect(requiresOwnDocument(path)).toBe(false);
      expect(isCheckoutPath(path)).toBe(false);
    }
  });
});
