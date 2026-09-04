import { BROWSER_API_BASE, createLiveClient, type ApiMode, type CommerceClient } from "./client";
import { createMockClient } from "./mock";
import { ensureSession } from "./session-client";

/** Build-time selection. Default is live; `NEXT_PUBLIC_API_MODE=mock` walks the fixture. */
export const API_MODE: ApiMode = process.env.NEXT_PUBLIC_API_MODE === "mock" ? "mock" : "live";

/** Display only: the browser never calls this origin directly (see /api/backend). */
export const API_BASE_LABEL = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

let singleton: CommerceClient | null = null;

export function getClient(): CommerceClient {
  if (!singleton) {
    singleton =
      API_MODE === "mock"
        ? createMockClient()
        : createLiveClient({ baseUrl: BROWSER_API_BASE, onUnauthorized: ensureSession });
  }
  return singleton;
}

export type { CommerceClient } from "./client";
