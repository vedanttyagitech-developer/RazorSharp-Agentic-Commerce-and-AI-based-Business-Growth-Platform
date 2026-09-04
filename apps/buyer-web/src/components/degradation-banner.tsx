"use client";

import { AlertTriangle } from "lucide-react";
import { useDegradation } from "./providers";

/**
 * Spec 19.12: a degraded path is always visible to the buyer. Silent degradation is a
 * defect. Notices are announced politely and can be dismissed once read; the underlying
 * condition is re-reported by whatever detected it.
 */
export function DegradationBanner() {
  const { notices, clear } = useDegradation();
  return (
    <div role="status" aria-live="polite" aria-atomic="false" className="contents">
      {notices.length > 0 ? (
        <ul className="border-b border-orange-300 bg-orange-50 text-orange-950 dark:border-orange-700 dark:bg-orange-950 dark:text-orange-50">
          {notices.map((notice) => (
            <li key={notice.id} className="mx-auto flex w-full max-w-5xl items-center gap-3 px-4 py-2 text-sm">
              <AlertTriangle className="h-4 w-4 text-orange-600 dark:text-orange-400 shrink-0" aria-hidden="true" />
              <p className="flex-1 text-xs sm:text-sm">
                <span className="font-semibold">Degraded: {notice.component}.</span> {notice.message} You can always type.
              </p>
              <button type="button" onClick={() => clear(notice.id)} className="rounded-lg border border-current px-2.5 py-1 text-xs font-semibold hover:bg-orange-100 dark:hover:bg-orange-900/40 transition cursor-pointer" aria-label={`Dismiss notice about ${notice.component}`}>
                Dismiss
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
