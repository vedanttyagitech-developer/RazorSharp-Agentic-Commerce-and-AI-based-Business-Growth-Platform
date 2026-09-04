# Brief 9 for Gemini — make the console honest, then make it verified

Brief 8 is merged and the good half is genuinely good: nine of thirteen client methods call
the real API and label themselves with `is_live`. This brief fixes three gaps found while
verifying it. The first is a credibility problem and matters more than it looks.

---

## STEP ZERO — every session

```bash
cd /Users/vedanttyagi/Desktop/acr-worktrees/gemini-catalogue
pwd && git branch --show-current
git merge --ff-only main
```

`pwd` must be that worktree, branch `gemini/catalogue`. If it shows
`/Users/vedanttyagi/Desktop/Agentic Commerce for Razorpay`, stop and change directory.

## Fix 1 — Three surfaces look live and are entirely invented

This is the important one.

`client.ts` has three methods that make **no network call at all** and return hardcoded
fixtures:

```
getOrders()             -> DEMO_ORDERS
getRefunds()            -> DEMO_REFUNDS
getReviewQueue()        -> DEMO_REVIEW_QUEUE
getCatalogueProducts()  -> CATALOGUE_PRODUCTS   (local, and that is fine — see Fix 2)
```

And the pages that render them carry **zero** `is_live` references and **zero** labels:

| Page | `is_live` refs | `SIMULATED` label |
| --- | --- | --- |
| `/operations` (orders, refunds, review queue) | 0 | 0 |
| `/catalogue` | 0 | 0 |
| `/onboarding` | 0 | 0 |

Meanwhile `/`, `/evidence` and `/inspector` label themselves carefully. That asymmetry is
worse than uniform mocking, because the labelled pages teach a reviewer to trust the
unlabelled ones. A judge who clicks Operations, sees a confident order list and a refund
state tracker, and later discovers both are invented will discount the pages that were
honest too.

Brief 8 said it directly: *a page mixing real and invented numbers without distinction is
worse than one showing fewer.* This is that.

**Do this now, unconditionally, because it needs nothing from Claude:**

- Every surface that renders fixture data shows the same badge the other pages use. Reuse
  the existing component and vocabulary so it reads as one system: `SIMULATED · MOCK`
  against `LIVE · COMMITTED`.
- The badge sits where the data is, not only in a page header. An order table built from
  fixtures says so above the table.
- `/onboarding` states plainly that configuration persists to browser storage and does not
  yet create a tenant. It is a real flow over a backend that does not exist; say that.
- Add a short line under any simulated table explaining what it will show when wired, so
  the page reads as unfinished rather than dishonest.

**Then wire what becomes wireable.** Claude is adding two endpoints that do not exist today:

```
GET /v1/orders?status=&limit=&cursor=      the order collection
GET /v1/refunds?state=&limit=&cursor=      the refund collection
```

When they land, replace `getOrders` and `getRefunds` with real calls following the exact
pattern the other nine methods already use: fetch, return with `is_live: true`, fall back to
the fixture with `is_live: false` on failure. Then the badge on `/operations` becomes truthful
by itself.

The review queue stays simulated. Human-review cases are created by the Reconciliation and
Resolution services, which are not built yet. Label it and leave it.

## Fix 2 — The catalogue exists in three places and can drift

All three are at 247 products today. They will not stay that way by accident.

```
packages/merchant-sim/src/merchant_sim/catalogue.py    the authority
apps/buyer-web/src/lib/api/mock.ts        FIXTURE       generated from it
apps/merchant-console/src/lib/api/products-data.ts     generated from it
```

You already proved the first two agree. Now make that a test rather than a one-time check.

- Write one test that reads all three and asserts they hold the same SKUs, and that for
  every SKU the name, category, unit, list price in integer paise, stock and tax basis
  points match exactly.
- Put it where it will actually run. A Vitest test in the console that reads the Python file
  is awkward; a Python test in `packages/merchant-sim/tests/` that parses both TypeScript
  files is the more reliable direction, since the simulator is the authority and Python is
  where the repository's test discipline lives. Name it `test_ms_catalogue_parity.py`.
- Make the failure message useful: name the SKU and the field that diverged, not just a
  count. A parity test that says "247 != 246" costs an hour to debug.
- If you generate `products-data.ts` from a script, commit the script next to it so the next
  person regenerates rather than hand-edits.

## Fix 3 — The console has no end-to-end test

Everything else in the repository has one. Add Playwright to `apps/merchant-console`, in the
same shape as the storefront's so the two feel like one project.

Cover the paths a judge will actually walk:

- The dashboard loads and the retained-revenue figure renders.
- `/evidence` shows the arithmetic: version N's approved total, the corrected total, and the
  difference, with the audit verification result visible.
- `/operations` renders all four refund states as visibly different, since conflating
  `REFUND_PENDING` with `REFUND_UNKNOWN` is the mistake that refunds a buyer twice.
- `/catalogue` filters, searches by a Hindi synonym, and paginates without the table
  collapsing.
- The scenario lever posts to the injection endpoint. Assert the request was made, not that
  a toast appeared.
- **Every simulated surface shows its badge.** After Fix 1, make that a test, so the labels
  cannot quietly disappear later.

Wire it as `npm run e2e` and make sure `npm run build` still passes.

## Boundaries, unchanged

Yours: `apps/merchant-console/**`, `apps/buyer-web/src/{app,components,features}/**` except
`app/api/**`, `apps/buyer-web/public/**`, `agent_runtime/prompts/**`, and `merchant_sim`'s
`catalogue.py`, `search.py`, `textfold.py`, `grounding.py`, plus a new test file under
`packages/merchant-sim/tests/`.

Not yours: the kernel, `platform-db`, `payment-adapters`, `durable-work`, `commerce-api`,
`durable-worker`, `commerce-domain`; `merchant_sim`'s `fees.py`, `policy.py`, `store.py`,
`kernel_adapter.py`, `injection.py`, `scenarios.py`; the whole of `agent-runtime` except
`prompts/`; `apps/buyer-web/src/lib/api/{client,types,problem}.ts`;
`apps/buyer-web/src/lib/security/**`; `conftest.py`, `pyproject.toml`, `uv.lock`,
`.github/**`, `infra/**`, `docs/adr/**`, `PROJECT_SPECIFICATION.md`, `docs/DEMO.md`,
`docs/STATUS.md`.

Note the console's own `src/lib/api/` **is** yours; only the storefront's is not.

## Gate

```bash
cd apps/merchant-console && npm run lint && npm run typecheck && npm run test && npm run build && npm run e2e
cd ../buyer-web && npm run lint && npm run typecheck && npm run test && npm run build && npm run e2e
export PATH="$HOME/.local/bin:$PATH"
uv run --no-sync python -m pytest packages/merchant-sim -o addopts="" -q
```

## Reporting

Append to `docs/briefs/GEMINI_REPORT.md` with real command output. State plainly, per page,
whether it is live or simulated after this brief, and confirm the parity test fails when you
deliberately change one price in one of the three files. A parity test nobody has seen fail
is not yet a test.
