"use client";

/**
 * The merchant's own view of the catalogue: every SKU, listed or not.
 *
 * Two rules run through this page.
 *
 * **Price is an integer count of paise and the browser never converts it.** The table
 * renders `unit_price` -- the server's own `MoneyOut` -- and the editor below takes paise
 * as an integer, because a rupee field would mean a multiplication here, and a price this
 * app computed could disagree with the price the kernel revalidates at admission.
 *
 * **Nothing mutates merchant state except `POST /v1/scenario/injections`.** There is no
 * local edit, no optimistic row, and no draft kept in the browser. The injection is
 * applied by the merchant simulator under its own lock and audited as `SCENARIO_INJECTION`
 * in the same transaction; this page renders the label and the revision that came back,
 * and then re-reads the catalogue. A revision that has moved past the one the visible page
 * was read at is stated rather than hidden, because that staleness is exactly what the
 * kernel refuses a checkout over.
 */
import { useCallback, useState } from "react";
import { api } from "@/lib/api/client";
import { formatCount, formatMinor, formatMoney } from "@/lib/money";
import { useRead } from "@/lib/useRead";
import { CATEGORIES, type Injection, type Product } from "@/lib/api/types";
import {
  Button,
  Chip,
  Empty,
  Loading,
  Panel,
  ProblemPanel,
  TableWrap,
  Td,
  Th,
  When,
  cx,
} from "@/components/ui";
import { FilterChip } from "@/components/ops/Filters";
import { Pager, useCursors } from "@/components/ops/Pager";

type Listing = "" | "listed" | "delisted";
type Stock = "" | "available" | "unavailable";

/** A whole count of the smallest unit, and nothing else. No sign, no point, no exponent. */
const DIGITS = /^\d+$/;

