'use client';
import { useEffect, useState } from 'react';
type Service = {status: string; notice: string};
type Health = Record<'api'|'worker'|'voice', Service>;
export function RuntimeReadiness({api, refreshKey}: {api: <T>(path: string) => Promise<T>; refreshKey: string}) {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState('');
  const [checked, setChecked] = useState('');
  useEffect(() => {
    let active = true;
    const refresh = async () => {
      try {
        const result = await api<Health>('ops/runtime-health');
        if (active) { setHealth(result); setError(''); setChecked(new Date().toLocaleTimeString()); }
      } catch {
        if (active) { setHealth(null); setError('Runtime check unavailable. API, worker and voice health are unknown.'); }
      }
    };
    void refresh();
    const timer = setInterval(() => void refresh(), 15000);
    return () => {active = false; clearInterval(timer);};
  }, [api, refreshKey]);
  return <section className="platform-card" aria-label="Runtime readiness">
    <h2>Runtime readiness</h2>
    <p>Live checks every 15 seconds. Service health does not prove payment-provider or end-to-end voice availability.</p>
    {error && <p role="alert">{error}</p>}
    <div className="platform-stats">{(['api','worker','voice'] as const).map(name => <article key={name}>
      <span>{name === 'api' ? 'Commerce API' : name === 'worker' ? 'Execution worker' : 'Voice gateway'}</span>
      <strong>{health?.[name]?.status ?? (error ? 'unknown' : 'Checking…')}</strong>
      <small>{health?.[name]?.notice ?? 'No current health evidence.'}</small>
    </article>)}</div>
    {health && <small>Checked {checked}</small>}
  </section>;
}
