# Brief for Gemini — storefront, agent surface, catalogue, merchant console

## STEP ZERO — do this before anything else

Run these three commands now and paste the output back before you read further or edit a
single file:

```bash
cd /Users/vedanttyagi/Desktop/acr-worktrees/gemini-catalogue
pwd
git branch --show-current
```

`pwd` must print exactly `/Users/vedanttyagi/Desktop/acr-worktrees/gemini-catalogue` and
the branch must be `gemini/catalogue`.

**If `pwd` prints anything else — especially
`/Users/vedanttyagi/Desktop/Agentic Commerce for Razorpay` — stop.** You are in the shared
integration tree where another assistant works, and every edit you make there will collide
with it. This has already happened once: six storefront files were edited in the wrong tree
and had to be moved by hand. Change directory and check again.

Re-run `pwd` at the start of every work session. A tool inherits the directory it was
launched from, and reading this file does not move you.

---

Paste this whole file as your first message in the Gemini session. It is your complete
scope. Read it before touching anything.

---

## 1. You are not working alone

Two AI assistants build this repository at the same time.

- **Claude** owns everything that touches money: the transaction kernel, the database
  layer, the Razorpay adapter, the HTTP API and the durable worker. It is building the API
  and worker right now, which is the piece that makes the demo able to take a payment.
- **You (Gemini)** own the entire visible product: the storefront, the AI agent surface,
  catalogue data and search, and the merchant console.

Until recently both assistants edited one working tree and overwrote each other. That is
solved by giving each side its own git worktree.

**Your worktree, the only directory you may edit:**

```
/Users/vedanttyagi/Desktop/acr-worktrees/gemini-catalogue     branch: gemini/catalogue
```

Never edit `/Users/vedanttyagi/Desktop/Agentic Commerce for Razorpay` (the integration
tree) or `/Users/vedanttyagi/Desktop/acr-worktrees/claude-backend`. If you find yourself
in either, stop and change directory.

Your worktree has its own PostgreSQL database, `commerce_test_gem`, already migrated with
roles bootstrapped, and its `.env` is already correct. Never point it at `commerce_test`;
that is Claude's, and two test runs against one database corrupt each other's rows.

You can work at full speed without waiting for Claude. Everything in this brief runs
against mock data today and swaps to the real API later without a rewrite.

## 2. What you are building

A governed agentic commerce platform for the Razorpay AI Buildathon, Track 1. The
architecture exists to prove one sentence:

> **Agents propose; deterministic systems authorize and execute.**

An AI agent can search, build a basket and propose a checkout. It can never move money.
Money moves only through a transaction kernel that re-verifies every fact under database
locks first.

**The product concept, and this is deliberate:** a pixel-faithful clone of Zepto's
quick-commerce storefront, with an AI shopping agent working inside it. The familiarity is
the point. A judge recognises the surface instantly, and the story lands harder: this is
the app you already use, except an AI is shopping in it, and the thing standing between
that AI and your money is a kernel that refuses stale approvals.

The demonstration is eleven steps, in `PROJECT_SPECIFICATION.md` section 2.5. Steps 5 to 8
are the ones that matter: the merchant's price changes underneath an approved checkout,
the old approval is refused, the exact difference is shown, and a new version is approved.

Read `docs/briefs/WORK_LEDGER.md` for what is done and what is left, and
`docs/WORKSTREAMS.md` for the state of the tree.

## 3. Assets are local. Never hotlink.

Download every product image and every product description into the repository and serve
them from there. No runtime dependency on any external CDN.

- Images under `apps/buyer-web/public/products/<sku>.webp`, category and banner art under
  `public/categories/` and `public/banners/`.
- Convert to WebP, size them for the grid they appear in (roughly 400px for tiles, 800px
  for product pages), and keep the whole directory under about 15 MB.
- Product copy lives in `packages/merchant-sim/src/merchant_sim/catalogue.py` as data, not
  scattered through components.
- Once nothing loads from an external image host, the content security policy entry that
  allowed one is dead. **Do not edit `csp.ts` yourself** — append the request to
  `docs/briefs/REQUESTS_TO_CLAUDE.md` and Claude removes it.

Local assets also mean the demo cannot break because someone else's server rate-limited
you mid-recording.

## 4. Files you own

Create, edit and delete these freely:

