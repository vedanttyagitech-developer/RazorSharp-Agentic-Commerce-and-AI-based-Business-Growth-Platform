'use client';
import { useEffect, useState } from 'react';
import Link from 'next/link';
import { ProtocolBuyerJourney } from '@/components/protocol-buyer-journey';
import { ProtocolLab } from '@/components/protocol-lab';
import { RuntimeReadiness } from '@/components/runtime-readiness';
import { RefundQueue } from '@/components/refund-queue';
import './platform.css';
import {
  EvidenceExplorer,
  EvidenceDocument,
  ProtocolEvidence,
  MetricsEvidence,
  ReviveCommand,
} from '@/components/platform-evidence';

type Mode = {
  tenant_id: string;
  mode: string;
  safe_mode: boolean;
  blocked: string[];
  still_available: string[];
};
type Attempt = {
  payment_attempt_id: string;
  checkout_id: string;
  recorded_state: string;
  verified: {
    present: boolean;
    status: string | null;
    source: string | null;
    amount_minor: number | null;
    currency: string | null;
    observed_at: string | null;
  };
  findings: { code: string; detail: string }[];
  updated_at: string;
};
type Case = {
  case_key: string;
  priority: string;
  state: string;
  reason_code: string;
  checkout_id: string;
};
type Page = { offset: number; limit: number; has_more: boolean };
type Snapshot = {
  mode: Mode;
  reconciliation: Page & { attempts: Attempt[] };
  cases: Page & { cases: Case[]; scope: string };
  outbox: Page & {
    waiting?: {
      parked: number;
      overdue: number;
      parked_beyond_seconds: number;
      overdue_beyond_seconds: number;
      oldest: Record<string, unknown> | null;
    };
    counts: Record<string, number>;
    commands: ({ command_id?: string; id?: string; status?: string } & Record<
      string,
      unknown
    >)[];
  };
  at: string;
};
async function api<T = unknown>(
  path: string,
  method = 'GET',
  body?: unknown,
): Promise<T> {
  const options: RequestInit = {
    method,
    credentials: 'same-origin',
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json', 'X-Platform-Request': '1' },
  };
  if (path === 'ops/runtime-health') options.signal = AbortSignal.timeout(5000);
  if (body !== undefined && method !== 'GET')
    options.body = JSON.stringify(body);
  const response = await fetch(`/api/platform/${path}`, options);
  const data = (await response.json()) as T & { detail?: string };
  if (!response.ok)
    throw new Error(data.detail || `Request failed (${response.status})`);
  return data;
}
type Filters = {
  limit: number;
  unresolved: boolean;
  checkout: string;
  status: string;
  offset: number;
};
const initialFilters: Filters = {
  limit: 50,
  unresolved: false,
  checkout: '',
  status: '',
  offset: 0,
};
async function loadSnapshot(
  filters: Filters = initialFilters,
): Promise<Snapshot> {
  const [mode, reconciliation, cases, outbox] = await Promise.all([
    api<Mode>('ops/safe-mode'),
    api<Snapshot['reconciliation']>(
      `review/reconciliation?limit=${filters.limit}&offset=${filters.offset}&unresolved_only=${filters.unresolved}${filters.checkout ? '&checkout_id=' + encodeURIComponent(filters.checkout) : ''}`,
    ),
    api<Snapshot['cases']>(
      `review/queue?limit=${filters.limit}&offset=${filters.offset}`,
    ),
    api<Snapshot['outbox']>(
      `ops/outbox?limit=${filters.limit}&offset=${filters.offset}${filters.status ? '&status=' + filters.status : ''}`,
    ),
  ]);
  return { mode, reconciliation, cases, outbox, at: new Date().toISOString() };
}
const tabs = [
  'Overview',
  'Reconciliation',
  'Review queue',
  'Execution queue',
  'Refunds',
  'Incident controls',
  'Evidence explorer',
  'ACP & UCP',
  'MCP',
  'Protocol evidence',
  'Metrics',
  'Trust boundary',
] as const;
export default function Platform() {
  const [merchantId, setMerchantId] = useState('');
  const [filters, setFilters] = useState<Filters>(initialFilters);
  const [appliedFilters, setAppliedFilters] = useState<Filters>(initialFilters);
  const [tab, setTab] = useState<string>(() => typeof window !== 'undefined' && ['ACP','UCP'].includes(new URLSearchParams(window.location.search).get('buyerProtocol') ?? '') ? 'ACP & UCP' : 'Overview');
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [detail, setDetail] = useState<unknown>(null);
  const [confirmation, setConfirmation] = useState('');
  async function refresh(nextFilters: Filters = filters) {
    setBusy(true);
    setError('');
    setDetail(null);
    try {
      const identity = await api<{ merchant_id: string }>(
        'session',
        'POST',
        {},
      );
      setMerchantId(identity.merchant_id);
      setSnapshot(await loadSnapshot(nextFilters));
      setFilters(nextFilters);
      setAppliedFilters(nextFilters);
    } catch (e) {
      setError((e as Error).message);
      setSnapshot(null);
    } finally {
      setBusy(false);
    }
  }
  useEffect(() => {
    let active = true;
    void api<{ merchant_id: string }>('session', 'POST', {})
      .then((identity) => {
        if (active) setMerchantId(identity.merchant_id);
        return loadSnapshot();
      })
      .then((value) => {
        if (active) setSnapshot(value);
      })
      .catch((e) => {
        if (active) setError((e as Error).message);
      });
    return () => {
      active = false;
    };
  }, []);
  async function inspect(path: string) {
    setDetail(null);
    setBusy(true);
    setError('');
    try {
      setDetail(await api(path));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function changeMode() {
    if (!snapshot) return;
    setBusy(true);
    setError('');
    try {
      await api('ops/safe-mode', 'POST', {
        enabled: !snapshot.mode.safe_mode,
        confirm: confirmation,
      });
      setConfirmation('');
      await refresh();
    } catch (e) {
      setError((e as Error).message);
      setSnapshot(null);
      setBusy(false);
    }
  }
  return (
    <main className="platform-shell">
      <aside className="platform-nav">
        <Link href="/" className="platform-brand">
          razorsharp<span>PLATFORM CONSOLE</span>
        </Link>
        <div className="platform-tag">Open demo</div>
        <nav aria-label="Platform navigation">
          {tabs.map((name) => (
            <button
              key={name}
              aria-current={tab === name ? 'page' : undefined}
              onClick={() => {
                setTab(name);
                setDetail(null);
              }}
            >
              {name}
            </button>
          ))}
        </nav>
        <Link href="/merchant">Merchant workspace ↗</Link>
        <Link href="/shop">Buyer storefront ↗</Link>
      </aside>
      <section className="platform-main">
        <header>
          <div>
            <span className="platform-eyebrow">
              AGENTIC COMMERCE / OPERATIONS
            </span>
            <h1>{tab}</h1>
            <p>Understand the evidence. Act within the boundary.</p>
          </div>
          {snapshot && (
            <div className="platform-actions">
              <button disabled={busy} onClick={() => void refresh()}>
                Refresh evidence
              </button>
            </div>
          )}
        </header>
        <div className="platform-notice">
          Demo application controls · No Razorpay internal privileges · No
          bank-backed Reserve authorization
        </div>
        {error && (
          <p role="alert" className="platform-error">
            {error}
          </p>
        )}
        {!snapshot ? (
          <section className="platform-card platform-login">
            <span className="platform-eyebrow">OPEN DEMO</span>
            <h2>
              {error
                ? 'Demo temporarily unavailable'
                : 'Opening the platform demo…'}
            </h2>
            <p>No account, OAuth, or access code needed.</p>
            {error && (
              <button
                className="platform-primary"
                disabled={busy}
                onClick={() => void refresh()}
              >
                {busy ? 'Opening…' : 'Try again'}
              </button>
            )}
          </section>
        ) : (
          <>
            <div className="platform-scope">
              <span>
                Tenant <code>{snapshot.mode.tenant_id}</code>
              </span>
              <span>
                Fetched {new Date(snapshot.at).toLocaleTimeString()} · Manual
                refresh
              </span>
            </div>
            {['Reconciliation', 'Review queue', 'Execution queue'].includes(
              tab,
            ) && (
              <form
                className="platform-card evidence-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  void refresh({ ...filters, offset: 0 });
                }}
              >
                <label>
                  Page size
                  <select
                    value={filters.limit}
                    onChange={(e) =>
                      setFilters({ ...filters, limit: Number(e.target.value) })
                    }
                  >
                    {[20, 50, 100].map((n) => (
                      <option key={n}>{n}</option>
                    ))}
                  </select>
                </label>
                {tab === 'Reconciliation' && (
                  <>
                    <label>
                      <input
                        type="checkbox"
                        checked={filters.unresolved}
                        onChange={(e) =>
                          setFilters({
                            ...filters,
                            unresolved: e.target.checked,
                          })
                        }
                      />
                      Only attempts with findings
                    </label>
                    <label>
                      Checkout identifier
                      <input
                        value={filters.checkout}
                        onChange={(e) =>
                          setFilters({
                            ...filters,
                            checkout: e.target.value.trim(),
                          })
                        }
                      />
                    </label>
                  </>
                )}
                {tab === 'Execution queue' && (
                  <label>
                    Command status
                    <select
                      value={filters.status}
                      onChange={(e) =>
                        setFilters({ ...filters, status: e.target.value })
                      }
                    >
                      <option value="">All statuses</option>
                      {['PENDING', 'LEASED', 'FAILED', 'DEAD', 'DONE'].map(
                        (v) => (
                          <option key={v}>{v}</option>
                        ),
                      )}
                    </select>
                  </label>
                )}
                <button disabled={busy}>Apply filters</button>
                <small>
                  Pages follow recorded creation order. Findings filter the
                  attempts on each page; an empty page can still have older
                  attempts. Refresh from the first page for newly arrived
                  records.
                </small>
              </form>
            )}
            {['Reconciliation', 'Review queue', 'Execution queue'].includes(
              tab,
            ) &&
              (() => {
                const page =
                  tab === 'Reconciliation'
                    ? snapshot.reconciliation
                    : tab === 'Review queue'
                      ? snapshot.cases
                      : snapshot.outbox;
                return (
                  <nav className="platform-pagination" aria-label="Queue pages">
                    <button
                      disabled={busy || page.offset === 0}
                      onClick={() =>
                        void refresh({
                          ...appliedFilters,
                          offset: Math.max(0, page.offset - page.limit),
                          limit: page.limit,
                        })
                      }
                    >
                      Previous page
                    </button>
                    <span>Page {Math.floor(page.offset / page.limit) + 1}</span>
                    <button
                      disabled={busy || !page.has_more}
                      onClick={() =>
                        void refresh({
                          ...appliedFilters,
                          offset: page.offset + page.limit,
                          limit: page.limit,
                        })
                      }
                    >
                      Next page
                    </button>
                    <button
                      disabled={busy || page.offset === 0}
                      onClick={() =>
                        void refresh({ ...appliedFilters, offset: 0 })
                      }
                    >
                      Newest records
                    </button>
                  </nav>
                );
              })()}
            {tab === 'Evidence explorer' && (
              <EvidenceExplorer api={api} merchantId={merchantId} />
            )}
            {tab === 'ACP & UCP' && <div className="protocol-pair"><div><ProtocolBuyerJourney protocol="ACP" /><ProtocolLab api={api} protocol="ACP" /></div><div><ProtocolBuyerJourney protocol="UCP" /><ProtocolLab api={api} protocol="UCP" /></div></div>}
            {tab === 'MCP' && <ProtocolLab api={api} protocol="MCP" />}
            {tab === 'Protocol evidence' && <ProtocolEvidence api={api} />}
            {tab === 'Metrics' && <MetricsEvidence />}
            {tab === 'Overview' && (
              <>
                <RuntimeReadiness api={api} refreshKey={snapshot.at} />
                <div className="platform-stats">
                  <article>
                    <span>Tenant posture</span>
                    <strong>{snapshot.mode.mode}</strong>
                    <small>Current kernel response</small>
                  </article>
                  <article>
                    <span>Attempts shown</span>
                    <strong>{snapshot.reconciliation.attempts.length}</strong>
                    <small>Current page, not all-time volume</small>
                  </article>
                  <article>
                    <span>Cases shown</span>
                    <strong>{snapshot.cases.cases.length}</strong>
                    <small>Current review page</small>
                  </article>
                  <article>
                    <span>Failed commands</span>
                    <strong>
                      {snapshot.outbox.counts.FAILED ?? 'Unavailable'}
                    </strong>
                    <small>Tenant-wide outbox count</small>
                  </article>
                </div>
                <div className="platform-card">
                  <h2>Three separate sources of truth</h2>
                  <div className="platform-columns">
                    <p>
                      <b>Buyer approval</b>Authorizes the exact checkout. An
                      operator cannot substitute their approval.
                    </p>
                    <p>
                      <b>Local recorded state</b>Describes the application’s
                      durable records, holds and execution progress.
                    </p>
                    <p>
                      <b>Provider evidence</b>Shows what recorded provider
                      verification established. Missing evidence stays unknown.
                    </p>
                  </div>
                  <button onClick={() => setTab('Reconciliation')}>
                    Investigate payment attempts →
                  </button>
                </div>
              </>
            )}
            {tab === 'Reconciliation' && (
              <div className="platform-card">
                <h2>Recorded state / provider evidence</h2>
                <p>
                  Stored verification evidence; opening this page does not fetch
                  a new answer from Razorpay.
                </p>
                {snapshot.reconciliation.attempts.length === 0 ? (
                  <p>No attempts in this page.</p>
                ) : (
                  <div className="platform-table">
                    <table>
                      <thead>
                        <tr>
                          <th>Attempt</th>
                          <th>Local state</th>
                          <th>Provider evidence</th>
                          <th>Findings</th>
                          <th>Action</th>
                        </tr>
                      </thead>
                      <tbody>
                        {snapshot.reconciliation.attempts.map((a) => (
                          <tr key={a.payment_attempt_id}>
                            <td>
                              <code>{a.payment_attempt_id}</code>
                            </td>
                            <td>{a.recorded_state}</td>
                            <td>
                              {a.verified.present === true ? (
                                <div className="provider-summary">
                                  <strong>
                                    {String(
                                      a.verified.status ?? 'Status unavailable',
                                    )}
                                  </strong>
                                  <small>
                                    {String(
                                      a.verified.source ?? 'Source unavailable',
                                    )}
                                  </small>
                                  {typeof a.verified.amount_minor ===
                                    'number' && (
                                    <span>
                                      {String(a.verified.currency ?? '')}{' '}
                                      {(a.verified.amount_minor / 100).toFixed(
                                        2,
                                      )}
                                    </span>
                                  )}
                                  {typeof a.verified.observed_at ===
                                    'string' && (
                                    <small>
                                      Observed{' '}
                                      {new Date(
                                        a.verified.observed_at,
                                      ).toLocaleString()}
                                    </small>
                                  )}
                                </div>
                              ) : (
                                <span>No recorded provider verification</span>
                              )}
                            </td>
                            <td>
                              {a.findings.map((f) => f.code).join(', ') ||
                                'None recorded'}
                            </td>
                            <td>
                              <button
                                disabled={busy}
                                onClick={() =>
                                  void inspect(
                                    `review/reconciliation/${a.payment_attempt_id}`,
                                  )
                                }
                              >
                                Inspect
                              </button>
                              <button
                                disabled={busy}
                                onClick={() =>
                                  void inspect(
                                    `inspector/payment-attempts/${a.payment_attempt_id}`,
                                  )
                                }
                              >
                                Full payment evidence
                              </button>
                              <button
                                disabled={busy}
                                onClick={() =>
                                  void inspect(
                                    `audit/streams/payment_attempt/${a.payment_attempt_id}/verify`,
                                  )
                                }
                              >
                                Verify audit chain
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            )}
            {tab === 'Review queue' && (
              <div className="platform-card">
                <h2>Human review</h2>
                <p>
                  Read-only review cases. Assignment, decisions and recovery
                  execution are not enabled in this console.
                </p>
                {snapshot.cases.cases.length === 0 ? (
                  <p>No review cases in this page.</p>
                ) : (
                  snapshot.cases.cases.map((c) => (
                    <article className="platform-case" key={c.case_key}>
                      <div>
                        <b>
                          {c.priority} · {c.reason_code}
                        </b>
                        <p>
                          {c.state} · Checkout {c.checkout_id}
                        </p>
                      </div>
                      <button
                        disabled={busy}
                        onClick={() =>
                          void inspect(
                            `review/queue/${encodeURIComponent(c.case_key)}`,
                          )
                        }
                      >
                        View evidence
                      </button>
                    </article>
                  ))
                )}
              </div>
            )}
            {tab === 'Refunds' && (
              <RefundQueue platform refreshKey={snapshot.at} />
            )}
            {tab === 'Execution queue' && (
              <div className="platform-card">
                <h2>Durable execution</h2>
                <p>
                  Dead commands can be reviewed for recovery under their
                  existing authority. A failed or unknown payment is not
                  permission to create another attempt.
                </p>
                <dl className="outbox-counts">
                  {Object.entries(snapshot.outbox.counts).map(
                    ([status, count]) => (
                      <div key={status}>
                        <dt>{status}</dt>
                        <dd>{count}</dd>
                      </div>
                    ),
                  )}
                </dl>
                <p>
                  Counts cover the whole tenant, independent of the selected
                  page or status filter.
                </p>
                {snapshot.outbox.waiting && (
                  <section aria-label="Waiting work">
                    <h3>Waiting work · tenant-wide</h3>
                    <p>
                      {snapshot.outbox.waiting.parked} parked beyond{' '}
                      {snapshot.outbox.waiting.parked_beyond_seconds}s ·{' '}
                      {snapshot.outbox.waiting.overdue} overdue by more than{' '}
                      {snapshot.outbox.waiting.overdue_beyond_seconds}s
                    </p>
                    <p>
                      Parked work is scheduled for later. Overdue work is due
                      but has not been claimed; inspect worker health before
                      taking action.
                    </p>
                    {snapshot.outbox.waiting.oldest && (
                      <details>
                        <summary>Oldest waiting command</summary>
                        <Evidence value={snapshot.outbox.waiting.oldest} />
                      </details>
                    )}
                  </section>
                )}
                {snapshot.outbox.commands.length ? (
                  snapshot.outbox.commands.map((c, i) => (
                    <details key={i}>
                      <summary>
                        {String(c.command_id ?? c.id ?? `Command ${i + 1}`)} ·{' '}
                        {String(c.status ?? '')}
                      </summary>
                      <Evidence value={c} />
                      <ReviveCommand
                        command={c}
                        api={api}
                        onUpdated={() => refresh()}
                      />
                    </details>
                  ))
                ) : (
                  <p>No commands in this page.</p>
                )}
              </div>
            )}
            {tab === 'Incident controls' && (
              <div className="platform-card">
                <h2>Tenant Safe Mode</h2>
                <p>
                  Current mode: <b>{snapshot.mode.mode}</b>. Applies only to the
                  tenant shown above.
                </p>
                <div className="platform-columns">
                  <p>
                    <b>Blocked in Safe Mode</b>
                    {snapshot.mode.blocked.join(', ')}
                  </p>
                  <p>
                    <b>Still available</b>
                    {snapshot.mode.still_available.join(', ')}
                  </p>
                </div>
                <p>
                  {snapshot.mode.safe_mode
                    ? 'Leaving Safe Mode restores the operations permitted by normal mode.'
                    : 'Entering Safe Mode restricts new delegated actions; it does not turn unknown outcomes into failures.'}
                </p>
                <label>
                  Type CHANGE TENANT MODE to confirm
                  <input
                    value={confirmation}
                    onChange={(e) => setConfirmation(e.target.value)}
                    autoComplete="off"
                  />
                </label>
                <button
                  className="platform-primary"
                  disabled={busy || confirmation !== 'CHANGE TENANT MODE'}
                  onClick={() => void changeMode()}
                >
                  {snapshot.mode.safe_mode
                    ? 'Leave tenant Safe Mode'
                    : 'Enter tenant Safe Mode'}
                </button>
                <small>
                  Backend rechecks the operator session and records the
                  transition. No global platform switch is exposed.
                </small>
              </div>
            )}
            {tab === 'Trust boundary' && (
              <div className="platform-card">
                <h2>Authority has a boundary</h2>
                <button disabled={busy} onClick={() => void inspect('config')}>
                  Read runtime configuration
                </button>
                <p>
                  This console does not hold signing permission, export keys, or
                  create buyer permissions.
                </p>
                <ul>
                  <li>
                    Reserve permission artifacts belong to this application’s
                    simulator issuer.
                  </li>
                  <li>
                    Payment confirmation comes from validated provider evidence,
                    not the simulator signature.
                  </li>
                  <li>Live HSM inventory is not connected to this console.</li>
                  <li>
                    Settlement, dispute handling, key rotation and independent
                    recovery approvals are not available here.
                  </li>
                </ul>
                <p>
                  No operator session grants additional Razorpay account access.
                </p>
              </div>
            )}
            {detail !== null && (
              <section className="platform-card" aria-label="Selected evidence">
                <button onClick={() => setDetail(null)}>Close evidence</button>
                <h2>Recorded evidence and available plans</h2>
                <p>
                  Plans are explanatory; this console does not execute recovery
                  or move money.
                </p>
                <EvidenceDocument value={detail} />
              </section>
            )}
          </>
        )}
      </section>
    </main>
  );
}
function Evidence({ value }: { value: unknown }) {
  return (
    <pre className="platform-evidence">{JSON.stringify(value, null, 2)}</pre>
  );
}
