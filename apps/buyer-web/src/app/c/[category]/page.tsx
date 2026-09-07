/**
 * One aisle of the merchant's catalogue.
 *
 * Pagination is by the server's cursor, and pages are appended rather than merged: the
 * API decides what the second page contains and in what order, and a client that tried to
 * de-duplicate or re-sort would be second-guessing a catalogue revision it cannot see.
 *
 * An unknown slug is not a crash. The API answers 422 for a category it does not have,
 * and the honest rendering of that is "this shop has no such aisle" with a way back --
 * not a stack of problem-document fields a buyer has no use for.
 *
 * The loaded pages are held under the slug they were fetched for, so navigating to
 * another aisle shows a loading state by comparison at render time rather than by wiping
 * state inside an effect. One aisle's products can never appear under another's heading.
 */
"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useState } from "react";

import { Button, EmptyState, ErrorState } from "@/components/ui";
import { api } from "@/lib/api/client";
import { ApiError, humanMessage } from "@/lib/api/problem";
import type { Product } from "@/lib/api/types";
import { CATEGORIES, categoryLabel } from "@/lib/product-images";
import { useCart } from "@/features/cart/use-cart";
import { ProductGrid } from "@/features/storefront/product-grid";

const PAGE_SIZE = 50;

type Aisle =
  | { key: string; status: "loading" }
  | { key: string; status: "missing" }
  | { key: string; status: "error"; problem: string }
  | {
      key: string;
      status: "ready";
      products: Product[];
      cursor: string | null;
      matched: number;
      /** A failure while appending a later page. The pages already shown stay shown. */
      pageProblem: string | null;
      loadingMore: boolean;
    };

export default function CategoryPage({ params }: { params: Promise<{ category: string }> }) {
  const { category } = use(params);
  const slug = decodeURIComponent(category);

  const [attempt, setAttempt] = useState(0);
  const key = `${attempt} ${slug}`;
  const [state, setState] = useState<Aisle>({ key, status: "loading" });

  const cart = useCart();

  useEffect(() => {
    const controller = new AbortController();

    api
      .products({ category: slug, limit: PAGE_SIZE, signal: controller.signal })
      .then((page) => {
        if (controller.signal.aborted) return;
        setState({
          key,
          status: "ready",
          products: page.products,
          cursor: page.next_cursor,
          matched: page.matched,
          pageProblem: null,
          loadingMore: false,
        });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        // 422 means the server has no such category; 404 means the route resolved to
        // nothing. Either way the buyer asked for an aisle that does not exist, which is
        // a fact about the shop rather than a fault to apologise for.
        if (error instanceof ApiError && (error.status === 422 || error.status === 404)) {
          setState({ key, status: "missing" });
          return;
        }
        setState({
          key,
          status: "error",
          problem: error instanceof ApiError ? humanMessage(error) : "Could not reach the catalogue.",
        });
      });

    return () => controller.abort();
  }, [key, slug]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  // Anything held under a different slug belongs to the aisle the buyer just left.
  const current: Aisle = state.key === key ? state : { key, status: "loading" };
  const nextCursor = current.status === "ready" && !current.loadingMore ? current.cursor : null;

  const loadMore = useCallback(() => {
    const cursor = nextCursor;
    if (!cursor) return;
    setState((held) =>
      held.key === key && held.status === "ready" ? { ...held, loadingMore: true, pageProblem: null } : held,
    );

    api
      .products({ category: slug, limit: PAGE_SIZE, cursor })
      .then((page) => {
        setState((held) =>
          held.key === key && held.status === "ready"
            ? {
                ...held,
                products: [...held.products, ...page.products],
                cursor: page.next_cursor,
                loadingMore: false,
              }
            : held,
        );
      })
      .catch((error: unknown) => {
        setState((held) =>
          held.key === key && held.status === "ready"
            ? {
                ...held,
                loadingMore: false,
                pageProblem:
                  error instanceof ApiError ? humanMessage(error) : "Could not load the next page.",
              }
            : held,
        );
      });
  }, [key, nextCursor, slug]);

  if (current.status === "missing") {
    return (
      <main className="column py-10">
        <EmptyState
          title={`This shop has no “${slug}” aisle`}
          detail="The merchant's catalogue is split into ten categories. Pick one below."
          action={
            <ul className="mt-2 flex flex-wrap justify-center gap-2">
              {CATEGORIES.map((known) => (
                <li key={known}>
                  <Link
                    href={`/c/${known}`}
                    className="inline-flex items-center rounded-full bg-[var(--tint-1)] px-3 py-1.5 text-[13px] font-medium text-[var(--ink-2)] transition hover:bg-[var(--tint-2)]"
                  >
                    {categoryLabel(known)}
                  </Link>
                </li>
              ))}
            </ul>
          }
        />
      </main>
    );
  }

  if (current.status === "error") {
    return (
      <main className="column py-6">
        <h1 className="text-[20px] font-bold text-[var(--ink)]">{categoryLabel(slug)}</h1>
        <ErrorState title="The catalogue did not answer" detail={current.problem} onRetry={retry} />
      </main>
    );
  }

  const loaded = current.status === "ready" ? current : null;

  return (
    <main className="column py-6">
      <header className="mb-5">
        <h1 className="text-[20px] font-bold text-[var(--ink)]">{categoryLabel(slug)}</h1>
        <p className="mt-1 text-[12px] text-[var(--ink-4)]" aria-live="polite">
          {loaded
            ? `${loaded.matched} ${loaded.matched === 1 ? "product" : "products"} · showing ${loaded.products.length}`
            : "Loading the aisle…"}
        </p>
      </header>

      <ProductGrid
        products={loaded?.products ?? []}
        loading={!loaded}
        emptyLabel={`Nothing is listed in ${categoryLabel(slug)} right now`}
        emptyDetail="The merchant has no products on this shelf at the current catalogue revision."
        quantities={cart.quantities}
        onAdd={cart.add}
        onSetQuantity={cart.setQuantity}
        busySku={cart.busySku}
      />

      {loaded?.pageProblem ? (
        <p role="alert" className="mt-6 text-center text-[13px] text-[var(--red)]">
          {loaded.pageProblem}
        </p>
      ) : null}

      {loaded?.cursor ? (
        <div className="mt-8 flex justify-center">
          <Button variant="ghost" onClick={loadMore} busy={loaded.loadingMore}>
            Load more
          </Button>
        </div>
      ) : null}
    </main>
  );
}