```
apps/buyer-web/src/app/**                    except app/api/** (route handlers)
apps/buyer-web/src/components/**
apps/buyer-web/src/features/**
apps/buyer-web/src/lib/product-images.ts
apps/buyer-web/public/**
apps/buyer-web/package.json                  UI dependencies only
apps/merchant-console/**                     a new app you will create

packages/merchant-sim/src/merchant_sim/catalogue.py    product data and copy
packages/merchant-sim/src/merchant_sim/search.py       search and ranking
packages/merchant-sim/src/merchant_sim/textfold.py     Hindi and Hinglish folding
packages/merchant-sim/src/merchant_sim/grounding.py
packages/merchant-sim/tests/test_catalogue.py
packages/merchant-sim/tests/test_search.py
packages/merchant-sim/tests/test_textfold.py
```

## 5. Files you must never touch

Editing these breaks money handling or collides with Claude's in-flight work. If one must
change, **write the request in `docs/briefs/REQUESTS_TO_CLAUDE.md` instead of editing.**

```
packages/transaction-kernel/**      the kernel
packages/platform-db/**             schema, migrations, row-level security
packages/payment-adapters/**        Razorpay
packages/durable-work/**            the outbox
packages/commerce-api/**            the HTTP API — Claude is writing it right now
packages/durable-worker/**          the worker — same
packages/commerce-domain/**         Money, canonical hashing

merchant_sim/fees.py                money arithmetic
merchant_sim/policy.py              merchant policy
merchant_sim/store.py               state the kernel reads
merchant_sim/kernel_adapter.py      implements a kernel protocol
merchant_sim/injection.py

apps/buyer-web/src/lib/api/client.ts    the API contract
apps/buyer-web/src/lib/api/types.ts     response schemas
apps/buyer-web/src/lib/api/problem.ts   error parsing
apps/buyer-web/src/lib/security/**      content security policy, nonces
apps/buyer-web/src/app/api/**           session and proxy route handlers

conftest.py, pyproject.toml, uv.lock, .github/**, infra/**, .env, .gitignore
docs/adr/**, PROJECT_SPECIFICATION.md, docs/WORKSTREAMS.md
```

`apps/buyer-web/src/lib/api/mock.ts` is **shared**. You may edit the `FIXTURE` product
array at the top. You may not change `loadState`, `productView`, `search`, or any function
producing an approval, decision or delta — those encode the contract Claude must satisfy.

## 6. Rules that must never break

Not style preferences. Each is load-bearing, and a judge may check.

1. **The approval card submits exactly what it displayed.** The content hash, amount and
   currency it posts are the values it rendered. Never re-fetch or recompute at submit
   time. A stale closure here authorises a payment for the wrong amount.
2. **Razorpay's `checkout.js` loads only on the checkout route.** Never in the layout.
3. **A browser callback is never proof of payment.** After the Razorpay handler fires, show
   "verifying" and wait for the server timeline to report capture. Never render a paid
   state from the handler alone.
4. **The session token stays in an HttpOnly cookie.** Never `localStorage`,
   `sessionStorage`, or any `NEXT_PUBLIC_` variable.
5. **Every price shown comes from a server response.** Never compute a price, tax,
   discount or total in the browser. If a number is not in the payload, do not show it.
6. **No secret reaches the browser.** The Razorpay key id is public and arrives from the
   API; the key secret never appears in frontend code.
7. **Money is integer minor units.** Paise as integers, everywhere, including any
   strike-through MRP. The current `getMockMrp()` in `product-images.ts` derives a price
   with floating-point arithmetic (`1.15 + (listPrice % 7) * 0.02`); that is a real bug in
   a payments codebase. Make it integer arithmetic, or make it real catalogue data.
8. **The agent proposes, never executes.** No UI you build may let the agent approve, pay,
   refund or revoke. Those are trusted-surface actions the human performs.

## 7. Your work, in order

### P0 — the storefront, complete and self-contained

**1. Localise every asset.** Download all product imagery and copy into the repository as
described in section 3, rewrite `product-images.ts` to resolve local paths, and delete
every remote URL. Keep the Zepto visual language: the same layout, spacing, colour, card
shapes and density.

**2. Finish the clone to full fidelity.** Home with promo banners and category rails,
category and subcategory pages, product detail, cart, and the delivery-time framing that
makes quick commerce feel like quick commerce. **Mobile first** — Zepto is a phone app and
a judge will open it on a phone. Test at 390px width before you consider a screen done.

**3. Fix the real bugs.**
   - `features/storefront/use-basket-actions.ts` around line 141: `addOne` no longer
     re-reads the basket before incrementing, so two quick taps silently lose quantity.
     Restore an authoritative read, or introduce one shared basket cache the three
     components share.
   - Dark mode is broken in every new surface: `globals.css` defines a full dark palette
     but the header, category grid, banners, category view and product detail hardcode
     light colours. Judges use dark-mode laptops.
   - The Google Fonts link is blocked by the app's own content security policy, so the
     typography silently never loads. Self-host the font files under `public/fonts/`.
   - Eight of twenty category tiles map to categories with no products and silently return
     the whole catalogue. Either give them real products or remove them. A tile must never
     lie.
   - `components/basket-link.tsx` is dead; nothing imports it.

