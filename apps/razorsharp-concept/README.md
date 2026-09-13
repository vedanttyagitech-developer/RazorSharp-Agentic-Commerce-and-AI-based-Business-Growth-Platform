# RazorSharp frontend

The current buyer copilot and Merchant Command frontend, included directly in the platform repository. Platform branding is RazorSharp; the assistant is Razor AI.

## Current integration

- `/`: platform overview and capability showcase.
- `/shop`: backend catalogue discovery, durable cart, exact bill review, manual Razorpay Checkout, Reserve Pay simulation, order tracking, payment acknowledgements and merchant support escalation.
- `/merchant`: simulated business dashboard and growth tooling, plus backend-connected order/support/refund controls where configured. Sample analytics are not live merchant reporting. Gmail execution is not implemented.
- Voice connects to the voice gateway for transcription, agent turns, grounded speech and checkout guidance. Browser microphone permission is required. Automated generated-audio coverage does not establish physical microphone quality.
- Reserve Pay uses backend authority, admission and executor paths with a simulated provider. It is not a live NPCI UAP integration.
- Buyer support escalates an issue to the merchant. Buyers do not initiate or approve refunds.
- `/voice` and `/cart` are retired; these capabilities live inside `/shop`.

## Local run

Use Node 22.13 or later. From this directory:

```sh
npm ci
npm run dev
```

Open http://localhost:8000/shop/ or http://localhost:8000/merchant/. Both `npm run dev` and `npm start` launch the mounted frontend/API, Action Executor and voice gateway together using the root repository's development configuration. The API defaults to port 8000 and voice to 8100. Configure database and provider credentials in the root `.env`; provider secrets belong only on the backend. `npm start` reuses the built frontend. For an intentionally API-only session, run `bash scripts/run_mounted_demo.sh --api-only` from the repository root.

The development session bridges are disabled in production unless `RESERVE_LOCAL_DEMO=true` is explicitly set for a controlled demo. That switch is not production authentication. Do not expose the local demo as a production financial service.

```sh
node --test tests/*.test.mjs
npx tsc --noEmit
npm run build
```
