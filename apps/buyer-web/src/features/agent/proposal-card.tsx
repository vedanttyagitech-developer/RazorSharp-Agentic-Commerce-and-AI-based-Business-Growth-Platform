"use client";
import { Check, ShieldCheck } from "lucide-react";

import { formatMinor } from "@/lib/money";
import type { CheckoutProposal } from "./types";

export function ProposalCard({
  proposal,
  onAuthorize,
  isAuthorizing,
}: {
  proposal: CheckoutProposal;
  onAuthorize: (proposal: CheckoutProposal) => void;
  isAuthorizing?: boolean;
}) {
  return (
    <div
      role="region"
      aria-label={`Checkout Proposal Version ${proposal.version}`}
      className="rounded-2xl border-2 border-[#0c831f]/40 bg-surface p-4 shadow-sm space-y-3.5 transition-all"
    >
      {/* Header with Governance Tag */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line pb-2.5">
        <div className="flex items-center gap-2">
          <span className="flex h-5 items-center rounded-full bg-[#0c831f] px-2 text-[10px] font-black uppercase tracking-wider text-white">
            Proposal v{proposal.version}
          </span>
          <span className="text-xs font-bold text-foreground">
            Awaiting Human Authorization
          </span>
        </div>
        <span className="text-[10px] font-mono text-muted">
          Hash: {proposal.contentHash.slice(0, 8)}...
        </span>
      </div>

      {/* Governance Explanation */}
      <div className="rounded-xl border border-amber-500/20 bg-amber-50/50 p-2.5 text-[11px] text-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
        <strong className="block font-bold">Trusted Surface Handoff</strong>
        The shopping agent drafted this basket. By design, autonomous agents cannot execute payments. You must review the facts and authorize on the trusted surface.
      </div>

      {/* Item List */}
      <div className="space-y-1.5 divide-y divide-line/60">
        {proposal.items.map((item) => (
          <div key={item.sku} className="pt-1.5 flex items-center justify-between text-xs">
            <div>
              <span className="font-semibold text-foreground">{item.name}</span>
              <span className="text-muted ml-1.5">× {item.quantity}</span>
            </div>
            <span className="font-mono tabular-nums font-bold text-foreground">
              {formatMinor(item.subtotalMinor, proposal.currency)}
            </span>
          </div>
        ))}
      </div>

      {/* Totals Breakdown */}
      <div className="border-t border-line pt-2.5 space-y-1 text-xs">
        <div className="flex justify-between text-muted">
          <span>Items subtotal</span>
          <span className="font-mono tabular-nums">{formatMinor(proposal.itemsSubtotalMinor, proposal.currency)}</span>
        </div>
        <div className="flex justify-between text-muted">
          <span>Delivery fee</span>
          <span className="font-mono tabular-nums">
            {proposal.deliveryFeeMinor === 0 ? "FREE" : formatMinor(proposal.deliveryFeeMinor, proposal.currency)}
          </span>
        </div>
        {proposal.deliveryTaxMinor > 0 && (
          <div className="flex justify-between text-muted">
            <span>GST on delivery</span>
            <span className="font-mono tabular-nums">{formatMinor(proposal.deliveryTaxMinor, proposal.currency)}</span>
          </div>
        )}
        <div className="flex justify-between text-sm font-black text-foreground pt-1 border-t border-line/60">
          <span>Total Authorized Amount</span>
          <span className="font-mono tabular-nums text-[#0c831f]">
            {formatMinor(proposal.totalMinor, proposal.currency)}
          </span>
        </div>
      </div>

      {/* Human-in-the-loop Authorization Button */}
      <button
        type="button"
        disabled={isAuthorizing || proposal.status === "authorized"}
        onClick={() => onAuthorize(proposal)}
        className="w-full flex items-center justify-center gap-2 rounded-xl bg-[#0c831f] hover:bg-[#0a721b] disabled:bg-stone-300 dark:disabled:bg-stone-800 text-white py-2.5 px-4 font-bold text-xs shadow-xs transition active:scale-[0.99] cursor-pointer"
      >
        {isAuthorizing ? (
          <>
            <span className="flex h-3 w-3 rounded-full bg-white animate-spin" />
            <span>Verifying with Kernel...</span>
          </>
        ) : proposal.status === "authorized" ? (
          <>
            <Check className="h-4 w-4" aria-hidden="true" />
            <span>Authorized by Human</span>
          </>
        ) : (
          <>
            <ShieldCheck className="h-4 w-4" aria-hidden="true" />
            <span>Review &amp; Authorize Payment</span>
          </>
        )}
      </button>
    </div>
  );
}
