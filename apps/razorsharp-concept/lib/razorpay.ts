// Razorpay Standard Checkout, loaded into this document and opened on the buyer's own click.
//
// Why the provider's own screen and not a form of ours: specification 2.4. Card numbers,
// UPI PINs and OTPs are entered on Razorpay's surface, inside Razorpay's iframe, and never
// reach this application, its state, its logs or its backend. The platform's own evidence
// rules depend on that boundary -- `commerce_protocols.core.evidence` screens PAN and CVC
// out of the immutable chain precisely because they must never have been there.
//
// Three things this module refuses to do, each of which the old preview did:
//
//   1. **It does not decide the outcome.** `handler` fires when Razorpay hands the browser
//      back a payment id. That is a *report*, not a confirmation: `POST /v1/payments/verify`
//      records it as BROWSER_CALLBACK evidence and the backend still waits for the webhook.
//      A caller that treats `handler` as success is asserting a capture nobody verified.
//   2. **It does not treat dismissal as failure.** Closing the modal leaves the attempt
//      exactly where it was -- possibly paid, in a tab the buyer switched away from. The
//      only honest reading is "outcome unknown", which is what `dismissed` means here.
//   3. **It does not compute an amount.** Everything it opens with came from the payment
//      handoff, which reads the amount the Execution Grant was issued for.

/** The fields Razorpay hands back when a payment completes in the browser. */
export type RazorpayReturn = {
  razorpay_payment_id: string;
  razorpay_order_id: string;
  razorpay_signature: string;
};

export type RazorpayHandoff = {
  keyId: string;
  orderId: string;
  amountMinor: number;
  currency: string;
  merchantName: string;
  description: string;
  remainingMs?: number;
};

/** How the modal ended. `paid` is a browser report; only the backend can confirm it. */
export type RazorpayOutcome =
  | { kind: 'reported'; report: RazorpayReturn }
  | { kind: 'dismissed' }
  | { kind: 'failed'; code: string; description: string };

export class RazorpayUnavailableError extends Error {
  constructor(readonly cause_: 'blocked' | 'offline') {
    super(
      cause_ === 'blocked'
        ? "Razorpay's checkout script did not load. An extension, a content blocker or a " +
            'content-security policy is refusing it.'
        : 'Razorpay could not be reached. Payment status is not confirmed; recover this same checkout before trying again.',
    );
    this.name = 'RazorpayUnavailableError';
  }
}

const SCRIPT = 'https://checkout.razorpay.com/v1/checkout.js';

type RazorpayInstance = { open: () => void; close: () => void };
type RazorpayGlobal = new (options: Record<string, unknown>) => RazorpayInstance;

declare global {
  interface Window {
    Razorpay?: RazorpayGlobal;
  }
}

/**
 * Marks a provider surface this module has put out of the way but deliberately NOT removed.
 */
const NEUTRALISED = 'data-rs-provider-surface-neutralised';

const containers = (selector: string): HTMLElement[] =>
  typeof document === 'undefined'
    ? []
    : (Array.from(document.querySelectorAll(selector)) as HTMLElement[]);

/**
 * Stop a provider surface swallowing the page, WITHOUT taking it out of the document.
 *
 * `instance.close()` is Razorpay's own teardown, and on the surface the escape control
 * exists for -- a checkout whose script never finished initialising -- it does nothing. What
 * survives is `.razorpay-container`: fixed, the full viewport, opaque, `pointer-events:auto`,
 * at the maximum z-index, with the body still scroll-locked. The page underneath then renders
 * perfectly and cannot be touched: `elementFromPoint` over the recovery screen's own "Resume
 * this Razorpay checkout" button returns the provider's iframe rather than the button.
 *
 * An earlier version of this function fixed that by REMOVING the container. That was wrong,
 * and wrong in a way worth recording, because it looked correct in the browser and passed a
 * test. checkout.js keeps its own reference to the node it created, and there is no
 * documented teardown API that says otherwise. Take the node out of the document and the
 * next `open()` on the same order does not merely render blank -- in Chrome 152 it spins the
 * main thread and the tab stops responding entirely. Reproduced twice, with and without
 * instrumentation, on `order_TaOywd8WlrfJZY`.
 *
 * So the node stays exactly where the provider put it, and only two inline properties change:
 * enough for the buyer to reach the page again, and nothing the SDK cannot undo. Both are set
 * WITHOUT `!important` on purpose -- if checkout.js writes its own `display` when it reopens,
 * the later inline write simply wins, and this module has not fought it.
 *
 * This touches nothing outside the document. The attempt, its Execution Grant and its
 * provider order are untouched and still recoverable, which is the entire point of leaving by
 * this door rather than by starting a second payment.
 */
