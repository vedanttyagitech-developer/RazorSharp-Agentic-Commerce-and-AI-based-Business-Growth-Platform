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
    // A reverse-proxied deployment: the socket is same-origin, so the policy needs nothing
    // beyond `'self'`, and a stray `ws://127.0.0.1:8100` in a shipped policy would be a
    // permission granted to whatever happens to listen on the viewer's own machine.
    delete process.env.NEXT_PUBLIC_VOICE_GATEWAY_ORIGIN;
    vi.stubEnv("NODE_ENV", "production");
    try {
      expect(directive(defaultPolicy(newNonce()), "connect-src")).toEqual(["'self'"]);
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it("does not put the ticket endpoint in the policy, because minting is same-origin", () => {
    const allowed = directive(defaultPolicy(newNonce()), "connect-src");
    expect(allowed).not.toContain("http://127.0.0.1:8100");
  });
});

describe("the checkout policy", () => {
  it("keeps the voice origin and adds Razorpay's, on the checkout route only", () => {
    const checkout = directive(checkoutPolicy(newNonce()), "connect-src");
    expect(checkout).toContain("'self'");
    expect(checkout).toContain("https://api.razorpay.com");
    expect(directive(defaultPolicy(newNonce()), "connect-src")).not.toContain(
      "https://api.razorpay.com",
    );
    expect(directive(defaultPolicy(newNonce()), "frame-src")).toEqual(["'none'"]);
  });

  it("is the policy for the payment surface and nothing else", () => {
    expect(isCheckoutPath("/checkout")).toBe(true);
    expect(isCheckoutPath("/checkout/abc")).toBe(true);
    expect(isCheckoutPath("/voice")).toBe(false);
    expect(isCheckoutPath("/checkouts")).toBe(false);
  });
});
