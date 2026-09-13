'use client';
import { useEffect, useState } from 'react';
import { merchantCall } from './live-merchant';
type RefundPage = {
  counts: Record<string, number>;
  next_cursor: string | null;
  refunds: {
    refund_id: string;
    order_reference: string | null;
    state: string;
    amount: { display: string; currency: string };
    reason: string;
    updated_at: string;
    provider_refund_id: string | null;
  }[];
};
export function RefundQueue({
  platform = false,
  refreshKey = 0,
}: {
  platform?: boolean;
  refreshKey?: number | string;
}) {
  const [page, setPage] = useState<RefundPage | null>(null),
    [state, setState] = useState(''),
    [cursor, setCursor] = useState(''),
    [revision, setRevision] = useState(0),
    [error, setError] = useState(''),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true;
    queueMicrotask(() => {
      if (active) {
        setBusy(true);
        setError('');
        setPage(null);
      }
    });
    const query = new URLSearchParams({ limit: '20' });
    if (state) query.set('state', state);
    if (cursor) query.set('cursor', cursor);
    const path = `refunds?${query}`;
    const read = platform
      ? fetch(`/api/platform/${path}`, {
          credentials: 'same-origin',
          cache: 'no-store',
          headers: { 'X-Platform-Request': '1' },
        }).then(async (r) => {
          const data = (await r.json()) as RefundPage & { detail?: string };
          if (!r.ok)
            throw new Error(data.detail || 'Refund records unavailable');
          return data;
        })
      : merchantCall(path);
    void read
      .then((data) => {
        if (active) setPage(data as unknown as RefundPage);
      })
      .catch((e) => {
        if (active) setError(e.message);
      })
      .finally(() => {
        if (active) setBusy(false);
      });
    return () => {
      active = false;
    };
  }, [platform, state, cursor, revision, refreshKey]);
  return (
    <section className="panel platform-card">
      <h2>Refund monitoring</h2>
      <p>
        Recorded refund outcomes. Pending or unknown results require review;
        they are not permission to retry a payment.
      </p>
      <label>
        Refund state{' '}
        <select
          value={state}
          onChange={(e) => {
            setState(e.target.value);
            setCursor('');
          }}
        >
          <option value="">All states</option>
          {[
            'REFUND_PENDING',
            'REFUND_UNKNOWN',
            'REFUND_FAILED',
            'RECONCILING',
            'ESCALATED',
            'REFUNDED',
            'PARTIALLY_REFUNDED',
          ].map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
      </label>
      <button
        className="secondary"
        disabled={busy}
        onClick={() => setRevision((n) => n + 1)}
      >
        Refresh refunds
      </button>
      {busy && <output>Reading refunds…</output>}
      {error && <p role="alert">{error}</p>}
      {page && (
        <>
          <p>
            Counts cover all accessible records, independent of this page and
            filter.
          </p>
          <dl className="record-fields">
            {Object.entries(page.counts).map(([s, n]) => (
              <div key={s}>
                <dt>{s}</dt>
                <dd>{n}</dd>
              </div>
            ))}
          </dl>
          {page.refunds.map((r) => (
            <article className="evidence-event" key={r.refund_id}>
              <h3>
                {r.order_reference || 'No confirmed order'} ·{' '}
                {r.amount.currency} {r.amount.display}
              </h3>
              <p>
                {r.state} · {r.reason}
              </p>
              <p>Updated {new Date(r.updated_at).toLocaleString()}</p>
              <details>
                <summary>Refund identifiers</summary>
                <p>{r.refund_id}</p>
                <p>Provider: {r.provider_refund_id || 'Not recorded'}</p>
              </details>
            </article>
          ))}
          {!page.refunds.length && <p>No refunds match this filter.</p>}
          {page.next_cursor && (
            <button
              className="secondary"
              disabled={busy}
              onClick={() => setCursor(page.next_cursor!)}
            >
              Next refunds
            </button>
          )}
        </>
      )}
      {cursor && (
        <button
          className="secondary"
          disabled={busy}
          onClick={() => setCursor('')}
        >
          Latest refunds
        </button>
      )}
    </section>
  );
}
