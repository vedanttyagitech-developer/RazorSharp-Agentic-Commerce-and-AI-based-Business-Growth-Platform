# Workstreams: ownership, state, and the split

This document records what changed between commit `5fd6703` and now, which defects that
work carries, and who owns which files once the repository is split into two worktrees.
Read sections 1 and 4 at the start of every session; read the rest before touching
something you do not own.

Written 2026-09-04. Deadline 2026-09-05.

---

## 1. State of the tree right now

Verified directly. `main` is at **`e95fa9c`**. Five commits sit on the `5fd6703` checkpoint:

| Commit | Subject | Scope |
|---|---|---|
| `2c38591` | CI actually runs the database tests | CI, root conftest, 13 test markers |
| `7e854e6` | Deployment layer | infra/, .dockerignore, DEPLOY.md, validate_infra.sh |
| `d8913c9` | Storefront: categories, promotions, product surfaces | apps/buyer-web (3,768 lines) |
| `e95fa9c` | Finish the wave 2 gate: export the kernel surface | kernel `__init__.py`, lint |

**The most important fact, stated precisely.** Across `5fd6703..d8913c9` — the three
commits the analysts reviewed — **no file under any `packages/*/src` directory changed**,
and no migration changed. `git diff --stat 5fd6703..d8913c9 -- 'packages/*/src'` returns
empty. The only Python touched was 13 test files, each gaining one line
(`pytestmark = pytest.mark.db`): 26 insertions, 0 deletions, no assertion or fixture altered.

**That is no longer true of the tip.** `e95fa9c` modifies
`packages/transaction-kernel/src/transaction_kernel/__init__.py`, +165/-3. I read it: it is
**additive re-exports only**. The wave 2 modules (approvals, checkout_content, checkouts,
evidence, payments, refunds) were unreachable from the package root, so nothing outside the
kernel could import them. The three removed lines are an import replaced by an expanded
multi-line form plus two `__all__` entries moved during alphabetisation; `ActorType` and
`MerchantStateSource` are both still exported (verified). No kernel logic changed. The same
commit reorders imports in eight `payment-adapters` tests to clear lint.

So: **the money path's behaviour is untouched. The kernel's public surface grew.**

**Uncommitted right now** — further product-UI work on top of `d8913c9`, which no analyst
reviewed: `app/products/[sku]/page.tsx`, `components/product-img.tsx`,
`features/storefront/product-detail.tsx` (+184), and `app/globals.css`.

**Worktrees already exist** — five, not the three the split plan assumed:

```
.../Agentic Commerce for Razorpay   e95fa9c  [main]
.../acr-worktrees/claude-backend    e95fa9c  [claude/backend]
.../acr-worktrees/gemini-catalogue  e95fa9c  [gemini/catalogue]
.../acr-worktrees/agent-runtime     6612087  [wt/agent-runtime]
.../acr-worktrees/voice             494b36b  [wt/voice]
```

`claude/backend` and `gemini/catalogue` are cut and current. What remains is the
per-worktree database and the merge protocol (section 5).

---

## 2. The four groups of change

### 2.1 CI and test-database policy (`2c38591`)

Before this, CI exported only `DATABASE_URL`, which no database conftest reads. Every RLS
and concurrency suite skipped and CI reported green while proving nothing —
`docs/THREAT_MODEL.md:20-24` said so. Fixed, and verified rather than trusted: all four
names the test tree reads (`DATABASE_URL_TEST_{ADMIN,KERNEL,APP,WORKER}`) are exported and
every spelling matches. `scripts/setup_ci_db.py` applies migrations, then bootstraps roles,
then asserts each is `NOSUPERUSER NOBYPASSRLS` with `LOGIN`. That last assertion is
load-bearing: a superuser bypasses RLS and would make the whole isolation suite pass
vacuously. Roughly 25 modules move from unproven-in-CI to proven. Keep all of it. The
accompanying root `conftest.py` is a different matter — see D2.

| File | Change | Risk |
|---|---|---|
| `.github/workflows/ci.yml` | +79/-6; db bootstrap, 4 role DSNs, frontend + infra jobs | med |
| `scripts/setup_ci_db.py` | new, 170 lines; migrate → bootstrap → verify | med |
| `conftest.py` (root) | new, 51 lines; any skip fails the session | **high** |
| 13 × `packages/*/tests/test_*.py` | +2 each; `pytestmark = pytest.mark.db` | none |

