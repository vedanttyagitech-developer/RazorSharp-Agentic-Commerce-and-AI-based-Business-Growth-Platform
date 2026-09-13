'use client';
import { useEffect, useState } from 'react';
import type { ProductCard } from '@/lib/commerce';
import { merchantCall } from './live-merchant';
import { MERCHANT_STATE_CHANGED } from '@/lib/merchant-sync';
type Page = {
  products: ProductCard[];
  next_cursor: string | null;
  matched: number;
};
export function MerchantCatalogue() {
  const [listed, setListed] = useState(''),
    [available, setAvailable] = useState(''),
    [category, setCategory] = useState(''),
    [filter, setFilter] = useState({ listed: '', available: '', category: '' }),
    [cursor, setCursor] = useState(''),
    [page, setPage] = useState<Page | null>(null),
    [error, setError] = useState(''),
    [revision, setRevision] = useState(0);
  useEffect(() => {
    const refresh = () => setRevision((n) => n + 1);
    window.addEventListener(MERCHANT_STATE_CHANGED, refresh);
    return () => window.removeEventListener(MERCHANT_STATE_CHANGED, refresh);
  }, []);
  useEffect(() => {
    let active = true;
    queueMicrotask(() => {if(active) {setPage(null); setError('');}});
    const query = new URLSearchParams({ limit: '20' });
    for (const [key, value] of Object.entries(filter))
      if (value) query.set(key, value);
    if (cursor) query.set('cursor', cursor);
    void merchantCall(`catalogue/products?${query}`)
      .then((data) => {
        if (active) setPage(data as unknown as Page);
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [filter, cursor, revision]);
  return (
    <section className="panel">
      <h3>Catalogue records</h3>
      <p>
        Find products to manage, then use the proposal form below to review a
        change.
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setCursor('');
          setFilter({ listed, available, category: category.trim() });
        }}
      >
        <label>
          Listing{' '}
          <select value={listed} onChange={(e) => setListed(e.target.value)}>
            <option value="">All</option>
            <option value="true">Listed</option>
            <option value="false">Unlisted</option>
          </select>
        </label>
        <label>
          Availability{' '}
          <select
            value={available}
            onChange={(e) => setAvailable(e.target.value)}
          >
            <option value="">All</option>
            <option value="true">Available</option>
            <option value="false">Unavailable</option>
          </select>
        </label>
        <label>
          Category{' '}
          <input
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            placeholder="Category slug, e.g. dairy"
          />
        </label>
        <button className="secondary">Apply catalogue filters</button>
      </form>
      {error && <p role="alert">{error}</p>}
      {!page && !error && <output>Reading catalogue…</output>}
      {page && (
        <>
          <p>{page.matched} matching products</p>
          {page.products.map((p) => (
            <article className="evidence-event" key={p.sku}>
              <h4>{p.display_name}</h4>
              <p>
                {p.sku} · {p.category} · {p.currency} {p.unit_price.display}
              </p>
              <p>
                {p.stock_units} units · {p.is_listed ? 'Listed' : 'Unlisted'} ·{' '}
                {p.is_available ? 'Available' : 'Unavailable'}
              </p>
            </article>
          ))}
          {page.next_cursor && (
            <button
              className="secondary"
              onClick={() => setCursor(page.next_cursor!)}
            >
              Next products
            </button>
          )}
        </>
      )}
      {cursor && (
        <button className="secondary" onClick={() => setCursor('')}>
          First products
        </button>
      )}
    </section>
  );
}
