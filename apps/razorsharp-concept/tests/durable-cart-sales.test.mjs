import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
function fixture({ loseCreateResponse = false, liveCheckout = false } = {}) {
  let current = null, created = 0, sequence = 0;
  const keys = [];
  const byKey = new Map();
  const commerce = {
    cart: {
      current: async () => ({ cart: current }),
      read: async () => ({ cart_id: 'recover-existing', lines: [] }),
      create: async key => {
        keys.push(key);
        if (!byKey.has(key)) byKey.set(key, {cart_id: `cart-${++created}`, lines: []});
        const result = byKey.get(key);
        if (loseCreateResponse) { loseCreateResponse = false; throw Error('lost response'); }
        current = result;
        return result;
      },
    },
    checkout: { list: async () => ({ checkouts: liveCheckout ? [{cart_id:'recover-existing'}] : [] }) },
  };
  const exports = {};
  const code = ts.transpileModule(readFileSync(new URL('../lib/durable-cart.ts', import.meta.url), 'utf8'),
    {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
  vm.runInNewContext(code, {exports, require: () => ({commerce, idempotencyKey: () => `key-${++sequence}`})});
  const cart = new exports.DurableCart(() => {});
  return {cart, keys, created: () => created, clearCurrent: () => {current = null;}};
}
test('Initial shopping context creates one empty cart across simultaneous restores', async () => {
  const f = fixture();
  const [first, second] = await Promise.all([f.cart.restore(), f.cart.restore()]);
  assert.equal(first.cart_id, second.cart_id);
  assert.equal(f.created(), 1);
  assert.equal(first.lines.length, 0);
});
test('Lost create response reuses its key; a later cart lifecycle uses a fresh key', async () => {
  const f = fixture({loseCreateResponse:true});
  await assert.rejects(f.cart.restore(), /lost response/);
  await f.cart.restore();
  assert.equal(f.created(), 1);
  assert.equal(f.keys[0], f.keys[1]);
  f.clearCurrent();
  await f.cart.restore();
  assert.equal(f.created(), 2);
  assert.notEqual(f.keys[1], f.keys[2]);
});
test('An earlier checkout stays out of the editable cart until new shopping starts', async () => {
  const f = fixture({liveCheckout:true});
  const result = await f.cart.restore();
  assert.equal(result, null);
  assert.equal(f.created(), 0);
});

test('Starting a new cart after recovery does not copy the earlier purchase', async () => {
  const f = fixture({liveCheckout:true});
  await f.cart.restore();
  const current = await f.cart.newCart();
  assert.equal(current.cart_id, 'cart-1');
  assert.equal(current.lines.length, 0);
  assert.equal((await f.cart.restore()).cart_id, current.cart_id);
  assert.equal(f.created(), 1);
});