### 2.2 Deployment and packaging (`7e854e6`)

A GKE Autopilot layer: three multi-stage images, Kustomize base plus two overlays,
Terraform, and a runbook. The analyst re-ran the non-Docker half of `validate_infra.sh`: all
four overlays render and kubeconform reports 29 resources valid with **zero skipped
schemas**, so the CRDs are genuinely checked. Terraform never writes a secret into state —
no `google_secret_manager_secret_version` anywhere, and `google_sql_user` uses IAM auth with
no password attribute. The infrastructure is demo-ready; **the demo is not**, and the runbook
says so: `commerce_api/app.py` and `durable_worker/main.py` do not exist, so two of three
workloads would CrashLoopBackOff. Only buyer-web would serve.

| File | Change | Risk |
|---|---|---|
| `.dockerignore` | new, 50 lines; excludes `.env`, `*.csv`, key patterns | low |
| `docs/DEPLOY.md` | new, 342 lines; empty project → live host | low |
| `infra/sql/01-…`, `02-…` | new; migration + app identity grants | med |
| `infra/…/db-migrate/networkpolicy.yaml` | new; policy **moved**, byte-identical | none |
| `infra/…/workloads/networkpolicies.yaml` | -29; deletion half of that move | none |
| `infra/docker/buyer-web.Dockerfile` | guards `cp -r public/.` on empty dir | none |
| `scripts/validate_infra.sh` | new, 265 lines; 6-phase validation harness | low |
| `.gitignore` | +3; `.terraform/`, `*.tfstate*` | none |

The 29-line deletion looks alarming and is not a regression. Two analysts independently
diffed both sides: identical bodies, same `policyTypes: [Ingress, Egress]`, no CIDR widened.

### 2.3 Buyer storefront and catalogue UI (`d8913c9`)

~2,000 lines of capable catalogue UI: category grid and view, promo banners, image handling
with an error fallback, quantity steppers, richer product and basket pages. The interaction
work is a real improvement — an in-flight mutation guard, a retry affordance, a
screen-reader live region, and URL-as-state so Back and deep links work.

Critically, it **did not touch the governed core**. `approval-card.tsx`,
`razorpay-launcher.tsx`, `checkout-journey.tsx`, `lib/razorpay.ts` and the API proxy are
byte-identical. `mock.ts`'s eleven-step walk still injects the price change, emits
`REVALIDATION`, computes exact deltas, and enforces the three-key echo. The basket renders
every figure through `formatMinor(basket.quote.<field>)` and recomputes nothing.

What makes it unshippable is framing, not code. The app titles itself **"Zepto Clone Demo"**,
renders that wordmark in `#950EDB`, and hotlinks a real competitor's CDN for 100% of its
imagery through a widened CSP — 112 references in `product-images.ts` plus 21 across nine
other files. `public/` holds only a `.gitkeep`, so there is no fallback.

| File | Change | Risk |
|---|---|---|
| `lib/product-images.ts` | new, 447 lines; 83 CDN URLs, fabricated MRP | **high** |
| `lib/security/csp.ts` | `img-src` gains the CDN, all routes | **high** |
| `app/layout.tsx` | title → "Zepto Clone Demo"; Google Fonts link | **high** |
| `components/app-header.tsx` | 25→299 lines; wordmark; **drops mock-mode badge** | **high** |
| `storefront/use-basket-actions.ts` | rewrite; guard + retry, but lost-update bug | **high** |
| `lib/api/mock.ts` | +61 additive; 9 SKUs, 404 path now unreachable | **high** |
| `storefront/search-panel.tsx` | ~560 lines; URL-as-state homepage shell | low |
| `storefront/product-detail.tsx` | ~517 lines; fabricated rating, EMI claim | med |
| `storefront/{category-view,category-grid,promo-banners}.tsx` | new, ~734 lines | med |
| `storefront/basket-view.tsx` | ~431 lines; money still server-derived | low |
| `components/product-img.tsx` | new, 46 lines; `SafeImage` error fallback | none |
| 2 new test files | 597 lines; real behavioural coverage | none |

### 2.4 Cross-cutting

