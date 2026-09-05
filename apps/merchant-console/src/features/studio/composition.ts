/**
 * What a merchant composes, and the two rules that make composing it safe.
 *
 * **A composition can only ever narrow.** Every function here takes the specialist the
 * server declared and returns a subset of it. There is no path through this module by
 * which a capability the server did not list for that role reaches a composition, which
 * is why the compose screen offers checkboxes over a server list and never a text field:
 * a merchant who cannot spell a capability cannot ask for one. `AgentPrincipal.subset_for`
 * refuses a widening on the server side; this is the same rule stated where the merchant
 * is standing, so the screen and the kernel are arguing for the same thing.
 *
 * **A stored draft is re-narrowed against the server on every load.** The platform's
 * roster is not this browser's to remember. A draft saved last week that names a
 * capability the server has since stopped declaring loses it, and `restore` reports what
 * it dropped rather than discarding it quietly -- a merchant whose agent silently lost a
 * capability would go on believing it held one.
 *
 * Persistence is `localStorage`, and the screen says so. There is no endpoint behind
 * this: publishing an immutable merchant-configuration version is specification 7.1 step
 * 16, and section 36 puts the authoring/publish/rollback workflow outside P0. Writing a
 * draft into the browser and saying it is a draft in the browser is the honest amount of
 * persistence to claim; a Publish button with nothing behind it would be the half-built
 * UI that section 36 closes by forbidding.
 */
import type { AgentCapabilities, SpecialistCapabilities } from "@/lib/api/types";

/**
 * A briefing is bounded because an unbounded one is a place to hide a novel.
 *
 * The platform's own fence caps a merchant string at 400 characters for the same reason
 * (`agent_runtime.core.fencing.MAX_MERCHANT_TEXT_CHARS`). This bound is the console's,
 * on a field the platform does not yet read, and it is looser because a briefing is a
 * paragraph a merchant writes about their shop rather than a product name. It is a bound
 * all the same.
 */
export const BRIEFING_MAX_CHARS = 1200;

/** Long enough for "Weekend stock assistant", short enough to be a label and not a note. */
export const NAME_MAX_CHARS = 60;

/**
 * One composed assistant.
 *
 * `role` is a specialist the server named. `capabilities` is a subset of what the server
 * declared for that role -- never a superset, never a string this console invented.
 * `briefing` is merchant-authored text and is treated everywhere as *data*: specification
 * 6.5 requires merchant instructions to remain lower-precedence data that cannot change
 * system invariants, and this console never presents it as anything else.
 */
export interface Composition {
  role: string;
  capabilities: string[];
  name: string;
  briefing: string;
}

/** The specialist row for a role, or `null` when this session's roster has no such role. */
export function roleIn(caps: AgentCapabilities, role: string): SpecialistCapabilities | null {
  return caps.specialists.find((entry) => entry.specialist === role) ?? null;
}

/**
 * The chosen capabilities, intersected with what the server declared for that role.
 *
 * Order comes from the server's list rather than from the merchant's clicks, so two
 * compositions holding the same set are the same list and a diff between them is about
 * capabilities rather than about the order somebody happened to tick boxes in.
 */
export function narrow(role: SpecialistCapabilities, chosen: Iterable<string>): string[] {
  const wanted = new Set(chosen);
  return role.capabilities.filter((capability) => wanted.has(capability));
}

/**
 * The composition a merchant starts from: the role's full declared set, enabled.
 *
 * Starting wide and letting the merchant switch things off is the shape specification 7.2
 * describes -- "enable or disable non-money features" over the built-in agents -- and it
 * is also the honest default, because the ceiling is what the platform will bind whether
 * or not this console draws it.
 */
export function startingComposition(role: SpecialistCapabilities): Composition {
  return {
    role: role.specialist,
    capabilities: [...role.capabilities],
    name: "",
    briefing: "",
  };
}

/**
 * A stored draft, reconciled with the roster the server is declaring right now.
 *
 * `dropped` is the part that matters. A capability the draft named and the server no
 * longer lists for that role is removed and reported, because the alternative is a screen
 * that keeps showing a merchant a capability their agent would not be bound.
 */
export interface Restored {
  composition: Composition;
  /** Capabilities the stored draft named that this role no longer declares. */
  dropped: string[];
  /** The stored role, when the server's roster no longer has it. */
  missingRole: string | null;
}

export function reconcile(caps: AgentCapabilities, stored: Composition): Restored | null {
  const first = caps.specialists[0];
  if (first === undefined) return null;
  const role = roleIn(caps, stored.role);
  if (role === null) {
    return {
      composition: startingComposition(first),
      dropped: [],
      missingRole: stored.role,
    };
  }
  const kept = narrow(role, stored.capabilities);
  const declared = new Set(role.capabilities);
  return {
    composition: {
      role: role.specialist,
      capabilities: kept,
      name: stored.name.slice(0, NAME_MAX_CHARS),
      briefing: stored.briefing.slice(0, BRIEFING_MAX_CHARS),
    },
    dropped: stored.capabilities.filter((capability) => !declared.has(capability)),
    missingRole: null,
  };
}

// -------------------------------------------------------------------- this browser

const STORAGE_KEY = "acr.studio.composition.v1";

/**
 * Read the draft this browser is holding, or `null`.
 *
 * Every failure mode returns `null` rather than throwing: `localStorage` is absent during
 * server rendering, throws outright in some privacy modes, and can hold whatever a
 * previous version of this console wrote. None of those is worth a broken screen, and
 * none of them is a fact about the merchant's agent.
 */
export function readStoredComposition(): Composition | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw === null) return null;
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return null;
    const value = parsed as Record<string, unknown>;
    if (typeof value.role !== "string") return null;
    const capabilities = Array.isArray(value.capabilities)
      ? value.capabilities.filter((entry): entry is string => typeof entry === "string")
      : [];
    return {
      role: value.role,
      capabilities,
      name: typeof value.name === "string" ? value.name : "",
      briefing: typeof value.briefing === "string" ? value.briefing : "",
    };
  } catch {
    return null;
  }
}

/** Write the draft. Returns whether the browser accepted it, so the screen can say. */
export function writeStoredComposition(composition: Composition): boolean {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(composition));
    return true;
  } catch {
    return false;
  }
}

export function clearStoredComposition(): boolean {
  try {
    window.localStorage.removeItem(STORAGE_KEY);
    return true;
  } catch {
    return false;
  }
}
