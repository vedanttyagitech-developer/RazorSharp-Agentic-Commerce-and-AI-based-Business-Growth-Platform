// `DUPLICATE_OPERATION` answers two different questions, and this screen used to hear only one.
//
// The backend replies `already_approved: this version already carries a recorded approval`
// with HTTP 200 in both of these:
//
//   1. A RECOVERY. The first request was admitted, an attempt and a grant exist, and the
//      buyer reloaded while Razorpay was open. Continuing is exactly right, and this file
//      exists partly to stop a fix for (2) breaking it.
//
//   2. A version whose admission was REFUSED after the approval had been recorded -- an
//      expired stock hold is the ordinary way to get there. No attempt was written and none
//      ever will be for this version.
//
// Read as (1), case (2) polled forty-five seconds for a provider order that cannot exist and
// then told the buyer "the store has not started the payment. Its payment worker has not
// picked this up" -- an accusation against a worker that was running perfectly, about a
// payment that was never started, on a screen offering no way forward.
//
// The decision bodies are byte-identical, so the checkout is asked instead. That is a read.

import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

function load(path, fetch, extra = {}) {
  const exports = {};
  const code = ts.transpileModule(readFileSync(new URL('../' + path, import.meta.url), 'utf8'), {
    fileName: path,
    compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022},
  }).outputText;
  vm.runInNewContext(code, {exports, fetch, crypto, AbortController, console, require: () => ({}), ...extra});
  return exports;
}


/** A stand-in for `element.style`: records what was set and honours removeProperty. */
function makeStyle(initial = {}) {
  return {
    ...initial,
    removeProperty(name) {
      delete this[name === 'pointer-events' ? 'pointerEvents' : name];
    },
  };
}

const DUPLICATE = {code: 'DUPLICATE_OPERATION', detail: 'already_approved: this version already carries a recorded approval', title: 'ApprovalStateError', status: 200};
const CARD = {checkout_id: 'checkout', version: 1, content_hash: 'hash', amount_minor: 5750, currency: 'INR'};

function harness(view) {
  let stored = null, reads = 0;
  const sessionStorage = {getItem: () => stored, setItem: (_k, v) => (stored = v), removeItem: () => (stored = null)};
  const commerce = {
    checkout: {
      approveAndPay: async () => DUPLICATE,
      read: async () => {
        reads++;
        return view;
      },
    },
  };
  const api = load('lib/manual-pay.ts', () => {
    throw Error('No payment mutation is permitted while resolving a duplicate');
  }, {sessionStorage, require: () => ({commerce, admitted: x => x, CommerceError: Error})});
  return {api, reads: () => reads};
}

test('A replayed approval that really did start a payment still continues', async () => {
  // The reload-mid-payment case. An attempt and its provider order exist, so the duplicate
  // is the buyer's own request arriving twice and the screen must go on waiting for it.
  const {api, reads} = harness({
    state: 'AWAITING_PAYMENT',
    order_id: null,
    attempt: {attempt_id: 'attempt', state: 'SUBMITTED', razorpay_order_id: 'order_live'},
  });
  const decision = await api.approve(api.pendingFor(CARD));
  assert.equal(decision.code, 'DUPLICATE_OPERATION');
  assert.equal(reads(), 1, 'the checkout is consulted rather than the decision body guessed at');
});

test('A confirmed purchase replayed after the fact is not mistaken for a stranded approval', async () => {
  // `order_id` without a live attempt must not be read as "nothing started" -- it is the
  // strongest possible evidence that something did.
  const {api} = harness({state: 'PAID', order_id: 'order', attempt: null});
  const decision = await api.approve(api.pendingFor(CARD));
  assert.equal(decision.code, 'DUPLICATE_OPERATION');
});

test('An approval that never became a payment says so, instead of blaming the payment worker', async () => {
  const {api} = harness({state: 'APPROVED', order_id: null, attempt: null});
  const failure = await api.approve(api.pendingFor(CARD)).then(() => null, error => error);
  assert.equal(failure?.name, 'ApprovalStartedNoPayment');
  assert.equal(failure.checkoutState, 'APPROVED');
  assert.match(failure.message, /Nothing has been charged and nothing is in flight/);
  assert.match(failure.message, /Refresh stock and review the latest bill/);
  // The two claims this replaced. Neither was true, and neither may come back.
  assert.doesNotMatch(failure.message, /worker/i);
  assert.doesNotMatch(failure.message, /failed/i);
});

