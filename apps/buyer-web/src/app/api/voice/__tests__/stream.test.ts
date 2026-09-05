/**
 * The same-origin socket path, and what it says when it is the one answering.
 *
 * Reaching this handler always means something is misconfigured -- in a deployment, the
 * reverse proxy is not forwarding the path; in development, something asked this origin
 * for a socket the gateway serves. The test is that it says which, rather than 404ing on a
 * path that genuinely exists.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

async function loadRoute() {
  vi.resetModules();
  return import("../stream/route");
}

beforeEach(() => {
  vi.stubEnv("VOICE_GATEWAY_URL", "http://127.0.0.1:8100");
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("GET /api/voice/stream", () => {
  it("explains that a proxy serves this path, and names the address that works locally", async () => {
    const { GET } = await loadRoute();

    const response = GET();
    const body = await response.json();

    expect(response.status).toBe(503);
    expect(response.headers.get("content-type")).toContain("application/problem+json");
    expect(body.detail).toContain("ws://127.0.0.1:8100/v1/voice/stream");
    expect(body.detail).toContain("reverse proxy");
  });

  it("serves one verb and reaches nothing, because it is an explanation and not a relay", async () => {
    // A guard against someone answering a bug report by growing an upgrade proxy here.
    // The reasoning against that is recorded in lib/security/csp.ts; this keeps the file
    // honest about being a signpost -- no second verb to smuggle a transport into, and no
    // outbound call that could make its failures look like the gateway's.
    const forbidden = () => {
      throw new Error("this route must not talk to anything");
    };
    vi.stubGlobal("fetch", forbidden);

    const route = await loadRoute();
    expect(Object.keys(route).sort()).toEqual(["GET", "dynamic"]);
    expect(route.GET().status).toBe(503);

    vi.unstubAllGlobals();
  });
});
