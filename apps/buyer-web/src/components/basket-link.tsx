"use client";

import Link from "next/link";

import { useBasketRef } from "./providers";

export function BasketLink() {
  const { lineCount } = useBasketRef();
  return (
    <Link href="/basket" className="underline-offset-4 hover:underline">
      Basket{lineCount > 0 ? <span className="ml-1 rounded-full bg-accent px-1.5 text-xs text-accent-ink" aria-label={`${lineCount} lines`}>{lineCount}</span> : null}
    </Link>
  );
}
