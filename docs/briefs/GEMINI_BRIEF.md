# Brief for Gemini — catalogue services and product UI

Paste this whole file as your first message in the Gemini session. It is the complete
scope. Read it before touching anything.

---

## 1. You are not working alone

Two AI assistants are building this repository at the same time.

- **Claude** owns the financial backend: the transaction kernel, the database layer, the
  Razorpay adapter, the HTTP API and the durable worker. It is currently building the API
  and worker, which is the piece that makes the whole demo run.
- **You (Gemini)** own catalogue data, product search, and the buyer-facing UI.

Until now both assistants edited one working tree and overwrote each other. That is fixed
by giving each side its own git worktree. **Your worktree is the only directory you may
touch:**

```
/Users/vedanttyagi/Desktop/acr-worktrees/gemini-catalogue      branch: gemini/catalogue
```

Never edit `/Users/vedanttyagi/Desktop/Agentic Commerce for Razorpay` (that is the
integration tree) or `/Users/vedanttyagi/Desktop/acr-worktrees/claude-backend`. If you
find yourself in either, stop and change directory.

Your worktree has its own PostgreSQL database, `commerce_test_gem`, already migrated and
bootstrapped. Its `.env` is already correct. Do not point it at `commerce_test`; that is
Claude's, and two test runs against one database will corrupt each other's rows.

## 2. What this project is

A governed agentic commerce platform for the Razorpay AI Buildathon, Track 1. The
principle the whole architecture exists to prove is one sentence:

> **Agents propose; deterministic systems authorize and execute.**

An AI agent may search a catalogue and build a basket. It may never move money. Money
moves only through a transaction kernel that re-checks every fact under database locks
before it permits anything. The demonstration is eleven steps, and steps 5 to 8 are the
point: the merchant's price changes underneath an approved checkout, the old approval is
refused, the exact difference is shown to the buyer, and a new version is approved.

Read `PROJECT_SPECIFICATION.md` section 2.5 for those eleven steps and
`docs/adr/0003-service-layer.md` for the decisions that are already settled. Read
`docs/WORKSTREAMS.md` for the current state of the tree.

Your work is the surface a judge sees first. It has to look excellent and it must not
weaken any guarantee underneath it.

## 2b. Where your work sits in the plan

The specification lists twenty-one implementation steps. Six are finished, two are in
flight, thirteen have not started. `docs/briefs/WORK_LEDGER.md` has the full table with
evidence. Your part of it:

| Step | Your work | When |
| --- | --- | --- |
| 8 | Buyer storefront screens: de-brand and repair | **now** |
| 6 | Catalogue data and search, extended | **now, after the P0 list** |
| 13 | Merchant Copilot console UI | next |
| 12 | Merchant onboarding screens | next |
| 17 | Protocol Inspector view, once Claude exposes the data | next |
| 19 | Frontend end-to-end and accessibility tests | next |
| 20 | Demo and presentation assets | next |

Claude is meanwhile building step 7, the HTTP API and the durable worker, which is the
only thing standing between a proven kernel and a working demonstration. After that it
takes the agent layer, support services, and the UCP, AP2 and ACP protocols. None of
those are yours; do not start them, and do not create files under their packages.

## 3. Files you own

You may create, edit and delete these freely:

```
apps/buyer-web/src/app/**                     except route handlers under app/api/**
apps/buyer-web/src/components/**
apps/buyer-web/src/features/**
apps/buyer-web/src/lib/product-images.ts
apps/buyer-web/public/**
apps/buyer-web/package.json                   dependencies for UI work only
packages/merchant-sim/src/merchant_sim/catalogue.py     product data
packages/merchant-sim/src/merchant_sim/search.py        search and ranking
packages/merchant-sim/src/merchant_sim/textfold.py      Hinglish folding
packages/merchant-sim/src/merchant_sim/grounding.py
packages/merchant-sim/tests/test_catalogue.py
packages/merchant-sim/tests/test_search.py
packages/merchant-sim/tests/test_textfold.py
apps/merchant-console/**                      a new app, if you get to it
```

## 4. Files you must not touch

Editing any of these will break money handling, break the other assistant's work, or
both. If you believe one needs to change, **write the request in
`docs/briefs/REQUESTS_TO_CLAUDE.md` instead of editing it.**

