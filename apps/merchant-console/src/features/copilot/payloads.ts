/**
 * What the Merchant Copilot puts in `structured`, parsed rather than cast.
 *
 * The API declares `structured` as `unknown` because five specialists write into it and
 * the envelope is theirs, not this console's. Parsing it here with the same discipline the
 * REST client uses gives this surface the one property it cannot do without: a payload
 * whose shape does not fit is *named as unreadable* rather than half-drawn. A card that
 * rendered `undefined` as a blank cell would put a hole in a figure column and let an
 * operator read the hole as a zero.
 *
 * Every count is nullable on purpose. Specification 6.6 requires a recommendation to cite
 * its window, its sample size and whether the data is synthetic, and the merchant tools
 * distinguish "counted zero" from "could not derive" all the way to the wire. That
 * distinction only survives if the schema keeps `null` reachable and the components draw
 * it as *not measured*; collapsing it to `0` with a default would erase the difference at
 * the last possible moment, in the one place nobody would look for it.
 *
 * The schemas are loose. A server that starts sending more about a card must not break the
 * parse at the boundary -- what this console draws is only what it can read, and an extra
 * field it does not know about is not an error.
 */
import { z } from "zod";

import { INJECTION_KINDS } from "@/lib/api/types";

/** A count the platform either derived or could not. `null` is an answer, never a zero. */
const Count = z.number().int().nullish();

/**
 * An amount, as integer minor units and the server's own decimal string.
 *
 * Nothing in this feature adds, subtracts or divides two of these. Specification 6.6
 * requires a discount recommendation to carry gross revenue, discount cost and net
 * captured and retained revenue as four separate server-supplied figures, and the reason
 * it names four rather than three is precisely that a console must never be the thing
 * that computes the fourth.
 */
export const AmountSchema = z
  .object({
    minor: z.number().int(),
    currency: z.string(),
    display: z.string().nullish(),
  })
  .loose();

export const CatalogueHealthSchema = z
  .object({
    kind: z.literal("catalogue_health"),
    synthetic: z.boolean(),
    source: z.string(),
    catalogue_revision: Count,
    sample_size: Count,
    products: Count,
    listed: Count,
    delisted: Count,
    out_of_stock: Count,
    low_stock: Count,
  })
  .loose();

/**
 * One product the platform flagged, in either producer's spelling.
 *
 * Two halves of this platform describe the same flagged product differently, and this
 * console draws whichever one answered. The deterministic runner sends `anomaly`,
 * `stock_units`, `is_listed` and a plain `name`; the agent runtime sends `kind`, a nested
 * `detail.stock_units`, a `safe_label` and the name fenced into `merchant_text` with a
 * `quarantined` flag. Requiring only the intersection and reading the rest through
 * `anomalyKind` and `anomalyUnits` is what lets one card render both, instead of one of
 * them arriving and being named unreadable.
 *
 * `merchant_text` is deliberately *not* what the card prints. It carries the runtime's
 * `<merchant_data>` fence, which is an instruction to a model about what it is reading and
 * is noise to the person reading a card; the display name is `name`, or the platform's own
 * `safe_label` when there is no unfenced name to show.
 */
export const AnomalySchema = z
  .object({
    sku: z.string(),
    name: z.string().nullish(),
    merchant_text: z.string().nullish(),
    safe_label: z.string().nullish(),
    quarantined: z.boolean().nullish(),
    stock_units: Count,
    is_listed: z.boolean().nullish(),
    anomaly: z.string().nullish(),
    kind: z.string().nullish(),
    detail: z.object({ stock_units: Count }).loose().nullish(),
  })
  .loose();

/** What the platform called this anomaly, under whichever key the producer used. */
export function anomalyKind(anomaly: Anomaly): string | null {
  return anomaly.anomaly ?? anomaly.kind ?? null;
}

/**
 * The units behind an anomaly, or `null` when neither producer counted them.
 *
 * `null` is an answer here and is drawn as "not measured". Falling back to `0` would turn
 * a figure nobody counted into an empty shelf, which is the one reading a merchant would
 * act on.
 */
export function anomalyUnits(anomaly: Anomaly): number | null {
  return anomaly.stock_units ?? anomaly.detail?.stock_units ?? null;
}

/**
 * What the card calls this product to the person reading it.
 *
 * Never `merchant_text`: that string is fenced for a model. A quarantined name is not
 * drawn at all -- the caller shows the safe label and says it was quarantined -- so an
 * attempted instruction reaches neither the model as text nor the merchant as a name.
 */
export function anomalyLabel(anomaly: Anomaly): string | null {
  if (anomaly.quarantined === true) return anomaly.safe_label ?? `catalogue item ${anomaly.sku}`;
  return anomaly.name ?? anomaly.safe_label ?? null;
}

