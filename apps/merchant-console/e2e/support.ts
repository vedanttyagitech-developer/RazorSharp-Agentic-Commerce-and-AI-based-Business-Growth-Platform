/**
 * What every spec in this suite shares: the platform's shapes, the skip guard, and two
 * formatters that deliberately do not call the application's own.
 *
 * The formatters matter more than they look. `rupees` renders integer paise through
 * `Intl.NumberFormat` over minor/100, where `src/lib/money.ts` renders the same figure by
 * truncating and padding. Two implementations agreeing on ₹1,23,456.78 is a check; one
 * implementation compared against itself is a tautology that would keep passing through a
 * rewrite of the thing it is supposed to be testing.
 *
 * The skip guard exists because a suite that reads live data has two failure modes that
 * look identical in a terminal -- the console is broken, or nobody started the API -- and
 * only the first one is a regression. `failure-path.spec.ts` is the deliberate exception:
 * it needs a dead upstream and provides its own.
 */
import { expect, type Page } from "@playwright/test";

/** Where the platform is. The specs read it directly to decide whether to run at all. */
export const API = process.env.COMMERCE_API_URL ?? "http://127.0.0.1:8000";

// --------------------------------------------------------------------------- shapes

export interface Money {
  minor: number;
  currency: string;
  display: string;
}

export interface Session {
  merchant_id: string;
  tenant_id: string;
  actor_type: string;
  capabilities: string[];
}

export interface Counted {
  counts: Record<string, number>;
  next_cursor: string | null;
  scope: string;
}

export interface OrderSummary {
  order_id: string;
  checkout_id: string;
  state: string;
  amount: Money;
  payment_attempt_id: string;
}

export interface OrdersPage extends Counted {
  orders: OrderSummary[];
}

export interface RefundListItem {
  refund_id: string;
  state: string;
  row_status: string;
  amount: Money;
  captured_minor: number | null;
  currency: string;
}

export interface RefundsPage extends Counted {
  refunds: RefundListItem[];
}

export interface Product {
  sku: string;
  display_name: string;
  name_en: string;
  category: string;
  unit_price_minor: number;
  unit_price: Money;
  is_listed: boolean;
  is_available: boolean;
  stock_units: number;
}

export interface CataloguePage {
  products: Product[];
  next_cursor: string | null;
  matched: number;
  counts_by_category: Record<string, number>;
  revision: number;
}

export interface SearchResponse {
  normalized_query: string;
  hits: Product[];
  freshness: { source: string; catalogue_revision: number };
}

export interface SafeMode {
  mode: string;
  scope: string;
  safe_mode: boolean;
  permitted: Record<string, boolean>;
}

export interface OutboxPage {
  counts: Record<string, number>;
}

export interface RetainedRevenue {
  checkout_id: string;
  currency: string;
  stale_version: number | null;
  stale_approved_minor: number | null;
  corrected_version: number | null;
  corrected_total_minor: number | null;
  captured_minor: number | null;
  captured_from: string | null;
  difference_minor: number | null;
  direction: string;
  refunded_minor: number;
  net_retained_minor: number | null;
  explanation: string;
}

export interface AuditVerification {
  intact: boolean;
  empty: boolean;
  length: number;
  events_verified: number;
  head_seq: number | null;
  code: string;
}

export interface ProofChain {
  payment_attempt_id: string | null;
  verdict: {
    tier: string;
    ok: boolean;
    checks: Array<{ name: string; ok: boolean; applicable: boolean }>;
  };
}

/** One escalated case, as `GET /v1/review/queue` sends it. */
export interface ReviewCase {
  case_key: string;
  state: string;
  priority: string;
  reason_code: string;
  reason_family: string;
  checkout_id: string;
  payment_attempt_id: string | null;
  monetary_exposure: Money | null;
  opened_by: string;
  detections: number;
  audit_event_id: string;
  audit_seq: number;
  audit_self_hash: string;
  correlation_id: string;
  attempts_used: number | null;
  attempts_bound: number;
  target_response_by: string;
  target_response_seconds: number;
  proof_chain: { payment_attempt_id: string | null };
}

export interface Queue {
  cases: ReviewCase[];
  priority_counts: Record<string, number>;
  limit: number;
  scope: string;
}