```
packages/transaction-kernel/**       the kernel. Never.
packages/platform-db/**              schema, migrations, row-level security
packages/payment-adapters/**         Razorpay
packages/durable-work/**             the outbox
packages/commerce-api/**             the HTTP API (Claude is writing it right now)
packages/durable-worker/**           the worker (same)
packages/commerce-domain/**          Money, canonical hashing

packages/merchant-sim/src/merchant_sim/fees.py           money arithmetic
packages/merchant-sim/src/merchant_sim/policy.py         merchant policy
packages/merchant-sim/src/merchant_sim/store.py          state the kernel reads
packages/merchant-sim/src/merchant_sim/kernel_adapter.py kernel protocol
packages/merchant-sim/src/merchant_sim/injection.py

apps/buyer-web/src/lib/api/client.ts     the API contract
apps/buyer-web/src/lib/api/types.ts      the response schemas
apps/buyer-web/src/lib/api/problem.ts    error parsing
apps/buyer-web/src/lib/security/**       content security policy, nonces
apps/buyer-web/src/app/api/**            session and proxy route handlers

conftest.py, pyproject.toml, uv.lock, .github/**, infra/**, .env, .gitignore
docs/adr/**, PROJECT_SPECIFICATION.md, docs/WORKSTREAMS.md
```

`apps/buyer-web/src/lib/api/mock.ts` is **shared**. You may edit the `FIXTURE` product
array at the top. You may not change `loadState`, `productView`, `search` behaviour, or
any function that produces an approval, a decision or a delta — those encode the API
contract Claude must satisfy.

## 5. Rules that must never break

These are not style preferences. Each one is load-bearing.

1. **The approval card submits exactly what it displayed.** The content hash, amount and
   currency it posts must be the values it rendered, never re-fetched or recomputed. A
   stale closure here is a payment for the wrong amount.
2. **Razorpay's `checkout.js` loads only on the checkout route.** Not in the layout, not
   globally.
3. **A browser callback is never proof of payment.** The UI waits for the server timeline
   to report capture. Never render "paid" from the Razorpay handler alone.
4. **The session token stays in an HttpOnly cookie.** Never put it in `localStorage`,
   `sessionStorage`, or a `NEXT_PUBLIC_` variable.
5. **Money shown to a buyer comes from a server response.** Never compute a price,
   discount, tax or total in the browser. If a number is not in the payload, do not
   display it.
6. **No secret ever reaches the browser.** The Razorpay key id is public and comes from
   the API; the key secret must never appear in frontend code.
7. **Catalogue changes are data only.** In `catalogue.py` you may add, remove and edit
   products. You may not change the `Product` dataclass shape, and you may not change how
   prices are represented: integer minor units, never floats.

## 6. Your work, in order

### P0 — do these first, they block the submission

**1. Remove the Zepto branding. This is the most important task in this brief.**

The storefront currently clones Zepto, a real Indian quick-commerce company: 12 source
files reference it by name and 82 product images are hotlinked from `cdn.zeptonow.com`.
This submission is judged by Razorpay, who serve that market. The demo merchant is meant
to be a fictional simulator. Beyond the obvious trademark and reputation problem, the
images will break the moment that CDN refuses hotlinking.

- Invent a fictional store name and identity. Use it consistently.
- Self-host every product image under `apps/buyer-web/public/products/`. Generated or
  clearly synthetic imagery is fine, and a clean CSS placeholder is better than a
  hotlink. Keep the total under about 5 MB.
- Ask Claude to revert the `img-src` entry in `csp.ts` once nothing needs it. Do not edit
  that file yourself; put the request in `REQUESTS_TO_CLAUDE.md`.
- Remove every fabricated commercial claim: the hardcoded `★ 4.8 (32 reviews)` on every
  product page, the strike-through "MRP" invented with float arithmetic in
  `product-images.ts`, and the "No Cost EMI" text. A payments submission must not display
  invented financial claims.
- `category-view.test.tsx` asserts on Zepto's marketing copy. Rewrite those assertions to
  target roles and structure instead of copy.

