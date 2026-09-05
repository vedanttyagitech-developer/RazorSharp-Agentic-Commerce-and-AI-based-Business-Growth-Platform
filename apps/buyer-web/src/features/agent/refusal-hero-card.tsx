"use client";
import { Check, ArrowRight, ShieldAlert } from "lucide-react";

import { formatMinor } from "@/lib/money";
import type { ReapprovalDecision } from "./types";

export function RefusalHeroCard({
  decision,
  onApproveNext,
  onCancel,
  isApproving,
}: {
  decision: ReapprovalDecision;
  onApproveNext: (decision: ReapprovalDecision) => void;
  onCancel?: () => void;
  isApproving?: boolean;
}) {
  const diffMinor = decision.newTotalMinor - decision.oldTotalMinor;

  return (
    <div
      role="alert"
      aria-label="Kernel Price Protection Refusal"
      className="rounded-2xl border-2 border-rose-500/70 bg-surface p-4 sm:p-5 shadow-lg space-y-4 transition-all"
    >
      {/* Refusal Banner Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-rose-200 dark:border-rose-900 pb-3">
        <div className="flex items-center gap-2">
          <span className="flex h-6 items-center gap-1.5 rounded-full bg-rose-600 px-2.5 text-[10px] font-black uppercase tracking-wider text-white shadow-xs animate-pulse">
            <ShieldAlert className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            <span>Hero Moment · Kernel Guard</span>
          </span>
          <span className="text-xs sm:text-sm font-black text-rose-700 dark:text-rose-300">
            Re-Approval Required
          </span>
        </div>
        <span className="rounded-full bg-rose-100 dark:bg-rose-950 px-2 py-0.5 text-[10px] font-bold text-rose-800 dark:text-rose-200">
          v{decision.invalidatedVersion} INVALIDATED ➔ v{decision.nextVersion} PROPOSED
        </span>
      </div>

      {/* Narrative Statement for Judges */}
      <div className="space-y-1.5 text-xs text-foreground/90">
        <p className="font-bold text-rose-950 dark:text-rose-100">
          The merchant modified pricing while checkout was in progress.
        </p>
        <p className="text-muted leading-relaxed">
          The transaction kernel refused to execute payment against stale facts. A lesser system would have silently debited your card for the higher amount. Here, Version {decision.invalidatedVersion} is permanently dead; Version {decision.nextVersion} reflects the live state.
        </p>
      </div>

      {/* Delta Diff Table */}
      <div className="rounded-xl border border-line bg-surface-raised overflow-hidden">
        <div className="bg-surface px-3 py-2 border-b border-line flex items-center justify-between text-[11px] font-bold text-muted uppercase tracking-wider">
          <span>Detected Field Change</span>
          <span>Old Value ➔ New Value</span>
        </div>
        <div className="divide-y divide-line/60">
          {decision.deltas.map((delta, idx) => (
            <div key={idx} className="px-3 py-2.5 flex flex-col xs:flex-row xs:items-center justify-between gap-1 text-xs">
              <div className="space-y-0.5 min-w-0">
                <span className="font-semibold text-foreground break-words">{delta.label}</span>
                <span className="block text-[10px] font-mono text-muted break-all">
                  Reason: {delta.reason}
                </span>
              </div>
              <div className="text-left xs:text-right shrink-0 pt-0.5 xs:pt-0">
                <div className="flex items-center gap-1.5 font-mono tabular-nums font-bold">
                  <span className="line-through text-muted text-[11px]">{delta.before}</span>
                  <ArrowRight className="h-3 w-3 text-rose-600 dark:text-rose-400 shrink-0" aria-hidden="true" />
                  <span className="text-foreground text-xs">{delta.after}</span>
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Total Impact Summary */}
      <div className="flex items-center justify-between rounded-xl bg-rose-50 dark:bg-rose-950/40 p-3 border border-rose-200 dark:border-rose-900 text-xs">
        <div>
          <span className="font-bold text-rose-950 dark:text-rose-200 block">Total Price Impact</span>
          <span className="text-[11px] text-rose-800 dark:text-rose-300">
            Difference of +{formatMinor(diffMinor, decision.currency)}
          </span>
        </div>
        <div className="text-right font-mono tabular-nums">
          <span className="line-through text-xs text-muted block">
            {formatMinor(decision.oldTotalMinor, decision.currency)}
          </span>
          <strong className="text-base font-black text-rose-700 dark:text-rose-300">
            {formatMinor(decision.newTotalMinor, decision.currency)}
          </strong>
        </div>
      </div>

      {/* Actions */}
      <div className="flex flex-col sm:flex-row gap-2 pt-1">
        <button
          type="button"
          disabled={isApproving}
          onClick={() => onApproveNext(decision)}
          className="flex-1 min-h-[44px] flex items-center justify-center gap-2 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white py-3 px-4 font-bold text-xs shadow-xs transition active:scale-[0.99] focus-visible:ring-2 focus-visible:ring-emerald-500 cursor-pointer disabled:opacity-50"
        >
          {isApproving ? (
            <>
              <span className="flex h-3 w-3 rounded-full bg-white animate-spin" />
              <span>Authorizing Version {decision.nextVersion}...</span>
            </>
          ) : (
            <>
              <Check className="h-4 w-4" aria-hidden="true" />
              <span>Authorize Version {decision.nextVersion} ({formatMinor(decision.newTotalMinor, decision.currency)})</span>
            </>
          )}
        </button>

        {onCancel && (
          <button
            type="button"
            disabled={isApproving}
            onClick={onCancel}
            className="min-h-[44px] flex items-center justify-center rounded-xl border border-line bg-surface hover:bg-surface-raised text-muted hover:text-foreground py-3 px-4 font-bold text-xs transition focus-visible:ring-2 focus-visible:ring-[#0c831f] cursor-pointer"
          >
            Cancel Order
          </button>
        )}
      </div>
    </div>
  );
}