### P1 — the AI agent surface, your biggest differentiator

**4. Build the agent panel.** This is what makes the storefront agentic rather than a
shopping app, and it is the screen the pitch video will linger on. Claude is building the
agent's backend; you build the surface, against mock responses, to a clean interface so it
swaps over later without a rewrite.

   - A dockable chat panel: message list, composer, streaming assistant text, and a clear
     visual distinction between the agent talking and the system reporting a fact.
   - **Tool activity, made visible.** When the agent searches or edits the basket, show it
     as a labelled chip: "searched catalogue: doodh", "added 2 x Amul Milk", "built
     checkout v1". This is the "explainable" half of the track's requirement and it is
     mostly a UI achievement.
   - **Proposals are not actions.** When the agent proposes a checkout, render it as a
     card that the human confirms on the trusted approval surface. Make the handoff
     visible: the agent hands over, the human approves.
   - **The refusal is the hero moment.** When the kernel returns a reapproval decision,
     render every delta as a before-and-after row with a plain sentence explaining that
     version N is invalidated and version N+1 needs approval. Design this screen carefully;
     it is the single most important thing a judge sees.
   - Multilingual: English, Hindi and Hinglish, in the input and in the rendered replies.
   - A voice panel shell that renders the transcript contract from specification 19.5
     (partial and final transcripts, freshness, degradation events). **No audio capture
     yet** — Claude builds the pipeline later; you build the surface it will fill.
   - A degradation banner for when a dependency is down, per specification 19.12.

**5. Put the Track 1 story above the fold.** The section explaining what this storefront
proves is currently collapsed below a disclosure. That section is the entire
differentiator. Give it a real place. Also restore an honest "mock data" badge so nobody
mistakes a simulated payment for a live one.

### P2 — catalogue services

**6. Make the mock and the real catalogue agree.** `mock.ts`'s `FIXTURE` has nine products
that do not exist in `merchant_sim/catalogue.py`, including an iPhone at ₹1,26,899 in an
`electronics` category the simulator has never heard of. When Claude's API lands, live mode
will not match mock mode. Pick one direction and make both sides identical.

**7. Build a convincing catalogue.** In `catalogue.py`, a real Indian quick-commerce range:
groceries, dairy, snacks, beverages, personal care, household, and whatever categories your
storefront shows. Integer paise pricing, honest stock, real product copy, categories and
subcategories that match the tiles exactly.

**8. Make search feel Indian.** Extend `search.py` and `textfold.py` so `doodh`, `दूध`,
`milk`, `atta`, `aata`, `आटा` and `chawal` all resolve to the right products, with
transliteration and common misspellings. Cover it in `test_search.py` and
`test_textfold.py`.

### P3 — merchant console and the rest

**9. `apps/merchant-console`**, a new Next.js app for the merchant side: catalogue
management, a revenue panel, and the retained-revenue evidence the eleven-step demo
produces (what the platform saved by refusing a stale approval). Read-only against mock
data is fine; Claude exposes the real endpoints later.

**10. Protocol Inspector view** once Claude exposes the data: the proof chain, grant
lifecycle, provider requests and webhook deliveries rendered as something a payments
engineer would enjoy reading.

**11. Merchant onboarding screens** for specification section 7.

**12. Accessibility and end-to-end.** Keyboard navigation, `aria-live` on state changes,
colour never the only signal, visible focus. Playwright for the eleven-step journey.
Specification 29.8.

**13. Demo assets.** README screenshots, an architecture diagram, and the visuals for the
five-minute pitch.

## 8. How to work

Every session starts with the step-zero check. If `pwd` is not your worktree, nothing else
in this brief applies:

```bash
cd /Users/vedanttyagi/Desktop/acr-worktrees/gemini-catalogue && pwd && git branch --show-current
```

Before finishing any task, all of these pass:

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

Commit small, on your branch only, with messages that say why. Never commit `.env`,
`node_modules`, `.next`, or anything matching `*api_keys*`. Never run `git merge`,
`git rebase` or `git push` — Claude and the owner handle integration.

To pick up Claude's finished work: `git merge --ff-only main`. If that fails, stop and say
so rather than resolving a conflict in a file you do not own.

## 9. When you are done

Write `docs/briefs/GEMINI_REPORT.md`: what you built, what you verified and with what
command output, what you could not finish, and every request you left in
`REQUESTS_TO_CLAUDE.md`. Claude reads that when wiring the two halves together.

Report honestly. A known gap is useful. A false claim of green costs more time than the
work it saved.
