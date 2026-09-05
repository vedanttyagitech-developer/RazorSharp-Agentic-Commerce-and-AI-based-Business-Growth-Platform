# Security review: the running platform's authentication, credentials, isolation and proxies

Date: 2026-09-05. Method: adversarial, against the platform **as it runs** —
`http://127.0.0.1:8000` (commerce-api), `http://localhost:3000` (buyer storefront),
`http://localhost:3001` (merchant console) — plus source reading only where a request could
not settle a question. Every claim in `docs/THREAT_MODEL.md` and in the two proxy files was
treated as a hypothesis to falsify. Where a claim held, it is recorded as held; where it
broke, it was fixed with a regression test; where it could not be reached, it is named as a
blind spot rather than asserted.

The running services are served from the **main checkout**, not this worktree, so the live
probes below exercised the same code this branch started from; the fixes were verified
against this worktree (the Python suite, and a production `next build` + `next start` of the
storefront on a spare port).

## Headline

Four real defects, all fixed with a test that would have caught them:

| # | Severity | Defect | Fix |
| --- | --- | --- | --- |
| 1 | High | The buyer storefront proxy handed the browser a **raw bearer token** (with a caller-chosen `buyer_ref`) via `POST /api/backend/v1/demo/sessions` | Refuse `/v1/demo/*` from the browser-facing handler, as the console proxy already does |
| 2 | Medium | `GET/POST /v1/orders/{id}` was a **cross-buyer existence oracle** and leaked another buyer's `checkout_id` | A non-owned order now answers exactly as a missing one |
| 3 | Medium | `POST /v1/ops/outbox/{id}/revive` had **no actor guard**, so an AGENT holding the scenario key could re-drive a money command | Refuse `ActorType.AGENT`, mirroring the Safe Mode switch |
| 4 | Low | A malformed body could make the validation handler itself raise → **client-triggerable 500** | Encode validation errors JSON-safely (bytes decoded), as FastAPI's own handler does |

Everything else tested **held**. Full detail below, then the latent gaps I chose to
document rather than change on a hunch, then the blind spots.

Gate: `ruff check` and `ruff format --check` clean on `packages`; `mypy` strict passes over
227 source files; the storefront builds. Test suite **4521 passed, 6 skipped, 0 failed**;
my delta is **+8 tests** over a pre-change worktree baseline of 4513, zero regressions. The
6 skips are pre-existing (`voice-runtime` real-audio tests, skipped because
`GOOGLE_CLOUD_PROJECT` is unset), not introduced here; the stated 4519 on main is that
baseline with those 6 running under CI credentials.

---

## 1. Defects found and fixed

### 1.1 High — the storefront proxy leaks a working bearer token to the browser

**Claim tested (from `apps/buyer-web/.../route.ts`):** "The browser … never sees a bearer
token. … An XSS that reaches `document.cookie` therefore still cannot read it … It does not
forward arbitrary headers … a caller cannot smuggle its own `Authorization`."

**Falsified.** The proxy forwards *any* path to the API with the session bearer, and it did
**not** exclude the session-mint route the way the console proxy does. So a page on the
storefront origin could ask the proxy to proxy the mint route:

```
POST http://localhost:3000/api/backend/v1/demo/sessions
{"tenant_slug":"demo","actor_type":"BUYER","buyer_ref":"victim-alice"}
→ 201 {"token":"owwkORtHDxXC…","buyer_ref":"victim-alice", …}
```

The token is a **real, working credential** — used directly against the API it returned
`200` on `GET /v1/orders` and `/v1/config` — and its `buyer_ref` is **attacker-chosen**.
Because ownership everywhere keys on `buyer_ref` (`deps.assert_owner`:
`row.buyer_ref != ctx.buyer_ref → 404`), a token minted for a known/guessable `buyer_ref`
reads and acts as that buyer. This is the same family as the two findings the review brief
cites (the once-unsigned cookie that minted for another `buyer_ref`; the scenario key once
attached to mint). The console proxy learned the lesson and blocks `/v1/demo/*` with a 404;
the buyer proxy had not.

A same-origin `fetch` is required (the cross-site-write guard holds), so the practical
exploit is **XSS amplification**: a same-origin script turns into a durable (8-hour),
portable, identity-chosen token, exfiltratable to a server that can reach the API base —
which in the local/demo model is anyone, and in a hardened deploy is bounded by API
reachability. The parallel review of capability boundaries independently confirmed the
impact: a session minted with `buyer_ref=alice` reads alice's checkout in full, so the
chosen-`buyer_ref` token is account takeover where refs are known, not "an anonymous token
anyone could mint."