export interface InspectorDocument {
  payment_attempt_id: string;
  checkout_id: string;
  checkout_version: number;
  state: string;
  amount_minor: number;
  currency: string;
  receipt: string;
  provider_order_id: string | null;
  provider_payment_id: string | null;
  state_history: Array<Record<string, unknown>>;
  grants: Array<Record<string, unknown>>;
  commands: Array<Record<string, unknown>>;
  provider_requests: Array<Record<string, unknown>>;
  webhook_deliveries: Array<Record<string, unknown>>;
  reconciliation_runs: Array<Record<string, unknown>>;
  order: Record<string, unknown> | null;
  refunds: Array<Record<string, unknown>>;
  findings: Array<{ name: string; ok: boolean; detail: string }>;
}

export interface InjectionDelta {
  field: string;
  before: boolean | number;
  after: boolean | number;
}

export interface Injection {
  injection_id: string;
  kind: string;
  label: string;
  sku: string | null;
  deltas: InjectionDelta[];
  revision_before: number;
  revision_after: number;
  audit_event_id: string;
  scenario_run_id: string;
}

// ------------------------------------------------------------------ refund vocabulary

/**
 * What the console says each refund state means, and who owns the row.
 *
 * Written out here rather than imported from `RefundsTab`, deliberately. A test that took
 * these strings from the component would agree with the component however the component
 * changed, including a change that quietly gave two states the same sentence -- which is
 * the exact conflation that refunds a buyer twice. This is the expectation; the component
 * is the thing under test.
 */
export const REFUND_MEANINGS: Readonly<Record<string, string>> = {
  REFUND_PENDING: "the provider was asked and has not answered — in flight, do not retry",
  REFUND_UNKNOWN: "the answer was lost; reconciliation owns this row, not an operator",
  REFUND_FAILED: "the provider said no — this refund did not happen",
};

/** The tile or row carrying one refund state's meaning. */
export function refundTile(page: Page, state: string) {
  return page.getByText(REFUND_MEANINGS[state]).first().locator("..");
}

// ----------------------------------------------------------------------- formatting

/**
 * Render integer paise the way the column should read, by a route independent of the
 * application's own formatter. This is a test asserting an expectation, not a browser
 * deriving a figure: the number itself is the server's, unmodified.
 */
export function rupees(minor: number, currency = "INR"): string {
  const symbol = currency === "INR" ? "₹" : `${currency} `;
  const body = new Intl.NumberFormat("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(Math.abs(minor) / 100);
  return `${minor < 0 ? "−" : ""}${symbol}${body}`;
}

/** Absent is an em dash and zero is an amount. Never the same string. */
export function orDash(minor: number | null, currency = "INR"): string {
  return minor === null ? "—" : rupees(minor, currency);
}

/** A count as the console groups it. */
export function count(value: number): string {
  return value.toLocaleString("en-IN");
}

// ---------------------------------------------------------------------------- reads

/** Read through the console's own proxy, on the page's operator session. */
export async function read<T>(page: Page, path: string): Promise<T> {
  const response = await page.request.get(path);
  expect(response.ok(), `${path} answered ${response.status()}`).toBeTruthy();
  return (await response.json()) as T;
}

// ----------------------------------------------------------------------- skip guard

/**
 * Ask the platform whether it is there.
 *
 * Returns the reason to skip, or null when it answered. Called once per spec file from a
 * `beforeAll`; the message is warned to the console because the list reporter prints a
 * skipped test's name and not its reason, and a run of dashes with no explanation reads
 * like a suite somebody quietly disabled.
 */
export async function platformUnreachable(suite: string): Promise<string | null> {
  let reason: string | null = null;
  try {
    const response = await fetch(`${API}/v1/config`);
    if (!response.ok) {
      reason =
        `The Commerce API at ${API} answered ${response.status} to GET /v1/config. ` +
        "Start it with `make demo` and re-run.";
    }
  } catch (cause) {
    reason =
      `The Commerce API at ${API} is not reachable (${cause instanceof Error ? cause.message : String(cause)}). ` +
      "These specs read live data and assert nothing without it — start the API with `make demo` and re-run.";
  }
  if (reason) console.warn(`\nSkipping ${suite}.\n${reason}\n`);
  return reason;
}
