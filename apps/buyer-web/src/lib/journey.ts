/**
 * The sixteen buyer-visible journey states of spec 8.2, and how they derive from the
 * kernel's checkout and payment states (spec 10.4, 10.5).
 *
 * Some states are transient and exist only in the browser (Searching, Revalidating while
 * a submit is in flight, Payment opening while the Razorpay surface loads). The rest are
 * projections of server state and are never advanced from a browser callback alone.
 */
import type { Checkout, CheckoutState, PaymentState } from "./api/types";

export const JOURNEY_STATES = [
  "SEARCHING",
  "AVAILABILITY_CHECKED",
  "QUOTE_CALCULATED",
  "INVENTORY_RESERVED",
  "APPROVAL_REQUIRED",
  "APPROVED",
  "REVALIDATING",
  "DELTA_REAPPROVAL_REQUIRED",
  "PAYMENT_OPENING",
  "AUTHORIZED",
  "CAPTURED",
  "PAYMENT_PENDING_UNKNOWN",
  "RECONCILIATION",
  "ORDER_CONFIRMED",
  "CANCELLATION_REFUND",
  "STALE_CAPTURE_AUTO_REFUND",
] as const;

export type JourneyState = (typeof JOURNEY_STATES)[number];

/** Text label + glyph + tone. The glyph and label carry the meaning; colour is decoration. */
export type Tone = "neutral" | "info" | "pending" | "success" | "warning" | "danger";

export interface JourneyMeta {
  label: string;
  glyph: string;
  tone: Tone;
  description: string;
}

export const JOURNEY_META: Record<JourneyState, JourneyMeta> = {
  SEARCHING: { label: "Searching", glyph: "…", tone: "pending", description: "Grounded catalogue search is running against live merchant state." },
  AVAILABILITY_CHECKED: { label: "Availability checked", glyph: "✓", tone: "info", description: "Results carry source, catalogue revision and a separate listing/stock verdict." },
  QUOTE_CALCULATED: { label: "Quote calculated", glyph: "Σ", tone: "info", description: "The fee engine priced the basket to the paisa; the UI only formats it." },
  INVENTORY_RESERVED: { label: "Inventory temporarily reserved", glyph: "⧗", tone: "pending", description: "A time-limited reservation holds the units behind this version." },
  APPROVAL_REQUIRED: { label: "Approval required", glyph: "?", tone: "warning", description: "The trusted card shows exactly what you would approve, with its content hash." },
  APPROVED: { label: "Approved", glyph: "✓", tone: "success", description: "Your approval is bound to this version and hash. It cannot be reused for a different total." },
  REVALIDATING: { label: "Revalidating", glyph: "↻", tone: "pending", description: "The kernel re-checks merchant state under lock before any money moves." },
  DELTA_REAPPROVAL_REQUIRED: { label: "Material delta, reapproval required", glyph: "Δ", tone: "danger", description: "Merchant state changed underneath your approval. The old version is invalidated." },
  PAYMENT_OPENING: { label: "Payment opening", glyph: "→", tone: "pending", description: "One Execution Grant, one Razorpay order. Standard Checkout is opening." },
  AUTHORIZED: { label: "Authorized", glyph: "◐", tone: "info", description: "Razorpay reports an authorization. Capture is still pending." },
  CAPTURED: { label: "Captured", glyph: "●", tone: "success", description: "Capture verified from provider evidence, never from the browser callback." },
  PAYMENT_PENDING_UNKNOWN: { label: "Payment pending / unknown", glyph: "?", tone: "warning", description: "The outcome is not yet verified. Unknown is never turned into failed by a UI timer." },
  RECONCILIATION: { label: "Reconciliation", glyph: "⇄", tone: "pending", description: "The worker fetches Razorpay by authoritative identifiers and applies verified evidence." },
  ORDER_CONFIRMED: { label: "Order confirmed", glyph: "✓", tone: "success", description: "Paid from verified capture. Evidence and the proof chain are attached." },
  CANCELLATION_REFUND: { label: "Cancellation / refund", glyph: "↩", tone: "neutral", description: "Cancelled within policy, or a buyer-confirmed refund is in progress." },
  STALE_CAPTURE_AUTO_REFUND: { label: "Stale capture, automatic refund", glyph: "⚠", tone: "danger", description: "A capture arrived for an invalidated version. Exactly one full refund is created; nothing is fulfilled." },
};

