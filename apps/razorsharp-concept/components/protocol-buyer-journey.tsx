'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { commerce, rawCommerceCall, type ApprovalCard, type ProductCard, type CheckoutView } from '@/lib/commerce';
import { basketWasRejected, retireForFreshReview } from '@/lib/protocol-buyer-recovery';
import { canRefreshCheckout, isSpentCheckout } from '@/lib/checkout-recovery';
import { ManualCheckout } from './manual-checkout';
import { EvidenceDocument } from './platform-evidence';

type Protocol = 'ACP' | 'UCP';
type Draft = { key: string; items: {sku: string; quantity: number}[]; card?: ApprovalCard };
type Status = { checkout: CheckoutView; protocol_response: unknown };
const noop = () => {};
const money = (minor: number, currency = 'INR') => new Intl.NumberFormat('en-IN', {style: 'currency', currency}).format(minor / 100);

export function ProtocolBuyerJourney({ protocol }: {protocol: Protocol}) {
  const generation = useRef(0);
  const storage = `protocol-buyer:${protocol}`;
  const [resumeId, setResumeId] = useState(() => {
    if (typeof window === 'undefined') return null;
    const params = new URLSearchParams(window.location.search);
    return params.get('buyerProtocol') === protocol ? params.get('checkout') : null;
  });
  const [query, setQuery] = useState('');
  const [products, setProducts] = useState<ProductCard[]>([]);
  const [knownProducts, setKnownProducts] = useState<Record<string, ProductCard>>({});
  const [cursor, setCursor] = useState<string | null>(null);
  const [searched, setSearched] = useState(false);
  const [quantities, setQuantities] = useState<Record<string, number>>({});
  const [draft, setDraft] = useState<Draft | null>(null);
  const [ready, setReady] = useState(false);
  const [status, setStatus] = useState<Status | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [pay, setPay] = useState(false);
  const [order, setOrder] = useState<unknown>(null);
  useEffect(() => {
    let active = true;
    queueMicrotask(() => {
      if (!active) return;
      try { const saved = sessionStorage.getItem(storage); if (saved) { const parsed = JSON.parse(saved); if (!resumeId || parsed.card?.checkout_id === resumeId) setDraft(parsed); } }
      catch { setError('Saved journey could not be restored. Check your buyer orders before starting again.'); }
      setReady(true);
    });
    return () => {active = false;};
  }, [storage, resumeId]);
  function save(next: Draft) { sessionStorage.setItem(storage, JSON.stringify(next)); setDraft(next); }
  function remember(items: ProductCard[]) {
    setKnownProducts(previous => ({...previous, ...Object.fromEntries(items.map(item => [item.sku, item]))}));
  }
  async function discover(more = false, browse = false) {
    setBusy(true); setError('');
    try {
      if (!more && !browse && query.trim()) {
        const result = await commerce.catalogue.search(query.trim(), {limit: 20});
        setProducts(result.hits); remember(result.hits); setCursor(null); setSearched(true);
      } else {
        const result = await commerce.catalogue.list({limit: 20, cursor: more ? cursor : undefined});
        setProducts(previous => more ? [...previous.filter(p => !result.products.some(next => next.sku === p.sku)), ...result.products] : result.products);
        remember(result.products); setCursor(result.next_cursor ?? null); setSearched(false);
      }
    }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  const refresh = useCallback(async () => {
    const checkoutId = draft?.card?.checkout_id || resumeId;
    if (!checkoutId) return;
    const epoch = generation.current;
    const next = await rawCommerceCall<Status>(`buyer-protocols/${protocol}/checkouts/${checkoutId}`);
    if (epoch !== generation.current) return;
    setStatus(next);
    if (next.checkout.approval_card && next.checkout.approval_card.content_hash !== draft?.card?.content_hash) {
      const updated = {...(draft ?? {key: crypto.randomUUID(), items: []}), card: next.checkout.approval_card};
      sessionStorage.setItem(storage, JSON.stringify(updated)); setDraft(updated); setPay(false);
      setMessage('Checkout changed. Review the new version before approving.');
    }
    if (next.checkout.order_id) {
      const confirmed = await commerce.orders.read(next.checkout.order_id);
      if (epoch === generation.current) setOrder(confirmed);
    }
  }, [draft, protocol, storage, resumeId]);
  useEffect(() => {
    if (!draft?.card && !resumeId) return;
    let active = true;
    const poll = () => { void refresh().catch(e => { if (active) setError((e as Error).message); }); };
    poll(); const timer = setInterval(poll, 5000);
    return () => {active = false; clearInterval(timer);};
  }, [draft, refresh, resumeId]);
  async function checkout() {
    setBusy(true); setError('');
    const next = draft ?? {key: crypto.randomUUID(), items: Object.entries(quantities).filter(([,quantity]) => quantity > 0).map(([sku,quantity]) => ({sku,quantity}))};
    try {
      save(next); // Persist the same request before sending; a lost response is retryable.
      const result = await rawCommerceCall<{card: ApprovalCard}>(`buyer-protocols/${protocol}/checkouts`, {method: 'POST', body: {items: next.items}, idempotencyKey: next.key});
      if (!result.card) throw new Error('An existing checkout needs recovery. Open Buyer storefront to review its payment status.');
      save({...next, card: result.card});
    } catch (e) {
      if (basketWasRejected(e)) {
        sessionStorage.removeItem(storage); setDraft(null);
        setQuantities(Object.fromEntries(next.items.map(item => [item.sku, item.quantity])));
        setMessage('Basket was rejected. Adjust the items or quantities and review again.');
      }
      setError((e as Error).message);
    }
    finally {setBusy(false);}
  }
  function resetJourney(items: {sku: string; quantity: number}[] = []) {
    generation.current += 1;
    sessionStorage.removeItem(storage);
    const url = new URL(window.location.href);
    if (url.searchParams.get('buyerProtocol') === protocol) {
      url.searchParams.delete('buyerProtocol'); url.searchParams.delete('checkout');
      window.history.replaceState(null, '', url);
    }
    setResumeId(null); setDraft(null); setStatus(null); setOrder(null); setPay(false);
    setError(''); setProducts([]); setKnownProducts({}); setCursor(null); setSearched(false);
    setQuantities(Object.fromEntries(items.map(item => [item.sku, item.quantity])));
  }
  async function freshReview() {
    const checkoutId = draft?.card?.checkout_id || resumeId;
    if (!checkoutId || busy) return;
    setBusy(true);
    try {
      let items: {sku: string; quantity: number}[] = [];
      if (draft?.card) items = await retireForFreshReview(draft.card);
      else {
        const latest = await commerce.checkout.read(checkoutId);
        if (!isSpentCheckout(latest)) throw Error('This checkout is not safely closed. Refresh its payment status before starting again.');
      }
      const refreshed: ProductCard[] = [];
      let next: string | null | undefined;
      do {
        const page = await commerce.catalogue.list({limit: 100, cursor: next});
        refreshed.push(...page.products); next = page.next_cursor;
      } while (next);
      resetJourney(items);
      setProducts(refreshed); remember(refreshed);
      setMessage('Stock refreshed. Adjust your basket, then create a new checkout to review the latest total.');
    } finally { setBusy(false); }
  }
  const card = draft?.card;
  return <section className="platform-card protocol-buyer" aria-label={`${protocol} buyer journey`}>
    <h2>{protocol} buyer checkout</h2>
    <p>1. Discover products → 2. Build basket → 3. Review exact checkout → 4. Approve and pay → 5. Verify order</p>
    <p>This uses your separate buyer session. The console operator does not approve for the buyer. {protocol === 'ACP' ? 'Checkout creation runs through the signed ACP transport.' : 'UCP line items map into the real checkout; payment escalates to the trusted buyer surface.'}</p>
    {!card && !resumeId && <>
      <label className="protocol-lab-query">Find products<input value={query} maxLength={80} disabled={busy || !!draft} onChange={event => setQuery(event.target.value)} placeholder="Search the real catalogue" /></label>
      <button disabled={busy || !ready || !!draft} onClick={() => void discover()}>Discover products</button>
      {searched && <p>Showing up to 20 ranked search matches. <button disabled={busy || !!draft} onClick={() => void discover(false, true)}>Browse full catalogue</button></p>}
      <div className="protocol-products">{products.map(product => <label key={product.sku}><strong>{product.display_name}</strong><span>{money(product.unit_price_minor, product.currency)} · {product.stock_units} available</span><input aria-label={`Quantity of ${product.display_name}`} type="number" min={0} max={Math.min(20, product.stock_units)} disabled={busy || !!draft || !product.is_available || !product.is_listed} value={quantities[product.sku] ?? 0} onChange={e => setQuantities(previous => ({...previous, [product.sku]: Math.max(0, Math.min(20, Number(e.target.value) || 0))}))} /></label>)}</div>
      {cursor && <button disabled={busy || !!draft} onClick={() => void discover(true)}>Load more products</button>}
      <section aria-label="Selected basket">
        <h3>Selected basket</h3>
        {Object.entries(quantities).filter(([,quantity]) => quantity > 0).map(([sku, quantity]) => {
          const product = knownProducts[sku];
          return <div key={sku}>
            <strong>{product?.display_name ?? sku}</strong>
            <p>{!product ? 'Current product details unavailable. Remove this item or discover it again.' : !product.is_listed ? 'Unlisted — remove or replace this item.' : !product.is_available ? 'Unavailable — remove or replace this item.' : `${product.stock_units} available`}</p>
            <input aria-label={`Basket quantity of ${product?.display_name ?? sku}`} type="number" min={0} max={20} value={quantity} disabled={busy || !!draft} onChange={e => setQuantities(previous => ({...previous, [sku]: Math.max(0, Math.min(20, Number(e.target.value) || 0))}))} />
            <button disabled={busy || !!draft} onClick={() => setQuantities(previous => ({...previous, [sku]: 0}))}>Remove {product?.display_name ?? sku}</button>
          </div>;
        })}
      </section>
      {draft && <p>Checkout request saved. Retry recovers the same request; it does not start another purchase.</p>}
      <button disabled={busy || !ready || (!draft && !Object.values(quantities).some(q => q > 0))} onClick={() => void checkout()}>{busy ? 'Preparing checkout…' : draft ? 'Recover checkout request' : `Create ${protocol} checkout & review`}</button>
    </>}
    {card && <>
      <h3>Exact checkout · version {card.version}</h3>
      <ul>{card.quote.lines.map(line => <li key={line.sku}>{line.name} × {line.quantity} · {money(line.subtotal_minor, card.currency)}</li>)}</ul>
      <strong>Total including fees and tax: {money(card.amount_minor, card.currency)}</strong>
      <p>Approval binds this amount, currency, checkout version and content hash.</p>
      <details><summary>Inspect exact approval binding</summary><EvidenceDocument value={{checkout_id: card.checkout_id, version: card.version, content_hash: card.content_hash, amount_minor: card.amount_minor, currency: card.currency, quote: card.quote}} /></details>
      <button onClick={() => void refresh().catch(e => setError((e as Error).message))}>Refresh payment / order status</button>
      <p>{status?.checkout.order_id ? 'Order confirmed from verified provider evidence.' : `Recorded state: ${status?.checkout.state ?? 'Loading'}. Pending or uncertain payment is not success.`}</p>
      {!status?.checkout.order_id && !pay && !['EXPIRED', 'CANCELLED', 'INVALIDATED'].includes(status?.checkout.state ?? '') && <button onClick={() => setPay(true)}>Continue to buyer approval & Razorpay</button>}
      {pay && !status?.checkout.order_id && <ManualCheckout key={card.content_hash} card={card} onConfirmed={() => {setPay(false); void refresh().catch(e => setError((e as Error).message));}} onBack={() => setPay(false)} onGuidance={setMessage} onVoiceStage={noop} onFreshReview={freshReview} onReviewChanged={() => {setPay(false); void refresh().catch(e => setError((e as Error).message));}} />}
      {status && <details><summary>{protocol} lifecycle response</summary><EvidenceDocument value={status.protocol_response} /></details>}
      {order !== null && <><h3>Verified order</h3><EvidenceDocument value={order} /></>}
    </>}
    {!card && resumeId && <><p>Restoring existing checkout {resumeId}. No new purchase will be created.</p><button onClick={() => void refresh().catch(e => setError((e as Error).message))}>Refresh existing checkout</button>{status && <EvidenceDocument value={status} />}{order !== null && <EvidenceDocument value={order} />}</>}
    {status?.checkout.order_id && <button disabled={busy} onClick={() => { resetJourney(); setMessage('Previous order is saved in Buyer orders. Discover products to start another purchase.'); }}>Start another purchase</button>}
    {status && !status.checkout.order_id && canRefreshCheckout(status.checkout) && <button disabled={busy} onClick={() => void freshReview().catch(e => setError((e as Error).message))}>Refresh stock and review again</button>}
    {message && <p>{message}</p>}{error && <p role="alert">{error}</p>}
  </section>;
}