test('The stranded approval is rendered with a title a buyer can act on', async () => {
  const {api} = harness({state: 'APPROVED', order_id: null, attempt: null});
  const failure = await api.approve(api.pendingFor(CARD)).then(() => null, error => error);
  const shown = api.manualMessage(failure);
  assert.equal(shown.title, 'This bill was approved but no payment started');
  assert.doesNotMatch(shown.title, /failed|refused/i);
});

test('An unreadable attempt state is unknown, never an accusation and never a failure', async () => {
  // `ProviderOrderPending` with no attempt state used to borrow the CREATED wording, which
  // names a culprit this screen cannot see.
  const api = load('lib/manual-pay.ts', () => {}, {require: () => ({commerce: {}, admitted: x => x, CommerceError: Error})});
  const unknown = api.manualMessage(new api.ProviderOrderPending(null));
  assert.match(unknown.detail, /unknown rather than failed/);
  assert.doesNotMatch(unknown.detail, /worker/i);
  assert.match(unknown.detail, /Do not start another payment/);
  // CREATED is a state the backend really did report, and it does mean nothing consumed the
  // grant. That wording is correct and stays.
  const created = api.manualMessage(new api.ProviderOrderPending('CREATED'));
  assert.match(created.detail, /payment worker has not picked this up/);
});

// ---------------------------------------------------------------------------------------
// The other half of the same dead end: a bill that is over, described correctly but with no
// control that could act on it.

function recovery() {
  return load('lib/checkout-recovery.ts', () => {});
}

test('A bill with no payment behind it is not described as one being checked', () => {
  const {recoveryMessage} = recovery();
  const message = recoveryMessage({state: 'APPROVED', order_id: null, attempt: null});
  assert.match(message, /No payment was started/);
  assert.match(message, /nothing has been charged/i);
  // The sentence this replaced promised an outcome that was never coming.
  assert.doesNotMatch(message, /awaiting a verified outcome/);
  assert.doesNotMatch(message, /checking its status/);
});

test('A live payment is still described as one being checked', () => {
  const {recoveryMessage} = recovery();
  // SUBMITTED with no provider order yet: not resumable, genuinely unresolved, and the
  // "do not start another payment" instruction must survive every change made here.
  const message = recoveryMessage({state: 'AWAITING_PAYMENT', order_id: null, attempt: {state: 'SUBMITTED'}});
  assert.match(message, /awaiting a verified outcome/);
  assert.match(message, /Do not start another payment/);
});

test('An escalated attempt is never reopened, whatever else changes', () => {
  const {recoveryMessage, canResumeManualCheckout, canRefreshCheckout} = recovery();
  const escalated = {state: 'PAYMENT_UNKNOWN', order_id: null, cancellable: true, attempt: {state: 'ESCALATED', razorpay_order_id: 'order_x'}};
  assert.match(recoveryMessage(escalated), /needs merchant review/);
  assert.equal(canResumeManualCheckout(escalated), false);
  assert.equal(canRefreshCheckout(escalated), false, 'an escalated attempt is not replaced by a fresh bill');
});

test('A spent bill can be reviewed again without a cancellation it cannot get', () => {
  const {canRefreshCheckout, isSpentCheckout} = recovery();
  for (const state of ['CANCELLED', 'EXPIRED', 'INVALIDATED']) {
    const spent = {state, order_id: null, attempt: null, cancellable: false};
    assert.equal(isSpentCheckout(spent), true, `${state} holds nothing`);
    assert.equal(canRefreshCheckout(spent), true, `${state} must offer a way forward`);
  }
});

