export type ToolStatus = "running" | "completed" | "failed";

export interface ToolActivity {
  id: string;
  name: "search_catalogue" | "modify_basket" | "propose_checkout" | "verify_policy";
  label: string;
  status: ToolStatus;
  detail?: string;
}

export interface ProposalItem {
  sku: string;
  name: string;
  quantity: number;
  unitPriceMinor: number;
  subtotalMinor: number;
}

export interface CheckoutProposal {
  version: number;
  basketId: string;
  items: ProposalItem[];
  itemsSubtotalMinor: number;
  deliveryFeeMinor: number;
  deliveryTaxMinor: number;
  totalMinor: number;
  currency: string;
  contentHash: string;
  status: "proposed" | "authorizing" | "authorized" | "rejected";
}

export interface ReapprovalDelta {
  fieldPath: string;
  label: string;
  before: string;
  after: string;
  reason: "PRICE_CHANGED" | "TOTAL_CHANGED" | "DELIVERY_CHANGED" | "INVENTORY_SHORTAGE";
}

export interface ReapprovalDecision {
  invalidatedVersion: number;
  nextVersion: number;
  reason: string;
  deltas: ReapprovalDelta[];
  oldTotalMinor: number;
  newTotalMinor: number;
  currency: string;
  newContentHash: string;
}

export interface AgentMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  timestamp: string;
  tools?: ToolActivity[];
  proposal?: CheckoutProposal;
  reapproval?: ReapprovalDecision;
  isStreaming?: boolean;
}
