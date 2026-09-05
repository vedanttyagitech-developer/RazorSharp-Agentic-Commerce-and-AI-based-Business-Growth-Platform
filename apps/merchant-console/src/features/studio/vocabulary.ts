/**
 * English for the strings the server sends, and nothing more than English.
 *
 * The distinction this file lives or dies on: the platform owns *which* capabilities
 * exist, and this table owns *how to say them out loud*. Every list the Agent Studio
 * renders is iterated from `GET /v1/agent/capabilities`; nothing here can add a
 * capability to a screen, remove one, or decide that one is safe. A row missing from
 * these tables is not an error and is never swallowed -- the capability renders as its
 * own string and is counted as `unclassified`, which is louder on screen than being
 * quietly listed among the reads.
 *
 * That asymmetry is deliberate. An annotation table always falls behind the registry it
 * annotates, so the failure mode has to be chosen in advance. "Unknown" is the direction
 * that fails towards a merchant asking a question; "assume it only reads" is the
 * direction that fails towards a merchant granting something nobody described.
 *
 * `kind` restates what the platform's own action table calls an action -- a read, or a
 * proposal recorded for a person to apply. There is no third value, because Registry A
 * has no verb that executes against money and this console must not invent a word that
 * implies one could exist.
 */

/** What an action does to platform state. Neither of these moves money. */
export type CapabilityKind = "read" | "propose" | "unclassified";

interface Gloss {
  /** The capability in a merchant's words, as a heading. */
  label: string;
  /**
   * The same thing as a verb phrase, for running into a sentence.
   *
   * Kept apart from `label` because the boundary screen builds a sentence by joining
   * these with commas, and a label that already contains one ("Approve a checkout, or
   * pay") turns that sentence into a list nobody can parse. A phrase with no comma in it
   * is the requirement; saying so here is cheaper than a comma-aware join.
   */
  verb: string;
  /** What it actually reads or writes, in one sentence. */
  detail: string;
  kind: CapabilityKind;
}

/**
 * The capabilities this console can speak about, keyed by the platform's own string.
 *
 * The merchant-surface rows come first because they are the ones a merchant composes
 * with. The consent rows below them are here only so that the verbs the server names in
 * `absent_by_construction` can be rendered as sentences rather than as identifiers --
 * this console never offers them, and their presence in this table grants nothing.
 */
const GLOSSARY: Readonly<Record<string, Gloss>> = {
  // --- what a merchant's agent may hold ------------------------------------
  "merchant.catalogue_health.read": {
    label: "Read catalogue health",
    verb: "read your catalogue health",
    detail:
      "Counts your products by listing state and stock: how many are listed, delisted, " +
      "out of stock and low. It reads the catalogue and changes nothing in it.",
    kind: "read",
  },
  "merchant.inventory_anomalies.read": {
    label: "Read inventory anomalies",
    verb: "read your inventory anomalies",
    detail:
      "Finds products whose listing and stock disagree — listed but out of stock, " +
      "delisted but still stocked. It names the SKU; it cannot restock or relist it.",
    kind: "read",
  },
  "merchant.checkout_metrics.read": {
    label: "Read checkout metrics",
    verb: "read your checkout metrics",
    detail:
      "Counts checkouts by state and orders over a window, from committed rows. It is " +
      "the only way this agent can speak about conversion, and it reads counts, not money.",
    kind: "read",
  },
  "merchant.growth_proposal.create": {
    label: "Draft a growth proposal",
    verb: "draft a growth proposal",
    detail:
      "Records a recommendation — a restock, a threshold, a listing change — as a " +
      "proposal citing the tool results it rests on. A proposal changes nothing. A person " +
      "applies it from the console, through the audited endpoint, or it stays a draft.",
    kind: "propose",
  },
  "support.case.read": {
    label: "Read a support case",
    verb: "read a support case",
    detail:
      "Opens a case from the human-review queue and reads its evidence. It cannot assign " +
      "a case, decide one, or close one.",
    kind: "read",
  },
  "policy.search": {
    label: "Search your policies",
    verb: "search your policies",
    detail:
      "Looks up the cancellation, refund and substitution policies you published, so an " +
      "answer quotes your rules rather than inventing them.",
    kind: "read",
  },
  "resolution.evaluate": {
    label: "Evaluate a resolution",
    verb: "evaluate a resolution",
    detail:
      "Produces a plan for what a buyer is owed under your policy. A plan is not a " +
      "refund: the money moves only when a person consents and the kernel admits it.",
    kind: "propose",
  },
  "support.escalate": {
    label: "Raise a support case",
    verb: "raise a support case",
    detail:
      "Opens a case for a human to look at. Raising a case is how the agent stops, not " +
      "how it acts.",
    kind: "propose",
  },
  "catalogue.read": {
    label: "Read your catalogue",
    verb: "read your catalogue",
    detail: "Searches and reads product rows, including price and stock as the platform holds them.",
    kind: "read",
  },
  "order.read": {
    label: "Read orders",
    verb: "read your orders",
    detail: "Reads the state of an order and its checkout. It cannot cancel one or refund one.",
    kind: "read",
  },
  "basket.write": {
    label: "Build a basket",
    verb: "build a basket",
    detail: "Adds and removes lines on a buyer's basket. A basket is not a purchase.",
    kind: "propose",
  },
  "checkout.create": {
    label: "Put a checkout up for approval",
    verb: "put a checkout up for approval",
    detail:
      "Builds the priced, versioned checkout a buyer is shown. It creates the thing that " +
      "asks for consent; it cannot give the consent.",
    kind: "propose",
  },
  "checkout.submit_approved": {
    label: "Submit an already-approved checkout",
    verb: "submit an already-approved checkout",
    detail:
      "Hands the kernel a checkout a human has already approved. The kernel re-checks the " +
      "approval against locked rows before anything is admitted.",
    kind: "propose",
  },

  // --- consent, which is nobody's to delegate -------------------------------
  "checkout.approve": {
    label: "Approve a checkout, or pay",
    verb: "approve a checkout or take a payment",
    detail:
      "The buyer's consent to spend, given on the trusted surface. It is the same " +
      "capability the word “pay” asks for.",
    kind: "unclassified",
  },
  "checkout.reject": {
    label: "Reject a checkout",
    verb: "reject a checkout",
    detail: "Refusing consent is consent's other half, and is given by the same person.",
    kind: "unclassified",
  },
  "checkout.cancel": {
    label: "Cancel a checkout",
    verb: "cancel a checkout",
    detail: "Withdrawing a purchase in progress is the buyer's decision.",
    kind: "unclassified",
  },
  "payment.verify": {
    label: "Verify a payment result",
    verb: "verify a payment result",
    detail: "Accepting a provider's answer about money is the kernel's, on the trusted surface.",
    kind: "unclassified",
  },
  "refund.request": {
    label: "Request a refund",
    verb: "request a refund",
    detail: "Asking for money back is the buyer's act, and executing it is the kernel's.",
    kind: "unclassified",
  },
  "authority.revoke": {
    label: "Revoke a delegated authority",
    verb: "revoke a delegated authority",
    detail:
      "Tearing up a mandate the buyer granted. An agent that could revoke could also " +
      "revoke the limits placed on itself.",
    kind: "unclassified",
  },
};

