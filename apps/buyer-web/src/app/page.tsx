/**
 * The home page: banners, categories, best sellers, and the trust strip.
 *
 * It renders on the client and fetches on mount because `api` talks to this app's own
 * origin at `/api/backend/...`, where the route handler attaches the bearer token the
 * browser is never given. A server component here would have no session to fetch with.
 *
 * The failure path is deliberately blunt. If the catalogue read fails the page says so
 * and offers to try again; it does not fall back to a fixture. A storefront that
 * substitutes invented products is showing a buyer a price no kernel ever agreed to,
 * and on the way to a payment that is worse than an empty screen.
 *
 * Basket writes live here rather than inside the grid so that exactly one component in
 * this tree talks to the basket API. Every quantity the cards draw came back from
 * `PUT /v1/baskets/{id}/lines/{sku}`; nothing is guessed forward before the server has
 * agreed to it, because an optimistic count that the server then declines is a count the
 * buyer would carry into an approval.
 */
"use client";

import { useCallback, useEffect, useState } from "react";

import { useBasketContext } from "@/components/providers";
import { ErrorState, Skeleton } from "@/components/ui";
import { CategoryGrid } from "@/features/storefront/category-grid";
import { ProductGrid } from "@/features/storefront/product-grid";
import { PromoBanners } from "@/features/storefront/promo-banners";
import { ApiError, api } from "@/lib/api/client";
import type { Product } from "@/lib/api/types";

const BEST_SELLER_LIMIT = 20;

/** A stable empty map, so a basket-less render does not hand the grid a new object. */
const NOTHING_IN_BASKET: Readonly<Record<string, number>> = Object.freeze({});

/** The five claims the trust strip makes, each with the artwork already in `public`. */
const ASSURANCES: ReadonlyArray<{ src: string; title: string; caption: string }> = [
  {
    src: "/infographics/delivery-10-min.svg",
    title: "Delivery in minutes",
    caption: "Picked, packed and out of the nearest store while you are still on the page.",
  },
  {
    src: "/infographics/best-prices.svg",
    title: "Best prices",
    caption: "Every price here is the merchant's own, quoted to the paisa by the server.",
  },
  {
    src: "/infographics/wide-assortment.svg",
    title: "Wide assortment",
    caption: "Groceries, household, personal care and electronics from a single basket.",
  },
  {
    src: "/infographics/cold-chain.svg",
    title: "Unbroken cold chain",
    caption: "Dairy, frozen and fresh travel chilled from the shelf to your door.",
  },
  {
    src: "/infographics/doorstep-return.svg",
    title: "Returns at the door",
    caption: "Hand back anything you are unhappy with; the refund is issued automatically.",
  },
];

/** Placeholders shaped like the 191 × 315 card, so the page does not jump when they go. */
function ProductGridSkeleton() {
  return (
    <div
      aria-hidden="true"
      className="grid grid-cols-2 justify-items-center gap-x-3 gap-y-6 md:grid-cols-3 md:gap-x-5 lg:grid-cols-4 xl:grid-cols-6"
    >
      {Array.from({ length: 12 }, (_, index) => (
        <Skeleton key={index} className="h-[315px] w-full max-w-[191px]" />
      ))}
    </div>
  );
}