/** Browser-only phases layered over server state. */
export type TransientPhase =
  | "idle"
  | "searching"
  | "availability_checked"
  | "revalidating"
  | "payment_opening"
  | "verification_pending";

const REFUND_STATES: ReadonlySet<PaymentState> = new Set([
  "REFUND_PENDING",
  "PARTIALLY_REFUNDED",
  "REFUNDED",
  "REFUND_UNKNOWN",
  "REFUND_FAILED",
]);

const STALE_STATES: ReadonlySet<PaymentState> = new Set(["STALE_CAPTURE", "AUTO_REFUND_PENDING"]);

export function deriveJourneyState(
  checkout: Pick<Checkout, "state" | "attempt" | "order_id" | "deltas" | "current_version" | "versions"> | null,
  phase: TransientPhase = "idle",
): JourneyState {
  if (phase === "searching") return "SEARCHING";
  if (phase === "availability_checked") return "AVAILABILITY_CHECKED";
  if (!checkout) return "QUOTE_CALCULATED";

  const payment = checkout.attempt?.state ?? null;
  const state: CheckoutState = checkout.state;

  // Stale capture wins over everything: money is being returned, nothing is fulfilled.
  if (payment && STALE_STATES.has(payment)) return "STALE_CAPTURE_AUTO_REFUND";
  if (state === "INVALIDATED_AWAITING_PAYMENT_RESULT") return "STALE_CAPTURE_AUTO_REFUND";
  if (payment && REFUND_STATES.has(payment)) {
    return refundAfterInvalidation(checkout.versions, checkout.current_version)
      ? "STALE_CAPTURE_AUTO_REFUND"
      : "CANCELLATION_REFUND";
  }
  if (state === "CANCELLED" || state === "EXPIRED") return "CANCELLATION_REFUND";

  // Browser-only phases are honoured only while the server state still matches them, so
  // a state that moved on (an SSE event, a reload) is never masked by a stale phase.
  if (phase === "revalidating" && state === "APPROVED") return "REVALIDATING";
  if (
    phase === "payment_opening" &&
    (state === "EXECUTION_PENDING" || (state === "AWAITING_PAYMENT" && (payment === "SUBMITTED" || payment === "CREATED")))
  ) {
    return "PAYMENT_OPENING";
  }

  switch (state) {
    case "DRAFT":
    case "QUOTED":
      return "QUOTE_CALCULATED";
    case "RESERVED":
      return "INVENTORY_RESERVED";
    case "APPROVAL_REQUIRED":
      return checkout.deltas.length > 0 || checkout.current_version > 1
        ? "DELTA_REAPPROVAL_REQUIRED"
        : "APPROVAL_REQUIRED";
    case "APPROVED":
      return "APPROVED";
    case "EXECUTION_PENDING":
      return "PAYMENT_OPENING";
    case "AWAITING_PAYMENT":
      if (payment === "AUTHORIZED") return "AUTHORIZED";
      if (payment === "CAPTURED") return "CAPTURED";
      if (payment === "RECONCILING" || payment === "ESCALATED") return "RECONCILIATION";
      if (payment === "UNKNOWN" || phase === "verification_pending") return "PAYMENT_PENDING_UNKNOWN";
      return "PAYMENT_OPENING";
    case "PAYMENT_UNKNOWN":
      return payment === "RECONCILING" ? "RECONCILIATION" : "PAYMENT_PENDING_UNKNOWN";
    case "PAYMENT_FAILED":
      return "CANCELLATION_REFUND";
    case "PAID":
      return checkout.order_id ? "ORDER_CONFIRMED" : "CAPTURED";
    case "INVALIDATED":
      return checkout.deltas.length > 0 ? "DELTA_REAPPROVAL_REQUIRED" : "CANCELLATION_REFUND";
    default:
      return "QUOTE_CALCULATED";
  }
}

function refundAfterInvalidation(versions: Checkout["versions"], current: number): boolean {
  const version = versions.find((v) => v.version === current);
  return version?.state === "INVALIDATED";
}

export function journeyIndex(state: JourneyState): number {
  return JOURNEY_STATES.indexOf(state);
}
