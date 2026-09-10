'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import {MerchantRefund} from './merchant-refund';
import { Badge } from './concept';
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
    actions?: Action[];
    may_have_more?: boolean;
    ok?: boolean;
    reason?: string;
  };
  if (!result.ok)
    throw Object.assign(new Error(data.detail || data.title || 'Merchant request refused'), {status:result.status,title:data.title});
  return data;
}
type Case = {
  case_id: string;
  order_id: string;
  order_reference: string;
  reason: string;
  note: string;
  status: string;
};
type Action = {
  action_id: string;
  kind: string;
  target: string;
  state: string;
  proposal: Record<string, unknown>;
  content_hash: string;
  expected_revision: number;
  outcome_note: string;
};
const transitions: Record<string, string[]> = {
  OPEN: ['ACKNOWLEDGED', 'CLOSED'],
  ACKNOWLEDGED: ['RESOLVED', 'CLOSED', 'OPEN'],
  RESOLVED: ['CLOSED', 'ACKNOWLEDGED'],
  CLOSED: [],
};
export function LiveMerchant({
  view,
}: {
  view: 'support' | 'activity' | 'catalogue';
}) {
  const { products } = useCatalogue();
  const [target, setTarget] = useState(''),
    [kind, setKind] = useState('STOCK_ADJUSTMENT'),
    [value, setValue] = useState('');
  const [cases, setCases] = useState<Case[]>([]),
    [actions, setActions] = useState<Action[]>([]),
    [error, setError] = useState(''),
    [busy, setBusy] = useState(false),
    [secret, setSecret] = useState(''),
    [note, setNote] = useState(''),
    [truncated, setTruncated] = useState(false),
    [notice, setNotice] = useState('');
  const key = useRef<{ signature: string; key: string } | null>(null);
  const load = useCallback(async () => {
    try {
      const data = await merchantCall(
        view === 'support' ? 'support/cases' : 'merchant/actions',
      );
      setError('');
      setCases(data.cases || []);
      setActions(data.actions || []);
      setTruncated(data.may_have_more === true);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [view]);
  useEffect(() => {
    let active = true;
    merchantCall(view === 'support' ? 'support/cases' : 'merchant/actions')
      .then((data) => {
        if (!active) return;
        setCases(data.cases || []);
        setActions(data.actions || []);
        setTruncated(data.may_have_more === true);
        setError('');
      })
      .catch((error) => {
        if (active) setError(error.message);
      });
    return () => {
      active = false;
    };
  }, [view]);
  const login = async () => {
    setBusy(true);
    setError('');
    try {
      await merchantCall('session', 'POST', { key: secret });
      setSecret('');
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const change = async (path: string, body: unknown) => {
    if (busy) return;
    const signature = JSON.stringify([path, body]);
    if (key.current?.signature !== signature)
      key.current = { signature, key: crypto.randomUUID() };
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const data = await merchantCall(path, 'POST', body, key.current.key);
      setNotice(
        data.ok === false
          ? `Not applied: ${data.reason}`
          : 'Backend state updated.',
      );
      key.current = null;
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="panel">
      <span className="eyebrow">MERCHANT WORKSPACE · BACKEND RECORDS</span>
      <h2>
        {view === 'support' ? 'Customer support queue' : 'Merchant actions'}
      </h2>
      <p>
        Merchant permissions are separate from the buyer session. Refunds require a separate explicit approval.
      </p>
      <details>
        <summary>Connect local demo merchant account</summary>
        <p>
          Use the backend demo scenario key. It stays in an HttpOnly session
          cookie; it is not stored in browser localStorage. This is local demo
          authentication, not production sign-in.
        </p>
        <label className="form-field">
          Demo scenario key
          <input
            type="password"
            autoComplete="off"
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
          />
        </label>
        <button className="primary" disabled={busy || !secret} onClick={login}>
          Connect merchant
        </button>
        <button
          className="subtle"
          disabled={busy}
          onClick={async () => {
            try {
              await merchantCall('logout', 'POST', {});
              setCases([]);
              setActions([]);
              setNotice('Merchant session disconnected.');
            } catch (e) {
              setError((e as Error).message);
            }
          }}
        >
          Disconnect
        </button>
      </details>
      <button className="secondary" disabled={busy} onClick={load}>
        Refresh records
      </button>
      {busy && <output>Reading merchant state…</output>}
      {error && <p role="alert">{error}</p>}
      {notice && <output>{notice}</output>}
      {truncated && <p>Showing one page. More records exist in the backend.</p>}
      {view === 'catalogue' && (
        <section>
          <h3>Propose a catalogue change</h3>
          <p>
            The backend captures the current revision. A draft changes no stock
            or price.
          </p>
          <label className="form-field">
            Product
            <select
              value={target}
              disabled={busy}
              onChange={(e) => setTarget(e.target.value)}
            >
              <option value="">Choose product</option>
              {products.map((p) => (
                <option key={p.sku} value={p.sku}>
                  {p.name} · {p.stock} units
                </option>
              ))}
            </select>
          </label>
          <label className="form-field">
            Change
            <select
              value={kind}
              disabled={busy}
              onChange={(e) => {
                setKind(e.target.value);
                setValue('');
              }}
            >
              <option value="STOCK_ADJUSTMENT">Set stock count</option>
              <option value="STOCK_RECEIPT">Receive stock</option>
              <option value="PRICE_CHANGE">Set unit price (paise)</option>
            </select>
          </label>
          <label className="form-field">
            {kind === 'PRICE_CHANGE' ? 'Price in paise' : 'Units'}
            <input
              type="number"
              min="0"
              step="1"
              value={value}
              disabled={busy}
              onChange={(e) => setValue(e.target.value)}
            />
          </label>
          <button
            className="primary"
            disabled={
              busy ||
              !target ||
              value === '' ||
              !Number.isSafeInteger(Number(value)) ||
              Number(value) < 0
            }
            onClick={() =>
              change('merchant/actions', {
                kind,
                target,
                proposal: {
                  [kind === 'PRICE_CHANGE' ? 'unit_price_minor' : 'units']:
                    Number(value),
                },
              })
            }
          >
            Create draft
          </button>
        </section>
      )}
      {view === 'support' ? (
        <>
          <label className="form-field">
            Case handling note
            <textarea
              maxLength={1000}
              value={note}
              disabled={busy}
              onChange={(e) => setNote(e.target.value)}
            />
          </label>
          {cases.map((c) => (
            <article className="evidence-event" key={c.case_id}>
              <h3>
                {c.order_reference} · {c.reason}
              </h3>
              <Badge>{c.status}</Badge>
              <blockquote>{c.note}</blockquote>
              <small>{c.case_id}</small>
              <div>
                {(transitions[c.status] || []).map((status) => (
                  <button
                    className="secondary"
                    disabled={busy || !note.trim()}
                    key={status}
                    onClick={() =>
                      change(`support/cases/${c.case_id}/advance`, {
                        status,
                        note,
                      })
                    }
                  >
                    {status === 'ACKNOWLEDGED'
                      ? 'Acknowledge'
                      : status === 'RESOLVED'
                        ? 'Mark case resolved'
                        : status === 'CLOSED'
                          ? 'Close case'
                          : 'Reopen'}
                  </button>
                ))}
              </div>
              <MerchantRefund caseId={c.case_id} orderId={c.order_id} reason={c.reason}/>
            </article>
          ))}
        </>
      ) : (
        actions.map((a) => (
          <article className="evidence-event" key={a.action_id}>
            <h3>
              {a.kind} · {a.target}
            </h3>
            <Badge>{a.state}</Badge>
            <p>Catalogue revision {a.expected_revision}</p>
            <dl>
              {Object.entries(a.proposal).map(([field, value]) => (
                <div key={field}>
                  <dt>{field}</dt>
                  <dd>{JSON.stringify(value)}</dd>
                </div>
              ))}
            </dl>
            <details>
              <summary>Approval binding</summary>
              <code style={{ overflowWrap: 'anywhere' }}>{a.content_hash}</code>
            </details>
            <p>{a.outcome_note}</p>
            {a.state === 'DRAFT' && (
              <button
                className="secondary"
                disabled={busy}
                onClick={() =>
                  change(`merchant/actions/${a.action_id}/submit`, {})
                }
              >
                Request approval
              </button>
            )}
            {a.state === 'AWAITING_APPROVAL' && (
              <button
                className="primary"
                disabled={busy}
                onClick={() =>
                  change(`merchant/actions/${a.action_id}/approve`, {
                    content_hash: a.content_hash,
                  })
                }
              >
                Approve this exact proposal
              </button>
            )}
            {a.state === 'APPROVED' && (
              <button
                className="primary"
                disabled={busy}
                onClick={() =>
                  change(`merchant/actions/${a.action_id}/execute`, {})
                }
              >
                Execute approved change
              </button>
            )}
          </article>
        ))
      )}
      {!busy && !error && !(view === 'support' ? cases : actions).length && (
        <p>No records returned for this session.</p>
      )}
    </section>
  );
}
