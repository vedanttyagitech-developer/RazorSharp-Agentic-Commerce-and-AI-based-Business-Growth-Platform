/**
 * The boundary, derived. Nothing on the boundary screen is written by hand.
 *
 * A merchant is being asked to believe a claim about what software may do with their
 * shop. The only version of that claim worth making is one computed from the same
 * document the gate is built from, so every list below is a set operation over
 * `GET /v1/agent/capabilities` and a composition that can only narrow it. There is no
 * marketing string in this module and no capability literal: change what the server
 * declares and every list here changes with it, including the ones that make the platform
 * look good.
 *
 * Four rings, and the difference between them is the argument:
 *
 *  1. `granted` — what this composition holds. The merchant chose these, from a list they
 *     could not add to.
 *  2. `disabled` — declared for this role and switched off. The merchant can switch them
 *     back on, and nothing else on this screen moves when they do.
 *  3. `elsewhereOnTheSurface` — held by the merchant harness but not by this role. Not a
 *     merchant decision at all: the roster says which specialist carries what.
 *  4. `neverOnAnyAgent` — the server's own `absent_by_construction`, plus everything this
 *     operator session holds that does not survive the intersection with the agent
 *     surface. Neither is negotiable from this screen or any other.
 *
 * The fourth ring is the one this module is most careful about, because it is the one a
 * reader is most likely to over-read. `absent_by_construction` is the API's list of
 * consent verbs, not an exhaustive catalogue of everything an agent cannot do — the
 * platform has many operations no agent may perform that this endpoint never names, and
 * `exhaustive` is false so the screen can say that out loud rather than implying the list
 * is the whole of it.
 */
import type { AgentCapabilities, SpecialistCapabilities } from "@/lib/api/types";

import { capabilityLabel, glossOf, type CapabilityKind } from "./vocabulary";
import { roleIn, type Composition } from "./composition";

/**
 * One capability, as the screen needs it.
 *
 * `known` is carried separately from `detail` so the screen can distinguish "the platform
 * declared this and we have a sentence for it" from "the platform declared this and this
 * console has never heard of it". The second is not a rendering failure to paper over: it
 * is a capability nobody described to the merchant, and it is drawn as one.
 */
export interface CapabilityRow {
  capability: string;
  label: string;
  detail: string | null;
  kind: CapabilityKind;
  known: boolean;
  /**
   * Whether the server listed a tool under this exact name for this role.
   *
   * The API answers `tools` from its own tool table, whose keys are capability strings on
   * the merchant surface — so for the roles this studio offers, a capability with no
   * matching tool name is a capability the harness carries and never calls. That is a
   * real distinction and the right one to show: `merchant.growth_proposal.create` has no
   * tool because a proposal is a record for a person, not a call against the platform.
   *
   * It is not a general property of the endpoint. On the buyer roster the two vocabularies
   * diverge (`catalog.search` against `catalogue.read`), so this flag is only read on
   * screens that show a merchant role, and the copy beside it never generalises.
   */
  toolNamed: boolean;
}

export interface Boundary {
  role: SpecialistCapabilities;
  granted: CapabilityRow[];
  disabled: CapabilityRow[];
  elsewhereOnTheSurface: { role: string; capabilities: CapabilityRow[] }[];
  /**
   * Capabilities this operator session holds that do not travel to any agent.
   *
   * The harness intersects the session with the agent surface, so this is the narrowing
   * the platform performs before a merchant is consulted at all. It is empty for an
   * operator session whose capabilities are already agent-safe, and empty is an answer
   * worth rendering: it says the boundary was drawn one level up, when the session was
   * minted, rather than here.
   */
  withheldFromEveryAgent: string[];
  /** The server's own list of verbs no agent may hold, verbatim. */
  neverOnAnyAgent: string[];
  /**
   * Whether `neverOnAnyAgent` is the whole of what an agent cannot do. It is not, and
   * saying so is the difference between an argument and a slogan.
   */
  exhaustive: false;
  counts: Record<CapabilityKind, number>;
}

function rowFor(capability: string, role: SpecialistCapabilities): CapabilityRow {
  const gloss = glossOf(capability);
  return {
    capability,
    label: capabilityLabel(capability),
    detail: gloss?.detail ?? null,
    kind: gloss?.kind ?? "unclassified",
    known: gloss !== null,
    toolNamed: role.tools.includes(capability),
  };
}

function tally(rows: readonly CapabilityRow[]): Record<CapabilityKind, number> {
  const counts: Record<CapabilityKind, number> = { read: 0, propose: 0, unclassified: 0 };
  for (const row of rows) counts[row.kind] += 1;
  return counts;
}

/**
 * The boundary for one composition, or `null` when the roster no longer has its role.
 *
 * `null` rather than an empty boundary: a screen with no role to describe should say the
 * roster moved, not draw an agent that holds nothing.
 */
export function deriveBoundary(
  caps: AgentCapabilities,
  composition: Composition,
): Boundary | null {
  const role = roleIn(caps, composition.role);
  if (role === null) return null;

  const chosen = new Set(composition.capabilities);
  const granted = role.capabilities.filter((c) => chosen.has(c)).map((c) => rowFor(c, role));
  const disabled = role.capabilities.filter((c) => !chosen.has(c)).map((c) => rowFor(c, role));

  const mine = new Set(role.capabilities);
  const elsewhereOnTheSurface = caps.specialists
    .filter((entry) => entry.specialist !== role.specialist)
    .map((entry) => ({
      role: entry.specialist,
      capabilities: entry.capabilities
        .filter((capability) => !mine.has(capability))
        .map((capability) => rowFor(capability, entry)),
    }))
    .filter((entry) => entry.capabilities.length > 0);

  const onTheAgentSurface = new Set(caps.agent_capabilities);
  const withheldFromEveryAgent = caps.session_capabilities.filter(
    (capability) => !onTheAgentSurface.has(capability),
  );

  return {
    role,
    granted,
    disabled,
    elsewhereOnTheSurface,
    withheldFromEveryAgent,
    neverOnAnyAgent: caps.absent_by_construction,
    exhaustive: false,
    counts: tally(granted),
  };
}

/**
 * Which of a turn's tool calls fall inside the composition, and which do not.
 *
 * This exists because of a gap the studio must not paper over. A turn from the sandbox
 * runs under this browser's operator session, which carries the role's whole declared
 * set; the composition is a draft in this browser and the platform has never seen it. So
 * a call the composition would have withheld still succeeds, and the only honest thing to
 * do is measure the difference and name it.
 *
 * `outside` is therefore not a denial and this module never calls it one. It is the
 * distance between the session that answered and the agent being composed — which is
 * worth seeing, because it is exactly what publishing would close.
 */
export interface Reconciliation {
  inside: string[];
  outside: string[];
}

export function reconcileCalls(
  composition: Composition,
  calls: readonly { name: string }[],
): Reconciliation {
  const granted = new Set(composition.capabilities);
  const inside: string[] = [];
  const outside: string[] = [];
  for (const call of calls) {
    // The merchant tool table keys its tools by capability name, so a call's name is the
    // capability it consumed on this surface. A name that is not a capability at all --
    // a presentation tool, say -- is neither inside nor outside a capability grant, and
    // is left out of both lists rather than counted against the merchant's choices.
    if (granted.has(call.name)) inside.push(call.name);
    else if (glossOf(call.name) !== null) outside.push(call.name);
  }
  return { inside, outside };
}
