'use client';
import { useState, useEffect } from 'react';
import { merchantCall } from './live-merchant';
import type { Product } from '@/lib/demo';
import {
  buildMerchantProposal,
  type ChangeKind,
  type ProposalInput,
} from '@/lib/merchant-proposal';
export function MerchantProposalForm({
  products,
  busy,
  onSubmit,
}: {
  products: Product[];
  busy: boolean;
  onSubmit: (body: unknown) => Promise<boolean>;
}) {
  const [currentOffer, setCurrentOffer] = useState<{
    offer_id: string;
    label: string;
  } | null>(null);
  const [contextError, setContextError] = useState('');
  useEffect(() => {
    let active = true;
    merchantCall('merchant/actions/context')
      .then((value) => {
        if (active) {
          setCurrentOffer(
            (
              value as unknown as {
                offer: { offer_id: string; label: string } | null;
              }
            ).offer,
          );
          setContextError('');
        }
      })
      .catch((e) => {
        if (active) setContextError(e.message);
      });
    return () => {
      active = false;
    };
  }, [products]);
  const [input, setInput] = useState<ProposalInput>({
      kind: 'STOCK_ADJUSTMENT',
      target: '',
      value: '',
      reason: '',
      listed: true,
      label: '',
      discount: 'percent',
      starts: '',
      ends: '',
    }),
    [error, setError] = useState('');
  const update = <K extends keyof ProposalInput>(
    key: K,
    value: ProposalInput[K],
  ) => setInput((old) => ({ ...old, [key]: value }));
  const offer = input.kind.startsWith('OFFER');
  return (
    <section className="action-review">
      <h3>Propose a business change</h3>
      {contextError && (
        <p role="alert">Offer context unavailable: {contextError}</p>
      )}
      {currentOffer && (
        <p>
          Running offer: <strong>{currentOffer.label}</strong> ·{' '}
          {currentOffer.offer_id}
        </p>
      )}
      {offer && (
        <p>
          One cart-wide offer at a time. Execution starts or ends the offer
          immediately. Dates are recorded terms; they do not schedule automatic
          activation or expiry.
        </p>
      )}
      <p>
        Create a draft first. Publication always requires review, exact approval
        and execution.
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setError('');
          try {
            const body = buildMerchantProposal(input);
            void onSubmit(body).then((ok) => {
              if (ok) setInput((old) => ({ ...old, value: '', reason: '' }));
            });
          } catch (e) {
            setError((e as Error).message);
          }
        }}
      >
        <div className="proposal-grid">
          <label className="form-field">
            Change
            <select
              value={input.kind}
              disabled={busy}
              onChange={(e) =>
                setInput((old) => ({
                  ...old,
                  kind: e.target.value as ChangeKind,
                  target: e.target.value === 'OFFER_END' ? currentOffer?.offer_id ?? '' : '',
                  value: '',
                }))
              }
            >
              {Object.entries({
                STOCK_ADJUSTMENT: 'Set stock count',
                STOCK_RECEIPT: 'Receive stock',
                PRICE_CHANGE: 'Set unit price',
                LISTING_CHANGE: 'List or unlist product',
                OFFER_START: 'Start an offer',
                OFFER_END: 'End an offer',
              }).map(([k, label]) => (
                <option key={k} value={k}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label className="form-field">
            {offer ? 'Offer identifier' : 'Product'}
            {offer ? (
              <input
                required
                maxLength={128}
                value={input.target}
                onChange={(e) => update('target', e.target.value)}
              />
            ) : (
              <select
                required
                disabled={busy}
                value={input.target}
                onChange={(e) => update('target', e.target.value)}
              >
                <option value="">Choose product</option>
                {products.map((p) => (
                  <option key={p.sku} value={p.sku}>
                    {p.name} · {p.stock} units
                  </option>
                ))}
              </select>
            )}
          </label>
          {input.kind === 'LISTING_CHANGE' ? (
            <label className="form-field">
              Listing
              <select
                value={String(input.listed)}
                onChange={(e) => update('listed', e.target.value === 'true')}
              >
                <option value="true">Listed for sale</option>
                <option value="false">Unlisted</option>
              </select>
            </label>
          ) : (
            input.kind !== 'OFFER_END' && (
              <label className="form-field">
                {input.kind === 'PRICE_CHANGE'
                  ? 'Unit price (paise)'
                  : input.kind === 'OFFER_START'
                    ? input.discount === 'percent'
                      ? 'Discount (basis points; 100 = 1%)'
                      : 'Discount (paise)'
                    : 'Units'}
                <input
                  required
                  type="number"
                  min="0"
                  step="1"
                  value={input.value}
                  onChange={(e) => update('value', e.target.value)}
                />
              </label>
            )
          )}
          {input.kind === 'OFFER_START' && (
            <>
              <label className="form-field">
                Offer name
                <input
                  required
                  value={input.label}
                  onChange={(e) => update('label', e.target.value)}
                />
              </label>
              <label className="form-field">
                Discount type
                <select
                  value={input.discount}
                  onChange={(e) =>
                    update('discount', e.target.value as 'percent' | 'flat')
                  }
                >
                  <option value="percent">Percentage</option>
                  <option value="flat">Fixed amount</option>
                </select>
              </label>
              <label className="form-field">
                Starts (your local time)
                <input
                  required
                  type="datetime-local"
                  value={input.starts}
                  onChange={(e) => update('starts', e.target.value)}
                />
              </label>
              <label className="form-field">
                Ends (your local time)
                <input
                  required
                  type="datetime-local"
                  value={input.ends}
                  onChange={(e) => update('ends', e.target.value)}
                />
              </label>
            </>
          )}
          <label className="form-field">
            Reason
            <input
              maxLength={1000}
              value={input.reason}
              onChange={(e) => update('reason', e.target.value)}
            />
          </label>
        </div>
        {error && <p role="alert">{error}</p>}
        <button className="primary" disabled={busy}>
          Create draft
        </button>
      </form>
    </section>
  );
}