**Fix** (`apps/buyer-web/src/app/api/backend/[...path]/route.ts`): the handler refuses a
`v1/demo` path before it forwards or mints on the browser's behalf; the compare is
lower-cased so a future case-insensitive upstream route cannot be reached past the guard.
The handler still mints on its *own* behalf through `mint()` (a direct upstream call, not
this path), so nothing legitimate is lost.

**Verified after the fix** (production `next build` + `next start` of this worktree):
`POST /api/backend/v1/demo/sessions → 404` ("Sessions are minted by this storefront's own
server, not on a page's behalf"), the `v1/Demo` case variant `→ 404`, ordinary proxying and
`GET /api/backend/session` (no `token` field) unchanged.

**Test:** `packages/commerce-api/tests/test_proxy_mint_guards.py` asserts, at the source,
that **both** proxies refuse a `v1/demo` path *before* the upstream forward — the invariant
"no browser-facing proxy proxies the session-mint route." (A JS runtime test is out of scope
here: the frontend test directories belong to other build units, and the Python suite cannot
drive the Next runtime.)

### 1.2 Medium — order routes were a cross-buyer existence oracle and leaked `checkout_id`

**Claim tested (`routers/orders.py` docstring):** "A missing order and somebody else's order
are both 404, because a 403 would confirm which order identifiers exist."

**Falsified.** `read_order` and `create_refund` called `refund_service.load_order` (scoped by
tenant only) and then `assert_owner(order.checkout_id)`. For an order that exists in the
tenant but belongs to another buyer, the two branches diverged:

```
# another buyer's real order id            →  404 {"title":"Checkout not found",
#                                                    "checkout_id":"01a06fdc-26ea-…"}   ← leaks a cross-buyer id
# a random (non-existent) order id          →  404 {"title":"Order not found",
#                                                    "order_id":"752036d2-…"}
```

Two distinguishable 404s (an existence oracle for order ids across buyers in one tenant) and
disclosure of the order's `checkout_id`. Order ids are uuid7, so this is an oracle for ids an
attacker already has (a receipt, a link, a log line) plus an identifier leak — not a blind
brute force. The checkout/basket routes did **not** have this flaw (a missing and a non-owned
checkout both answer `404 "…not found"` echoing only the id asked for).

**Fix** (`routers/orders.py`): a new `_assert_order_owner` translates an ownership failure
into the *identical* response `load_order` raises for a missing id — same title, same detail,
the order id echoed and never the checkout's. Applied to both the read and the refund path.

**Tests:** `test_capi_sec_orders_oracle.py` builds a real alice-owned checkout, seeds the
captured order onto it, and asserts a *different* buyer sees byte-identical bodies for that
order and a random uuid (normalising only the echoed id and the RFC 9457 `instance` path),
that no `checkout_id` appears, on both `GET` and `POST …/refunds`.

### 1.3 Medium — outbox revive had no actor guard (an agent could re-drive money)

**Claim tested (threat model, capability boundaries):** an AGENT "may not approve, reject,
cancel or request a refund … consent is not delegable"; money moves only through the kernel.

**Falsified for the revive control.** `POST /v1/ops/outbox/{id}/revive` was gated only by the
router-level `X-Scenario-Key` and did not consult the actor at all — the handler did not even
take the session context — whereas `POST /v1/ops/safe-mode` explicitly refuses an AGENT. A
`DEAD` outbox row is almost always a `PAYMENT_CREATE_ORDER` or `REFUND_EXECUTE` command, so
reviving one re-drives a money operation under its existing grant. An AGENT session presenting
the demo scenario key reached the handler (proven at the auth gate: a random id returned a
`409` usage error, not a `403`), which contradicts the parity the Safe Mode switch enforces.

**Fix** (`routers/ops.py`): `revive_command` now takes the session context and refuses
`ActorType.AGENT` before anything is touched, exactly as `set_safe_mode` does. The scenario
key is the operator *apparatus*, not an *identity*.

**Tests:** `test_capi_sec_ops_revive.py` asserts an AGENT holding the scenario key is refused
`403 "Reviving a command is an operator control"`, and that an OPERATOR with the same key is
**not** turned away (the fix does not over-block).

### 1.4 Low — a malformed body could crash the validation handler into a 500

**Falsified robustness claim.** `on_request_validation` passed `exc.errors()` straight into
the problem body. When a body cannot be parsed at all — JSON sent without
`application/json` — Pydantic records the offending value in each error's `input` as the raw
request **bytes**, which `JSONResponse` cannot serialise, so the 422 handler itself raised and
the caller got an unhandled `500`:

```
PUT /v1/baskets/{id}/lines/AMUL-DAIRY-001   (body {"quantity":1}, no application/json)
→ 500 Internal Server Error
```

Not on the admission/webhook decision path and no data disclosed, but a client-triggerable
5xx from a trivial request.

**Fix** (`errors.py`): a `_jsonable_errors` helper routes the error list through
`fastapi.encoders.jsonable_encoder` (as FastAPI's own default handler does), decoding bytes
with replacement so even a non-UTF-8 body cannot crash it; `loc`/`msg`/`type` are unchanged.
Applied at both the `RequestValidationError` handler and the `ValidationError` problem path.

**Tests:** `test_capi_sec_validation_bytes.py` covers the helper directly (a bytes `input`
becomes serialisable) and end to end (the wrong-content-type request is a clean `422`).

---

## 2. Claims tested that held

### 2.1 The two proxies

| Attack | Result |
| --- | --- |
| Header smuggling (`Authorization`, `X-Scenario-Key`, `Host`, `X-Forwarded-*`, `Cookie`) | **Held.** Only the 5-name request allowlist crosses; `Authorization` (and, console-side, `X-Scenario-Key` on allowlisted paths) is hard-set after. A smuggled valid scenario key did not widen a buyer read (`scope=own`), and a smuggled bogus `Authorization` was overridden. |
| Path traversal / host pivot (`http:%2f%2fevil.com`, `@evil.com`, `%2f%2f`, `%5c`, `%00`, double-encoding) | **Held.** Each segment is `encodeURIComponent(decodeURIComponent(...))`, so `/ : @ \ NUL` cannot re-form an authority; the `url.origin !== API_BASE.origin` recheck is an unreachable backstop. Every attempt stayed on `localhost:8000` and 404'd. Only Next-normalised `..` reaches sibling **same-origin** public paths (`/openapi.json`, `/healthz`), which cross no origin and need no auth. |
| Cross-site write (`Origin: evil`, `Sec-Fetch-Site: cross-site`, neither) | **Held.** All refused `403`; only `same-origin`/`none` accepted. `Sec-Fetch-Site: none` is not cross-site-forceable, and the console additionally requires a pre-existing signed operator cookie on writes (`401` without one). |
| Forged / replayed / cross-proxy cookie | **Held.** A bad HMAC tag is discarded whole (length-checked before compare); a forged identity mints a fresh anonymous/operator session instead; a genuine `acr_session` presented as `acr_operator` (or vice-versa) carries no identity across (distinct per-process secret and cookie name). |
| Bearer / scenario-key leak in bodies or headers | **Held** except finding 1.1. `GET …/session` has no `token` field; the console blocks `/v1/demo/*` (`404`); `local-demo-scenario-key` never appears in any proxied body/header scanned. |
| Scenario-key allowlist — **the key widens reads only** | **Held.** Every `scenario_key_ok` (`Operator`) dependency in the API is on a **GET**; no write route reads the header. So the console's no-slash prefixes (`v1/orders`, `v1/refunds`) attaching the key to a `POST …/refunds` grant nothing — an operator lacks `refund.request`, and the refund/approve/cancel services never consult the key. Confirmed live: operator `POST …/approve → 403`, `POST …/refunds → 403`. `/v1/review/` (the prefix that "fell behind" once) is now covered. |

### 2.2 Capability boundaries over HTTP

Every money mutation was refused for the wrong actor: an AGENT is `403` on approve, reject,
cancel, refund and `payment.verify` (and correctly *allowed* `checkout.submit_approved`); an
OPERATOR is `403` on the same money routes. Capability checks are enforced service-side
(`payment.verify` in `payment_service`, `refund.request` in both router and service, the
admission actions in `admission_service`). Ownership on checkout/basket routes returns `404`,
never `403`, and a real-but-other-buyer checkout id is byte-indistinguishable from a random
one.

### 2.3 Tenant isolation

With a second tenant seeded, cross-tenant read and write were `404` by every route tried;
a `tenant_id` in the body and `X-Tenant-*`/`Host` spoof headers were ignored (the session's
tenant won). At the database, tested as the **real** `NOSUPERUSER NOBYPASSRLS` app and kernel
roles (a superuser connection would pass vacuously): an unset `app.tenant_id` returns zero
rows (fail-closed), and a kernel INSERT labelled with another tenant is refused by the
`WITH CHECK` policy. `tenants` and `api_sessions` are deliberately outside RLS (keyed lookups
that precede tenant binding); neither exposes commerce data.

### 2.4 Idempotency and replay

Same key + different body is refused (`422 IdempotencyKeyReuseError`) on both a head mutation
(`PUT …/lines`) and the admission path (`approve`); same key + same body replays with
`Idempotent-Replayed: true`. On the admission path a refusal is a **200 decision**, not a 4xx
(`submit` of a superseded version → `200 REAPPROVAL_REQUIRED`/`STALE_CHECKOUT`; `cancel` of a
checkout with an open payment → `200 PAYMENT_PENDING`; duplicate submit → `200
DUPLICATE_OPERATION` naming the single winner). `approve`/`reject` legitimately return `409`
on a hash/state mismatch — they are the consent-recording step, not the kernel admission, so
this is correct, not a violation.

### 2.5 Webhooks

Forged or missing signature → `401`, nothing stored (0 inbox rows); a valid delivery replayed
byte-for-byte → `200 duplicate:true` on the same row (at-most-once), which is a 200 and not a
4xx; a forged pre-claim cannot suppress the genuine event; a >256 KiB body → `413` *before*
the signature check; the API key secret does not verify a webhook (distinct secret). Order is
raw-body-cap → constant-time HMAC → tenant-from-slug → claim, exactly as documented.

### 2.6 CSP actually served, and secrets in bundles

Both apps serve a coherent, nonce-based CSP — `default-src 'self'`, `object-src 'none'`,
`base-uri 'none'`, `frame-ancestors 'none'`, `form-action 'self'`, Razorpay origins allowed
only on `/checkout`. The historical `'strict-dynamic'` bug (which voided the neighbouring
`'self'` and blocked every script) is **gone**: no `strict-dynamic` on any route, the CSP
nonce matches the `x-nonce` header and the script tags, and the page's scripts load. The
console is strictly tighter (no `payment` permission, `COOP: same-origin`, `connect-src
'self'`). Isolated production builds of both apps were grepped for secrets by value (the real
`.env` credentials) and by pattern: **no** bearer token, scenario key, Razorpay secret, webhook
secret or cookie secret in any client chunk. Those secrets live only in the server route
handlers or the Python API; the sole browser-exposed Razorpay value is the public key,
delivered at request time, not baked into the bundle.

### 2.7 Redaction

`GET /v1/config` (unauthenticated) returns only booleans and a public key prefix
(`key_id_prefix: "rzp_test_"`, `webhook_secret_configured: true`) — no secret value.

---

## 3. Latent gaps and accepted risks (documented, not changed on a hunch)

- **The scenario-key allowlist drifts by construction.** `/v1/protocols/conformance` is gated
  on the key at the API (`401` without it, `200` with it) but is **not** in the console
  proxy's `SCENARIO_KEY_PATHS`, so the console reaches it as `401` — the same shape as the
  `/v1/review` bug the brief cites, but **fail-closed** (an availability gap, not a breach),
  and no console page calls it today. Recommend adding `v1/protocols/conformance` to the list
  and, more durably, deriving the list from the API's own gate rather than maintaining a
  parallel copy. Left unchanged because it is not a security defect and the console surface
  belongs to another build unit.

- **The console demo-block is case-sensitive.** `suffix === "v1/demo" ||
  suffix.startsWith("v1/demo/")` is bypassed by `v1/Demo/sessions`, which today hits a
  case-sensitive FastAPI `404` and mints nothing — latent only. The buyer-proxy fix in 1.1
  lower-cases its compare; the console would benefit from the same one-line hardening.

- **`buyer_ref` is the sole, client-chosen, unauthenticated ownership key.** Anyone may mint a
  session with `buyer_ref=alice` (no secret, no scenario key) and then read/approve/cancel as
  alice. This is an intended affordance of the demo `/v1/demo/sessions` ("no sign-up, no
  password"), and it is exactly why finding 1.1 matters: exposing a chosen-`buyer_ref` mint to
  the browser turns a demo convenience into an impersonation primitive. Before anything
  resembling production, `buyer_ref` must be bound to an authenticated identity rather than
  taken from the request.

- **`/v1/ops/*` trusts the scenario key as operator access, not an identity.** A BUYER session
  presenting the key reads (and could arm) Safe Mode; the actor-specific refusal exists only
  for AGENT (now on both `set_safe_mode` and `revive`). This is a deliberate property of the
  demo operator surface; the fix in 1.3 restores the AGENT parity, and binding the ops surface
  to an authenticated OPERATOR identity is the production step.

- **Two cosmetic source-vs-served notes** (from the CSP review): a stale comment in
  `payment-panel.tsx` mentions `strict-dynamic` that no longer exists, and the live *dev* CSP
  is looser (`'unsafe-inline'`/`'unsafe-eval'`) than the stricter policy the production build
  emits — audit the served *production* header, not the dev one.

---

## 4. Blind spots — what could not be tested, and why

- **The proxy fix has no JS runtime test.** The frontend test directories (`apps/*/e2e`,
  `apps/*/src/**/*.test.*`) belong to other build units and the Python suite cannot drive the
  Next runtime, so the regression guard is a source assertion (both proxies refuse `v1/demo`
  before forwarding) plus a manual production `next start` verification. A Playwright test that
  drives the real handler would be stronger and belongs to the storefront's own suite.

- **The storefront build was verified with webpack, not Turbopack.** Building inside a git
  worktree needs a symlinked `node_modules`, which Turbopack rejects ("points out of the
  filesystem root"); `next build --webpack` compiled the whole app, including the modified
  proxy route, cleanly. The gate's Turbopack build will run in a normal checkout at merge.

- **The full webhook effect (capture → order write) was not reached over HTTP.** Synthetic
  events had no matching local payment attempt, so the worker marked them `IGNORED`. Verify →
  dedupe → inbox → outbox enqueue → worker run were all exercised; the capture-to-order write
  was not. Finding 1.2's test seeds the captured order directly for the same reason.

- **"413 before buffering the whole body" is confirmed by behaviour and by source
  (`security.read_capped_body` streams and raises at the cap), not by measuring server
  memory** — that cannot be observed externally.

- **Cross-tenant probing is within one process.** Only the local stack exists; a
  multi-replica or per-tenant-credential deployment (ADR D7's accepted risk) was out of reach.

- **Live fixes were verified against this worktree, not the running services**, which serve
  the main checkout. The running-service probes therefore document the *pre-fix* behaviour
  faithfully; the fixes are proven by the suite and the spare-port storefront run and take
  effect on the running services when this branch merges.

- **6 suite skips are environmental** (`voice-runtime` real-audio tests need
  `GOOGLE_CLOUD_PROJECT`), so those voice paths were not exercised here.

---

## 5. Reproduction

```bash
export PATH="$HOME/.local/bin:$PATH"
# private test DB (all four overrides; the _WORKER one guards against a worker crossing DBs)
createdb -T commerce_ci_baseline commerce_test_s
psql commerce_test_s -f scripts/bootstrap_test_roles.sql
(cd packages/platform-db && DATABASE_URL=postgresql+psycopg://$USER@localhost:5432/commerce_test_s uv run alembic upgrade head)
psql commerce_test_s -f scripts/bootstrap_test_roles.sql   # re-assert grants after migration
export DATABASE_URL_TEST_APP=postgresql+psycopg://commerce_test_app:testpw@localhost:5432/commerce_test_s
export DATABASE_URL_TEST_KERNEL=postgresql+psycopg://commerce_test_kernel:testpw@localhost:5432/commerce_test_s
export DATABASE_URL_TEST_WORKER=postgresql+psycopg://commerce_test_worker:testpw@localhost:5432/commerce_test_s
export DATABASE_URL_TEST_ADMIN=postgresql+psycopg://$USER@localhost:5432/commerce_test_s

uv run ruff check packages && uv run ruff format --check packages
uv run mypy packages/*/src
uv run pytest packages/ -o addopts="" -q      # 4521 passed, 6 skipped

# the four new regression suites specifically
uv run pytest packages/commerce-api/tests/test_capi_sec_orders_oracle.py \
              packages/commerce-api/tests/test_capi_sec_ops_revive.py \
              packages/commerce-api/tests/test_capi_sec_validation_bytes.py \
              packages/commerce-api/tests/test_proxy_mint_guards.py -o addopts="" -q
```
