# The pitch video

The five-minute Track 1 pitch for RazorSharp, written as a [Remotion](https://remotion.dev)
composition rather than recorded in a slide tool — so every frame is code, every number on
screen traces to a file in this repository, and a re-cut is a diff instead of a re-record.

**Exactly 9000 frames at 30fps = 5:00**, which is the submission limit. The duration is
*derived* from the chapter table in `src/Pitch.tsx`, so a scene that gets re-timed cannot
silently leave the video at 4:58 or push it over.

## Run it

```bash
cd docs/pitch-video
npm install
npm run studio        # opens the Remotion editor, scrubbable, hot-reloading
```

```bash
npm run render        # out/razorsharp-pitch.mp4 — h264, crf 17, ~5 min to render
npm run render:fast   # half-scale, crf 28 — for checking timing, not for submitting
npm run still -- --frame=3150   # a single frame as PNG
npm run types         # tsc --noEmit; kept clean
```

Rendering downloads a headless Chrome on first run. Nothing else is needed — the two
typefaces are bundled through `@remotion/google-fonts`, deliberately, so a render on a
laptop and a render in CI produce the same pixels.

## The running order

Nine minutes of material had to become five, and the cut is an argument rather than a tour:

| At | Chapter | Length | Doing what |
|---|---|---|---|
| 0:00 | The claim | 32s | Concede that an AI can shop. Name the half nobody has. |
| 0:32 | The idea | 35s | Agent proposes, person approves, kernel authorizes — and the one function. |
| 1:07 | Capability absence | 30s | Why the model cannot pay: no row, and a gate that compares closures. |
| **1:37** | **A live refusal** | **60s** | A price change that leaves the total identical, caught and explained. |
| 2:37 | Exactly once | 45s | Commit-before-send, one winner by index, unknown ≠ failed. |
| **3:22** | **Proof** | **55s** | Ten links, fifteen checks, rebuilt on every request. |
| 4:17 | Breadth | 25s | Scale — late and short on purpose. |
| 4:42 | Close | 18s | One sentence and a URL. |

The two bold beats take **115 of the 300 seconds**. They are the only two a competing
submission cannot also claim, so they get more than a third of the runtime. Breadth is last
and shortest because "is there anything behind this?" is a question, not a pitch.

## What is on screen is checkable

Every figure in the video came out of the code, not out of a deck. If a judge greps for it,
it is there:

| On screen | Where it comes from |
|---|---|
| The nine numbered gates in `admit()` | `transaction-kernel/src/transaction_kernel/admission.py` |
| `catalog.search`, `basket.propose_line`, … | Registry A in `agent_runtime/capabilities/registry.py` |
| `checkout.approve`, `payment.execute` absent | no row in any specialist action table |
| ₹73.00 and ₹28.00 | real list prices in `merchant_sim/catalogue.py` |
| `lines[SKU].unit_minor`, `delivery_fee_minor` | the closed vocabulary in `transaction_kernel/material.py` |
| `uq_payment_attempts_one_non_terminal` | the partial unique index, checked by SQLSTATE + constraint name |
| The fifteen check names | `commerce_api/services/proof_chain.py` |
| The four spans | `SPANS` in `apps/razorsharp-concept/components/transaction-kernel.tsx` |
| `order_Ta6DZyvxhDtIS0` | a real Razorpay test-mode order created by the Action Executor |

The one thing on screen that is *illustrative* rather than recorded is the refusal scenario
itself — a ₹234.00 bill where the 1 L milk rises ₹12.00 and delivery drops ₹12.00. The SKUs
and their prices are real and the delta field paths are the real ones; the arithmetic is
constructed so the total is identical before and after, because that is precisely the case a
total-only comparison lets through. It reproduces against a live shop.

## How it is built

Six primitives in `src/ui.tsx` and nothing else — a scene frame, a rising line, a typed line,
a code block, a panel, a field, a tick. A pitch that invents a new visual idiom every twenty
seconds makes the viewer keep re-learning what they are looking at, and a judge watching a
dozen of these has no patience for it.

The palette in `src/theme.ts` spends two colours on exactly one meaning each:

- `refuse` **#FF6B5A** — the kernel declining something. Never decorative.
- `prove` **#3FD9A4** — a check that passed. Never decorative.

A viewer learns that in the first thirty seconds and can then read the rest of the video by
colour alone.

```
src/
  index.ts        registerRoot
  Root.tsx        the composition — 1920×1080, 30fps, duration derived from Pitch
  Pitch.tsx       the chapter table, the timeline, the progress rail
  theme.ts        palette, type scale, seconds()
  fonts.ts        Inter + JetBrains Mono, loaded not merely named
  ui.tsx          the six primitives
  scenes/         one file per chapter
```

## Narration

`NARRATION.md` carries the voice-over, timed beat by beat with a word budget for each. It is
written so that **it never reads the slide** — the frame already carries its own words, so
the voice says the thing the frame cannot.

Record it separately and lay it under the render; there is no audio in the composition, so
the video can be re-cut without re-recording and the narration re-read without re-rendering.