Two facts span all three commits. The financial packages are untouched (section 1). And
secrets are correctly excluded from both git and the Docker build context —
`git check-ignore` resolves `.env` to `.gitignore:3` and the Razorpay key CSV to line 8
(`*_api_keys*`). The new `.dockerignore` closed a live leak: the build context is the
repository root and there was previously no `.dockerignore`, so `.env` and the key CSV
were shipped to the Docker daemon on every build.

---

## 3. Defects and decisions needed

Most severe first. Every item names a file that was actually opened.

**D1 — Zepto branding and asset hotlinking.** `lib/product-images.ts`, `csp.ts:33`,
`layout.tsx`, `app-header.tsx`, `globals.css`. A public Razorpay submission serves a live
competitor's photography by hotlink, renders its wordmark and brand colour, titles itself a
clone, and prints `support@zeptonow.com` plus "Commodum Groceries Private Limited" —
Zepto's real operating entity — as seller, with real FSSAI numbers. It also contradicts
your own backend: `merchant_sim/catalogue.py` states nothing there integrates with any real
retailer. Second-order: a hotlink block mid-judging blanks the storefront. *Fix:* self-host
placeholders under `public/products/`, revert `img-src`, rename to a fictional merchant,
retokenise colours. Keep `SafeImage`.

**D2 — root `conftest.py` enforces a rule broader than the ADR, quoting text that does not
exist.** `conftest.py:14-26`. ADR 0003 D12 reads: *"CI applies migrations and bootstraps
roles, and fails if any `db` test is skipped."* Two scopes — CI, and `db` tests. The hook
honours neither: it sets `session.exitstatus = TESTS_FAILED` on any skip, any marker, any
machine. Its docstring quotes D12 as *"no test may skip because a dependency is
unconfigured; a skipped test is an integration failure"*; I grepped `docs/` and
`PROJECT_SPECIFICATION.md` and **that sentence appears nowhere in the repository**.
*Consequence:* a laptop without PostgreSQL exits 1 though every pure test passed, with no
test marked failed — so the junitxml shows zero failures beside a red job. It also
contradicts the `razorpay_live` marker at `pyproject.toml:54`, and both sibling worktrees
inherit it on rebase. *Fix:* gate on `os.environ.get("CI")` and on the `db` keyword; better,
push enforcement into the fixtures so an unreachable database fails a *named* test.

**D3 — basket lost-update.** `use-basket-actions.ts:131-138`. The PUT sets an absolute
quantity. The old code read the basket from the server first; the rewrite deleted that
docstring and now reads `lastBasket?.lines.find(…)?.quantity ?? 0` from React state, which
is `null` on first render. A buyer with 3 units clicks ADD before hydration; the client
PUTs quantity 1 and silently destroys 2 units while reporting success. Three components
each hold their own hook instance, so the caches never agree. *Fix:* re-read before
incrementing, or share one cache via `BasketRefContext`.

**D4 — exit-code clobber.** `conftest.py:26` assigns `session.exitstatus` unconditionally.
Ctrl-C during a run where anything skipped turns exit 2 into exit 1. *Fix:* escalate only
from `ExitCode.OK`.

**D5 — Google Fonts blocked by the app's own CSP.** `layout.tsx:14-16` loads Inter from
`fonts.googleapis.com`, but `csp.ts` sets `style-src` to `['self','unsafe-inline']` and
`font-src` to `['self','data:']`. Inter never loads on any route and two CSP violations
appear on every page load. *Fix:* self-host via `next/font`.

**D6 — mock and live now demonstrate different systems.** `mock.ts:98, 352, 842`. Nine SKUs
added (six oils, an iPhone at ₹1,26,899) that do not exist in `merchant_sim/catalogue.py`
(49 SKUs, all `GRO-*`, no `ELECTRONICS` category). `productView()` no longer 404s an unknown
SKU — it fabricates and persists one, deleting the `UNKNOWN_CATALOGUE_ITEM` path. Empty
query returns the whole catalogue with `score: 1, matched_terms: ["catalogue"]`, so a
grounding consumer cannot tell a real match from "we listed everything".

