/**
 * RFC 9457 problem details -> typed ApiError.
 *
 * ADR 0003 D15: every HTTP error is a problem document, and kernel denials are 200 with
 * a structured decision, never 4xx. So an ApiError is always a transport, contract or
 * request failure -- a denial is data, not an exception.
 */
import type { RecoveryCode } from "./types";

export interface ProblemDetails {
  type: string;
  title: string;
  status: number;
  detail?: string;
  instance?: string;
  /** Structured recovery code when the API attaches one (spec 6.7). */
  code?: RecoveryCode | string;
  /** Any extension members the server added. */
  extensions: Record<string, unknown>;
}

export const PROBLEM_CONTENT_TYPE = "application/problem+json";

export class ApiError extends Error {
  readonly problem: ProblemDetails;
  readonly status: number;
  readonly type: string;
  readonly title: string;
  readonly detail: string | undefined;
  readonly instance: string | undefined;
  readonly code: string | undefined;

  constructor(problem: ProblemDetails, options?: { cause?: unknown }) {
    super(problem.detail ? `${problem.title}: ${problem.detail}` : problem.title, options);
    this.name = "ApiError";
    this.problem = problem;
    this.status = problem.status;
    this.type = problem.type;
    this.title = problem.title;
    this.detail = problem.detail;
    this.instance = problem.instance;
    this.code = problem.code;
  }

  get isUnauthorized(): boolean {
    return this.status === 401;
  }
}

/** The request never reached the API, or the response never came back. */
export class NetworkError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = "NetworkError";
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError;
}

const RESERVED = new Set(["type", "title", "status", "detail", "instance", "code"]);

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/**
 * Build a ProblemDetails from a decoded body and the response status.
 *
 * Tolerates: a problem document, a plain JSON object with some of the members, a text
 * body, an empty body. The HTTP status always wins over a `status` member that is
 * missing or not a number; a server that sends `{"status": "500"}` is not trusted.
 */
export function problemFromBody(
  body: unknown,
  httpStatus: number,
  fallbackTitle: string,
): ProblemDetails {
  const record = asRecord(body);
  if (!record) {
    const text = typeof body === "string" && body.trim() ? body.trim() : undefined;
    return {
      type: "about:blank",
      title: fallbackTitle || `HTTP ${httpStatus}`,
      status: httpStatus,
      detail: text && text.length <= 500 ? text : undefined,
      extensions: {},
    };
  }
  const extensions: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(record)) {
    if (!RESERVED.has(key)) extensions[key] = value;
  }
  const status = typeof record.status === "number" ? record.status : httpStatus;
  const codeCandidate = record.code ?? record.recovery_code;
  return {
    type: typeof record.type === "string" ? record.type : "about:blank",
    title:
      typeof record.title === "string" && record.title
        ? record.title
        : fallbackTitle || `HTTP ${status}`,
    status,
    detail: typeof record.detail === "string" ? record.detail : undefined,
    instance: typeof record.instance === "string" ? record.instance : undefined,
    code: typeof codeCandidate === "string" ? codeCandidate : undefined,
    extensions,
  };
}

/** Parse a non-2xx Response into an ApiError. Never throws on a malformed body. */
export async function parseProblem(response: Response): Promise<ApiError> {
  const contentType = response.headers.get("content-type") ?? "";
  let body: unknown = undefined;
  try {
    const text = await response.text();
    if (contentType.includes("json")) {
      try {
        body = JSON.parse(text);
      } catch {
        body = text;
      }
    } else {
      body = text;
    }
  } catch {
    body = undefined;
  }
  return new ApiError(problemFromBody(body, response.status, response.statusText));
}

/** A 2xx body that does not match the provisional schema. Surfaced loudly, never guessed around. */
export function contractError(path: string, issues: string): ApiError {
  return new ApiError({
    type: "urn:buyer-web:contract-mismatch",
    title: "Response did not match the provisional API contract",
    status: 0,
    detail: `${path}: ${issues}`,
    extensions: { path },
  });
}