test('Nothing in flight is ever replaced without the backend agreeing to cancel it', () => {
  const {canRefreshCheckout, isSpentCheckout} = recovery();
  // A terminal state around an attempt is NOT spent: something was started inside it.
  const around = {state: 'CANCELLED', order_id: null, attempt: {state: 'FAILED'}, cancellable: false};
  assert.equal(isSpentCheckout(around), false);
  assert.equal(canRefreshCheckout(around), false);
  // A live, uncancellable checkout still cannot be replaced.
  assert.equal(canRefreshCheckout({state: 'AWAITING_PAYMENT', order_id: null, attempt: {state: 'SUBMITTED'}, cancellable: false}), false);
  // Nor can a paid one, spent-looking or not.
  assert.equal(isSpentCheckout({state: 'CANCELLED', order_id: 'order', attempt: null}), false);
  assert.equal(canRefreshCheckout({state: 'PAID', order_id: 'order', attempt: null, cancellable: true}), false);
});

// ---------------------------------------------------------------------------------------
// The escape control has to work on the surface it exists for: one whose script never
// finished starting, where the provider's own close() is a no-op.

test('Leaving a stuck provider surface frees the page WITHOUT removing the provider\'s node', async () => {
  // The node stays in the document on purpose. Removing it looked correct and passed a test,
  // and it wedges checkout.js: the next open() on the same order hangs the renderer outright.
  let closes = 0, removedOwnButton = 0, timer, button, mutations = 0, removedProviderNodes = 0;
  class StuckCheckout { constructor(o) { this.o = o; } on() {} open() {} close() { closes++; } }
  const container = {
    className: 'razorpay-container', attrs: {}, style: makeStyle(),
    setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; },
    remove() { removedProviderNodes++; },
  };
  const bodyStyle = makeStyle({overflow: 'hidden', contain: 'initial'});
  const doc = {
    createElement: () => ({setAttribute() {}, style: {}, remove() { removedOwnButton++; }}),
    body: {appendChild: b => { button = b; }, style: bodyStyle},
    querySelectorAll: selector =>
      selector === '.razorpay-container' ? [container]
      : Object.hasOwn(container.attrs, 'data-rs-provider-surface-neutralised') ? [container] : [],
  };
  const api = load('lib/razorpay.ts', () => { throw Error('No payment mutation from an escape'); }, {
    window: {Razorpay: StuckCheckout}, document: doc,
    setTimeout: fn => { timer = fn; return 1; }, clearTimeout: () => {},
    fetch: () => { mutations++; },
  });

  const opening = api.openRazorpay({keyId: 'rzp_test_fixture', orderId: 'order_stuck', amountMinor: 5750, currency: 'INR', merchantName: 'Test', description: 'Stuck frame'});
  await Promise.resolve();
  timer(); // the escape control appears only after the frame has had its time
  assert.match(button.textContent, /Return to payment status/);

  button.onclick();
  assert.equal((await opening).kind, 'dismissed', 'leaving is unresolved, never a failure');

  assert.equal(closes, 1, 'the provider is asked to close first');
  assert.equal(removedProviderNodes, 0, "the provider's own node is NEVER taken out of the document");
  assert.equal(container.style.display, 'none', 'it is put out of the way instead');
  assert.equal(container.style.pointerEvents, 'none', 'and stops taking clicks');
  assert.ok('data-rs-provider-surface-neutralised' in container.attrs, 'marked so it can be undone');
  assert.equal(bodyStyle.overflow, undefined, 'the page is unlocked');
  assert.equal(bodyStyle.contain, undefined);
  assert.equal(removedOwnButton, 1, 'the escape control removes itself, which IS ours to remove');
  assert.equal(mutations, 0, 'escaping a stuck frame sends no request of any kind');
});

