"use client";

import { ShieldAlert } from "lucide-react";
import type { DenialNotice } from "./types";

export function DenialCard({ denial }: { denial: DenialNotice }) {
  return (
    <div
      role="alert"
      className="my-2 rounded-xl border border-amber-300/80 bg-amber-50/80 p-3 text-xs shadow-xs dark:border-amber-700/60 dark:bg-amber-950/40"
    >
      <div className="flex items-start gap-2.5">
        <div className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-amber-200 text-amber-900 dark:bg-amber-800 dark:text-amber-100">
          <ShieldAlert className="h-3.5 w-3.5" aria-hidden="true" />
        </div>
        <div className="flex-1 space-y-1">
          <div className="flex items-center justify-between gap-2">
            <span className="font-bold tracking-tight text-amber-950 dark:text-amber-200 uppercase text-[10px]">
              Governance Gate: Action Denied
            </span>
            <span className="rounded bg-amber-200/80 dark:bg-amber-900/80 px-1.5 py-0.5 font-mono text-[9px] font-bold text-amber-900 dark:text-amber-200">
              {denial.reason_key}
            </span>
          </div>

          <p className="text-[11px] font-medium text-amber-900 dark:text-amber-300 leading-relaxed">
            Requested Capability: <code className="rounded bg-amber-100 dark:bg-amber-900/60 px-1 py-0.5 font-mono font-semibold">{denial.capability}</code>
          </p>

          <p className="text-[10px] text-amber-800/90 dark:text-amber-400/90 leading-tight pt-0.5">
            {denial.explanation ||
              "By Track 1 system invariants, conversational agents propose only and hold zero capability to authorize transactions, charge accounts, or execute refunds directly. All financial commitments require human approval on the trusted surface."}
          </p>
        </div>
      </div>
    </div>
  );
}
