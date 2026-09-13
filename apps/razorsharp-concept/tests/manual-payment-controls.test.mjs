import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

function harness(closed = false) {
  const states = [], refs = []; let slot = 0, refSlot = 0, tree, opened = 0, cleared = 0, checked = 0;
  const guidance = [];
  const react = {
    useState(initial) { const i = slot++; if (!(i in states)) states[i] = i === 0 ? 'provider-failed' : i === 6 ? {orderId: 'same-order'} : initial; return [states[i], v => { states[i] = v; }]; },
    useRef(value) { const i = refSlot++; return refs[i] ??= {current: value}; },
    useEffect() {}, useCallback: f => f, useEffectEvent: f => f,
  };
  const jsx = (type, props) => ({type, props: props ?? {}});
  const exports = {};
  const code = ts.transpileModule(readFileSync(new URL('../components/manual-checkout.tsx', import.meta.url), 'utf8'), {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX}}).outputText;
  vm.runInNewContext(code, {exports, AbortController, require(name) {
    if (name === 'react') return react;
    if (name === 'react/jsx-runtime') return {jsx, jsxs: jsx};
    if (name === '@/lib/demo') return {money: n => String(n)};
    if (name === '@/lib/checkout-recovery') return {canResumeManualCheckout: () => true};
    if (name === '@/lib/commerce') return {commerce: {checkout: {
      read: async () => ({attempt: {attempt_id: 'same-attempt'}}),
      payment: async () => ({attempt_id: 'same-attempt', razorpay_order_id: 'same-order', window_closed: closed}),
    }}};
    if (name === '@/lib/razorpay') return {openRazorpay: async () => {opened++; throw Error('SDK unavailable');}};
    if (name === '@/lib/manual-pay') return {
      pendingFor: () => ({checkoutId: 'same-checkout'}),
      awaitSettlement: async id => {assert.equal(id, 'same-checkout'); checked++; return {kind: 'merchant-review'};},
      clearPending: () => {cleared++;}, manualMessage: e => ({title: 'Unavailable', detail: e.message}),
    };
    return {};
  }});
  function draw() { slot = refSlot = 0; tree = exports.ManualCheckout({card: {checkout_id: 'same-checkout', quote: {lines: []}}, onGuidance: s => guidance.push(s)}); }
  function nodes(n) { if (!n || typeof n !== 'object') return []; if (Array.isArray(n)) return n.flatMap(nodes); return [n, ...nodes(n.props?.children)]; }
  const button = text => nodes(tree).find(n => n.type === 'button' && JSON.stringify(n.props.children).includes(text));
  draw();
  return {draw, button, states, guidance, stats: () => ({opened, cleared, checked})};
}

test('SDK reopen failure restores recovery controls and does not imply an open iframe', async () => {
  const h = harness();
  await h.button('Open Razorpay again').props.onClick(); h.draw();
  assert.equal(h.states[0], 'provider-failed');
  assert.ok(h.button('Open Razorpay again'));
  assert.ok(h.button('Check existing payment status'));
  assert.match(h.guidance.at(-1), /could not be reopened/);
  assert.deepEqual(h.stats(), {opened: 1, cleared: 0, checked: 0});
});

test('Closed payment window checks the same payment and renders merchant review without clearing recovery', async () => {
  const h = harness(true);
  await h.button('Open Razorpay again').props.onClick(); h.draw();
  assert.equal(h.states[0], 'merchant-review');
  assert.equal(h.button('Open Razorpay again'), undefined);
  assert.equal(h.button('Refresh stock and review again'), undefined);
  assert.ok(h.button('Check existing payment status'));
  assert.deepEqual(h.stats(), {opened: 0, cleared: 0, checked: 1});
});
