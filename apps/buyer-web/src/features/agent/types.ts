export type ToolStatus = "running" | "completed" | "failed";

export interface ToolActivity {
  id: string;
  name: string;
  label: string;
  status: ToolStatus;
  detail?: string;
}

export interface DenialNotice {
  capability: string;
  reason_key: string;
  explanation?: string;
}

export interface ProposalItem {
  sku: string;
  name: string;
  quantity: number;
  unitPriceMinor: number;
  subtotalMinor: number;
}

export interface BasketProposal {
  action: "basket.update";
  sku: string;
  quantity: number;
  basket_id?: string | null;
  executes_on?: string;
  display?: {
    quantity?: number;
    name?: string;
  };
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
  specialist?: string;
  specialistLabel?: string;
  routingReason?: string;
  language?: string;
  tools?: ToolActivity[];
  denials?: DenialNotice[];
  proposal?: CheckoutProposal;
  basketProposal?: BasketProposal;
  reapproval?: ReapprovalDecision;
  isStreaming?: boolean;
}