**D7 — fabricated commercial and financing claims.** `product-detail.tsx:402-404,440`,
`product-images.ts:198-216`: a hardcoded "★ 4.8 (32 reviews)", an MRP synthesised by float
arithmetic off the selling price, an "₹X OFF" badge, and "No Cost EMI available". On a
payments submission judged by payments people, a financing claim the integration does not
implement is the worst of these. `promo-banners.tsx:88-100` separately claims "₹0 Delivery
Fee" while the quote engine correctly charges one below the threshold.

**D8 — mock-mode disclosure removed.** `app-header.tsx` dropped the "Mock mode (no backend)"
badge and the "Synthetic products; Razorpay test mode" tagline; `API_MODE` now has no UI
consumer. Nothing says the demo is fabricated in-browser. *Fix:* restore the badge.

**D9 — over-claiming static copy.** `basket-view.tsx:460` renders "Admitted once via
server-side verification. Bound to Version 1 Policy-at-Sale Receipt…" unconditionally, in a
screen *before* admission, version hardcoded. The money there is correctly derived; the
claim is not.

**D10-D16 — deployment and CI, all verified against the files.**

| # | File | Consequence | Fix |
|---|---|---|---|
| D10 | `setup_ci_db.py:66` vs `platform-db/migrations/env.py:14` | Sets `sqlalchemy.url`, but env.py ignores Alembic config and reads `os.environ["DATABASE_URL"]`, raising if absent. CI is masked only because it sets both variables identically | Set `os.environ["DATABASE_URL"]` in `run_migrations` |
| D11 | `validate_infra.sh` | Installs kubeconform via `curl … \| tar xz` + `sudo mv`, no checksum, in a workflow that pins all six Actions by SHA | Pin a SHA256 |
| D12 | `infra/terraform/registry.tf:35` | Grants only `artifactregistry.reader` (the sole binding), but `DEPLOY.md` step 5 makes `gcloud builds submit` the image path — the push fails in a new project | Add a writer binding for the build identity |
| D13 | `.dockerignore:4-5` | `.env`/`.env.*` are unanchored, matching only the context root; a `.env` under `packages/` lands in a layer | `**/.env`, `**/.env.*` |
| D14 | `terraform.tfvars.example:1` | Claims tfvars are gitignored. They are not — `.gitignore:39-41` is `.terraform/`, `*.tfstate`, `*.tfstate.*` only | Add `*.tfvars` |
| D15 | `variables.tf:37` + baseline NetworkPolicy | `enable_fqdn_network_policy` defaults false while the baseline allows only DNS and metadata, so after one Terraform apply every Cloud SQL sidecar and the migration Job hang. `DEPLOY.md` calls this "Worker cannot reach api.razorpay.com" | Widen the troubleshooting row; sequence both applies |
| D16 | `merchant-sim/tests/test_ms_kernel_adapter.py` | Calls `pytest.skip` with no `pytestmark`, so `-m db` under-selects. *Correction:* one analyst also listed `test_tk_refunds.py` — I checked, it **is** marked at line 60 | Add the marker |

**D17 — smaller, all verified.** `setup_ci_db.py:122` defines a function named
`test_role_connections` taking a non-fixture positional argument (collectable if anyone runs
`pytest .`). `category-grid.tsx:16-41` — eight of twenty tiles map to `filterKey: 'all'`, so
"Jewellery" returns milk and atta. `search-panel.tsx` no longer sets `phase('searching')`,
making one of the sixteen journey states unreachable. `basket-link.tsx` was improved and
orphaned in the same commit. Dark mode is broken across five new files (96 hardcoded light
values, zero `dark:` variants). `ci.yml` embeds the literal password `testpw` four times.
mypy covers one package of eight. `node-version: 22` contradicts spec 21.8's Node 24.
`test-results/` is not gitignored. `docs/THREAT_MODEL.md:116` still calls the buyer surface
"Planned" and says `next.config.ts` is empty — false since `2577965`.

### Questions only you can answer

1. **Was the Zepto cloning deliberate, or did an assistant extrapolate it from screenshots
   you pasted for layout reference?** The comments read "matching Screenshot 1/2/3" and
   "Exact 20 official category graphics from zepto.com". This blocks submission either way.
2. **Sync direction for the nine mock-only SKUs:** add them (plus an `ELECTRONICS`
   category) to `catalogue.py`, or trim the mock to the 49 real SKUs? The second keeps mock
   and live provably identical, which is the claim the demo rests on.
