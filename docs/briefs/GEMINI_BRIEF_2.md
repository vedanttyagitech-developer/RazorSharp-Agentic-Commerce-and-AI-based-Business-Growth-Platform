# Brief 2 for Gemini — the submission surface

Your first brief is complete and merged. This is the next workload. Everything in brief 1
about worktrees, ownership and the rules that must never break still applies; read
`docs/briefs/GEMINI_BRIEF.md` again if anything is unclear.

---

## STEP ZERO — every session, before anything else

```bash
cd /Users/vedanttyagi/Desktop/acr-worktrees/gemini-catalogue
pwd
git branch --show-current
git merge --ff-only main
```

`pwd` must print `/Users/vedanttyagi/Desktop/acr-worktrees/gemini-catalogue` and the branch
must be `gemini/catalogue`. The merge pulls in Claude's work, including your own merged
commits. If the merge fails, stop and say so — do not resolve a conflict in a file you do
not own.

## What you did, and what it means

Your five commits are merged. Verified here before merging, not taken on report: the
basket lost-update is genuinely fixed, 79 assets are local with zero external image URLs
remaining, the mock and simulator catalogues now hold the same 58 products with no
divergence in either direction, the merchant console compiles, and the fabricated MRP now
uses integer basis points. The ownership boundary held across all five commits, and the
one change you needed in a file you do not own was requested in writing rather than taken.
That is exactly how this is supposed to work.

Claude has meanwhile removed the external image host from the content security policy,
which your localisation made possible.

## The situation

The submission is due today. Claude is finishing the HTTP API and the durable worker, which
is the piece that lets the system take a real Razorpay test-mode payment. Until that lands,
the storefront runs on mock data.

Your remaining work is the **submission surface**: the things a judge sees before they ever
run the code, and the things that make the demonstration survive being watched on a phone.

## Priority 1 — the README is the front door

`README.md` is 36 lines and contains no images. A judge opens the GitHub repository before
anything else, and right now it undersells a project with a 13,527-line transaction kernel
whose single-winner guarantee is proven with real contending database sessions.

Rewrite it. It must, in this order:

1. **Say what this is in two sentences**, including the one line the architecture exists to
   prove: agents propose; deterministic systems authorize and execute.
2. **Show it.** Screenshots of the storefront, the agent panel mid-conversation with tool
   chips visible, and above all the refusal card showing the price change caught underneath
   an approved checkout. Put images under `docs/images/` and keep the total under 5 MB.
   That refusal screenshot is the single most persuasive artefact in the repository.
3. **The eleven-step demonstration** as a short numbered list, with steps 5 to 8 called out
   as the part most conversational-commerce demos skip.
4. **An architecture diagram.** A clean SVG or WebP showing browser, API, kernel,
   PostgreSQL, worker, Razorpay, and the boundary the agent cannot cross. Draw the money
   path in one colour and the agent's reach in another; the picture should make the claim
   without a caption.
5. **How to run it**, pointing at `docs/DEMO.md` rather than duplicating it.
6. **An honest state-of-play table**, linking to `docs/STATUS.md`. Do not overstate.
   A submission candid about its gaps reads as more trustworthy than one that is not, and
   an overstated claim found by a judge costs more than an admitted gap.

Keep it tight. A reviewer skims; make the first screen carry the argument.

## Priority 2 — it will be opened on a phone

Quick commerce is a phone experience and the storefront is a clone of one. Only six files
currently use responsive classes, which suggests the mobile pass has not really happened.

- Test every screen at 390px wide: home, category, subcategory, product, basket, checkout,
  order, and the agent panel. Fix what breaks.
- The agent panel needs a real mobile treatment. A docked side panel does not work at
  390px; make it a bottom sheet or a full-screen mode with a clear way back to the
  storefront.
- Tap targets at least 44px. No horizontal scrolling on the body at any width.
- Check the approval card and the refusal card specifically. They carry hashes and money,
  they are the screens that matter most, and long monospace strings are exactly what breaks
  narrow layouts.

## Priority 3 — accessibility and an end-to-end test

Specification 29.8 asks for both and neither exists.

- **Accessibility**: keyboard navigation through the whole purchase journey without a
  mouse; `aria-live` on every state change the buyer needs to notice, especially the
  transition into "payment pending verification" and the arrival of a refusal; visible
  focus rings; colour never the only signal for availability or state; correct heading
  order. Run an audit and fix what it finds.
- **Playwright**, in `apps/buyer-web/e2e/`: one test that walks the whole eleven-step
  journey in mock mode and asserts the things that matter — that the approval card submits
  exactly the hash it displayed, that the refusal renders every delta, that version N is
  shown as invalidated and N+1 requires approval, and that no paid state appears before the
  timeline reports capture. Wire it into `package.json` as `npm run e2e`.

## Priority 4 — be ready for the real API

When Claude's API lands, the storefront must work against it without a rewrite. Prepare
now, without touching the files you do not own:

- Exercise every loading, empty and error path in the UI. A live API is slower than a mock
  and will surface states the mock never produced. Nothing should flash unstyled or jump.
- Every error must render as something a human understands, using the problem detail the
  server returns rather than a raw status code.
- Make the mode obvious. A visible badge saying whether the app is on mock data or a live
  API, so nobody watching a demo mistakes one for the other.
- Do not change `lib/api/client.ts`, `types.ts` or `problem.ts`. If a response shape looks
  wrong, write it in `docs/briefs/REQUESTS_TO_CLAUDE.md`; those files are the contract and
  Claude reconciles them.

## Priority 5 — only if the above is genuinely finished

- Merchant onboarding screens, specification section 7.
- Deepen the agent panel: streamed replies, and Hindi and Hinglish rendering end to end.
- Polish the merchant console's evidence view once Claude's real endpoints exist.

## Same boundaries as before

Never touch: `packages/transaction-kernel`, `packages/platform-db`,
`packages/payment-adapters`, `packages/durable-work`, `packages/commerce-api`,
`packages/durable-worker`, `packages/commerce-domain`; `merchant_sim`'s `fees.py`,
`policy.py`, `store.py`, `kernel_adapter.py`, `injection.py`; `lib/api/client.ts`,
`types.ts`, `problem.ts`; `lib/security/**`; `app/api/**`; `conftest.py`, `pyproject.toml`,
`uv.lock`, `.github/**`, `infra/**`, `docs/adr/**`, `PROJECT_SPECIFICATION.md`.

`mock.ts` stays shared: the `FIXTURE` array is yours, the contract functions are not.

## Gate before every commit

```bash
cd apps/buyer-web && npm run lint && npm run typecheck && npm run test && npm run build
cd ../merchant-console && npm run lint && npm run build
export PATH="$HOME/.local/bin:$PATH"
uv run --no-sync python -m pytest packages/merchant-sim -o addopts="" -q
```

Commit small, on your branch only. Never merge, rebase or push. When done, append to
`docs/briefs/GEMINI_REPORT.md` with real command output for every claim.

One note on the last report: it described the merchant console's figures without saying
they are simulated. The pages themselves do label them, which is right. Keep it that way in
the README too — every number a judge sees must be either real and traceable, or plainly
marked as illustrative.
