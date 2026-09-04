"use client";

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
            <li key={notice.id} className="mx-auto flex w-full max-w-5xl items-start gap-3 px-4 py-2 text-sm">
              <span aria-hidden="true" className="font-mono">⚠</span>
              <p className="flex-1">
                <span className="font-semibold">Degraded: {notice.component}.</span> {notice.message} You can always type.
              </p>
              <button type="button" onClick={() => clear(notice.id)} className="rounded border border-current px-2 py-0.5 text-xs" aria-label={`Dismiss notice about ${notice.component}`}>
                Dismiss
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
