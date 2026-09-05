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

import { useCallback, useEffect, useState, type ReactElement } from "react";

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

/**
 * The glyphs the trust strip draws, at the 48px the strip sets them in.
 *
 * Drawn here rather than pulled from `public/infographics`, because that artwork bakes
 * "10m" and "5K+" into the images themselves -- a delivery time no response carries and an
 * assortment four thousand items larger than the catalogue answers with -- and this
 * component cannot correct a claim that lives inside an SVG it only references.
 */
const GLYPH = "h-12 w-12 text-[var(--ink-3)]";

function ReceiptGlyph() {
  return (
    <svg
      viewBox="0 0 48 48"
      className={GLYPH}
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M13 6 H35 V42 L31.3 39 L27.7 42 L24 39 L20.3 42 L16.7 39 L13 42 Z" />
      <path d="M19 17 H29 M19 25 H29" />
    </svg>
  );
}

function SealGlyph() {
  return (
    <svg
      viewBox="0 0 48 48"
      className={GLYPH}
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="9" y="9" width="30" height="30" rx="7" />
      <path d="M20 15 V33 M28 15 V33 M14 21 H34 M14 27 H34" />
    </svg>
  );
}

function BarredShieldGlyph() {
  return (
    <svg
      viewBox="0 0 48 48"
      className={GLYPH}
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M24 5.5 L38 11.4 V22.8 C38 31.2 32.2 38.6 24 42.4 C15.8 38.6 10 31.2 10 22.8 V11.4 Z" />
      <path d="M17.2 28.8 L30.8 16" />
    </svg>
  );
}

function ParcelGlyph() {
  return (
    <svg
      viewBox="0 0 48 48"
      className={GLYPH}
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M24 6 L40 14.5 V31.5 L24 40 L8 31.5 V14.5 Z" />
      <path d="M8 14.5 L24 23 L40 14.5 M24 23 V40" />
    </svg>
  );
}

/**
 * The four things this page is entitled to say, and the glyph for each.
 *
 * The strip that stood here promised delivery in minutes, an unbroken cold chain and a
 * refund issued automatically at the door. This platform fulfils nothing and issues a
 * refund on its own only when a capture lands against a version nobody approved, and the
 * footer at the bottom of the same page said as much -- the two could not both be true,
 * and the one that was wrong was the one making promises. Every line below names a
 * property a reader can check in the network tab, and the last of them now agrees with
 * the footer instead of contradicting it.
 */
const ASSURANCES: ReadonlyArray<{ glyph: () => ReactElement; title: string; caption: string }> = [
  {
    glyph: ReceiptGlyph,
    title: "Priced by the server",
    caption:
      "Every figure on this storefront is integer paise the quote engine sent. The browser never adds, rounds or converts one.",
  },
  {
    glyph: SealGlyph,
    title: "An approval names exact bytes",
    caption:
      "You approve one version of a checkout, identified by its content hash. Move the basket and that approval stops working.",
  },
  {
    glyph: BarredShieldGlyph,
    title: "RazorAI cannot pay",
    caption:
      "Approving and paying are capabilities no agent holds. The panel proposes; the store's own pages decide.",
  },
  {
    glyph: ParcelGlyph,
    title: "Nothing is fulfilled",
    caption:
      "No order leaves a shelf and no money is real: this is a demonstration, and Razorpay runs in test mode throughout.",
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
        What this storefront can show you
      </h2>
      <ul className="grid grid-cols-1 gap-6 border-t border-[var(--header-line)] pt-10 sm:grid-cols-2 lg:grid-cols-4">
        {ASSURANCES.map(({ glyph: Glyph, title, caption }) => (
          <li key={title} className="flex flex-col items-center text-center">
            <Glyph />
            <p className="mt-3 text-[13px] font-semibold text-[var(--ink)]">{title}</p>
            <p className="mt-1 text-[12px] leading-relaxed text-[var(--ink-4)]">{caption}</p>
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