export const InventoryAnomaliesSchema = z
  .object({
    kind: z.literal("inventory_anomalies"),
    synthetic: z.boolean(),
    source: z.string(),
    catalogue_revision: Count,
    sample_size: Count,
    anomalies: z.array(AnomalySchema),
  })
  .loose();

export const CheckoutMetricsSchema = z
  .object({
    kind: z.literal("checkout_metrics"),
    synthetic: z.boolean(),
    source: z.string(),
    window: z.string().nullish(),
    sample_size: Count,
    checkouts_by_state: z.record(z.string(), z.number().int()),
    orders: Count,
  })
  .loose();

/**
 * The body a human would send, carried as data.
 *
 * `kind` is typed as a string rather than as the injection enum so that a proposal naming
 * a kind this console cannot apply still *renders*, with the reason it cannot be applied
 * stated on the card. Failing the parse instead would make an unapplicable proposal
 * vanish, and a proposal that disappears is indistinguishable from one that was never
 * made.
 */
export const ChangeBodySchema = z.object({ kind: z.string() }).loose();

export const ProposalChangeSchema = z
  .object({
    endpoint: z.string(),
    body: ChangeBodySchema,
    reversible: z.boolean().nullish(),
    reverses_to: ChangeBodySchema.nullish(),
  })
  .loose();

/**
 * Where the figures behind a proposal came from.
 *
 * `source` and `synthetic` are required and the rest is not, because those two are the
 * ones 6.6 makes non-negotiable: a recommendation drawn from a simulated catalogue that
 * does not say so is the exact defect this console was rebuilt to remove. A window or a
 * sample size the server did not send is drawn as not stated, which is a smaller failure
 * and an honest one.
 *
 * `read_by` names the tools the proposal rests on. An empty list is not a formatting
 * detail: a proposal that cannot name a tool has no evidence, and this console will not
 * offer to apply it.
 */
export const EvidenceSchema = z
  .object({
    source: z.string(),
    window: z.string().nullish(),
    sample_size: z.number().int().nullish(),
    synthetic: z.boolean(),
    catalogue_revision: z.number().int().nullish(),
    read_by: z.array(z.string()),
  })
  .loose();

export const ProposalSchema = z
  .object({
    kind: z.literal("proposal"),
    proposal_id: z.string(),
    lever: z.string(),
    title: z.string(),
    rationale: z.string(),
    metric: z.string(),
    gate: z.string(),
    evidence: EvidenceSchema,
    change: ProposalChangeSchema,
    applied: z.boolean(),
    where: z.string().nullish(),
    /**
     * Named amounts the server derived, for the levers that move money.
     *
     * Absent for an operational proposal such as a restock, which is why it is optional
     * rather than required. When a discount lever is wired it must arrive with the four
     * figures 6.6 names, each an integer the server supplied; this console renders the
     * ones it is given and computes none of them.
     */
    money: z.record(z.string(), AmountSchema).nullish(),
  })
  .loose();

export type CatalogueHealth = z.infer<typeof CatalogueHealthSchema>;
export type InventoryAnomalies = z.infer<typeof InventoryAnomaliesSchema>;
export type CheckoutMetrics = z.infer<typeof CheckoutMetricsSchema>;
export type Anomaly = z.infer<typeof AnomalySchema>;
export type Proposal = z.infer<typeof ProposalSchema>;
export type ChangeBody = z.infer<typeof ChangeBodySchema>;

/** The card a turn earned, if this console draws that kind. */
export type Card =
  | { kind: "catalogue_health"; value: CatalogueHealth }
  | { kind: "inventory_anomalies"; value: InventoryAnomalies }
  | { kind: "checkout_metrics"; value: CheckoutMetrics };

/**
 * Everything a turn's payload yielded, including what it failed to yield.
 *
 * `undrawn` and `malformed` exist so that a payload is never silently dropped. The first
 * is a kind this console has no component for -- a new card on the server, which someone
 * needs to notice -- and the second is a kind it does draw arriving in a shape that did
 * not fit, which is a disagreement between the two halves and the sort of thing that hides
 * for a week behind an empty panel.
 */
export interface Reading {
  card: Card | null;
  proposal: Proposal | null;
  undrawn: string | null;
  malformed: { kind: string; detail: string } | null;
}

const EMPTY: Reading = { card: null, proposal: null, undrawn: null, malformed: null };

function detailOf(error: z.ZodError): string {
  return error.issues
    .slice(0, 4)
    .map((issue) => `${issue.path.join(".") || "(root)"}: ${issue.message}`)
    .join("; ");
}

function declaredKind(structured: unknown): string | null {
  if (typeof structured !== "object" || structured === null) return null;
  const kind = (structured as Record<string, unknown>).kind;
  return typeof kind === "string" ? kind : null;
}

