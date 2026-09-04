"use client";

import Link from "next/link";

import { useBasketRef } from "./providers";
import { formatMinor } from "@/lib/money";

export function BasketLink() {
  const { lineCount, totalMinor, currency } = useBasketRef();

  if (lineCount === 0) {
    return (
      <Link
        href="/basket"
        className="inline-flex items-center gap-2 rounded-xl border border-line bg-surface px-3.5 py-1.5 text-xs font-bold text-foreground transition hover:border-[#ff3269] hover:bg-stone-50 dark:hover:bg-stone-800"
        aria-label="View basket (empty)"
      >
        <span aria-hidden="true" className="text-sm">🛒</span>
        <span>Cart</span>
      </Link>
    );
  }

  return (
    <Link
      href="/basket"
      className="inline-flex items-center gap-2 rounded-xl bg-[#ff3269] px-3.5 py-1.5 text-xs font-extrabold text-white shadow-sm transition hover:opacity-90 active:scale-95"
      aria-label={`View basket: ${lineCount} items, total ${totalMinor !== null ? formatMinor(totalMinor, currency) : ""}`}
    >
      <span aria-hidden="true" className="text-sm">🛍️</span>
      <span>
        {lineCount} {lineCount === 1 ? "Item" : "Items"}
      </span>
      {totalMinor !== null ? (
        <>
          <span aria-hidden="true" className="opacity-60">|</span>
          <span className="tabular-nums">{formatMinor(totalMinor, currency)}</span>
        </>
      ) : null}
    </Link>
  );
}
