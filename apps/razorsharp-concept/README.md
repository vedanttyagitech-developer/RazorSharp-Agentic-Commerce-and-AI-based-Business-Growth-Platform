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
npm run dev -- --port 3000
```

Run the platform API on port 8000, Action Executor and voice gateway on port 8100 with the root repository's development configuration. The server-side bridges default to these local addresses. `COMMERCE_API_URL`, `VOICE_GATEWAY_URL` and `COMMERCE_TENANT_SLUG` override those defaults. Provider secrets belong only on the backend.

The development session bridges are disabled in production unless `RESERVE_LOCAL_DEMO=true` is explicitly set for a controlled demo. That switch is not production authentication. Do not expose the local demo as a production financial service.

```sh
node --test tests/*.test.mjs
npx tsc --noEmit
npm run build
```

Local source upload and external hosting were declined; no deployment is part of this release check. See `../../docs/implementation/` for dated verification evidence and remaining limitations.
