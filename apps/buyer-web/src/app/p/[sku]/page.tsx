/**
 * One product, and where its price came from.
 *
 * There is no "fetch me this SKU" endpoint: the catalogue is served as pages. So this
 * screen reads the aisle the SKU's own code names -- SKUs are `<PRODUCT>-<CATEGORY>-<NNN>`
 * -- and follows the server's cursor until it finds the row or runs out of pages. That is
 * slower than a point read and entirely honest: the product shown is a row the catalogue
 * actually returned, at a revision the page can name, rather than a shape assembled in
 * the browser from a URL segment.
 *
 * The note at the bottom is the part that matters. A price here is a quotation from the
 * merchant's catalogue at a stated revision, not a promise: the total a buyer approves is
 * re-quoted at checkout and hashed into the approval, and if the catalogue moves in
 * between, the kernel refuses the stale approval rather than charging the old number.
 */
"use client";

/*
 * Plain <img>: local `.webp` files under a CSP that allows no external image host.
 */
/* eslint-disable @next/next/no-img-element */

import Link from "next/link";
import { use, useCallback, useEffect, useMemo, useState } from "react";

import { Amount, EmptyState, ErrorState, Skeleton, cx } from "@/components/ui";
import { api } from "@/lib/api/client";
import { ApiError, humanMessage } from "@/lib/api/problem";
import type { CataloguePage, Product } from "@/lib/api/types";
import { categoryForSku, categoryLabel, imagesFor } from "@/lib/product-images";
import { useBasket } from "@/features/basket/use-basket";
import { QuantityStepper } from "@/features/storefront/quantity-stepper";

/** Enough pages to cross the largest aisle several times over; a guard, not a budget. */
const MAX_PAGES = 10;

type Detail =
  | { key: string; status: "loading" }
  | { key: string; status: "ready"; product: Product }
  | { key: string; status: "missing" }
  | { key: string; status: "error"; problem: string };

async function findProduct(sku: string, signal: AbortSignal): Promise<Product | null> {
  const category = categoryForSku(sku);
  let cursor: string | null = null;

  for (let page = 0; page < MAX_PAGES; page += 1) {
    const result: CataloguePage = await api.products({
      category: category ?? undefined,
      limit: 100,
      cursor: cursor ?? undefined,
      signal,
    });
    const found = result.products.find((candidate) => candidate.sku === sku);
    if (found) return found;
    if (!result.next_cursor) return null;
    cursor = result.next_cursor;
  }
  return null;
}