export default function CataloguePage() {
  const [category, setCategory] = useState<string>("");
  const [listing, setListing] = useState<Listing>("");
  const [stock, setStock] = useState<Stock>("");
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  // Injections applied from this browser session, newest first. Each entry is a response
  // the API returned, kept so the re-read that follows an injection does not carry away
  // the evidence of what the operator just did.
  const [applied, setApplied] = useState<Injection[]>([]);
  const [injectionError, setInjectionError] = useState<unknown>(null);
  const cursors = useCursors();

  const page = useRead(
    (signal) =>
      api.products({
        category: category || undefined,
        listed: listing === "" ? undefined : listing === "listed",
        available: stock === "" ? undefined : stock === "available",
        limit: 50,
        cursor: cursors.cursor,
        signal,
      }),
    [category, listing, stock, cursors.cursor],
  );

  const found = useRead(
    (signal) =>
      query
        ? api.search(query, { limit: 50, signal })
        : Promise.resolve(null),
    [query],
  );

  const reload = useCallback(() => {
    page.reload();
    found.reload();
  }, [page, found]);

  const record = useCallback(
    (injection: Injection) => {
      setInjectionError(null);
      setApplied((current) => [injection, ...current]);
      // Merchant state has moved, so the visible rows are stale by exactly one revision.
      page.reload();
      found.reload();
    },
    [page, found],
  );

  function refilter(apply: () => void) {
    apply();
    cursors.reset();
  }

  // Searching answers from the ranked index; browsing answers from the paged list. They
  // are different questions and the page says which one it is answering.
  const searching = query !== "";
  const rows: Product[] = searching ? (found.data?.hits ?? []) : (page.data?.products ?? []);
  const revision = page.data?.revision ?? null;

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-[18px] font-semibold tracking-tight text-[var(--ink)]">Catalogue</h1>
          <p className="mt-0.5 text-[12.5px] text-[var(--muted)]">
            GET /v1/catalogue/products — the merchant&rsquo;s view, delisted rows included.
          </p>
        </div>
        {page.data && (
          <div className="flex flex-wrap items-center gap-2">
            <Chip tone="muted">revision {page.data.revision}</Chip>
            <Chip tone="muted">{formatCount(page.data.matched)} matched</Chip>
          </div>
        )}
      </header>

      <Panel title="Filters" subtitle="category and listing state are applied by the API, not here">
        <div className="space-y-3 p-4">
          <form
            role="search"
            onSubmit={(event) => {
              event.preventDefault();
              setQuery(draft.trim());
            }}
            className="flex flex-wrap items-center gap-2"
          >
            <label className="flex-1 min-w-[220px]">
              <span className="sr-only">Search the merchant&rsquo;s catalogue</span>
              <input
                type="search"
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                placeholder="Search the index — English, Hindi or Hinglish"
                className="w-full rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--surface)] px-2.5 py-1.5 text-[12.5px] text-[var(--ink)] placeholder:text-[var(--faint)]"
              />
            </label>
            <Button type="submit" variant="primary">
              Search
            </Button>
            {searching && (
              <Button
                onClick={() => {
                  setDraft("");
                  setQuery("");
                }}
              >
                Clear
              </Button>
            )}
          </form>

          <div className="flex flex-wrap items-center gap-1.5">
            <FilterChip
              active={category === ""}
              onClick={() => refilter(() => setCategory(""))}
              label="all categories"
              count={page.data?.matched}
            />
            {CATEGORIES.map((slug) => (
              <FilterChip
                key={slug}
                active={category === slug}
                onClick={() => refilter(() => setCategory(slug))}
                label={slug}
                count={page.data?.counts_by_category[slug]}
              />
            ))}
          </div>

          <div className="flex flex-wrap items-center gap-1.5">
            <FilterChip active={listing === ""} onClick={() => refilter(() => setListing(""))} label="listed: any" />
            <FilterChip active={listing === "listed"} onClick={() => refilter(() => setListing("listed"))} label="listed" tone="positive" />
            <FilterChip active={listing === "delisted"} onClick={() => refilter(() => setListing("delisted"))} label="delisted" tone="danger" />
            <span aria-hidden="true" className="mx-1 h-4 w-px bg-[var(--line)]" />
            <FilterChip active={stock === ""} onClick={() => refilter(() => setStock(""))} label="stock: any" />
            <FilterChip active={stock === "available"} onClick={() => refilter(() => setStock("available"))} label="sellable" tone="positive" />
            <FilterChip active={stock === "unavailable"} onClick={() => refilter(() => setStock("unavailable"))} label="not sellable" tone="warn" />
          </div>

          {searching && (
            <p className="text-[11.5px] text-[var(--warn)]" aria-live="polite">
              Showing ranked hits from GET /v1/catalogue/search. Category and listing filters apply to
              the paged list, not to this search — clear the search to page the full catalogue.
            </p>
          )}
        </div>
      </Panel>

      {injectionError != null && (
        <ProblemPanel
          error={injectionError}
          what="the injection — merchant state was not changed"
        />
      )}

      {applied.length > 0 && (
        <Panel
          title="Injections applied from this console"
          subtitle="responses returned by POST /v1/scenario/injections during this browser session"
          actions={
            <Button onClick={() => setApplied([])} variant="ghost">
              Clear this list
            </Button>
          }
        >
          <ul className="divide-y divide-[var(--line-soft)]">
            {applied.map((injection) => (
              <li key={injection.injection_id} className="px-4 py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <Chip tone="warn">{injection.label}</Chip>
                  <Chip tone="muted">{injection.kind}</Chip>
                  {injection.sku && <span className="mono text-[var(--ink)]">{injection.sku}</span>}
                  <span className="mono text-[var(--muted)]">
                    revision {injection.revision_before} → {injection.revision_after}
                  </span>
                  <When value={injection.injected_at} />
                </div>
                <ul className="mono mt-1.5 space-y-0.5">
                  {injection.deltas.map((delta) => (
                    <li key={delta.field} className="text-[var(--ink)]">
                      {delta.field}: <span className="text-[var(--muted)]">{String(delta.before)}</span>{" "}
                      → <span className="text-[var(--positive)]">{String(delta.after)}</span>
                    </li>
                  ))}
                </ul>
                {injection.note && (
                  <p className="mt-1 text-[11.5px] text-[var(--muted)] break-id">{injection.note}</p>
                )}
                <div className="mono mt-1 text-[var(--faint)] break-id">
                  audit event {injection.audit_event_id} · scenario run {injection.scenario_run_id}
                </div>
              </li>
            ))}
          </ul>
        </Panel>
      )}

      <Panel
        title={searching ? `Search: ${query}` : "Products"}
        subtitle={
          searching
            ? "GET /v1/catalogue/search — ranked, with the terms that matched"
            : "GET /v1/catalogue/products?category=&listed=&available=&limit=&cursor="
        }
        actions={<Button onClick={reload}>Re-read</Button>}
      >
        {(searching ? found.loading : page.loading) && <Loading label="Reading the catalogue" />}
        {searching && found.error != null && (
          <div className="p-4">
            <ProblemPanel error={found.error} what="the catalogue search" onRetry={found.reload} />
          </div>
        )}
        {!searching && page.error != null && (
          <div className="p-4">
            <ProblemPanel error={page.error} what="the catalogue page" onRetry={page.reload} />
          </div>
        )}
        {rows.length === 0 && !page.loading && !found.loading && page.error == null && found.error == null && (
          <Empty>
            {searching
              ? "The index returned no hit for that query. A SKU that cannot be found here cannot be found by the storefront either."
              : "No product matches this filter."}
          </Empty>
        )}

        {rows.length > 0 && (
          <TableWrap>
            <table className="w-full border-collapse text-[12px]">
              <thead>
                <tr>
                  <Th>SKU</Th>
                  <Th>Product</Th>
                  <Th>Category</Th>
                  <Th align="right">Unit price</Th>
                  <Th align="right">Stock</Th>
                  <Th>Listing</Th>
                  <Th>Read at</Th>
                  <Th>Change</Th>
                </tr>
              </thead>
              <tbody>
                {rows.map((product) => (
                  <ProductRow
                    key={product.sku}
                    product={product}
                    open={editing === product.sku}
                    onToggle={() => setEditing(editing === product.sku ? null : product.sku)}
                    onApplied={record}
                    onRefused={setInjectionError}
                    pageRevision={revision}
                  />
                ))}
              </tbody>
            </table>
          </TableWrap>
        )}

        {!searching && page.data && (
          <Pager
            cursors={cursors}
            nextCursor={page.data.next_cursor}
            shown={page.data.products.length}
            matched={page.data.matched}
            noun="products"
          />
        )}
        {searching && found.data && (
          <p className="border-t border-[var(--line)] px-4 py-2.5 text-[11.5px] text-[var(--faint)]">
            <span className="num text-[var(--ink)]">{found.data.hits.length}</span> hits for{" "}
            <span className="mono text-[var(--muted)]">{found.data.normalized_query}</span> · index{" "}
            <span className="mono text-[var(--muted)]">{found.data.freshness.source}</span> at revision{" "}
            <span className="num text-[var(--ink)]">{found.data.freshness.catalogue_revision}</span>
          </p>
        )}
      </Panel>
    </div>
  );
}