3. **Scope the D12 skip rule to CI and `db`-marked tests?** And amend the ADR to actually
   contain the sentence the conftest quotes?
4. **Fix the boundary crossings before or after the split?** Branching from `e95fa9c` makes
   them the baseline both worktrees inherit.
5. **Are you actually deploying to GCP before the deadline, or is `infra/` design
   evidence?** It decides whether D12 and D15 are worth an hour each today.
6. **Two workloads have no entrypoint.** `commerce_api/app.py` and `durable_worker/main.py`
   do not exist. That, not the infrastructure, is the critical path. In scope now?
7. **`payment_adapters.razorpay.env.load_config_from_env` requires
   `RAZORPAY_WEBHOOK_SECRET`, but the durable-worker SecretProviderClass withholds it by
   design.** Add a client-only loader, or build the worker's transport from key id/secret?
8. **The eleven-step demo injects its price change on the first basket line.** If a judge
   starts from the ₹1,26,899 iPhone the +₹10 delta is invisible. Is the script pinned to a SKU?
9. **`roles.py` declares five roles; `infra/sql` grants three.** Are `commerce_migration`
   and `commerce_analytics` aspirational, or has deployment not caught up?
10. **Merge cadence with the deadline tomorrow** — fixed intervals, or only on a green gate?

---

## 4. Ownership map

**CLAUDE** / **GEMINI**: edit freely. **SHARED-PROTOCOL**: one owner writes, the other
requests. **SHARED-FROZEN**: additive only, owner-approved.

| Path | Owner | Why |
|---|---|---|
| `packages/transaction-kernel/**` | CLAUDE | Admission, grants, approvals, receipts, content hash |
| `packages/platform-db/**` | CLAUDE | Schema, migrations, RLS, role grants |
| `packages/commerce-domain/**` | SHARED-FROZEN | Money, JCS, `canonical_hash`; both import it |
| `durable-work/**`, `durable-worker/**`, `payment-adapters/**` | CLAUDE | Outbox, worker, Razorpay |
| `packages/commerce-api/**` | CLAUDE | Except `routers/catalogue.py`, `services/catalogue.py` → GEMINI |
| `merchant_sim/{catalogue,search,textfold}.py` | GEMINI | Verified: import only `commerce_domain.Money` and each other |
| `merchant_sim/{store,grounding}.py` | SHARED-PROTOCOL | Gemini edits shape, Claude reviews semantics |
| `merchant_sim/{fees,kernel_adapter,policy,scenarios,injection}.py` | CLAUDE (frozen) | The only code that computes a total |
| `merchant_sim/__init__.py` | SHARED-PROTOCOL | Append-only; never reorder |
| `merchant-sim/tests/{test_fees,test_ms_kernel_adapter}.py` | CLAUDE | The tripwire |
| `merchant-sim/tests/{test_catalogue,test_search,test_textfold}.py` | GEMINI | |
| `buyer-web/src/lib/api/{types,client,problem,session-client}.ts` | SHARED-PROTOCOL (Claude) | The wire contract |
| `buyer-web/src/lib/api/mock.ts` | SPLIT | `FIXTURE` array → GEMINI; the rest → CLAUDE |
| `src/features/storefront/**`, `product-images.ts`, `product-img.tsx` | GEMINI | Product UI |
| `components/{approval-card,evidence-drawer,journey-rail,quote-breakdown,razorpay-launcher}.tsx`, `features/{checkout,order}/**`, `lib/{hash,money,journey,razorpay}.ts` | CLAUDE | Trusted surface |
| `lib/security/csp.ts`, `lib/server/env.ts`, `proxy.ts`, `app/api/**`, `next.config.ts` | CLAUDE | Security posture |
| `app/{layout,page,globals.css}`, `components/{app-header,ui}` | GEMINI | Shell and design |
| `components/providers.tsx` | SHARED-PROTOCOL | Constructs the client; holds the basket ref |
| `infra/**`, `scripts/**`, `.dockerignore` | CLAUDE | Deployment, grants, roles |
| `.github/workflows/ci.yml` | SHARED-PROTOCOL (Claude) | `frontend` job body → GEMINI |
| `conftest.py`, `pyproject.toml`, `uv.lock` | SHARED-PROTOCOL (Claude) | |
| `PROJECT_SPECIFICATION.md`, `docs/adr/**`, `STATUS.md`, `THREAT_MODEL.md` | SHARED-FROZEN | Owner only; ADRs append-only |
| `fixtures/golden/**` | SHARED-FROZEN | Regression anchors |

