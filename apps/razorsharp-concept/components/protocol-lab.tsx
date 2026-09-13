'use client';
import { useEffect, useState } from 'react';
import { EvidenceDocument, type PlatformApi } from './platform-evidence';

type Protocol = 'ACP' | 'UCP' | 'MCP';
type Result = { status: 'passed' | 'failed' | 'not_configured'; detail: string; steps: { name: string; passed: boolean; http_status: number; response: unknown }[] };
const descriptions: Record<Protocol, { title: string; scope: string; button: string }> = {
  ACP: { title: 'Checkout authentication', scope: 'Send an unsigned retrieval request to the ACP transport and verify it refuses access. This checks the auth boundary, not a signed checkout lifecycle.', button: 'Test ACP auth boundary' },
  UCP: { title: 'Discover merchant & platform', scope: 'Fetch both published UCP profiles and inspect their public verification keys. This does not test checkout execution or external certification.', button: 'Fetch UCP profiles' },
  MCP: { title: 'Discover tools & search', scope: 'Exchange your console session for scoped access, open an MCP session, list its tools and search the real catalogue. Credentials stay on the server.', button: 'Run MCP catalogue test' },
};
export function ProtocolLab({ api, protocol: selected }: { api: PlatformApi; protocol: Protocol }) {
  const [enabled, setEnabled] = useState<Partial<Record<Protocol, boolean>>>({});
  const setup = enabled[selected] ? 'Demo testing enabled for this tenant.' : '';
  useEffect(() => {
    let active = true;
    const read = () => { void api<{enabled: Record<Protocol, boolean>}>('protocols/demo-status').then(result => {
      if (active) setEnabled(result.enabled);
    }).catch(() => { if (active) setEnabled({}); }); };
    read(); const timer = setInterval(read, 5000);
    return () => { active = false; clearInterval(timer); };
  }, [api, selected]);
  const [query, setQuery] = useState('milk');
  const [results, setResults] = useState<Partial<Record<Protocol, Result>>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const result = results[selected];
  async function enable() {
    setBusy(true); setError('');
    try {
      await api<{detail: string}>('protocols/enable-demo', 'POST', {protocol: selected});
      setEnabled((await api<{enabled: Record<Protocol, boolean>}>('protocols/demo-status')).enabled);
      setResults(previous => ({...previous, [selected]: undefined}));
    } catch (e) { setError(e instanceof Error ? e.message : 'Could not enable demo testing.'); }
    finally { setBusy(false); }
  }
  async function run() {
    setBusy(true); setError('');
    setResults(previous => ({ ...previous, [selected]: undefined }));
    try {
      const next = await api<Result>('protocols/probe', 'POST', { protocol: selected, query: query.trim() || 'milk' });
      setResults(previous => ({ ...previous, [selected]: next }));
    } catch (e) { setError(e instanceof Error ? e.message : 'Test could not finish.'); }
    finally { setBusy(false); }
  }
  return <section className="platform-card protocol-lab" aria-label="Protocol test lab">
    <p className="eyebrow">LIVE LOCAL TRANSPORT TESTS</p>
    <h2>{selected}</h2>
    <p>Run a real request and inspect each response. These probes do not submit payments or buyer approvals.</p>
    <h3>{descriptions[selected].title}</h3>
    <p>{descriptions[selected].scope}</p>
    {selected === 'MCP' && <label className="protocol-lab-query">Catalogue search<input value={query} maxLength={80} disabled={busy} onChange={event => setQuery(event.target.value)} placeholder="Try milk or a product name" /></label>}
    <p>Demo testing uses server-held credentials. Enabling it does not publish an external integration.</p>
    <button disabled={busy} onClick={() => void enable()}>{enabled[selected] === undefined ? 'Enablement status unavailable — enable or retry' : setup ? 'Demo testing enabled' : 'Enable ' + selected + ' demo testing'}</button>
    {setup && <output>{setup}</output>}
    <button disabled={busy} onClick={() => void run()}>{busy ? 'Running protocol requests…' : descriptions[selected].button}</button>
    {error && <p role="alert">{error}</p>}
    <div aria-live="polite">
      {result && <><h3>{result.status === 'passed' ? 'Probe passed' : result.status === 'not_configured' ? 'Not configured in this deployment' : 'Probe failed'}</h3><p>{result.detail}</p>
        <ol className="protocol-lab-results">{result.steps.map((step, index) => <li key={index}><strong>{step.passed ? '✓' : '×'} {step.name}</strong><span>HTTP {step.http_status}</span><details><summary>Inspect response</summary><EvidenceDocument value={step.response} /></details></li>)}</ol></>}
    </div>
  </section>;
}
