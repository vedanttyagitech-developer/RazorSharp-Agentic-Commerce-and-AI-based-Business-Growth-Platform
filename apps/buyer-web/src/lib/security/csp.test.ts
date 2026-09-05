/**
 * The policy, and the one property that cannot be checked by reading it.
 *
 * A `connect-src` that does not admit the origin the client actually dials fails in the
 * least legible way this app has: the browser refuses the WebSocket, the socket closes
 * with no code and no reason, and the panel says "reconnecting" against a gateway that is
 * running perfectly. So the agreement between `csp.ts` and `features/voice/session.ts` is
 * asserted here rather than maintained by two comments pointing at each other.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { defaultVoiceUrl } from "@/features/voice/session";

import { checkoutPolicy, defaultPolicy, isCheckoutPath, newNonce } from "./csp";

function directive(policy: string, name: string): string[] {
  const found = policy
    .split(";")
    .map((part) => part.trim())
    .find((part) => part === name || part.startsWith(`${name} `));
  if (found === undefined) throw new Error(`no ${name} in ${policy}`);
  return found.slice(name.length).trim().split(/\s+/).filter(Boolean);
}

const original = process.env.NEXT_PUBLIC_VOICE_GATEWAY_ORIGIN;

afterEach(() => {
  if (original === undefined) delete process.env.NEXT_PUBLIC_VOICE_GATEWAY_ORIGIN;
  else process.env.NEXT_PUBLIC_VOICE_GATEWAY_ORIGIN = original;
});

describe("connect-src and the voice client", () => {
  it("admits the gateway the client dials by default in development", () => {
    const allowed = directive(defaultPolicy(newNonce()), "connect-src");
    expect(allowed).toContain("'self'");
    expect(allowed).toContain("ws://127.0.0.1:8100");
    // The client's own answer, not a second copy of the address.
    expect(defaultVoiceUrl().startsWith("ws://127.0.0.1:8100")).toBe(true);
  });

  it("admits a gateway a deployment moved, because both read the same variable", () => {
    process.env.NEXT_PUBLIC_VOICE_GATEWAY_ORIGIN = "https://voice.example.test/";

    const allowed = directive(defaultPolicy(newNonce()), "connect-src");
    const dialled = new URL(defaultVoiceUrl()).origin;

    expect(dialled).toBe("wss://voice.example.test");
    expect(allowed).toContain("wss://voice.example.test");
  });

  it("carries no loopback address into production when no gateway is configured", () => {
    // A reverse-proxied deployment: the voice socket is same-origin, so the policy needs
    // nothing beyond `'self'` for it, and a stray `ws://127.0.0.1:8100` in a shipped
    // policy would be a permission granted to whatever happens to listen on the viewer's
    // own machine. Razorpay's origins are always present now, independent of the gateway.
    delete process.env.NEXT_PUBLIC_VOICE_GATEWAY_ORIGIN;
    vi.stubEnv("NODE_ENV", "production");
    try {
      const allowed = directive(defaultPolicy(newNonce()), "connect-src");
      expect(allowed).not.toContain("ws://127.0.0.1:8100");
      expect(allowed).toContain("'self'");
      expect(allowed).toContain("https://api.razorpay.com");
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it("does not put the ticket endpoint in the policy, because minting is same-origin", () => {
    const allowed = directive(defaultPolicy(newNonce()), "connect-src");
    expect(allowed).not.toContain("http://127.0.0.1:8100");
  });
});

describe("the checkout policy is now the default policy", () => {
  it("admits Razorpay's connect origins on every route, not just checkout", () => {
    const def = directive(defaultPolicy(newNonce()), "connect-src");
    expect(def).toContain("'self'");
    expect(def).toContain("https://api.razorpay.com");
    // checkoutPolicy is a no-difference alias, so it admits exactly the same set.
    const checkout = directive(checkoutPolicy(newNonce()), "connect-src");
    expect(checkout).toContain("https://api.razorpay.com");
    // The default policy now frames the provider rather than refusing it outright.
    expect(directive(defaultPolicy(newNonce()), "frame-src")).toEqual([
      "https://api.razorpay.com",
      "https://checkout.razorpay.com",
    ]);
  });

  it("is a no-difference alias of the default policy", () => {
    const nonce = newNonce();
    expect(checkoutPolicy(nonce)).toBe(defaultPolicy(nonce));
  });

  it("still scopes the checkout path predicate, even though the policy no longer differs", () => {
    expect(isCheckoutPath("/checkout")).toBe(true);
    expect(isCheckoutPath("/checkout/abc")).toBe(true);
    expect(isCheckoutPath("/voice")).toBe(false);
    expect(isCheckoutPath("/checkouts")).toBe(false);
  });
});