/** One catalogue row, with its injection editor folded underneath it. */
function ProductRow({
  product,
  open,
  onToggle,
  onApplied,
  onRefused,
  pageRevision,
}: {
  product: Product;
  open: boolean;
  onToggle: () => void;
  onApplied: (injection: Injection) => void;
  onRefused: (error: unknown) => void;
  pageRevision: number | null;
}) {
  const stale = pageRevision !== null && product.freshness.catalogue_revision !== pageRevision;
  return (
    <>
      <tr className={cx("hover:bg-[var(--raised)]", open && "bg-[var(--raised)]")}>
        <Td className="mono text-[var(--ink)]">{product.sku}</Td>
        <Td>
          <div className="text-[var(--ink)]">{product.display_name}</div>
          <div className="mono mt-0.5 text-[var(--faint)]">
            {product.unit_label} · tax {product.tax_bp} bp
          </div>
        </Td>
        <Td className="mono text-[var(--muted)]">{product.category}</Td>
        <Td align="right">
          <span className="num text-[var(--ink)]">{formatMoney(product.unit_price)}</span>
          <div className="mono mt-0.5 text-[var(--faint)]">{product.unit_price_minor} paise</div>
        </Td>
        <Td align="right">
          <span
            className={cx(
              "num",
              product.stock_units === 0 ? "text-[var(--danger)]" : "text-[var(--ink)]",
            )}
          >
            {formatCount(product.stock_units)}
          </span>
        </Td>
        <Td>
          <div className="flex flex-wrap gap-1">
            <Chip tone={product.is_listed ? "positive" : "danger"}>
              {product.is_listed ? "listed" : "delisted"}
            </Chip>
            <Chip tone={product.is_available ? "positive" : "warn"}>
              {product.is_available ? "sellable" : "not sellable"}
            </Chip>
          </div>
        </Td>
        <Td>
          <When value={product.freshness.observed_at} />
          <div className="mono mt-0.5 text-[var(--faint)]">
            rev {product.freshness.catalogue_revision}
            {stale && <span className="ml-1 text-[var(--warn)]">· page is rev {pageRevision}</span>}
          </div>
        </Td>
        <Td>
          <Button onClick={onToggle} variant={open ? "primary" : "default"}>
            {open ? "Close" : "Inject"}
          </Button>
        </Td>
      </tr>
      {open && (
        <tr>
          <td colSpan={8} className="border-b border-[var(--line)] bg-[var(--bg)] p-0">
            <InjectionEditor product={product} onApplied={onApplied} onRefused={onRefused} />
          </td>
        </tr>
      )}
    </>
  );
}

