'use client';
import { useEffect, useRef, useState } from 'react';
import { watchCheckout } from '@/lib/checkout-events';
import {
  commerce,
  rawCommerceCall,
  type Order,
  type OrderSummary,
} from '@/lib/commerce';
import { Badge } from './concept';
import { OrderProtocolEvidence } from './order-protocol-evidence';
import { OrderTerms } from './order-terms';
import { PaymentAcknowledgement } from './payment-acknowledgement';

type Case = {
  case_id: string;
  order_id: string;
  reason: string;
  status: string;
  resolution_note?: string;
};
type Timeline = {
  entries: {
    id: string;
    summary: string;
    occurred_at: string;
    source: string;
  }[];
};
type Proof = {
  verdict: {
    tier: string;
    ok: boolean;
    checks: {
      name: string;
      ok: boolean;
      applicable: boolean;
      detail: string;
    }[];
  };
};
export function LiveOrders({
  support = false,
  initialOrderId,
}: {
  support?: boolean;
  initialOrderId?: string;
}) {
  const [orders, setOrders] = useState<OrderSummary[]>([]),
    [cursor, setCursor] = useState<string | null>(null),
    [selected, setSelected] = useState(initialOrderId || ''),
    [error, setError] = useState(''),
    [busy, setBusy] = useState(true),
    [revision, setRevision] = useState(0);
  const [reference, setReference] = useState(''),
    [status, setStatus] = useState('');
  const [filters, setFilters] = useState({ reference: '', status: '' });
  const generation = useRef(0);
  useEffect(() => {
    const abort = new AbortController();
    queueMicrotask(() => {if(!abort.signal.aborted) setBusy(true);});
    commerce.orders
      .list({ limit: 20, ...filters, signal: abort.signal })
      .then((page) => {
        setError('');
        setOrders(page.orders);
        setCursor(page.next_cursor);
      })
      .catch((e) => {
        if (e.name !== 'AbortError') setError(e.message);
      })
      .finally(() => {
        if (!abort.signal.aborted) setBusy(false);
      });
    return () => abort.abort();
  }, [revision, filters]);
  const more = async () => {
    if (!cursor || busy) return;
    const current = ++generation.current;
    setBusy(true);
    setError('');
    try {
      const page = await rawCommerceCall<{
        orders: OrderSummary[];
        next_cursor: string | null;
      }>('orders', {
        query: {
          cursor,
          limit: 20,
          reference: filters.reference || undefined,
          status: filters.status || undefined,
        },
      });
      if (current === generation.current) {
        setOrders((rows) => [
          ...rows,
          ...page.orders.filter(
            (row) => !rows.some((old) => old.order_id === row.order_id),
          ),
        ]);
        setCursor(page.next_cursor);
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="inner-page live-orders">
      <div className="section-heading">
        <span className="eyebrow">
          {support ? 'CUSTOMER SUPPORT' : 'YOUR PURCHASES'}
        </span>
        <h1>{support ? 'Your order. A real next step.' : 'Your orders'}</h1>
        <p>
          Records from your shopping account. Refresh to check the latest
          confirmed state.
        </p>
      </div>
      <button
        className="secondary"
        disabled={busy}
        onClick={() => {
          generation.current++;
          setRevision((v) => v + 1);
        }}
      >
        Refresh orders
      </button>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          generation.current++;
          setSelected('');
          setOrders([]);
          setCursor(null);
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
        <button className="secondary" disabled={busy}>
          Apply filters
        </button>
      </form>
      {error && <p role="alert">{error}</p>}
      {busy && <output>Reading your orders…</output>}
      {!busy && !error && !orders.length && (
        <section className="panel">
          <h3>No orders match this view</h3>
          <p>
            Unconfirmed payment attempts are not orders. Check your checkout
            before starting another payment.
          </p>
        </section>
      )}
      <div className="split-content">
        <section className="panel">
          <h3>
            {support
              ? 'Choose the order you need help with'
              : 'Purchase history'}
          </h3>
          {orders.map((row) => (
            <button
              className="linked-case-button"
              key={row.order_id}
              onClick={() => setSelected(row.order_id)}
              aria-pressed={selected === row.order_id}
            >
              <div>
                <strong>{row.reference}</strong>
                <p>{new Date(row.created_at).toLocaleString()}</p>
                <span>{row.amount.display}</span>
                <p>
                  {row.return_offered
                    ? row.return_closes_at
                      ? `Return window ends ${new Date(row.return_closes_at).toLocaleString()}`
                      : 'Returns offered at sale; read sale terms for conditions.'
                    : 'No return offer recorded at sale.'}
                </p>
              </div>
              <Badge>{row.state}</Badge>
            </button>
          ))}
          {cursor && (
            <button className="secondary" disabled={busy} onClick={more}>
              Load older orders
            </button>
          )}
        </section>
        {selected && (
          <OrderDetail
            key={`${selected}:${revision}`}
            id={selected}
            support={support}
          />
        )}
      </div>
    </div>
  );
}
function OrderDetail({ id, support }: { id: string; support: boolean }) {
  const [order, setOrder] = useState<Order | null>(null),
    [timeline, setTimeline] = useState<Timeline | null>(null),
    [proof, setProof] = useState<Proof | null>(null),
    [cases, setCases] = useState<Case[]>([]),
    [error, setError] = useState(''),
    [evidenceError, setEvidenceError] = useState(''),
    [caseError, setCaseError] = useState(''),
    [reason, setReason] = useState('item_damaged'),
    [note, setNote] = useState(''),
    [busy, setBusy] = useState(false),
    [refresh, setRefresh] = useState(0);
  const key = useRef<string | null>(null);
  useEffect(() => {
    const abort = new AbortController();
    commerce.orders
      .read(id, abort.signal)
      .then((value) => {
        setError('');
        setOrder(value);
      })
      .catch((e) => {
        if (e.name !== 'AbortError') setError(e.message);
      });
    rawCommerceCall<{ cases: Case[] }>(`orders/${id}/support-cases`, {
      signal: abort.signal,
    })
      .then((x) => {
        setCases(x.cases);
        setCaseError('');
      })
      .catch((e) => {
        if (e.name !== 'AbortError') setCaseError(e.message);
      });
    return () => abort.abort();
  }, [id, refresh]);
  useEffect(() => {
    if (!order) return;
    const abort = new AbortController();
    rawCommerceCall<Timeline>(`checkouts/${order.checkout_id}/timeline`, {
      signal: abort.signal,
    })
      .then((value) => {
        setEvidenceError('');
        setTimeline(value);
      })
      .catch((e) => {
        if (e.name !== 'AbortError') setEvidenceError(e.message);
      });
    return () => abort.abort();
  }, [order]);
  useEffect(() => {
    if (!order?.checkout_id) return;
    return watchCheckout(order.checkout_id, () =>
      setRefresh((value) => value + 1),
    );
  }, [order?.checkout_id]);
  const verify = async () => {
    if (!order) return;
    setBusy(true);
    setEvidenceError('');
    setProof(null);
    try {
      setProof(
        await rawCommerceCall<Proof>(`checkouts/${order.checkout_id}/proof`),
      );
    } catch (e) {
      setEvidenceError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const submit = async () => {
    if (busy || !note.trim()) return;
    setBusy(true);
    setCaseError('');
    key.current ??= crypto.randomUUID();
    try {
      await rawCommerceCall<Case>(`orders/${id}/support-cases`, {
        method: 'POST',
        body: { reason, note: note.trim() },
        idempotencyKey: key.current,
      });
      setRefresh((v) => v + 1);
      setNote('');
      key.current = null;
    } catch (e) {
      setCaseError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="panel">
      {error && <p role="alert">{error}</p>}
      {!order && !error && <output>Reading purchase…</output>}
      {order && (
        <>
          <h2>{order.reference}</h2>
          <Badge>{order.state}</Badge>
          <p>
            {order.amount.display} ·{' '}
            {order.payment?.state || 'No payment state returned'}
          </p>
          {order.quote?.lines.map((line) => (
            <div className="mini-product" key={line.sku}>
              <strong>{line.name}</strong>
              <span>
                {line.quantity} ×{' '}
                {new Intl.NumberFormat('en-IN', {
                  style: 'currency',
                  currency: order.currency,
                }).format(line.unit_price_minor / 100)}
              </span>
            </div>
          ))}
          <h3>Recorded events</h3>
          <OrderTerms orderId={id} />
          <OrderProtocolEvidence checkoutId={order.checkout_id} />
          <PaymentAcknowledgement orderId={order.order_id} />
          {timeline?.entries.map((entry) => (
            <div className="evidence-event" key={entry.id}>
              <strong>{entry.summary}</strong>
              <p>
                {new Date(entry.occurred_at).toLocaleString()} · {entry.source}
              </p>
            </div>
          ))}
          {timeline && !timeline.entries.length && (
            <p>No events were returned.</p>
          )}
          <button className="secondary" disabled={busy} onClick={verify}>
            Verify transaction evidence
          </button>
          {evidenceError && (
            <p role="alert">Evidence unavailable: {evidenceError}</p>
          )}
          {proof && (
            <div className="proof-view">
              <h3>
                {proof.verdict.tier} ·{' '}
                {proof.verdict.ok
                  ? 'Checks hold at this tier'
                  : 'Verification failed'}
              </h3>
              {proof.verdict.checks.map((check, i) => (
                <div className="evidence-event" key={i}>
                  <Badge>
                    {!check.applicable ? 'N/A' : check.ok ? 'Pass' : 'Fail'}
                  </Badge>
                  <strong>{check.name}</strong>
                  <p>{check.detail}</p>
                </div>
              ))}
            </div>
          )}
          <details open={support}>
            <summary>Get help with this order</summary>
            <p>
              Submit an issue for merchant review. This does not approve or
              execute a refund.
            </p>
            {cases.map((c) => (
              <div className="evidence-event" key={c.case_id}>
                <strong>{c.reason}</strong>
                <Badge>{c.status}</Badge>
                <p>Case {c.case_id}</p>
                {c.resolution_note && (
                  <p style={{ whiteSpace: 'pre-wrap' }}>
                    Merchant response: {c.resolution_note}
                  </p>
                )}
              </div>
            ))}
            {caseError && <p role="alert">{caseError}</p>}
            <label className="form-field">
              Issue
              <select
                value={reason}
                disabled={busy}
                onChange={(e) => {
                  setReason(e.target.value);
                  key.current = null;
                }}
              >
                <option value="item_damaged">Damaged item</option>
                <option value="item_not_delivered">Missing item</option>
                <option value="wrong_item">Wrong item</option>
                <option value="ordered_by_mistake">
                  Ordered by mistake / cancellation request
                </option>
                <option value="buyer_requested">Other order issue</option>
              </select>
            </label>
            <label className="form-field">
              What happened?
              <textarea
                maxLength={1000}
                disabled={busy}
                value={note}
                onChange={(e) => {
                  setNote(e.target.value);
                  key.current = null;
                }}
              />
            </label>
            <button
              className="primary"
              disabled={busy || !note.trim()}
              onClick={submit}
            >
              {busy ? 'Working…' : 'Send issue to merchant'}
            </button>
            <button
              className="subtle"
              disabled={busy}
              onClick={() => setRefresh((v) => v + 1)}
            >
              Refresh case status
            </button>
          </details>
        </>
      )}
    </section>
  );
}