**2. Fix the basket lost-update bug.** In
`apps/buyer-web/src/features/storefront/use-basket-actions.ts` around line 141, `addOne`
no longer re-reads the basket from the server before incrementing, so two quick taps or
two components acting at once silently lose quantity. Restore an authoritative read
before the mutation, or introduce one shared basket cache all three components use.

**3. Fix dark mode.** `globals.css` still defines a full `prefers-color-scheme: dark`
palette, but the new header, category grid, promo banners, category view and product
detail hardcode light colours. On a dark-mode laptop the storefront is currently broken.
Judges use laptops.

**4. Fix the fonts.** `layout.tsx` adds a Google Fonts stylesheet that the app's own
content security policy blocks, so the intended typography silently never loads. Either
self-host the font files under `public/` or put the request to allow the font origins in
`REQUESTS_TO_CLAUDE.md`. Self-hosting is better and needs no policy change.

**5. Fix the broken category tiles.** Eight of the twenty tiles in `category-grid.tsx`
map to categories that do not exist in the fixtures, and they silently return the whole
catalogue. Either add the missing categories to `catalogue.py` with real products, or
reduce the grid to the categories that exist. Never show a tile that lies.

**6. Delete the dead component.** `basket-link.tsx` was rewritten but nothing imports it
any more; the header inlines its own cart button.

### P1 — high value once P0 is done

**7. Put the Track 1 story back on the surface.** The section explaining what this
storefront proves is currently buried inside a collapsed disclosure below the fold. That
section is the entire differentiator: it is why this is a payments project and not a
shopping app. Give it a real place in the layout. Also restore the honest "Mock mode (no
backend)" badge that was deleted, so nobody mistakes simulated data for a live payment.

**8. Make the mock and the real catalogue agree.** `mock.ts`'s `FIXTURE` array gained
nine products that do not exist in `packages/merchant-sim/src/merchant_sim/catalogue.py`,
including an iPhone at ₹1,26,899 in an `electronics` category the simulator has never
heard of. When Claude's API lands, live mode will not match mock mode. Pick one direction
and make both sides identical: either add those products and categories properly to
`catalogue.py`, or trim the fixture back. Adding them is more impressive if you keep the
data honest.

**9. Grow the catalogue.** In `catalogue.py`, expand toward a convincing Indian quick
commerce range with correct integer paise pricing, real categories, and Hindi and
Hinglish search terms. Then extend `search.py` and `textfold.py` so queries like
`"doodh"`, `"दूध"`, `"milk"`, `"aata"` and `"atta"` all find the right product, with tests
in `test_search.py` and `test_textfold.py`.

### P2 — only if P0 and P1 are genuinely finished

**10. Merchant console.** A new Next.js app at `apps/merchant-console` showing the
merchant view: catalogue management, a revenue panel, and the retained-revenue evidence
that the eleven-step demo produces. Read-only against mock data is fine.

**11. Accessibility.** Keyboard navigation throughout, `aria-live` on state changes,
colour never the only signal, and visible focus rings. Specification section 29.8.

## 7. How to work

Run everything from your worktree:

```bash
cd /Users/vedanttyagi/Desktop/acr-worktrees/gemini-catalogue
```

Before you finish any task, all of these must pass:

```bash
cd apps/buyer-web && npm run lint && npm run typecheck && npm run test && npm run build
```

and for catalogue work:

```bash
export PATH="$HOME/.local/bin:$PATH"
uv run --no-sync python -m pytest packages/merchant-sim -o addopts="" -q
uv run --no-sync ruff check packages/merchant-sim
uv run --no-sync mypy packages/merchant-sim/src
```

Commit on your own branch, in small commits, with messages that say why rather than what.
Never commit `.env`, `node_modules`, `.next`, or any file matching `*api_keys*`. Never run
`git merge`, `git rebase`, or `git push` — the owner and Claude handle integration.

To pick up Claude's finished work:

```bash
git merge --ff-only main
```

If that fails, stop and say so rather than resolving a conflict in a file you do not own.

## 8. When you are done

Write a short report in `docs/briefs/GEMINI_REPORT.md`: what you built, what you verified
and with what command output, what you could not finish, and every request you left in
`REQUESTS_TO_CLAUDE.md`. Claude reads that file when wiring the two halves together.

Report honestly. If something does not work, say so. A known gap is useful; a false claim
of green costs more time than the work saved.
