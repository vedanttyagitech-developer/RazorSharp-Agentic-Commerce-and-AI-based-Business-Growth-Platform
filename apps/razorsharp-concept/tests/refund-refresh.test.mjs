import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
test('parent evidence refresh reloads refund data while retaining the selected filter', async () => {
  const states = [],
    effects = [],
    calls = [];
  let slot = 0,
    effectSlot = 0,
    tree;
  const react = {
    useState(initial) {
      const i = slot++;
      if (!(i in states)) states[i] = initial;
      return [
        states[i],
        (v) => {
          states[i] = typeof v === 'function' ? v(states[i]) : v;
        },
      ];
    },
    useEffect(fn, deps) {
      const i = effectSlot++,
        old = effects[i];
      if (!old || deps.some((d, j) => d !== old.deps[j])) {
        old?.cleanup?.();
        effects[i] = { deps, cleanup: fn() };
      }
    },
  };
  const jsx = (type, props) => ({ type, props: props || {} }),
    exports = {};
  vm.runInNewContext(
    ts.transpileModule(
      readFileSync(
        new URL('../components/refund-queue.tsx', import.meta.url),
        'utf8',
      ),
      {
        compilerOptions: {
          module: ts.ModuleKind.CommonJS,
          target: ts.ScriptTarget.ES2022,
          jsx: ts.JsxEmit.ReactJSX,
        },
      },
    ).outputText,
    {
      exports,
      queueMicrotask,
      URLSearchParams,
      require: (name) =>
        name === 'react'
          ? react
          : name === 'react/jsx-runtime'
            ? { jsx, jsxs: jsx }
            : {
                merchantCall: async (path) => {
                  calls.push(path);
                  return {
                    refunds: [],
                    counts: { REFUND_FAILED: calls.length },
                    next_cursor: null,
                  };
                },
              },
    },
  );
  const render = (key) => {
    slot = 0;
    effectSlot = 0;
    tree = exports.RefundQueue({ refreshKey: key });
  };
  const nodes = (n) =>
    !n || typeof n !== 'object'
      ? []
      : Array.isArray(n)
        ? n.flatMap(nodes)
        : [n, ...nodes(n.props?.children)];
  render(1);
  await new Promise((r) => setImmediate(r));
  render(1);
  nodes(tree)
    .find((n) => n.type === 'select')
    .props.onChange({ target: { value: 'REFUND_FAILED' } });
  render(1);
  await new Promise((r) => setImmediate(r));
  render(1);
  assert.equal(calls.length, 2);
  assert.match(calls[1], /state=REFUND_FAILED/);
  render(2);
  await new Promise((r) => setImmediate(r));
  render(2);
  assert.equal(calls.length, 3);
  assert.equal(calls[2], calls[1]);
  assert.match(JSON.stringify(tree), /REFUND_FAILED/);
  for (const effect of effects) effect.cleanup?.();
});
