import { test } from 'node:test';
import assert from 'node:assert/strict';
import { watchCheckout } from '../lib/checkout-events.ts';

test('timeline invalidation is coalesced and cleanup closes the stream', async () => {
  let source,
    reads = 0;
  globalThis.EventSource = class {
    listeners = {};
    closed = false;
    constructor(url) {
      this.url = url;
      source = this;
    }
    addEventListener(name, fn) {
      this.listeners[name] = fn;
    }
    close() {
      this.closed = true;
    }
  };
  const stop = watchCheckout('same-checkout', () => reads++);
  assert.equal(source.url, '/api/commerce/checkouts/same-checkout/events');
  source.listeners.timeline({});
  source.listeners.timeline({});
  await new Promise((resolve) => setTimeout(resolve, 180));
  assert.equal(reads, 1);
  source.listeners.complete({ data: '{"reason":"timeout"}' });
  assert.equal(source.closed, false);
  source.listeners.complete({ data: '{"reason":"terminal"}' });
  assert.equal(source.closed, true);
  stop();
  await new Promise((resolve) => setTimeout(resolve, 180));
  assert.equal(reads, 1, 'cleanup cancels pending invalidation');
  delete globalThis.EventSource;
});
