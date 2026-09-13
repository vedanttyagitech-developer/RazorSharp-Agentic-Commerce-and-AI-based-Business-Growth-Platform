'use client';
import { useState } from 'react';
import { rawCommerceCall } from '@/lib/commerce';
import { merchantCall } from './live-merchant';
type Policy = {
  binding_ok: boolean;
  binding_reason: string;
  policy_receipt_hash: string;
  checkout_version: number;
  policies: {
    kind: string;
    policy_version: number;
    terms: Record<string, unknown>;
  }[];
};
type Resolution = {
  findings: number;
  recorded_state: string;
  resolutions: {
    code: string;
    explanation: string;
    valid_until: string | null;
    recorded: boolean;
    options: {
      outcome: string;
      amount: { display: string; currency: string };
      confirmation: string;
      basis: string;
    }[];
    withheld: { outcome: string; detail: string }[];
  }[];
};
export function OrderTerms({
  orderId,
  merchant = false,
}: {
  orderId: string;
  merchant?: boolean;
}) {
  const [policy, setPolicy] = useState<Policy | null>(null),
    [resolution, setResolution] = useState<Resolution | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState('');
  async function read() {
    setBusy(true);
    setError('');
    setPolicy(null);
    setResolution(null);
    try {
      if (merchant)
        setPolicy(
          (await merchantCall(`orders/${orderId}/policy`)) as unknown as Policy,
        );
      else {
        const [p, r] = await Promise.all([
          rawCommerceCall<Policy>(`orders/${orderId}/policy`),
          rawCommerceCall<Resolution>(`orders/${orderId}/resolution`),
        ]);
        setPolicy(p);
        setResolution(r);
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="order-terms">
      <button className="secondary" disabled={busy} onClick={() => void read()}>
        {busy
          ? 'Reading sale terms…'
          : 'Read sale terms' + (merchant ? '' : ' and resolution')}
      </button>
      {error && <p role="alert">{error}</p>}
      {policy && (
        <>
          <h4>Policy-at-Sale Receipt · version {policy.checkout_version}</h4>
          <p>
            {policy.binding_ok
              ? 'Sale binding verified'
              : `Sale binding not verified: ${policy.binding_reason}`}
          </p>
          {policy.binding_ok &&
            policy.policies.map((p) => (
              <details key={p.kind}>
                <summary>
                  {p.kind} · Merchant Policy version {p.policy_version}
                </summary>
                <dl className="record-fields">
                  {Object.entries(p.terms).map(([k, v]) => (
                    <div key={k}>
                      <dt>{k.replaceAll('_', ' ')}</dt>
                      <dd>{JSON.stringify(v)}</dd>
                    </div>
                  ))}
                </dl>
              </details>
            ))}
          <details>
            <summary>Sale evidence hash</summary>
            <code>{policy.policy_receipt_hash}</code>
          </details>
        </>
      )}
      {resolution && (
        <>
          <h4>Resolution evaluation</h4>
          <p>
            {resolution.findings === 0
              ? 'The service evaluated this order and found no reconciliation findings.'
              : `${resolution.findings} recorded findings · ${resolution.recorded_state}`}
          </p>
          {resolution.resolutions.map((r, i) => (
            <article key={i}>
              <strong>{r.code}</strong>
              <p>{r.explanation}</p>
              {r.valid_until && (
                <p>
                  Evaluation valid until{' '}
                  {new Date(r.valid_until).toLocaleString()}. Refresh before
                  review.
                </p>
              )}
              {r.options.map((o, j) => (
                <div key={j}>
                  <b>
                    {o.outcome} · {o.amount.currency} {o.amount.display}
                  </b>
                  <p>
                    {o.basis} · Required confirmation: {o.confirmation}
                  </p>
                </div>
              ))}
              {r.withheld.map((o, j) => (
                <p key={j}>
                  Not offered: {o.outcome} — {o.detail}
                </p>
              ))}
            </article>
          ))}
          <p>
            These are evaluated options, not executed remedies. Raise a support
            case for merchant review; payment or refund completion requires
            verified provider evidence.
          </p>
        </>
      )}
    </section>
  );
}
