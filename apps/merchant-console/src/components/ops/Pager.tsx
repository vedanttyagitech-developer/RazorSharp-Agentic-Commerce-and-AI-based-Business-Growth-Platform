"use client";

/**
 * Keyset pagination, as the API actually does it.
 *
 * `next_cursor` is opaque and handed back verbatim; there is no page number and no total
 * page count, because a keyset cursor has neither. Going back is therefore a stack of the
 * cursors already visited rather than an arithmetic step, which is also what makes the
 * page after a cursor never repeat or skip a row however many orders land in between.
 */
import { useCallback, useState } from "react";
import { Button } from "@/components/ui";

export interface Cursors {
  /** The cursor for the page currently displayed. `undefined` is the first page. */
  cursor: string | undefined;
  /** True when there is a page before this one. */
  hasPrevious: boolean;
  next: (nextCursor: string) => void;
  previous: () => void;
  /** Return to the first page. Called whenever a filter changes the query. */
  reset: () => void;
}

export function useCursors(): Cursors {
  const [stack, setStack] = useState<Array<string | undefined>>([undefined]);
  const cursor = stack[stack.length - 1];
  const next = useCallback((nextCursor: string) => setStack((s) => [...s, nextCursor]), []);
  const previous = useCallback(
    () => setStack((s) => (s.length > 1 ? s.slice(0, -1) : s)),
    [],
  );
  const reset = useCallback(() => setStack([undefined]), []);
  return { cursor, hasPrevious: stack.length > 1, next, previous, reset };
}

export function Pager({
  cursors,
  nextCursor,
  shown,
  matched,
  noun,
}: {
  cursors: Cursors;
  nextCursor: string | null;
  shown: number;
  matched?: number;
  noun: string;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-t border-[var(--line)] px-4 py-2.5">
      <p className="text-[11.5px] text-[var(--muted)]">
        <span className="num text-[var(--ink)]">{shown}</span> {noun} on this page
        {matched !== undefined && (
          <>
            {" "}
            · <span className="num text-[var(--ink)]">{matched}</span> matched by this filter
          </>
        )}
        {nextCursor === null && shown > 0 && " · last page"}
      </p>
      <div className="flex items-center gap-2">
        <Button onClick={cursors.previous} disabled={!cursors.hasPrevious}>
          ← Previous
        </Button>
        <Button onClick={() => nextCursor && cursors.next(nextCursor)} disabled={nextCursor === null}>
          Next →
        </Button>
      </div>
    </div>
  );
}
