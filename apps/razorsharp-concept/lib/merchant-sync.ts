/** Invalidations carry no catalogue data or credentials. Readers refetch the API. */
export const MERCHANT_STATE_CHANGED = 'razorsharp:merchant-state-changed';
export function merchantStateChanged() {
  window.dispatchEvent(new Event(MERCHANT_STATE_CHANGED));
  try { window.localStorage.setItem(MERCHANT_STATE_CHANGED, `${Date.now()}:${Math.random()}`); } catch { /* Storage may be blocked. Focus still refreshes readers. */ }
}
export function subscribeMerchantChanges(refresh: () => void) {
  const storage = (event: StorageEvent) => { if (event.key === MERCHANT_STATE_CHANGED) refresh(); };
  window.addEventListener(MERCHANT_STATE_CHANGED, refresh);
  window.addEventListener('storage', storage);
  window.addEventListener('focus', refresh);
  return () => {
    window.removeEventListener(MERCHANT_STATE_CHANGED, refresh);
    window.removeEventListener('storage', storage);
    window.removeEventListener('focus', refresh);
  };
}
