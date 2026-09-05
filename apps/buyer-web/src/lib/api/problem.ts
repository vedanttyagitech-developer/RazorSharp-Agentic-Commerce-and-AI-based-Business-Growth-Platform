/**
 * RFC 9457 problem documents, which is how this API reports every failure.
 *
 * The one rule worth stating: a *denial* is not a failure. The kernel answers HTTP 200
 * with a decision object when it refuses a stale approval (ADR 0003 D15), because a
 * denial is the platform working correctly and a 4xx would invite every client in the
 * chain to retry it as though it were a fault. So this module is only ever reached by
 * transport errors, authentication problems and genuine faults -- never by a refusal.
 */

/** A parsed `application/problem+json` body, plus the status it arrived with. */
export interface Problem {
  type: string;
  title: string;
  status: number;
  detail?: string;
  /** Extension members: the API attaches `checkout_id`, `sku`, `capability` and friends. */
  [key: string]: unknown;
}

/** Thrown by the API client for anything that is not a 2xx. */
export class ApiError extends Error {
  readonly problem: Problem;
  readonly status: number;

  constructor(problem: Problem) {
    super(problem.detail || problem.title || `Request failed with ${problem.status}`);
    this.name = "ApiError";
    this.problem = problem;
    this.status = problem.status;
  }

  /** True when the session token is missing, unknown or expired: mint a new one. */
  get isUnauthenticated(): boolean {
    return this.status === 401;
  }

  /** True when the caller is authenticated but lacks the capability. Never retry. */
  get isForbidden(): boolean {
    return this.status === 403;
  }

  /** True when the server never got the request, or answered nothing usable. */
  get isTransport(): boolean {
    return this.status === 0 || this.status === 503 || this.status === 504;
  }
}

const GENERIC: Readonly<Record<number, string>> = {
  0: "Could not reach the server.",
  401: "This session has expired.",
  403: "This session is not allowed to do that.",
  404: "Not found.",
  409: "That has already changed.",
  422: "The request was not valid.",
  429: "Too many requests just now.",
  500: "Something went wrong on the server.",
  503: "The service is unavailable.",
};

/** Build a `Problem` from a `Response`, falling back when the body is not a problem doc. */
export async function problemFrom(response: Response): Promise<Problem> {
  const fallback: Problem = {
    type: "about:blank",
    title: GENERIC[response.status] ?? "Request failed",
    status: response.status,
  };
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("json")) return fallback;
  try {
    const body = (await response.json()) as Partial<Problem> | null;
    if (!body || typeof body !== "object") return fallback;
    return { ...fallback, ...body, status: response.status };
  } catch {
    return fallback;
  }
}

/** A problem for a request that never reached the server. */
export function transportProblem(cause: unknown): Problem {
  return {
    type: "about:blank",
    title: "Could not reach the server",
    status: 0,
    detail: cause instanceof Error ? cause.message : String(cause),
  };
}

/** One sentence a buyer can read. Never exposes an internal identifier. */
export function humanMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.problem.detail || error.problem.title || GENERIC[error.status] || "Something went wrong.";
  }
  return "Something went wrong.";
}