function neutraliseProviderSurface(): void {
  for (const container of containers('.razorpay-container')) {
    container.setAttribute(NEUTRALISED, '');
    container.style.display = 'none';
    container.style.pointerEvents = 'none';
  }
  const style = document.body?.style;
  if (!style?.removeProperty) return;
  // Razorpay locks the page while its modal is up and unlocks it in the teardown that did
  // not run.
  style.removeProperty('overflow');
  style.removeProperty('contain');
}

/**
 * Undo a neutralisation, so a surface the SDK reuses is visible when it reopens.
 *
 * Called before every open. If checkout.js builds a fresh container instead of reusing the
 * one it left, this is a no-op on a node nothing will look at again -- and `keepOneInteractiveSurface`
 * puts that node back out of the way once the new one exists.
 */
function restoreProviderSurface(): void {
  for (const container of containers(`[${NEUTRALISED}]`)) {
    container.removeAttribute(NEUTRALISED);
    container.style.removeProperty('display');
    container.style.removeProperty('pointer-events');
  }
}

/**
 * At most one provider surface may take clicks.
 *
 * If the SDK made a new container rather than reusing the old one, the old one is now a
 * restored, empty, full-viewport element at the maximum z-index -- the click-swallowing state
 * all of this exists to prevent, just with a different node in it. The newest container is
 * the live one; every earlier one goes back out of the way.
 */
function keepOneInteractiveSurface(): void {
  const all = containers('.razorpay-container');
  for (const stale of all.slice(0, -1)) {
    stale.setAttribute(NEUTRALISED, '');
    stale.style.display = 'none';
    stale.style.pointerEvents = 'none';
  }
}

let loading: Promise<RazorpayGlobal> | null = null;
let activeCheckout: {orderId: string; result: Promise<RazorpayOutcome>} | null = null;

/**
 * Load `checkout.js` once per document and hand back the constructor.
 *
 * Memoised on the promise rather than on a boolean: two buttons clicked in the same tick
 * would otherwise append two script tags, and the second `onload` would resolve against a
 * global the first had already installed.
 */
export function loadRazorpay(): Promise<RazorpayGlobal> {
  if (typeof window === 'undefined')
    return Promise.reject(new RazorpayUnavailableError('blocked'));
  if (window.Razorpay) return Promise.resolve(window.Razorpay);
  if (loading) return loading;

  loading = new Promise<RazorpayGlobal>((resolve, reject) => {
    const tag = document.createElement('script');
    const timeout = window.setTimeout(() => {
      tag.remove();
      reject(new RazorpayUnavailableError('offline'));
    }, 20_000);
    tag.src = SCRIPT;
    tag.async = true;
    tag.onload = () => {
      window.clearTimeout(timeout);
      // Loaded but no global: something served a different body at that URL. Treated as
      // blocked rather than retried, because retrying fetches the same wrong thing.
      if (window.Razorpay) resolve(window.Razorpay);
      else reject(new RazorpayUnavailableError('blocked'));
    };
    tag.onerror = () => {
      window.clearTimeout(timeout);
      tag.remove();
      reject(new RazorpayUnavailableError('offline'));
    };
    document.head.appendChild(tag);
  }).catch(error => { loading = null; throw error; });
  return loading;
}

/** True for a Razorpay test-mode key. The key id is public and says which mode it is. */
export function isTestKey(keyId: string): boolean {
  return keyId.startsWith('rzp_test_');
}

/**
 * Open Razorpay Checkout and resolve with how it ended.
 *
 * Resolves exactly once. Razorpay can fire `payment.failed` and then `ondismiss` for one
 * modal, and a caller that saw both would report a failure and then an unknown for the same
 * attempt -- so the first outcome wins and the rest are dropped.
 */
