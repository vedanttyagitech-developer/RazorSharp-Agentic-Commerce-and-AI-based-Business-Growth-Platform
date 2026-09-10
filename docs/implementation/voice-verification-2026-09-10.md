# Voice verification — 2026-09-10

## Verified

- Live generated speech through real Gemini STT, commerce agent and Gemini TTS: English, Hindi and Hinglish each passed two consecutive searches (milk, then bread), with nonempty product items, speech completion and playback release in the same pipeline. Initial run: 3 passed, 119.06 seconds total. MemoryTransport carries pipeline frames; this is not physical microphone or browser socket verification.
- Browser microphone connected and remained Listening. A typed Hindi request with voice connected rendered five actual milk catalogue products, a Hindi reply and returned to Listening. This browser check did not test speech recognition or independently confirm audible playback.
- 88 focused language/money, checkout guidance, echo gate and latest-intent tests passed.
- Hindi payment-choice parser previously missed रेज़रपे / रेजरपे and रिजर्व. Normalized nukta and added Razorpay Hindi spelling recognition. Frontend integration suite: 36 passed; TypeScript and lint checks passed. Negated and ambiguous choices still request clarification.

## Failures retained in the live regression test

The third contextual request (two packets of the previously shown bread) returned no basket proposal in all three languages. The test now verifies offer_is_proposal, quantity 2 and a SKU from the preceding bread results. These assertions currently fail; no assertion was removed to make the suite green.

An English diagnostic rerun returned the deterministic fallback text beginning “The reasoning layer is unavailable”. API log at 2026-09-10T01:13:58.685Z explicitly recorded TimeoutError and deterministic fallback. This proves an agent timeout on that rerun, not a universal explanation for every failed run.

## Latency evidence

- One recorded agent request made three sequential model calls from 01:12:35.571 to 01:12:50.433 UTC: about 14.9 seconds, before voice synthesis.
- Diagnostic English search turns took 21.95 and 14.67 seconds from input audio start through complete reply synthesis. These include input audio and endpointing silence; they are NOT time-to-first-audio measurements and exclude human playback duration.
- Agent adapter uses StreamingMode.NONE. Voice awaits the complete HTTP agent response before speaking.
- Gemini TTS uses generate_content, returns complete audio per synthesis phrase. Existing phrase splitting and lookahead exist, so it is inaccurate to claim no pipelining at all. Provider audio chunks are not streamed as generated.

## Still incomplete

- Real physical microphone three-language interaction and perceived playback quality.
- Contextual spoken cart addition across the frontend, because the underlying proposal test is failing.
- Hindi/Hinglish checkout guidance: backend guidance explicitly emits English text and en-IN.
- Per-stage production timing (speech end to final transcript, agent/tool duration, first audio received, actual playback start), needed for reliable latency targets.

No payment was initiated by these voice tests. Payment/refund execution logic was not changed.

## Discovery latency improvement (same day)

Implemented an opt-in production bridge fast path for complete, recognized category discovery requests. It makes exactly one capability-gated catalogue search, returns real result cards and a short server-authored localized reply, without model calls or cached price/stock. Complex constraints, comparisons, mutations and ambiguous references remain on the reasoning path. Scenario reasoning faults still retain their original precedence.

Recent discovery retains up to five product IDs per principal for up to five minutes, bounded to 256 entries. The next model turn receives these as display-context references, not current facts or authorization. The model must reread product state and clarify ambiguity. This context is process-local and not durable across restarts.

Live API samples: English 82 ms, Hindi 15 ms, Hinglish 12 ms, subsequent bread 12 ms, earphones 13 ms. All returned direct_catalogue_discovery, one tool call and actual catalogue hits.

Real generated-audio pipeline, measured from final STT transcript:

| Sample | Product frame ready | First audio frame ready |
| --- | ---: | ---: |
| English | 60 ms | 2,931 ms |
| Hindi | 21 ms | 2,024 ms |
| Hinglish | 99 ms | 2,854 ms |

The observed Hinglish transcript was “अब ब्रेड ऑप्शंस दिखाओ प्लीज।” Before adding that exact supported grammatical variant it still went through the model (10.6 s to cards); after the correction it used direct discovery. These small local samples are not p95 measurements, do not include speech endpointing before final transcript, and do not measure physical speaker playback. No provider audio streaming change was needed for this initial latency reduction.

Browser check: “Show me milk products.” rendered five actual catalogue product cards and the short reply on /shop. No simulated frontend timing delay was introduced.

Validation: 40 discovery + existing bridge tests passed; after adding the observed Hinglish case, the focused discovery suite passed 19 tests. Ruff and git diff --check passed. Prior contextual cart-add failures and English-only checkout guidance remain separate outstanding issues; this work does not claim to fix those.

## Follow-up: cart, comparisons and checkout language

### Cart regression corrected and verified

The earlier third-turn test had no durable cart and referred to “that bread” after five options. Those failures alone did not establish a broken cart implementation. The corrected test creates the cart as the UI does and explicitly chooses the first displayed bread. All three real generated-speech cases passed (English/Hindi/Hinglish, 172.57 s total), with exact quantity 2 and a SKU from the preceding results.

Browser test: Show bread → “Add two packets of the first bread shown to my cart” → “Added 2 · Britannia Brown Bread 400 g” and cart count 2. Navigate away and back → cart count still 2. Separate live ambiguous request “Add two of those breads” returned a clarification listing five options, with no proposal. No financial action was taken.

### Comparison card defect fixed

The bridge previously retained only the last payload per read kind and iterated a frozenset to choose the output. This could lose or inconsistently choose products after present_products. It now records chronological read order and preserves the exact product cards explicitly returned by present_products. Product facts still come through gated reads; proposal projection remains separate.

23 bridge tests passed, including a new two-product presentation regression. Live English/Hindi/Hinglish price-and-pack-size comparisons returned Britannia Brown Bread 400 g at INR50 and Sandwich White Bread 400 g at INR40 with both cards, no grounding corrections or denials. Browser showed both cards and both prices. These model comparison samples took 13.0–15.44 seconds; this work fixes correctness, not the remaining comparison latency. Unknown attributes are not supplied by this bridge.

### Spoken checkout localization implemented

The voice pipeline now carries the established conversation locale into deterministic checkout guidance. Hindi/Hinglish conversation uses Hindi speech; English uses English. Review, Reserve Pay simulator wording, manual Razorpay guidance, changed-bill refusal, unresolved payment, failed/expired checkout and confirmed order are localized. Outcome and amount still derive from the buyer-authenticated checkout read. Guidance never opens consent or records approval.

Three real generated-speech checkout tests passed (53.44 s): correct locale, exact reviewed amount, unchanged APPROVAL_REQUIRED state and no consent frames. 71 focused gateway/pipeline/guidance tests passed. Final targeted run: 18 passed, 7 wire-contract tests skipped without their live configuration. Ruff and diff checks passed. Physical microphone quality and all-browser checkout transitions remain separate from these pipeline tests.
