'use client';
import { useEffect, useRef, useState } from 'react';
import { MerchantRefund } from './merchant-refund';
import { Badge } from './concept';
import {
  MERCHANT_STATE_CHANGED,
  merchantStateChanged,
} from '@/lib/merchant-sync';
import {
  MerchantActionCard,
  type MerchantAction,
} from './merchant-action-card';
import { MerchantCatalogue } from './merchant-catalogue';
import { MerchantProposalForm } from './merchant-proposal-form';
import { OrderTerms } from './order-terms';
import { useCatalogue } from '@/lib/catalogue';
export async function merchantCall(
  path: string,
  method = 'GET',
  body?: unknown,
  key?: string,
) {
  const result = await fetch('/api/merchant/' + path, {
    method,
    headers: {
      'Content-Type': 'application/json',
      ...(key ? { 'Idempotency-Key': key } : {}),
    },
    ...(method !== 'GET' && body !== undefined
      ? { body: JSON.stringify(body) }
      : {}),
  });
  const data = (await result.json()) as {
    detail?: string;
    title?: string;
    cases?: Case[];
    actions?: MerchantAction[];
    may_have_more?: boolean;
    ok?: boolean;
    reason?: string;
  };
  if (!result.ok)
    throw Object.assign(
      new Error(data.detail || data.title || 'Merchant request refused'),
      { status: result.status, title: data.title },
    );
  return data;
}
type Case = {
  case_id: string;
  order_id: string;
  order_reference: string;
  reason: string;
  note: string;
  status: string;
  opened_by: string;
  handled_by: string | null;
  resolution_note: string;
  created_at: string;
  updated_at: string;
};
const transitions: Record<string, string[]> = {
  OPEN: ['ACKNOWLEDGED', 'CLOSED'],
  ACKNOWLEDGED: ['RESOLVED', 'CLOSED', 'OPEN'],
  RESOLVED: ['CLOSED', 'ACKNOWLEDGED'],
  CLOSED: ['ACKNOWLEDGED'],
};
export function LiveMerchant({
  view,
  revision = 0,
  orderFilter,
  onClearOrderFilter,
}: {
  view: 'support' | 'activity' | 'catalogue';
  revision?: number;
  orderFilter?: { id: string; reference: string } | null;
  onClearOrderFilter?: () => void;
}) {
  const { products } = useCatalogue();
  const [cases, setCases] = useState<Case[]>([]),
    [actions, setActions] = useState<MerchantAction[]>([]),
    [error, setError] = useState(''),
    [busy, setBusy] = useState(false),
    [reading, setReading] = useState(true),
    [notice, setNotice] = useState(''),
    [notes, setNotes] = useState<Record<string, string>>({}),
    [filter, setFilter] = useState(''),
    [offset, setOffset] = useState(0),
    [more, setMore] = useState(false),
    [refresh, setRefresh] = useState(0);
  const key = useRef<{ signature: string; key: string } | null>(null);
  const path =
    (view === 'support' ? 'support/cases' : 'merchant/actions') +
    (view === 'support' && orderFilter
      ? '?order_id=' + encodeURIComponent(orderFilter.id) + '&limit=20&offset='
      : '?limit=20&offset=') +
    offset +
    (filter
      ? '&' +
        (view === 'support' ? 'status' : 'state') +
        '=' +
        encodeURIComponent(filter)
      : '');
  useEffect(() => {
    const onChange = () => {
      setReading(true);
      setRefresh((v) => v + 1);
    };
    window.addEventListener(MERCHANT_STATE_CHANGED, onChange);
    return () => window.removeEventListener(MERCHANT_STATE_CHANGED, onChange);
  }, []);
  useEffect(() => {
    let active = true;
    merchantCall(path)
      .then((data) => {
        if (!active) return;
        setCases(data.cases || []);
        setActions(data.actions || []);
        setMore(data.may_have_more === true);
        setError('');
      })
      .catch((e) => {
        if (active) setError(e.message);
      })
      .finally(() => {
        if (active) setReading(false);
      });
    return () => {
      active = false;
    };
  }, [path, revision, refresh]);
  const change = async (
    path: string,
    body: unknown,
    method = 'POST',
  ): Promise<boolean> => {
    if (busy) return false;
    const signature = JSON.stringify([method, path, body]);
    if (key.current?.signature !== signature)
      key.current = { signature, key: crypto.randomUUID() };
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const data = await merchantCall(path, method, body, key.current.key);
      setNotice(
        data.ok === false
          ? 'Not applied: ' + data.reason
          : 'Backend state updated.',
      );
      key.current = null;
      merchantStateChanged();
      return data.ok !== false;
    } catch (e) {
      setError((e as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  };
  const statuses =
    view === 'support'
      ? Object.keys(transitions)
      : [
          'DRAFT',
          'AWAITING_APPROVAL',
          'APPROVED',
          'QUEUED',
          'EXECUTING',
          'SUCCEEDED',
          'FAILED',
          'UNKNOWN',
          'REJECTED',
          'EXPIRED',
          'CANCELLED',
          'STALE',
        ];
  return (
    <section className="panel">
      <span className="eyebrow">MERCHANT WORKSPACE · BACKEND RECORDS</span>
      <h2>
        {view === 'support' ? 'Customer support queue' : 'Merchant actions'}
      </h2>
      <p>
        Every consequential change carries its own review and approval. Case
        handling does not authorize a refund.
      </p>
      <div className="record-toolbar">
        <label className="form-field">
          Status
          <select
            value={filter}
            disabled={busy}
            onChange={(e) => {
              setFilter(e.target.value);
              setOffset(0);
              setReading(true);
            }}
          >
            <option value="">All statuses</option>
            {statuses.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
        </label>
        <button
          className="secondary"
          disabled={busy || reading}
          onClick={() => {
            setReading(true);
            setRefresh((v) => v + 1);
          }}
        >
          Refresh records
        </button>
        <span>
          Records{' '}
          {(view === 'support' ? cases : actions).length ? offset + 1 : 0}–
          {offset + (view === 'support' ? cases : actions).length}
        </span>
      </div>
      {view === 'support' && orderFilter && (
        <p>
          Cases for <strong>{orderFilter.reference}</strong>{' '}
          <button className="secondary" onClick={onClearOrderFilter}>
            Show all support cases
          </button>
        </p>
      )}
      {view === 'support' &&
        orderFilter &&
        !reading &&
        !error &&
        !cases.length && (
          <p>No support cases match this order and status filter.</p>
        )}
      {error && <p role="alert">{error}</p>}
      {notice && <output>{notice}</output>}
      {reading && <output>Reading merchant state…</output>}
      {view === 'catalogue' && <MerchantCatalogue />}
      {view === 'catalogue' && (
        <MerchantProposalForm
          products={products}
          busy={busy}
          onSubmit={(body) => change('merchant/actions', body)}
        />
      )}
      <div aria-busy={reading} className="record-list">
        {view === 'support'
          ? cases.map((c) => (
              <article className="evidence-event action-record" key={c.case_id}>
                <header>
                  <h3>
                    {c.order_reference} · {c.reason}
                  </h3>
                  <Badge>{c.status}</Badge>
                </header>
                <blockquote>{c.note}</blockquote>
                <dl className="record-fields">
                  <div>
                    <dt>Opened by</dt>
                    <dd>{c.opened_by}</dd>
                  </div>
                  <div>
                    <dt>Handled by</dt>
                    <dd>{c.handled_by || 'Not assigned'}</dd>
                  </div>
                  <div>
                    <dt>Opened</dt>
                    <dd>{new Date(c.created_at).toLocaleString()}</dd>
                  </div>
                  <div>
                    <dt>Last updated</dt>
                    <dd>{new Date(c.updated_at).toLocaleString()}</dd>
                  </div>
                </dl>
                {c.resolution_note && (
                  <p>
                    <strong>Recorded resolution:</strong> {c.resolution_note}
                  </p>
                )}
                <OrderTerms orderId={c.order_id} merchant />
                <label className="form-field">
                  Case handling note
                  <textarea
                    maxLength={1000}
                    value={notes[c.case_id] || ''}
                    disabled={busy}
                    onChange={(e) =>
                      setNotes({ ...notes, [c.case_id]: e.target.value })
                    }
                  />
                </label>
                <div className="record-actions">
                  {(transitions[c.status] || []).map((status) => (
                    <button
                      className="secondary"
                      disabled={busy || reading || !notes[c.case_id]?.trim()}
                      key={status}
                      onClick={() =>
                        void change('support/cases/' + c.case_id + '/advance', {
                          status,
                          note: notes[c.case_id],
                        })
                      }
                    >
                      {status === 'ACKNOWLEDGED'
                        ? c.status === 'CLOSED' ? 'Reopen case' : 'Acknowledge'
                        : status === 'RESOLVED'
                          ? 'Mark case resolved'
                          : status === 'CLOSED'
                            ? 'Close case'
                            : 'Reopen'}
                    </button>
                  ))}
                </div>
                <MerchantRefund
                  caseId={c.case_id}
                  orderId={c.order_id}
                  reason={c.reason}
                />
              </article>
            ))
          : actions.map((a) => (
              <MerchantActionCard
                key={a.action_id + ':' + a.content_hash + ':' + a.state}
                action={a}
                busy={busy || reading}
                change={change}
              />
            ))}
      </div>
      {!reading && !error && !(view === 'support' ? cases : actions).length && (
        <p>No records match this filter.</p>
      )}
      <nav aria-label="Record pages" className="record-actions">
        <button
          className="secondary"
          disabled={busy || reading || offset === 0}
          onClick={() => {
            setOffset((v) => Math.max(0, v - 20));
            setReading(true);
          }}
        >
          Previous records
        </button>
        <button
          className="secondary"
          disabled={busy || reading || !more}
          onClick={() => {
            setOffset((v) => v + 20);
            setReading(true);
          }}
        >
          Next records
        </button>
      </nav>
    </section>
  );
}