/**
 * Read a proposal, whether it arrived as the payload or beside one.
 *
 * Both placements are real. A turn whose whole answer is a proposal sends it at the top
 * level with `kind: "proposal"`; a turn that answered with figures and then proposed
 * something about them carries it under `proposal` next to the card. Looking in one place
 * only would drop half of them.
 */
function readProposal(structured: unknown): Proposal | null {
  const direct = ProposalSchema.safeParse(structured);
  if (direct.success) return direct.data;
  if (typeof structured !== "object" || structured === null) return null;
  const nested = ProposalSchema.safeParse((structured as Record<string, unknown>).proposal);
  return nested.success ? nested.data : null;
}

export function readPayload(structured: unknown): Reading {
  if (structured === null || structured === undefined) return EMPTY;
  const proposal = readProposal(structured);
  const kind = declaredKind(structured);
  if (kind === null) return { ...EMPTY, proposal };
  if (kind === "proposal") {
    if (proposal !== null) return { ...EMPTY, proposal };
    const failed = ProposalSchema.safeParse(structured);
    const detail = failed.success ? "" : detailOf(failed.error);
    return { ...EMPTY, malformed: { kind, detail } };
  }

  const schemas = {
    catalogue_health: CatalogueHealthSchema,
    inventory_anomalies: InventoryAnomaliesSchema,
    checkout_metrics: CheckoutMetricsSchema,
  } as const;
  if (!(kind in schemas)) return { ...EMPTY, proposal, undrawn: kind };

  const schema = schemas[kind as keyof typeof schemas];
  const parsed = schema.safeParse(structured);
  if (!parsed.success) {
    return { ...EMPTY, proposal, malformed: { kind, detail: detailOf(parsed.error) } };
  }
  return { ...EMPTY, proposal, card: { kind, value: parsed.data } as Card };
}

// ------------------------------------------------------------------------- vocabularies

/**
 * The growth levers of specification 9.1, in the words that table uses.
 *
 * Written out rather than de-underscored mechanically, because "top_seller_out_of_stock"
 * is a row of that table and a reviewer checking this console against the specification
 * should see the row's own name. A lever absent here still renders as itself: the server
 * owns this vocabulary, and a lever this table has fallen behind on must appear rather
 * than be swallowed.
 */
const LEVER_NAMES: Readonly<Record<string, string>> = {
  reapproval_with_substitution: "Reapproval with substitution",
  threshold_nudge: "Threshold nudge",
  failed_payment_retry: "Failed-payment retry inside reservation",
  abandoned_checkout_payment_link: "Abandoned checkout to Payment Link",
  refund_or_store_credit: "Cash refund or store-credit choice",
  top_seller_out_of_stock: "Top-seller out-of-stock alert",
  reservation_countdown: "Reservation countdown",
  cheaper_slot_or_fee: "Cheaper slot or fee alternative",
  policy_bounded_cross_sell: "Policy-bounded cross-sell",
  goal_basket: "Goal basket",
  one_tap_reorder: "One-tap reorder",
  preferences_with_ttl: "Explicit preferences with TTL",
  merchant_offers_as_policy: "Merchant offers as policy objects",
  hindi_hinglish_journey: "Hindi/Hinglish journey",
  external_ai_buyer_channel: "External AI-buyer channel",
  catalogue_discoverability_health: "Catalogue discoverability health",
  checkout_configuration_analysis: "Checkout-configuration analysis",
};

export function leverName(lever: string): string {
  return LEVER_NAMES[lever] ?? lever.replace(/_/g, " ");
}

/** The four figures 6.6 requires of a discount recommendation, and how to head them. */
const AMOUNT_LABELS: Readonly<Record<string, string>> = {
  gross_revenue: "Gross revenue",
  discount_cost: "Discount cost",
  net_captured_revenue: "Net captured revenue",
  retained_revenue: "Retained revenue",
};

export function amountLabel(key: string): string {
  return AMOUNT_LABELS[key] ?? key.replace(/_/g, " ");
}

/**
 * The anomaly kinds the merchant simulator reports.
 *
 * Delisted and out of stock stay apart here for the same reason they stay apart in the
 * tool: one is a product the merchant took off sale and the other is a product they can
 * restock this afternoon, and a console that merged them would propose the wrong remedy.
 */
const ANOMALY_NAMES: Readonly<Record<string, string>> = {
  out_of_stock: "out of stock",
  listed_out_of_stock: "listed, out of stock",
  delisted: "delisted",
  delisted_with_stock: "delisted, still stocked",
  low_stock: "low stock",
};

export function anomalyName(anomaly: string): string {
  return ANOMALY_NAMES[anomaly] ?? anomaly.replace(/_/g, " ");
}

