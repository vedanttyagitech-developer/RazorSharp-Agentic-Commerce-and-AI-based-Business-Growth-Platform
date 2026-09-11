// The merchant copilot must not state a figure the store did not record.
//
// It used to. Five string literals picked by regex, two of them carrying numbers: "revenue
// is up 18.4%, with breakfast essentials driving the story" and "your sample inventory
// shows one low-stock product". Both rendered beside a panel showing the real confirmed
// order value, with nothing to tell a reader which was which.
//
// The growth figure was the worse one, because the backend refuses to produce it by
// design: every insights response carries its own sentence saying the number is
// "Confirmed order value before refunds; not net revenue, profit, or campaign-attributed
// growth." An uplift percentage is exactly what the platform declines to measure.
//
// So the tests that matter are the negative ones: with no facts loaded, the copilot must
// say it does not know rather than fill the gap, and no reply may ever contain a percentage
// the backend never sent.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

/** Load the pure half of lib/merchant-facts.ts. The hook half needs React and is not used here. */
function load() {
  const source = readFileSync(new URL('../lib/merchant-facts.ts', import.meta.url), 'utf8')
    // The module imports React and the bridge helper for `useMerchantFacts`; neither is
    // reachable from `copilotAnswer`, which is what these tests exercise.
    .replace(/^import .*$/gm, '');
  const code = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const exports = {};
  vm.runInNewContext(code, { exports, Intl, console });
  return exports;
}

const { copilotAnswer, formatMinor, LOW_STOCK_UNITS } = load();

const NOTHING = {
  sales: null,
  days: null,
  salesDefinition: null,
  openCases: null,
  totalCases: null,
  policyVersion: null,
  refundWindowDays: null,
  loading: false,
};

const REAL = {
  sales: [{ currency: 'INR', orders: 53, salesMinor: 137803780 }],
  days: 7,
  salesDefinition:
    'Confirmed order value before refunds; not net revenue, profit, or campaign-attributed growth. Currencies are never combined.',
  openCases: 3,
  totalCases: 6,
  policyVersion: 2,
  refundWindowDays: 7,
  loading: false,
};

const QUESTIONS = [
  "What is driving this week's growth?",
  'Which products need restocking?',
  'Suggest a weekend offer',
  'Help with the oldest customer case',
  'anything at all',
];

test('No reply invents a percentage, and the retired literals cannot come back', () => {
  const shelf = [{ name: 'Amul Taaza milk', stock: 42 }];
  for (const facts of [NOTHING, REAL]) {
    for (const question of QUESTIONS) {
      const answer = copilotAnswer(question, facts, shelf);
      assert.doesNotMatch(
        answer,
        /\d+(\.\d+)?\s*%/,
        `a percentage appeared in: ${answer}`,
      );
      assert.doesNotMatch(answer, /18\.4|breakfast essentials|₹50/, `a retired literal returned: ${answer}`);
    }
  }
});

test('With nothing loaded it says so rather than filling the gap', () => {
  assert.match(copilotAnswer('how are sales', NOTHING, []), /do not have/i);
  assert.match(copilotAnswer('restock', NOTHING, []), /not loaded/i);
  assert.match(copilotAnswer('customer case', NOTHING, []), /could not read|will not guess/i);
});

test('An empty shelf is not the same answer as a shelf with nothing low', () => {
  const missing = copilotAnswer('restock', REAL, []);
  const stocked = copilotAnswer('restock', REAL, [{ name: 'Milk', stock: 42 }]);
  assert.notEqual(missing, stocked);
  assert.match(stocked, /Nothing is at or below/);
  assert.match(stocked, new RegExp(`${LOW_STOCK_UNITS} units`));
});

// The percentage guard above cannot see this one. "Your sample inventory shows one
// low-stock product" carries no percent sign and no retired phrase, so the only thing that
// catches its return is the count moving with the shelf it was given.
test('The low-stock count tracks the shelf rather than being stated', () => {
  const shelf = (lows) => [
    ...lows.map((stock, i) => ({ name: `Low ${i}`, stock })),
    { name: 'Plenty', stock: 99 },
  ];
  const one = copilotAnswer('restock', REAL, shelf([2]));
  const three = copilotAnswer('restock', REAL, shelf([2, 4, 9]));
  assert.match(one, /^1 of 2 products/, one);
  assert.match(three, /^3 of 4 products/, three);
  // Named lowest-first, so a merchant sees the worst case before the rest.
  assert.match(three, /Low 0 \(2\), Low 1 \(4\), Low 2 \(9\)/, three);
  // A shelf of four with three low cannot produce the same sentence as a shelf with one.
  assert.notEqual(one, three);
});

test('Sales carry the backend figure and the backend caveat, verbatim', () => {
  const answer = copilotAnswer('how is the business doing', REAL, []);
  assert.ok(answer.includes(formatMinor(137803780, 'INR')), answer);
  assert.match(answer, /53 confirmed orders/);
  assert.match(answer, /last 7 days/);
  // The caveat is the platform's own sentence, not a paraphrase of it.
  assert.ok(answer.includes(REAL.salesDefinition), 'the definition was dropped or reworded');
});

test('Zero confirmed orders reads as zero, not as a missing figure', () => {
  const quiet = { ...REAL, sales: [] };
  assert.match(copilotAnswer('sales', quiet, []), /No confirmed orders in the last 7 days/);
});

test('One case and many cases are counted, and the real refund window is quoted', () => {
  assert.match(copilotAnswer('case', REAL, []), /3 open cases of 6/);
  assert.match(copilotAnswer('case', REAL, []), /refund window is 7 days/);
  const single = { ...REAL, openCases: 1, totalCases: 4 };
  assert.match(copilotAnswer('case', single, []), /1 open case of 4/);
  const none = { ...REAL, openCases: 0, totalCases: 4 };
  assert.match(copilotAnswer('case', none, []), /No open cases/);
});

test('A price answer names the version a change would be proposed against', () => {
  assert.match(copilotAnswer('suggest an offer', REAL, []), /policy version 2/);
  // And says so honestly when it does not know which version.
  assert.match(copilotAnswer('suggest an offer', NOTHING, []), /current published policy/);
});
