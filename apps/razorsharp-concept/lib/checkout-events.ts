/** Events invalidate displayed evidence; only a fresh backend read decides payment state. */
export function watchCheckout(
  checkoutId: string,
  refresh: () => void,
): () => void {
  if (typeof EventSource === 'undefined') return () => {};
  const stream = new EventSource(
    `/api/commerce/checkouts/${encodeURIComponent(checkoutId)}/events`,
  );
  let timer: ReturnType<typeof setTimeout> | undefined;
  const changed = () => {
    if (timer === undefined)
      timer = setTimeout(() => {
        timer = undefined;
        refresh();
      }, 150);
  };
  stream.addEventListener('timeline', changed);
  // Native EventSource reconnects with Last-Event-ID after transport interruptions.
  stream.addEventListener('complete', (event) => {
    try {
      if (JSON.parse((event as MessageEvent).data).reason === 'terminal')
        stream.close();
    } catch {
      /* Malformed control data never decides payment state. */
    }
    changed();
  });
  return () => {
    stream.close();
    if (timer !== undefined) clearTimeout(timer);
  };
}
