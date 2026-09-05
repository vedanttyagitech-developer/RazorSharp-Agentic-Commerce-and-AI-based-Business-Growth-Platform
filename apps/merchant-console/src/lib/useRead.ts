"use client";

/**
 * One read, with its three honest outcomes: reading, read, or failed.
 *
 * There is no fourth state. `data` and `error` are both tied to the exact inputs that
 * produced them, so a read that fails leaves no figures behind and a filter change does
 * not leave the previous filter's rows on screen under a new heading. That is the whole
 * point: a panel that kept its last-good numbers through a failure would show an operator
 * a queue depth from ten minutes ago while the platform was unreachable, and they would
 * act on it.
 *
 * Every read is abortable, and an abort is not a failure -- it is the effect cleaning up
 * after a filter change, and surfacing it would flash an error on every keystroke.
 */
import { useCallback, useEffect, useRef, useState } from "react";

export interface ReadState<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  /** Re-run the read. Safe to call from a button; cancels any read still in flight. */
  reload: () => void;
}

interface Settled<T> {
  /** The inputs this outcome belongs to. An outcome from other inputs is not shown. */
  key: string;
  data: T | null;
  error: unknown;
}

function isAbort(cause: unknown): boolean {
  return cause instanceof DOMException && cause.name === "AbortError";
}

export function useRead<T>(
  read: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
): ReadState<T> {
  const [settled, setSettled] = useState<Settled<T> | null>(null);
  const [nonce, setNonce] = useState(0);

  // The identity of this read: its inputs, plus the counter a manual reload advances.
  const key = JSON.stringify([...deps, nonce]);

  // The reader closes over props and state that change every render, while the read
  // itself must only re-run when `key` moves. This effect keeps the latest closure
  // available to the one below it, and is declared first so it has already run by the
  // time that one fires on the same commit.
  const reader = useRef(read);
  useEffect(() => {
    reader.current = read;
  });

  useEffect(() => {
    const controller = new AbortController();
    let live = true;
    reader
      .current(controller.signal)
      .then((value) => {
        if (live) setSettled({ key, data: value, error: null });
      })
      .catch((cause: unknown) => {
        if (live && !isAbort(cause)) setSettled({ key, data: null, error: cause });
      });
    return () => {
      live = false;
      controller.abort();
    };
  }, [key]);

  const reload = useCallback(() => setNonce((value) => value + 1), []);
  const current = settled && settled.key === key ? settled : null;
  return {
    data: current?.data ?? null,
    error: current?.error ?? null,
    loading: current === null,
    reload,
  };
}
