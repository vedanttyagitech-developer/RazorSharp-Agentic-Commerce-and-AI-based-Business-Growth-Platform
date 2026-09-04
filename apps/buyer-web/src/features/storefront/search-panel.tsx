"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";

import { AvailabilityBadge, FreshnessLine } from "@/components/availability";
import { Alert, Button, StatusPill } from "@/components/ui";
import { useClient } from "@/components/providers";
import type { SearchResponse } from "@/lib/api/types";
import { JOURNEY_META } from "@/lib/journey";
import { formatMinor } from "@/lib/money";

import { useBasketActions } from "./use-basket-actions";

const SUGGESTIONS = ["milk", "doodh", "दूध", "atta", "dal", "eggs", "paneer", "chocos", "snacks", "chai"];
const LOCALES = [
  { value: "en-IN", label: "English" },
  { value: "hi-IN", label: "हिन्दी" },
  { value: "hi-Latn-IN", label: "Hinglish" },
];

/**
 * Grounded discovery (spec 6.2, 8.2). Every result shows where it came from and how
 * fresh it is, and reports listing and stock as separate facts.
 */
export function SearchPanel() {
  const client = useClient();
  const { addOne, busySku, error: basketError, lastBasket } = useBasketActions();
  const [query, setQuery] = useState("");
  const [locale, setLocale] = useState("en-IN");
  const [phase, setPhase] = useState<"idle" | "searching" | "checked">("idle");
  const [results, setResults] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run(term: string) {
    const trimmed = term.trim();
    if (!trimmed) return;
    setPhase("searching");
    setError(null);
    try {
      const response = await client.search({ q: trimmed, locale });
      setResults(response);
      setPhase("checked");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Search failed");
      setPhase("idle");
    }
  }

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void run(query);
  }

  const journey = phase === "searching" ? JOURNEY_META.SEARCHING : phase === "checked" ? JOURNEY_META.AVAILABILITY_CHECKED : null;

  return (
    <section aria-labelledby="search-heading" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 id="search-heading" className="text-2xl font-semibold">Grounded product discovery</h1>
        <div role="status" aria-live="polite">
          {journey ? <StatusPill tone={journey.tone} glyph={journey.glyph} label={journey.label} /> : <span className="text-sm text-muted">Search in English, Hindi or Hinglish</span>}
        </div>
      </div>

      <form onSubmit={onSubmit} className="flex flex-wrap items-end gap-2" role="search">
        <label className="flex-1 text-sm">
          <span className="mb-1 block font-medium">Search the catalogue</span>
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="e.g. doodh, दूध, atta, eggs"
            className="w-full rounded-md border border-line bg-surface px-3 py-2"
            autoComplete="off"
          />
        </label>
        <label className="text-sm">
          <span className="mb-1 block font-medium">Display language</span>
          <select value={locale} onChange={(event) => setLocale(event.target.value)} className="rounded-md border border-line bg-surface px-3 py-2">
            {LOCALES.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
        </label>
        <Button type="submit" busy={phase === "searching"}>Search</Button>
      </form>

      <div className="flex flex-wrap gap-2" aria-label="Suggested searches">
        {SUGGESTIONS.map((term) => (
          <button key={term} type="button" onClick={() => { setQuery(term); void run(term); }} className="rounded-full border border-line px-3 py-1 text-sm hover:bg-stone-100 dark:hover:bg-stone-800">
            {term}
          </button>
        ))}
      </div>

      {error ? <Alert tone="danger" title="Search failed" role="alert">{error}</Alert> : null}
      {basketError ? <Alert tone="danger" title="Basket update failed" role="alert">{basketError}</Alert> : null}
      {lastBasket ? (
        <p role="status" className="text-sm">
          Basket <span className="font-mono text-xs">{lastBasket.basket_id}</span> now has {lastBasket.lines.length} line{lastBasket.lines.length === 1 ? "" : "s"}
          {lastBasket.quote ? <> · total {formatMinor(lastBasket.quote.total_minor, lastBasket.quote.currency)}</> : null}. <Link href="/basket" className="underline">View basket</Link>
        </p>
      ) : null}

      {results ? (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
            <p>
              {results.hits.length} result{results.hits.length === 1 ? "" : "s"} for <q>{results.query}</q>
              {results.normalized_query !== results.query ? <span className="text-muted"> (normalized: <span className="font-mono">{results.normalized_query}</span>)</span> : null}
            </p>
            <FreshnessLine freshness={results.freshness} />
          </div>
          {results.hits.length === 0 ? (
            <p className="text-sm text-muted">Nothing in the catalogue matches. Only products the merchant actually returned can be shown; nothing is invented.</p>
          ) : (
            <ul className="grid gap-3 sm:grid-cols-2">
              {results.hits.map((hit) => (
                <li key={hit.sku} className="flex flex-col gap-2 rounded-lg border border-line bg-surface p-3">
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <Link href={`/products/${encodeURIComponent(hit.sku)}`} className="font-medium underline-offset-4 hover:underline">{hit.display_name}</Link>
                      <p className="text-xs text-muted">{hit.display_name === hit.name_en ? hit.name_hi : hit.name_en} · {hit.unit_label} · {hit.category}</p>
                    </div>
                    <strong className="tabular-nums">{formatMinor(hit.unit_price_minor, hit.currency)}</strong>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <AvailabilityBadge product={hit} />
                    <span className="text-xs text-muted">matched: {hit.matched_terms.join(", ") || "—"} · score {hit.score}</span>
                  </div>
                  <FreshnessLine freshness={hit.freshness} />
                  <div>
                    <Button variant="secondary" onClick={() => void addOne(hit.sku)} disabled={!hit.is_available} busy={busySku === hit.sku} aria-label={`Add ${hit.display_name} to basket`}>
                      {hit.is_available ? "Add to basket" : hit.is_listed ? "Sold out" : "Not available"}
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </section>
  );
}
