'use client';
import { useEffect, useState } from 'react';
import { commerce, type ProductCard } from '@/lib/commerce';
export function ProductFacts({ sku }: { sku: string }) {
  const [data, setData] = useState<ProductCard | null>(null),
    [error, setError] = useState('');
  useEffect(() => {
    const abort = new AbortController();
    commerce.catalogue
      .product(sku, abort.signal)
      .then(setData)
      .catch((e) => {
        if (e.name !== 'AbortError') setError(e.message);
      });
    return () => abort.abort();
  }, [sku]);
  return (
    <section>
      {error ? (
        <p role="alert">{error}</p>
      ) : !data ? (
        <output>Checking current product details…</output>
      ) : (
        <dl className="record-fields">
          <div>
            <dt>Current price</dt>
            <dd>
              {data.currency} {data.unit_price.display}
            </dd>
          </div>
          <div>
            <dt>Availability</dt>
            <dd>
              {!data.is_listed
                ? 'Unlisted'
                : data.is_available
                  ? `${data.stock_units} units available`
                  : 'Unavailable'}
            </dd>
          </div>
          <div>
            <dt>Delivery promise</dt>
            <dd>
              {data.delivery_promise_days === 0
                ? 'Same day'
                : `${data.delivery_promise_days} days`}
            </dd>
          </div>
          <div>
            <dt>Product identifier</dt>
            <dd>{data.sku}</dd>
          </div>
        </dl>
      )}
      <p>
        Merchant catalogue facts. Your cart is quoted again before approval.
      </p>
    </section>
  );
}
