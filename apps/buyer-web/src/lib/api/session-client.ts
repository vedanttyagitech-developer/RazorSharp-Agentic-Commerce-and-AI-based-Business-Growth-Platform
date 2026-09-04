/**
 * Browser-side session bootstrap. Talks only to the same-origin /api/session route
 * handler; the bearer token itself is never returned to JavaScript.
 */
import { SessionInfoSchema, type SessionInfo } from "./types";

let inflight: Promise<boolean> | null = null;

export async function readSession(): Promise<SessionInfo | null> {
  try {
    const response = await fetch("/api/session", { method: "GET", cache: "no-store" });
    if (!response.ok) return null;
    const parsed = SessionInfoSchema.safeParse(await response.json());
    return parsed.success ? parsed.data : null;
  } catch {
    return null;
  }
}

/** Mint a demo session (POST /v1/demo/sessions server-side). Coalesces concurrent calls. */
export function ensureSession(): Promise<boolean> {
  if (!inflight) {
    inflight = (async () => {
      try {
        const response = await fetch("/api/session", { method: "POST", cache: "no-store" });
        return response.ok;
      } catch {
        return false;
      } finally {
        inflight = null;
      }
    })();
  }
  return inflight;
}

export async function endSession(): Promise<void> {
  try {
    await fetch("/api/session", { method: "DELETE" });
  } catch {
    // Nothing to recover: the cookie is HttpOnly and expires on its own.
  }
}
