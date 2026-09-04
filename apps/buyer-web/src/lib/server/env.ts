/**
 * Server-only runtime facts. Nothing here is a secret: the API base is a public origin
 * and the session cookie name is a convention. Razorpay keys never appear in this app.
 */

export const SESSION_COOKIE = "buyer_session";

export function apiMode(): "live" | "mock" {
  return process.env.NEXT_PUBLIC_API_MODE === "mock" ? "mock" : "live";
}

/** Upstream FastAPI origin used by route handlers. `API_BASE` (server-only) overrides. */
export function apiBase(): string {
  return (process.env.API_BASE ?? process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000").replace(/\/$/, "");
}

export function isProduction(): boolean {
  return process.env.NODE_ENV === "production";
}
