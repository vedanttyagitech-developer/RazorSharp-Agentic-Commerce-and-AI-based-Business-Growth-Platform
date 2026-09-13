'use client';
import { useEffect, useState } from 'react';
import { merchantCall } from './live-merchant';
import { RefundQueue } from './refund-queue';
import { OrderTerms } from './order-terms';
import type { Order, OrdersPage } from '@/lib/commerce';

export function LiveMerchantOrders({
  onSupport,
}: {
  onSupport: (order: { id: string; reference: string }) => void;
}) {
  const [page, setPage] = useState<OrdersPage | null>(null);
  const [reference, setReference] = useState(''),
    [status, setStatus] = useState('');
  const [filters, setFilters] = useState({ reference: '', status: '' });
  const [cursor, setCursor] = useState('');
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState('');
  const [detail, setDetail] = useState<Order | null>(null);
  const [selected, setSelected] = useState('');
  useEffect(() => {
    let active = true;
    void merchantCall(
      'orders?limit=20&' +
        new URLSearchParams(
          Object.entries(filters).filter(([, value]) => value),
        ).toString() +
        (cursor ? '&cursor=' + encodeURIComponent(cursor) : ''),
    )
      .then((data) => {
        if (active) {
          setPage(data as unknown as OrdersPage);
          setError('');
        }
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [cursor, revision, filters]);
  useEffect(() => {
    let active = true;
    if (selected)
      void merchantCall('orders/' + encodeURIComponent(selected))
        .then((data) => {
          if (active) {
            setDetail(data as unknown as Order);
            setError('');
          }
        })
        .catch((e) => {
          if (active) setError(e.message);
        });
    return () => {
      active = false;
    };
  }, [selected, revision]);
  return (
    <section className="panel">
      <span className="eyebrow">MERCHANT ORDERS · BACKEND RECORDS</span>
      <h2>Confirmed orders</h2>
      <p>
        Recorded sales for this merchant. Unconfirmed payment attempts are not
        orders.
      </p>
      <button className="secondary" onClick={() => setRevision((n) => n + 1)}>
        Refresh orders
      </button>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setCursor('');
          setSelected('');
          setDetail(null);
          setPage(null);
          setFilters({ reference: reference.trim(), status });
        }}
      >
        <label>
          Order reference{' '}
          <input
            value={reference}
            onChange={(e) => setReference(e.target.value)}
            placeholder="RS-…"
          />
        </label>
        <label>
          Order state{' '}
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">All states</option>
            {[
              'CONFIRMED',
              'FULFILMENT_BLOCKED',
              'CANCELLED',
              'PARTIALLY_REFUNDED',
              'REFUNDED',
            ].map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
        </label>
        <button className="secondary">Apply filters</button>
      </form>
      {error && <p role="alert">{error}</p>}
      {!page && !error && <output>Reading orders…</output>}
      {page?.orders.map((order) => (
        <article className="evidence-event" key={order.order_id}>
          <h3>{order.reference}</h3>
          <p>
            {order.state} · {order.amount.currency} {order.amount.display}
          </p>
          <p>{new Date(order.created_at).toLocaleString()}</p>
          <button
            className="secondary"
            onClick={() => {
              setSelected(order.order_id);
              setDetail(null);
              setRevision((n) => n + 1);
            }}
          >
            Inspect {order.reference}
          </button>
        </article>
      ))}
      {page && !page.orders.length && <p>No confirmed orders returned.</p>}
      {page?.next_cursor && (
        <button
          className="secondary"
          onClick={() => {
            setCursor(page.next_cursor!);
            setSelected('');
            setDetail(null);
          }}
        >
          Next orders
        </button>
      )}
      {cursor && (
        <button
          className="secondary"
          onClick={() => {
            setCursor('');
            setSelected('');
            setDetail(null);
          }}
        >
          Latest orders
        </button>
      )}
      <details>
        <summary>Monitor refunds across orders</summary>
        <RefundQueue />
      </details>
      {detail !== null && (
        <section>
          <h3>Recorded order details</h3>
          <h4>
            {detail.reference} · {detail.state}
          </h4>
          <p>
            {detail.currency} {detail.amount.display} ·{' '}
            {new Date(detail.created_at).toLocaleString()}
          </p>
          <p>
            Payment: {detail.payment?.state ?? 'No payment record returned'}
          </p>
          {detail.payment?.capture_evidence && (
            <p>
              Capture evidence: {detail.payment.capture_evidence.kind} ·{' '}
              {detail.payment.capture_evidence.reference}
            </p>
          )}
          {detail.duration_seconds != null && (
            <p>
              Checkout to confirmed order: {detail.duration_seconds} seconds
            </p>
          )}
          <h4>Refund records</h4>
          {detail.refunds.length ? (
            detail.refunds.map((r) => (
              <p key={r.refund_id}>
                {r.currency} {(r.amount_minor / 100).toFixed(2)} · {r.state} ·{' '}
                {r.reason}
              </p>
            ))
          ) : (
            <p>No refunds recorded.</p>
          )}
          <OrderTerms
            key={detail.order_id}
            orderId={detail.order_id}
            merchant
          />
          <button
            className="secondary"
            onClick={() =>
              onSupport({ id: detail.order_id, reference: detail.reference })
            }
          >
            Open support cases for merchant review
          </button>
          <p>
            The support desk will show cases for this order. A refund requires a
            support case and a separate approval.
          </p>
          <details>
            <summary>Technical evidence</summary>
            <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
              {JSON.stringify(detail, null, 2)}
            </pre>
          </details>
        </section>
      )}
    </section>
  );
}
