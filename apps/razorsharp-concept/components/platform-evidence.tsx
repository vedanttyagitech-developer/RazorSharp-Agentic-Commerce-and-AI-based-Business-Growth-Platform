'use client';
import { useState } from 'react';
export type PlatformApi = <T = unknown>(
  path: string,
  method?: string,
  body?: unknown,
) => Promise<T>;
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const labels: Record<string, string> = {
  attempt: 'Payment attempt',
  audit: 'Audit hash chain',
  revenue: 'Stale-approval financial evidence',
  protocol: 'Protocol interaction',
  checkout: 'Checkout proof',
};
export function EvidenceExplorer({
  api,
  merchantId,
}: {
  api: PlatformApi;
  merchantId: string;
}) {
  const [kind, setKind] = useState('attempt'),
    [id, setId] = useState(''),
    [checkout, setCheckout] = useState(''),
    [aggregate, setAggregate] = useState('checkout'),
    [data, setData] = useState<unknown>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState('');
  async function read() {
    setBusy(true);
    setError('');
    setData(null);
    try {
      const path =
        kind === 'attempt'
          ? `inspector/payment-attempts/${id}`
          : kind === 'audit'
            ? `audit/streams/${aggregate}/${id}/verify`
            : kind === 'revenue'
              ? `merchants/${id}/evidence/retained-revenue${checkout ? '?checkout_id=' + checkout : ''}`
              : kind === 'protocol'
                ? `inspector/protocols/${id}`
                : `checkouts/${id}/proof`;
      setData(await api(path));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="platform-card">
      <span className="platform-eyebrow">VERIFY RECORDED EVIDENCE</span>
      <h2>Follow the transaction</h2>
      <p>
        Inspect committed records, verify an audit chain, or measure a
        controlled stale-approval scenario. Missing evidence stays unavailable.
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void read();
        }}
        className="evidence-form"
      >
        <label>
          Evidence
          <select
            value={kind}
            onChange={(e) => {
              setKind(e.target.value);
              if (e.target.value === 'revenue') setId(merchantId);
              setData(null);
              setError('');
            }}
          >
            {Object.entries(labels).map(([key, label]) => (
              <option value={key} key={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
        {kind === 'audit' && (
          <label>
            Aggregate
            <select
              value={aggregate}
              onChange={(e) => setAggregate(e.target.value)}
            >
              {[
                'checkout',
                'payment_attempt',
                'webhook_inbox',
                'merchant',
                'protocol_interaction',
              ].map((v) => (
                <option key={v}>{v}</option>
              ))}
            </select>
          </label>
        )}
        <label>
          {kind === 'revenue'
            ? 'Merchant identifier'
            : kind === 'checkout'
              ? 'Checkout identifier'
              : kind === 'audit'
                ? 'Aggregate identifier'
                : kind === 'protocol'
                  ? 'Interaction identifier'
                  : 'Payment attempt identifier'}
          <input
            required
            value={id}
            onChange={(e) => setId(e.target.value.trim())}
            placeholder="UUID from recorded evidence"
          />
        </label>
        {kind === 'revenue' && (
          <label>
            Checkout identifier (optional)
            <input
              value={checkout}
              onChange={(e) => setCheckout(e.target.value.trim())}
            />
          </label>
        )}
        <button
          disabled={
            busy ||
            !uuid.test(id) ||
            (kind === 'revenue' && !!checkout && !uuid.test(checkout))
          }
        >
          {busy ? 'Verifying…' : 'Read evidence'}
        </button>
      </form>
      {error && <p role="alert">{error}</p>}
      {data !== null && <EvidenceDocument value={data} />}
    </section>
  );
}
export function EvidenceDocument({ value }: { value: unknown }) {
  if (!value || typeof value !== 'object')
    return (
      <pre className="platform-evidence">{JSON.stringify(value, null, 2)}</pre>
    );
  const data = value as Record<string, unknown>;
  const verdict = data.verdict as { ok?: boolean; tier?: string } | undefined;
  return (
    <>
      <div className="evidence-summary">
        {[
          'state',
          'status',
          'code',
          'explanation',
          'binding_reason',
          'direction',
          'currency',
          'captured_minor',
          'refunded_minor',
          'net_retained_minor',
          'controlled_scenario',
          'intact',
          'ok',
          'first_break',
        ]
          .filter((k) => data[k] !== undefined)
          .map((k) => (
            <div key={k}>
              <span>{k.replaceAll('_', ' ')}</span>
              <strong>{String(data[k])}</strong>
            </div>
          ))}
        {verdict && (
          <div>
            <span>Verification tier</span>
            <strong>
              {verdict.tier} · {verdict.ok ? 'Passed' : 'Not verified'}
            </strong>
          </div>
        )}
      </div>
      {Object.entries(data)
        .filter(([, v]) => Array.isArray(v))
        .map(([name, entries]) => (
          <section className="evidence-block" key={name}>
            <h3>
              {name.replaceAll('_', ' ')}{' '}
              <small>{(entries as unknown[]).length} records</small>
            </h3>
            {(entries as unknown[]).length === 0 ? (
              <p>No records returned.</p>
            ) : (
              (entries as unknown[]).map((entry, i) => {
                const row =
                  entry && typeof entry === 'object'
                    ? (entry as Record<string, unknown>)
                    : {};
                const label =
                  row.summary ??
                  row.explanation ??
                  row.state ??
                  row.status ??
                  row.code ??
                  row.action ??
                  row.type ??
                  `Record ${i + 1}`;
                return (
                  <details key={i}>
                    <summary>
                      {typeof label === 'string'
                        ? label
                        : JSON.stringify(label)}
                    </summary>
                    <pre className="platform-evidence">
                      {JSON.stringify(entry, null, 2)}
                    </pre>
                  </details>
                );
              })
            )}
          </section>
        ))}
      <details>
        <summary>Complete evidence document</summary>
        <pre className="platform-evidence">
          {JSON.stringify(value, null, 2)}
        </pre>
      </details>
    </>
  );
}
export function ProtocolEvidence({ api }: { api: PlatformApi }) {
  const [data, setData] = useState<unknown>(null),
    [error, setError] = useState(''),
    [busy, setBusy] = useState(false);
  async function read() {
    setBusy(true);
    setError('');
    setData(null);
    try {
      const [matrix, conformance] = await Promise.all([
        api('protocols'),
        api('protocols/conformance'),
      ]);
      setData({ matrix, conformance });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="platform-card">
      <h2>Protocol configuration and claim boundaries</h2>
      <p>
        Read this backend’s pins and disclaimers. This is not external
        certification or a newly executed conformance test.
      </p>
      <button disabled={busy} onClick={() => void read()}>
        Read protocol configuration
      </button>
      {error && <p role="alert">{error}</p>}
      {data !== null && (
        <>
          <div className="platform-table">
            <table>
              <thead>
                <tr>
                  <th>Protocol</th>
                  <th>Version</th>
                  <th>Claim boundary</th>
                  <th>Qualification</th>
                </tr>
              </thead>
              <tbody>
                {(
                  data as {
                    matrix: {
                      pins: {
                        protocol: string;
                        version: string;
                        claim_boundary: string;
                        disclaimer: string;
                      }[];
                    };
                  }
                ).matrix.pins.map((pin) => (
                  <tr key={pin.protocol}>
                    <td>{pin.protocol}</td>
                    <td>{pin.version}</td>
                    <td>{pin.claim_boundary}</td>
                    <td>{pin.disclaimer}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <EvidenceDocument value={data} />
        </>
      )}
    </section>
  );
}
export function MetricsEvidence() {
  const [text, setText] = useState(''),
    [error, setError] = useState(''),
    [busy, setBusy] = useState(false),
    [query, setQuery] = useState('');
  async function read() {
    setBusy(true);
    setError('');
    try {
      const r = await fetch('/api/platform/ops/metrics', {
        credentials: 'same-origin',
        cache: 'no-store',
      });
      if (!r.ok) throw Error('Metrics unavailable (' + r.status + ')');
      setText(await r.text());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const rows = text
    .split('\n')
    .filter(
      (line) =>
        line &&
        !line.startsWith('#') &&
        line.toLowerCase().includes(query.toLowerCase()),
    );
  return (
    <section className="platform-card">
      <h2>Process metrics</h2>
      <p>
        Live Prometheus exposition from this API process. Counters reset with
        the process; this view does not aggregate replicas or estimate capacity.
      </p>
      <button disabled={busy} onClick={() => void read()}>
        {busy ? 'Reading…' : 'Read current metrics'}
      </button>
      {error && <p role="alert">{error}</p>}
      {text && (
        <>
          <label>
            Filter instruments
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="admission, grant, latency…"
            />
          </label>
          <p>{rows.length} matching series</p>
          <div className="platform-table">
            <table>
              <thead>
                <tr>
                  <th>Instrument and labels</th>
                  <th>Value</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((line, i) => {
                  const split = line.lastIndexOf(' ');
                  return (
                    <tr key={i}>
                      <td>
                        <code>{line.slice(0, split)}</code>
                      </td>
                      <td>{line.slice(split + 1)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {!rows.length && <p>No matching series recorded.</p>}
          </div>
          <details>
            <summary>Metric definitions and raw exposition</summary>
            <pre className="platform-evidence">{text}</pre>
          </details>
        </>
      )}
    </section>
  );
}
export function ReviveCommand({
  command,
  api,
  onUpdated,
}: {
  command: Record<string, unknown>;
  api: PlatformApi;
  onUpdated: () => Promise<void>;
}) {
  const id =
    typeof command.command_id === 'string'
      ? command.command_id
      : typeof command.id === 'string'
        ? command.id
        : '';
  const [open, setOpen] = useState(false),
    [confirm, setConfirm] = useState(''),
    [busy, setBusy] = useState(false),
    [message, setMessage] = useState('');
  if (command.status !== 'DEAD') return null;
  async function revive() {
    setBusy(true);
    setMessage('');
    try {
      const result = await api<{ code: string; status: string }>(
        `ops/outbox/${id}/revive`,
        'POST',
        { confirm },
      );
      setMessage(
        `${result.code} · ${result.status}. Only the existing command was requested for recovery.`,
      );
      setOpen(false);
      await onUpdated();
    } catch (e) {
      setMessage(
        (e as Error).message +
          ' Check current command state before trying again.',
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="command-recovery">
      <button onClick={() => setOpen((v) => !v)} disabled={busy}>
        Review dead-command recovery
      </button>
      {open && (
        <>
          <h3>Requeue this exact command</h3>
          <p>
            This can resume an existing money operation. It preserves the
            original payload and grant; it does not create a new payment
            attempt. The backend refuses commands that are no longer DEAD.
          </p>
          <code>{id}</code>
          <label>
            Enter the command identifier to confirm
            <input
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
            />
          </label>
          <button
            disabled={busy || confirm !== id}
            onClick={() => void revive()}
          >
            {busy ? 'Requesting recovery…' : 'Revive existing command'}
          </button>
        </>
      )}
      {message && <output>{message}</output>}
    </section>
  );
}
