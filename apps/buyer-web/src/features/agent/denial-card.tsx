/**
 * What the panel shows when the agent asked for something outside its grant.
 *
 * This is a first-class rendering, not an error. Nothing broke: the buyer said "approve
 * it" or "pay now", the harness looked for a tool behind `checkout.approve`, and there is
 * none to look for, because consent is not delegable and the capability was never on the
 * agent surface. Drawing that as a red failure would teach a buyer to retry it, and would
 * misdescribe the single property this project exists to demonstrate.
 *
 * So the card is calm, it names the capability the way an audit log does, and it says in
 * a sentence why no agent will ever hold it. Amber and a barred shield rather than red
 * and a crash, and the heading carries the meaning on its own so the colour is never the
 * only signal.
 */
"use client";

import type { Denial } from "@/lib/api/types";

/** The Registry B capabilities a buyer asks for by name, in the words they asked in. */
const READABLE_CAPABILITIES: Readonly<Record<string, string>> = {
  "checkout.approve": "approve a checkout",
  "checkout.reject": "reject a checkout",
  "checkout.cancel": "cancel a checkout",
  "payment.verify": "confirm a payment",
  "refund.request": "start a refund",
  "grant.revoke": "revoke a payment grant",
  "basket.write": "change the basket",
  "catalogue.read": "read the catalogue",
  "checkout.create": "open a checkout",
  "checkout.submit_approved": "submit an approved checkout",
  "order.read": "read an order",
};

/**
 * The reason keys `agent_service` records, as sentences.
 *
 * The two that matter most are genuinely different facts and are worth distinguishing
 * for anyone reading over the buyer's shoulder: `not_on_agent_surface` means the session
 * does hold this capability -- a buyer may approve their own checkout -- and an agent
 * still may not exercise it on their behalf. `capability_missing` means the session never
 * held it at all.
 */
const READABLE_REASONS: Readonly<Record<string, string>> = {
  not_on_agent_surface:
    "You hold this one yourself, and it does not travel. Approving and paying are your consent, " +
    "so no tool for them is ever bound to an agent.",
  capability_missing: "This session does not carry that capability, so no tool for it exists.",
  tool_not_registered: "There is no tool registered for that action at all.",
  tool_budget_exhausted:
    "The turn's fixed tool budget was already spent. Ask again and it starts fresh.",
  tool_unavailable: "That part of the store could not be reached, so nothing was guessed.",
  tool_failed: "That check did not complete, and an unchecked answer is worse than none.",
  injected_instruction:
    "Some product text tried to issue an instruction. Product text is read as description, " +
    "never as a command.",
  specialist_not_allowed: "That tool belongs to a different specialist than the one that answered.",
};

function readableCapability(capability: string): string {
  return READABLE_CAPABILITIES[capability] ?? capability.replace(/[._]/g, " ");
}

function readableReason(reasonKey: string): string {
  return (
    READABLE_REASONS[reasonKey] ?? "The capability gate refused the call before anything ran."
  );
}

function BarredShield() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none">
      <path
        d="M12 2.8 L19 5.7 V11.4 C19 15.6 16.1 19.3 12 21.2 C7.9 19.3 5 15.6 5 11.4 V5.7 Z"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
      <path d="M8.6 14.4 L15.4 8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}

/**
 * Every refusal the harness recorded this turn.
 *
 * Renders nothing when there were none, so a caller can mount it unconditionally under
 * each reply without asking whether it will show.
 */
export function DenialCard({ denials }: { denials: readonly Denial[] }) {
  if (denials.length === 0) return null;

  return (
    <section
      className="mt-2 rounded-lg border border-white/10 bg-white/[0.04] p-3 text-slate-200"
      aria-label="Refused by the capability gate"
    >
      <div className="flex items-start gap-2">
        <span className="mt-px shrink-0 text-amber-300">
          <BarredShield />
        </span>
        <div className="min-w-0">
          <p className="text-[13px] font-semibold text-slate-100">
            RazorAI is not allowed to do that
          </p>
          <p className="mt-0.5 text-[12px] text-slate-300">
            It asked, and the capability gate refused before any tool ran. That is the system
            working, not a fault.
          </p>
        </div>
      </div>

      <ul className="mt-2.5 flex flex-col gap-2">
        {denials.map((denial, index) => (
          <li
            key={`${denial.capability}-${denial.reason_key}-${index}`}
            className="rounded-md border border-white/10 bg-black/25 px-2.5 py-2"
          >
            <p className="text-[12px] font-semibold text-slate-200">
              Refused: {readableCapability(denial.capability)}
            </p>
            <p className="mt-0.5 text-[12px] leading-[1.45] text-slate-300">
              {readableReason(denial.reason_key)}
            </p>
            <p className="tnum mt-1 font-mono text-[9px] tracking-[0.04em] text-slate-500">
              {denial.capability} · {denial.reason_key}
            </p>
          </li>
        ))}
      </ul>
    </section>
  );
}
