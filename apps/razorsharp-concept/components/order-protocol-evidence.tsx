'use client';
import { useState } from 'react';
import { rawCommerceCall } from '@/lib/commerce';
type Interaction = {
  interaction_id: string;
  protocol: string;
  protocol_version: string;
  started_at: string;
};
export function OrderProtocolEvidence({ checkoutId }: { checkoutId: string }) {
  const [items, setItems] = useState<Interaction[] | null>(null),
    [detail, setDetail] = useState<unknown>(null),
    [error, setError] = useState(''),
    [busy, setBusy] = useState(false);
  async function read(id?: string) {
    setBusy(true);
    setError('');
    setDetail(null);
    try {
      if (id) setDetail(await rawCommerceCall(`inspector/protocols/${id}`));
      else
        setItems(
          (
            await rawCommerceCall<{ interactions: Interaction[] }>(
              `checkouts/${checkoutId}/protocol-evidence`,
            )
          ).interactions,
        );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="order-terms">
      <button className="secondary" disabled={busy} onClick={() => void read()}>
        Read checkout protocol evidence
      </button>
      {error && <p role="alert">{error}</p>}
      {items && (
        <>
          <h4>Recorded protocol interactions</h4>
          {!items.length && (
            <p>
              No protocol interaction was recorded for this checkout. A direct
              storefront checkout does not imply an external protocol
              interaction.
            </p>
          )}
          {items.map((i) => (
            <div key={i.interaction_id}>
              <p>
                {i.protocol} {i.protocol_version} ·{' '}
                {new Date(i.started_at).toLocaleString()}
              </p>
              <button
                className="secondary"
                disabled={busy}
                onClick={() => void read(i.interaction_id)}
              >
                Inspect recorded interaction
              </button>
            </div>
          ))}
        </>
      )}
      {detail !== null && (
        <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
          {JSON.stringify(detail, null, 2)}
        </pre>
      )}
    </section>
  );
}