/** The gloss for a capability, or `null` when this console has never heard of it. */
export function glossOf(capability: string): Gloss | null {
  return GLOSSARY[capability] ?? null;
}

/**
 * What to call a capability on screen.
 *
 * An unknown capability is printed as the platform spelled it. That is deliberately
 * unpolished: a raw identifier on a merchant-facing screen is a visible sign that this
 * console is behind the platform, and the alternative — a prettified
 * `merchant growth proposal create` — would hide exactly that.
 */
export function capabilityLabel(capability: string): string {
  return GLOSSARY[capability]?.label ?? capability;
}

export function capabilityKind(capability: string): CapabilityKind {
  return GLOSSARY[capability]?.kind ?? "unclassified";
}

/**
 * The capability as a verb phrase, for a sentence.
 *
 * An unknown capability falls back to its own string, which reads badly in prose and is
 * meant to: a sentence about your assistant that contains a bare identifier is a sentence
 * telling you this console does not know what that identifier does.
 */
export function capabilityVerb(capability: string): string {
  return GLOSSARY[capability]?.verb ?? capability;
}

/**
 * The five specialists, in the words the roster uses.
 *
 * The merchant harness reaches only two of them, and which two is the server's answer,
 * not this table's: the studio offers whatever `specialists[]` contained. These names
 * exist so the two it does offer are not called `growth` and `case` in a heading.
 */
const ROLE_NAMES: Readonly<Record<string, { title: string; summary: string }>> = {
  growth: {
    title: "Growth",
    summary:
      "Reads catalogue health, stock anomalies and the checkout funnel, and writes " +
      "proposals a person applies. It is the assistant most shops want first.",
  },
  case: {
    title: "Case",
    summary:
      "Reads one support case and its evidence, for a shop that wants an assistant " +
      "beside the review queue rather than beside the catalogue.",
  },
  shopping: { title: "Shopping", summary: "Discovery and basket building, on the buyer's surface." },
  checkout: { title: "Checkout", summary: "Priced, versioned checkouts, on the buyer's surface." },
  support: { title: "Support", summary: "Policy lookup and resolution plans, on the buyer's surface." },
};

export function roleTitle(role: string): string {
  return ROLE_NAMES[role]?.title ?? role;
}

/**
 * A question each capability can actually answer, for the sandbox to offer.
 *
 * Keyed on capability rather than kept as a flat list, so that switching a capability off
 * removes its question. A suggestion that produced "I cannot do that" would teach a
 * merchant that the box is decorative, and a suggestion still on offer after its
 * capability was switched off would teach them the switch is.
 *
 * A capability with no question here contributes none. That is the right failure: an
 * invented question about a capability nobody described would be this console guessing
 * what the platform can answer.
 */
const QUESTIONS: Readonly<Record<string, string>> = {
  "merchant.catalogue_health.read": "How is my catalogue health?",
  "merchant.inventory_anomalies.read": "Which products are out of stock?",
  "merchant.checkout_metrics.read": "How are my checkouts and orders doing?",
  "merchant.growth_proposal.create": "Propose what to do about the stock anomalies",
  "support.case.read": "Show me the support case I am looking at",
};

export function questionsFor(capabilities: readonly string[]): string[] {
  const asked = new Set<string>();
  const out: string[] = [];
  for (const capability of capabilities) {
    const question = QUESTIONS[capability];
    if (question === undefined || asked.has(question)) continue;
    asked.add(question);
    out.push(question);
  }
  return out;
}

export function roleSummary(role: string): string | null {
  return ROLE_NAMES[role]?.summary ?? null;
}
