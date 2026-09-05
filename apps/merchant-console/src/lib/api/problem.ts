/**
 * RFC 9457 problem documents, which is how this API reports every failure.
 *
 * The console differs from the storefront in one respect: it shows the operator the whole
 * problem rather than one reassuring sentence. An operator debugging a stuck queue needs
 * the status, the title, the detail and any extension member the API attached; softening
 * that into "something went wrong" would remove the only information the screen exists to
 * carry.
 */

/** A parsed `application/problem+json` body, plus the status it arrived with. */
export interface Problem {
  type: string;
  title: string;
  status: number;
  detail?: string;
  /** Extension members: the API attaches `checkout_id`, `merchant_id`, `code` and friends. */
  [key: string]: unknown;
}

/** Thrown by the API client for anything that is not a 2xx. Never for a denial. */
export class ApiError extends Error {
  readonly problem: Problem;
  readonly status: number;

  constructor(problem: Problem) {
    super(problem.detail || problem.title || `Request failed with ${problem.status}`);
    this.name = "ApiError";
    this.problem = problem;
    this.status = problem.status;
  }
}

const GENERIC: Readonly<Record<number, string>> = {
  0: "Could not reach the platform.",
  401: "This operator session has expired.",
  403: "This operator session is not allowed to do that.",
  404: "Not found, or not visible to this session.",
  409: "That has already changed.",
  422: "The request was not valid.",
  429: "Too many requests just now.",
  500: "The API raised an unhandled error.",
  503: "The platform is unavailable.",
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
    title: "Could not reach the platform",
    status: 0,
    detail: cause instanceof Error ? cause.message : String(cause),
  };
}

/** The problem behind any thrown value, so a panel can render it without narrowing twice. */
export function problemOf(error: unknown): Problem {
  if (error instanceof ApiError) return error.problem;
  return {
    type: "about:blank",
    title: "Unexpected failure in the console",
    status: 0,
    detail: error instanceof Error ? error.message : String(error),
  };
}

/**
 * The extension members of a problem, which is where the API puts what an operator needs.
 *
 * The five standard members are stripped; everything else is returned in the order the
 * server sent it, stringified for display and never reinterpreted.
 */
export function extensionsOf(problem: Problem): Array<[string, string]> {
  const standard = new Set(["type", "title", "status", "detail", "instance"]);
  return Object.entries(problem)
    .filter(([key]) => !standard.has(key))
    .map(([key, value]) => [key, typeof value === "string" ? value : JSON.stringify(value)] as [string, string]);
}
