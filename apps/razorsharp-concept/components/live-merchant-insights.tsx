'use client';
import { useEffect, useState } from 'react';
import { LiveMerchant, merchantCall } from './live-merchant';
type Snapshot = {
  days: number;
  observed_at: string;
  since: string;
  totals: { currency: string; orders: number; sales_minor: number }[];
  definition: string;
};
export function LiveMerchantInsights({
  refreshKey = 0,
}: {
  refreshKey?: number;
}) {
  const [days, setDays] = useState(7),
    [data, setData] = useState<Snapshot | null>(null),
    [error, setError] = useState(''),
    [revision, setRevision] = useState(0);
  useEffect(() => {
    let active = true;
    merchantCall(`merchant/insights?days=${days}`)
      .then((value) => {
        if (active) {
          setData(value as unknown as Snapshot);
          setError('');
        }
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [days, revision, refreshKey]);
  return (
    <>
      <section className="panel">
        <span className="eyebrow">BUSINESS & GROWTH · RECORDED FACTS</span>
        <h1>Your business, from the source.</h1>
        <p>
          Confirmed sales from this merchant’s backend records. No estimated
          uplift or invented funnel counts.
        </p>
        <label className="form-field">
          Period
          <select
            value={days}
            onChange={(e) => {
              setDays(Number(e.target.value));
              setData(null);
            }}
          >
            <option value={7}>Last 7 days</option>
            <option value={30}>Last 30 days</option>
            <option value={90}>Last 90 days</option>
          </select>
        </label>
        <button className="secondary" onClick={() => setRevision((v) => v + 1)}>
          Refresh metrics
        </button>
        {error && <p role="alert">{error}</p>}
        {data && !error && (
          <>
            <div className="metric-grid">
              {data.totals.map((row) => (
                <section className="panel" key={row.currency}>
                  <span>Confirmed order value · {row.currency}</span>
                  <h2>
                    {new Intl.NumberFormat('en-IN', {
                      style: 'currency',
                      currency: row.currency,
                    }).format(row.sales_minor / 100)}
                  </h2>
                  <p>{row.orders} confirmed orders</p>
                </section>
              ))}
            </div>
            {!data.totals.length && <p>No confirmed orders in this period.</p>}
            <p>{data.definition}</p>
            <small>Read at {new Date(data.observed_at).toLocaleString()}</small>
          </>
        )}
      </section>
      <LiveMerchant revision={refreshKey} view="activity" />
    </>
  );
}
