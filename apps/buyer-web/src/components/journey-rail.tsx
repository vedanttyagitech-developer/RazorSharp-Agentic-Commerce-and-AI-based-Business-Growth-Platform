"use client";

import { JOURNEY_META, JOURNEY_STATES, type JourneyState } from "@/lib/journey";

import { TONE_CLASSES } from "./ui";

/**
 * All sixteen required UI states (spec 8.2), each with its own glyph and label. The
 * current one carries aria-current and a visible "now" marker; ones already reached in
 * this session are marked "seen". Colour is decoration on top of that text.
 */
export function JourneyRail({ current, visited }: { current: JourneyState; visited: ReadonlySet<JourneyState> }) {
  return (
    <nav aria-label="Transaction journey states">
      <ol className="grid grid-cols-2 gap-1.5 text-xs sm:grid-cols-4">
        {JOURNEY_STATES.map((state) => {
          const meta = JOURNEY_META[state];
          const isCurrent = state === current;
          const seen = visited.has(state);
          return (
            <li
              key={state}
              aria-current={isCurrent ? "step" : undefined}
              className={`flex items-center gap-1.5 rounded border px-2 py-1 ${isCurrent ? `${TONE_CLASSES[meta.tone]} ring-2 ring-accent font-semibold` : seen ? "border-line bg-surface" : "border-dashed border-line text-muted"}`}
            >
              <span aria-hidden="true" className="w-4 text-center font-mono">{meta.glyph}</span>
              <span className="flex-1">{meta.label}</span>
              {isCurrent ? <span className="rounded bg-accent px-1 text-[10px] uppercase text-accent-ink">now</span> : seen ? <span className="sr-only">(seen)</span> : null}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