### The difficult cases, resolved

**`kernel_adapter.py` is the boundary, and the boundary is the file.** I read its imports: it
pulls `CurrentMerchantState`, `build_checkout_content`, `ContentLine`, `ReceiptInputs` and
the receipt policy types straight from `transaction_kernel`, and its output is hashed into
the approval. Gemini should never open it. Gemini's obligations when changing catalogue code:
`quote_basket(basket, store=…)` keeps its signature; `Quote` keeps
`lines/items_subtotal/items_tax/delivery_fee/delivery_tax/total/currency/freshness` with those
exact names; unavailability is reported as *unavailable*, never re-priced; `FeePolicy` keeps
its four fields; an unchanged store keeps producing the same `catalogue_revision`.

**`lib/api/` belongs to the side that must satisfy the contract.** If the UI side may edit
`types.ts`, it can "fix" a mismatch by loosening a schema and the backend never learns it
drifted — worse than a merge conflict, because it is silent.

**`packages/agent-runtime` is Claude's and stays in its own worktree.** Its
`google-adk`/`google-genai` dependency refers to Gemini the *model provider*, not Gemini the
coding assistant. Ownership follows what code does: this is the capability broker and
grounding fence, the code that stops an agent acting outside its grant. The one shared seam
is the catalogue tool argument shapes in `capabilities/tools.py`.

**`merchant-sim` genuinely straddles the line.** Its own docstrings draw it: a `Product`
carries no stock, price or availability, because those live in `MerchantStore`. Any catalogue
edit reaching `fees.py`, `kernel_adapter.py`, `store.py` or `policy.py` is a money-path
change wearing product-data clothing.

### When you need to change something you do not own

Do not edit it. Append a numbered entry to `docs/HANDOFF.md` on your own branch: the file,
the exact change, why, and what breaks without it. The owner implements it on their branch
and it arrives in the next merge. `HANDOFF.md` is the single documentation exception —
append-only, one section per side, so its conflicts stay trivial.

An edit to `merchant-sim/tests/test_ms_kernel_adapter.py` from the catalogue worktree is a
**merge blocker**, not a negotiation. That test is the tripwire for every rule above; if the
side that broke the seam may edit the test that catches it, the split has no enforcement.

---

## 5. Working in parallel: the mechanics

**Layout.** `claude/backend` and `gemini/catalogue` already exist at `e95fa9c` (verified).
The original checkout stops being a place anyone edits and becomes the integration worktree
where `main` lives and merges happen. Say that to both assistants — a split where one side
keeps editing `main` is not a split.

**Per worktree, once.** Git carries tracked files only:

```
WT=/Users/vedanttyagi/Desktop/acr-worktrees/gemini-catalogue
cp "$MAIN/.env" "$WT/.env"            # 0600. Do NOT copy the keys CSV.
cd "$WT" && PATH="$HOME/.local/bin:$PATH" uv sync --all-extras --dev
cd "$WT/apps/buyer-web" && npm ci
```

Ports so both stacks run at once: backend keeps API 8000 / Next 3000; catalogue uses 8001 / 3001.

**The database collision is the real hazard.** Both sides resolve to
`localhost:5432/commerce_test` — hardcoded as the default at
`platform-db/tests/conftest.py:20,24,32` and `transaction-kernel/tests/conftest.py:41`
(verified). Two pytest runs there fight over rows and locks, and the contention tests in
`test_grants.py` and `test_admission.py` open real contending sessions and assert a single
winner. They will fail nondeterministically — the worst failure to debug the day before a
deadline. Fix: one database per worktree, same cluster, same roles.
`bootstrap_test_roles.sql` is idempotent and its GRANTs apply to the connected database:

```
createdb commerce_test_cat && createdb commerce_dev_cat
cd "$WT" && PATH="$HOME/.local/bin:$PATH" \
  DATABASE_URL=postgresql+psycopg://vedanttyagi@localhost:5432/commerce_test_cat \
  DATABASE_URL_TEST_ADMIN=postgresql+psycopg://vedanttyagi@localhost:5432/commerce_test_cat \
  uv run --no-sync python scripts/setup_ci_db.py
```

