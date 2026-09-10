# RazorSharp

A voice-oriented commerce platform with a buyer copilot and Merchant Command. Razor AI assists discovery and proposes actions; the Transaction Trust Kernel validates financial execution against buyer authority, the reviewed bill and sale-bound terms.

## Boundaries

- Buyer UI: catalogue, durable cart, reviewed checkout, orders and support escalation.
- Manual payment: Razorpay-hosted Checkout with backend provider verification and reconciliation.
- Reserve Pay: backend-governed delegated spending with a simulated provider; not live NPCI UAP certification or integration.
- Merchant workspace: simulated business/growth views and backend-connected support/refund controls. Gmail campaigns are not live.
- Refunds: buyer case → merchant review/approval → Kernel admission → executor/provider evidence. The buyer copilot cannot approve a refund.
- Protocol adapters do not bypass Kernel admission. Model speech and browser callbacks are not proof of payment.

## Run locally

Use Python 3.14, uv, PostgreSQL and Node 22.13 or later. Start with `.env.example`; keep credentials outside Git.

```sh
uv sync --all-extras --dev
make bootstrap
make seed
make demo
```

In another terminal:

```sh
cd apps/razorsharp-concept
npm ci
npm run dev -- --port 3000
```

Voice additionally requires the voice gateway and configured speech credentials. See [demo runbook](docs/DEMO.md) and [frontend setup](apps/razorsharp-concept/README.md). Older implementation notes describe earlier versions; dated verification reports record what was actually checked.

## Validation

```sh
make gate
```

Backend packages run in separate pytest processes because they have package-local fixtures. CI requires the test database, and runs the current frontend's regression tests, TypeScript check and production build. Network-dependent voice and Razorpay tests are separate credentialled checks, not silently treated as offline passes.

See [release verification](docs/implementation/release-readiness-2026-09-10.md) for results and remaining limitations. This is a development/test-mode project, not a claim of production certification or zero defects.