/**
 * The only way this console changes merchant state.
 *
 * Three levers, each one injection: price in paise, stock in units, and the listing flag.
 * The response is rendered whole -- label, deltas, and the revision the change produced --
 * because the operator's next question is always "did that land, and what is the catalogue
 * revision now", and a green tick answers neither.
 */
function InjectionEditor({
  product,
  onApplied,
  onRefused,
}: {
  product: Product;
  onApplied: (injection: Injection) => void;
  onRefused: (error: unknown) => void;
}) {
  const [paise, setPaise] = useState(String(product.unit_price_minor));
  const [units, setUnits] = useState(String(product.stock_units));
  const [note, setNote] = useState("");
  const [pending, setPending] = useState(false);

  // The outcome is reported upward rather than kept here, because applying an injection
  // re-reads the catalogue and that re-read unmounts this editor with the table under it.
  // An operator who lost the response they had just been shown would have to go and look
  // up what their own change did.
  async function inject(body: Parameters<typeof api.inject>[0]) {
    setPending(true);
    try {
      onApplied(await api.inject(body));
    } catch (cause) {
      onRefused(cause);
    } finally {
      setPending(false);
    }
  }

  // Validated as typed, not as parsed. `Number.parseInt("315.50", 10)` is 315, and an
  // integrality check on that answer can never fail, because the truncation has already
  // happened -- so an operator typing the rupee figure out of habit would set ₹3.15 as the
  // live price with the button still enabled and nothing on screen saying anything was
  // dropped. A price is a run of digits or it is not a price.
  const paiseEntry = paise.trim();
  const unitsEntry = units.trim();
  const paiseValid = DIGITS.test(paiseEntry) && Number.isSafeInteger(Number(paiseEntry)) && Number(paiseEntry) > 0;
  const unitsValid = DIGITS.test(unitsEntry) && Number.isSafeInteger(Number(unitsEntry));
  const paiseValue = Number(paiseEntry);
  const unitsValue = Number(unitsEntry);

  return (
    <div className="space-y-3 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Chip tone="warn">SCENARIO INJECTION</Chip>
        <span className="text-[11.5px] text-[var(--muted)]">
          POST /v1/scenario/injections — applied by the merchant simulator and audited in the same
          transaction. Nothing is changed in this browser.
        </span>
      </div>

      <div className="grid gap-3 md:grid-cols-3">
        <div className="rounded-[var(--r-md)] border border-[var(--line)] bg-[var(--surface)] p-3">
          <span className="eyebrow">Price · PRICE_SET</span>
          <label className="mt-1.5 block">
            <span className="sr-only">Unit price in paise for {product.sku}</span>
            <input
              inputMode="numeric"
              pattern="[0-9]*"
              autoComplete="off"
              aria-invalid={paiseValid ? undefined : true}
              aria-describedby={`price-echo-${product.sku}`}
              value={paise}
              onChange={(event) => setPaise(event.target.value)}
              className={cx(
                "num w-full rounded-[var(--r-sm)] border bg-[var(--bg)] px-2 py-1.5 text-[var(--ink)]",
                paiseValid ? "border-[var(--line)]" : "border-[var(--danger)]",
              )}
            />
          </label>
          <p className="mt-1 text-[11px] text-[var(--faint)]">
            Integer paise, as the API stores it. Currently{" "}
            <span className="num text-[var(--muted)]">{product.unit_price_minor}</span> ={" "}
            {formatMoney(product.unit_price)}.
          </p>
          {/*
            The entry read back as money before it is sent. `315.50` typed into a paise
            field is a different price from the one the operator meant, and the only place
            that is obvious is beside the button, in the same notation the row above uses.
          */}
          <p
            id={`price-echo-${product.sku}`}
            aria-live="polite"
            className={cx("mt-1 text-[11.5px]", paiseValid ? "text-[var(--ink)]" : "text-[var(--danger)]")}
          >
            {paiseValid
              ? `Will set ${formatMinor(paiseValue, product.unit_price.currency)}`
              : "Digits only, and more than zero. A decimal point is not a paise figure."}
          </p>
          <div className="mt-2">
            <Button
              variant="primary"
              disabled={pending || !paiseValid}
              onClick={() => inject({ kind: "PRICE_SET", sku: product.sku, value: paiseValue, note })}
            >
              Set price
            </Button>
          </div>
        </div>

        <div className="rounded-[var(--r-md)] border border-[var(--line)] bg-[var(--surface)] p-3">
          <span className="eyebrow">Stock · STOCK_SET</span>
          <label className="mt-1.5 block">
            <span className="sr-only">Stock units for {product.sku}</span>
            <input
              inputMode="numeric"
              pattern="[0-9]*"
              autoComplete="off"
              aria-invalid={unitsValid ? undefined : true}
              aria-describedby={`stock-echo-${product.sku}`}
              value={units}
              onChange={(event) => setUnits(event.target.value)}
              className={cx(
                "num w-full rounded-[var(--r-sm)] border bg-[var(--bg)] px-2 py-1.5 text-[var(--ink)]",
                unitsValid ? "border-[var(--line)]" : "border-[var(--danger)]",
              )}
            />
          </label>
          <p className="mt-1 text-[11px] text-[var(--faint)]">
            Currently <span className="num text-[var(--muted)]">{product.stock_units}</span> units.
          </p>
          <p
            id={`stock-echo-${product.sku}`}
            aria-live="polite"
            className={cx("mt-1 text-[11.5px]", unitsValid ? "text-[var(--ink)]" : "text-[var(--danger)]")}
          >
            {unitsValid
              ? `Will set ${formatCount(unitsValue)} units`
              : "Whole units only. Zero is allowed; a fraction is not."}
          </p>
          <div className="mt-2 flex flex-wrap gap-2">
            <Button
              variant="primary"
              disabled={pending || !unitsValid}
              onClick={() => inject({ kind: "STOCK_SET", sku: product.sku, value: unitsValue, note })}
            >
              Set stock
            </Button>
            <Button
              variant="danger"
              disabled={pending}
              onClick={() => inject({ kind: "SELL_OUT", sku: product.sku, note })}
              title="Take the last unit. The audit records the STOCK_SET the simulator performed."
            >
              Sell out
            </Button>
          </div>
        </div>

        <div className="rounded-[var(--r-md)] border border-[var(--line)] bg-[var(--surface)] p-3">
          <span className="eyebrow">Listing · AVAILABILITY_SET</span>
          <p className="mt-1.5 text-[11.5px] text-[var(--muted)]">
            Sold out and delisted lead to different conversations, so they are different fields.
            Currently {product.is_listed ? "listed" : "delisted"}.
          </p>
          <div className="mt-2">
            <Button
              variant={product.is_listed ? "danger" : "primary"}
              disabled={pending}
              onClick={() =>
                inject({ kind: "AVAILABILITY_SET", sku: product.sku, value: !product.is_listed, note })
              }
            >
              {product.is_listed ? "Delist" : "List"}
            </Button>
          </div>
        </div>
      </div>

      <label className="block">
        <span className="eyebrow">Note recorded on the injection</span>
        <input
          value={note}
          onChange={(event) => setNote(event.target.value)}
          maxLength={200}
          placeholder="why this change is being made"
          className="mt-1 w-full rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--surface)] px-2 py-1.5 text-[12.5px] text-[var(--ink)] placeholder:text-[var(--faint)]"
        />
      </label>

      {pending && <Loading label="Applying the injection" />}
    </div>
  );
}