function TrustStrip() {
  return (
    <section aria-labelledby="assurances-heading" className="mt-14">
      <h2 id="assurances-heading" className="sr-only">
        Why shop here
      </h2>
      <ul className="grid grid-cols-2 gap-6 border-t border-[var(--header-line)] pt-10 sm:grid-cols-3 lg:grid-cols-5">
        {ASSURANCES.map((assurance) => (
          <li key={assurance.src} className="flex flex-col items-center text-center">
            {/* eslint-disable-next-line @next/next/no-img-element -- a local SVG drawn at a
                fixed 64px gains nothing from the optimiser, which would also need
                dangerouslyAllowSVG turned on for every image in the app. */}
            <img src={assurance.src} alt="" width={64} height={64} className="h-16 w-16" />
            <p className="mt-3 text-[13px] font-semibold text-[var(--ink)]">{assurance.title}</p>
            <p className="mt-1 text-[12px] leading-relaxed text-[var(--ink-4)]">{assurance.caption}</p>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** What went wrong, in the words the API used. Never a substitute for the data itself. */
interface ReadProblem {
  title: string;
  detail?: string;
}

function problemOf(error: unknown, fallback: string): ReadProblem {
  if (error instanceof ApiError) {
    return { title: error.problem.title, detail: error.problem.detail };
  }
  return { title: fallback, detail: error instanceof Error ? error.message : undefined };
}

export default function HomePage() {
  const { basketId, setBasketId, refresh } = useBasketContext();

  const [products, setProducts] = useState<Product[] | null>(null);
  const [problem, setProblem] = useState<ReadProblem | null>(null);
  const [attempt, setAttempt] = useState(0);

  /** SKU to the quantity the server currently holds on that basket line. */
  const [quantities, setQuantities] = useState<Record<string, number>>({});
  // With no basket there is nothing in it, which is derived rather than stored: clearing
  // the map in an effect would be a second source of truth for the same fact.
  const shown = basketId ? quantities : NOTHING_IN_BASKET;
  const [busySku, setBusySku] = useState<string | null>(null);
  const [writeFailure, setWriteFailure] = useState<string | null>(null);

  const retry = useCallback(() => {
    setProducts(null);
    setProblem(null);
    setAttempt((current) => current + 1);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;

    api
      .products({ limit: BEST_SELLER_LIMIT, signal: controller.signal })
      .then((page) => {
        if (!cancelled) setProducts(page.products);
      })
      .catch((error: unknown) => {
        if (cancelled || (error instanceof DOMException && error.name === "AbortError")) return;
        setProblem(problemOf(error, "The catalogue could not be read"));
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [attempt]);

  // The cards have to show what is already in the basket, or a returning buyer sees ADD
  // on something they added a minute ago.
  useEffect(() => {
    if (!basketId) return;
    const controller = new AbortController();
    let cancelled = false;

    api
      .basket(basketId, controller.signal)
      .then((basket) => {
        if (!cancelled) {
          setQuantities(Object.fromEntries(basket.lines.map((line) => [line.sku, line.quantity])));
        }
      })
      .catch(() => {
        // A basket the server has forgotten is the provider's problem to clear; the grid
        // simply shows nothing in it rather than an invented quantity.
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [basketId]);

  const write = useCallback(
    async (sku: string, quantity: number) => {
      setBusySku(sku);
      setWriteFailure(null);
      try {
        let id = basketId;
        if (!id) {
          const created = await api.createBasket();
          id = created.basket_id;
          setBasketId(id);
        }
        const basket = await api.setLine(id, sku, quantity);
        setQuantities(Object.fromEntries(basket.lines.map((line) => [line.sku, line.quantity])));
        await refresh();
      } catch (error) {
        const failed = problemOf(error, "That could not be added to your basket");
        setWriteFailure(failed.detail ? `${failed.title}. ${failed.detail}` : failed.title);
      } finally {
        setBusySku(null);
      }
    },
    [basketId, setBasketId, refresh],
  );

  const onAdd = useCallback(
    (sku: string) => void write(sku, (shown[sku] ?? 0) + 1),
    [write, shown],
  );
  const onSetQuantity = useCallback((sku: string, quantity: number) => void write(sku, quantity), [write]);

  return (
    <div className="column py-6">
      <PromoBanners />

      {/* The grid brings its own heading and landmark; wrapping it in a second would
          make a screen reader announce the aisle list twice. */}
      <div className="mt-10">
        <CategoryGrid />
      </div>

      <section aria-labelledby="best-sellers-heading" className="mt-12">
        <h2 id="best-sellers-heading" className="mb-4 text-[20px] font-bold text-[var(--ink)]">
          Best sellers
        </h2>

        <p role="status" aria-live="polite" className="sr-only">
          {busySku ? "Updating your basket" : ""}
        </p>
        {writeFailure ? (
          <p role="alert" className="mb-4 rounded-[var(--r-md)] bg-red-50 px-4 py-3 text-[13px] text-[var(--red)]">
            {writeFailure} Your basket has not been changed.
          </p>
        ) : null}

        <div aria-busy={products === null && problem === null}>
          {problem ? (
            <ErrorState
              title={problem.title}
              detail={problem.detail ?? "Nothing has been changed. The read can be tried again."}
              onRetry={retry}
            />
          ) : products === null ? (
            <ProductGridSkeleton />
          ) : (
            <ProductGrid
              products={products}
              emptyLabel="The catalogue answered with nothing"
              emptyDetail="The merchant has no listed products right now. That is what the API returned, not a page still loading."
              quantities={shown}
              onAdd={onAdd}
              onSetQuantity={onSetQuantity}
              busySku={busySku}
            />
          )}
        </div>
      </section>

      <TrustStrip />
    </div>
  );
}