Then a gitignored env file per worktree exporting all five `DATABASE_URL*` names at the
`_cat` database. Both variables above are needed — see D10. Worth doing: change those
conftest defaults to require the environment variable and fail loudly. Today a worktree that
forgets its exports quietly runs against the *other* side's database. Five lines on Claude's
side, and it removes the only silent failure mode in this plan.

**Gates.** Backend: `uv run --no-sync pytest packages/ -q`, plus the frontend suite when it
touches `src/lib/api`. Catalogue: `npm run lint && npm run typecheck && npm run test && npm
run build`, plus `uv run --no-sync pytest packages/merchant-sim -q` for any
`catalogue.py`/`search.py` change — including `test_fees.py` and `test_ms_kernel_adapter.py`,
not to own them but because they say whether the seam broke.

**Merge protocol.**

- The owner merges. Neither assistant merges its own branch or checks out `main`.
- Order is fixed: **backend first, UI second**. Merge `claude/backend` → `main`; Gemini
  rebases `gemini/catalogue` onto it and fixes its own breakage; then merge. Reversed, the
  UI merges against a contract that is about to change.
- Rebase, never repeated back-merges. The history is part of the submission.
- `uv.lock` conflicts are never hand-merged: take `main`'s file, re-apply the
  `pyproject.toml` member addition, run `uv lock` once in the integration worktree.
- Pre-merge gate against a third database: the full backend suite, the four frontend
  commands, and the D12 basename check — `ls packages/*/tests/*.py | xargs -n1 basename |
  sort | uniq -d` must print nothing, because pytest prepend import mode breaks on duplicate
  basenames and both sides will independently want to write `test_catalogue.py`.
- `wt/agent-runtime` and `wt/voice` are behind and both modify `pyproject.toml` and
  `uv.lock`. Rebase each before it merges; merge one at a time, regenerating the lock after each.

---

## 6. Contracts that must not break

| Invariant | Defined in | How it breaks |
|---|---|---|
| Canonical checkout content and its frozen hash | `transaction_kernel/checkout_content.py`; vector in `test_tk_checkout_content.py` | Any field rename or reorder changes every approval hash and every receipt already issued |
| `MerchantStateSource` / `CurrentMerchantState` | `transaction_kernel/admission.py:117-127`, implemented in `merchant_sim/kernel_adapter.py` | A catalogue refactor renaming a `Quote` field breaks admission at runtime, not import |
| An unchanged store reproduces approved content byte-for-byte | `merchant-sim/tests/test_ms_kernel_adapter.py` | A non-deterministic `catalogue_revision` denies every submit with `REAPPROVAL_REQUIRED` — step 5 firing when it should not |
| Only the fee engine computes a total | `merchant_sim/fees.py` | Browser-side money — `product-images.ts:198` already does this (D7) |
| Money is integer minor units; JCS canonicalization | `commerce_domain/{money,jcs,hashing}.py` | Float arithmetic anywhere; changing serialization invalidates every recorded hash |
| The wire contract | `buyer-web/src/lib/api/types.ts`, ADR 0003 endpoint catalogue | A loosened Zod schema turns a backend break into a silent UI bug that surfaces on stage |
| Buyer echoes hash + amount + currency, exactly three keys | `components/approval-card.tsx`, `lib/hash.ts`, mock's `approveVersion` | Changing what is echoed, or when confirm enables, changes what the buyer authorised |
| Kernel denials arrive as 200 with a structured decision | ADR 0003 D15, `lib/api/client.ts` | Mapping a denial to an HTTP error hides the governance story |
| App role physically cannot write financial tables | `platform_db/rls.py`, `roles.py`, `scripts/bootstrap_*.sql` | A grant loosened to make a test pass erases what `test_tenant_isolation.py` proves |
| Only the worker talks to Razorpay | ADR 0003 D3; `infra/…/networkpolicies.yaml` | An egress rule added to another workload |

Attribution of individual edits to one assistant or the other is **uncertain** throughout
and is deliberately not asserted — changes are described by content instead. The one
attribution claim with hard evidence is that `d8913c9`'s own commit message flags the Zepto
CDN as "recorded for the owner rather than reverted unilaterally", which is not how an
author describes their own change.