/**
 * The registered tools, in English.
 *
 * Two runners answer this endpoint and they spell the same tool differently -- the
 * deterministic runner records the capability name, the agent runtime records the bound
 * function name -- so both spellings are listed rather than one being normalised into the
 * other. Normalising would mean this table quietly deciding which runner's ledger is the
 * real one.
 */
const TOOL_NAMES: Readonly<Record<string, string>> = {
  "merchant.catalogue_health.read": "read catalogue health",
  "merchant.inventory_anomalies.read": "read inventory anomalies",
  "merchant.checkout_metrics.read": "read checkout metrics",
  "merchant.growth_proposal.create": "drafted a proposal",
  "support.case.read": "read the support case",
  catalogue_health_read: "read catalogue health",
  inventory_anomalies_read: "read inventory anomalies",
  checkout_metrics_read: "read checkout metrics",
  growth_proposal_create: "drafted a proposal",
  present_metrics: "drew the figures card",
};

export function toolName(name: string): string {
  return TOOL_NAMES[name] ?? name.replace(/[._]/g, " ");
}

/**
 * The reason keys the harness records when it refuses, as sentences.
 *
 * `capability_missing` and `not_on_agent_surface` are different facts and are worth the
 * two entries: the first says this session never held the capability, the second says it
 * does and the capability still does not travel to an agent. Only the second is the
 * property this project exists to demonstrate, and a console that read them as one thing
 * would lose it.
 */
const REASON_SENTENCES: Readonly<Record<string, string>> = {
  capability_missing: "This session does not carry that capability, so no tool for it exists.",
  not_on_agent_surface:
    "The session holds this one, and it does not travel. It is exercised by a person on a " +
    "trusted surface, so no tool for it is ever bound to an agent.",
  tool_not_registered: "There is no tool registered for that action at all.",
  tool_budget_exhausted: "The turn's fixed tool budget was already spent. Ask again and it starts fresh.",
  tool_unavailable: "That part of the platform could not be reached, so nothing was guessed.",
  tool_failed: "That read did not complete, and an unchecked figure is worse than none.",
  not_found: "The identifier named nothing this session may read.",
  not_permitted: "The session was refused by the API for that read.",
  invalid_argument: "The call was rejected before it ran because its arguments were not valid.",
  injected_instruction:
    "Merchant-authored text tried to issue an instruction. Catalogue text is read as " +
    "description, never as a command.",
  specialist_not_allowed: "That tool belongs to a different specialist than the one that answered.",
  metric_not_read: "The figures were not read this turn, so there was nothing honest to draw.",
  unknown_metric: "No such metric exists on the merchant surface.",
};

export function reasonSentence(reasonKey: string): string {
  return REASON_SENTENCES[reasonKey] ?? "The capability gate refused the call before anything ran.";
}

// ------------------------------------------------------------------------- apply guards

/**
 * The one endpoint this console will send a proposal's change to.
 *
 * A proposal is a record for a human to act on and the console's half of that bargain is
 * narrow on purpose: it posts the proposal's own body to the scenario controller, which
 * applies it under the merchant simulator's lock and audits it as `SCENARIO_INJECTION` in
 * the same transaction. It writes no merchant table itself and it calls no other endpoint
 * on a proposal's say-so, so a proposal naming a different route is refused here rather
 * than followed.
 */
export const APPLY_ENDPOINT = "POST /v1/scenario/injections";

const APPLICABLE_KINDS: ReadonlySet<string> = new Set<string>(INJECTION_KINDS);

/**
 * Why this proposal cannot be applied from this console, or `null` when it can.
 *
 * Each of the three refusals is a real failure mode rather than defensive noise. An
 * endpoint this console does not implement would make the apply button a lie; an
 * injection kind the scenario controller does not accept would turn one press into a 422
 * the merchant has to interpret; and a proposal naming no tool has no evidence behind it,
 * which 6.6 forbids and which is the only one of the three a reader would not otherwise
 * be able to see.
 */
export function applyBlockedReason(proposal: Proposal): string | null {
  const endpoint = proposal.change.endpoint.trim();
  if (endpoint !== APPLY_ENDPOINT) {
    return (
      `This proposal names ${endpoint || "no endpoint"}. This console applies a proposal only ` +
      `through ${APPLY_ENDPOINT}, so there is no button for this one.`
    );
  }
  if (!APPLICABLE_KINDS.has(proposal.change.body.kind)) {
    return (
      `The scenario controller does not accept a change of kind ${proposal.change.body.kind}, ` +
      "so pressing apply would only produce a rejection."
    );
  }
  if (proposal.evidence.read_by.length === 0) {
    return (
      "This proposal names no tool it rests on. Specification 6.6 requires a recommendation " +
      "to cite what it was derived from, and a figure with no read behind it is not evidence."
    );
  }
  return null;
}