export async function openRazorpay(handoff: RazorpayHandoff): Promise<RazorpayOutcome> {
  const openedAt=performance.now();
  if(handoff.remainingMs!==undefined&&handoff.remainingMs<=0)throw new Error("Payment window closed. Check payment status.");
  const Razorpay = await loadRazorpay();
  // A surface left neutralised by an earlier escape may be the very one checkout.js is about
  // to reuse. Make it visible again before it is asked to open, never after.
  restoreProviderSurface();
  if (activeCheckout) {
    if (activeCheckout.orderId === handoff.orderId) return activeCheckout.result;
    throw new Error('Another Razorpay checkout is already open. Return to its payment status before opening a different purchase.');
  }
  const result = new Promise<RazorpayOutcome>((resolve, reject) => {
    let settled = false;
    let windowTimer: ReturnType<typeof setTimeout> | undefined;
    let escapeTimer: ReturnType<typeof setTimeout> | undefined;
    let returnButton: HTMLButtonElement | undefined;
    let deadlineBadge: HTMLDivElement | undefined;
    let badgeTimer: ReturnType<typeof setInterval> | undefined;
    const cleanup = () => {
      if (escapeTimer !== undefined) clearTimeout(escapeTimer);
      returnButton?.remove();
      deadlineBadge?.remove();
      if(badgeTimer!==undefined)clearInterval(badgeTimer);
      if(windowTimer!==undefined)clearTimeout(windowTimer);
    };
    const settle = (outcome: RazorpayOutcome) => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(outcome);
    };

    const instance = new Razorpay({
      key: handoff.keyId,
      // `order_id` is what binds this modal to the order the worker created under the
      // Execution Grant. Without it Razorpay would happily take a payment against no order,
      // and no webhook could ever be matched back to this checkout.
      order_id: handoff.orderId,
      amount: handoff.amountMinor,
      currency: handoff.currency,
      name: handoff.merchantName,
      description: handoff.description,
      // Explicit demo identity, only for public Test Mode keys. Never apply to live payments.
      ...(isTestKey(handoff.keyId) ? {
        prefill: { name: 'Vedant Tyagi', contact: '+919876543210' },
        notes: { demo_billed_to: 'Vedant Tyagi', demo_identity: 'test_mode_only' },
      } : {}),
      // Account settings may still require contact; the demo prefill supplies it.
      hidden: { contact: true },
      retry: { enabled: false },
      handler: (report: RazorpayReturn) => settle({ kind: 'reported', report }),
      modal: { ondismiss: () => settle({ kind: 'dismissed' }), escape: true },
      theme: { color: '#0b0b0c' },
    });

    const failed = (event: { error?: { code?: string; description?: string } }) =>
      settle({
        kind: 'failed',
        code: event?.error?.code || 'PAYMENT_FAILED',
        description: event?.error?.description || 'Razorpay reported that the payment failed.',
      });
    (instance as unknown as { on?: (name: string, fn: typeof failed) => void }).on?.(
      'payment.failed',
      failed,
    );

    try {
      const remaining=handoff.remainingMs===undefined?undefined:handoff.remainingMs-(performance.now()-openedAt);
      if(remaining!==undefined&&remaining<=0){settle({kind:"dismissed"});return;}
      instance.open();
      if(remaining!==undefined)windowTimer=setTimeout(()=>{instance.close();settle({kind:"dismissed"});neutraliseProviderSurface();},remaining);
      keepOneInteractiveSurface();
      if(!settled&&remaining!==undefined&&typeof document!=='undefined'){
        deadlineBadge=document.createElement('div');
        deadlineBadge.className='payment-window payment-window-provider';
        deadlineBadge.style.cssText='position:fixed;left:50%;top:8px;transform:translateX(-50%);z-index:2147483647;pointer-events:none;width:min(94vw,440px);padding:10px 14px';
        const updateBadge=()=>{if(!deadlineBadge)return;const seconds=Math.max(0,Math.ceil((handoff.remainingMs!-(performance.now()-openedAt))/1000));deadlineBadge.dataset.urgent=String(seconds<=30);deadlineBadge.textContent=`${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')} · ${seconds<=30?'Finish payment now — less than 30 seconds left.':'Complete payment before this checkout closes.'}`};
        updateBadge();document.body.appendChild(deadlineBadge);badgeTimer=setInterval(updateBadge,250);
      }

      // The provider frame is cross-origin: we cannot honestly infer whether its UI
      // loaded. The escape reports dismissal, while the separate server payment deadline
      // closes this UI without claiming the provider failed.
      if (!settled && typeof document !== 'undefined') escapeTimer = setTimeout(() => {
        returnButton = document.createElement('button');
        returnButton.type = 'button';
        returnButton.textContent = 'Return to payment status';
        returnButton.setAttribute('aria-label', 'Return to payment status without starting another payment');
        returnButton.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483647;padding:12px 18px;border:1px solid #e9b970;border-radius:12px;background:#fff8ec;color:#18181b;font:600 14px system-ui;box-shadow:0 4px 24px #0003;cursor:pointer';
        returnButton.onclick = () => {
          instance.close();
          settle({kind: 'dismissed'});
          neutraliseProviderSurface();
        };
        document.body.appendChild(returnButton);
      }, 15_000);
    } catch (error) { cleanup(); reject(error); }
  });
  activeCheckout = {orderId: handoff.orderId, result};
  try { return await result; }
  finally { if (activeCheckout?.result === result) activeCheckout = null; }
}