test('Reopening after an escape makes the neutralised surface visible again', async () => {
  // The regression that mattered: escape, then resume the SAME order. If the surface stayed
  // hidden the buyer would get a blank frame; if it had been removed the tab would hang.
  let opens = 0, timer, button;
  class Checkout { constructor(o) { this.o = o; } on() {} open() { opens++; } close() {} }
  const container = {
    className: 'razorpay-container', attrs: {}, style: makeStyle(),
    setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; },
    remove() { throw Error('the provider node must never be removed'); },
  };
  const doc = {
    createElement: () => ({setAttribute() {}, style: {}, remove() {}}),
    body: {appendChild: b => { button = b; }, style: makeStyle()},
    querySelectorAll: selector =>
      selector === '.razorpay-container' ? [container]
      : Object.hasOwn(container.attrs, 'data-rs-provider-surface-neutralised') ? [container] : [],
  };
  const api = load('lib/razorpay.ts', () => {}, {window: {Razorpay: Checkout}, document: doc, setTimeout: fn => { timer = fn; return 1; }, clearTimeout: () => {}});
  const handoff = {keyId: 'rzp_test_fixture', orderId: 'order_same', amountMinor: 5750, currency: 'INR', merchantName: 'Test', description: 'Resume'};

  const first = api.openRazorpay(handoff);
  await Promise.resolve();
  timer();
  button.onclick();
  assert.equal((await first).kind, 'dismissed');
  assert.equal(container.style.display, 'none', 'hidden while the buyer is back on our page');

  void api.openRazorpay(handoff); // the same order: a resume, never a second payment
  await Promise.resolve();
  assert.equal(opens, 2, 'the escape released the single-active-checkout latch');
  assert.equal(container.style.display, undefined, 'and the surface is visible again, not blank');
  assert.equal(container.style.pointerEvents, undefined);
  assert.equal('data-rs-provider-surface-neutralised' in container.attrs, false);
});

test('A surface the SDK abandons cannot come back as an invisible click trap', async () => {
  // If checkout.js builds a NEW container instead of reusing the old one, restoring the old
  // one would put a full-viewport empty element at the maximum z-index back over the page.
  let timer, button;
  class Checkout { constructor(o) { this.o = o; } on() {} open() {} close() {} }
  const mk = name => ({
    name, className: 'razorpay-container', attrs: {}, style: makeStyle(),
    setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; },
    remove() { throw Error('never removed'); },
  });
  const older = mk('older'), newer = mk('newer');
  let live = [older];
  const doc = {
    createElement: () => ({setAttribute() {}, style: {}, remove() {}}),
    body: {appendChild: b => { button = b; }, style: makeStyle()},
    querySelectorAll: selector =>
      selector === '.razorpay-container' ? live
      : live.filter(c => 'data-rs-provider-surface-neutralised' in c.attrs),
  };
  const api = load('lib/razorpay.ts', () => {}, {window: {Razorpay: Checkout}, document: doc, setTimeout: fn => { timer = fn; return 1; }, clearTimeout: () => {}});
  const handoff = {keyId: 'rzp_test_fixture', orderId: 'order_same', amountMinor: 5750, currency: 'INR', merchantName: 'Test', description: 'Resume'};

  const first = api.openRazorpay(handoff);
  await Promise.resolve();
  timer();
  button.onclick();
  await first;
  live = [older, newer]; // the SDK built a fresh one on reopen rather than reusing
  void api.openRazorpay(handoff);
  await Promise.resolve();
  assert.equal(newer.style.display, undefined, 'the live surface is interactive');
  assert.equal(older.style.display, 'none', 'the abandoned one is put back out of the way');
  assert.equal(older.style.pointerEvents, 'none');
});

test('Escaping leaves the order recoverable, so the same checkout can be reopened', async () => {
  // The attempt is untouched by the sweep, so the very next open on the same order id is a
  // resume rather than a second payment.
  let opens = 0, timer, button;
  class Checkout { constructor(o) { this.o = o; } on() {} open() { opens++; } close() { this.o.modal.ondismiss(); } }
  const doc = {
    createElement: () => ({setAttribute() {}, style: {}, remove() {}}),
    body: {appendChild: b => { button = b; }, style: {removeProperty() {}}},
    querySelectorAll: () => [],
  };
  const api = load('lib/razorpay.ts', () => {}, {window: {Razorpay: Checkout}, document: doc, setTimeout: fn => { timer = fn; return 1; }, clearTimeout: () => {}});
  const handoff = {keyId: 'rzp_test_fixture', orderId: 'order_same', amountMinor: 5750, currency: 'INR', merchantName: 'Test', description: 'Resume'};
  const first = api.openRazorpay(handoff);
  await Promise.resolve();
  timer();
  button.onclick();
  assert.equal((await first).kind, 'dismissed');
  // The same order opens again, and it is a second OPEN, never a second payment: the order
  // id is the one the Execution Grant already created. Left pending on purpose -- a modal
  // nobody has answered yet is the state a resumed checkout is supposed to be in.
  void api.openRazorpay(handoff);
  await Promise.resolve();
  assert.equal(opens, 2, 'the escape released the single-active-checkout latch');
});