export default function ProductPage({ params }: { params: Promise<{ sku: string }> }) {
  const { sku: raw } = use(params);
  const sku = decodeURIComponent(raw);

  const [attempt, setAttempt] = useState(0);
  const key = `${attempt} ${sku}`;
  const [state, setState] = useState<Detail>({ key, status: "loading" });

  const basket = useBasket();
  const images = useMemo(() => imagesFor(sku), [sku]);

  useEffect(() => {
    const controller = new AbortController();

    findProduct(sku, controller.signal)
      .then((found) => {
        if (controller.signal.aborted) return;
        setState(found ? { key, status: "ready", product: found } : { key, status: "missing" });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && (error.status === 404 || error.status === 422)) {
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
  }, [key, sku]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  // A result held under another SKU belongs to the product page the buyer just left.
  const current: Detail = state.key === key ? state : { key, status: "loading" };

  if (current.status === "loading") return <ProductSkeleton />;

  if (current.status === "missing") {
    return (
      <main className="column py-10">
        <EmptyState
          title="No product with that code"
          detail={`The merchant's catalogue has nothing under ${sku}. It may have been delisted, or the link may be mistyped.`}
          action={
            <Link
              href="/"
              className="inline-flex items-center rounded-full bg-[var(--green)] px-4 py-2 text-[13px] font-semibold text-white"
            >
              Back to the shop
            </Link>
          }
        />
      </main>
    );
  }

  if (current.status === "error") {
    return (
      <main className="column py-10">
        <ErrorState title="The catalogue did not answer" detail={current.problem} onRetry={retry} />
      </main>
    );
  }

  const product = current.product;
  const quantity = basket.quantities[product.sku] ?? 0;
  const unavailable = !product.is_available;
  const status = product.is_listed ? "Out of stock" : "Not available";
  const busy = basket.busySku === product.sku;
  const category = categoryForSku(product.sku) ?? product.category;

  return (
    <main className="column py-6">
      <nav aria-label="Breadcrumb" className="mb-5 text-[12px] text-[var(--ink-4)]">
        <Link href="/" className="hover:text-[var(--ink-2)]">
          Home
        </Link>
        <span aria-hidden="true"> / </span>
        <Link href={`/c/${category}`} className="hover:text-[var(--ink-2)]">
          {categoryLabel(category)}
        </Link>
      </nav>

      <div className="grid gap-8 lg:grid-cols-[minmax(0,420px)_minmax(0,1fr)]">
        {/* Keyed by SKU so the selected view resets by remounting, not by an effect. */}
        <Gallery key={product.sku} images={images} name={product.display_name} />

        <div className="max-w-2xl">
          <h1 className="text-[20px] leading-[1.3] font-bold text-[var(--ink)]">{product.display_name}</h1>
          {product.name_hi && product.name_hi !== product.display_name ? (
            <p className="mt-1 text-[14px] text-[var(--ink-3)]" lang="hi">
              {product.name_hi}
            </p>
          ) : null}
          <p className="mt-2 text-[13px] font-medium text-[var(--ink-unit)]">{product.unit_label}</p>

          <div className="mt-6 flex flex-wrap items-center gap-6">
            <div>
              <Amount money={product.unit_price} className="text-[28px] font-extrabold text-[var(--ink)]" />
              <p className="mt-1 text-[12px] text-[var(--ink-4)]">
                Inclusive of tax at {formatRate(product.tax_bp)}% ({product.tax_bp} basis points). The tax on your
                basket is worked out by the merchant&rsquo;s quote engine, never by this page.
              </p>
            </div>

            {unavailable ? (
              <p className="text-[14px] font-semibold text-[var(--ink-5)]">{status}</p>
            ) : quantity > 0 ? (
              <QuantityStepper
                quantity={quantity}
                busy={busy}
                size="lg"
                itemLabel={product.display_name}
                onChange={(next) => basket.setQuantity(product.sku, next)}
              />
            ) : (
              <button
                type="button"
                onClick={() => basket.add(product.sku)}
                disabled={busy}
                aria-busy={busy || undefined}
                aria-label={`Add ${product.display_name} to basket`}
                className="inline-flex h-[44px] w-[168px] items-center justify-center rounded-[var(--r-sm)] border border-[var(--green-add)] bg-[var(--green-add-bg)] text-[14px] font-semibold text-[var(--green-add)] transition disabled:cursor-not-allowed disabled:opacity-50"
              >
                ADD TO BASKET
              </button>
            )}
          </div>

          <p className="mt-4 text-[13px]" aria-live="polite">
            {unavailable ? (
              <span className="text-[var(--ink-3)]">
                {product.is_listed
                  ? "The merchant stocks this but has none at the moment. It can be added again once the catalogue shows units."
                  : "The merchant has taken this off the catalogue. It cannot be added to a basket."}
              </span>
            ) : (
              <span className="text-[var(--green)]">
                In stock &middot; <span className="tnum">{product.stock_units}</span>{" "}
                {product.stock_units === 1 ? "unit" : "units"} at the merchant
              </span>
            )}
          </p>

          <PriceProvenance product={product} />
        </div>
      </div>
    </main>
  );
}

/** Basis points to a percentage string. A rate, not an amount: no money is derived here. */
function formatRate(basisPoints: number): string {
  const percent = basisPoints / 100;
  return Number.isInteger(percent) ? String(percent) : percent.toFixed(2);
}

function Gallery({ images, name }: { images: string[]; name: string }) {
  const [index, setIndex] = useState(0);
  const position = Math.min(index, images.length - 1);

  return (
    <div>
      <div className="overflow-hidden rounded-[var(--r-lg)] border-[0.5px] border-[var(--card-line)] bg-white">
        <img
          src={images[position]}
          alt={images.length > 1 ? `${name}, view ${position + 1} of ${images.length}` : name}
          width={420}
          height={420}
          decoding="async"
          className="aspect-square w-full object-contain"
        />
      </div>
      {images.length > 1 ? (
        <ul className="mt-3 flex flex-wrap gap-2" aria-label={`${images.length} views of ${name}`}>
          {images.map((image, at) => (
            <li key={image}>
              <button
                type="button"
                onClick={() => setIndex(at)}
                aria-label={`Show view ${at + 1} of ${images.length}`}
                aria-pressed={at === position}
                className={cx(
                  "block h-16 w-16 overflow-hidden rounded-[var(--r-md)] border bg-white p-1 transition",
                  at === position
                    ? "border-[var(--green-add)]"
                    : "border-[var(--card-line)] hover:border-[var(--ink-5)]",
                )}
              >
                <img
                  src={image}
                  alt=""
                  width={64}
                  height={64}
                  loading="lazy"
                  decoding="async"
                  className="h-full w-full object-contain"
                />
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/**
 * The provenance note. Every field in it is the server's: the revision the row was read
 * at, the source that served it and the moment it was observed.
 */
function PriceProvenance({ product }: { product: Product }) {
  const observedAt = formatObservedAt(product.freshness.observed_at);
  return (
    <section
      aria-labelledby="price-provenance"
      className="mt-8 rounded-[var(--r-lg)] border-[0.5px] border-[var(--card-line)] bg-[var(--tint-3)] p-4"
    >
      <h2 id="price-provenance" className="text-[14px] font-bold text-[var(--ink)]">
        Why you&rsquo;re seeing this price
      </h2>
      <p className="mt-2 text-[13px] leading-[1.6] text-[var(--ink-3)]">
        It is the merchant&rsquo;s own listed price, read from their live catalogue at revision{" "}
        <span className="tnum font-semibold text-[var(--ink)]">{product.freshness.catalogue_revision}</span> via{" "}
        <span className="font-semibold text-[var(--ink)]">{product.freshness.source}</span>
        {observedAt ? <> on {observedAt}</> : null}. Nothing on this page adds to it or rounds it.
      </p>
      <p className="mt-2 text-[13px] leading-[1.6] text-[var(--ink-3)]">
        Your basket is priced again when you check out, and the total you approve is hashed into the approval. If the
        catalogue moves between your approval and payment, the transaction kernel refuses the stale approval and shows
        you what changed rather than charging the old number.
      </p>
      <p className="mt-3 text-[12px] text-[var(--ink-4)]">
        SKU <span className="tnum">{product.sku}</span>
      </p>
    </section>
  );
}

/** The server's timestamp, rendered in the reader's locale. Empty when it is unparseable. */
function formatObservedAt(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  return at.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function ProductSkeleton() {
  return (
    <main className="column py-6">
      <Skeleton className="h-4 w-40" />
      <div className="mt-6 grid gap-8 lg:grid-cols-[minmax(0,420px)_minmax(0,1fr)]">
        <Skeleton className="aspect-square w-full max-w-[420px]" />
        <div className="flex flex-col gap-3">
          <Skeleton className="h-7 w-3/4" />
          <Skeleton className="h-4 w-24" />
          <Skeleton className="mt-4 h-10 w-40" />
          <Skeleton className="mt-6 h-32 w-full" />
        </div>
      </div>
    </main>
  );
}
