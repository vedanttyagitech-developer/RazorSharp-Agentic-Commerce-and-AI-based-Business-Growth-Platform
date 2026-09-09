# RazorSharp homepage: highlights and innovations

Scope: homepage storytelling and presentation only. Preserve buyer/merchant application handlers, payment state, Reserve Pay UI and parallel implementation work. Keep local; no publication.

## Narrative and layout

1. Keep the existing identity hero. Add header anchors to Highlights and Innovations; preserve access to both copilots.
2. Retain the multi-category shopping photograph and merchant entrance. Remove unsupported percentage-as-impact framing.
3. Highlights: lead with the merchant/buyer problems, explain intended value without invented results. Six concise challenge cards.
4. Merchant growth: interactive five-step story, from observed catalogue/business data to reviewed action and measured outcome. Clearly identify this as a product-flow illustration, not a live campaign or analytics result.
5. Innovations: interactive illustrative transaction scenarios (exact purchase, changed offer, repeated request, revocation). Explicitly distinguish illustrations from backend verification. Never trigger a payment or change real data from this homepage.
6. Architecture: two copilots with role cards and current maturity. Shopping model-backed when configured; Checkout/Support deterministic; merchant workspace includes preview flows. Voice gateway is implemented, runtime availability is configuration-dependent. Voice is not authority.
7. Evidence: ten expandable evidence links matching proof_chain.py, not an invented Merkle chain. Historical content and audit verification, missing evidence stays missing. Link to repository source.
8. Protocols: ACP, UCP, MCP/AP2 and Reserve/UAP with explicit boundaries. Adapter support is not distribution or certification. Reserve is simulated provider, not live NPCI debit.
9. End with clear routes to Shop, Merchant Command and public source. Runtime states and performance claims must not be fabricated.

## Visual design

Scoped homepage CSS; graphite/white surfaces, cobalt navigation, violet AI, orange/yellow attention, red refusal. Existing photograph supplies multi-category art. Lucide icons supply architecture semantics. Staggered reveal through IntersectionObserver, animated connector flows, transaction transition states, hover elevation, sticky evidence detail on wide screens. No auto-approval and no arbitrary live counters. Pause Motion and prefers-reduced-motion suppress decorative animation; keyboard controls retain native button/link semantics. Mobile stacks grids and preserves every detail.

## Evidence reviewed 2026-09-10

- commerce_api/services/agent_bridge.py: BRIDGED_SPECIALISTS is Shopping; Checkout and Support deterministic.
- merchant_controller/__init__.py: typed proposals, canonical action hash, approval lifecycle; no financial authority.
- commerce_api/services/proof_chain.py: ten committed-record links; recomputed canonical hash and audit-chain verification.
- commerce_api/routers/protocols.py: pinned protocol matrix, disclaimers for ACP/ChatGPT and UCP/Gemini.
- commerce_api/routers/reserve.py: production simulator guard.
- voice-session.tsx and voice-runtime: gateway path exists; runtime status must not be inferred from this homepage.
- Razorpay agentic-payments public page: conversational commerce and pre-authorized spending are relevant product themes. Exact current Track 1 scoring rubric was not verified; do not claim full rubric coverage or endorsement.

## Acceptance

Header anchors work; native buttons select merchant/scenario/architecture states; all explanatory copy has honest status; no fake runtime proof; no core/shared financial edits; responsive CSS and reduced motion; TypeScript and production build. Browser visual testing only when requested. No deployment.
